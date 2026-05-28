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
    jsonl_path: Optional[Path] = None,
    records: Optional[List[Dict[str, Any]]] = None,
    threshold_pct: float = DEFAULT_THRESHOLD_PCT,
) -> Optional[ReentryEstimate]:
    """基于历史 forward return 分布给出卖出后买回点参考。

    Args:
        asset / regime: 分组键
        current_price: 现价（cost currency）。None / <=0 → 返回 None（无法折算价格）
        window: forward return 窗口，"30d" / "90d"。该窗口在历史数据里无样本
            （如 90d 尚未回填）→ 返回 None（标 unavailable，由调用方处理）
        records: 预读的 verdict_review 记录；None 则读 jsonl_path

    Returns:
        ReentryEstimate 或 None（无现价 / 该 window 无历史样本）
    """
    if current_price is None or current_price <= 0:
        return None
    if records is None:
        if jsonl_path is None:
            from core.memory_store import MemoryStore
            jsonl_path = MemoryStore().root / ".dreams" / "verdict_review.jsonl"
        records = _load_reviews(jsonl_path)

    returns: List[float] = []
    for r in records:
        if r.get("asset") != asset or r.get("regime_at_decision") != regime:
            continue
        ret = (r.get("actual_returns") or {}).get(window)
        if ret is None:
            continue
        returns.append(ret * 100)  # 转百分比

    if not returns:
        # 该 window 在历史里无样本（如 90d 未回填）→ unavailable
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
    jsonl_path: Optional[Path] = None,
    records: Optional[List[Dict[str, Any]]] = None,
    windows: Tuple[str, ...] = ("30d", "90d"),
) -> str:
    """给 CIO brief 用的卖出后路径参考文本（多 window）。无可用数据返回 ""。"""
    if current_price is None or current_price <= 0:
        return ""
    if records is None:
        if jsonl_path is None:
            from core.memory_store import MemoryStore
            jsonl_path = MemoryStore().root / ".dreams" / "verdict_review.jsonl"
        records = _load_reviews(jsonl_path)

    lines: List[str] = []
    any_data = False
    for w in windows:
        est = get_reentry_estimate(
            asset, regime, current_price, window=w, records=records,
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
