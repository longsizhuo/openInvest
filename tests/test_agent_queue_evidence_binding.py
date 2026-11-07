from datetime import datetime, timedelta, timezone
import json

from openinvest.core.memory_store import MemoryStore
from openinvest.core.runner.event_format import EventBrief
from openinvest.services import agent_queue as q


def test_later_report_does_not_complete_a_request_from_older_inputs(tmp_path,monkeypatch):
    root=tmp_path/"memory";root.mkdir()
    monkeypatch.setattr("openinvest.core.memory_store.MEMORY_ROOT",root)
    now=datetime.now(timezone.utc)
    monkeypatch.setattr(q,"_now",lambda:now)
    q.enqueue_committee(["SYNTHETIC"],[],"new risk")
    day=root/".committee"/now.date().isoformat();day.mkdir(parents=True)
    (day/"SYNTHETIC.md").write_text(f"# Committee: SYNTHETIC\n**Symbol**: SYNTHETIC\n**Run at**: {(now+timedelta(minutes=5)).isoformat()}\n**Verdict**: HOLD (confidence 0.6)\n")
    assert len(q.pending_committee_items(now+timedelta(minutes=6)))==1
    assert q.bind_context_requests(root,"SYNTHETIC",EventBrief("",{}),(now-timedelta(minutes=1)).isoformat())==[]


def test_all_causes_survive_and_versions_must_be_covered(tmp_path,monkeypatch):
    from openinvest.db.event_store import EventStore
    root=tmp_path/"memory";root.mkdir()
    monkeypatch.setattr("openinvest.core.memory_store.MEMORY_ROOT",root)
    now=datetime.now(timezone.utc)
    db=EventStore(str(tmp_path/"db/events.db"))
    event={"event_id":"cause", "one_line_claim":"synthetic", "stance":"risk", "severity":"mid",
           "affected_symbols":["SYNTHETIC"],"ts":now.isoformat()}
    db.upsert_event(event);first=db.get_event("cause")["_version_id"]
    q.enqueue_committee(["SYNTHETIC"],["cause"],"first")
    db.upsert_event({**event,"severity":"high"});second=db.get_event("cause")["_version_id"]
    q.enqueue_committee(["SYNTHETIC"],["cause"],"escalated")
    assert len(q.pending_committee_items()[0]["request_ids"])==2
    prepared=datetime.now(timezone.utc).isoformat()
    assert len(q.bind_context_requests(root,"SYNTHETIC",EventBrief("facts",{"cause":first}),prepared))==1
    assert len(q.bind_context_requests(root,"SYNTHETIC",EventBrief("facts",{"cause":second}),prepared))==2
    db.conn.close()


def test_more_than_eight_event_causes_are_retained_in_one_request(tmp_path,monkeypatch):
    root=tmp_path/"memory"
    monkeypatch.setattr("openinvest.core.memory_store.MEMORY_ROOT",root)
    ids=[f"cause-{i}" for i in range(20)]
    q.enqueue_committee(["SYNTHETIC"],ids,"multiple independent changes")
    assert q.pending_committee_items()[0]["event_ids"]==ids


def test_bound_completed_decision_acknowledges_exact_request(tmp_path,monkeypatch):
    from openinvest.core.decision_evidence import prepare_context,record_decision,_write_once
    root=tmp_path/"memory";root.mkdir()
    monkeypatch.setattr("openinvest.core.memory_store.MEMORY_ROOT",root)
    q.enqueue_committee(["SYNTHETIC"],[],"review")
    bindings=q.bind_context_requests(root,"SYNTHETIC",EventBrief("",{}),datetime.now(timezone.utc).isoformat())
    context=prepare_context(root,"SYNTHETIC",{"trigger_requests":bindings})
    evidence=record_decision(root,"SYNTHETIC",{}, {"verdict":"HOLD","decision_status":"READY","reasoning_review":{"status":"passed","origin":"synthetic_unit_fixture"}},{},context_id=context["context_id"])
    directory=root/".evidence/transcripts";directory.mkdir()
    (directory/(evidence["decision_id"]+".md")).write_text("synthetic transcript")
    _write_once(directory/(evidence["decision_id"]+".json"),{"text":"synthetic transcript"})
    assert q.acknowledge_context_decision(root,context["context_id"],evidence["decision_id"])==1
    assert q.pending_committee_items()==[]


def test_expired_requests_remain_visible_even_after_polling(tmp_path,monkeypatch):
    from openinvest.jobs.job_watchdog import find_agent_backlog
    root=tmp_path/"memory";root.mkdir()
    monkeypatch.setattr("openinvest.core.memory_store.MEMORY_ROOT",root)
    now=datetime.now(timezone.utc)
    monkeypatch.setattr(q,"_now",lambda:now-timedelta(hours=25))
    q.enqueue_committee(["SYNTHETIC"],[],"risk")
    monkeypatch.setattr(q,"_now",lambda:now)
    q.mark_drained()
    assert q.pending_committee_items(now)==[]
    assert q.pending_committee_items(now,include_expired=True)[0]["status"]=="expired"
    assert any("SYNTHETIC" in r["text"] for r in find_agent_backlog(now))


def test_reasoning_review_wait_does_not_hide_expired_risk_cause(tmp_path,monkeypatch):
    root=tmp_path/"memory";root.mkdir()
    now=datetime.now(timezone.utc)
    monkeypatch.setattr(q,"_now",lambda:now-timedelta(hours=25))
    q.enqueue_committee(["SYNTHETIC"],[],"risk",memory_root=root)
    binding={"request_id":q.pending_committee_items(memory_root=root)[0]["request_ids"][0],"symbol":"SYNTHETIC"}
    q.mark_requests_waiting_review(root,[binding],"synthetic_context","synthetic_hash","failed")
    pending=q.pending_committee_items(now,include_expired=True,memory_root=root)[0]
    assert pending["status"]=="expired" and pending["expired"]
    assert pending["review_requests"][0]["review_status"]=="failed"


from tests.integrity_helpers import reviewed_event_storage
import pytest

# These tests exercise storage/ranking/dispatch with explicitly reviewed fixtures.
pytestmark = pytest.mark.usefixtures("reviewed_event_storage")
