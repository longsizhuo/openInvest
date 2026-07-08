"""Macro Strategist - 只看宏观 + 系统性风险

不评价单一资产的技术面，不看用户持仓。判断"投资环境是否健康"。

⚠️ Prompt 本体在 `capabilities/committee/macro_strategist/macro_strategist.md`（OpenClaw/Hermes-Agent
   一致的 SKILL.md 模式）。
"""
from openinvest.capabilities.committee.i18n import (
    build_field_value_language_directive,
    build_output_language_directive,
    localize_prompt_output_requirements,
)
from openinvest.capabilities.loader import load_skill


def build_macro_strategist_prompt() -> str:
    prompt = localize_prompt_output_requirements(load_skill("macro_strategist"))
    return (
        f"{build_output_language_directive(artifact='analysis')}\n"
        f"{build_field_value_language_directive()}\n\n{prompt}"
    )

__all__ = ["build_macro_strategist_prompt"]
