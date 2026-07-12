"""时间序列纯变换（calc 层，ADR-026）

从 core/benchmarks.py 拆出的纯计算核：基准序列的数据类型与纯变换。
拉取（yfinance / eastmoney）与缓存读写留在 core/benchmarks.py（IO shell）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict


@dataclass
class BenchmarkSeries:
    """一条基准的时间序列：每个 key 是 YYYY-MM-DD，value 是相对 start_date 的累计涨幅 %"""
    key: str
    color: str
    group: str
    dash: str
    points: Dict[str, float]  # {"2026-04-28": 12.34, ...}


def _generate_constant_apr(
    apr_pct: float, start: str, end: str
) -> Dict[str, float]:
    """常数年化 → 模拟一个净值序列：start 日 1.0，按日累计复利"""
    start_dt = datetime.strptime(start, "%Y-%m-%d").date()
    end_dt = datetime.strptime(end, "%Y-%m-%d").date()
    daily_rate = (1 + apr_pct / 100) ** (1 / 365) - 1
    out: Dict[str, float] = {}
    cur = start_dt
    nav = 1.0
    while cur <= end_dt:
        out[cur.strftime("%Y-%m-%d")] = nav
        nav *= (1 + daily_rate)
        cur += timedelta(days=1)
    return out


def to_pct_series(prices: Dict[str, float], start_date: str) -> Dict[str, float]:
    """把绝对价 series 转成"相对 start_date 的累计涨幅 %"。

    第一个点 0%，之后每天 (price / start_price - 1) * 100。
    没有 start_date 当天数据就用最早能找到的有效价当 baseline。
    """
    if not prices:
        return {}
    # 找 baseline：start_date 当天，没有就用最早的
    sorted_dates = sorted(prices.keys())
    baseline_date = start_date if start_date in prices else next(
        (d for d in sorted_dates if d >= start_date), sorted_dates[0]
    )
    baseline = prices[baseline_date]
    if baseline <= 0:
        return {}
    return {d: ((p / baseline) - 1) * 100 for d, p in prices.items() if d >= baseline_date}


__all__ = [
    "BenchmarkSeries",
    "_generate_constant_apr",
    "to_pct_series",
]
