"""services/event_normalizer 单测 —— mock OpenAI client，覆盖 sanitize / 批量 / 失败降级"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from services.event_normalizer import (
    _parse_events_json,
    _sanitize_event,
    normalize,
)
from services.news_sources import RawNewsItem


def _make_resp(json_obj):
    """伪造 openai chat.completions.create 返回值"""
    msg = MagicMock()
    msg.content = json.dumps(json_obj)
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    resp.usage = MagicMock(prompt_tokens=100, completion_tokens=50)
    return resp


def test_sanitize_event_drops_missing_claim():
    assert _sanitize_event({"idx": 0, "one_line_claim": ""}, offset=0) is None
    assert _sanitize_event({"idx": "bad"}, offset=0) is None


def test_sanitize_event_normalizes_invalid_enum_values():
    ne = _sanitize_event({
        "idx": 1, "one_line_claim": "x", "stance": "BOGUS", "severity": "extreme",
        "entities": ["NVidia", "  semis  ", ""], "affected_symbols": ["NVDA"],
    }, offset=0)
    assert ne.event["stance"] == "neutral"  # invalid → neutral
    assert ne.event["severity"] == "low"
    assert ne.event["entities"] == ["nvidia", "semis"]


def test_parse_events_json_handles_markdown_fence():
    text = "```json\n{\"events\":[{\"idx\":0,\"one_line_claim\":\"x\",\"stance\":\"risk\",\"severity\":\"high\"}]}\n```"
    out = _parse_events_json(text, expected_size=1, offset=0)
    assert len(out) == 1
    assert out[0].event["stance"] == "risk"


def test_parse_events_json_invalid_returns_empty():
    assert _parse_events_json("not json", expected_size=1, offset=0) == []
    assert _parse_events_json('{"events": "not a list"}', expected_size=1, offset=0) == []


def test_normalize_calls_llm_and_attaches_raw_items(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")

    items = [
        RawNewsItem(src_name="rss:r", title="A", url="https://r.com/a", snippet="snip a"),
        RawNewsItem(src_name="rss:r", title="B", url="https://r.com/b", snippet="snip b"),
    ]
    fake_resp = _make_resp({
        "events": [
            {"idx": 0, "one_line_claim": "Apple beats", "stance": "opportunity",
             "severity": "mid", "entities": ["apple"], "affected_symbols": ["AAPL"],
             "ts": "2026-05-13T10:00:00Z"},
            {"idx": 1, "one_line_claim": "Macron resigns", "stance": "risk",
             "severity": "low", "entities": ["france"], "affected_symbols": [],
             "ts": "2026-05-13T09:00:00Z"},
        ]
    })

    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = fake_resp
    with patch("openai.OpenAI", return_value=fake_client):
        out = normalize(items, skip_embedding=True)

    assert len(out) == 2
    assert out[0].event["one_line_claim"] == "Apple beats"
    assert out[0].raw_item is items[0]
    assert out[1].raw_item is items[1]
    # 调用了一次（≤ MAX_BATCH）
    assert fake_client.chat.completions.create.call_count == 1


def test_normalize_skips_when_no_api_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    items = [RawNewsItem(src_name="x", title="t", url="u", snippet="s")]
    assert normalize(items) == []


def test_normalize_embedding_attached_by_default(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    items = [RawNewsItem(src_name="x", title="t", url="u", snippet="s")]
    fake_resp = _make_resp({
        "events": [{"idx": 0, "one_line_claim": "claim", "stance": "neutral", "severity": "low"}]
    })
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = fake_resp
    with patch("openai.OpenAI", return_value=fake_client):
        out = normalize(items)
    assert len(out) == 1
    assert out[0].embedding is not None
    assert len(out[0].embedding) == 1024


def test_normalize_batches_above_max(monkeypatch):
    """26 条 → 2 batch（25 + 1）"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    items = [RawNewsItem(src_name="x", title=f"t{i}", url=f"u{i}", snippet="s") for i in range(26)]
    # 第一个 batch 返回 25 条，第二个返回 1 条
    resp1 = _make_resp({"events": [
        {"idx": i, "one_line_claim": f"claim {i}", "stance": "neutral", "severity": "low"}
        for i in range(25)
    ]})
    resp2 = _make_resp({"events": [
        {"idx": 25, "one_line_claim": "claim 25", "stance": "neutral", "severity": "low"}
    ]})
    fake_client = MagicMock()
    fake_client.chat.completions.create.side_effect = [resp1, resp2]
    with patch("openai.OpenAI", return_value=fake_client):
        out = normalize(items, skip_embedding=True)
    assert len(out) == 26
    assert fake_client.chat.completions.create.call_count == 2


def test_normalize_continues_on_batch_failure(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    items = [RawNewsItem(src_name="x", title=f"t{i}", url=f"u{i}", snippet="s") for i in range(26)]
    resp_ok = _make_resp({"events": [
        {"idx": 25, "one_line_claim": "claim 25", "stance": "neutral", "severity": "low"}
    ]})
    fake_client = MagicMock()
    # 第一批挂，第二批成功
    fake_client.chat.completions.create.side_effect = [RuntimeError("boom"), resp_ok]
    with patch("openai.OpenAI", return_value=fake_client):
        out = normalize(items, skip_embedding=True)
    assert len(out) == 1
    assert out[0].raw_idx == 25
