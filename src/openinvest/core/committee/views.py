"""views — 跨资产共享的 Macro 评估（从 core/committee.py 拆分）。

职责：`run_macro_view`（跑一次 Macro Strategist，跨资产共享后 CIO 各自引用）。
通过 agent_io 的 `_create_agent` + `_ask` 发起 LLM 调用。
"""
from __future__ import annotations

from openinvest.capabilities.committee.macro_strategist import build_macro_strategist_prompt
from openinvest.core.committee.agent_io import _ask, _create_agent


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------

def run_macro_view(macro_data_brief: str, *, event_brief: str = "") -> str:
    """跨资产共享的 Macro 评估，跑一次后 CIO 各自引用

    event_brief: 事件层（第一层）注入的盘中事件上下文（结构化文本，按时间排序，
                 含 supersedes 标记）。空字符串 = 不注入，行为完全等价于现状。
                 只有 Macro 看到（事件 RAG 严格隔离原则）。
    """
    agent = _create_agent(build_macro_strategist_prompt(), role="macro", round_label="macro")
    from openinvest.capabilities.committee.i18n import bilingual
    event_section = (
        bilingual(
            f"\n\n## 当前事件上下文（按时间排序，最新在前；可能含 supersedes 标记）\n{event_brief}\n",
            f"\n\n## Current event context (sorted by time, latest first; may include supersedes markers)\n{event_brief}\n",
        )
        if event_brief else ""
    )
    return _ask(agent, bilingual(
        f"# 当前宏观数据参考:\n{macro_data_brief}{event_section}\n\n请按格式输出 Macro 评估。",
        f"# Current macro data reference:\n{macro_data_brief}{event_section}\n\nPlease output the Macro assessment in the required format.",
    ))


__all__ = [
    "run_macro_view",
]
