"""event_notifier 单测 —— stance→icon 映射（issue #210 回归：opportunity 曾用 🎯
暗示买入，经验研究显示反向指标，换成中性的 🔍）。"""
from __future__ import annotations

from openinvest.services.event_notifier import _build_subject, _STANCE_ICON


def test_stance_icon_map_has_no_bullish_opportunity_icon():
    """opportunity 不再映射到暗示"命中/买入"的图标（🎯），risk/neutral 不变。"""
    assert _STANCE_ICON["opportunity"] != "🎯"
    assert _STANCE_ICON["risk"] == "🚨"
    assert _STANCE_ICON["neutral"] == "📰"


def test_build_subject_all_opportunity_uses_neutral_icon():
    events = [{"stance": "opportunity", "affected_symbols": ["GC=F"]}]
    subject = _build_subject(events)
    assert "🎯" not in subject
    assert "[Opportunity]" in subject


def test_build_subject_all_risk_keeps_alarm_icon():
    events = [{"stance": "risk", "affected_symbols": ["NDQ.AX"]}]
    subject = _build_subject(events)
    assert subject.startswith("🚨")
    assert "[Risk]" in subject


def test_build_subject_mixed_stances_uses_neutral_label():
    events = [{"stance": "risk"}, {"stance": "opportunity"}]
    subject = _build_subject(events)
    assert "[Mixed]" in subject
