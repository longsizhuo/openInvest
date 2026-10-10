"""Scribe - 无裁决模式（INVEST_NO_VERDICT_MODE）下替代 CIO：只整理正反理由，不下结论

⚠️ Prompt 本体在 `capabilities/committee/scribe/scribe.md`。服务端逐字闸在
`core/committee/debate.py:find_verdict_language`，prompt 只是第一道。
"""
from typing import Any, Dict

from openinvest.capabilities.loader import load_skill


def build_scribe_prompt(asset: Dict[str, Any]) -> str:
    # ponytail: 只出中文——闸的关键词表也只覆盖中文 + 裁决枚举；英文部署要用时两边一起补
    return load_skill(
        "scribe",
        asset_name=asset.get("display_name", asset.get("symbol")),
        asset_symbol=asset["symbol"],
    )
