"""views — 跨资产共享的 Macro / WealthContext 评估（从 core/committee.py 拆分，逻辑逐字不变）。

职责：`run_macro_view`（跑一次 Macro Strategist）+ `run_wealth_context_view`
（跑一次 WealthContextOfficer，wealth_context 为空时返回 portfolio_only stub 不调 LLM）。
两者都通过 agent_io 的 `_create_agent` + `_ask` 发起 LLM 调用；
run_wealth_context_view 保留内部 `from capabilities.committee.wealth_context_officer import ...` inner import。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from capabilities.committee.macro_strategist import PROMPT_MACRO_STRATEGIST
from core.committee.agent_io import _ask, _create_agent


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------

def run_macro_view(macro_data_brief: str, *, event_brief: str = "") -> str:
    """跨资产共享的 Macro 评估，跑一次后 CIO 各自引用

    event_brief: 事件层（第一层）注入的盘中事件上下文（结构化文本，按时间排序，
                 含 supersedes 标记）。空字符串 = 不注入，行为完全等价于现状。
                 只有 Macro 看到（事件 RAG 严格隔离原则）。
    """
    agent = _create_agent(PROMPT_MACRO_STRATEGIST, role="macro", round_label="macro")
    event_section = (
        f"\n\n## 当前事件上下文（按时间排序，最新在前；可能含 supersedes 标记）\n{event_brief}\n"
        if event_brief else ""
    )
    return _ask(agent, f"# 当前宏观数据参考:\n{macro_data_brief}{event_section}\n\n请按格式输出 Macro 评估。")


def run_wealth_context_view(wealth_context: Optional[Dict[str, Any]],
                            portfolio_cash_cny: float) -> str:
    """跨资产共享的 WealthContextOfficer 评估，跑一次后 Risk Officer + CIO 引用。

    wealth_context 为 None / 空 → 直接返回 portfolio_only stub（不调 LLM，省成本）。

    **production 调用方走 load_wealth_context_view()** —— 它会自动读 user.md
    + portfolio cash 后调本函数. 本函数留 explicit 接口给测试 / backtest 注入用.
    """
    from capabilities.committee.wealth_context_officer import PROMPT_WEALTH_CONTEXT_OFFICER

    if not wealth_context:
        return (
            f"SOLVENCY_BUFFER_LEVEL: unknown\n"
            f"ACCOUNT_PURPOSE: N/A\n"
            f"PORTFOLIO_CASH_CNY: {portfolio_cash_cny:.2f}\n"
            f"INVESTABLE_CASH_CNY: {portfolio_cash_cny:.2f}\n"
            f"BACKUP_BUFFER_CNY: 0\n"
            f"EXPLANATION_TO_RISK: user.md 没填 wealth_context，按 portfolio cash 判断流动性 + 风险。\n"
            f"EXPLANATION_TO_CIO: 加仓决策受 portfolio cash 限制。"
        )

    agent = _create_agent(PROMPT_WEALTH_CONTEXT_OFFICER,
                          role="wealth_context", round_label="wealth_context")
    import json as _json
    ctx_brief = (
        f"# 用户 wealth_context（user.md frontmatter）：\n"
        f"```json\n{_json.dumps(wealth_context, ensure_ascii=False, indent=2)}\n```\n\n"
        f"# Portfolio cash 现状：¥{portfolio_cash_cny:.2f} CNY\n\n"
        f"请按格式输出真实流动性评估。"
    )
    return _ask(agent, ctx_brief)


__all__ = [
    "run_macro_view",
    "run_wealth_context_view",
]
