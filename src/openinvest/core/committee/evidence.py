"""Render supplied facts for the final decision without inventing an as-of time."""
from __future__ import annotations

from datetime import datetime


def cio_source_context(market_data: str, regime_brief: str, analysis_as_of: str | None = None) -> str:
    from openinvest.capabilities.committee.i18n import bilingual
    try:
        as_of = datetime.fromisoformat(analysis_as_of or "")
        if as_of.tzinfo is None:
            raise ValueError("timezone missing")
        clock = as_of.isoformat()
    except (ValueError, TypeError):
        clock = "unknown; do not invent a calendar expiry or claim an input was current"
    blocks = [
        "=== SOURCE EVIDENCE FOR CIO ===",
        f"ANALYSIS_AS_OF: {clock}",
        bilingual(
            "原始输入用于核对分析员的转述，不是额外的独立投票。行情各自的观测时间仍以输入为准。"
            "复核计划的有效期必须晚于 ANALYSIS_AS_OF；时间未知时不要虚构日期。",
            "Use source inputs to check the analysts' summaries, not as extra independent votes. "
            "Each observation retains its own timestamp. A review expiry must be later than "
            "ANALYSIS_AS_OF; do not invent calendar dates when that time is unknown.",
        ),
    ]
    if market_data:
        blocks.append("=== ORIGINAL MARKET DATA ===\n" + market_data)
    if regime_brief:
        blocks.append("=== RULE-BASED REGIME AND HISTORICAL REFERENCE ===\n" + regime_brief)
    return "\n\n".join(blocks)
