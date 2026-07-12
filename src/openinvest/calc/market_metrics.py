"""市场指标 — 数值计算的唯一来源 (Single Source of Truth, Layer 1)

所有 MA / RSI / ATR / 分位 / 波动率的计算只在这里。
任何下游（exchange_fee.analyze_multi_timeframe / core.regime / dreaming）
都必须 import 这个模块，**禁止重复实现**。

设计原则:
- 纯函数：输入 DataFrame，输出 dict，无 I/O 无副作用
- 字段语义稳定：字段名一旦定下来不许改（regime SSOT 依赖这些字段名）
- 缺数据 → 字段为 None，不报错（让下游决定怎么处理）

2026-05-26 指标算法修复（A 组）:
- RSI / ATR 从简单 SMA 平滑改为 **Wilder 平滑 (RMA, 递归 α=1/N)**，对齐
  TradingView / 券商默认口径。
- price_quantile_2y 从 (cur-min)/(max-min) 区间归一改为**真百分位排名**
  （窗口内收盘价 ≤ 当前价的比例）。
- 新增 return_30d / rebound_off_30d_low（regime crash 双条件 + recovery 用）。
- 新增 rvol（相对成交量，依赖 DB 补存 Volume，见 B 组）。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

# crash 跌幅腿 / recovery 反弹用的回溯窗口（交易日）
LOOKBACK_30D = 30
# rvol 默认均量窗口（交易日）
RVOL_WINDOW = 20
# "近 2 年"窗口的**单一可信源**（交易日）。所有"2y 分位/口径"统一引此常量，
# 杜绝 504/730/yfinance"2y" 多处各写。回测 compute_regime_return_frame 与生产
# compute_metrics / utils.sentiment 必须同引此值。
# 2026-06-13 口径审计根因：get_history_data(period) 不截断、返回 get_history_df(默认 730 行)，
# price_quantile/VIX 分位对"730 行(≈2.9年)"而非 504 算 → 本常量 + tail() 修正(transcript 校验)。
TRADING_DAYS_2Y = 504


def _safe_last(series: pd.Series) -> Optional[float]:
    """series 末值；空或 NaN 返 None"""
    if series.empty:
        return None
    val = series.iloc[-1]
    if pd.isna(val):
        return None
    return float(val)


def _calc_rsi(close: pd.Series, period: int = 14) -> Optional[float]:
    """Wilder RSI（标准定义，周期默认 14）。

    平滑用 Wilder 的 RMA —— 递归 `avg_t = avg_{t-1} + (x_t - avg_{t-1})/period`，
    等价 `ewm(alpha=1/period, adjust=False).mean()`。这才是 Wilder 1978 原版，
    也是 TradingView / 多数券商的默认 RSI。

    （旧实现用 `rolling(period).mean()` 是简单 SMA，数值更跳、与行情软件对不上，
    已废弃。）

    Wilder 原版 seed 用前 period 个值的 SMA；ewm 用首值 seed。两者差异随
    `(1-1/period)^n` 指数衰减，在 ≥ ~100 根 warmup 后 < 1e-3，传入 2y 数据时
    末位小数级误差，符合验收要求。
    """
    if len(close) < period + 1:
        return None
    delta = close.diff()
    # 首行 diff 为 NaN，填 0；不影响 ≥period warmup 后的递归结果
    gain = delta.clip(lower=0).fillna(0.0)
    loss = (-delta.clip(upper=0)).fillna(0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()
    last_gain = _safe_last(avg_gain)
    last_loss = _safe_last(avg_loss)
    if last_gain is None or last_loss is None:
        return None
    if last_loss == 0:
        return 100.0
    rs = last_gain / last_loss
    return float(100 - (100 / (1 + rs)))


def _atr_pct_series(df: pd.DataFrame, period: int = 14) -> Optional[pd.Series]:
    """逐日 ATR%（ATR / 当日收盘 * 100）序列，Wilder 平滑。

    True Range = max(high-low, |high-prev_close|, |low-prev_close|)，
    **正确包含跳空**（gap）。仅当 DataFrame 带 High/Low 列时走真 TR；DB 只有
    Close 时退化为 |ΔClose|（B 组补存 OHLC 后此退化分支不再走）。
    """
    if len(df) < period + 1:
        return None
    close = df["Close"]
    # 仅当 High/Low 列存在**且近 period+1 根无 NaN** 时才走真 TR。
    # （列存在但值为 NaN = 老数据未回填，此时退化为收盘价差，不能让 ATR 变 None）
    use_high_low = (
        "High" in df.columns and "Low" in df.columns
        and bool(df["High"].tail(period + 1).notna().all())
        and bool(df["Low"].tail(period + 1).notna().all())
    )
    if use_high_low:
        high = df["High"]
        low = df["Low"]
        prev_close = close.shift(1)
        # max(axis=1) 默认 skipna：首行 prev_close 为 NaN 时退化为 (high-low)
        tr = pd.concat(
            [
                (high - low).abs(),
                (high - prev_close).abs(),
                (low - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
    else:
        # 退化分支：DB 仅有 Close（无 High/Low）。低估真实波动，B 组补 OHLC 后失效。
        tr = close.diff().abs()
    atr = tr.ewm(alpha=1.0 / period, adjust=False).mean()
    return atr / close * 100


def _calc_atr_pct(df: pd.DataFrame, period: int = 14) -> Optional[float]:
    """ATR 百分比 = ATR / 当前收盘价 * 100（Wilder 平滑，周期默认 14）。

    平滑用 Wilder RMA（ewm alpha=1/period, adjust=False），不是 SMA。
    分母是最新收盘价（标准 ATR% 口径）。
    """
    series = _atr_pct_series(df, period=period)
    if series is None:
        return None
    val = _safe_last(series)
    return None if val is None else float(val)


def _calc_atr_spike_ratio(
    df: pd.DataFrame,
    period: int = 14,
    baseline_window: int = 252,
    min_periods: int = 120,
) -> Optional[float]:
    """波动突变比 = 当日 ATR% / 自身近 1 年（252 交易日）滚动中位 ATR%。

    独立快崩防御 ATR 腿的通用口径（2026-06）：尺度无关（任何资产自校准，
    无需 per-asset 绝对阈值），且对历史尖峰稳健——滚动分位制会被 2 年窗里的
    旧尖峰"脱敏"（2020 COVID 留窗内 → 2022 真实告警分位被压低），中位数基线
    不受尾部影响。≥2.0 = 波动较自身常态翻倍 = 快崩哨兵。
    样本不足 min_periods 时返回 None（graceful，防御腿不触发）。
    """
    series = _atr_pct_series(df, period=period)
    if series is None or len(series.dropna()) < min_periods:
        return None
    baseline = series.rolling(baseline_window, min_periods=min_periods).median()
    last_atr = _safe_last(series)
    last_base = _safe_last(baseline)
    if last_atr is None or last_base is None or last_base <= 0:
        return None
    return float(last_atr / last_base)


def _calc_atr_pct_median_1y(
    df: pd.DataFrame,
    period: int = 14,
    baseline_window: int = 252,
    min_periods: int = 120,
) -> Optional[float]:
    """近 1 年滚动中位 ATR%（atr_spike_ratio 的分母，单独暴露）。

    #113 regime 尺度无关化的趋势腿归一化因子：MA spread% ÷ 本值 =
    "趋势强度折算成多少个典型日波"。窗口/最小样本与 spike ratio 同参，
    保证 ma120 可算时本值几乎必可算（都要 ≥120 样本）。"""
    series = _atr_pct_series(df, period=period)
    if series is None or len(series.dropna()) < min_periods:
        return None
    baseline = series.rolling(baseline_window, min_periods=min_periods).median()
    val = _safe_last(baseline)
    return None if val is None or val <= 0 else float(val)


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


def _calc_price_quantile(close: pd.Series,
                         window: int = TRADING_DAYS_2Y) -> Optional[float]:
    """真百分位排名 (percentile rank)：近 `window` 交易日内收盘价 ≤ 当前价的比例 (0~1)。

    含义：返回 0.78 表示"近 2 年里 78% 的交易日收盘价 ≤ 当前价"，是统计意义上的分位。

    （旧实现是 (cur-min)/(max-min) 区间归一 —— 那是"当前价在最高最低之间的位置"，
    单根历史插针就能压缩整条区间、严重失真，且与"分位"语义不符，已废弃。）

    **窗口在函数内强制 tail(window)**（2026-06-13 口径修正）：此前靠 caller 传"恰好 2y"
    数据，但 get_history_data 实际返回全量 DB → 分位对全量历史算（price_quantile_2y
    名不副实，且随 DB 增长漂移）。现在无论传入多长，只取最后 window 根，与回测
    compute_regime_return_frame 的 rolling(TRADING_DAYS_2Y) 同口径。
    """
    valid = close.dropna()
    if valid.empty:
        return None
    valid = valid.tail(window)   # 强制近 2 年窗口，单一可信源 TRADING_DAYS_2Y
    cur = valid.iloc[-1]
    if pd.isna(cur):
        return None
    return float((valid <= cur).sum() / len(valid))


def _calc_return_30d(close: pd.Series, lookback: int = LOOKBACK_30D) -> Optional[float]:
    """过去 lookback 个交易日的累计收益率（cur/lookback日前 - 1，负数=跌）。

    给 regime crash 的"跌幅腿"用：return_30d ≤ -0.20 即 30 日累计跌 ≥ 20%。
    """
    if len(close) < lookback + 1:
        return None
    prev = close.iloc[-(lookback + 1)]
    cur = close.iloc[-1]
    if pd.isna(prev) or pd.isna(cur) or prev == 0:
        return None
    return float(cur / prev - 1)


def _calc_rebound_off_low(close: pd.Series, window: int = LOOKBACK_30D) -> Optional[float]:
    """当前价相对近 window 个交易日最低收盘价的反弹幅度 (cur/low - 1, 正数=已反弹)。

    给 regime recovery 用：rebound ≥ 0.10 即从近 30 日低点反弹 ≥ 10%。
    """
    if len(close) < window:
        return None
    recent = close.iloc[-window:]
    low = recent.min()
    cur = close.iloc[-1]
    if pd.isna(low) or pd.isna(cur) or low == 0:
        return None
    return float(cur / low - 1)


def _calc_rvol(df: pd.DataFrame, period: int = RVOL_WINDOW) -> Optional[float]:
    """相对成交量 RVOL = 当日成交量 / 前 period 日均量（不含当日）。

    > 1 = 放量，< 1 = 缩量。依赖 DB 补存 Volume（B 组）；无 Volume 列或数据不足
    返回 None。FX / 部分指数无真实成交量（Volume 全 0 或 NaN）时也返回 None。
    """
    if "Volume" not in df.columns:
        return None
    vol = df["Volume"]
    if len(vol) < period + 1:
        return None
    cur = vol.iloc[-1]
    window = vol.iloc[-(period + 1):-1]
    # 数据完整性闸门：当日量或基准窗口里出现 NaN / ≤0 → 成交量不可靠
    # （如 GC=F 连续合约、FX 的成交量稀疏/为 0），不给误导性的 RVOL。
    if pd.isna(cur) or cur <= 0:
        return None
    if window.isna().any() or (window <= 0).any():
        return None
    avg = window.mean()
    if avg == 0:
        return None
    return float(cur / avg)


def compute_metrics(df: pd.DataFrame) -> Dict[str, Any]:
    """从历史 K 线 DataFrame 算出所有市场指标。

    Args:
        df: pandas DataFrame，必须有 'Close' 列；'High'/'Low' 有则 ATR 走真 TR，
            'Volume' 有则能算 rvol

    Returns:
        dict 字段（缺数据时为 None）:
          current_price: 最新收盘价
          ma20 / ma120 / ma250: 20/120/250 日简单均线
          rsi14: 14 日 Wilder RSI
          price_quantile_2y: 真百分位排名（窗口内 ≤ 当前价的比例，0-1）—— 通常传 2 年数据
          atr_pct: 14 日 Wilder ATR 占当前价的百分比（真 TR，含跳空）
          atr_spike_ratio: 波动突变比（当日 ATR% / 近 1 年滚动中位 ATR%），
            regime crash 波动腿 + 独立快崩防御共用；样本 <120 时 None
          atr_pct_median_1y: 近 1 年滚动中位 ATR%（regime 趋势腿归一化因子）；
            样本 <120 时 None
          volatility_annualized: 年化波动率（基于全部样本）
          max_drawdown: 最大回撤（负数）
          return_30d: 过去 30 交易日累计收益率（regime crash 跌幅腿）
          rebound_off_30d_low: 相对近 30 日低点的反弹幅度（regime recovery）
          rvol: 相对成交量（当日量 / 前 20 日均量；无 Volume 时 None）
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
            "atr_spike_ratio": None,
            "atr_pct_median_1y": None,
            "volatility_annualized": None,
            "max_drawdown": None,
            "return_30d": None,
            "rebound_off_30d_low": None,
            "rvol": None,
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
        "atr_spike_ratio": _calc_atr_spike_ratio(df, period=14),
        "atr_pct_median_1y": _calc_atr_pct_median_1y(df, period=14),
        "volatility_annualized": _calc_volatility_annualized(close),
        "max_drawdown": _calc_max_drawdown(close),
        "return_30d": _calc_return_30d(close),
        "rebound_off_30d_low": _calc_rebound_off_low(close),
        "rvol": _calc_rvol(df),
        "n_samples": int(len(df)),
    }

# 完整历史导出面（含下划线名/常量）——façade `import *` 的完备性依赖本列表
__all__ = [
    "LOOKBACK_30D",
    "RVOL_WINDOW",
    "TRADING_DAYS_2Y",
    "_safe_last",
    "_calc_rsi",
    "_atr_pct_series",
    "_calc_atr_pct",
    "_calc_atr_spike_ratio",
    "_calc_atr_pct_median_1y",
    "_calc_max_drawdown",
    "_calc_volatility_annualized",
    "_calc_price_quantile",
    "_calc_return_30d",
    "_calc_rebound_off_low",
    "_calc_rvol",
    "compute_metrics",
]
