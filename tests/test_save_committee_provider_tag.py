"""save_committee_transcript 的 Provider 标注契约

2026-07-14：此前硬编码 "Provider: claude (skill mode)"，不管实际是谁跑的
Coordinator 协议——Hermes 接入后会把 Hermes 产出的 transcript 误标成 Claude，
污染 Dreaming 按 provider 分桶的模式挖掘。provider 参数可选、默认 "claude"，
保证历史唯一调用方（Claude Code）零行为变化。
跑：uv run pytest tests/test_save_committee_provider_tag.py -q
"""
from __future__ import annotations

from openinvest.core import memory_store as ms
from openinvest.core.runner.coordinator import save_committee_transcript

_RAW = """
=== MACRO ===
宏观中性

=== QUANT_R1 ===
SIGNAL: bullish
STRENGTH: 6

=== RISK_R1 ===
SIGNAL: concerned
STRENGTH: 5

=== QUANT_R2 ===
SIGNAL: bullish
STRENGTH: 6

=== RISK_R2 ===
SIGNAL: concerned
STRENGTH: 5

=== CIO ===
VERDICT: HOLD
CONFIDENCE: 0.6
DOMINANT_VIEW: risk
SUGGESTED_ALLOC_CNY: 0
"""


def test_default_provider_is_claude_unchanged(tmp_path, monkeypatch):
    """不传 provider → 与历史硬编码字符串逐字节一致（向后兼容）。"""
    monkeypatch.setattr(ms, "MEMORY_ROOT", tmp_path)
    result = save_committee_transcript("TESTSYM", _RAW)
    from pathlib import Path
    saved_text = Path(result["saved"]).read_text(encoding="utf-8")
    assert "**Provider**: claude (skill mode)" in saved_text


def test_explicit_provider_is_recorded(tmp_path, monkeypatch):
    """传 provider="hermes" → transcript 准确标注实际调用方，不再一律 claude。"""
    monkeypatch.setattr(ms, "MEMORY_ROOT", tmp_path)
    result = save_committee_transcript("TESTSYM2", _RAW, provider="hermes")
    from pathlib import Path
    saved_text = Path(result["saved"]).read_text(encoding="utf-8")
    assert "**Provider**: hermes (skill mode)" in saved_text
    assert "claude" not in saved_text
