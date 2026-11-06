from pathlib import Path

from openinvest.core.evidence_eligibility import Eligibility
from openinvest.db.event_store import EventStore
from openinvest.services.event_replication import sync_public_events


def test_replication_filters_private_events_and_honors_revocation(tmp_path):
    source=tmp_path/"source"; target=tmp_path/"target"
    store=EventStore(str(source/"db/events.db"))
    try:
        _, news=store.upsert_event({"one_line_claim":"Synthetic public news", "stance":"risk", "severity":"mid", "affected_symbols":["SYNTHETIC"]})
        store.add_source(news,src_name="synthetic",url="https://www.reuters.com/synthetic-test")
        _, private=store.upsert_event({"one_line_claim":"Private account alarm", "event_type":"price_alert", "stance":"risk", "severity":"mid", "affected_symbols":["SYNTHETIC"]})
        store.add_source(private,src_name="synthetic",url="https://www.reuters.com/synthetic-private")
        before=store.conn.execute("SELECT count(*) FROM events").fetchone()[0]
        result=sync_public_events(source,target,{"reuters.com"})
        assert result["imported_public_events"] == 1
        dst=EventStore(str(target/"db/events.db"))
        try:
            assert dst.get_event(news) and dst.get_event(private) is None
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
