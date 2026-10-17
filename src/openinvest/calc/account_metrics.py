"""account_metrics.py — 真实账户业绩纯计算（calc 层 T0，ADR-026：禁 IO / 禁时钟）

输入：日度 NAV 行 ``[(date, nav_end, inflow, outflow), ...]``（日期升序，基准币种计价）、
开盘 NAV（第一行之前的状态按第一天价格估值）、"今天"（时间是输入不是环境）、可选基准
收盘序列 ``{date: close}``。IO 壳 ``services/account_performance.py`` 负责重放账本 + 取价。

公式（与 Wealthfolio / Ghostfolio 同口径）：

- **TWR**（时间加权，日度链接）：
  ``r_d = (End_d + Outflow_d − Start_d − Inflow_d) / (Start_d + Inflow_d)``，
  ``Start_d = End_{d−1}``（第一天 = opening_nav）；窗口收益 = ``Π(1 + r_d) − 1``。
  外部现金流（存取款、外部资金建仓）当天视为开盘即到账，因而进分母。
  30d 窗口 = 今天往前 30 个自然日之后的所有 r_d；YTD = 上年 12-31 之后；历史不足
  30 天时 30d 不报（None），YTD 从成立起算。
- **XIRR**（金额加权，二分法，年 = 365.25 天）：现金流 = −opening_nav（首日）、
  逐日 −inflow / +outflow、末日 +End。历史跨度 < 30 天不报。
- **最大回撤 / 年化波动**都在 TWR 链接指数 ``I_d = Π(1 + r_k)`` 上算——存取款不是
  回撤。波动 = ``stdev(ln(1 + r_d), ddof=1) × √252``：日期网格是持仓标的交易日的并集
  （非自然日），所以按 252 个交易日年化。
- **vs 基准**：复用 ``calc.strategy_metrics.vs_benchmark``（同窗口总收益差 + 跑赢天数比例），
  基准序列前值填充到同一网格；窗口 = 成立以来。
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from openinvest.calc.strategy_metrics import total_return_pct, vs_benchmark

# (date YYYY-MM-DD, nav_end, inflow, outflow) —— inflow / outflow 均为非负
NavRow = Tuple[str, float, float, float]

DAYS_PER_YEAR = 365.25
TRADING_DAYS_PER_YEAR = 252
XIRR_MIN_SPAN_DAYS = 30
WINDOW_30D_DAYS = 30


def _iso(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def _parse(s: str) -> date:
    return date.fromisoformat(s[:10])


def daily_returns(rows: List[NavRow], opening_nav: float) -> List[Tuple[str, float]]:
    """逐日 TWR 子期收益 ``[(date, r_d), ...]``。分母 ≤ 0（当天没有在险资本）记 0。"""
    out: List[Tuple[str, float]] = []
    start = float(opening_nav)
    for day, end, inflow, outflow in rows:
        denom = start + inflow
        r = (end + outflow - start - inflow) / denom if denom > 0 else 0.0
        out.append((day, r))
        start = float(end)
    return out


def linked_index(returns: List[Tuple[str, float]]) -> List[Tuple[str, float]]:
    """TWR 链接指数 ``[(date, Π(1+r)), ...]``，起点（开盘）= 1.0 不在列表里。"""
    out: List[Tuple[str, float]] = []
    idx = 1.0
    for day, r in returns:
        idx *= 1.0 + r
        out.append((day, idx))
    return out


def window_twr_pct(returns: List[Tuple[str, float]], after: Optional[str]) -> Optional[float]:
    """``after`` 之后（严格大于）所有子期链接的收益 %；``after=None`` = 全部。"""
    growth = 1.0
    n = 0
    for day, r in returns:
        if after is None or day > after:
            growth *= 1.0 + r
            n += 1
    if n == 0:
        return None
    return round((growth - 1.0) * 100, 2)


def twr_windows(returns: List[Tuple[str, float]], today: str) -> Dict[str, Optional[float]]:
    """``{"30d", "ytd", "all"}`` 三个窗口的 TWR %。

    30d：成立不足 30 天 → None（否则就是 since-inception 冒充 30d）。
    YTD：成立在今年内 → 从成立起算（Wealthfolio 同约定）。
    """
    if not returns:
        return {"30d": None, "ytd": None, "all": None}
    t = _parse(today)
    anchor_30d = _iso(t - timedelta(days=WINDOW_30D_DAYS))
    anchor_ytd = f"{t.year - 1}-12-31"
    first = returns[0][0]
    return {
        "30d": window_twr_pct(returns, anchor_30d) if first <= anchor_30d else None,
        "ytd": window_twr_pct(returns, anchor_ytd if first <= anchor_ytd else None),
        "all": window_twr_pct(returns, None),
    }


def xirr(cashflows: List[Tuple[str, float]], *, lo: float = -0.9999, hi: float = 10.0) -> Optional[float]:
    """XIRR（年化 %，二分法，年 = 365.25 天）。投资者视角：投入为负、收回为正。

    没有正负两向现金流、或 [lo, hi] 内无根 → None。
    """
    flows = [(_parse(d), float(a)) for d, a in cashflows if a]
    if len(flows) < 2 or not (any(a < 0 for _, a in flows) and any(a > 0 for _, a in flows)):
        return None
    t0 = min(d for d, _ in flows)
    years = [((d - t0).days / DAYS_PER_YEAR, a) for d, a in flows]

    def npv(rate: float) -> float:
        return sum(a / (1.0 + rate) ** y for y, a in years)

    f_lo, f_hi = npv(lo), npv(hi)
    if math.isnan(f_lo) or math.isnan(f_hi) or (f_lo < 0) == (f_hi < 0):
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        f_mid = npv(mid)
        if abs(f_mid) < 1e-9 or (hi - lo) < 1e-10:
            break
        if (f_mid < 0) == (f_lo < 0):
            lo, f_lo = mid, f_mid
        else:
            hi = mid
    return round(((lo + hi) / 2) * 100, 2)


def account_xirr(rows: List[NavRow], opening_nav: float) -> Optional[float]:
    """账户 XIRR：−opening（首日）、逐日 −inflow/+outflow、末日 +End；跨度 < 30 天不报。"""
    if not rows or (_parse(rows[-1][0]) - _parse(rows[0][0])).days < XIRR_MIN_SPAN_DAYS:
        return None
    flows: Dict[str, float] = {rows[0][0]: -float(opening_nav)}
    for day, _end, inflow, outflow in rows:
        flows[day] = flows.get(day, 0.0) - inflow + outflow
    last_day, last_end = rows[-1][0], rows[-1][1]
    flows[last_day] = flows.get(last_day, 0.0) + float(last_end)
    return xirr(sorted(flows.items()))


def max_drawdown(index: List[Tuple[str, float]], opening_date: str) -> Dict[str, Any]:
    """链接指数上的最大回撤 ``{"pct": ≤0, "peak": date, "trough": date}``（开盘 1.0 也算候选峰）。"""
    peak_val, peak_day = 1.0, opening_date
    worst, worst_peak, worst_trough = 0.0, opening_date, opening_date
    for day, val in index:
        if val > peak_val:
            peak_val, peak_day = val, day
        dd = val / peak_val - 1.0 if peak_val > 0 else 0.0
        if dd < worst:
            worst, worst_peak, worst_trough = dd, peak_day, day
    return {"pct": round(worst * 100, 2), "peak": worst_peak, "trough": worst_trough}


def annual_volatility_pct(returns: List[Tuple[str, float]]) -> Optional[float]:
    """stdev(ln(1+r), ddof=1) × √252，%；不足 2 个有效子期 → None。"""
    logs = [math.log1p(r) for _, r in returns if r > -1.0]
    n = len(logs)
    if n < 2:
        return None
    mean = sum(logs) / n
    var = sum((x - mean) ** 2 for x in logs) / (n - 1)
    return round(math.sqrt(var) * math.sqrt(TRADING_DAYS_PER_YEAR) * 100, 2)


def _ffill_on(grid: List[str], series: Dict[str, float]) -> List[Tuple[str, float]]:
    """把 ``{date: close}`` 前值填充到 ``grid`` 日期上；网格首日之前无数据 → 空列表。"""
    if not series:
        return []
    dates = sorted(series)
    out: List[Tuple[str, float]] = []
    j, last = 0, None
    for day in grid:
        while j < len(dates) and dates[j] <= day:
            last = series[dates[j]]
            j += 1
        if last is None:
            return []
        out.append((day, float(last)))
    return out


def benchmark_block(
    index: List[Tuple[str, float]], opening_date: str,
    symbol: str, closes: Dict[str, float],
) -> Optional[Dict[str, Any]]:
    """vs 基准（成立以来）：基准前值填充到账户网格 + 开盘锚点，再交给 ``vs_benchmark``。"""
    if not index or not closes:
        return None
    anchor = _iso(_parse(opening_date) - timedelta(days=1))
    strat = [(anchor, 1.0)] + list(index)
    bench = _ffill_on([anchor] + [d for d, _ in index], closes)
    if len(bench) < 2:
        return None
    vs = vs_benchmark(strat, bench)
    return {
        "symbol": symbol,
        "window": "since_inception",
        "return_same_window": total_return_pct(bench),
        "excess": vs["alpha_pct"],
        "beat_days_pct": vs["beat_days_pct"],
    }


def account_metrics(
    rows: List[NavRow], opening_nav: float, today: str,
    *, benchmark_symbol: str = "", benchmark_closes: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """status / 日报共用的业绩块（不含 data_quality，那是 IO 壳的事）。"""
    returns = daily_returns(rows, opening_nav)
    index = linked_index(returns)
    opening_date = rows[0][0] if rows else today
    windows = twr_windows(returns, today)
    return {
        "since": opening_date,
        "as_of": rows[-1][0] if rows else None,
        "nav": round(float(rows[-1][1]), 2) if rows else None,
        "twr_30d": windows["30d"],
        "twr_ytd": windows["ytd"],
        "twr_all": windows["all"],
        "xirr": account_xirr(rows, opening_nav),
        "max_drawdown": max_drawdown(index, opening_date),
        "volatility_annual": annual_volatility_pct(returns),
        "benchmark": benchmark_block(index, opening_date, benchmark_symbol, benchmark_closes or {})
        if benchmark_symbol else None,
    }


__all__ = [
    "NavRow", "DAYS_PER_YEAR", "TRADING_DAYS_PER_YEAR", "XIRR_MIN_SPAN_DAYS", "WINDOW_30D_DAYS",
    "daily_returns", "linked_index", "window_twr_pct", "twr_windows", "xirr", "account_xirr",
    "max_drawdown", "annual_volatility_pct", "benchmark_block", "account_metrics",
]
