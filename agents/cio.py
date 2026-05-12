"""CIO - 综合 Quant / Macro / Risk Officer 三人输出，给最终客户备忘

CIO 不重做分析，只综合 + 决策 + 输出执行方案。
强制读三方的 SIGNAL/ONE_LINER + 用户上下文，给完整的投行级 memo。

⚠️ Prompt 本体在 `agents/skills/cio/SKILL.md`。
"""
from typing import Any, Dict

from agents.skills_loader import load_skill


def build_cio_prompt(asset: Dict[str, Any]) -> str:
    """渲染 CIO prompt（含 asset 占位符替换）"""
    asset_name = asset.get("display_name", asset.get("symbol"))
    return load_skill(
        "cio",
        asset_name=asset_name,
        asset_symbol=asset["symbol"],
    )


__all__ = ["build_cio_prompt"]
