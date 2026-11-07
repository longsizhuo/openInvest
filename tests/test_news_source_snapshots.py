"""Source corrections survive handoff, without converting inference to source text."""
from datetime import datetime,timedelta,timezone
import json
import sqlite3

import pytest

from openinvest.core.memory_store import MemoryStore
from openinvest.db.event_store import EventStore
from openinvest.services import agent_queue as q
from openinvest.services.event_ingest import ingest_events
from openinvest.services.news_sources import RawNewsItem
from tests.integrity_helpers import reviewed_event_storage,approve_event_reporting


@pytest.fixture
def source_context(tmp_path,monkeypatch):
    monkeypatch.setattr("openinvest.paths.INVEST_ROOT",tmp_path)
    monkeypatch.setattr("openinvest.core.memory_store.MEMORY_ROOT",tmp_path/"memory")
    monkeypatch.setattr("openinvest.db.event_store.DB_PATH",str(tmp_path/"db/events.db"))
    monkeypatch.setenv("INVEST_REASONING_BY","agent")
    monkeypatch.setattr("openinvest.services.event_trigger._watched_symbols",lambda:[])
    monkeypatch.setattr("openinvest.services.embeddings.embed_text",lambda text:None)
    return tmp_path


def news(title="Original port closure"):
    return RawNewsItem(src_name="verified-feed",title=title,url="https://example.invalid/story",
                       snippet=title,text=title+". This is the original source body.")


def packet(item,**updates):
    return {"title":item["title"],"url":item["url"],"snippet":item["snippet"],
            "source_snapshot_id":item["source_snapshot_id"],"one_line_claim":item["title"],
            "stance":"risk","severity":"high","affected_symbols":["SYNTHETIC"],**updates}


def test_identical_url_new_body_and_dismissed_story_still_offer_correction(source_context):
    assert q.enqueue_news([news()])==1
    first=q.pending_news_items()[0]
    q.pending_news(dismiss_source_ids=[first["source_snapshot_id"]])
    assert q.pending_news_items()==[]
    assert q.enqueue_news([news("Port has reopened")])==1
    current=q.pending_news_items()
    assert len(current)==1 and current[0]["source_snapshot_id"]!=first["source_snapshot_id"]


def test_binding_cannot_modify_original_source_and_old_completion_stays_completed(source_context,reviewed_event_storage):
    q.enqueue_news([news()]);first=q.pending_news_items()[0]
    out=ingest_events([packet(first,snippet="Agent added an unreported gold demand conclusion")])
    assert out["ingested"]==1
    database=EventStore(str(source_context/"db/events.db"))
    saved=database.get_sources(out["events"][0]["event_id"])[0]
    assert saved["snippet"]==first["snippet"] and saved["raw_text"]==first["text"]
    assert json.loads(saved["provenance_json"])["source_origin"]=="captured_adapter_input"
    assert q.pending_news_items()==[]
    q.enqueue_news([news("Port has reopened")]);second=q.pending_news_items()[0]
    assert second["source_snapshot_id"]!=first["source_snapshot_id"]
    assert ingest_events([packet(second,stance="neutral",severity="low")])["ingested"]==1
    assert q.pending_news_items()==[]  # The old raw version must not reappear.
    database.conn.close()


def test_unbound_source_rewrite_is_rejected_without_claiming_ingestion(source_context):
    q.enqueue_news([news()]);first=q.pending_news_items()[0]
    changed=packet(first,snippet="Invented causal source text");changed.pop("source_snapshot_id")
    result=ingest_events([changed])
    assert result["status"]=="error" and "content_mismatch" in result["error"]
    assert not (source_context/"db/events.db").exists()
    assert q.pending_news_items()[0]["source_snapshot_id"]==first["source_snapshot_id"]


def test_qualified_readonly_api_cannot_write_or_create_missing_database(source_context):
    path=source_context/"db/events.db"
    writer=EventStore(str(path))
    writer.upsert_event({"event_id":"sample","one_line_claim":"Original report","stance":"risk","severity":"high",
                         "affected_symbols":["SYNTHETIC"],"ts":datetime.now(timezone.utc).isoformat()})
    approve_event_reporting(writer,"sample")
    writer.conn.close()
    reader=EventStore(str(path),readonly=True)
    assert reader.list_current_with_sources()[0]["claim_kind"]=="normalized_summary_not_verbatim_source"
    with pytest.raises(sqlite3.OperationalError):reader.upsert_event({"one_line_claim":"forbidden"})
    reader.conn.close()
    missing=source_context/"does-not-exist/events.db"
    with pytest.raises(sqlite3.OperationalError):EventStore(str(missing),readonly=True)
    assert not missing.parent.exists()


def test_mutable_queue_cannot_change_the_canonical_source_even_with_a_recomputed_hash(source_context):
    q.enqueue_news([news()]);original=q.pending_news_items()[0]
    path=source_context/"memory/.agent_queue/news.jsonl"
    forged=json.loads(path.read_text());forged.update(title="Invented report",snippet="Invented causal claim",text="Invented raw source")
    forged["source_snapshot_id"]=q.news_content_hash(forged)
    path.write_text(json.dumps(forged)+"\n")
    assert q.pending_news_items()[0]["title"]==original["title"]
    result=ingest_events([packet(forged)])
    assert result["status"]=="error" and "unknown source_snapshot_id" in result["error"]


def test_expired_unprocessed_source_remains_visible_and_watchdog_reports_it(source_context,monkeypatch):
    from openinvest.jobs.job_watchdog import find_agent_backlog
    earlier=datetime.now(timezone.utc)-timedelta(hours=49)
    monkeypatch.setattr(q,"_now",lambda:earlier)
    q.enqueue_news([news()])
    now=earlier+timedelta(hours=49)
    monkeypatch.setattr(q,"_now",lambda:now)
    result=q.pending_news()
    assert result["pending"]==0 and result["expired_unprocessed"]==1
    assert q.pending_news(include_expired=True)["items"][0]["status"]=="expired_unprocessed"
    assert find_agent_backlog(now)


def test_same_url_batch_keeps_each_normalization_with_its_own_source(source_context,reviewed_event_storage):
    q.enqueue_news([news("First state"),news("Second state")]);items=q.pending_news_items()
    result=ingest_events([packet(item) for item in items])
    assert result["ingested"]==2
    database=EventStore(str(source_context/"db/events.db"))
    for row in database.conn.execute("SELECT payload_json FROM event_versions"):
        event=json.loads(row[0])
        if event["sources"]:
            assert event["one_line_claim"] in event["sources"][0]["snippet"]
    database.conn.close()


def captured_sina(monkeypatch):
    from openinvest.services import news_collection as collection
    monkeypatch.setattr(collection,"SOURCES",("sina:152",))
    now=datetime.now(timezone.utc)
    def timestamp(delta):
        return (now-timedelta(minutes=delta)).astimezone(collection.CST).strftime("%Y-%m-%d %H:%M:%S")
    body=json.dumps({"result":{"data":{"feed":{"list":[
        {"id":"synthetic-item","create_time":timestamp(5),"rich_text":"Original port closure"},
        {"id":"boundary","create_time":timestamp(120),"rich_text":"outside interval"}]}}}}).encode()
    collection.collect(since=(now-timedelta(hours=1)).isoformat(),now=now,
                       fetcher=lambda *args:("https://source.invalid/feed",body))
    return q.pending_news_items()[0]


def test_reserved_locator_cannot_be_rewritten_with_whitespace_or_foreign_adapter(source_context,monkeypatch):
    original=captured_sina(monkeypatch)
    assert original["url"].startswith("openinvest-source://sina/152/")
    saved=ingest_events([packet(original)])
    assert saved["ingested"]==1
    forged=packet(original,url=" "+original["url"],title="Invented reopening",snippet="Invented reopening")
    forged.pop("source_snapshot_id")
    assert ingest_events([forged])["status"]=="error"
    q.enqueue_news([RawNewsItem(src_name="rss:unrelated",url=original["url"],title="Invented reopening",snippet="Invented reopening")])
    assert q.pending_news_items()==[]
    foreign=q._read("news.jsonl")[-1]
    assert ingest_events([packet(foreign)])["status"]=="error"
    from openinvest.services.news_collection import source_snapshot
    assert source_snapshot(foreign["source_snapshot_id"])["allowed_use"]=="audit_only"
    reader=EventStore(str(source_context/"db/events.db"),readonly=True)
    assert reader.get_event(saved["events"][0]["event_id"])["one_line_claim"]==original["title"]
    reader.conn.close()


@pytest.mark.parametrize("parent",["source_snapshot","source_response"])
def test_revoked_capture_or_parent_blocks_pending_binding_and_remains_auditable(source_context,monkeypatch,parent):
    from openinvest.core.evidence_eligibility import Eligibility
    from openinvest.services.news_collection import source_snapshot
    original=captured_sina(monkeypatch)
    ident=original["source_snapshot_id"] if parent=="source_snapshot" else original["raw_meta"]["response_sha256"]
    Eligibility(source_context).revoke(parent+":"+ident,"synthetic revocation")
    assert q.pending_news_items()==[]
    audit=source_snapshot(original["source_snapshot_id"])
    assert audit["processing_status"]=="revoked" and audit["allowed_use"]=="audit_only"
    assert audit["original_input"]["text"]==original["text"]
    assert ingest_events([packet(original)])["status"]=="error"


def test_revocation_propagates_to_stored_event_and_all_advice_reads(source_context,monkeypatch):
    from openinvest.core.evidence_eligibility import Eligibility
    original=captured_sina(monkeypatch)
    saved=ingest_events([packet(original)]);eid=saved["events"][0]["event_id"]
    gate=Eligibility(source_context)
    gate.revoke("source_snapshot:"+original["source_snapshot_id"],"synthetic exclusion")
    assert "event:"+eid in gate.excluded_ids()
    reader=EventStore(str(source_context/"db/events.db"),readonly=True)
    assert reader.get_event(eid) is None and reader.get_event(eid,purpose="audit")
    assert reader.list_recent()==[] and reader.list_current_with_sources()==[]
    assert reader.recall("SYNTHETIC")==[]
    assert reader.recall("SYNTHETIC",as_of=datetime.now(timezone.utc))==[]
    reader.conn.close()


def test_json_receipts_cannot_hide_pending_canonical_database_state(source_context):
    q.enqueue_news([news()]);original=q.pending_news_items()[0]
    receipt={"source_snapshot_id":original["source_snapshot_id"],"event_id":"nonexistent"}
    for name in ("news_completions.jsonl","news_dismissed.jsonl"):
        (source_context/"memory/.agent_queue"/name).write_text(json.dumps(receipt)+"\n")
    assert q.pending_news_items()[0]["source_snapshot_id"]==original["source_snapshot_id"]


def test_legacy_queue_is_explicitly_audit_only_and_requires_reacquisition(source_context):
    path=source_context/"memory/.agent_queue/news.jsonl";path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"url":"https://example.invalid/legacy","title":"Legacy text",
                               "queued_at":datetime.now(timezone.utc).isoformat()})+"\n")
    result=q.pending_news()
    assert result["pending"]==0 and result["legacy_verification_required"]==1
    legacy=result["legacy_items"][0]
    assert legacy["allowed_use"]=="audit_only" and "source_snapshot_id" not in legacy
