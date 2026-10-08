"""event_notifier 单测 —— stance→icon 映射（issue #210 回归：opportunity 曾用 🎯
暗示买入，事件研究显示其后 5 日上涨的不到一半，换成中性的 🔍）。"""
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


def test_untrusted_event_text_cannot_inject_html_or_links_into_email():
    """新闻标题/claim/来源都来自外部源：渲染进邮件 HTML 后不能出现注入的标签、链接或 javascript: 链接"""
    from openinvest.services.event_notifier import _build_markdown
    from openinvest.services.notifier import render_markdown_email

    evil = ('<img src=x onerror=alert(1)><a href="http://evil.example">BUY NOW</a> [click](http://evil.example)'
            r' \[BUY\](http://evil.example) `<b>`')
    md = _build_markdown(
        [{
            "one_line_claim": evil,
            "stance": "risk", "severity": "high", "ts": "2026-10-08T00:00:00Z",
            "affected_symbols": ["<b>X</b>"],
            "sources": [
                {"title": evil, "url": "javascript:alert(1)", "src_name": "<i>feed</i>"},
                {"title": "ok", "url": 'http://news.example/a) <img src=y> "t"', "src_name": "feed"},
            ],
        }],
        committee_task_id=None, api_base_url="http://127.0.0.1:8765", holdings_snapshot={},
    )
    out = render_markdown_email(md)
    # 注入内容只能以转义后的文字出现（&lt;img …），不能成为真标签 / 真链接
    for bad in ("<img", '<a href="http://evil', "javascript:", "<b>X", "<i>feed", "&amp;lt;"):
        assert bad not in out, bad
    assert "&lt;img src=x onerror=alert(1)&gt;" in out
    assert 'href="http://news.example/a%29%20%3Cimg%20src=y%3E%20%22t%22"' in out  # 正常 http 链接保留、特殊字符被编码
