from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import json

import pytest

from openinvest.db.event_store import EventStore
from openinvest.services.event_trigger import is_triggerable


@pytest.fixture
def store(tmp_path):
    database=EventStore(str(tmp_path/"db/events.db"),embedding_dim=4)
    yield database
    database.conn.close()


def event(**changes):
    return {"one_line_claim":"Synthetic company reported a risk", "stance":"risk","severity":"high",
            "event_type":"earnings","affected_symbols":["SYNTHETIC"],"entities":["synthetic"],
            "ts":datetime.now(timezone.utc).isoformat(),**changes}


def source(title):
    return SimpleNamespace(url="https://example.invalid/article",src_name="test",title=title,
                           snippet=title,fetched_at=datetime.now(timezone.utc).isoformat())


def test_same_url_correction_updates_current_view_and_preserves_previous_snapshot(store):
    first=store.write_news_observation(event(),source("original report"))
    anchor=datetime.now(timezone.utc)
    changed=store.write_news_observation(event(one_line_claim="Original report was corrected",stance="neutral",severity="low"),source("correction"))
    assert changed["event"]["event_id"]==first["event"]["event_id"]
    assert changed["event"]["one_line_claim"]=="Original report was corrected"
    assert changed["event"]["severity"]=="low"
    assert store.get_sources(first["event"]["event_id"])[0]["title"]=="correction"
    old=store.recall("SYNTHETIC",as_of=anchor,min_severity="low")
    assert old[0]["one_line_claim"]==first["event"]["one_line_claim"]
    assert old[0]["sources"][0]["title"]=="original report"
    assert is_triggerable({**changed["event"],"revision_previous":changed["previous"]},["synthetic"],"mid")


def test_exact_content_retry_does_not_create_revision(store):
    raw=source("unchanged")
    first=store.write_news_observation(event(),raw)
    count=store.conn.execute("SELECT COUNT(*) FROM event_versions").fetchone()[0]
    second=store.write_news_observation(event(),raw)
    assert not second["changed"]
    assert store.conn.execute("SELECT COUNT(*) FROM event_versions").fetchone()[0]==count
    assert first["event"]["event_id"]==second["event"]["event_id"]


def test_revoked_event_revision_is_audit_only_and_cannot_reenter_trigger(tmp_path,monkeypatch):
    from openinvest.core.evidence_eligibility import Eligibility
    from openinvest.services.event_ingest import ingest_events
    monkeypatch.setattr("openinvest.db.event_store.DB_PATH",str(tmp_path/"db/events.db"))
    monkeypatch.setattr("openinvest.core.memory_store.MEMORY_ROOT",tmp_path/"memory")
    captured=[]
    monkeypatch.setattr("openinvest.services.event_trigger.trigger_for_new_events",lambda events,**kw:captured.extend(events) or {"committee_task_id":None})
    base={"title":"original","url":"https://example.invalid/revoked","one_line_claim":"Synthetic warning",
          "stance":"risk","severity":"high","event_type":"earnings","affected_symbols":["SYNTHETIC"]}
    first=ingest_events([base]);eid=first["events"][0]["event_id"]
    Eligibility(tmp_path).revoke("event:"+eid,"synthetic withdrawal")
    captured.clear()
    result=ingest_events([{**base,"title":"updated","one_line_claim":"Synthetic warning revised"}])
    assert result["status"]=="partial" and result["audit_only_event_ids"]==[eid]
    assert result["events"]==[] and captured==[]
    database=EventStore(str(tmp_path/"db/events.db"))
    assert database.get_event(eid) is None
    assert database.get_event(eid,purpose="audit")["one_line_claim"]=="Synthetic warning revised"
    database.conn.close()


def test_repeated_statement_on_different_days_is_not_an_old_invisible_event(store):
    first=store.write_news_observation(event(ts=(datetime.now(timezone.utc)-timedelta(days=30)).isoformat()))
    second=store.write_news_observation(event())
    assert first["event"]["event_id"]!=second["event"]["event_id"]
    assert second["event"]["event_id"] in {r["event_id"] for r in store.recall("SYNTHETIC")}


def test_agent_same_url_severity_escalation_reaches_shared_trigger(tmp_path,monkeypatch):
    from openinvest.services.event_ingest import ingest_events
    monkeypatch.setattr("openinvest.db.event_store.DB_PATH",str(tmp_path/"db/events.db"))
    monkeypatch.setattr("openinvest.core.memory_store.MEMORY_ROOT",tmp_path/"memory")
    captured=[]
    monkeypatch.setattr("openinvest.services.event_trigger.trigger_for_new_events",lambda events,**kw:captured.extend(events) or {"committee_task_id":None})
    base={"title":"uncertain report","url":"https://example.invalid/a","one_line_claim":"Synthetic warning",
          "stance":"neutral","severity":"low","event_type":"earnings","affected_symbols":["SYNTHETIC"]}
    ingest_events([base]);captured.clear()
    out=ingest_events([{**base,"title":"confirmed risk","stance":"risk","severity":"high"}])
    assert out["ingested"]==1
    assert captured[0]["severity"]=="high" and captured[0]["revision_previous"]["severity"]=="low"


def test_corroborating_new_url_does_not_become_a_cooldown_bypassing_revision(store):
    original=store.write_news_observation(event(),source("report"))
    another=source("report");another.url="https://another.invalid/report"
    corroboration=store.write_news_observation(event(),another)
    assert corroboration["event"]["event_id"]==original["event"]["event_id"]
    assert not corroboration["changed"] and corroboration["source_added"]
    assert len(store.get_sources(original["event"]["event_id"]))==2


def test_removed_symbol_still_receives_correction_route(store):
    initial=store.write_news_observation(event(affected_symbols=["A","B"]),source("report"))
    correction=store.write_news_observation(event(affected_symbols=["B"],stance="neutral",severity="low"),source("corrected"))
    assert correction["event"]["affected_symbols"]==["B"]
    assert is_triggerable({**correction["event"],"revision_previous":initial["event"]},["a"],"mid")


def test_legacy_preimage_is_saved_before_first_revision_without_backdating(store):
    old=store.write_news_observation(event(),source("original"))
    store.conn.execute("DELETE FROM event_versions");store.conn.commit()
    store.write_news_observation(event(one_line_claim="corrected",stance="neutral",severity="low"),source("corrected"))
    versions=[json.loads(row[0]) for row in store.conn.execute("SELECT payload_json FROM event_versions ORDER BY id")]
    assert versions[0]["one_line_claim"]==old["event"]["one_line_claim"]
    assert versions[0]["sources"][0]["title"]=="original"
    assert versions[0]["vintage"]["legacy_history_unknown"] is True
    assert versions[-1]["one_line_claim"]=="corrected" and versions[-1]["sources"][0]["title"]=="corrected"


def test_late_processing_of_old_download_cannot_undo_newer_correction(store):
    first=source("original")
    store.write_news_observation(event(),first)
    store.write_news_observation(event(one_line_claim="corrected",stance="neutral",severity="low"),source("corrected"))
    old=store.write_news_observation(event(),first)
    assert not old["changed"] and old["ignored"]=="older_source_observation"
    assert old["event"]["one_line_claim"]=="corrected"


def test_delayed_copy_from_another_url_cannot_revive_corrected_risk(store):
    original=source("original risk article")
    first=store.write_news_observation(event(),original)
    store.write_news_observation(event(one_line_claim="Original report was incorrect",stance="neutral",severity="low"),source("correction"))
    delayed=source("old syndicated copy");delayed.url="https://another.invalid/copy"
    delayed.fetched_at=original.fetched_at
    conflict=store.write_news_observation(event(),delayed)
    assert conflict["audit_only"] and conflict["source_conflict"] and conflict["event"] is None
    current=store.get_event(first["event"]["event_id"])
    assert current["one_line_claim"]=="Original report was incorrect"
    assert current["stance"]=="neutral" and current["severity"]=="low"
    assert store.conn.execute("SELECT COUNT(*) FROM news_observation_conflicts WHERE status='review_required'").fetchone()[0]==1


def test_delayed_copy_cannot_reverse_label_only_correction_of_same_claim(store):
    original=source("original risk label")
    first=store.write_news_observation(event(),original)
    store.write_news_observation(event(stance="neutral",severity="low"),source("label correction"))
    delayed=source("old syndicated risk label");delayed.url="https://another.invalid/copy"
    delayed.fetched_at=original.fetched_at
    result=store.write_news_observation(event(),delayed)
    assert result["source_conflict"] and result["event"] is None
    current=store.get_event(first["event"]["event_id"])
    assert current["stance"]=="neutral" and current["severity"]=="low"


def test_publication_metadata_only_revision_preserves_each_reported_time(store):
    original=source("same content");original.published_at="2026-10-01T01:00:00Z"
    first=store.write_news_observation(event(),original)
    later=source("same content");later.published_at="2026-10-01T02:00:00Z"
    revised=store.write_news_observation(event(),later)
    assert revised["changed"]
    versions=[json.loads(row[0]) for row in store.conn.execute("SELECT payload_json FROM event_versions ORDER BY id")]
    assert versions[0]["vintage"]["observation_published_at"]==original.published_at
    assert versions[-1]["vintage"]["observation_published_at"]==later.published_at
    assert versions[-1]["vintage"]["published_at"]==original.published_at
    assert store.get_sources(first["event"]["event_id"])[0]["published_at"]==later.published_at


def test_live_recall_is_one_database_snapshot_across_process_connections(store,monkeypatch):
    first=store.write_news_observation(event(),source("original source"))
    writer=EventStore(str(store.data_root/"db/events.db"),embedding_dim=4)
    original=store.get_sources
    changed=False
    def sources_with_concurrent_correction(eid):
        nonlocal changed
        if not changed:
            changed=True
            writer.write_news_observation(event(one_line_claim="Corrected report",stance="neutral",severity="low"),source("corrected source"))
        return original(eid)
    monkeypatch.setattr(store,"get_sources",sources_with_concurrent_correction)
    observed=store.recall("SYNTHETIC",min_severity="low")[0]
    assert observed["one_line_claim"]==first["event"]["one_line_claim"]
    assert observed["sources"][0]["title"]=="original source"
    writer.conn.close()
