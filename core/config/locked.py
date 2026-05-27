"""不可调参数 — 真实参数源，绕过注入链

代码从这里读值。_loader.py 的 YAML/CLI/env 注入链完全不碰这个模块。
改这里的值 = 改源码 = git diff 显眼 = 需要 ADR。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LockedVerdictScoring:
    """HOLD 判定参数 — anti-reward-hacking 设计承诺

    来源: jobs/verdict_review.py:244-246
          "绝不让系统自学习这把给自己打分的尺子"
    ADR: 改动需要新 ADR 推翻此承诺
    """
    k_flat: float = 1.0
    flat_ceiling_pct: float = 8.0
    default_daily_vol_pct: float = 2.0


@dataclass(frozen=True)
class LockedDreamingScoring:
    """Dreaming 评分权重 + caution 门槛 — ADR 008 试金石验证

    来源: jobs/dreaming.py:67,72-73,497-499
          ADR 008 用 Phase1.5 + post-cutoff 试金石验证
    ADR: 改动需要新 ADR 推翻 ADR 008
    """
    min_score: float = 0.8
    score_hit_rate_weight: float = 0.7
    score_sample_weight: float = 0.3
    caution_min_base_down: float = 0.15
    caution_lift_full: float = 0.20


@dataclass(frozen=True)
class LockedCommitteeDefaults:
    """Committee 工程参数 — 非决策参数

    来源: core/committee.py:38-40,89,129,531
          工程经验值 + Optuna 30 trial 确认 max_rounds 是 placebo
    ADR: 无（但改动无收益）
    """
    llm_max_attempts: int = 3
    llm_base_delay: float = 2.0
    llm_max_delay: float = 20.0
    temperature: float = 0.2
    max_tool_iterations: int = 4
    max_debate_rounds_default: int = 1
    max_debate_rounds_live: int = 4


@dataclass(frozen=True)
class LockedPromptIdentity:
    """Prompt 内容标识 — ADR 007 钉死 CIO zero-shot

    来源: ADR 007 "CIO 保持 zero-shot...few-shot 验证不通"
    ADR: 改动需要新 ADR 推翻 ADR 007
    """
    cio_zero_shot: bool = True
    cash_opportunity_cost_rule: bool = True


# === 模块级单例（代码直接读这个） ===
_LOCKED: tuple[
    LockedVerdictScoring,
    LockedDreamingScoring,
    LockedCommitteeDefaults,
    LockedPromptIdentity,
] | None = None


def get_locked() -> tuple[
    LockedVerdictScoring,
    LockedDreamingScoring,
    LockedCommitteeDefaults,
    LockedPromptIdentity,
]:
    """返回所有 locked 参数组。始终返回硬编码默认值，不读 YAML/CLI/env。"""
    global _LOCKED
    if _LOCKED is None:
        _LOCKED = (
            LockedVerdictScoring(),
            LockedDreamingScoring(),
            LockedCommitteeDefaults(),
            LockedPromptIdentity(),
        )
    return _LOCKED
