"""Unknown acquisition cannot become a report or borrow another version's trust."""
from datetime import datetime,timezone,timedelta
import json
import pytest

from openinvest.db.event_store import EventStore
from openinvest.services import agent_queue as q
from openinvest.services.event_ingest import ingest_events
from tests.test_news_source_snapshots import source_context,captured_sina,packet,news
from tests.test_analysis_dispatch_recovery import context,enqueue,NOW


def test_normal_unbound_agent_submission_remains_audit_only_on_every_advice_read(source_context):
    response=ingest_events([{"title":"Unverified report","url":"https://example.invalid/unbound",
        "snippet":"Agent invented this source text","one_line_claim":"Agent invented this source text",
        "stance":"risk","severity":"high","affected_symbols":["SYNTHETIC"]}])
    assert response["ingested"]==0 and response["audit_only_event_ids"]
    reader=EventStore(str(source_context/"db/events.db"))
    eid=response["audit_only_event_ids"][0]
    assert reader.get_event(eid) is None and reader.get_event(eid,purpose="audit")
    assert reader.list_recent()==[] and reader.list_current_with_sources()==[]
    assert reader.recall("SYNTHETIC")==[] and reader.recall("SYNTHETIC",as_of=datetime.now(timezone.utc))==[]
    assert reader.get_sources(eid)==[] and reader.get_sources(eid,purpose="audit")[0]["snippet"]=="Agent invented this source text"
    assert reader.source_reporting_summary()["current_unqualified_events"]==1
    reader.conn.close()


def test_merely_sealed_adapter_input_is_not_automatic_source_authentication(source_context):
    q.enqueue_news([news()]);candidate=q.pending_news_items()[0]
    response=ingest_events([packet(candidate)])
    assert response["ingested"]==0
    reader=EventStore(str(source_context/"db/events.db"))
    assert reader.list_recent()==[] and reader.list_recent(purpose="audit")
    reader.conn.close()


def test_unknown_correction_cannot_overwrite_verified_current_report(source_context,monkeypatch):
    source=captured_sina(monkeypatch)
    response=ingest_events([packet(source)]);assert response["ingested"]==1
    eid=response["events"][0]["event_id"]
    reader=EventStore(str(source_context/"db/events.db"))
    before=reader.get_event(eid)
    from openinvest.services.news_sources import RawNewsItem
    unknown=RawNewsItem(src_name="agent:unverified",url="https://example.invalid/copy",title="Different fabricated report",snippet="Fabricated")
    attempted=reader.write_news_observation({**before,"event_id":eid,"one_line_claim":"Fabricated reopening","stance":"neutral","severity":"low"},unknown)
    assert attempted["audit_only"] and attempted["source_verification_required"]
    assert reader.get_event(eid)["one_line_claim"]==before["one_line_claim"]
    assert reader.conn.execute("SELECT COUNT(*) FROM news_observation_conflicts").fetchone()[0]==1
    reader.conn.close()


def test_unknown_sibling_text_is_filtered_and_changed_claim_cannot_borrow_trust(source_context,monkeypatch):
    source=captured_sina(monkeypatch);response=ingest_events([packet(source)]);eid=response["events"][0]["event_id"]
    reader=EventStore(str(source_context/"db/events.db"))
    reader.add_source(eid,src_name="agent:unknown",url="https://example.invalid/sibling",title="Invented",snippet="UNVERIFIED_SIBLING")
    assert reader.get_event(eid)
    assert len(reader.get_sources(eid))==1 and len(reader.get_sources(eid,purpose="audit"))==2
    recalled=reader.recall("SYNTHETIC",min_severity="low")
    assert recalled and all(source["snippet"]!="UNVERIFIED_SIBLING" for source in recalled[0]["sources"])
    reader.upsert_event({**reader.get_event(eid,purpose="audit"),"one_line_claim":"Unauthenticated new claim"},revision=True)
    assert reader.get_event(eid) is None
    original=reader.get_sources(eid,purpose="audit")[0]
    assert not reader.source_unchanged(original["url"],original["title"],original["snippet"],published_at=original["published_at"],raw_text=original["raw_text"])
    reader.conn.close()


def test_source_isolation_is_visible_to_rag_sentiment_and_quality_monitor(source_context,monkeypatch):
    response=ingest_events([{"title":"Unknown report","url":"https://example.invalid/source",
        "one_line_claim":"UNVERIFIED_PRIVATE_CLAIM","stance":"risk","severity":"high","affected_symbols":["SYNTHETIC"]}])
    assert response["audit_only_event_ids"]
    from openinvest.core.runner import event_brief as eb
    from openinvest.core.runner.loaders import append_event_stance_line
    from openinvest.services.decision_quality import findings
    reader=EventStore(str(source_context/"db/events.db"))
    monkeypatch.setattr(eb,"_get_event_store",lambda:reader)
    brief=eb.resolve_event_brief_multi(["SYNTHETIC"])
    assert "[SOURCE_QUALITY]" in brief and "UNVERIFIED_PRIVATE_CLAIM" not in brief
    assert brief.source_quality["current_unqualified_events"]==1
    assert "unknown" in append_event_stance_line("SYNTHETIC MARKET SENTIMENT",brief,"SYNTHETIC")
    assert any(row["key"].startswith("quality:source_authentication_pending:") for row in findings(source_context/"memory"))
    reader.conn.close()


def test_unverified_followup_is_retained_and_can_resume_after_content_bound_review(context):
    from openinvest.services import analysis_dispatch as dispatch
    from openinvest.services.decision_quality import findings
    from tests.integrity_helpers import approve_event_reporting
    store,box,calls=context
    database=EventStore(str(store.data_root/"db/events.db"))
    _,eid=database.upsert_event({"event_id":"unknown","one_line_claim":"Unknown acquisition","severity":"high","stance":"risk",
                               "affected_symbols":["GC=F"],"ts":NOW.isoformat()})
    ident=enqueue(box,"pending-source",["GC=F"],event_ids=[eid],event_symbols={eid:["GC=F"]})
    box.flush(now=NOW,sender=lambda *args:True)
    dispatch.dispatch_followups(box,now=NOW,event_store=database)
    with box.connection() as connection:
        pending=connection.execute("SELECT status,closed,submitted FROM followups WHERE notification_id=?",(ident,)).fetchone()
    assert pending["status"]=="verification_required" and json.loads(pending["closed"])=={} and json.loads(pending["submitted"])=={}
    assert calls==[] and any(row["key"].startswith("quality:followup_uncertain:") for row in findings(store.root))
    approve_event_reporting(database,eid)
    dispatch.dispatch_followups(box,now=NOW+timedelta(minutes=1),event_store=database)
    with box.connection() as connection:
        assert connection.execute("SELECT status FROM followups WHERE notification_id=?",(ident,)).fetchone()[0]=="queued"
    assert len(calls)==1
    database.conn.close()


@pytest.mark.parametrize("mode",["backend","agent"])
def test_same_asset_retains_the_second_unverified_cause_after_dispatching_the_first(context,monkeypatch,mode):
    from openinvest.services import analysis_dispatch as dispatch
    from tests.integrity_helpers import approve_event_reporting
    store,box,calls=context
    monkeypatch.setenv("INVEST_REASONING_BY",mode)
    if mode=="agent":monkeypatch.setattr(q,"_now",lambda:NOW)
    database=EventStore(str(store.data_root/"db/events.db"))
    for eid in ("a","b"):
        database.upsert_event({"event_id":eid,"one_line_claim":eid,"severity":"high","stance":"risk",
                               "affected_symbols":["GC=F"],"ts":NOW.isoformat()})
    approve_event_reporting(database,"a")
    ident=enqueue(box,"two-causes-one-asset",["GC=F"],event_ids=["a","b"],event_symbols={"a":["GC=F"],"b":["GC=F"]})
    box.flush(now=NOW,sender=lambda *args:True)
    dispatch.dispatch_followups(box,now=NOW,event_store=database)
    assert [call["event_ids"] for call in calls]==([["a"]] if mode=="backend" else [])
    if mode=="agent":assert q.pending_committee_items(now=NOW,memory_root=store.root)[0]["event_ids"]==["a"]
    with box.connection() as connection:
        assert connection.execute("SELECT status FROM followups WHERE notification_id=?",(ident,)).fetchone()[0]=="verification_required"
    approve_event_reporting(database,"b")
    dispatch.dispatch_followups(box,now=NOW+timedelta(hours=13),event_store=database)
    assert [call["event_ids"] for call in calls]==([["a"],["b"]] if mode=="backend" else [])
    if mode=="agent":
        actual={eid for request in q.pending_committee_items(now=NOW,memory_root=store.root) for eid in request["event_ids"]}
        assert actual=={"a","b"}
    with box.connection() as connection:
        result=connection.execute("SELECT status,submitted_causes FROM followups WHERE notification_id=?",(ident,)).fetchone()
    assert result["status"]=="queued" and set(json.loads(result["submitted_causes"])["GC=F"])=={"a","b"}
    database.conn.close()


def test_resolved_source_incident_cannot_permanently_silence_a_later_one(source_context):
    from openinvest.services.decision_quality import findings
    from tests.integrity_helpers import approve_event_reporting
    reader=EventStore(str(source_context/"db/events.db"))
    def new_incident(name):
        _,eid=reader.upsert_event({"event_id":name,"one_line_claim":name,"severity":"high","stance":"risk",
                                  "affected_symbols":["SYNTHETIC"]})
        alert=next(row for row in findings(source_context/"memory") if row["key"].startswith("quality:source_authentication_pending:"))
        return eid,alert["key"]+"#once"
    first,first_claim=new_incident("first")
    approve_event_reporting(reader,first)
    assert not any(row["key"].startswith("quality:source_authentication_pending:") for row in findings(source_context/"memory"))
    _,second_claim=new_incident("second")
    assert first_claim!=second_claim
    reader.conn.close()


def test_withdrawn_reservation_cannot_permanently_starve_another_asset(context):
    from openinvest.services import analysis_dispatch as dispatch
    from openinvest.core.evidence_eligibility import Eligibility
    from tests.integrity_helpers import approve_event_reporting
    store,box,calls=context
    database=EventStore(str(store.data_root/"db/events.db"))
    for eid,symbol in (("a","GC=F"),("b","NDQ.AX")):
        database.upsert_event({"event_id":eid,"one_line_claim":eid,"severity":"high","stance":"risk","affected_symbols":[symbol]})
        approve_event_reporting(database,eid)
    ident=enqueue(box,"withdrawn-reservation",["GC=F","NDQ.AX"],event_ids=["a","b"],event_symbols={"a":["GC=F"],"b":["NDQ.AX"]})
    box.flush(now=NOW,sender=lambda *args:True)
    with box.connection() as connection:
        connection.execute("UPDATE followups SET status='preparing',reserved=?,changed=? WHERE notification_id=?",(json.dumps(["GC=F"]),NOW.timestamp(),ident))
    Eligibility(store.data_root).revoke("event:a","synthetic withdrawal while preparing")
    dispatch.dispatch_followups(box,now=NOW+timedelta(minutes=6),event_store=database)
    assert [call["symbols"] for call in calls]==[["NDQ.AX"]]
    with box.connection() as connection:
        result=connection.execute("SELECT status,reserved FROM followups WHERE notification_id=?",(ident,)).fetchone()
    assert result["status"]=="partially_cancelled" and json.loads(result["reserved"])==[]
    database.conn.close()


@pytest.mark.parametrize("failure",["unqualified","unavailable","read_error","advisory"])
def test_explicit_event_id_failure_cannot_turn_into_empty_news_and_a_normal_committee(source_context,monkeypatch,failure):
    # Prepare this synthetic ledger before imports that initialize its storage.
    (source_context/"memory").mkdir(exist_ok=True)
    from tests.test_committee_contract import _seed_minimal_memory
    from openinvest.core.runner import session
    from openinvest.core.evidence_eligibility import EvidenceIntegrityError
    import sqlite3
    _seed_minimal_memory(source_context/"memory")
    reader=EventStore(str(source_context/"db/events.db"))
    _,eid=reader.upsert_event({"one_line_claim":"Unknown source","severity":"high","stance":"risk","affected_symbols":["TEST.AX"]})
    monkeypatch.setattr(session,"_get_event_store",lambda:None if failure=="unavailable" else reader)
    if failure=="read_error":
        def fail_read(*args):raise sqlite3.OperationalError("synthetic read failure")
        monkeypatch.setattr(reader,"get_event_with_sources",fail_read)
    if failure=="advisory":monkeypatch.setattr(session,"is_advisory_mode",lambda:True)
    model_calls=[]
    monkeypatch.setattr(session,"run_macro_view",lambda *args,**kwargs:model_calls.append("macro"))
    monkeypatch.setattr(session,"run_committee_for_symbol",lambda *args,**kwargs:model_calls.append("committee"))
    with pytest.raises(EvidenceIntegrityError,match="DATA_UNTRUSTED"):
        session.run_committee_session(symbols=["TEST.AX"],event_ids=[eid])
    assert model_calls==[]
    reader.conn.close()
