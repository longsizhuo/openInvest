"""核心 RAG 路径单测 — _resolve_event_brief + format_event_brief

PR #5 Copilot CR 提到这俩是事件 RAG 核心但没单测。补全：
- feature flag gating (env INVEST_EVENT_RAG_ENABLED)
- caller override 短路
- recall 失败 graceful 退化
- format 输出含 supersedes 标记
- format 输出 sources 顺序确定（PR #5 P1 CR: set comprehension 不确定性已修）
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.committee_runner import _resolve_event_brief, format_event_brief


# ============================================================================
# _resolve_event_brief
# ============================================================================

def test_resolve_returns_override_when_caller_passes_string(monkeypatch):
    """caller 显式传 override（含空串）→ 直接 return，不查 DB"""
    monkeypatch.setenv("INVEST_EVENT_RAG_ENABLED", "true")
    # 即使 DB 有数据，override 优先
    result = _resolve_event_brief("AAPL", override="EXPLICIT_OVERRIDE")
    assert result == "EXPLICIT_OVERRIDE"


def test_resolve_returns_empty_when_caller_overrides_empty(monkeypatch):
    """override="" 是合法的"我不要事件"信号 → 返空串，不 fallback 到 recall"""
    monkeypatch.setenv("INVEST_EVENT_RAG_ENABLED", "true")
    result = _resolve_event_brief("AAPL", override="")
    assert result == ""


def test_resolve_disabled_by_env_flag(monkeypatch):
    """INVEST_EVENT_RAG_ENABLED=false → 返 ""，不查 DB"""
    monkeypatch.setenv("INVEST_EVENT_RAG_ENABLED", "false")
    result = _resolve_event_brief("AAPL", override=None)
    assert result == ""


def test_resolve_disabled_by_env_flag_variants(monkeypatch):
    """0 / no / off 都视为 disabled"""
    for val in ["0", "no", "off", "False", "OFF"]:
        monkeypatch.setenv("INVEST_EVENT_RAG_ENABLED", val)
        result = _resolve_event_brief("AAPL", override=None)
        assert result == "", f"INVEST_EVENT_RAG_ENABLED={val!r} 应该 disable"


def test_resolve_default_on_when_env_unset(monkeypatch):
    """env 未设 → 当 true（PR #6 default-on 决策）。recall 失败 graceful 返空"""
    monkeypatch.delenv("INVEST_EVENT_RAG_ENABLED", raising=False)
    # 让 _get_event_store 返 None 模拟 init 失败 → 应该 graceful 返 ""
    with patch("core.runner.event_brief._get_event_store", return_value=None):
        result = _resolve_event_brief("AAPL", override=None)
        assert result == ""


def test_resolve_graceful_on_recall_exception(monkeypatch):
    """store.recall 抛异常 → graceful 退化空字符串，不阻断委员会"""
    monkeypatch.setenv("INVEST_EVENT_RAG_ENABLED", "true")

    class FakeBrokenStore:
        vec_loaded = False

        def recall(self, *a, **kw):
            raise RuntimeError("DB locked or whatever")

    with patch("core.runner.event_brief._get_event_store",
               return_value=FakeBrokenStore()):
        result = _resolve_event_brief("AAPL", override=None)
        assert result == ""


def test_resolve_calls_recall_with_env_params(monkeypatch):
    """env var 调参（window_days / min_severity / top_k）真的传给 recall"""
    monkeypatch.setenv("INVEST_EVENT_RAG_ENABLED", "true")
    monkeypatch.setenv("INVEST_EVENT_RAG_WINDOW_DAYS", "14")
    monkeypatch.setenv("INVEST_EVENT_RAG_MIN_SEVERITY", "high")
    monkeypatch.setenv("INVEST_EVENT_RAG_TOP_K", "3")

    captured = {}

    class FakeStore:
        vec_loaded = False

        def recall(self, symbol, **kwargs):
            captured["symbol"] = symbol
            captured.update(kwargs)
            return []

    with patch("core.runner.event_brief._get_event_store", return_value=FakeStore()):
        _resolve_event_brief("NVDA", override=None)

    assert captured["symbol"] == "NVDA"
    assert captured["time_window_days"] == 14
    assert captured["min_severity"] == "high"
    assert captured["top_k"] == 3


# ============================================================================
# format_event_brief
# ============================================================================

def test_format_empty_list_returns_empty_string():
    assert format_event_brief([]) == ""


def test_format_single_event_basic_structure():
    events = [{
        "ts": "2026-05-15 14:32",
        "stance": "risk",
        "severity": "high",
        "affected_symbols": ["NVDA"],
        "one_line_claim": "Nvidia Q1 guidance miss",
        "sources": [{"src_name": "reuters"}, {"src_name": "bloomberg"}],
    }]
    out = format_event_brief(events)
    assert "[2026-05-15 14:32]" in out
    assert "[risk/high]" in out
    assert "[NVDA]" in out
    assert "Nvidia Q1 guidance miss" in out
    assert "reuters" in out
    assert "bloomberg" in out


def test_format_sources_deterministic_order():
    """PR #5 P1 CR 修复验证: sources 顺序必须确定（同输入 → 同输出 → token-stable）"""
    events = [{
        "ts": "2026-05-15",
        "stance": "neutral",
        "severity": "mid",
        "affected_symbols": ["TEST"],
        "one_line_claim": "x",
        "sources": [
            {"src_name": "ft"},
            {"src_name": "bloomberg"},
            {"src_name": "reuters"},
        ],
    }]
    out1 = format_event_brief(events)
    out2 = format_event_brief(events)
    out3 = format_event_brief(events)
    assert out1 == out2 == out3, "format_event_brief 必须 deterministic"
    # 实际 sorted: bloomberg / ft / reuters
    sources_part = out1.split("(sources: ")[1].split(")")[0]
    assert sources_part == "bloomberg, ft, reuters", (
        f"sources 应 sorted，实际: {sources_part!r}"
    )


def test_format_supersedes_annotation():
    """有 supersedes 字段 → 输出含 ↳ 标记"""
    events = [{
        "ts": "2026-05-15",
        "stance": "opportunity",
        "severity": "mid",
        "affected_symbols": ["GC=F"],
        "one_line_claim": "Powell dovish",
        "sources": [{"src_name": "ft"}],
        "supersedes": "evt_old_hawkish_id",
    }]
    out = format_event_brief(events)
    assert "↳" in out and "supersedes" in out
    assert "evt_old_hawkish_id" in out


def test_format_handles_missing_optional_fields():
    """部分事件 fields 缺失（如 sources / supersedes）不应 crash"""
    events = [{
        "ts": "2026-05-15",
        "stance": "neutral",
        "severity": "low",
        # 没有 affected_symbols / sources / supersedes
        "one_line_claim": "minimal event",
    }]
    out = format_event_brief(events)
    assert "minimal event" in out  # 至少 claim 渲染了


def test_format_output_parseable_by_sentiment_parser():
    """互解析契约回归：format_event_brief 的头行必须能被
    utils.sentiment._parse_event_brief_entries 解析（EVENT_STANCE 聚合行依赖）。
    改任何一边的格式必须同步另一边——本测试防单边漂移。
    """
    from utils.sentiment import _parse_event_brief_entries

    events = [
        {
            "ts": "2026-05-15T14:32:00+00:00",
            "stance": "risk",
            "severity": "high",
            "affected_symbols": ["FAKE.AX", "NVDA"],
            "one_line_claim": "bad",
            "sources": [{"src_name": "reuters"}],
        },
        {
            "ts": "2026-05-14T08:00:00Z",
            "stance": "opportunity",
            "severity": "mid",
            "affected_symbols": ["OTHER=F"],
            "one_line_claim": "good",
            "sources": [],
        },
        {
            # ts 空 / 无 symbols 的边角也要能解析（不抛、字段容错）
            "ts": "",
            "stance": "neutral",
            "severity": "low",
            "affected_symbols": [],
            "one_line_claim": "meh",
            "sources": [],
        },
    ]
    out = format_event_brief(events)
    entries = _parse_event_brief_entries(out)
    assert len(entries) == 3, f"3 条事件应解析出 3 个头行，实际 {len(entries)}:\n{out}"
    assert entries[0]["stance"] == "risk" and entries[0]["sev"] == "high"
    assert entries[0]["syms"] == {"FAKE.AX", "NVDA"}
    assert entries[0]["ts"] is not None
    assert entries[1]["syms"] == {"OTHER=F"}
    assert entries[1]["ts"] is not None  # Z 后缀可解析
    assert entries[2]["ts"] is None and entries[2]["syms"] == set()
