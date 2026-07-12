"""Regime 概率统计纯核（calc 层，ADR-026）

从 core/regime_probability.py 拆出的纯计算：概率分布数据类、分位数、
币种映射、regime→forward return 帧计算。全部函数吃传入的数据（df/closes/
returns 列表），零 IO。数据加载（MarketStore/verdict_review.jsonl/汇率腿）
留在 core/regime_probability.py（IO shell）。

方向纪律：calc/regime 不得 import 本模块（本模块依赖 calc.regime.classify_regime，
反向会成环）——与旧 core/regime.py 时代的同一条纪律。
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

# 默认阈值：30d return ±5% 算"大涨/大跌"
DEFAULT_THRESHOLD_PCT = 5.0
# 低于此样本量标记 low_confidence
MIN_CONFIDENT_N = 10
# 买回参考用的"悲观但可能"分位。⚠️ 口径 = forward return 的**期末**分位（D+window 收盘），
# 不是途中最低价——途中触及某价位的概率恒 ≥ 期末仍低于它的概率，两者不可互换
#（issue #179 P1-A⑦；要真"途中低点"分布需用 min_w 路径统计，暂未接入）
REENTRY_DOWNSIDE_QUANTILE = 0.20


@dataclass
class RegimeProbability:
    """单个 (asset, regime) 的概率分布"""
    asset: str
    regime: str
    n: int
    p_up: float          # P(return > threshold%)
    p_down: float        # P(return < -threshold%)
    p_flat: float        # P(|return| <= threshold%)
    median_return: float # 中位 return (%)
    mean_return: float   # 均值 return (%)
    threshold_pct: float # 使用的阈值
    low_confidence: bool # effective_n < MIN_CONFIDENT_N（重叠窗口源用独立样本判定）
    # 独立样本估计：重叠窗口源（OHLC 日度 forward return）≈ n/窗口天数；
    # 非重叠源（verdict_review，逐决策）= n。默认 0 → summary 回退用 n。
    effective_n: int = 0

    def summary_line(self) -> str:
        """一行中文摘要，给 daily report 用"""
        conf = " ⚠样本不足" if self.low_confidence else ""
        eff = self.effective_n or self.n
        # 重叠窗口源：明确标注独立样本量，避免 n 被误读为独立观测数
        overlap = f"，重叠窗口独立≈{eff}" if eff != self.n else ""
        return (
            f"{self.asset} {self.regime} (n={self.n}{overlap}): "
            f"涨>{self.threshold_pct:.0f}% {self.p_up * 100:.0f}%、"
            f"跌>{self.threshold_pct:.0f}% {self.p_down * 100:.0f}%、"
            f"中位 {self.median_return:+.1f}%"
            f"{conf}"
        )


def _percentile(values: List[float], q: float) -> float:
    """线性插值分位数（q ∈ [0,1]）。values 非空。"""
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * q
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return xs[int(k)]
    return xs[lo] * (hi - k) + xs[hi] * (k - lo)


@dataclass
class ReentryEstimate:
    """单个 (asset, regime, window) 的卖出后路径 / 买回点估计。

    基于历史 forward return 分布给"悲观但可能"的低点 + 触及概率，
    作为 CIO 出 TRIM 时填 REENTRY_PRICE 的数据参考（不是强制值）。
    """
    asset: str
    regime: str
    window: str
    n: int
    current_price: float
    threshold_pct: float
    p_below_current: float   # P(forward return < 0)，即跌破现价概率
    p_down: float            # P(forward return < -threshold%)
    median_return_pct: float
    downside_pct: float      # forward return 的低分位（REENTRY_DOWNSIDE_QUANTILE）
    downside_price: float    # current_price × (1 + downside_pct/100)
    has_downside: bool       # downside_price < current_price → 存在低于现价的买回参考
    low_confidence: bool     # effective_n < MIN_CONFIDENT_N（重叠窗口源用独立样本判定）
    # 独立样本估计：OHLC 重叠日度 forward return ≈ n/窗口天数；verdict_review = n。
    effective_n: int = 0
    # 报价币种符号（display 用）。GC=F 报价是美元/盎司，硬编码 ¥ 会把 $4116
    # 标成 ¥4116（2026-06-12 用户邮件困惑事故）。channel 币种（如积存金 ¥/克）
    # 是另一回事，由 CIO 的渠道快照负责。
    currency: str = "¥"

    def summary_line(self) -> str:
        conf = " ⚠样本不足" if self.low_confidence else ""
        eff = self.effective_n or self.n
        overlap = f"，重叠窗口独立≈{eff}" if eff != self.n else ""
        if not self.has_downside:
            return (
                f"{self.window} (n={self.n}{overlap}): 该 regime 历史上 {self.window} 内"
                f"期末仍低于现价的概率仅 {self.p_below_current * 100:.0f}%，"
                f"{int(REENTRY_DOWNSIDE_QUANTILE * 100)} 分位仍 {self.downside_pct:+.1f}%"
                f"（期末口径下无明显低于现价的买回参考）{conf}"
            )
        return (
            f"{self.window} (n={self.n}{overlap}): 期末低于现价概率 {self.p_below_current * 100:.0f}%、"
            f"跌>{self.threshold_pct:.0f}% 概率 {self.p_down * 100:.0f}%；"
            f"悲观情形({int(REENTRY_DOWNSIDE_QUANTILE * 100)}分位·期末) {self.downside_pct:+.1f}% "
            f"→ {self.currency}{self.downside_price:,.2f}；中位 {self.median_return_pct:+.1f}%{conf}"
        )


def calibrate_profile(
    profile: Dict[str, Any],
    *,
    shrinkage_k: Optional[float] = None,
    band_gamma: Optional[float] = None,
) -> Dict[str, Any]:
    """路径分布校准层（2026-06，walk-forward 时代分桶诊断出两个结构性缺陷的修复）：

    1. **小样本收缩**：条件分布按 effective_n 向同资产无条件分布收缩，
       λ = eff_n / (eff_n + k)。治 downtrend 这类独立样本个位数的桶拿 4 个
       样本装精确（GC 07-09 downtrend 带覆盖实测 7%）。k=0 → λ=1 → 禁用。
    2. **带宽校准**：P10/P90/downside 围绕中位按 γ 扩张（带覆盖 7 个时代里
       6 个 < 80% 目标，结构性偏窄）。γ=1 → 禁用。

    参数默认从 config 读（defaults.yaml `path:` 节 → PathConfig，经 fit/OOS
    验证前默认禁用，ADR-010 rule 4）；显式传参覆盖（fit 脚本网格搜索用）。
    返回新 dict（不改原 profile），uncond_windows 缺失时原样返回。
    """
    if shrinkage_k is None or band_gamma is None:
        from openinvest.core.config import load_config
        cfg = load_config().path
        if shrinkage_k is None:
            shrinkage_k = cfg.shrinkage_k
        if band_gamma is None:
            band_gamma = cfg.band_gamma
    if (not shrinkage_k and band_gamma == 1.0) or not profile:
        return profile
    uncond = profile.get("uncond_windows") or {}
    out = dict(profile)
    out["windows"] = {}
    out["calibration"] = {"shrinkage_k": shrinkage_k, "band_gamma": band_gamma}
    for w, st in (profile.get("windows") or {}).items():
        st = dict(st)
        u = uncond.get(w)
        if shrinkage_k and u:
            lam = st["effective_n"] / (st["effective_n"] + shrinkage_k)
            # 只校准两边都有的字段（fit 脚本喂的 mini-profile 无 p_down）
            for key in ("median_pct", "p_below", "p_down",
                        "p10_pct", "p90_pct", "downside_pct"):
                if key in st and key in u:
                    st[key] = round(lam * st[key] + (1 - lam) * u[key], 4)
            # 概率字段 clip 护栏：凸组合本身封闭于 [0,1]，这里只防上游脏数据静默传播
            for key in ("p_below", "p_down"):
                if key in st:
                    st[key] = min(1.0, max(0.0, st[key]))
        if band_gamma != 1.0:
            med = st["median_pct"]
            for key in ("p10_pct", "p90_pct", "downside_pct"):
                if key in st:
                    st[key] = round(med + band_gamma * (st[key] - med), 4)
        out["windows"][w] = st
    return out


def quote_currency_prefix(asset: str) -> str:
    """yfinance 报价币种符号（display 用，按 ticker 后缀判定）。

    注意是**报价**币种：GC=F 报美元/盎司、NDQ.AX 报澳元——与用户交易渠道的
    币种/单位（如浙商积存金 ¥/克）无关。未识别后缀返回 "$"（yfinance 无后缀
    ticker 默认美股）；指数（^ 开头）是点位无币种，返回 ""。
    """
    a = asset.upper()
    if a.startswith("^"):
        return ""
    if a.endswith(".AX"):
        return "A$"
    if a.endswith(".HK"):
        return "HK$"
    if a.endswith((".SS", ".SZ")):
        return "¥"
    return "$"


# yfinance 交易所后缀 → 报价币种 ISO。覆盖主流交易所；漏标会让 convert_ccy_for 误判
# 币种错配、驱动一条假汇率卷积（如 .L 报 GBP 却当 USD）。未知后缀按 USD 兜底,但因
# currency_overlay 仅在汇率序列真实存在时才挂(get_path_profile),不会凭空臆造一条 overlay。
_QUOTE_CCY_SUFFIX = {
    ".AX": "AUD", ".HK": "HKD", ".SS": "CNY", ".SZ": "CNY",
    ".L": "GBP", ".T": "JPY", ".TO": "CAD", ".V": "CAD",
    ".PA": "EUR", ".DE": "EUR", ".AS": "EUR", ".BR": "EUR", ".MI": "EUR",
    ".MC": "EUR", ".LS": "EUR", ".F": "EUR", ".VI": "EUR", ".HE": "EUR", ".IR": "EUR",
    ".SW": "CHF", ".SI": "SGD", ".KS": "KRW", ".KQ": "KRW",
    ".TWO": "TWD", ".TW": "TWD", ".NS": "INR", ".BO": "INR",
    ".SA": "BRL", ".ST": "SEK", ".OL": "NOK", ".CO": "DKK",
    ".NZ": "NZD", ".JO": "ZAR", ".BK": "THB", ".JK": "IDR",
}
# 加密对 BTC-USD / ETH-EUR 的计价币（- 后缀）；稳定币按其锚定法币算汇率
_CRYPTO_QUOTE_CCY = {
    "USD": "USD", "USDT": "USD", "USDC": "USD", "EUR": "EUR", "GBP": "GBP",
    "JPY": "JPY", "AUD": "AUD", "CNY": "CNY", "KRW": "KRW",
}


def quote_currency_iso(asset: str) -> Optional[str]:
    """yfinance 报价币种 ISO（GC=F→USD, NDQ.AX→AUD, 0700.HK→HKD, 510300.SS→CNY, SHEL.L→GBP）。
    指数（^）无币种 → None。与 quote_currency_prefix 同源后缀规则，供汇率卷积用。"""
    a = asset.upper()
    if a.startswith("^"):
        return None
    if "-" in a:  # 加密对：BTC-USD / ETH-EUR
        return _CRYPTO_QUOTE_CCY.get(a.rsplit("-", 1)[1], "USD")
    for suf, ccy in _QUOTE_CCY_SUFFIX.items():  # .TWO 在 .TW 前 → 长后缀优先
        if a.endswith(suf):
            return ccy
    return "USD"


def convert_ccy_for(asset: str, holding_cost_ccy: Optional[str]) -> Optional[str]:
    """持仓计价币种 != 资产报价币种 → 返回需转换到的币种（path-profile 汇率卷积用），否则 None。
    例：GC=F 报价 USD 但浙商积存金 cost_currency=CNY → 返回 "CNY"。"""
    base = quote_currency_iso(asset)
    if holding_cost_ccy and base and holding_cost_ccy.upper() != base:
        return holding_cost_ccy.upper()
    return None


# price_quantile_2y 的滚动窗口（≈2 年交易日），对齐 compute_metrics 传 "2y" 数据的口径
from openinvest.calc.market_metrics import TRADING_DAYS_2Y as _TRADING_DAYS_2Y  # 单一可信源（504）


def forward_return(
    symbol: str, asof: str, calendar_days: int,
    *, closes: "pd.Series",
) -> Optional[float]:
    """决策日→N 个**日历日**后的收益（小数，非 %）。canonical 单一可信源。

    口径（与 compute_regime_return_frame / path_review.realized_path 逐位一致）：
    - base = 价格序列中 **≤ asof 的最后一根收盘**
    - target = **≥ asof + calendar_days 日历天的首根收盘**
    - 窗口未走完（target 落在序列尾部之外）→ None（未成熟，不补值）

    **日历天**是全系统统一口径：路径分布、概率表、干预反事实账本都按日历天，
    这样"30d 反事实损益"和 CIO 当时看的"30d 路径分布"是同一时间跨度。
    注意区别于 df.shift(-N)（那是 N 个**交易日** ≈ 1.4×N 日历天）。

    calc 纯核：closes **必传**（IO 由 core.regime_probability.forward_return
    同签名包装负责——closes=None 时从 MarketStore 拉取后委托这里）。
    """
    import pandas as pd  # 本模块 pandas 走惰性 import（见 compute_regime_return_frame）
    if closes is None or len(closes) == 0:
        return None
    idx = closes.index
    ts = pd.Timestamp(asof)
    i = idx.searchsorted(ts, side="right") - 1
    if i < 0:
        return None
    j = idx.searchsorted(ts + pd.Timedelta(days=calendar_days), side="left")
    if j >= len(idx):
        return None
    return float(closes.iloc[j]) / float(closes.iloc[i]) - 1.0


def _percentile_rank(window):  # window: np.ndarray
    cur = window[-1]
    return float((window <= cur).sum() / len(window))


def _make_regime_probability(
    asset: str, regime: str, returns_pct: List[float], threshold_pct: float,
    *, window_days: int = 1,
) -> RegimeProbability:
    """从一组 forward return(%) 聚合出 RegimeProbability。

    window_days: forward return 的窗口天数。OHLC 源是**重叠**的日度 forward return
    （相邻样本重叠 window_days-1 天），独立样本 ≈ n/window_days；low_confidence 用这个
    effective_n 而非原始 n 判定，避免重叠样本把置信度撑虚高。verdict_review 源是逐决策
    的非重叠样本 → window_days=1 → effective_n = n。
    """
    n = len(returns_pct)
    effective_n = max(1, n // window_days)
    p_up = sum(1 for r in returns_pct if r > threshold_pct) / n
    p_down = sum(1 for r in returns_pct if r < -threshold_pct) / n
    return RegimeProbability(
        asset=asset, regime=regime, n=n,
        p_up=round(p_up, 4), p_down=round(p_down, 4),
        p_flat=round(1.0 - p_up - p_down, 4),
        median_return=round(statistics.median(returns_pct), 2),
        mean_return=round(statistics.mean(returns_pct), 2),
        threshold_pct=threshold_pct,
        low_confidence=effective_n < MIN_CONFIDENT_N,
        effective_n=effective_n,
    )


def compute_regime_return_frame(
    df, symbol: Optional[str] = None, *, windows: Tuple[str, ...] = ("30d", "90d"),
):
    """对历史每一天算 regime + forward return（纯算术，复用 production classify_regime）。

    Args:
        df: OHLC DataFrame（index=DatetimeIndex，必有 Close，有 High/Low 走真 TR）
        symbol: 传则用 per-asset regime 阈值
        windows: forward return 窗口（"30d"/"90d"，按**日历日**前看）

    Returns:
        DataFrame：index=date，列 =
          - "regime"
          - "fwd_<w>"     窗口期末 forward return（小数）
          - "min_<w>"     窗口内**途中最深回踩**（相对当日收盘的最低点收益，小数）
          - "tmin_<w>"    到达谷底的交易日数（路径时序："什么时候跌"）
          - "max_<w>"     窗口内**途中最高冲高**（镜像 min；卖出时机的核心读数：
                          "先给更高卖点再跌"要靠它才看得见——min 轴只问跌没跌过）
          - "tmax_<w>"    到达顶点的交易日数（"什么时候涨"）
          - "persist_<w>" 当日 regime 在窗内实际持续的交易日数（含当日，cap 到窗末）
                          ——形状占比混着 regime 切换成分，靠它判断解读权重
          - "wdays_<w>"   窗内交易日数（persist 的分母参照）
          - "atr_pct"     当日 ATR%（路径形状的自校准"显著"单位，≥1×ATR 才算数）
        指标 warmup 不足的头部 regime=unknown；lookahead 不足的尾部各前向列=NaN。
        空输入返回空 DataFrame。
    """
    import numpy as np
    import pandas as pd
    from openinvest.calc.regime import classify_regime

    if df is None or df.empty or "Close" not in df.columns:
        return pd.DataFrame()

    close = df["Close"].astype(float)
    n = len(close)

    ma20 = close.rolling(20).mean()
    ma120 = close.rolling(120).mean()
    ret30 = close / close.shift(30) - 1.0
    rebound = close / close.rolling(30).min() - 1.0
    quantile = close.rolling(_TRADING_DAYS_2Y, min_periods=20).apply(
        _percentile_rank, raw=True,
    )

    # ATR%（Wilder RMA，真 TR 含跳空）—— 复刻 utils.market_metrics._calc_atr_pct 口径，
    # 但保留为全序列（原函数只返回末值）
    # 口径对齐 market_metrics._atr_pct_series：仅当 High/Low 近 15 根（period+1）
    # 全非 NaN 才走真 TR，否则退化收盘价差——两路径必须同源（#113 后 atr 喂两条归一化腿）
    has_hl = (
        "High" in df.columns and "Low" in df.columns
        and bool(df["High"].tail(15).notna().all())
        and bool(df["Low"].tail(15).notna().all())
    )
    if has_hl:
        high, low = df["High"].astype(float), df["Low"].astype(float)
        prev_close = close.shift(1)
        tr = pd.concat([
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)
    else:
        tr = close.diff().abs()
    atr_pct = tr.ewm(alpha=1.0 / 14, adjust=False).mean() / close * 100.0
    # #113 尺度无关分类需要的两个归一化序列（口径同 market_metrics：252 窗 / ≥120 样本中位）
    atr_med = atr_pct.rolling(252, min_periods=120).median()
    # 分母 ≤0（长期横盘/填充价）→ NaN → _v() 转 None，与 live _calc_atr_spike_ratio 同语义
    atr_med = atr_med.where(atr_med > 0)
    atr_spike = atr_pct / atr_med

    def _v(x):
        return None if pd.isna(x) else float(x)

    regimes = []
    for i in range(n):
        regimes.append(classify_regime({
            "ma20": _v(ma20.iat[i]),
            "ma120": _v(ma120.iat[i]),
            "atr_pct": _v(atr_pct.iat[i]),
            "atr_spike_ratio": _v(atr_spike.iat[i]),
            "atr_pct_median_1y": _v(atr_med.iat[i]),
            "price_quantile_2y": _v(quantile.iat[i]),
            "return_30d": _v(ret30.iat[i]),
            "rebound_off_30d_low": _v(rebound.iat[i]),
        }, symbol=symbol)["regime"])

    out = pd.DataFrame({"regime": regimes}, index=df.index)

    # regime 前向连续段长（不 cap）：persist_<w> 用。倒序扫一遍 O(n)。
    run = np.ones(n, dtype=int)
    for k in range(n - 2, -1, -1):
        if regimes[k + 1] == regimes[k]:
            run[k] = run[k + 1] + 1

    # forward return（日历日）：找 date + Nd 当天或之后第一个收盘
    idx = df.index
    vals = close.values
    arange = np.arange(n)
    for w in windows:
        days = int(w.rstrip("d"))
        pos = idx.searchsorted(idx + pd.Timedelta(days=days), side="left")
        fwd = np.full(n, np.nan)
        valid = pos < n
        fwd[valid] = vals[pos[valid]] / vals[arange[valid]] - 1.0
        out[f"fwd_{w}"] = fwd
        # 路径化（2026-06）：窗口内途中最深回踩/最高冲高 + 各自到达的交易日数。
        # 只在 lookahead 完整（valid）的行算，与 fwd 同口径。
        mins = np.full(n, np.nan)
        tmins = np.full(n, np.nan)
        maxs = np.full(n, np.nan)
        tmaxs = np.full(n, np.nan)
        persists = np.full(n, np.nan)
        wdays = np.full(n, np.nan)
        for i in np.flatnonzero(valid):
            seg = vals[i + 1: pos[i] + 1]
            if seg.size:
                j = int(np.argmin(seg))
                mins[i] = seg[j] / vals[i] - 1.0
                tmins[i] = j + 1  # 交易日数（t+1 起算）
                j = int(np.argmax(seg))
                maxs[i] = seg[j] / vals[i] - 1.0
                tmaxs[i] = j + 1
            # 窗内交易日数（含当日）与 regime 实际持续（精确逐行 cap，无近似魔数）
            wdays[i] = pos[i] - i + 1
            persists[i] = min(run[i], pos[i] - i + 1)
        out[f"min_{w}"] = mins
        out[f"tmin_{w}"] = tmins
        out[f"max_{w}"] = maxs
        out[f"tmax_{w}"] = tmaxs
        out[f"persist_{w}"] = persists
        out[f"wdays_{w}"] = wdays

    # 当日 ATR%：路径形状的自校准"显著"单位（无绝对百分比 magic number）
    out["atr_pct"] = atr_pct.values

    return out



__all__ = [
    "DEFAULT_THRESHOLD_PCT",
    "MIN_CONFIDENT_N",
    "REENTRY_DOWNSIDE_QUANTILE",
    "RegimeProbability",
    "ReentryEstimate",
    "_percentile",
    "_percentile_rank",
    "_make_regime_probability",
    "calibrate_profile",
    "quote_currency_prefix",
    "quote_currency_iso",
    "convert_ccy_for",
    "_QUOTE_CCY_SUFFIX",
    "_CRYPTO_QUOTE_CCY",
    "forward_return",
    "compute_regime_return_frame",
]
