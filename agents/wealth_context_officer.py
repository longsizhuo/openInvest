"""WealthContextOfficer — 解读用户 off-portfolio 真实财务背景

⚠️ Prompt 本体在 `agents/skills/wealth_context_officer/SKILL.md`（SKILL.md 模式）。
   这里只 re-export 加载结果，保持向后兼容旧 import。

为什么需要这个角色？
====================
Risk Officer 只看 portfolio.md 里的 cash 数字。如果用户的 portfolio 是
"零花钱账户"（off-portfolio 还有 emergency fund / 家族 backup / 多账户
分散），Risk 会把 "cash < ¥1000" 误判 high_risk → CIO 喊 SELL → 卖掉本来
该长期持有的仓位。

这是个**用户上下文缺失**问题——LLM 推理逻辑没错，但前提条件错。
"""
from agents.skills_loader import load_skill


PROMPT_WEALTH_CONTEXT_OFFICER = load_skill("wealth_context_officer")


__all__ = ["PROMPT_WEALTH_CONTEXT_OFFICER"]
