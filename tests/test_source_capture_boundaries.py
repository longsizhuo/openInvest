"""Exercise real collector code with isolated transport and evidence time bounds."""
from datetime import datetime,timezone,timedelta
import json
from unittest.mock import patch,MagicMock

from openinvest.core.evidence_eligibility import Eligibility
from openinvest.services import agent_queue as q
from openinvest.services.news_sources import fetch_all
from openinvest.services.source_reporting import reporting_use
from tests.test_news_source_snapshots import source_context,captured_sina
from tests.test_source_reporting_policy import row


def test_controlled_adapter_report_requires_the_owned_receipt_and_preserves_asof(source_context,monkeypatch):
    monkeypatch.delenv("SEARXNG_URL",raising=False)
    before=datetime.now(timezone.utc)
    parsed=MagicMock();parsed.entries=[{"title":"Original report","link":"https://publisher.invalid/1","summary":"Source excerpt"}]
    with patch("openinvest.services.news_sources.rss_feed._fetch_feed",return_value=parsed):
        items=fetch_all(rss_feeds=[{"name":"synthetic","url":"https://feed.invalid"}],capture=True,data_root=source_context)
    q.enqueue_news(items);record=q.pending_news_items()[0];source=row(record)
    state=reporting_use(source_context,source)
    assert state["allowed"] and state["kind"]=="controlled_adapter_report"
    assert not reporting_use(source_context,source,as_of=before)["allowed"]
    assert reporting_use(source_context,source,as_of=datetime.now(timezone.utc))["allowed"]
    code=record["raw_meta"]["collector_code_sha256"]
    source["provenance_json"]=json.dumps({"source_snapshot_id":record["source_snapshot_id"]})
    Eligibility(source_context).revoke("collector_version:"+code,"synthetic collector defect")
    assert not reporting_use(source_context,source)["allowed"]
    # Even the canonical input metadata is not the authority for the code hash.
    source["provenance_json"]=json.dumps({"collector_capture_id":record["raw_meta"]["collector_capture_id"]})
    assert not reporting_use(source_context,source)["allowed"]


def test_real_api_body_cannot_be_backdated_by_a_new_queue_payload(source_context,monkeypatch):
    original=captured_sina(monkeypatch)
    from openinvest.services.news_sources import RawNewsItem
    item=RawNewsItem(src_name=original["source"],title=original["title"],url=original["url"],snippet=original["snippet"],
        text=original["text"],published_at=original["published_at"],fetched_at=(datetime.now(timezone.utc)-timedelta(days=30)).isoformat(),
        raw_meta={**original["raw_meta"]})
    item.raw_meta["fetched_at"]=item.fetched_at
    q.enqueue_news([item]);forged=q.pending_news_items()[-1]
    assert not reporting_use(source_context,row(forged))["allowed"]
    assert reporting_use(source_context,row(original))["allowed"]


def test_swallowed_transport_error_is_distinct_from_empty_success(source_context,monkeypatch):
    monkeypatch.delenv("SEARXNG_URL",raising=False)
    errors={};counts={}
    with patch("openinvest.services.news_sources.rss_feed._fetch_feed",side_effect=RuntimeError("synthetic transport failure")):
        assert fetch_all(rss_feeds=[{"name":"broken","url":"https://feed.invalid"}],source_counts=counts,source_errors=errors)==[]
    assert errors=={"rss:broken":["RuntimeError"]} and counts=={"rss:broken":0}
    from openinvest.jobs.event_watch import _record_source_health
    from openinvest.jobs.job_watchdog import find_silent_sources
    from openinvest.core.memory_store import MemoryStore
    _record_source_health(counts,errors=errors)
    health=MemoryStore().state_get("news_source_health")
    assert health["rss:broken"]["status"]=="failed"
    assert find_silent_sources(health)[0]["text"].startswith("[QUALITY_ALERT]")
    _record_source_health(counts,errors={})
    health=MemoryStore().state_get("news_source_health")
    assert health["rss:broken"]["status"]=="empty" and find_silent_sources(health)==[]


def test_feedparser_error_return_is_not_reported_as_empty_success(source_context):
    import feedparser
    import io
    parsed=feedparser.parse(io.BytesIO(b"<rss><channel><broken>"))
    assert parsed.bozo and not parsed.entries
    errors={}
    with patch("openinvest.services.news_sources.rss_feed._fetch_feed",return_value=parsed):
        assert fetch_all(rss_feeds=[{"name":"malformed","url":"https://feed.invalid"}],source_errors=errors)==[]
    assert errors=={"rss:malformed":["FeedParseError"]}


def test_revoked_api_observation_cannot_be_repacked_with_old_decoder_or_missing_parent(source_context,monkeypatch):
    from openinvest.services.news_sources import RawNewsItem
    from openinvest.services.event_ingest import ingest_events
    from tests.test_news_source_snapshots import packet
    original=captured_sina(monkeypatch)
    Eligibility(source_context).revoke("source_observation:"+original["raw_meta"]["source_observation_id"],"synthetic observation invalid")
    assert q.pending_news_items()==[]
    meta={**original["raw_meta"],"decoder_version":1,"locator_projection_version":2};meta.pop("source_observation_id")
    forged=RawNewsItem(src_name=original["source"],title=original["title"],url=original["url"],snippet=original["snippet"],
        text=original["text"],published_at=original["published_at"],fetched_at=original["fetched_at"],raw_meta=meta)
    q.enqueue_news([forged]);candidate=q.pending_news_items()[0]
    assert not reporting_use(source_context,row(candidate))["allowed"]
    assert ingest_events([packet(candidate)])["ingested"]==0
