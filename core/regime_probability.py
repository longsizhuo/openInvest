"""Regime 概率表 — 基于历史 verdict_review 数据的条件概率分布

设计决策（2026-05-28）：
- 按 (asset, regime) 分组，不按 verdict 分。原因：regime 是信号，verdict 几乎无增量。
  验证见 /tmp/regime_vs_verdict_signal.md。
- 阈值 5% 做成参数，默认 5%。
- n<10 的组返回 low_confidence 标记。
- 数据源：memory/.dreams/verdict_review.jsonl（由 jobs/verdict_review.py 生成）。
"""
from __future__ import annotations

import json
import logging
import math
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# 默认阈值：30d return ±5% 算"大涨/大跌"
DEFAULT_THRESHOLD_PCT = 5.0
# 低于此样本量标记 low_confidence
MIN_CONFIDENT_N = 10
# 买回参考用的"悲观但可能"分位（forward return 的低分位 → 卖出后可能触及的低点）
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
    low_confidence: bool # n < MIN_CONFIDENT_N

    def summary_line(self) -> str:
        """一行中文摘要，给 daily report 用"""
        conf = " ⚠样本不足" if self.low_confidence else ""
        return (
            f"{self.asset} {self.regime} (n={self.n}): "
            f"涨>{self.threshold_pct:.0f}% {self.p_up * 100:.0f}%、"
            f"跌>{self.threshold_pct:.0f}% {self.p_down * 100:.0f}%、"
            f"中位 {self.median_return:+.1f}%"
            f"{conf}"
        )


def _load_reviews(jsonl_path: Path) -> List[Dict[str, Any]]:
    """读 verdict_review.jsonl，返回记录列表。文件不存在返空列表。"""
    if not jsonl_path.exists():
        return []
    records = []
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return records


def build_probability_table(
    jsonl_path: Path,
    threshold_pct: float = DEFAULT_THRESHOLD_PCT,
    window: str = "30d",
) -> Dict[Tuple[str, str], RegimeProbability]:
    """从 verdict_review.jsonl 构建 (asset, regime) → 概率分布 表。

    Args:
        jsonl_path: verdict_review.jsonl 路径
        threshold_pct: 涨跌阈值百分比，默认 5%
        window: 使用的 return 窗口，默认 "30d"

    Returns:
        dict[(asset, regime)] → RegimeProbability
    """
    records = _load_reviews(jsonl_path)
    if not records:
        return {}

    # 按 (asset, regime) 分组
    groups: Dict[Tuple[str, str], List[float]] = {}
    for r in records:
        asset = r.get("asset", "")
        regime = r.get("regime_at_decision", "")
        ret = r.get("actual_returns", {}).get(window)
        if not asset or not regime or ret is None:
            continue
        key = (asset, regime)
        groups.setdefault(key, []).append(ret * 100)  # 转为百分比

    # 计算概率
    table: Dict[Tuple[str, str], RegimeProbability] = {}
    for (asset, regime), returns in groups.items():
        n = len(returns)
        p_up = sum(1 for r in returns if r > threshold_pct) / n
        p_down = sum(1 for r in returns if r < -threshold_pct) / n
        p_flat = 1.0 - p_up - p_down
        table[(asset, regime)] = RegimeProbability(
            asset=asset,
            regime=regime,
            n=n,
            p_up=round(p_up, 4),
            p_down=round(p_down, 4),
            p_flat=round(p_flat, 4),
            median_return=round(statistics.median(returns), 2),
            mean_return=round(statistics.mean(returns), 2),
            threshold_pct=threshold_pct,
            low_confidence=n < MIN_CONFIDENT_N,
        )

    return table


def get_regime_probability(
    asset: str,
    regime: str,
    table: Optional[Dict[Tuple[str, str], RegimeProbability]] = None,
    jsonl_path: Optional[Path] = None,
    threshold_pct: float = DEFAULT_THRESHOLD_PCT,
) -> Optional[RegimeProbability]:
    """查询单个 (asset, regime) 的概率分布。

    可以传预构建的 table（避免重复读文件），或传 jsonl_path 现场构建。
    两者都传时 table 优先。

    Returns:
        RegimeProbability 或 None（无数据）
    """
    if table is None:
        if jsonl_path is None:
            # 默认路径
            from core.memory_store import MemoryStore
            jsonl_path = MemoryStore().root / ".dreams" / "verdict_review.jsonl"
        table = build_probability_table(jsonl_path, threshold_pct)
    return table.get((asset, regime))


# ============================================================================
# 买回点估计 (reentry) — 给 TRIM 决策用："卖出后历史上会跌到哪、概率多大"
# ============================================================================


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
    low_confidence: bool     # n < MIN_CONFIDENT_N

    def summary_line(self) -> str:
        conf = " ⚠样本不足" if self.low_confidence else ""
        if not self.has_downside:
            return (
                f"{self.window} (n={self.n}): 该 regime 历史上 {self.window} 内"
                f"跌破现价概率仅 {self.p_below_current * 100:.0f}%，"
                f"{int(REENTRY_DOWNSIDE_QUANTILE * 100)} 分位仍 {self.downside_pct:+.1f}%"
                f"（无明显低于现价的买回点）{conf}"
            )
        return (
            f"{self.window} (n={self.n}): 跌破现价概率 {self.p_below_current * 100:.0f}%、"
            f"跌>{self.threshold_pct:.0f}% 概率 {self.p_down * 100:.0f}%；"
            f"悲观情形({int(REENTRY_DOWNSIDE_QUANTILE * 100)}分位) {self.downside_pct:+.1f}% "
            f"→ ¥{self.downside_price:,.2f}；中位 {self.median_return_pct:+.1f}%{conf}"
        )


def get_reentry_estimate(
    asset: str,
    regime: str,
    current_price: Optional[float],
    *,
    window: str = "30d",
    source: str = "ohlc",
    jsonl_path: Optional[Path] = None,
    records: Optional[List[Dict[str, Any]]] = None,
    threshold_pct: float = DEFAULT_THRESHOLD_PCT,
    days: int = 100000,
) -> Optional[ReentryEstimate]:
    """基于历史 forward return 分布给出卖出后买回点参考。

    Args:
        asset / regime: 分组键
        current_price: 现价（cost currency）。None / <=0 → 返回 None（无法折算价格）
        window: forward return 窗口，"30d" / "90d"。该窗口无样本 → 返回 None
        source: "ohlc"（默认，几十年 OHLC 直算，0 token）| "verdict_review"（旧源，保留）
        records / jsonl_path: 仅 source="verdict_review" 时用

    Returns:
        ReentryEstimate 或 None（无现价 / 该 window 无历史样本）
    """
    if current_price is None or current_price <= 0:
        return None

    if source == "ohlc":
        returns = _ohlc_forward_returns(asset, regime, window, days=days)
    else:
        # verdict_review 源（保留：命中率/校准仍用；这里只在显式指定时走）
        if records is None:
            if jsonl_path is None:
                from core.memory_store import MemoryStore
                jsonl_path = MemoryStore().root / ".dreams" / "verdict_review.jsonl"
            records = _load_reviews(jsonl_path)
        returns = []
        for r in records:
            if r.get("asset") != asset or r.get("regime_at_decision") != regime:
                continue
            ret = (r.get("actual_returns") or {}).get(window)
            if ret is None:
                continue
            returns.append(ret * 100)  # 转百分比

    if not returns:
        # 该 window 在历史里无样本（如 90d verdict_review 未回填）→ unavailable
        return None

    n = len(returns)
    downside_pct = round(_percentile(returns, REENTRY_DOWNSIDE_QUANTILE), 2)
    downside_price = round(current_price * (1 + downside_pct / 100), 4)
    return ReentryEstimate(
        asset=asset,
        regime=regime,
        window=window,
        n=n,
        current_price=current_price,
        threshold_pct=threshold_pct,
        p_below_current=round(sum(1 for x in returns if x < 0) / n, 4),
        p_down=round(sum(1 for x in returns if x < -threshold_pct) / n, 4),
        median_return_pct=round(statistics.median(returns), 2),
        downside_pct=downside_pct,
        downside_price=downside_price,
        has_downside=downside_price < current_price,
        low_confidence=n < MIN_CONFIDENT_N,
    )


def build_reentry_reference_text(
    asset: str,
    regime: str,
    current_price: Optional[float],
    *,
    source: str = "ohlc",
    jsonl_path: Optional[Path] = None,
    records: Optional[List[Dict[str, Any]]] = None,
    windows: Tuple[str, ...] = ("30d", "90d"),
) -> str:
    """给 CIO brief 用的卖出后路径参考文本（多 window）。无可用数据返回 ""。

    source: "ohlc"（默认，几十年 OHLC 直算）| "verdict_review"（旧源，保留）。
    """
    if current_price is None or current_price <= 0:
        return ""
    if source == "verdict_review" and records is None:
        if jsonl_path is None:
            from core.memory_store import MemoryStore
            jsonl_path = MemoryStore().root / ".dreams" / "verdict_review.jsonl"
        records = _load_reviews(jsonl_path)

    lines: List[str] = []
    any_data = False
    for w in windows:
        est = get_reentry_estimate(
            asset, regime, current_price,
            window=w, source=source, records=records,
        )
        if est is None:
            lines.append(f"- {w}: 历史样本不足 / unavailable")
        else:
            any_data = True
            lines.append(f"- {est.summary_line()}")
    if not any_data:
        return ""
    return (
        f"# 卖出后路径参考（regime={regime} 历史 forward return 分布，仅供 TRIM 决策）：\n"
        f"- 现价: ¥{current_price:,.2f}\n"
        + "\n".join(lines)
        + "\n（若要 TRIM，REENTRY_PRICE 必须低于现价；历史上跌破现价概率低 = 卖出后大概率买不回更低 = 别 TRIM）"
    )


# ============================================================================
# OHLC 直算数据源（纯算术，0 LLM token）—— 几十年历史 regime → forward return 分布
# ============================================================================
#
# 为什么换：概率表核心是 (regime → forward return)，regime 用 MA/ATR/分位算、return
# 用价格算，**全是算术**，不需要 committee 跑过。旧源 verdict_review.jsonl 只有 276 条
# （受 committee 实际跑过的次数限制），把样本量卡死在几十/几百。改用 MarketStore 里
# 几十年 OHLC 对历史每一天直算，样本量 → 数千，且复用 production 的 classify_regime
# 保证 regime 口径一致。

# price_quantile_2y 的滚动窗口（≈2 年交易日），对齐 compute_metrics 传 "2y" 数据的口径
_TRADING_DAYS_2Y = 504


def _percentile_rank(window):  # window: np.ndarray
    cur = window[-1]
    return float((window <= cur).sum() / len(window))


def _make_regime_probability(
    asset: str, regime: str, returns_pct: List[float], threshold_pct: float,
) -> RegimeProbability:
    """从一组 forward return(%) 聚合出 RegimeProbability（verdict_review / OHLC 共用）。"""
    n = len(returns_pct)
    p_up = sum(1 for r in returns_pct if r > threshold_pct) / n
    p_down = sum(1 for r in returns_pct if r < -threshold_pct) / n
    return RegimeProbability(
        asset=asset, regime=regime, n=n,
        p_up=round(p_up, 4), p_down=round(p_down, 4),
        p_flat=round(1.0 - p_up - p_down, 4),
        median_return=round(statistics.median(returns_pct), 2),
        mean_return=round(statistics.mean(returns_pct), 2),
        threshold_pct=threshold_pct,
        low_confidence=n < MIN_CONFIDENT_N,
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
        DataFrame：index=date，列 = ["regime", "fwd_30d", "fwd_90d", ...]。
        指标 warmup 不足的头部 regime=unknown；lookahead 不足的尾部 fwd_*=NaN。
        空输入返回空 DataFrame。
    """
    import numpy as np
    import pandas as pd
    from core.regime import classify_regime

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
    has_hl = (
        "High" in df.columns and "Low" in df.columns
        and not df["High"].isna().all() and not df["Low"].isna().all()
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

    def _v(x):
        return None if pd.isna(x) else float(x)

    regimes = []
    for i in range(n):
        regimes.append(classify_regime({
            "ma20": _v(ma20.iat[i]),
            "ma120": _v(ma120.iat[i]),
            "atr_pct": _v(atr_pct.iat[i]),
            "price_quantile_2y": _v(quantile.iat[i]),
            "return_30d": _v(ret30.iat[i]),
            "rebound_off_30d_low": _v(rebound.iat[i]),
        }, symbol=symbol)["regime"])

    out = pd.DataFrame({"regime": regimes}, index=df.index)

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

    return out


def _ohlc_forward_returns(
    asset: str, regime: str, window: str, *, days: int = 100000,
) -> List[float]:
    """从几十年 OHLC 取 (asset, regime) 在 window 的 forward return(%) 列表（0 token）。"""
    from db.market_store import MarketStore
    df = MarketStore().get_history_df(asset.upper(), days=days)
    frame = compute_regime_return_frame(df, asset.upper(), windows=(window,))
    col = f"fwd_{window}"
    if frame.empty or col not in frame.columns:
        return []
    sub = frame[frame["regime"] == regime].dropna(subset=[col])
    return (sub[col] * 100).tolist()


def build_probability_table_from_ohlc(
    symbols: List[str],
    *,
    window: str = "30d",
    threshold_pct: float = DEFAULT_THRESHOLD_PCT,
    days: int = 100000,
) -> Dict[Tuple[str, str], RegimeProbability]:
    """从 MarketStore 几十年 OHLC 直算 (asset, regime) → 概率分布（0 LLM token）。

    与 build_probability_table（verdict_review 源）返回同型 dict，可直接替换。
    """
    from db.market_store import MarketStore
    store = MarketStore()
    table: Dict[Tuple[str, str], RegimeProbability] = {}
    col = f"fwd_{window}"
    for sym in symbols:
        sym = sym.upper()
        df = store.get_history_df(sym, days=days)
        frame = compute_regime_return_frame(df, sym, windows=(window,))
        if frame.empty or col not in frame.columns:
            continue
        frame = frame.dropna(subset=[col])
        for regime in frame["regime"].unique():
            if regime == "unknown":
                continue
            rets = (frame.loc[frame["regime"] == regime, col] * 100).tolist()
            if not rets:
                continue
            table[(sym, regime)] = _make_regime_probability(
                sym, regime, rets, threshold_pct,
            )
    return table
