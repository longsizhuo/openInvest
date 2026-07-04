"""issue #26 验收：确定性 entity→symbol 两层映射。

验收标准（issue 原文）：
- 含 "nasdaq" 实体的事件 → 持 NDQ.AX 的用户召回命中 + EVENT_STANCE(NDQ.AX) 有值
- 持 QQQ 的用户同样自动工作（通用，无用户特定 ticker 驱动）
- goldman 类词边界误伤防护沿用
- 虚构标的 tracks 声明可用（驱动链无硬编码）
"""
from __future__ import annotations

import pytest

from openinvest.services.symbol_map import canonical_symbols_for_entities, proxy_symbols_for


# ---------- 第一层：实体 → canonical ----------

def test_entities_nasdaq_maps_to_ndx():
    assert "^NDX" in canonical_symbols_for_entities(["nasdaq", "tech"])
    assert "^NDX" in canonical_symbols_for_entities(["nasdaq-100"])
    assert "^NDX" in canonical_symbols_for_entities(["ndx"])


def test_entities_gold_migrated():
    """黄金映射从 event_normalizer 迁入后行为不变"""
    assert "GC=F" in canonical_symbols_for_entities(["central_bank_gold", "gold"])
    assert "GC=F" in canonical_symbols_for_entities(["bullion"])


def test_entities_word_boundary_no_false_positive():
    """goldman ≠ gold；nasdaqish 这类不存在的词也不该命中"""
    assert canonical_symbols_for_entities(["goldman sachs", "banks"]) == []


def test_entities_sp500_and_dow():
    assert "^GSPC" in canonical_symbols_for_entities(["s&p 500"])
    assert "^GSPC" in canonical_symbols_for_entities(["spx"])
    assert "^DJI" in canonical_symbols_for_entities(["dow jones"])


# ---------- 第二层：用户标的 → 代理集合 ----------

def test_proxy_ndq_includes_ndx():
    assert proxy_symbols_for("NDQ.AX") == frozenset({"NDQ.AX", "^NDX"})


def test_proxy_qqq_works_same():
    """通用性：换一个用户持 QQQ，同样自动工作"""
    assert "^NDX" in proxy_symbols_for("QQQ")


def test_proxy_unknown_symbol_is_self_only():
    assert proxy_symbols_for("AAPL") == frozenset({"AAPL"})


def test_proxy_tracks_field_extends():
    """虚构标的 + strategy tracks 字段 → 驱动链无任何硬编码"""
    assert proxy_symbols_for("TRACK.AX", tracks="^TEST") == frozenset({"TRACK.AX", "^TEST"})
    assert proxy_symbols_for("TRACK.AX", tracks=["^T1", "^T2"]) == frozenset(
        {"TRACK.AX", "^T1", "^T2"})


def test_proxy_empty_graceful():
    assert proxy_symbols_for("") == frozenset()
    assert proxy_symbols_for(None) == frozenset()  # type: ignore[arg-type]


# ---------- 接线验收：normalizer 兜底 ----------

def test_normalizer_fallback_maps_nasdaq_event():
    from openinvest.services.event_normalizer import _sanitize_event

    ne = _sanitize_event({
        "idx": 0, "one_line_claim": "Nasdaq 100 rallies on chip earnings",
        "stance": "opportunity", "severity": "mid",
        "entities": ["nasdaq", "semis"],
        "affected_symbols": ["NVDA"],
    }, offset=0)
    assert "^NDX" in ne.event["affected_symbols"]
    assert "NVDA" in ne.event["affected_symbols"]  # 原有不丢


# ---------- 接线验收：recall aliases ----------

def test_recall_alias_matches_index_event(tmp_path):
    """标 ^NDX 的事件，持 NDQ.AX 的用户经 aliases 召回命中"""
    from datetime import datetime, timezone
    from openinvest.db.event_store import EventStore

    store = EventStore(db_path=tmp_path / "ev.db", embedding_dim=4)
    store.upsert_event({
        "one_line_claim": "Nasdaq 100 futures slide on rate fears",
        "event_type": "macro", "stance": "risk", "severity": "high",
        "source_reliability": "high",
        "ts": datetime.now(timezone.utc).isoformat(),
        "entities": ["nasdaq", "us_rates"],
        "affected_symbols": ["^NDX"],
    }, embedding=None)

    from openinvest.services.symbol_map import proxy_symbols_for
    hits = store.recall("NDQ.AX", aliases=sorted(proxy_symbols_for("NDQ.AX")))
    assert len(hits) == 1, "aliases 没让 NDQ.AX 命中 ^NDX 事件"
    # 不带 aliases 的旧行为：不命中（守对照）
    assert store.recall("NDQ.AX") == []


# ---------- 接线验收：per-asset EVENT_STANCE ----------

def test_event_stance_line_matches_proxied_event():
    from openinvest.utils.sentiment import event_stance_line_for_symbol

    brief = (
        "[2026-06-10T08:00:00+00:00] [risk/high] [^NDX] (sources: reuters)\n"
        "index event\n\n"
        "[2026-06-09T08:00:00+00:00] [opportunity/mid] [UNRELATED=F]\nother\n"
    )
    line = event_stance_line_for_symbol(brief, "NDQ.AX")
    assert line is not None and line.startswith("EVENT_STANCE(NDQ.AX): net risk")
    # QQQ 用户同样工作
    assert event_stance_line_for_symbol(brief, "QQQ") is not None
    # 无关标的不命中
    assert event_stance_line_for_symbol(brief, "AAPL") is None
    # 虚构标的 + tracks 声明
    line2 = event_stance_line_for_symbol(brief, "TRACK.AX", tracks="^NDX")
    assert line2 is not None and "EVENT_STANCE(TRACK.AX)" in line2
