"""干预规则词汇纯核（域绑定纯模块，ADR-026）

从 core/runner/intervention.py 拆出：regime 标签抽取 + rule→家族映射
（聚合口径 single source，decision_ledger / discipline / web_api /
intervention_review 同用）。干预日志/快照落盘留在 intervention.py。
"""
from __future__ import annotations

def _extract_regime_label(regime_brief: str) -> str:
    """从 format_regime_brief 输出提取 regime label（第一行 'REGIME: xxx'）"""
    for line in regime_brief.splitlines():
        if line.startswith("REGIME:"):
            return line.split(":", 1)[1].strip()
    return ""


def rule_family(rule: str) -> str:
    """干预 rule → 粗粒度家族（聚合钱口径用，single source）。

    纯字符串映射，同时吃 live（defense_*/sanity4_*/sanity5_*）和 reconstruct
    （reconstructed_trim_blocked/_defense_downgrade）两套命名，让"同一底层规则
    拦截"在 live 行与历史重建行之间并桶——否则按细粒度 rule 聚合会拆成两组。
    - trim_blocked：sanity4(集中度)/sanity5(买回点)/config 关 lens/重建的 TRIM 被拦，都是"拦减仓"
    - buy_defense：快崩防御对买侧降级，"拦加仓"
    """
    r = rule or ""
    if "trim" in r or "sanity4" in r or "sanity5" in r or "concentration_lens" in r:
        return "trim_blocked"
    if "defense" in r or "downgrade" in r:
        return "buy_defense"
    return "other"



__all__ = [
    "_extract_regime_label",
    "rule_family",
]
