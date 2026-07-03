"""committee capability — 4 角色 AI 投资委员会辩论

提供五个角色的 prompt builder：
- CIO（首席投资官）— build_cio_prompt
- Macro Strategist（宏观策略师）— PROMPT_MACRO_STRATEGIST
- Quant Analyst（量化分析师）— build_quant_prompt
- Risk Officer（风控官）— build_risk_officer_prompt
- Wealth Context Officer（财富背景官）— PROMPT_WEALTH_CONTEXT_OFFICER
"""
from capabilities.committee.cio import build_cio_prompt
from capabilities.committee.macro_strategist import PROMPT_MACRO_STRATEGIST
from capabilities.committee.quant import build_quant_prompt
from capabilities.committee.risk_officer import build_risk_officer_prompt
from capabilities.committee.wealth_context_officer import PROMPT_WEALTH_CONTEXT_OFFICER

__all__ = [
    "build_cio_prompt",
    "PROMPT_MACRO_STRATEGIST",
    "build_quant_prompt",
    "build_risk_officer_prompt",
    "PROMPT_WEALTH_CONTEXT_OFFICER",
]
