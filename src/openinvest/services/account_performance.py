"""真实账户业绩（IO 壳）：重放账本 → 日度 NAV 序列 → calc.account_metrics

数据源：
- ``memory/portfolio_history.jsonl``（PortfolioManager buy/sell/deposit/withdraw、CommSec 邮件、
  web_api 黄金记账、DCA 对账）——交易与外部现金流
- ``memory/portfolio.md``——**当前真实状态**；账本早于系统上线的持仓/现金没有记录，所以
  序列锚定在当前状态：开盘状态 = 当前 − Σ账本增量（``data_quality.unreconciled`` 列出），
  保证序列末日 = status 的总资产
- ``db/market_data.db``——收盘价、汇率（只读库，不走网络；缺价日前值填充并计数）

估值复用全仓库唯一总资产实现 ``utils.fx.total_portfolio_value``（``rate_override`` 喂当日汇率），
成本兜底 / 缺价缺汇率口径与 status 完全一致。

外部现金流（进 TWR 分母、XIRR 现金流）：deposit / withdraw、``funding_source=external_funding``
的买入与 DCA 对账、赠金，以及 manual_override / delete_holding 这类无现金对手的持仓修正
（按当日收盘估值）。buy(cash_deduct) / sell / CommSec 成交是现金↔持仓的内部转换，不是现金流。

网格 = 持仓标的交易日并集（非自然日）；周末记的交易挂到下一个交易日估值。
"""
from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import date
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from openinvest.calc.account_metrics import account_metrics
from openinvest.calc.gold import GOLD_OZ_PER_GRAM
from openinvest.core.portfolio_manager import PortfolioManager
from openinvest.db.market_store import MarketStore
from openinvest.utils.base_currency import base_currency
from openinvest.utils.eastmoney_fund import canonical_fund_symbol, extract_fund_code
from openinvest.utils.exchange_fee import get_history_data
from openinvest.utils.fx import total_portfolio_value, unvalued
from openinvest.utils.quotes import is_eastmoney_fund

log = logging.getLogger(__name__)

# web_api /api/gold/* 记账用的符号；对应 proxy_kind=gold_cny_per_gram 的持仓（symbol 通常 GC=F）
GOLD_LEDGER_SYMBOL = "GOLD-CNY"
GOLD_PROXY_KIND = "gold_cny_per_gram"
# 黄金流水在当前持仓里没有对应行（已卖光 + delete_holding）时的估值配置：不依赖持仓元数据
_GOLD_LEDGER_HOLDING = {"symbol": GOLD_LEDGER_SYMBOL, "proxy_kind": GOLD_PROXY_KIND,
                        "yfinance_proxy": "GC=F", "cost_currency": "CNY"}
_ALL_ROWS = 100000
_BUY_ACTIONS = {"buy", "bought", "dca_reconcile_backfill", "dca_reconcile_reverse", "gift"}
_SELL_ACTIONS = {"sell", "sold"}


# ---------- 1. 账本 → 事件 ----------

def _ledger_events(records: List[Dict[str, Any]], alias: Dict[str, str]) -> Tuple[List[Dict[str, Any]], int]:
    """账本记录 → 事件 dict（日期升序）。返回 (events, 跳过的未知 action 数)。

    事件字段：date / symbol / units（增量）/ cash_ccy / cash（增量）/ flow_ccy / flow（外部现金流，
    +入 −出）/ correction（True = 持仓修正，现金流按当日市价在估值时补算）。
    manual_override / delete_holding 是绝对值：先按从零重放算出增量，让所有事件都是增量。
    """
    events: List[Dict[str, Any]] = []
    skipped = 0
    units_from_zero: Dict[str, float] = defaultdict(float)
    for r in sorted(records, key=lambda x: (x.get("trade_date") or x.get("ts_origin") or x.get("ts") or "")[:10]):
        day = str(r.get("trade_date") or r.get("ts_origin") or r.get("ts") or "")[:10]
        action = str(r.get("action") or "").lower()
        if len(day) != 10:
            skipped += 1
            continue
        sym = alias.get(str(r.get("symbol") or ""), str(r.get("symbol") or ""))
        ccy = str(r.get("currency") or "CNY").upper()
        units = float(r.get("units") or 0)
        price = float(r.get("price") or r.get("price_per_unit") or 0)
        amount = float(r.get("total_amount") or abs(units) * price)
        ev: Dict[str, Any] = {"date": day, "symbol": sym, "units": 0.0, "cash_ccy": ccy, "cash": 0.0,
                              "flow_ccy": ccy, "flow": 0.0, "correction": False}
        if action in _BUY_ACTIONS:
            sign = 1.0 if units >= 0 else -1.0
            ev["units"] = units
            if r.get("funding_source") == "external_funding" or action == "gift":
                ev["flow"] = sign * amount          # 外部新钱建仓（对账冲回则为流出）
            else:
                ev["cash"] = -sign * amount         # 子弹池现金 ↔ 持仓，内部转换
        elif action in _SELL_ACTIONS:
            ev["units"] = -abs(units)
            net = r.get("net_amount")
            ev["cash"] = float(net) if net is not None else amount - float(r.get("fee") or 0)
        elif action == "deposit":
            ev["cash"] = abs(units)
            ev["flow"] = abs(units)
        elif action == "withdraw":
            ev["cash"] = -abs(units)
            ev["flow"] = -abs(units)
        elif action == "manual_override":
            ev["units"] = units - units_from_zero[sym]
            ev["correction"] = True
        elif action == "delete_holding":
            ev["units"] = -units_from_zero[sym]
            ev["correction"] = True
        else:
            skipped += 1
            continue
        units_from_zero[sym] += ev["units"]
        events.append(ev)
    return events, skipped


# ---------- 2. 行情库读序列 ----------

def _db_closes(store: MarketStore, symbol: str) -> Optional[pd.Series]:
    """行情库里该 symbol 的收盘序列（index=YYYY-MM-DD 字符串，升序）；没有 → None。"""
    try:
        df = store.get_history_df(symbol, days=_ALL_ROWS)
    except Exception as e:  # noqa: BLE001
        log.warning("account_performance: 读 %s 行情失败: %s", symbol, e)
        return None
    if df is None or df.empty or "Close" not in df:
        return None
    s = pd.to_numeric(df["Close"], errors="coerce").dropna()
    s = s[s > 0]
    if s.empty:
        return None
    s.index = [ts.strftime("%Y-%m-%d") for ts in s.index]
    return s[~s.index.duplicated(keep="last")].sort_index()


def _price_series(store: MarketStore, symbol: str, holding: Optional[Dict[str, Any]]) -> Optional[pd.Series]:
    """持仓按 cost_currency 计价的收盘序列。口径与 utils.quotes.get_quote 一致：
    黄金代理 = GC=F USD/oz ÷ 31.1035 × USDCNY；场外基金读 FUND:<code>；其余直接按 symbol/proxy。"""
    h = holding or {}
    proxy = str(h.get("yfinance_proxy") or symbol)
    if (not h or h.get("_from_ledger")) and extract_fund_code(symbol):
        # 已删掉的场外基金持仓：元数据没了，按代码读 FUND:<code>
        return _db_closes(store, canonical_fund_symbol(symbol))
    if str(h.get("proxy_kind") or "") == GOLD_PROXY_KIND:
        oz, usdcny = _db_closes(store, proxy), _db_closes(store, "USDCNY=X")
        if oz is None or usdcny is None:
            return None
        # 只在 GC=F 的交易日上出点（汇率前值填充到这些日期）：FX 对偶有周末行，不该把周末拉进估值网格
        fx_on_oz = usdcny.reindex(sorted(set(oz.index) | set(usdcny.index))).ffill().reindex(oz.index)
        both = pd.DataFrame({"oz": oz, "fx": fx_on_oz}).dropna()
        return (both["oz"] / GOLD_OZ_PER_GRAM * both["fx"]).rename(symbol) if not both.empty else None
    if h and is_eastmoney_fund(h):
        return _db_closes(store, canonical_fund_symbol(symbol))
    return _db_closes(store, proxy)


def _fx_series(store: MarketStore, ccy: str, base: str) -> Optional[pd.Series]:
    """ccy → base 的日度汇率序列（直接对 / 反向对取倒数 / 经 USD 交叉），只读库。"""
    if ccy == base:
        return None
    direct = _db_closes(store, f"{ccy}{base}=X")
    if direct is not None:
        return direct
    inverse = _db_closes(store, f"{base}{ccy}=X")
    if inverse is not None:
        return 1.0 / inverse
    if "USD" in (ccy, base):
        return None
    leg1, leg2 = _fx_series(store, ccy, "USD"), _fx_series(store, "USD", base)
    if leg1 is None or leg2 is None:
        return None
    grid = sorted(set(leg1.index) | set(leg2.index))
    both = pd.DataFrame({"a": leg1, "b": leg2}).reindex(grid).ffill().dropna()
    return (both["a"] * both["b"]) if not both.empty else None


def _at(series: Optional[pd.Series], day: str) -> Tuple[Optional[float], bool]:
    """序列在 ``day`` 的值（≤ day 的最后一个）；返回 (value, 是否前值填充)。"""
    if series is None or series.empty:
        return None, False
    pos = series.index.searchsorted(day, side="right") - 1
    if pos < 0:
        return None, False
    v = float(series.iloc[pos])
    return (v if math.isfinite(v) and v > 0 else None), series.index[pos] != day


# ---------- 3. 主入口 ----------

def build_account_performance(pm: Optional[PortfolioManager] = None, *, today: Optional[str] = None,
                              base: Optional[str] = None) -> Dict[str, Any]:
    """status["performance"] / 日报"账户业绩"段共用的业绩块。

    历史不足 2 个估值日（或没有账本）→ ``{"status": "insufficient_history", ...}``。
    """
    pm = pm or PortfolioManager()
    base = (base or base_currency()).upper()
    today = today or date.today().strftime("%Y-%m-%d")
    store = MarketStore()

    holdings = [dict(h) for h in pm.holdings if not h.get("is_tracking_only")]
    by_sym = {str(h.get("symbol")): h for h in holdings}
    alias = {GOLD_LEDGER_SYMBOL: s for s, h in by_sym.items() if str(h.get("proxy_kind") or "") == GOLD_PROXY_KIND}
    records = pm.store.read_history()
    events, skipped = _ledger_events(records, alias)
    if not events:
        return {"status": "insufficient_history", "reason": "no_ledger", "base_currency": base}
    # 账本里出现过、当前持仓已没有的 symbol（卖光 + delete_holding 是正常流程）：元数据从流水推——
    # 计价币种取该 symbol 最近一条记录的 currency，黄金流水固定按 gold_cny_per_gram（不然 AUD 持仓会被
    # 当人民币估值、黄金历史全部无法估值，回撤/波动算成天文数字——CR 阻塞项）
    for r in sorted(records, key=lambda x: (x.get("trade_date") or x.get("ts_origin") or x.get("ts") or "")[:10]):
        sym = alias.get(str(r.get("symbol") or ""), str(r.get("symbol") or ""))
        if not sym or sym in by_sym or str(r.get("action") or "").lower() in ("deposit", "withdraw"):
            continue
        if sym == GOLD_LEDGER_SYMBOL:
            by_sym[sym] = dict(_GOLD_LEDGER_HOLDING)
        else:
            by_sym[sym] = {"symbol": sym, "cost_currency": str(r.get("currency") or "CNY").upper(),
                           "_from_ledger": True}

    # 开盘状态 = 当前 − Σ账本增量（账本早于系统上线的部分）
    delta_units: Dict[str, float] = defaultdict(float)
    delta_cash: Dict[str, float] = defaultdict(float)
    for ev in events:
        delta_units[ev["symbol"]] += ev["units"]
        delta_cash[ev["cash_ccy"]] += ev["cash"]
    units: Dict[str, float] = defaultdict(float)
    for sym in set(by_sym) | set(delta_units):
        units[sym] = float(by_sym.get(sym, {}).get("units") or 0) - delta_units.get(sym, 0.0)
    cash: Dict[str, float] = defaultdict(float)
    for ccy in {str(c).upper() for c in pm.cash} | set(delta_cash):
        cash[ccy] = float(pm.cash.get(ccy, 0) or 0) - delta_cash.get(ccy, 0.0)
    unreconciled = {
        "units": {s: round(v, 6) for s, v in units.items() if abs(v) > 1e-9},
        "cash": {c: round(v, 2) for c, v in cash.items() if abs(v) > 0.005},
    }

    # 价格 / 汇率序列（只读库，各一次）
    prices = {sym: _price_series(store, sym, by_sym.get(sym)) for sym in units}
    ccys = {str(c).upper() for c in cash} | {
        "CNY" if str(by_sym.get(s, {}).get("proxy_kind") or "") == GOLD_PROXY_KIND
        else str(by_sym.get(s, {}).get("cost_currency") or "CNY").upper() for s in units
    }
    fx = {c: _fx_series(store, c, base) for c in ccys if c != base}

    first_day = events[0]["date"]
    grid = sorted({d for s in prices.values() if s is not None for d in s.index if first_day <= d <= today})
    if len(grid) < 2:
        return {"status": "insufficient_history", "reason": "fewer_than_2_valuation_days",
                "base_currency": base, "since": first_day}
    # 事件挂到 ≥ 事件日的第一个网格日（周末记的交易在下一交易日估值）；晚于网格的挂末日
    by_day: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for ev in events:
        pos = min(pd.Index(grid).searchsorted(ev["date"], side="left"), len(grid) - 1)
        by_day[grid[pos]].append(ev)

    def _holding_snapshot(sym: str) -> Dict[str, Any]:
        h = by_sym.get(sym, {})
        gold = str(h.get("proxy_kind") or "") == GOLD_PROXY_KIND
        return {"symbol": sym, "units": units[sym], "avg_cost": h.get("avg_cost", 0),
                "cost_currency": "CNY" if gold else str(h.get("cost_currency") or "CNY").upper()}

    def _market(day: str) -> Tuple[Dict[str, Optional[float]], Dict[str, Optional[float]], Dict[str, bool]]:
        """当日价格 / 汇率（≤ day 最后一个）+ 每个标的当天是否前值填充。"""
        px, ff = {}, {}
        for sym, s in prices.items():
            px[sym], ff[sym] = _at(s, day)
        return px, {c: _at(s, day)[0] for c, s in fx.items()}, ff

    def _value(px: Dict[str, Optional[float]], rates: Dict[str, Optional[float]]) -> Tuple[float, Dict[str, str]]:
        snap = SimpleNamespace(cash=dict(cash), holdings=[_holding_snapshot(s) for s in units])
        return total_portfolio_value(snap, px, base, rate_override=rates)

    def _to_base(ccy: str, amount: float, rates: Dict[str, Optional[float]]) -> Optional[float]:
        rate = 1.0 if ccy == base else rates.get(ccy)
        return amount * rate if rate is not None else None

    rows: List[Tuple[str, float, float, float]] = []
    opening_nav, _ = _value(*_market(grid[0])[:2])
    filled_days, unconverted_flows, cost_fallback, last_status = 0, 0, set(), {}
    for day in grid:
        inflow = outflow = 0.0
        px, rates, ff = _market(day)   # 一天只取一次：修正事件取价与收盘估值共用
        for ev in by_day.get(day, ()):
            units[ev["symbol"]] += ev["units"]
            cash[ev["cash_ccy"]] += ev["cash"]
            flow_ccy, flow = ev["flow_ccy"], ev["flow"]
            if ev["correction"]:
                h = _holding_snapshot(ev["symbol"])
                p = px.get(ev["symbol"]) or float(h["avg_cost"] or 0)
                flow_ccy, flow = h["cost_currency"], ev["units"] * p
            if not flow:
                continue
            v = _to_base(flow_ccy, flow, rates)
            if v is None:
                unconverted_flows += 1
                continue
            if v > 0:
                inflow += v
            else:
                outflow += -v
        nav, status = _value(px, rates)
        filled_days += int(any(ff[s] and units[s] > 0 for s in prices))
        cost_fallback |= {k for k, v in status.items() if v == "cost_fallback"}
        last_status = status
        rows.append((day, nav, round(inflow, 2), round(outflow, 2)))

    cfg_symbol = _benchmark_symbol()
    bench = _benchmark_closes(store, cfg_symbol, today) if cfg_symbol else {}
    out = account_metrics(rows, opening_nav, today, benchmark_symbol=cfg_symbol if bench else "",
                          benchmark_closes=bench)
    bench_missing = None
    if cfg_symbol and not bench:
        bench_missing = {"symbol": cfg_symbol, "reason": "no_data"}
    elif cfg_symbol and out.get("benchmark") is None:
        bench_missing = {"symbol": cfg_symbol, "reason": "starts_after_inception"}
    out["status"] = "ok"
    out["base_currency"] = base
    out["opening_nav"] = round(opening_nav, 2)
    out["data_quality"] = {
        "days": len(rows),
        "filled_days": filled_days,
        "events": len(events),
        "skipped_events": skipped,
        "unconverted_flows": unconverted_flows,
        "cost_fallback": sorted(cost_fallback),
        "unvalued": unvalued(last_status),
        "unreconciled": unreconciled,
        **({"benchmark_unavailable": bench_missing} if bench_missing else {}),
    }
    return out


def _benchmark_symbol() -> str:
    from openinvest.core.config import load_config
    return str(getattr(load_config().portfolio, "benchmark_symbol", "") or "").strip()


# 基准 symbol → 本进程今天已经走过一次 get_history_data（库 + yfinance 增量）的日期。没人持有基准，
# 其他路径不会替它刷新；但 status / MCP status 被 agent 高频调时也不该每次都打一次 yfinance
# （库里缺当天 bar 的周末 / 收盘前每次都会请求）——每进程每天最多刷一次，其余只读库。
_BENCH_REFRESHED: Dict[str, str] = {}


def _benchmark_closes(store: MarketStore, symbol: str, today: str) -> Dict[str, float]:
    """基准收盘 {date: close}：每进程每天最多经 get_history_data 刷一次，之后只读行情库；失败返回空。"""
    if _BENCH_REFRESHED.get(symbol) != today:
        try:
            get_history_data(symbol, "max")
            _BENCH_REFRESHED[symbol] = today
        except Exception as e:  # noqa: BLE001
            log.warning("account_performance: 基准 %s 刷新失败，用库里已有的: %s", symbol, e)
    s = _db_closes(store, symbol)
    return {} if s is None else {d: float(v) for d, v in s.items()}


# ---------- 4. 渲染（日报 / 邮件） ----------

def _pct(v: Optional[float]) -> str:
    return "n/a" if v is None else f"{v:+.2f}%"


def render_performance_md(perf: Dict[str, Any]) -> str:
    """日报"账户业绩"段（markdown，邮件 HTML 与 chat 变体同一文本）。不可用时一行说明。"""
    if perf.get("status") != "ok":
        return f"- 账户业绩：历史不足（{perf.get('reason', perf.get('status'))}），记满 2 个交易日后出现"
    dq = perf.get("data_quality") or {}
    mdd = perf.get("max_drawdown") or {}
    b = perf.get("benchmark")
    lines = [
        f"- TWR（时间加权）：30 天 {_pct(perf.get('twr_30d'))} ｜ 今年 {_pct(perf.get('twr_ytd'))}"
        f" ｜ 自 {perf.get('since')} {_pct(perf.get('twr_all'))}",
        f"- XIRR（年化金额加权）：{_pct(perf.get('xirr'))}"
        + ("（历史 < 30 天不报）" if perf.get("xirr") is None else ""),
        f"- 最大回撤：{_pct(mdd.get('pct'))}"
        + (f"（{mdd.get('peak')} → {mdd.get('trough')}）" if mdd.get("pct") else "")
        + f" ｜ 年化波动：{_pct(perf.get('volatility_annual'))}",
    ]
    if b:
        lines.append(f"- vs 基准 {b['symbol']}（同窗口）：基准 {_pct(b.get('return_same_window'))}"
                     f"，超额 {_pct(b.get('excess'))}，跑赢天数 {b.get('beat_days_pct', 0):.0f}%")
    elif dq.get("benchmark_unavailable"):
        bu = dq["benchmark_unavailable"]
        why = "基准历史晚于账户成立日" if bu.get("reason") == "starts_after_inception" else "行情不可用"
        lines.append(f"- vs 基准 {bu.get('symbol')}：{why}")
    notes = []
    if dq.get("filled_days"):
        notes.append(f"{dq['filled_days']} 天缺价前值填充")
    if dq.get("cost_fallback"):
        notes.append("按成本价计入：" + "、".join(dq["cost_fallback"]))
    if dq.get("unvalued"):
        notes.append("未计入：" + "、".join(dq["unvalued"]))
    if dq.get("unreconciled", {}).get("units") or dq.get("unreconciled", {}).get("cash"):
        notes.append("账本早于系统的开盘持仓按成立日估值（status performance.data_quality.unreconciled）")
    lines.append(f"- 数据：{dq.get('days')} 个交易日" + ("；" + "；".join(notes) if notes else ""))
    return "\n".join(lines)


__all__ = ["build_account_performance", "render_performance_md", "GOLD_LEDGER_SYMBOL"]
