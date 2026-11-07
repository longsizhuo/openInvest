"""Collection failures, source-watermark recovery and late inserts are observable."""
from datetime import datetime,timedelta,timezone
import json

import pytest

from openinvest.core.memory_store import MemoryStore
from openinvest.services import news_collection as collection
from openinvest.services import agent_queue as q


NOW=datetime(2026,11,7,12,tzinfo=timezone.utc)


@pytest.fixture
def local(tmp_path,monkeypatch):
    store=MemoryStore(tmp_path/"tenant/memory")
    monkeypatch.setattr(collection,"SOURCES",("wscn:test",))
    return store


def page(title,published,*,cursor=None,old=False):
    rows=[{"id":title,"title":title,"content_text":title,"display_time":published.timestamp(),"uri":"/livenews/"+title}]
    if old:rows.append({"id":"boundary","title":"boundary","content_text":"boundary","display_time":(NOW-timedelta(hours=2)).timestamp(),"uri":"/livenews/boundary"})
    return json.dumps({"data":{"items":rows,"next_cursor":cursor}}).encode()


def test_failed_fetch_cannot_certify_a_quiet_window(local):
    result=collection.collect(store=local,since=(NOW-timedelta(hours=1)).isoformat(),now=NOW,
                              fetcher=lambda *args:(_ for _ in ()).throw(OSError("synthetic source unavailable")))
    assert result["status"]=="coverage_incomplete" and not result["quiet_window_certified"]
    assert result["sources"]["wscn:test"]["pending"]
    assert "complete_through" not in result["sources"]["wscn:test"]


def test_explicit_initial_window_is_required_instead_of_last_ingest_time(local):
    calls=[]
    result=collection.collect(store=local,now=NOW,fetcher=lambda *args:calls.append(args))
    assert result["status"]=="coverage_unknown" and calls==[]


def test_unfinished_cursor_resumes_and_old_coverage_does_not_jump_to_now(local):
    calls=[]
    def fetch(source,cursor):
        calls.append(cursor)
        if cursor is None:return "https://source.invalid/feed",page("head",NOW-timedelta(minutes=5),cursor=10)
        if cursor==10:return "https://source.invalid/feed",page("middle",NOW-timedelta(minutes=30),cursor=20)
        return "https://source.invalid/feed",page("tail",NOW-timedelta(hours=2))
    first=collection.collect(store=local,since=(NOW-timedelta(hours=1)).isoformat(),now=NOW,max_pages=2,fetcher=fetch)
    assert first["sources"]["wscn:test"]["next_cursor"]==20
    second=collection.collect(store=local,now=NOW+timedelta(minutes=5),max_pages=3,fetcher=fetch)
    assert calls==[None,10,None,20]
    assert second["sources"]["wscn:test"]["complete_through"]==NOW.isoformat()
    assert second["status"]=="coverage_incomplete"  # Newer unscanned interval still exists.


def test_delayed_publication_inside_overlap_is_queued_after_prior_complete_window(local):
    cutoff=(NOW-timedelta(hours=1)).isoformat()
    collection.collect(store=local,since=cutoff,now=NOW,
        fetcher=lambda *args:("https://source.invalid/feed",page("first",NOW-timedelta(minutes=10),old=True)))
    result=collection.collect(store=local,now=NOW+timedelta(minutes=5),
        fetcher=lambda *args:("https://source.invalid/feed",page("delayed",NOW-timedelta(minutes=45),old=True)))
    assert result["status"]=="ok" and result["queued"]==1
    assert len(q._read("news.jsonl",local.root))==2


def test_durable_candidate_survives_handoff_failure(local,monkeypatch):
    original=q.enqueue_news
    monkeypatch.setattr(q,"enqueue_news",lambda *args,**kwargs:(_ for _ in ()).throw(OSError("synthetic queue outage")))
    with pytest.raises(OSError):
        collection.collect(store=local,since=(NOW-timedelta(hours=1)).isoformat(),now=NOW,
            fetcher=lambda *args:("https://source.invalid/feed",page("needs-handoff",NOW-timedelta(minutes=10),old=True)))
    monkeypatch.setattr(q,"enqueue_news",original)
    result=collection.collect(store=local,now=NOW+timedelta(hours=4),
        fetcher=lambda *args:("https://source.invalid/feed",page("boundary-only",NOW-timedelta(hours=2))))
    assert result["queued"]==1
    assert q._read("news.jsonl",local.root)[0]["title"]=="needs-handoff"


def test_feed_without_per_item_locator_is_preserved_for_verification(local,monkeypatch):
    monkeypatch.setattr(collection,"SOURCES",("sina:152",))
    rows=[{"create_time":"2026-11-07 19:30:00","rich_text":"Original report"},
          {"id":"old","create_time":"2026-11-07 18:00:00","rich_text":"boundary"}]
    result=collection.collect(store=local,since=(NOW-timedelta(hours=1)).isoformat(),now=NOW,
        fetcher=lambda *args:("https://source.invalid/feed",json.dumps({"result":{"data":{"feed":{"list":rows}}}}).encode()))
    assert result["pending_verification"]==1 and result["queued"]==0
    assert not result["quiet_window_certified"]
    import sqlite3
    with sqlite3.connect(local.data_root/"db/source_snapshots.sqlite") as database:
        records=[json.loads(row[0]) for row in database.execute("SELECT payload FROM raw_candidates")]
    assert records[0]["item_id_origin"]=="local_content_hash" and records[0]["url"] is None


def test_restored_old_content_creates_a_new_observation(local):
    values=[]
    for index,title in enumerate(["A","B","A"]):
        body=json.loads(page(title,NOW-timedelta(minutes=10),old=True))
        body["data"]["items"][0]["id"]="same-story"
        body["data"]["items"][0]["uri"]="/livenews/same-story"
        result=collection.collect(store=local,since=(NOW-timedelta(hours=1)).isoformat(),now=NOW+timedelta(minutes=index),
                                  fetcher=lambda *args,body=body:("https://source.invalid/feed",json.dumps(body).encode()))
        values.append(result["queued"])
    assert values==[1,1,1]


def test_unsupported_one_page_budget_is_explicitly_rejected(local):
    with pytest.raises(ValueError,match="at least two pages"):
        collection.collect(store=local,since=(NOW-timedelta(hours=1)).isoformat(),now=NOW,max_pages=1)


def test_received_time_is_recorded_after_response_not_at_window_start(local):
    times=iter([NOW+timedelta(seconds=10),NOW+timedelta(seconds=20)])
    result=collection.collect(store=local,since=(NOW-timedelta(hours=1)).isoformat(),now=NOW,clock=lambda:next(times),
        fetcher=lambda *args:("https://source.invalid/feed",page("source",NOW-timedelta(minutes=10),old=True)))
    row=q._read("news.jsonl",local.root)[0]
    assert row["fetched_at"]==(NOW+timedelta(seconds=20)).isoformat()
    assert row["raw_meta"]["requested_at"]==(NOW+timedelta(seconds=10)).isoformat()
    assert result["sources"]["wscn:test"]["window_end"]==NOW.isoformat()


def test_earliest_v1_pending_candidate_recovers_outside_current_window_without_rewriting_payload(local):
    raw=collection.RawCollection(local.data_root)
    body=page("legacy-needs-handoff",NOW-timedelta(minutes=10))
    receipt={**raw.snapshot("wscn:test","https://source.invalid/feed",body,NOW),"decoder_version":1}
    item=collection.decode_page("wscn:test",body,None,version=1)[0][0]
    original={**item,"fetched_at":NOW.isoformat(),"source_receipt":receipt}
    encoded=json.dumps(original,ensure_ascii=False)
    with raw.db:raw.db.execute("INSERT INTO raw_candidates(id,source,payload,locator_verified) VALUES(?,?,?,1)",("legacy-id","wscn:test",encoded))
    raw.db.close()
    result=collection.collect(store=local,since=(NOW+timedelta(hours=3)).isoformat(),now=NOW+timedelta(hours=4),
        fetcher=lambda *args:("https://source.invalid/feed",page("outside-current-window",NOW-timedelta(hours=2))))
    assert result["queued"]==1
    recovered=q._read("news.jsonl",local.root)[0]
    assert recovered["title"]=="legacy-needs-handoff"
    assert len(recovered["raw_meta"]["source_observation_id"])==64
    raw=collection.RawCollection(local.data_root)
    assert raw.db.execute("SELECT payload FROM raw_candidates WHERE id='legacy-id'").fetchone()[0]==encoded
    assert raw.db.execute("SELECT observation_id FROM raw_observation_aliases WHERE candidate_id='legacy-id'").fetchone()[0]==recovered["raw_meta"]["source_observation_id"]
    raw.db.close()
