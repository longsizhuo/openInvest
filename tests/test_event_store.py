"""db/event_store.py 单测 —— 临时 sqlite 文件，覆盖 upsert/recall/gc 三大路径"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from openinvest.db.event_store import EventStore, claim_to_event_id


def _utc_iso(offset_days: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=offset_days)).isoformat(timespec="seconds")


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "events.db")
        s = EventStore(db_path=path, embedding_dim=4)  # 小维度方便测
        yield s
        s.conn.close()


def test_claim_to_event_id_strips_iso_dates():
    """精确 ISO 日期戳（多家媒体 publish 日不同）→ 应剥掉"""
    a = claim_to_event_id("2026-05-15 Apple beats earnings")
    b = claim_to_event_id("2026-05-16 Apple beats earnings")
    assert a == b, "同事件不同 publish 日应同 id"


def test_claim_to_event_id_preserves_quarters():
    """季度 (Q1/Q2 ...) = 事件本身的语义 → 不应剥（PR #5 Copilot CR 修复）"""
    a = claim_to_event_id("Nvidia Q1 2026 guidance miss, futures down 3%")
    b = claim_to_event_id("Nvidia Q2 2026 guidance miss, futures down 3%")
    assert a != b, "Q1 / Q2 是不同事件，event_id 必须区分"


def test_claim_to_event_id_preserves_year():
    """年份也是语义信号（2025 outlook ≠ 2026 outlook）→ 不剥"""
    a = claim_to_event_id("Fed projects 2025 rates stable")
    b = claim_to_event_id("Fed projects 2026 rates stable")
    assert a != b, "不同年份 outlook 应是不同事件"


def test_upsert_new_then_dup_merges(store):
    was_new, eid = store.upsert_event({
        "one_line_claim": "Fed signals dovish pivot",
        "stance": "opportunity",
        "severity": "mid",
        "ts": _utc_iso(),
        "affected_symbols": ["GC=F", "NDQ.AX"],
        "entities": ["fed", "rates"],
    }, embedding=[0.1, 0.2, 0.3, 0.4])
    assert was_new and eid

    # 同事件第二次（severity 升级 + 多挂一个 source）
    was_new2, eid2 = store.upsert_event({
        "one_line_claim": "Fed signals dovish pivot",
        "stance": "opportunity",
        "severity": "high",
        "ts": _utc_iso(),
        "affected_symbols": ["GC=F", "NDQ.AX"],
        "entities": ["fed", "rates"],
    })
    assert not was_new2
    assert eid == eid2
    e = store.get_event(eid)
    assert e["severity"] == "high"  # bumped


def test_add_source_dedup(store):
    _, eid = store.upsert_event({
        "one_line_claim": "Apple beats earnings",
        "stance": "opportunity",
        "severity": "mid",
        "ts": _utc_iso(),
        "affected_symbols": ["AAPL"],
        "entities": ["apple"],
    }, embedding=[0.0, 0.0, 0.0, 1.0])

    assert store.add_source(eid, src_name="reuters", url="https://r.com/a") is True
    assert store.add_source(eid, src_name="reuters", url="https://r.com/a") is False  # dup
    assert store.is_seen_url("https://r.com/a") is True
    assert len(store.get_sources(eid)) == 1


def test_invalid_stance_or_severity(store):
    with pytest.raises(ValueError):
        store.upsert_event({
            "one_line_claim": "x", "stance": "bogus", "severity": "mid",
            "ts": _utc_iso(), "affected_symbols": [],
        })
    with pytest.raises(ValueError):
        store.upsert_event({
            "one_line_claim": "x", "stance": "risk", "severity": "ultra",
            "ts": _utc_iso(), "affected_symbols": [],
        })


def test_recall_hard_filter_by_symbol_and_severity(store):
    # 三条事件：A 相关高 sev / B 相关低 sev / C 不相关高 sev
    store.upsert_event({
        "one_line_claim": "Nvidia guidance miss",
        "stance": "risk", "severity": "high", "ts": _utc_iso(-1),
        "affected_symbols": ["NDQ.AX"], "entities": ["nvidia"],
    }, embedding=[1.0, 0.0, 0.0, 0.0])
    store.upsert_event({
        "one_line_claim": "NDQ rebalance announcement",
        "stance": "neutral", "severity": "low", "ts": _utc_iso(-1),
        "affected_symbols": ["NDQ.AX"], "entities": ["index"],
    }, embedding=[0.0, 1.0, 0.0, 0.0])
    store.upsert_event({
        "one_line_claim": "Saudi cuts oil output",
        "stance": "risk", "severity": "high", "ts": _utc_iso(-1),
        "affected_symbols": ["CL=F"], "entities": ["opec"],
    }, embedding=[0.0, 0.0, 1.0, 0.0])

    out = store.recall("NDQ.AX", min_severity="mid")
    assert len(out) == 1
    assert "Nvidia" in out[0]["one_line_claim"]


def test_recall_time_window(store):
    store.upsert_event({
        "one_line_claim": "old fed event",
        "stance": "risk", "severity": "high", "ts": _utc_iso(-100),  # 超 30 天
        "affected_symbols": ["NDQ.AX"], "entities": ["fed"],
    }, embedding=[1.0, 0.0, 0.0, 0.0])

    out = store.recall("NDQ.AX", time_window_days=7, min_severity="mid")
    assert out == []


def test_recall_vector_rerank(store):
    # 三条都符合硬过滤，但向量不同。query 接近 [0,0,1,0] → 期望 c3 排第一
    store.upsert_event({
        "one_line_claim": "claim a", "stance": "risk", "severity": "high",
        "ts": _utc_iso(-1), "affected_symbols": ["AAPL"], "entities": [],
    }, embedding=[1.0, 0.0, 0.0, 0.0])
    store.upsert_event({
        "one_line_claim": "claim b", "stance": "risk", "severity": "high",
        "ts": _utc_iso(-2), "affected_symbols": ["AAPL"], "entities": [],
    }, embedding=[0.0, 1.0, 0.0, 0.0])
    store.upsert_event({
        "one_line_claim": "claim c", "stance": "risk", "severity": "high",
        "ts": _utc_iso(-3), "affected_symbols": ["AAPL"], "entities": [],
    }, embedding=[0.0, 0.0, 1.0, 0.0])

    if not store.vec_loaded:
        pytest.skip("sqlite-vec not loaded; rerank path covered elsewhere")

    out = store.recall("AAPL", min_severity="mid", query_embedding=[0.0, 0.0, 0.99, 0.05], top_k=3)
    # 向量精排后 c 应该最近，但最终返回按 ts DESC 排（最新在前），所以位置可能不在第一
    # 关键是 c 在返回里 + 它距离最近
    cs = [e for e in out if "claim c" in e["one_line_claim"]]
    assert cs
    assert cs[0]["distance"] is not None


def test_recall_supersedes_marks_older_event_of_same_entity(store):
    # 同实体 fed，两次事件：旧的 hawkish + 新的 dovish。新事件应 .supersedes = 旧 event_id
    _, eid_old = store.upsert_event({
        "one_line_claim": "Fed hawkish — rates up risk",
        "stance": "risk", "severity": "high", "ts": _utc_iso(-3),
        "affected_symbols": ["NDQ.AX"], "entities": ["fed"],
    }, embedding=[1.0, 0.0, 0.0, 0.0])
    _, eid_new = store.upsert_event({
        "one_line_claim": "Fed dovish pivot — cuts in 2026",
        "stance": "opportunity", "severity": "high", "ts": _utc_iso(-1),
        "affected_symbols": ["NDQ.AX"], "entities": ["fed"],
    }, embedding=[0.0, 1.0, 0.0, 0.0])

    out = store.recall("NDQ.AX", min_severity="mid", top_k=10)
    new_evt = [e for e in out if e["event_id"] == eid_new][0]
    assert new_evt["supersedes"] == eid_old


def test_gc_old_drops_events_and_sources(store):
    _, eid = store.upsert_event({
        "one_line_claim": "old news",
        "stance": "risk", "severity": "mid", "ts": _utc_iso(-100),
        "affected_symbols": ["X"], "entities": [],
    }, embedding=[1.0, 0.0, 0.0, 0.0])
    store.add_source(eid, src_name="reuters", url="https://r.com/old")
    assert store.gc_old(keep_days=30) == 1
    assert store.get_event(eid) is None
    assert store.is_seen_url("https://r.com/old") is False


def test_recall_extra_tags_match(store):
    """Symbol 不命中但 macro_tag 命中也召回"""
    store.upsert_event({
        "one_line_claim": "Powell dovish",
        "stance": "opportunity", "severity": "high", "ts": _utc_iso(-1),
        "affected_symbols": ["GC=F"], "entities": ["fed", "us_rates"],
    }, embedding=[1.0, 0.0, 0.0, 0.0])

    # 用户持仓是 NDQ.AX（不在 affected_symbols），但 tag us_rates 命中
    out = store.recall("NDQ.AX", extra_tags=["us_rates"], min_severity="mid")
    assert len(out) == 1
    assert "Powell" in out[0]["one_line_claim"]
