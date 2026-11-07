import json
from pathlib import Path
import pytest

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


def test_insight_named_like_top_level_memory_does_not_recurse(tmp_path):
    store=MemoryStore(tmp_path/"memory")
    store.write("MEMORY","index",{},"old index")
    store.write("insights/MEMORY","insight",{"asset":"SYNTHETIC","title":"allowed insight"},"qualified content")
    doc=store.read("insights/MEMORY",purpose="audit")
    approve(tmp_path,"insight:MEMORY",insight_payload("MEMORY",doc.metadata,doc.body))
    assert store.read("insights/MEMORY").body=="qualified content"
    assert store.read("MEMORY").body=="qualified content"


def test_registry_deleted_and_research_revoked_are_negative_closure_roots(tmp_path):
    from datetime import datetime,timezone
    from openinvest.db.event_store import EventStore
    database=EventStore(str(tmp_path/"db/events.db"))
    for eid in ("deleted","revoked","child"):
        database.upsert_event({"event_id":eid,"one_line_claim":eid,"stance":"risk","severity":"high",
                               "affected_symbols":["SYNTHETIC"],"ts":datetime.now(timezone.utc).isoformat()})
    integrity=tmp_path/"integrity";integrity.mkdir()
    (integrity/"exclusions.jsonl").touch()
    (integrity/"eligibility.json").write_text(json.dumps({"schema":1,"artifacts":{
        "event:deleted":{"deleted":True},"event:revoked":{"research_participation":"revoked"},
        "event:child":{"parents":["event:deleted"]}}}))
    assert database.list_recent()==database.recall("SYNTHETIC")==[]
    for eid in ("deleted","revoked","child"):
        assert database.get_event(eid) is None
        assert database.get_event(eid,purpose="audit") is not None
    database.conn.close()


@pytest.mark.parametrize("invalid",[None,"",123,"z"*64])
def test_invalid_parent_digest_cannot_disable_parent_binding(tmp_path,invalid):
    root=tmp_path/"data";root.mkdir()
    approve(root,"source:parent",{"v":1})
    approve(root,"derived:child",{"v":2},parents=["source:parent"])
    path=root/"integrity/eligibility.json";state=json.loads(path.read_text())
    state["artifacts"]["derived:child"]["parent_hashes"]["source:parent"]=invalid
    state["artifacts"]["source:parent"]["sha256"]=digest({"v":3})
    path.write_text(json.dumps(state))
    assert not Eligibility(root).allows("derived:child",{"v":2},"learn")


@pytest.mark.parametrize("invalid",["not_advice",{"advice":False},["made_up_use"]])
def test_invalid_use_schema_does_not_grant_by_substring_or_dict_membership(tmp_path,invalid):
    root=tmp_path/"data";root.mkdir();approve(root,"source:sample",{"v":1})
    path=root/"integrity/eligibility.json";state=json.loads(path.read_text())
    state["artifacts"]["source:sample"]["allowed_uses"]=invalid;path.write_text(json.dumps(state))
    assert not Eligibility(root).allows("source:sample",{"v":1},"advice")


def test_unknown_model_insight_is_auditable_without_advice_qualification(tmp_path):
    from openinvest.core.evidence_eligibility import model_scope
    store=MemoryStore(tmp_path/"memory")
    store.write("insights/sample","insight",{"asset":"SYNTHETIC","title":"negative example"},"valid wrong forecast")
    doc=store.read("insights/sample",purpose="audit")
    with model_scope("hermes:unknown"):
        payload=insight_payload("sample",doc.metadata,doc.body)
        approve(tmp_path,"insight:sample",payload)
        assert store.read("insights/sample") is None
        assert store.read("insights/sample",purpose="audit").body=="valid wrong forecast"
        assert Eligibility(tmp_path).allows("insight:sample",payload,"learn")


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


def test_generated_index_and_diary_cannot_revive_revoked_parent(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    meta = {"asset":"SYNTHETIC", "title":"valid negative example"}
    body = "A real forecast was wrong; retain the negative example."
    store.write("insights/example", "insight", meta, body)
    approve(tmp_path, "parent", {"observed":"source"})
    approve(tmp_path, "insight:example", insight_payload("example",meta,body), parents=["parent"])
    for name in ("MEMORY","DREAMS"):
        store.write(name,"generated",{},"Old copied claim: " + body)
        assert body in store.read(name).body
    Eligibility(tmp_path).revoke("parent","source withdrawn")
    for name in ("MEMORY","DREAMS"):
        assert body not in store.read(name).body
        assert body in store.read(name,purpose="audit").body


def test_broken_revocation_ledger_is_not_empty_news(tmp_path, monkeypatch):
    import pytest
    from openinvest.core.evidence_eligibility import EvidenceIntegrityError
    from openinvest.core.runner import event_brief
    from openinvest.db.event_store import EventStore
    from openinvest.core.decision_evidence import preparation_state
    root = tmp_path
    (root / "integrity").mkdir()
    (root / "integrity/eligibility.json").write_text('{"schema":1,"artifacts":{}}')
    (root / "integrity/exclusions.jsonl").write_text('broken json')
    db = EventStore(str(root / "db/events.db"))
    monkeypatch.setattr(event_brief,"_EVENT_STORE_SINGLETON",db)
    with pytest.raises(EvidenceIntegrityError):
        event_brief.resolve_event_brief_multi(["SYNTHETIC"])
    with pytest.raises(EvidenceIntegrityError):
        preparation_state(root / "memory")
    db.conn.close()


def test_structurally_invalid_governance_is_visible_and_blocks_preparation(tmp_path):
    import pytest
    from openinvest.core.evidence_eligibility import EvidenceIntegrityError
    from openinvest.core.decision_evidence import preparation_state
    from openinvest.services.decision_quality import findings
    directory=tmp_path / "integrity"; directory.mkdir()
    (directory / "exclusions.jsonl").write_text("")
    for registry in ([], {"schema":1,"artifacts":None}, {"schema":999,"artifacts":{}},
                     {"schema":1,"artifacts":{"x":{"parents":None}}}):
        (directory / "eligibility.json").write_text(json.dumps(registry))
        with pytest.raises(EvidenceIntegrityError):
            preparation_state(tmp_path / "memory")
        assert any(x["key"]=="quality:evidence_governance_unavailable" for x in findings(tmp_path / "memory"))
