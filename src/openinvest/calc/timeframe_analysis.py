"""多时间维度切片分析（calc 层，ADR-026）

从 utils/exchange_fee.py 拆出的纯计算核：所有函数吃传入的 DataFrame，零 fetch。
注意 `_calc_max_drawdown` / `_calc_volatility` 与 calc/market_metrics 的同名系
函数**口径不同**（本模块吃 Series、market_metrics 吃 OHLC df），禁止合并。
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd


def _apply_cutoff(df: pd.DataFrame, as_of_date: Optional[str]) -> pd.DataFrame:
    """把 df 截到 cutoff 当日（含）。

    语义：T 日决策时**可以看 T 日的 close**（用户场景：晚间 cron 跑委员会 / 用户睡前
    查 verdict，市场已收盘）。所以保留 `index <= cutoff` 数据，去掉 cutoff 之后所有
    交易日（保证 LLM 看不到未来）。

    跟 backtest_committee.py:_patch_tools_to_date 原有的 `df.index < next_day`
    语义一致。
    """
    if as_of_date is None or df.empty:
        return df
    cutoff = pd.to_datetime(as_of_date)
    # df.index 可能是 tz-aware（yfinance 默认带时区），cutoff 是 naive → 对齐
    try:
        if df.index.tz is not None:
            cutoff = cutoff.tz_localize(df.index.tz)
    except (AttributeError, TypeError):
        pass
    return df[df.index <= cutoff]


# ==========================================
# 数学工具
# ==========================================
def _calc_change(start: float, end: float) -> float:
    if start == 0: return 0.0
    return (end - start) / start


def _calc_max_drawdown(series: pd.Series) -> float:
    if series.empty: return 0.0
    roll_max = series.cummax()
    drawdown = (series - roll_max) / roll_max
    return drawdown.min()


def _calc_volatility(series: pd.Series) -> float:
    if len(series) < 2: return 0.0
    return series.pct_change().std() * np.sqrt(252)


def _analyze_slice(df_slice: pd.DataFrame, label: str, current_price: float) -> str:
    if df_slice.empty:
        return f"- **{label}**: No Data"
    start_price = df_slice['Close'].iloc[0]
    change = _calc_change(start_price, current_price)
    mdd = _calc_max_drawdown(df_slice['Close'])
    vol_str = ""
    if len(df_slice) > 20:
        vol = _calc_volatility(df_slice['Close'])
        vol_str = f", Vol: {vol:.2%}"
    return f"- **{label}**: Ret: {change:.2%}, MaxDD: {mdd:.2%}{vol_str}"


def analyze_multi_timeframe(hist: pd.DataFrame, title: str) -> str:
    """格式化层 — 数值计算交给 calc.market_metrics.compute_metrics（SSOT 唯一来源）。

    本函数只负责：拿 metrics dict + 切窗口算阶段收益 + 拼成给 LLM 看的字符串。
    任何 MA / RSI / 分位 / ATR 改动 → 改 calc/market_metrics.py，不要在这里加。
    """
    from openinvest.calc.market_metrics import compute_metrics

    if hist.empty:
        return f"数据缺失: {title}"

    metrics = compute_metrics(hist)
    current_price = metrics["current_price"]
    if current_price is None:
        return f"数据缺失: {title}"

    ma_120 = metrics["ma120"]
    ma_250 = metrics["ma250"]
    rsi_14 = metrics["rsi14"]
    pos = metrics["price_quantile_2y"]
    rvol = metrics.get("rvol")

    slices = {
        "1-Week": hist.tail(5),
        "1-Month": hist.tail(21),
        "6-Months": hist.tail(126),
        "1-Year": hist.tail(252),
        # 504 交易日 ≈ 2 年。用整个 hist（get_history_df(days=730) ≈ 730 交易日
        # ≈ 2.9 日历年）会把 2Y Ret/MaxDD 窗口高估 ~45%；2026-06-13 修 price_quantile
        # 时加了 tail(504) 但漏了这个兄弟切片（CR 命中）。
        "2-Years": hist.tail(504),
    }

    rsi_str = f"{rsi_14:.2f}" if rsi_14 is not None else "N/A"
    # CONTAMINATION CHANNEL (ADR-022): 绝对价位/宏观点位逐字进 prompt → 记忆过历史的 LLM 可反推年代;归一化能压低但杀纪律规则(VIX>20=fear 吃绝对值),不可消除。
    report_lines = [
        f"--- {title} ANALYSIS ---",
        # RSI(14) 为 Wilder 平滑（与 TradingView/券商口径一致）
        f"Current Price: {current_price:.4f} | RSI(14, Wilder): {rsi_str}",
    ]

    if pos is not None:
        # 真百分位排名：历史 X% 的交易日收盘价 ≤ 当前价（不是区间归一位置）
        report_lines.append(
            f"Price Percentile (2y): {pos:.0%} (历史 {pos:.0%} 交易日收盘价 ≤ 当前价)"
        )
    if rvol is not None:
        # 相对成交量：> 1 放量，< 1 缩量（依赖 DB 补存 Volume）
        report_lines.append(f"RVOL(20): {rvol:.2f}x (当日量 / 前 20 日均量)")

    report_lines.append("**Timeframe Performance:**")
    for label, df_slice in slices.items():
        report_lines.append(_analyze_slice(df_slice, label, current_price))

    report_lines.append("**Key Levels:**")
    if ma_120 is not None:
        report_lines.append(f"- MA120 (Trend): {ma_120:.4f}")
    if ma_250 is not None:
        report_lines.append(f"- MA250 (Base): {ma_250:.4f}")
        if ma_250 != 0:
            bias = (current_price / ma_250 - 1)
            report_lines.append(f"- MA250 Deviation: {bias:.2%}")

    return "\n".join(report_lines)


__all__ = [
    "_apply_cutoff",
    "_calc_change",
    "_calc_max_drawdown",
    "_calc_volatility",
    "_analyze_slice",
    "analyze_multi_timeframe",
]
