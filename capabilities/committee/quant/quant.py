"""Quant Analyst - 只看技术面 + 价量 + 模式识别 + 市场 Regime

不看宏观，不看用户持仓。专注"市场本身在告诉我什么"。

⚠️ Prompt 本体在 `capabilities/committee/quant/quant.md`（opening）+ `SKILL_rebuttal.md`
   （round 2 cross-challenge）。SKILL.md 模式跟 OpenClaw/Hermes 一致。

REGIME 上下文由 core.regime.format_regime_brief 在外层注入到 user message，
本 prompt 模块只声明"会收到 REGIME 字段"以及"如何遵循 REGIME"。
任何 regime 阈值或分类逻辑都在 core/regime.py，**禁止在本文件硬编码 regime**。
"""
from typing import Any, Dict

from capabilities.loader import load_skill


def build_quant_prompt(asset: Dict[str, Any], round_label: str = "opening") -> str:
    """渲染 Quant prompt（含 asset 占位符替换）

    round_label="opening" → SKILL.md (Round 1 独立陈述)
    round_label="rebuttal" → SKILL_rebuttal.md (Round 2 cross-challenge)
    """
    asset_name = asset.get("display_name", asset.get("symbol"))
    return load_skill(
        "quant",
        round_label=round_label,
        asset_name=asset_name,
        asset_symbol=asset["symbol"],
    )


__all__ = ["build_quant_prompt"]
