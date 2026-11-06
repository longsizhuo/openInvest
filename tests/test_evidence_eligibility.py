import json
from pathlib import Path

from openinvest.core.evidence_eligibility import Eligibility, digest, insight_payload
from openinvest.core.memory_store import MemoryStore
from openinvest.db.insights_db import InsightsDB


def approve(root, artifact_id, payload, *, outcome="wrong", parents=()):
    p = root / "integrity"
    p.mkdir(exist_ok=True)
    (p / "exclusions.jsonl").touch(exist_ok=True)
    file = p / "eligibility.json"
    state = json.loads(file.read_text()) if file.exists() else {"schema": 1, "artifacts": {}}
    state["artifacts"][artifact_id] = {
        "sha256": digest(payload), "source_validity": "valid", "judgment_outcome": outcome,
        "origin": "prospective", "allowed_uses": ["advice", "learn", "evaluate", "publish"],
        "review_id": "synthetic-review", "reviewed_at": "2026-11-06T00:00:00+00:00",
        "parents": list(parents),
        "parent_hashes": {key: state["artifacts"][key]["sha256"] for key in parents},
    }
    if artifact_id.startswith("insight:") or artifact_id == "derived:confidence_lookup":
        from openinvest.core.evidence_eligibility import runtime_scope
        state["artifacts"][artifact_id]["scope"] = runtime_scope()
    file.write_text(json.dumps(state))


def test_md_sqlite_deny_unknown_but_preserve_audit(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    store.write("insights/example", "insight", {"asset": "SYNTHETIC", "title": "title"}, "wrong forecast")
    db = InsightsDB(str(tmp_path / "db/insights.db"))
    try:
        db.upsert("example", "SYNTHETIC", "title", "wrong forecast")
        assert store.read("insights/example") is None and db.get("example") is None
        assert db.list_all() == [] and db.list_fresh() == []
        assert store.read("insights/example", purpose="audit").body == "wrong forecast"
        assert db.count() == 1 and len(db.list_all(purpose="audit")) == 1
        payload = insight_payload("example", {"asset": "SYNTHETIC", "title": "title"}, "wrong forecast")
        approve(tmp_path, "insight:example", payload)
        assert store.read("insights/example") is not None
        assert db.get("example") is not None  # valid negative sample is retained
        db.delete("example")
        db.upsert("example", "SYNTHETIC", "title", "wrong forecast")  # simulate old restore
        assert db.get("example") is None
        assert store.read("insights/example") is None
    finally:
        db.close()


def test_content_change_revocation_and_missing_exclusions_fail_closed(tmp_path):
    approve(tmp_path, "parent", {"raw": "observed"})
    approve(tmp_path, "child", {"derived": "negative outcome"}, parents=["parent"])
    policy = Eligibility(tmp_path)
    assert policy.allows("child", {"derived": "negative outcome"}, "learn")
    assert not policy.allows("child", {"derived": "edited outcome"}, "learn")
    policy.revoke("parent", "research participation revoked")
    assert not policy.allows("child", {"derived": "negative outcome"}, "learn")
    (tmp_path / "integrity/exclusions.jsonl").unlink()
    assert not policy.allows("child", {"derived": "negative outcome"}, "learn")


def test_pending_dream_never_writes_publishable_memory(tmp_path):
    from openinvest.jobs.dreaming import deep_sleep
    store = MemoryStore(tmp_path / "memory")
    c = dict(asset="SYNTHETIC", verdict="BUY", regime=["test"], window_days=7,
             count=10, hit_rate=0.95, avg_return_pct=4)
    assert deep_sleep(store, [c]) == []
    for name in ("insights", "MEMORY.md", "DREAMS.md"):
        assert not (store.root / name).exists()
    assert "source_lineage_and_independent_evaluation_required" in (store.root / ".dreams/events.jsonl").read_text()


def test_raw_tool_retrieval_cannot_bypass_md_gate(tmp_path, monkeypatch):
    import openinvest.core.memory_store as memory
    from openinvest.capabilities.tools import _impl_query_dreaming_insights
    monkeypatch.setattr(memory, "MEMORY_ROOT", tmp_path / "memory")
    store = MemoryStore()
    store.write("insights/synthetic", "insight", {"asset": "SYNTHETIC"}, "unverified")
    assert _impl_query_dreaming_insights("SYNTHETIC") == []
