"""Shared dispatch identity, uncertain outcomes and due-time budgeting."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from threading import Event

import pytest

from openinvest.core.config import set_config_override
from openinvest.core.evidence_eligibility import Eligibility
from openinvest.core.memory_store import MemoryStore
from openinvest.db.event_store import EventStore
from openinvest.services import analysis_dispatch as dispatch
from openinvest.services.event_trigger import reserve_analysis
from openinvest.services.notification_outbox import NotificationOutbox
from tests.integrity_helpers import approve_event_reporting


NOW = datetime(2026, 11, 8, 12, tzinfo=timezone.utc)


@pytest.fixture
def context(tmp_path, monkeypatch):
    monkeypatch.setattr("openinvest.paths.INVEST_ROOT", tmp_path)
    monkeypatch.setenv("INVEST_REASONING_BY", "backend")
    store = MemoryStore(tmp_path / "memory")
    store.write("strategy", "strategy", {"target_assets": [{"symbol": "SYNTH_A"}, {"symbol": "SYNTH_B"}]}, "")
    box = NotificationOutbox(store)
    database = EventStore(str(store.data_root / "db/events.db"))
    for eid in ("a", "b"):
        database.upsert_event({"event_id": eid, "one_line_claim": "synthetic " + eid,
            "severity": "high", "stance": "risk", "affected_symbols": ["SYNTH_A"], "ts": NOW.isoformat()})
        approve_event_reporting(database, eid)
    calls = []
    monkeypatch.setattr("openinvest.services.event_trigger._trigger_committee",
        lambda **kwargs: calls.append(kwargs) or "task-" + str(len(calls)))
    yield store, box, database, calls
    database.conn.close()


def enqueue(box, key, ids=("a",), symbol="SYNTH_A"):
    ident = box.enqueue(key, {"subject": "synthetic", "plain": "facts", "html": "facts", "discord": "facts",
        "followup": {"symbols": [symbol], "event_ids": list(ids), "severity_by_symbol": {symbol: 3},
            "revisions": [symbol], "event_symbols": {eid: [symbol] for eid in ids}}},
        category="market_risk", policy="decision", now=NOW)
    box.flush(only_id=ident, now=NOW, sender=lambda *args: True)
    return ident


def state(box, ident):
    with box.connection() as db:
        return dict(db.execute("SELECT * FROM followups WHERE notification_id=?", (ident,)).fetchone())


def same_article(database):
    """Independent qualified acquisitions of identical saved publisher text."""
    for eid, minutes in (("a", 2), ("b", 1)):
        database.add_source(eid, src_name="reviewed-publisher", url="https://example.invalid/shared-article",
            title="Original publisher text", snippet="The retained original source body.",
            raw_text="Original retained publisher body", published_at=(NOW-timedelta(hours=1)).isoformat(),
            fetched_at=(NOW-timedelta(minutes=minutes)).isoformat())
        approve_event_reporting(database, eid)


def metadata_revision(database, eid="a"):
    source = database.get_sources(eid, purpose="audit")[0]
    database.add_source(eid, **{key:source[key] for key in ("src_name", "url", "title", "snippet", "raw_text")},
        published_at=(NOW-timedelta(minutes=59)).isoformat(), fetched_at=(NOW+timedelta(minutes=1)).isoformat())
    approve_event_reporting(database, eid)


def test_same_current_cause_across_followups_dispatches_once_and_new_version_dispatches(context):
    store, box, database, calls = context
    first = enqueue(box, "first")
    dispatch.dispatch_followups(box, now=NOW, event_store=database)
    second = enqueue(box, "duplicate")
    dispatch.dispatch_followups(box, now=NOW+timedelta(minutes=1), event_store=database)
    assert len(calls) == 1 and state(box, first)["status"] == state(box, second)["status"] == "queued"
    assert json.loads(state(box, second)["shared_causes"])["SYNTH_A"]["a"]["notification_id"] == first
    event = database.get_event("a", purpose="audit")
    database.upsert_event({**event, "one_line_claim": "genuine new source change"}, revision=True)
    approve_event_reporting(database, "a")
    third = enqueue(box, "real-revision")
    dispatch.dispatch_followups(box, now=NOW+timedelta(minutes=2), event_store=database)
    assert len(calls) == 2 and state(box, third)["status"] == "queued"
    assert len(store.state_get("event_committee_triggers")["runs"]) == 2
    assert not (store.root / ".agent_queue/committee_completions.jsonl").exists()


def test_overlap_submits_only_the_new_cause(context):
    _, box, database, calls = context
    enqueue(box, "a")
    dispatch.dispatch_followups(box, now=NOW, event_store=database)
    overlap = enqueue(box, "a-and-b", ("a", "b"))
    dispatch.dispatch_followups(box, now=NOW+timedelta(minutes=1), event_store=database)
    assert [call["event_ids"] for call in calls] == [["a"], ["b"]]
    assert set(json.loads(state(box, overlap)["submitted_causes"])["SYNTH_A"]) == {"a", "b"}


def test_inflight_other_followup_waits_for_receipt_and_never_submits_in_parallel(context, monkeypatch):
    _, box, _, calls = context
    entered, release = Event(), Event()
    first = enqueue(box, "first")
    second = enqueue(box, "second")
    def trigger(**kwargs):
        calls.append(kwargs)
        entered.set()
        assert release.wait(5)
        return "confirmed-task"
    monkeypatch.setattr("openinvest.services.event_trigger._trigger_committee", trigger)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(dispatch.dispatch_followups, box, now=NOW, only_id=first)
        assert entered.wait(5)
        try:
            dispatch.dispatch_followups(box, now=NOW, only_id=second)
            assert len(calls) == 1
            assert state(box, second)["defer_reason"] == "shared_dispatch_in_progress"
        finally:
            release.set()
        future.result(timeout=5)
    dispatch.dispatch_followups(box, now=NOW+timedelta(minutes=6), only_id=second)
    assert len(calls) == 1 and state(box, second)["status"] == "queued"


def test_unknown_dispatch_cannot_be_bypassed_by_a_new_notification(context, monkeypatch):
    _, box, database, calls = context
    original = database.get_event
    def material_version(*args, **kwargs):
        event = original(*args, **kwargs)
        return {**event, '_analysis_cause_id':'body-'+str(event['_version_id']),
                '_analysis_version_id':'body-'+str(event['_version_id'])} if event else None
    monkeypatch.setattr(database, 'get_event', material_version)
    def uncertain(**kwargs):
        calls.append(kwargs)
        raise TimeoutError("synthetic uncertain submission")
    monkeypatch.setattr("openinvest.services.event_trigger._trigger_committee", uncertain)
    first = enqueue(box, "first")
    dispatch.dispatch_followups(box, now=NOW, event_store=database)
    second = enqueue(box, "second")
    dispatch.dispatch_followups(box, now=NOW+timedelta(minutes=1), event_store=database)
    dispatch.dispatch_followups(box, now=NOW+timedelta(hours=25), event_store=database)
    assert len(calls) == 1 and state(box, first)["status"] == "unknown"
    assert state(box, second)["status"] == "verification_required"
    assert state(box, second)["error"] == "shared_dispatch_outcome_unknown"
    current = database.get_event("a", purpose="audit")
    database.upsert_event({**current, "one_line_claim": "a genuinely newer source"}, revision=True)
    approve_event_reporting(database, "a")
    third = enqueue(box, "new-version")
    dispatch.dispatch_followups(box, now=NOW+timedelta(hours=25), only_id=third, event_store=database)
    assert len(calls) == 2  # The old uncertain version does not hide a genuinely different cause.


def test_budget_due_skips_source_preparation_and_attempts_then_resumes_at_release(context, monkeypatch):
    store, box, database, calls = context
    set_config_override({"event": {"committee_daily_cap": 1}})
    reserve_analysis(store, [("OTHER", 3)], now=NOW-timedelta(hours=23))
    ident = enqueue(box, "deferred")
    duplicate = enqueue(box, "duplicate-deferred")
    dispatch.dispatch_followups(box, now=NOW, event_store=database)
    before = state(box, ident)
    assert before["defer_reason"] == "cost_budget" and before["next_eligible_at"] == (NOW+timedelta(hours=1)).timestamp()
    duplicate_before = state(box, duplicate)
    assert duplicate_before['defer_reason'] == 'cost_budget' and duplicate_before['next_eligible_at'] == before['next_eligible_at']
    original = database.get_event
    monkeypatch.setattr(database, "get_event", lambda *a, **kw: pytest.fail("source preparation before due"))
    for minute in (1, 5, 59):
        assert dispatch.dispatch_followups(box, now=NOW+timedelta(minutes=minute), event_store=database) == []
    assert state(box, ident)["attempts"] == before["attempts"] and calls == []
    assert state(box, duplicate)['attempts'] == duplicate_before['attempts']
    monkeypatch.setattr(database, "get_event", original)
    dispatch.dispatch_followups(box, now=NOW+timedelta(hours=1), event_store=database)
    assert len(calls) == 1 and state(box, ident)["status"] == "queued"
    assert state(box, duplicate)['status'] == 'queued'


def test_budget_deferral_wakes_for_actual_withdrawal_without_running_a_model(context):
    store, box, database, calls = context
    set_config_override({"event": {"committee_daily_cap": 1}})
    reserve_analysis(store, [("OTHER", 3)], now=NOW)
    ident = enqueue(box, "deferred")
    dispatch.dispatch_followups(box, now=NOW, event_store=database)
    Eligibility(store.data_root).revoke("event:a", "synthetic source withdrawal")
    dispatch.dispatch_followups(box, now=NOW+timedelta(minutes=1), event_store=database)
    assert state(box, ident)["status"] == "revoked" and calls == []


def test_committed_legacy_coverage_migrates_only_the_current_exact_version(context):
    _, box, database, calls = context
    first = enqueue(box, "old")
    version = database.get_event("a")["_version_id"]
    with box.connection() as db:
        db.execute("UPDATE followups SET status='queued',submitted=?,submitted_causes=? WHERE notification_id=?",
            (json.dumps({"SYNTH_A": "old-confirmed-task"}), json.dumps({"SYNTH_A": {"a": version}}), first))
    second = enqueue(box, "new")
    dispatch.dispatch_followups(box, now=NOW, event_store=database)
    assert calls == [] and state(box, second)["status"] == "queued"


def test_material_identity_reuses_content_across_event_ids_without_relabelling_versions(context):
    _, box, database, calls = context
    same_article(database)
    cause_id = database.get_event("a")["_analysis_cause_id"]
    first = enqueue(box, "first", ("a",))
    dispatch.dispatch_followups(box, now=NOW, event_store=database)
    first_version = json.loads(state(box, first)["submitted_causes"])["SYNTH_A"]["a"]
    alias = enqueue(box, "same-source-other-event-id", ("b",))
    dispatch.dispatch_followups(box, now=NOW+timedelta(minutes=1), event_store=database)
    assert len(calls) == 1
    row = state(box, alias)
    assert json.loads(row["submitted_causes"])["SYNTH_A"]["b"] == database.get_event("b")["_version_id"]
    assert json.loads(row["shared_causes"])["SYNTH_A"]["b"]["cause_id"] == cause_id
    metadata_revision(database)
    metadata_only = enqueue(box, "metadata-only-version", ("a",))
    dispatch.dispatch_followups(box, now=NOW+timedelta(minutes=2), event_store=database)
    assert len(calls) == 1
    assert json.loads(state(box, first)["submitted_causes"])["SYNTH_A"]["a"] == first_version
    assert json.loads(state(box, metadata_only)["submitted_causes"])["SYNTH_A"]["a"] == database.get_event("a")["_version_id"]


def test_legacy_receipt_for_another_version_cannot_guess_content_equivalence(context, monkeypatch):
    _, box, database, calls = context
    old_version = database.get_event("a")["_version_id"]
    old = enqueue(box, "legacy")
    with box.connection() as db:
        db.execute("UPDATE followups SET status='queued',submitted=?,submitted_causes=? WHERE notification_id=?",
            (json.dumps({"SYNTH_A": "old-task"}), json.dumps({"SYNTH_A": {"a": old_version}}), old))
    event = database.get_event("a", purpose="audit")
    database.upsert_event({**event, "one_line_claim": "new observed version"}, revision=True)
    approve_event_reporting(database, "a")
    original = database.get_event
    def with_identity(*args, **kwargs):
        event = original(*args, **kwargs)
        return {**event, "_analysis_cause_id": "canonical", "_analysis_version_id": "content"} if event else None
    monkeypatch.setattr(database, "get_event", with_identity)
    fresh = enqueue(box, "fresh")
    dispatch.dispatch_followups(box, now=NOW, only_id=fresh, event_store=database)
    assert len(calls) == 1 and state(box, fresh)["status"] == "queued"
    assert json.loads(state(box, old)["submitted_causes"])["SYNTH_A"]["a"] == old_version


def test_configured_budget_increase_wakes_a_deferred_request_early(context):
    store, box, database, calls = context
    set_config_override({"event": {"committee_daily_cap": 1}})
    reserve_analysis(store, [("OTHER", 3)], now=NOW)
    ident = enqueue(box, "deferred")
    dispatch.dispatch_followups(box, now=NOW, event_store=database)
    assert calls == [] and state(box, ident)["next_eligible_at"] > NOW.timestamp()
    set_config_override({"event": {"committee_daily_cap": 2}})
    dispatch.dispatch_followups(box, now=NOW+timedelta(minutes=1), event_store=database)
    assert len(calls) == 1 and state(box, ident)["status"] == "queued"


def test_recovered_preparation_cannot_leave_the_old_worker_free_to_submit_again(context, monkeypatch):
    from openinvest.services import event_trigger
    store, box, _, calls = context
    ident = enqueue(box, "one-request")
    entered, release = Event(), Event()
    original = event_trigger.reserve_analysis
    reserved = []
    def reserve_and_pause(*args, **kwargs):
        admitted = original(*args, **kwargs)
        reserved.append(admitted)
        if len(reserved) == 1:
            entered.set()
            assert release.wait(5)
        return admitted
    monkeypatch.setattr(event_trigger, "reserve_analysis", reserve_and_pause)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(dispatch.dispatch_followups, box, now=NOW, only_id=ident)
        assert entered.wait(5)
        try:
            dispatch.dispatch_followups(box, now=NOW+timedelta(minutes=6), only_id=ident)
            assert len(calls) == 1
        finally:
            release.set()
        future.result(timeout=5)
    assert len(calls) == 1 and state(box, ident)["status"] == "queued"
    assert len(store.state_get("event_committee_triggers")["runs"]) == 1


def test_unsubmitted_old_content_can_progress_after_its_owner_finishes_a_new_version(context):
    store, box, database, calls = context
    same_article(database)
    set_config_override({"event": {"committee_daily_cap": 1}})
    reserve_analysis(store, [("OTHER", 3)], now=NOW)
    first = enqueue(box, "first-old-content")
    dispatch.dispatch_followups(box, now=NOW, event_store=database)
    database.add_source("a", src_name="reviewed-publisher", url="https://example.invalid/shared-article",
        title="Original publisher text", snippet="Genuinely changed source body", raw_text="A changed original",
        published_at=NOW.isoformat(), fetched_at=NOW.isoformat())
    approve_event_reporting(database, "a")
    set_config_override({"event": {"committee_daily_cap": 2}})
    dispatch.dispatch_followups(box, now=NOW+timedelta(minutes=1), event_store=database)
    assert len(calls) == 1 and state(box, first)["status"] == "queued"
    other = enqueue(box, "independent-old-content-still-current", ("b",))
    dispatch.dispatch_followups(box, now=NOW+timedelta(minutes=2), event_store=database)
    assert state(box, other)["defer_reason"] == "cost_budget", json.dumps(state(box, other))
    dispatch.dispatch_followups(box, now=NOW+timedelta(hours=24), event_store=database)
    assert len(calls) == 2 and calls[-1]["event_ids"] == ["b"]
    assert state(box, other)["status"] == "queued"


@pytest.mark.parametrize("failure", ["event_revoked", "old_source_revoked", "purpose_removed", "snapshot_missing"])
def test_shared_receipt_requires_its_original_event_and_saved_source_version(context, failure):
    from openinvest.services.source_reporting import event_review_id, source_fingerprint
    store, box, database, calls = context
    same_article(database)
    first = enqueue(box, "original")
    dispatch.dispatch_followups(box, now=NOW, only_id=first, event_store=database)
    with box.connection() as db:
        original_receipt = dict(db.execute("SELECT * FROM analysis_causes").fetchone())
    assert original_receipt["task_id"] == "task-1"
    assert database.get_event("a")["_analysis_cause_id"] == database.get_event("b")["_analysis_cause_id"]
    if failure == "event_revoked":
        Eligibility(store.data_root).revoke("event:a", "original receipt parent revoked")
    elif failure == "old_source_revoked":
        old_source = database.get_sources("a", purpose="audit")[0]
        metadata_revision(database)
        Eligibility(store.data_root).revoke("source_report:"+source_fingerprint(old_source), "original snapshot revoked")
        assert database.get_event("a") is not None  # A fresh current head cannot repair the old receipt.
    elif failure == "purpose_removed":
        review = event_review_id(database.get_event("a", purpose="audit"), database.get_sources("a", purpose="audit"))
        path = store.data_root / "integrity/eligibility.json"
        value = json.loads(path.read_text())
        value["artifacts"][review]["allowed_uses"] = ["audit"]
        path.write_text(json.dumps(value))
    else:
        with database.conn:
            database.conn.execute("DELETE FROM event_versions WHERE id=?", (int(original_receipt["source_event_version"]),))
    assert database.get_event("b") is not None
    follower = enqueue(box, "follower", ("b",))
    dispatch.dispatch_followups(box, now=NOW+timedelta(minutes=2), only_id=follower, event_store=database)
    blocked = state(box, follower)
    assert len(calls) == 1 and blocked["status"] == "verification_required"
    assert blocked["error"] == "shared_dispatch_source_unqualified"
    assert json.loads(blocked["submitted"]) == {}
    with box.connection() as db:
        receipt = dict(db.execute("SELECT * FROM analysis_causes WHERE cause_id=? AND cause_version=?",
            (original_receipt["cause_id"], original_receipt["cause_version"])).fetchone())
    assert receipt["status"] == "verification_required"
    assert receipt["task_id"] == original_receipt["task_id"] and receipt["source_event_version"] == original_receipt["source_event_version"]
    assert state(box, first)["status"] == "verification_required"
    dispatch.dispatch_followups(box, now=NOW+timedelta(hours=25), only_id=follower, event_store=database)
    assert len(calls) == 1
    if failure == "purpose_removed":
        value["artifacts"][review]["allowed_uses"] = ["advice"]
        path.write_text(json.dumps(value))
        dispatch.dispatch_followups(box, now=NOW+timedelta(minutes=3), only_id=follower, event_store=database)
        assert len(calls) == 1 and state(box, follower)["status"] == "verification_required"
    database.add_source("b", src_name="reviewed-publisher", url="https://example.invalid/shared-article",
        title="A materially changed publication", snippet="New observed source body", raw_text="New source text",
        published_at=NOW.isoformat(), fetched_at=(NOW+timedelta(minutes=3)).isoformat())
    approve_event_reporting(database, "b")
    fresh = enqueue(box, "new-original-content", ("b",))
    dispatch.dispatch_followups(box, now=NOW+timedelta(minutes=4), only_id=fresh, event_store=database)
    assert len(calls) == 2 and state(box, fresh)["status"] == "queued"


def test_eligibility_purpose_change_without_exclusion_wakes_budget_defer(context):
    from openinvest.services.source_reporting import event_review_id
    store, box, database, calls = context
    set_config_override({"event": {"committee_daily_cap": 1}})
    reserve_analysis(store, [("OTHER", 3)], now=NOW)
    ident = enqueue(box, "deferred")
    dispatch.dispatch_followups(box, now=NOW, event_store=database)
    before = state(box, ident)
    assert before["next_eligible_at"] > NOW.timestamp()
    exclusions = store.data_root / "integrity/exclusions.jsonl"
    original_exclusions = exclusions.read_bytes()
    key = event_review_id(database.get_event("a", purpose="audit"), database.get_sources("a", purpose="audit"))
    path = store.data_root / "integrity/eligibility.json"
    registry = json.loads(path.read_text())
    registry["artifacts"][key]["allowed_uses"] = ["audit"]
    path.write_text(json.dumps(registry))
    dispatch.dispatch_followups(box, now=NOW+timedelta(minutes=1), event_store=database)
    assert exclusions.read_bytes() == original_exclusions
    assert calls == [] and state(box, ident)["status"] == "verification_required"
    assert state(box, ident)["error"] == "source_acquisition_verification_pending"
    assert state(box, ident)["attempts"] == before["attempts"] + 1


def test_target_removal_wakes_budget_defer_before_due(context):
    store, box, database, calls = context
    set_config_override({"event": {"committee_daily_cap": 1}})
    reserve_analysis(store, [("OTHER", 3)], now=NOW)
    ident = enqueue(box, "deferred")
    dispatch.dispatch_followups(box, now=NOW, event_store=database)
    assert state(box, ident)["next_eligible_at"] > NOW.timestamp()
    store.write("strategy", "strategy", {"target_assets": [{"symbol":"SYNTH_B"}]}, "")
    dispatch.dispatch_followups(box, now=NOW+timedelta(minutes=1), event_store=database)
    assert calls == [] and state(box, ident)["status"] == "cancelled"
    assert json.loads(state(box, ident)["closed"]) == {"SYNTH_A":"target_removed"}


def test_partial_followup_rechecks_previously_shared_coverage_before_becoming_settled(context):
    store, box, database, calls = context
    same_article(database)
    first = enqueue(box, "original")
    dispatch.dispatch_followups(box, now=NOW, only_id=first, event_store=database)
    database.upsert_event({"event_id":"c", "one_line_claim":"Independent source pending review",
        "severity":"high", "stance":"risk", "affected_symbols":["SYNTH_B"], "ts":NOW.isoformat()})
    follower = box.enqueue("partial-follower", {"subject":"synthetic", "plain":"facts", "html":"facts", "discord":"facts",
        "followup":{"symbols":["SYNTH_A","SYNTH_B"], "event_ids":["b","c"],
            "severity_by_symbol":{"SYNTH_A":3,"SYNTH_B":3}, "revisions":["SYNTH_A","SYNTH_B"],
            "event_symbols":{"b":["SYNTH_A"],"c":["SYNTH_B"]}}},
        category="market_risk", policy="decision", now=NOW)
    box.flush(only_id=follower, now=NOW, sender=lambda *args:True)
    dispatch.dispatch_followups(box, now=NOW+timedelta(minutes=1), only_id=follower, event_store=database)
    assert len(calls) == 1 and json.loads(state(box, follower)["submitted"])["SYNTH_A"] == "task-1"
    Eligibility(store.data_root).revoke("event:a", "previously reused receipt revoked")
    approve_event_reporting(database, "c")
    dispatch.dispatch_followups(box, now=NOW+timedelta(minutes=2), only_id=follower, event_store=database)
    assert [call["symbols"] for call in calls] == [["SYNTH_A"], ["SYNTH_B"]]
    assert state(box, follower)["status"] == "verification_required"
    assert state(box, follower)["error"] == "shared_dispatch_source_unqualified"


def test_old_confirmed_integer_version_with_identical_saved_text_seeds_before_budget(context):
    store, box, database, calls = context
    same_article(database)
    original_version = database.get_event("a")["_version_id"]
    original = enqueue(box, "legacy-confirmed")
    with box.connection() as db:
        db.execute("UPDATE followups SET status='queued',submitted=?,submitted_causes=? WHERE notification_id=?",
            (json.dumps({"SYNTH_A":"legacy-confirmed-task"}),
             json.dumps({"SYNTH_A":{"a":original_version}}), original))
    metadata_revision(database)
    assert database.get_event("a")["_version_id"] != original_version
    follower = enqueue(box, "current-alias", ("b",))
    dispatch.dispatch_followups(box, now=NOW+timedelta(minutes=2), only_id=follower, event_store=database)
    assert calls == [] and state(box, follower)["status"] == "queued"
    reference = json.loads(state(box, follower)["shared_causes"])["SYNTH_A"]["b"]
    assert reference["task_id"] == "legacy-confirmed-task" and reference["source_event_version"] == str(original_version)
    assert not store.state_get("event_committee_triggers")


@pytest.mark.parametrize("stale_preparation_still_matches", [False, True])
def test_real_bootstrap_between_reservation_and_submit_prevents_a_second_dispatch(context, monkeypatch, stale_preparation_still_matches):
    from openinvest.services import event_trigger
    from openinvest.services.operational_recovery import bind_confirmed_content_dispatches
    store, box, database, calls = context
    same_article(database)
    original_version = database.get_event("a")["_version_id"]
    legacy = enqueue(box, "legacy")
    metadata_revision(database)
    worker = enqueue(box, "worker", ("b",))
    original_reserve = event_trigger.reserve_analysis
    bindings = []
    def bind_after_reserve(*args, **kwargs):
        admitted = original_reserve(*args, **kwargs)
        with box.connection() as db:
            db.execute("UPDATE followups SET status='queued',submitted=?,submitted_causes=? WHERE notification_id=?",
                (json.dumps({"SYNTH_A":"already-confirmed"}), json.dumps({"SYNTH_A":{"a":original_version}}), legacy))
            # The supported bridge only replaces a dormant prepared claim.
            db.execute("UPDATE followups SET status='pending',next_eligible_at=?,defer_reason='cost_budget' WHERE notification_id=?",
                       ((NOW+timedelta(hours=1)).timestamp(), worker))
        bindings.append(bind_confirmed_content_dispatches(store, apply=True, now=NOW))
        if stale_preparation_still_matches:
            # Isolate cause ownership CAS: even a matching old token/status
            # cannot authorize submission after the real bridge took its cause.
            with box.connection() as db:
                db.execute("UPDATE followups SET status='preparing',next_eligible_at=NULL WHERE notification_id=?", (worker,))
        return admitted
    monkeypatch.setattr(event_trigger, "reserve_analysis", bind_after_reserve)
    dispatch.dispatch_followups(box, now=NOW, only_id=worker, event_store=database)
    assert bindings[0]["unique_causes"] == 1 and calls == []
    assert state(box, worker)["status"] == "pending"
    assert json.loads(state(box, worker)["reserved"]) == ["SYNTH_A"]
    assert len(store.state_get("event_committee_triggers")["runs"]) == 1
    dispatch.dispatch_followups(box, now=NOW+timedelta(minutes=1), only_id=worker, event_store=database)
    assert calls == [] and state(box, worker)["status"] == "queued"
    assert json.loads(state(box, worker)["submitted"]) == {"SYNTH_A":"already-confirmed"}


def test_partial_cause_cas_failure_rolls_back_all_other_claims_and_reuses_budget(context, monkeypatch):
    from openinvest.services import event_trigger
    from openinvest.services.operational_recovery import bind_confirmed_content_dispatches
    store, box, database, calls = context
    same_article(database)
    event = database.get_event("b", purpose="audit")
    database.upsert_event({**event, "affected_symbols":["SYNTH_B"]}, revision=True)
    approve_event_reporting(database, "b")
    original_version = database.get_event("b")["_version_id"]
    legacy = enqueue(box, "legacy-b", ("b",), "SYNTH_B")
    metadata_revision(database, "b")
    worker = box.enqueue("two-symbol-worker", {"subject":"synthetic","plain":"facts","html":"facts","discord":"facts",
        "followup":{"symbols":["SYNTH_A","SYNTH_B"],"event_ids":["a","b"],
            "severity_by_symbol":{"SYNTH_A":3,"SYNTH_B":3},"revisions":["SYNTH_A","SYNTH_B"],
            "event_symbols":{"a":["SYNTH_A"],"b":["SYNTH_B"]}}},
        category="market_risk", policy="decision", now=NOW)
    box.flush(only_id=worker, now=NOW, sender=lambda *args:True)
    original_reserve = event_trigger.reserve_analysis
    def bridge(*args, **kwargs):
        admitted = original_reserve(*args, **kwargs)
        with box.connection() as db:
            db.execute("UPDATE followups SET status='queued',submitted=?,submitted_causes=? WHERE notification_id=?",
                (json.dumps({"SYNTH_B":"legacy-b-confirmed"}),json.dumps({"SYNTH_B":{"b":original_version}}),legacy))
            db.execute("UPDATE followups SET status='pending',next_eligible_at=?,defer_reason='cost_budget' WHERE notification_id=?",
                       ((NOW+timedelta(hours=1)).timestamp(),worker))
        assert bind_confirmed_content_dispatches(store, apply=True, now=NOW)["unique_causes"] == 1
        with box.connection() as db:
            db.execute("UPDATE followups SET status='preparing',next_eligible_at=NULL WHERE notification_id=?", (worker,))
        return admitted
    monkeypatch.setattr(event_trigger, "reserve_analysis", bridge)
    dispatch.dispatch_followups(box, now=NOW, only_id=worker, event_store=database)
    assert calls == []
    with box.connection() as db:
        rows = {row["symbol"]:dict(row) for row in db.execute("SELECT * FROM analysis_causes")}
    assert rows["SYNTH_A"]["status"] == "preparing" and rows["SYNTH_A"]["task_id"] is None
    assert rows["SYNTH_B"]["status"] == "queued" and rows["SYNTH_B"]["task_id"] == "legacy-b-confirmed"
    assert set(json.loads(state(box, worker)["reserved"])) == {"SYNTH_A","SYNTH_B"}
    monkeypatch.setattr(event_trigger, "reserve_analysis", original_reserve)
    dispatch.dispatch_followups(box, now=NOW+timedelta(minutes=1), only_id=worker, event_store=database)
    assert [call["symbols"] for call in calls] == [["SYNTH_A"]]
    assert state(box, worker)["status"] == "queued"
    assert len(store.state_get("event_committee_triggers")["runs"]) == 2


def test_two_aliases_in_one_followup_commit_one_canonical_claim(context):
    store, box, database, calls = context
    same_article(database)
    worker = enqueue(box, "same-content-twice", ("a","b"))
    dispatch.dispatch_followups(box, now=NOW, only_id=worker, event_store=database)
    assert len(calls) == 1 and calls[0]["event_ids"] == ["a","b"]
    assert state(box, worker)["status"] == "queued"
    with box.connection() as db:
        rows = db.execute("SELECT status,task_id FROM analysis_causes").fetchall()
    assert [tuple(row) for row in rows] == [("queued","task-1")]
    assert len(store.state_get("event_committee_triggers")["runs"]) == 1
