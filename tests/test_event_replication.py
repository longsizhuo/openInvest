from pathlib import Path

from openinvest.core.evidence_eligibility import Eligibility
from openinvest.db.event_store import EventStore
from openinvest.services.event_replication import sync_public_events
from tests.integrity_helpers import approve_event_reporting,approve_public_event


def seed_public(root,claim="Approved report"):
    store=EventStore(str(root/"db/events.db"))
    _,eid=store.upsert_event({"event_id":"public","one_line_claim":claim,"stance":"risk","severity":"high","affected_symbols":["SYNTHETIC"]})
    store.add_source(eid,src_name="public",url="https://www.reuters.com/public-item",title="Public original",snippet="Original source report")
    return store,eid


def test_replication_filters_private_events_and_honors_revocation(tmp_path):
    source=tmp_path/"source"; target=tmp_path/"target"
    store=EventStore(str(source/"db/events.db"))
    try:
        _, news=store.upsert_event({"one_line_claim":"Synthetic public news", "stance":"risk", "severity":"mid", "affected_symbols":["SYNTHETIC"],
                                   "source_reliability":"UNAPPROVED_PRIVATE_FIELD"})
        store.add_source(news,src_name="synthetic",url="https://www.reuters.com/synthetic-test")
        _, private=store.upsert_event({"one_line_claim":"Private account alarm", "event_type":"price_alert", "stance":"risk", "severity":"mid", "affected_symbols":["SYNTHETIC"]})
        store.add_source(private,src_name="synthetic",url="https://www.reuters.com/synthetic-private")
        before=store.conn.execute("SELECT count(*) FROM events").fetchone()[0]
        # A domain alone cannot grant advice or permission to share a summary.
        assert sync_public_events(source,target,{"reuters.com"})["imported_public_events"]==0
        approve_event_reporting(store,news)
        assert sync_public_events(source,target,{"reuters.com"})["publication_review_required"]==1
        approve_public_event(store,news)
        result=sync_public_events(source,target,{"reuters.com"})
        assert result["imported_public_events"] == 1
        dst=EventStore(str(target/"db/events.db"))
        try:
            assert dst.get_event(news) is None and dst.get_event(news,purpose="audit")
            assert dst.get_event(news,purpose="audit")["source_reliability"] is None
            assert dst.get_event(private,purpose="audit") is None
            # Imported upstream IDs are not acquisition evidence in the target.
            approve_event_reporting(dst,news)
            assert dst.get_event(news)
            Eligibility(source).revoke(f"event:{news}","synthetic source invalidated")
            sync_public_events(source,target,{"reuters.com"})
            assert dst.get_event(news) is None
            assert dst.get_event(news,purpose="audit") is not None
            assert dst.recall("SYNTHETIC") == []
        finally:
            dst.conn.close()
        assert store.conn.execute("SELECT count(*) FROM events").fetchone()[0] == before
    finally:
        store.conn.close()


def test_publication_check_and_copy_use_the_same_event_snapshot(tmp_path,monkeypatch):
    source,target=tmp_path/"source",tmp_path/"target"
    writer,eid=seed_public(source,claim="PRIVATE OLD SUMMARY")
    read=EventStore.get_event_with_sources
    updated=False
    def update_before_qualified_snapshot(reader,event_id,**kwargs):
        nonlocal updated
        if reader.data_root==source and not updated:
            updated=True
            writer.upsert_event({**writer.get_event(eid,purpose="audit"),"one_line_claim":"APPROVED PUBLIC SUMMARY"},revision=True)
            approve_event_reporting(writer,eid);approve_public_event(writer,eid)
        return read(reader,event_id,**kwargs)
    monkeypatch.setattr(EventStore,"get_event_with_sources",update_before_qualified_snapshot)
    sync_public_events(source,target,{"reuters.com"})
    replica=EventStore(str(target/"db/events.db"))
    try:
        saved=replica.get_event(eid,purpose="audit")
        assert saved and saved["one_line_claim"]=="APPROVED PUBLIC SUMMARY"
    finally:replica.conn.close();writer.conn.close()


def test_publication_withdrawal_blocks_dependent_copy_but_not_independent_capture(tmp_path):
    from openinvest.services.event_replication import publication_id
    from openinvest.services.news_sources import fetch_all
    from openinvest.services.agent_queue import enqueue_news
    from unittest.mock import patch,MagicMock
    source,target=tmp_path/"source",tmp_path/"target"
    writer,eid=seed_public(source)
    approve_event_reporting(writer,eid);approve_public_event(writer,eid)
    key=publication_id(writer.get_event(eid),writer.get_sources(eid))
    sync_public_events(source,target,{"reuters.com"})
    replica=EventStore(str(target/"db/events.db"))
    try:
        approve_event_reporting(replica,eid);assert replica.get_event(eid)
        Eligibility(source).revoke(key,"synthetic publication withdrawn")
        assert sync_public_events(source,target,{"reuters.com"})["propagated_exclusions"]==1
        assert replica.get_event(eid) is None and replica.get_sources(eid)==[]
        parsed=MagicMock();parsed.entries=[{"title":"Public original","link":"https://www.reuters.com/public-item","summary":"Original source report"}]
        with patch("openinvest.services.news_sources.rss_feed._fetch_feed",return_value=parsed):
            items=fetch_all(rss_feeds=[{"name":"independent","url":"https://feed.invalid"}],capture=True,data_root=target)
        enqueue_news(items,memory_root=target/"memory")
        observation=replica.write_news_observation(replica.get_event(eid,purpose="audit"),items[0])
        assert not observation.get("audit_only") and replica.get_event(eid)
        sync_public_events(source,target,{"reuters.com"})
        assert replica.get_event(eid) and replica.get_sources(eid)[0]["reporting_evidence"]["kind"]=="controlled_adapter_report"
    finally:replica.conn.close();writer.conn.close()
