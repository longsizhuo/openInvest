"""New material facts are distinct; paraphrases, fetch dates and unknown receipts are not."""
from datetime import datetime, timedelta, timezone
import hashlib
import json

import pytest

from openinvest.core.config import set_config_override
from openinvest.core.memory_store import MemoryStore
from openinvest.services import email_budget
from openinvest.services.notification_outbox import NotificationOutbox
from openinvest.jobs import price_sentinel
from tests.test_price_sentinel import pipeline


NOW = datetime(2026,11,9,12,tzinfo=timezone.utc)


def news(body="Original observed publication",**extra):
    return {"event_id":"synthetic","stance":"risk","severity":"high","one_line_claim":"Model summary",
        "sources":[{"url":"https://example.invalid/live-story","title":"Source title","snippet":body}],
        "_analysis_cause_id":"source-body:"+body,"_analysis_version_id":hashlib.sha256(body.encode()).hexdigest(),**extra}


def test_news_identity_changes_for_original_body_not_model_words_or_acquisition_dates():
    first = news()
    same = {**first,"one_line_claim":"A different model paraphrase","_version_id":999,
        "ts":(NOW+timedelta(hours=1)).isoformat()}
    same["sources"] = [{**first["sources"][0],"fetched_at":NOW.isoformat(),"published_at":"unverified newer estimate"}]
    identity = email_budget.urgent_identity({"events":[first]})
    assert identity == email_budget.urgent_identity({"events":[same]})
    assert identity != email_budget.urgent_identity({"events":[news("New observed material publication")]})


def test_retraction_of_prior_high_risk_is_distinct_even_when_current_severity_is_low():
    original = news()
    correction = news("Corrected source publication",stance="neutral",severity="low",
                      revision_previous={"stance":"risk","severity":"high"})
    key = email_budget.urgent_identity({"events":[correction]})
    assert key and key != email_budget.urgent_identity({"events":[original]})
    assert key == email_budget.urgent_identity({"events":[{**correction,"one_line_claim":"Same correction, new wording"}]})


def test_aliases_of_one_observed_publication_have_one_notification_identity():
    first = news()
    alias = {**first,'event_id':'another-normalized-alias','one_line_claim':'Different summary'}
    assert email_budget.urgent_identity({'events':[first]}) == email_budget.urgent_identity({'events':[first,alias]})


def test_material_identity_does_not_expand_the_four_urgent_plus_one_daily_budget(tmp_path):
    set_config_override({"event":{"email_mode":"daily_and_risk","email_daily_cap":5}})
    store = MemoryStore(tmp_path/"memory")
    for number in range(4):
        _, receipt = email_budget.reserve(receiver="owner@example.invalid",subject=str(number),plain_body="synthetic",
            notification_id=str(number),category="market_risk",urgency_key=email_budget.urgent_identity({"events":[news(str(number))]}),
            store=store,now=NOW)
        email_budget.finish(store,receipt,"accepted")
    with pytest.raises(email_budget.EmailDeferred):
        email_budget.reserve(receiver="owner@example.invalid",subject="fifth",plain_body="synthetic",notification_id="fifth",
            category="market_risk",urgency_key=email_budget.urgent_identity({"events":[news("fifth")]}),store=store,now=NOW)
    assert email_budget.reserve(receiver="owner@example.invalid",subject="daily",plain_body="synthetic",
        notification_id="daily",category="daily_report",report_date=NOW.astimezone().date().isoformat(),store=store,now=NOW)[1]


def hit(horizons):
    return {"direction":"down","basis":"prior_session_returns","severity":"mid",
            "observations":[{"horizon_sessions":value} for value in horizons]}


def test_new_five_session_breach_is_not_suppressed_by_a_same_day_one_session_notice(tmp_path):
    box = NotificationOutbox(MemoryStore(tmp_path/"memory"))
    key1, condition1 = price_sentinel._price_notice_identity(box,"SYNTH",hit([1]),"2026-11-09",NOW)
    box.enqueue(key1,{"subject":"synthetic","plain":"facts","html":"facts","discord":"facts","cooldown":condition1},
                category="market_risk",policy="decision",now=NOW)
    key2, condition2 = price_sentinel._price_notice_identity(box,"SYNTH",hit([1,5]),"2026-11-09",NOW+timedelta(minutes=5))
    assert key1 != key2 and condition1["domain"] != condition2["domain"]
    again = price_sentinel._price_notice_identity(box,"SYNTH",hit([1,5]),"2026-11-09",NOW+timedelta(minutes=10))
    assert again == (key2,condition2)


@pytest.mark.parametrize("phase",["accepted","unknown"])
def test_legacy_notice_identity_and_unconfirmed_delivery_are_preserved(tmp_path,phase):
    box = NotificationOutbox(MemoryStore(tmp_path/"memory"))
    legacy = "market-observation:SYNTH:down:prior_session_returns:mid"
    key = legacy+":2026-11-09"
    ident = box.enqueue(key,{"subject":"synthetic","plain":"facts","html":"facts","discord":"facts",
        "events":[{"one_line_claim":"SYNTH 1交易日累计 -2%; observed"}],
        "cooldown":{"domain":legacy,"seconds":7200}},category="market_risk",policy="decision",now=NOW)
    with box.connection() as db:
        db.execute("UPDATE deliveries SET status=? WHERE notification_id=?", (phase,ident))
    same_key, same = price_sentinel._price_notice_identity(box,"SYNTH",hit([1]),"2026-11-09",NOW+timedelta(minutes=1))
    assert same_key == key and same["domain"] == legacy
    same_id = box.enqueue(same_key,{"subject":"changed model words","plain":"x","html":"x","discord":"x"},
                          category="market_risk",policy="decision",now=NOW)
    assert same_id == ident and all(row["status"] == phase for row in box.receipt(ident)["channels"].values())
    different_key, different = price_sentinel._price_notice_identity(box,"SYNTH",hit([1,5]),"2026-11-09",NOW+timedelta(minutes=2))
    assert different_key != key and different["domain"] != legacy


def test_delivery_cooldown_keeps_a_new_observation_in_audit_without_a_new_notice(pipeline):
    import sqlite3
    first = price_sentinel.run(dry_run=False)
    pipeline.deliver()
    path = pipeline.store.data_root/"db/source_snapshots.sqlite"
    with sqlite3.connect(path) as db:
        before = db.execute("SELECT count(*) FROM backend_observations").fetchone()[0]
    pipeline.now += timedelta(minutes=5)
    pipeline.frames.update(closes=[100,100.6,102],price=102,last_bar_utc=pipeline.now-timedelta(minutes=5))
    second = price_sentinel.run(dry_run=False)
    with sqlite3.connect(path) as db:
        after = db.execute("SELECT count(*) FROM backend_observations").fetchone()[0]
    assert first["notifications_queued"] == 1 and second["notifications_queued"] == 0
    assert second["detected"] == 1 and after > before


def test_same_queued_backend_observation_does_not_create_new_event_versions(pipeline):
    from openinvest.db.event_store import EventStore
    first = price_sentinel.run(dry_run=False)
    reader = EventStore(str(pipeline.store.data_root/"db/events.db"))
    try:
        versions = reader.conn.execute("SELECT count(*) FROM event_versions").fetchone()[0]
        assert versions > 0
        price_sentinel.run(dry_run=False)
        assert reader.conn.execute("SELECT count(*) FROM event_versions").fetchone()[0] == versions
    finally:
        reader.conn.close()


@pytest.mark.parametrize("difference",["body","permission_parent","fetch_time_only"])
def test_collection_keeps_changed_text_and_independent_permission_parents(monkeypatch,difference):
    from openinvest.services.news_sources import RawNewsItem,fetch_all
    first = RawNewsItem(src_name="provider",url="https://example.invalid/article",title="Original title",
        snippet="Original observed body",raw_meta={"collector_capture_id":"first"} if difference=="permission_parent" else {})
    second = RawNewsItem(src_name="provider",url=first.url,title=first.title,
        snippet="Changed original body" if difference=="body" else first.snippet,
        fetched_at=(NOW+timedelta(hours=1)).isoformat(),
        raw_meta={"collector_capture_id":"second"} if difference=="permission_parent" else {})
    monkeypatch.setenv("SEARXNG_URL","")
    monkeypatch.setattr("openinvest.services.news_sources.ddgs_news.fetch_ddgs_news",lambda **kwargs:[first])
    monkeypatch.setattr("openinvest.services.news_sources.yfinance_news.fetch_yfinance_news",lambda **kwargs:[second])
    result = fetch_all(queries=["SYNTH"],symbols=["SYNTH"])
    assert len(result) == (1 if difference=="fetch_time_only" else 2)
