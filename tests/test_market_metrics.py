"""市场指标计算正确性测试（A 组指标算法修复 + B3 rvol）

覆盖:
- RSI 用 Wilder 平滑（对齐 TradingView/券商），不是 SMA
- ATR 用真 TR（含跳空）+ Wilder 平滑；有 High/Low 时不退化为收盘价差
- price_quantile_2y 是真百分位排名（≤ 当前价比例），不是 (cur-min)/(max-min)
- return_30d / rebound_off_30d_low / rvol 新增字段
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from openinvest.utils.market_metrics import (
    _calc_atr_pct,
    _calc_price_quantile,
    _calc_rebound_off_low,
    _calc_return_30d,
    _calc_rsi,
    compute_metrics,
)


def _close(values) -> pd.Series:
    return pd.Series([float(v) for v in values])


# ---------------- RSI: Wilder ----------------

def _reference_wilder_rsi(close: pd.Series, period: int = 14) -> float:
    """Wilder 1978 原版（SMA seed + 递归平滑）—— TradingView 同口径的参照实现"""
    delta = close.diff().dropna()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.iloc[:period].mean()
    avg_loss = loss.iloc[:period].mean()
    for i in range(period, len(gain)):
        avg_gain = (avg_gain * (period - 1) + gain.iloc[i]) / period
        avg_loss = (avg_loss * (period - 1) + loss.iloc[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def _sma_rsi(close: pd.Series, period: int = 14) -> float:
    """旧的错误实现（SMA 平滑）—— 用来证明新实现确实换了口径"""
    delta = close.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain.iloc[-1] / loss.iloc[-1]
    return 100 - 100 / (1 + rs)


def test_rsi_matches_wilder_reference():
    rng = np.random.default_rng(42)
    # 300 根：足够 warmup，ewm seed 与 SMA seed 的差异已衰减到末位小数
    prices = 100 + np.cumsum(rng.normal(0, 1, 300))
    close = _close(prices)
    got = _calc_rsi(close, period=14)
    ref = _reference_wilder_rsi(close, period=14)
    assert got is not None
    assert abs(got - ref) < 0.5, f"Wilder RSI 偏离参照: got={got}, ref={ref}"


def test_rsi_differs_from_old_sma_version():
    rng = np.random.default_rng(7)
    prices = 100 + np.cumsum(rng.normal(0, 1.5, 200))
    close = _close(prices)
    wilder = _calc_rsi(close, period=14)
    sma = _sma_rsi(close, period=14)
    # 两种平滑必须给出可区分的数值（否则说明没真的改成 Wilder）
    assert abs(wilder - sma) > 0.3, f"Wilder 与 SMA 太接近: {wilder} vs {sma}"


def test_rsi_bounds_and_all_up():
    # 全程上涨 → loss=0 → RSI=100
    close = _close(range(1, 60))
    assert _calc_rsi(close, period=14) == 100.0


# ---------------- ATR: 真 TR + Wilder ----------------

def test_atr_uses_true_range_with_high_low():
    """收盘价完全不动，但日内振幅大：真 TR 应捕捉到，收盘价差分支会算成 ~0"""
    n = 40
    df_hl = pd.DataFrame({
        "Close": [100.0] * n,
        "High": [105.0] * n,
        "Low": [95.0] * n,
    })
    df_close_only = pd.DataFrame({"Close": [100.0] * n})

    atr_hl = _calc_atr_pct(df_hl, period=14)
    atr_close = _calc_atr_pct(df_close_only, period=14)

    assert atr_hl is not None and atr_close is not None
    # 真 TR ≈ (105-95)/100 = 10%；收盘价差 = 0
    assert atr_hl > 9.0, f"真 TR ATR 应 ~10%, got {atr_hl}"
    assert atr_close < 0.01, f"收盘价差分支应 ~0, got {atr_close}"


def test_atr_includes_gap():
    """跳空：收盘价逐日 +1（close diff=1），但每天 High/Low 紧贴收盘。
    引入一个向下跳空日，真 TR 必须包含 |low-prev_close| 这一项。"""
    closes = [100, 101, 102, 103, 90, 91, 92, 93, 94, 95,
              96, 97, 98, 99, 100, 101]
    highs = [c + 0.5 for c in closes]
    lows = [c - 0.5 for c in closes]
    df = pd.DataFrame({"Close": closes, "High": highs, "Low": lows})
    atr = _calc_atr_pct(df, period=14)
    # 第 5 天 prev_close=103, low=89.5 → TR ≈ 13.5，远大于 high-low=1
    # 退化为 high-low（仅 1）则 ATR 会显著偏小
    df_no_gap_consideration = pd.DataFrame({"Close": closes})
    atr_close = _calc_atr_pct(df_no_gap_consideration, period=14)
    assert atr is not None and atr_close is not None
    assert atr > atr_close, f"含跳空的真 TR ATR 应 > 收盘价差版: {atr} vs {atr_close}"


def _ohlc_frame(n: int = 300) -> pd.DataFrame:
    """随机游走收盘 ±0.5%/日，日内振幅 ±1%：真 TR ≈ 2%，收盘价差 ≈ 0.4%（差 ~5 倍）"""
    rng = np.random.default_rng(3)
    close = 100 * np.cumprod(1 + rng.normal(0, 0.005, n))
    idx = pd.bdate_range("2025-01-01", periods=n)
    return pd.DataFrame({"Close": close, "High": close * 1.01, "Low": close * 0.99}, index=idx)


def _with_close_only_row(df: pd.DataFrame) -> pd.DataFrame:
    """末尾追加一根只有收盘价的行（现价缓存写入的盘中/节假日行），涨 1.5%"""
    row = pd.DataFrame({"Close": [df["Close"].iloc[-1] * 1.015]},
                       index=[df.index[-1] + pd.Timedelta(days=1)])
    return pd.concat([df, row])


def test_atr_trailing_close_only_row_does_not_collapse_series():
    """一根缺 H/L 的末行（盘中现价缓存）不计入 TR、ATR 沿用前值，不能把整条序列打成收盘价差
    （旧口径：近 15 根任一缺 H/L → 全序列退化，ATR 掉到真实值的 ~20-40%）"""
    df = _ohlc_frame()
    clean, dirty = compute_metrics(df), compute_metrics(_with_close_only_row(df))
    for key in ("atr_pct", "atr_pct_median_1y", "atr_spike_ratio"):
        assert abs(dirty[key] / clean[key] - 1) < 0.05, (key, clean[key], dirty[key])


def test_atr_close_only_frame_uses_close_diff():
    """整表无 High/Low（无列或列全 NaN）→ 仍走 |ΔClose| 的 Wilder ATR"""
    df = _ohlc_frame(60)
    close = df["Close"]
    expected = float((close.diff().abs().ewm(alpha=1 / 14, adjust=False).mean() / close * 100).iloc[-1])
    assert abs(_calc_atr_pct(df[["Close"]], period=14) - expected) < 1e-9
    df_nan_hl = df.assign(High=np.nan, Low=np.nan)
    assert abs(_calc_atr_pct(df_nan_hl, period=14) - expected) < 1e-9


def test_atr_unbackfilled_old_segment_does_not_inflate_spike():
    """前段老数据没回填 H/L、后段 OHLC 不到 120 根：老段不进 1 年中位（旧口径同款排除），
    spike 不能被"老段 |ΔClose| 拉低的中位"虚抬成快崩（≥2.0）"""
    df = _ohlc_frame(400)
    df.iloc[:300, df.columns.get_loc("High")] = np.nan
    df.iloc[:300, df.columns.get_loc("Low")] = np.nan
    m = compute_metrics(df)
    assert m["atr_spike_ratio"] is None or m["atr_spike_ratio"] < 1.5
    assert m["atr_pct"] > 1.5  # 后段真 TR ≈ 2%，不被老段收盘价差稀释


def test_atr_regime_frame_matches_market_metrics_with_close_only_row():
    """regime_probability 全序列回放与 market_metrics 同源：含缺 H/L 行时逐日 ATR% 完全一致"""
    from openinvest.calc.market_metrics import _atr_pct_series
    from openinvest.calc.regime_probability import compute_regime_return_frame

    df = _with_close_only_row(_ohlc_frame())
    df.iloc[100, df.columns.get_loc("High")] = np.nan  # 中段再挖一根（节假日现价行）
    frame = compute_regime_return_frame(df, windows=("30d",))
    np.testing.assert_allclose(frame["atr_pct"].values, _atr_pct_series(df, 14).values, rtol=1e-12)


# ---------------- 价格分位: 真百分位排名 ----------------

def test_price_quantile_is_percentile_rank():
    # 当前价是历史最大 → 百分位 = 1.0
    assert _calc_price_quantile(_close(range(1, 101))) == 1.0


def test_price_quantile_differs_from_minmax_on_outlier():
    """一根历史插针：百分位排名稳健，(cur-min)/(max-min) 会被压扁"""
    series = _close([10] * 50 + [1000] + [10])  # 末值=10
    pct = _calc_price_quantile(series)
    # 52 个值里有 51 个 ≤ 10 → ~0.98
    assert pct > 0.9, f"百分位应接近 1（多数 ≤ 当前）, got {pct}"
    # 对照：min-max 归一会算成 (10-10)/(1000-10)=0 —— 证明口径不同
    minmax = (10 - 10) / (1000 - 10)
    assert abs(pct - minmax) > 0.5


# ---------------- 新增字段 ----------------

def test_return_30d():
    # 31 个点，从 100 跌到 80 → -20%
    closes = list(np.linspace(100, 80, 31))
    assert abs(_calc_return_30d(_close(closes)) - (-0.20)) < 1e-9


def test_rebound_off_low():
    # 近 30 日最低 80，当前 92 → 反弹 15%
    closes = [100] * 5 + [80] + [92]  # window=30 但样本<30 → None
    assert _calc_rebound_off_low(_close(closes)) is None
    closes2 = [100] * 20 + [80] + [85] * 8 + [92]  # len=30
    reb = _calc_rebound_off_low(_close(closes2))
    assert reb is not None and abs(reb - (92 / 80 - 1)) < 1e-9


def test_rvol_present_and_absent():
    n = 30
    df = pd.DataFrame({
        "Close": [100.0] * n,
        "Volume": [1000.0] * (n - 1) + [3000.0],  # 末日放量 3x
    })
    m = compute_metrics(df)
    assert m["rvol"] is not None and abs(m["rvol"] - 3.0) < 1e-9
    # 无 Volume 列 → None
    m2 = compute_metrics(pd.DataFrame({"Close": [100.0] * n}))
    assert m2["rvol"] is None


def test_compute_metrics_empty_has_new_keys():
    m = compute_metrics(pd.DataFrame())
    for k in ("return_30d", "rebound_off_30d_low", "rvol"):
        assert k in m and m[k] is None


# ---------- 口径修正：price_quantile 强制 tail(504)（2026-06-13 审计）----------

def test_price_quantile_uses_only_last_504():
    """传入超长序列时，分位只对最后 504 根算（不对全量）——根因 bug 修正"""
    import pandas as pd
    from openinvest.utils.market_metrics import _calc_price_quantile, TRADING_DAYS_2Y
    assert TRADING_DAYS_2Y == 504
    # 前 600 根全是高价(1000)，后 504 根递增 1..504；当前价=504 在近504窗里是最高=100%
    old = [1000.0] * 600
    recent = [float(i) for i in range(1, 505)]
    s = pd.Series(old + recent)
    q = _calc_price_quantile(s)
    # 若错误地对全量(1104根)算：504 比 600 个 1000 都小 → 分位很低
    # 正确(近504)：504 是窗口内最大 → 100%
    assert q == 1.0, f"应只看近504根(分位100%)，实得 {q}"


def test_price_quantile_window_param_overridable():
    import pandas as pd
    from openinvest.utils.market_metrics import _calc_price_quantile
    s = pd.Series([float(i) for i in range(1, 101)])  # 1..100 递增
    # window=10 → 当前价 100 在近10根(91..100)里最大 = 100%
    assert _calc_price_quantile(s, window=10) == 1.0


def test_trading_days_2y_single_source():
    """regime_probability 与 market_metrics 引同一个 504（单一可信源）"""
    from openinvest.utils.market_metrics import TRADING_DAYS_2Y
    from openinvest.core.regime_probability import _TRADING_DAYS_2Y
    assert TRADING_DAYS_2Y is _TRADING_DAYS_2Y or TRADING_DAYS_2Y == _TRADING_DAYS_2Y == 504
