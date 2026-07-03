"""Macro Strategist - 只看宏观 + 系统性风险

不评价单一资产的技术面，不看用户持仓。判断"投资环境是否健康"。

⚠️ Prompt 本体在 `capabilities/committee/macro_strategist/macro_strategist.md`（OpenClaw/Hermes-Agent
   一致的 SKILL.md 模式）。这里只 re-export 加载结果，保持向后兼容旧 import。
"""
from capabilities.loader import load_skill


PROMPT_MACRO_STRATEGIST = load_skill("macro_strategist")


__all__ = ["PROMPT_MACRO_STRATEGIST"]
