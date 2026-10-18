"""ledger_replay.py — ``portfolio_history.jsonl`` 的唯一读法（calc 层 T0，ADR-026：禁 IO / 禁时钟）

``replay_ledger(records, alias=...) -> (List[Activity], LedgerState)``：把形状不统一的流水记录
（``buy``/``bought``/``sold``、``price`` 与 ``price_per_unit``+``total_amount`` 并存、
``funding_source=external_funding`` ……）**有状态地**折叠成统一的 :class:`Activity`。
所有读流水的消费方（账户业绩 / 决议↔成交匹配 / …）只认 Activity 的 ``cash_effect`` / ``units_effect`` /
``external``，不再各抄一份别名表（ADR-027 D2/D3）。PR1 纯重构：对老记录的解释与旧
``account_performance._ledger_events`` 逐字一致（``tests/test_ledger_replay_invariance.py`` 用旧实现做 oracle）。

归一表（老动作 → Activity）：

==============================  ==========  ==========================  ============================
老 ``action``                   action      cash_effect                 external
==============================  ==========  ==========================  ============================
``buy`` / ``bought``            buy         ``-amount``（子弹池现金）   ``funding_source=external_funding``
                                                                         时 True 且 cash_effect=0
``dca_reconcile_backfill``      buy         同上，source=dca_reconcile  同上（对账行必带 external_funding）
``dca_reconcile_reverse``       sell        同上（units 为负）          同上
``gift``                        gift        0                           True
``sell`` / ``sold``             sell        ``net_amount``，缺省 ``amount-fee``  False
``deposit`` / ``withdraw``      同名        ``±|金额|``                 True
``manual_override``             correction  0（无现金对手）             False，``units_effect`` = 新-旧
``delete_holding``              同名        0                           False，``units_effect`` = -旧
其余 / 没有可用日期             unknown     0（保留、计数、不进回放）
==============================  ==========  ==========================  ============================

``gift`` 不并进 ``buy``（ADR-027 D2 的新写入动作表里 gift 自成一类）：并了之后"赠股"和"我下的买单"
在 Activity 上不可区分，决议匹配会把它当成交。``manual_override`` 记的是**绝对值**，所以必须按日期序
折叠、从前序状态算增量——这就是回放是有状态的原因。

返回的 Activity 列表按日期升序（同日保持文件顺序，与旧实现的稳定排序一致）；需要文件顺序的读方按 ``seq`` 排。
``LedgerState`` 只用于回放（从零开始累计，**不是**当前快照，也不写回快照）：账本早于系统上线的部分没有记录，
所以它的 units/cash 与 ``portfolio.md`` 的差额正是 ``account_performance`` 的 ``unreconciled``。

价格口径核实（ADR-027 D4，决定 PR2 的 split / 分红回放怎么写；本 PR 不处理 split / 分红）
------------------------------------------------------------------------------
``db/market_data.db`` 的 ``daily_prices.close`` 存的是 **yfinance 复权价（auto_adjust=True：拆股 + 分红都复权）**：

- 抓取处全是 ``yf.Ticker(sym).history(period=...)``（``utils/exchange_fee.get_history_data`` 增量 5d / 全量 2y、
  ``scripts/backfill_history`` 的 ``period="max"``），全仓**没有**任何调用传 ``auto_adjust=False`` / ``actions``；
  yfinance 1.7.0 的 ``history()`` 默认 ``auto_adjust=True``。``exchange_fee`` 里的"复权拼接哨兵"注释也写明
  分红/拆股后 yfinance 会**全历史回溯重新复权**。场外基金走东方财富前复权净值（同口径，分红再投资）。
- 实测生产库（只读拷贝）：NVDA 2024-06-07 收盘 120.68（拆股前原始价约 1209，已 ÷10）、AAPL 2020-08-28 收盘 121.06
  （原始价 499.23 ÷4 = 124.81，再被分红复权压低到 121.06）——拆股和分红都已体现在历史价里。

对 PR2 的含义：
1. **split**：价格序列已按拆股后口径连续，所以回放的数量也必须在**所有历史日期**用拆股后口径（拆股前的数量 ×ratio
   重述）。不能在拆股日才把 ``units`` ×ratio —— 那样拆股前用"拆股前数量 × 复权价"低估 ratio 倍，拆股日 NAV 凭空跳 ×ratio。
2. **分红**：复权价路径本身已把分红当作再投资算进了收益；若 PR2 再把 ``dividend`` 的现金记进 NAV，该标的的
   TWR 会把同一笔分红算两次。PR2 必须二选一（对有分红活动的标的改读未复权价，或分红只做现金流分类、不再入 NAV），
   不能两头都入。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

from openinvest.core.schemas import Activity

_EXTERNAL_FUNDING = "external_funding"
# 老 action → 归一后的 Activity.action（买入类：cash/external 由 funding_source 决定）
_BUY_LIKE = {"buy": "buy", "bought": "buy", "dca_reconcile_backfill": "buy", "dca_reconcile_reverse": "sell"}
_RECONCILE = frozenset({"dca_reconcile_backfill", "dca_reconcile_reverse"})
_SELL_LIKE = frozenset({"sell", "sold"})
_CASH_ACTIONS = frozenset({"deposit", "withdraw"})
_EPS = 1e-9


@dataclass
class LedgerState:
    """回放累计状态（只用于回放，不写回快照）。units/avg_cost 按 symbol，cash 按币种。"""
    units: Dict[str, float] = field(default_factory=dict)
    avg_cost: Dict[str, float] = field(default_factory=dict)   # 近似加权均价（买入加权、卖出不变、清仓归零），不含汇率
    cash: Dict[str, float] = field(default_factory=dict)       # Σ cash_effect
    unknown: int = 0                                           # 未知 action / 无可用日期 的行数（保留在 Activity 列表里）


def _day(r: Mapping[str, Any]) -> str:
    return str(r.get("trade_date") or r.get("ts_origin") or r.get("ts") or "")[:10]


def _s(v: Any) -> Optional[str]:
    return None if v is None else str(v)


def _opt_f(v: Any) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _activity(seq: int, r: Dict[str, Any], alias: Mapping[str, str], held: Mapping[str, float]) -> Activity:
    """单条记录 → Activity。``held`` = 到这条为止的回放持仓（只给 manual_override / delete_holding 算增量用）。"""
    raw_action = str(r.get("action") or "").lower()
    raw_sym = str(r.get("symbol") or "")
    sym = alias.get(raw_sym, raw_sym)
    day = _day(r)
    units = float(r.get("units") or 0)
    price = float(r.get("price") or r.get("price_per_unit") or 0)
    amount = float(r.get("total_amount") or abs(units) * price)
    f: Dict[str, Any] = dict(
        symbol=sym or None, currency=str(r.get("currency") or "CNY").upper(),
        trade_date=day if len(day) == 10 else None,
        settle_date=_s(r.get("settle_date")), nav_date=_s(r.get("nav_date")),
        source=_s(r.get("source")), idempotency_key=_s(r.get("idempotency_key")), note=_s(r.get("note")),
        fee=_opt_f(r.get("fee")), ts=_s(r.get("ts_origin") or r.get("ts")), seq=seq, raw=r,
    )
    trade = dict(units=None if r.get("units") is None else units, price=price or None, amount=amount)
    if raw_action in _BUY_LIKE:
        sign = 1.0 if units >= 0 else -1.0
        ext = r.get("funding_source") == _EXTERNAL_FUNDING
        if raw_action in _RECONCILE:
            f["source"] = "dca_reconcile"
        f.update(trade, action=_BUY_LIKE[raw_action], units_effect=units, external=ext,
                 cash_effect=0.0 if ext else -sign * amount)
    elif raw_action == "gift":
        f.update(trade, action="gift", units_effect=units, external=True, cash_effect=0.0)
    elif raw_action in _SELL_LIKE:
        net = r.get("net_amount")
        f.update(trade, action="sell", units_effect=-abs(units),
                 cash_effect=float(net) if net is not None else amount - float(r.get("fee") or 0))
    elif raw_action in _CASH_ACTIONS:
        sign = 1.0 if raw_action == "deposit" else -1.0
        f.update(action=raw_action, amount=abs(units), external=True, cash_effect=sign * abs(units))
    elif raw_action == "manual_override":
        f.update(trade, action="correction", correction=True, units_effect=units - held.get(sym, 0.0))
    elif raw_action == "delete_holding":
        f.update(action="delete_holding", correction=True, units_effect=-held.get(sym, 0.0))
    else:
        f.update(action="unknown")
    return Activity(**f)


def _apply(state: LedgerState, a: Activity) -> None:
    state.cash[a.currency] = state.cash.get(a.currency, 0.0) + a.cash_effect
    if a.action in _CASH_ACTIONS:
        return
    sym = a.symbol or ""
    before = state.units.get(sym, 0.0)
    after = before + a.units_effect
    state.units[sym] = after
    if a.action in ("buy", "gift") and a.units_effect > 0:
        cost = a.amount or 0.0
        state.avg_cost[sym] = (state.avg_cost.get(sym, 0.0) * before + cost) / after if before > _EPS else cost / a.units_effect
    elif after <= _EPS:
        state.avg_cost.pop(sym, None)


def replay_ledger(records: List[Dict[str, Any]], *, alias: Mapping[str, str]) -> Tuple[List[Activity], LedgerState]:
    """流水记录 → (Activity 日期升序列表, 回放终态)。

    ``alias``：账本符号 → 持仓符号（如 ``GOLD-CNY`` → ``GC=F``），没有就传 ``{}``。
    每条输入记录恰好产出一条 Activity（含 unknown），所以 ``len(activities) == len(records)``。
    """
    state = LedgerState()
    out: List[Activity] = []
    # 稳定排序：同日保持文件顺序（旧实现同款）；seq 记原下标
    for seq, r in sorted(enumerate(records), key=lambda p: _day(p[1])):
        a = _activity(seq, r, alias, state.units)
        out.append(a)
        if a.action == "unknown" or a.trade_date is None:
            state.unknown += 1
            continue
        _apply(state, a)
    return out, state


def external_flow(a: Activity) -> float:
    """外部现金流（+入 −出，``a.currency`` 计价），进 TWR 分母 / XIRR；非外部 = 0。

    deposit / withdraw 就是 ``cash_effect``；external_funding 的买卖与赠股是新钱直接变持仓
    （``cash_effect`` = 0），流量 = ``±amount``（符号随 ``units_effect``）。
    """
    if not a.external:
        return 0.0
    if a.action in _CASH_ACTIONS:
        return a.cash_effect
    return (1.0 if a.units_effect >= 0 else -1.0) * (a.amount or 0.0)


__all__ = ["LedgerState", "external_flow", "replay_ledger"]
