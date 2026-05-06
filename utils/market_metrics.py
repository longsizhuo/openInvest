"""市场指标 — 数值计算的唯一来源 (Single Source of Truth, Layer 1)

所有 MA / RSI / ATR / 分位 / 波动率的计算只在这里。
任何下游（exchange_fee.analyze_multi_timeframe / core.regime / dreaming）
都必须 import 这个模块，**禁止重复实现**。

设计原则:
- 纯函数：输入 DataFrame，输出 dict，无 I/O 无副作用
- 字段语义稳定：字段名一旦定下来不许改（regime SSOT 依赖这些字段名）
- 缺数据 → 字段为 None，不报错（让下游决定怎么处理）
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np
import pandas as pd


def _safe_last(series: pd.Series) -> Optional[float]:
    """series 末值；空或 NaN 返 None"""
    if series.empty:
        return None
    val = series.iloc[-1]
    if pd.isna(val):
        return None
    return float(val)


def _calc_rsi(close: pd.Series, period: int = 14) -> Optional[float]:
    if len(close) < period + 1:
        return None
    delta = close.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    if loss.empty or pd.isna(loss.iloc[-1]):
        return None
    if loss.iloc[-1] == 0:
        return 100.0
    rs = gain.iloc[-1] / loss.iloc[-1]
    return float(100 - (100 / (1 + rs)))


def _calc_atr_pct(df: pd.DataFrame, period: int = 14) -> Optional[float]:
    """ATR 百分比 = ATR / 当前收盘价 * 100。无 High/Low 时退化为收盘价绝对差"""
    if len(df) < period + 1:
        return None
    close = df["Close"]
    if "High" in df.columns and "Low" in df.columns:
        high = df["High"]
        low = df["Low"]
        prev_close = close.shift(1)
        tr = pd.concat(
            [
                (high - low).abs(),
                (high - prev_close).abs(),
                (low - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
    else:
        tr = close.diff().abs()
    atr = tr.rolling(window=period).mean()
    last_atr = _safe_last(atr)
    last_price = _safe_last(close)
    if last_atr is None or last_price is None or last_price == 0:
        return None
    return float(last_atr / last_price * 100)


def _calc_max_drawdown(close: pd.Series) -> Optional[float]:
    if close.empty:
        return None
    roll_max = close.cummax()
    drawdown = (close - roll_max) / roll_max
    val = drawdown.min()
    return None if pd.isna(val) else float(val)


def _calc_volatility_annualized(close: pd.Series) -> Optional[float]:
    if len(close) < 2:
        return None
    val = close.pct_change().std() * np.sqrt(252)
    return None if pd.isna(val) else float(val)


def _calc_price_quantile(close: pd.Series) -> Optional[float]:
    """当前价在区间内的分位 (0=区间最低 1=区间最高)"""
    if close.empty:
        return None
    high = close.max()
    low = close.min()
    cur = close.iloc[-1]
    if pd.isna(high) or pd.isna(low) or pd.isna(cur):
        return None
    if high == low:
        return 0.5
    return float((cur - low) / (high - low))


def compute_metrics(df: pd.DataFrame) -> Dict[str, Any]:
    """从历史 K 线 DataFrame 算出所有市场指标。

    Args:
        df: pandas DataFrame，必须有 'Close' 列；'High'/'Low' 可选（有则 ATR 更准）

    Returns:
        dict 字段（缺数据时为 None）:
          current_price: 最新收盘价
          ma20 / ma120 / ma250: 20/120/250 日简单均线
          rsi14: 14 日 RSI
          price_quantile_2y: 当前价在传入区间内的分位（0-1）—— 通常传 2 年数据
          atr_pct: 14 日 ATR 占当前价的百分比（衡量近期波动幅度）
          volatility_annualized: 年化波动率（基于全部样本）
          max_drawdown: 最大回撤（负数）
          n_samples: 样本数（让下游知道数据多薄）
    """
    if df.empty or "Close" not in df.columns:
        return {
            "current_price": None,
            "ma20": None,
            "ma120": None,
            "ma250": None,
            "rsi14": None,
            "price_quantile_2y": None,
            "atr_pct": None,
            "volatility_annualized": None,
            "max_drawdown": None,
            "n_samples": 0,
        }

    close = df["Close"]
    return {
        "current_price": _safe_last(close),
        "ma20": _safe_last(close.rolling(window=20).mean()),
        "ma120": _safe_last(close.rolling(window=120).mean()),
        "ma250": _safe_last(close.rolling(window=250).mean()),
        "rsi14": _calc_rsi(close, period=14),
        "price_quantile_2y": _calc_price_quantile(close),
        "atr_pct": _calc_atr_pct(df, period=14),
        "volatility_annualized": _calc_volatility_annualized(close),
        "max_drawdown": _calc_max_drawdown(close),
        "n_samples": int(len(df)),
    }


__all__ = ["compute_metrics"]
