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
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# 默认阈值：30d return ±5% 算"大涨/大跌"
DEFAULT_THRESHOLD_PCT = 5.0
# 低于此样本量标记 low_confidence
MIN_CONFIDENT_N = 10


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
