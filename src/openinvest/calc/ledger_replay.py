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
``dividend``                    同名        ``amount - tax_withheld``   False（内部流：进 NAV 不进 TWR 分母）
``interest`` / ``return_of_capital``  同名  ``+amount``                 False
``fee`` / ``tax``               同名        ``-amount``                 False
``split``                       同名        0                           False，``units_effect`` = 拆股增量，``split_ratio`` = 新/旧
其余 / 没有可用日期             unknown     0（保留、计数、不进回放）
==============================  ==========  ==========================  ============================

新写入的记录（PR2 起，``schema_version == 2``）由写入方算好 ``cash_effect`` / ``units_effect`` / ``external``，
读方**只认这三个字段**（ADR-027 D2）：上表是缺这些字段时的推导口径。DRIP 的那条 buy 也是 v2 记录。
**唯一例外是 split 的 ``units_effect``**：写入方记的是快照口径（快照份数 × (ratio−1)），回放始终用账本口径
``held × (ratio−1)``（``held`` = 回放里该标的已累计的份数）——否则拆股后的 manual_override / delete_holding 会用错基数。
NAV 回放不受影响（split 在拆股后口径下是空操作）。

数值字段（units / price / total_amount / net_amount / fee）解析不了的行：``action`` 分类照旧、``unparsed=True``、
效果全 0，不进回放、计入 ``LedgerState.unknown``——一行脏数据不能让整个读方崩（旧 ``history_as_trades`` 从不转数字，
所以它不能因为别处的脏行而抛）。

``gift`` 不并进 ``buy``（ADR-027 D2 的新写入动作表里 gift 自成一类）：并了之后"赠股"和"我下的买单"
在 Activity 上不可区分，决议匹配会把它当成交。``manual_override`` 记的是**绝对值**，所以必须按日期序
折叠、从前序状态算增量——这就是回放是有状态的原因。

返回的 Activity 列表按日期升序（同日保持文件顺序，与旧实现的稳定排序一致）；需要文件顺序的读方按 ``seq`` 排。
``LedgerState`` 只用于回放（从零开始累计，**不是**当前快照，也不写回快照）：账本早于系统上线的部分没有记录，
所以它的 units/cash 与 ``portfolio.md`` 的差额正是 ``account_performance`` 的 ``unreconciled``。

价格口径与 split / 分红（ADR-027 D4；PR1 核实，PR2 落地）
------------------------------------------------------------
``db/market_data.db`` 的 ``daily_prices.close`` 是 **yfinance 复权价（Adj Close：拆股 + 分红都复权）**；
PR2 起同表另有 ``close_raw`` = yfinance 的 ``Close``，**只按拆股复权、不含分红复权**（实测 AAPL 2020-08-28：
Close 124.81 = 原始价 499.23 ÷ 4，Adj Close 120.96 = 再被分红复权压低）。所以不管读哪一列，价格都已经是**拆股后口径**：

1. **split**：回放里的数量必须在所有历史日期用拆股后口径——拆股前的买卖数量 × 此后所有 split 的 ratio 之积
   （:func:`later_split_factors`）；split 事件本身在这个口径下是空操作（市值不变）。不能在拆股日才把 ``units`` ×ratio：
   那样拆股前"拆股前数量 × 拆股后价"低估 ratio 倍，拆股日 NAV 凭空跳变。
2. **分红**：账本记的是真实现金。复权价已把分红当再投资折进了历史价，再把 ``dividend`` 现金记进 NAV 就是算两次；
   所以估值读 ``close_raw``（services/account_performance 的 ``price_basis``：全有 → raw，缺一天 → 整只复权价）。
   raw 基准下分红是除息日价格下跌 + 账本里的现金流入，两者抵消，TWR 连续。
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
# PR2 新活动：dividend / interest / fee / tax / return_of_capital 只动现金（symbol 只是归属，不是持仓变动）
_INCOME_ACTIONS = frozenset({"dividend", "interest", "return_of_capital"})
_COST_ACTIONS = frozenset({"fee", "tax"})
_CASH_ONLY = _CASH_ACTIONS | _INCOME_ACTIONS | _COST_ACTIONS
_EPS = 1e-9


@dataclass
class LedgerState:
    """回放累计状态（只用于回放，不写回快照）。units/avg_cost 按 symbol，cash 按币种。"""
    units: Dict[str, float] = field(default_factory=dict)
    avg_cost: Dict[str, float] = field(default_factory=dict)   # 近似加权均价（买入加权、卖出不变、清仓归零），不含汇率
    cash: Dict[str, float] = field(default_factory=dict)       # Σ cash_effect
    unknown: int = 0                                           # 不进回放的行数：未知 action / 无可用日期 / 数值解析失败（仍在 Activity 列表里）


def _day(r: Mapping[str, Any]) -> str:
    return str(r.get("trade_date") or r.get("ts_origin") or r.get("ts") or "")[:10]


def _s(v: Any) -> Optional[str]:
    return None if v is None else str(v)


def _opt_f(v: Any) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


# 老 action → Activity.action（不在表里 = unknown）
_ACTION = {**_BUY_LIKE, "gift": "gift", "sell": "sell", "sold": "sell", "deposit": "deposit", "withdraw": "withdraw",
           "manual_override": "correction", "delete_holding": "delete_holding",
           **{a: a for a in _CASH_ONLY - _CASH_ACTIONS}, "split": "split"}


def _effects(raw_action: str, r: Mapping[str, Any], sym: str, held: Mapping[str, float]) -> Dict[str, Any]:
    """数量 / 金额 / 现金与持仓效果。数值字段解析不了会抛 ValueError / TypeError（调用方降级为 unparsed）。"""
    if raw_action not in _ACTION:
        return {"action": "unknown"}
    units = float(r.get("units") or 0)
    price = float(r.get("price") or r.get("price_per_unit") or 0)
    amount = float(r.get("total_amount") or abs(units) * price)
    trade = dict(units=None if r.get("units") is None else units, price=price or None, amount=amount)
    if raw_action in _BUY_LIKE:
        sign = 1.0 if units >= 0 else -1.0
        ext = r.get("funding_source") == _EXTERNAL_FUNDING
        return dict(trade, action=_ACTION[raw_action], units_effect=units, external=ext,
                    cash_effect=0.0 if ext else -sign * amount)
    if raw_action == "gift":
        return dict(trade, action="gift", units_effect=units, external=True, cash_effect=0.0)
    if raw_action in _SELL_LIKE:
        net = r.get("net_amount")
        return dict(trade, action="sell", units_effect=-abs(units),
                    cash_effect=float(net) if net is not None else amount - float(r.get("fee") or 0))
    if raw_action in _CASH_ACTIONS:
        sign = 1.0 if raw_action == "deposit" else -1.0
        return dict(action=raw_action, amount=abs(units), external=True, cash_effect=sign * abs(units))
    if raw_action == "manual_override":
        return dict(trade, action="correction", correction=True, units_effect=units - held.get(sym, 0.0))
    if raw_action in _INCOME_ACTIONS | _COST_ACTIONS:   # 缺 cash_effect 时的推导；v2 记录会被 _activity 用记录值覆盖
        amount = abs(float(r.get("amount") or 0))
        tax = float(r.get("tax_withheld") or 0)
        cash = {"dividend": amount - tax, "interest": amount, "return_of_capital": amount}.get(raw_action, -amount)
        return dict(action=raw_action, amount=amount, tax_withheld=tax or None, cash_effect=cash)
    if raw_action == "split":
        ratio = float(r.get("split_ratio"))   # 缺 / 不是数 → TypeError / ValueError → unparsed
        if not ratio > 0:
            raise ValueError(f"split_ratio 必须 > 0: {ratio}")
        return dict(action="split", split_ratio=ratio, units_effect=held.get(sym, 0.0) * (ratio - 1))
    return dict(action="delete_holding", correction=True, units_effect=-held.get(sym, 0.0))


def _activity(seq: int, r: Dict[str, Any], alias: Mapping[str, str], held: Mapping[str, float]) -> Activity:
    """单条记录 → Activity。``held`` = 到这条为止的回放持仓（只给 manual_override / delete_holding 算增量用）。"""
    raw_action = str(r.get("action") or "").lower()
    raw_sym = str(r.get("symbol") or "")
    sym = alias.get(raw_sym, raw_sym)
    day = _day(r)
    f: Dict[str, Any] = dict(
        symbol=sym or None, currency=str(r.get("currency") or "CNY").upper(),
        trade_date=day if len(day) == 10 else None,
        settle_date=_s(r.get("settle_date")), nav_date=_s(r.get("nav_date")),
        source="dca_reconcile" if raw_action in _RECONCILE else _s(r.get("source")),
        idempotency_key=_s(r.get("idempotency_key")), note=_s(r.get("note")),
        fee=_opt_f(r.get("fee")), ts=_s(r.get("ts_origin") or r.get("ts")), seq=seq, raw=r,
    )
    try:
        f.update(_effects(raw_action, r, sym, held))
        if f["action"] != "unknown" and r.get("schema_version") == 2:   # 新记录：写入方算好的效果为准（ADR-027 D2）
            for k in ("cash_effect", "units_effect"):
                # split 例外：写入方的 units_effect 是「快照份数 × (ratio−1)」，而回放的 LedgerState 累计的是账本份数
                # （不含系统上线前的开盘持仓）。拆股后再来一条 manual_override / delete_holding 时，「新值 − held」要按同一口径
                # 才和不拆股时 correction 吸收开盘持仓的语义一致——所以 split 在回放里始终用 held × (ratio−1)（_effects 已算）
                if k == "units_effect" and f["action"] == "split":
                    continue
                if r.get(k) is not None:
                    f[k] = float(r[k])
            if r.get("external") is not None:
                f["external"] = bool(r["external"])
    except (TypeError, ValueError):
        f.update(action=_ACTION.get(raw_action, "unknown"), unparsed=True)
    return Activity(**f)


def _apply(state: LedgerState, a: Activity) -> None:
    state.cash[a.currency] = state.cash.get(a.currency, 0.0) + a.cash_effect
    sym = a.symbol or ""
    if a.action in _CASH_ONLY:
        # return_of_capital 降成本：avg_cost -= amount / 持仓量（降到 0 为止，超出部分是资本利得不是负成本）
        if a.action == "return_of_capital" and sym in state.avg_cost and state.units.get(sym, 0.0) > _EPS:
            state.avg_cost[sym] = max(state.avg_cost[sym] - (a.amount or 0.0) / state.units[sym], 0.0)
        return
    before = state.units.get(sym, 0.0)
    after = before + a.units_effect
    state.units[sym] = after
    if a.action == "split":
        if sym in state.avg_cost and a.split_ratio:
            state.avg_cost[sym] /= a.split_ratio
    elif a.action in ("buy", "gift") and a.units_effect > 0:
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
        if not a.replayable:
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


def later_split_factors(activities: List[Activity]) -> Dict[int, float]:
    """``{seq: 该活动之后（回放序）同标的所有 split 的 ratio 之积}``——把活动的数量换算成拆股后口径（模块 docstring 第 1 点）。
    没有后续拆股 = 不出现在结果里（读方 ``.get(seq, 1.0)``）；只含能回放且有 symbol 的活动。``activities`` 须是
    :func:`replay_ledger` 的输出（日期升序，同日保持文件顺序）。"""
    running: Dict[str, float] = {}
    out: Dict[int, float] = {}
    for a in reversed(activities):
        if not a.replayable or not a.symbol:
            continue
        f = running.get(a.symbol, 1.0)
        if f != 1.0 and a.action != "split":   # split 自己在拆股后口径下是空操作，不需要换算
            out[a.seq] = f
        if a.action == "split" and a.split_ratio:
            running[a.symbol] = f * a.split_ratio
    return out


def recent_income(activities: List[Activity], since: str) -> Dict[str, Dict[str, float]]:
    """``{action: {币种: 现金合计}}``：``trade_date >= since``（YYYY-MM-DD）的 dividend（税后净额）与 interest。
    status 的"最近 30 天分红 / 利息"一行用；没有则返回空 dict。"""
    out: Dict[str, Dict[str, float]] = {}
    for a in activities:
        if a.replayable and a.action in ("dividend", "interest") and (a.trade_date or "") >= since:
            by_ccy = out.setdefault(a.action, {})
            by_ccy[a.currency] = by_ccy.get(a.currency, 0.0) + a.cash_effect
    return out


__all__ = ["LedgerState", "external_flow", "later_split_factors", "recent_income", "replay_ledger"]
