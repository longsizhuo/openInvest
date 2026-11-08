"""Dispatch durable analysis causes after a notification attempt, under one budget."""
from __future__ import annotations

import hashlib
import json
import time
import uuid


def current_targets(store):
    strategy = store.read("strategy")
    if strategy is None:
        raise ValueError("strategy_not_available")
    return {row["symbol"] for row in strategy.metadata.get("target_assets", []) if row.get("symbol")}


def _cause_version(event):
    return event.get("_analysis_version_id", event.get("_version_id", 0))


def _cause_id(eid, event):
    return event.get('_analysis_cause_id', eid)


def _defer_inputs(store):
    """Wake early for withdrawal/strategy/budget changes, not every news poll."""
    from openinvest.core.config import load_config
    cfg = load_config(_force_reload=True).event
    value = {"budget": [cfg.committee_daily_cap, cfg.committee_cooldown_hours,
                         cfg.committee_escalation_bypass]}
    for path in (store.root / "strategy.md", store.data_root / "integrity/exclusions.jsonl",
                 store.data_root / 'integrity/eligibility.json'):
        value[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _seed_confirmed_causes(db, now, live_events, index):
    """Old committed per-row submissions can supply dispatch receipts, never completion."""
    rows = db.execute("SELECT notification_id,submitted,submitted_causes FROM followups "
                      "WHERE submitted!='{}' AND submitted_causes!='{}'").fetchall()
    for row in rows:
        submitted, coverage = json.loads(row["submitted"]), json.loads(row["submitted_causes"])
        for symbol, events in coverage.items():
            task = submitted.get(symbol)
            if not isinstance(task, str) or not task:
                continue
            for eid, version in events.items():
                if type(version) is not int:
                    continue
                current = live_events.get(eid) or index.get_event(eid)
                if current is None:
                    continue
                candidate = {'source_event_id':eid, 'source_event_version':str(version),
                    'cause_id':_cause_id(eid, current), 'cause_version':str(_cause_version(current))}
                if not _receipt_source_qualified(index, candidate):
                    continue  # Both the exact saved input and current permission must still qualify.
                db.execute("""INSERT OR IGNORE INTO analysis_causes
                    (symbol,cause_id,cause_version,source_event_id,source_event_version,notification_id,status,task_id,changed)
                    VALUES(?,?,?,?,?,?,'queued',?,?)""",
                    (symbol, _cause_id(eid, current), str(_cause_version(current)), eid, str(version), row["notification_id"], task, now.timestamp()))


def _receipt_source_qualified(index, receipt):
    """A dispatch receipt cannot outlive its original source's permission to use it."""
    import sqlite3
    from openinvest.core.evidence_eligibility import Eligibility
    from openinvest.services.news_identity import analysis_identity
    from openinvest.services.source_reporting import reporting_use
    try:
        eid, version = receipt['source_event_id'], int(receipt['source_event_version'])
        current = index.get_event(eid)
        expected = (receipt['cause_id'], receipt['cause_version'])
        if current is None or (_cause_id(eid, current), str(_cause_version(current))) != expected:
            return False
        row = index.conn.execute('SELECT payload_json FROM event_versions WHERE id=? AND event_id=?',
                                 (version, eid)).fetchone()
        if row is None:
            return False
        original = json.loads(row[0])
        original['_version_id'] = version
        sources = original.get('sources', [])
        denied = Eligibility(index.data_root).excluded_ids()
        if (original.get('event_id') != eid or index._source_excluded(eid, denied, sources=sources)
                or not index._reporting_allowed(original, sources=sources, support=original.get('reporting_support'))):
            return False
        qualified = [source for source in sources
                     if reporting_use(index.data_root, source, policy=index._policy())['allowed']]
        cause_id, cause_version = analysis_identity(original, qualified)
        return (cause_id, str(cause_version)) == expected
    except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error):
        return False


def _claim_causes(outbox, ident, causes, now, live_events, *, event_store=None):
    from openinvest.db.event_store import EventStore
    if not any(causes.values()):
        return {}, {}, {}
    index = event_store or EventStore(str(outbox.store.data_root / 'db/events.db'))
    try:
        return _claim_causes_from_index(outbox, ident, causes, now, live_events, index)
    finally:
        if event_store is None:
            index.conn.close()


def _claim_causes_from_index(outbox, ident, causes, now, live_events, index):
    """Claim exact causes before spending; another row cannot dispatch them concurrently."""
    shared, blocked, owned = {}, {}, {}
    with outbox.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        _seed_confirmed_causes(db, now, live_events, index)
        legacy = db.execute("""SELECT f.notification_id,f.status,n.payload FROM followups f
            JOIN notifications n ON n.id=f.notification_id
            WHERE f.status IN ('unknown','dispatching') AND f.notification_id!=?""", (ident,)).fetchall()
        for symbol, events in causes.items():
            for eid, version in events.items():
                cause_id = _cause_id(eid, live_events[eid])
                key = (symbol, cause_id, str(version))
                for old in legacy:
                    spec = json.loads(old["payload"]).get("followup") or {}
                    # A tracked old version does not block a genuinely new one.
                    tracked = db.execute("SELECT 1 FROM analysis_causes WHERE notification_id=? "
                                         "AND symbol=? AND (source_event_id=? OR cause_id=?) LIMIT 1",
                                         (old["notification_id"], symbol, eid, cause_id)).fetchone()
                    routing = spec.get('event_symbols', {})
                    if (not tracked and symbol in spec.get("symbols", []) and eid in spec.get("event_ids", [])
                            and (not routing or symbol in routing.get(eid, []))):
                        db.execute("""INSERT OR IGNORE INTO analysis_causes
                            (symbol,cause_id,cause_version,source_event_id,source_event_version,notification_id,status,changed)
                            VALUES(?,?,?,?,?,?,?,?)""", (*key, eid, str(live_events[eid].get('_version_id', 0)), old["notification_id"], old["status"], now.timestamp()))
                db.execute("""INSERT OR IGNORE INTO analysis_causes
                    (symbol,cause_id,cause_version,source_event_id,source_event_version,notification_id,status,changed)
                    VALUES(?,?,?,?,?,?,'preparing',?)""", (*key, eid, str(live_events[eid].get('_version_id', 0)), ident, now.timestamp()))
                row = db.execute("SELECT * FROM analysis_causes WHERE symbol=? AND cause_id=? AND cause_version=?",
                                 key).fetchone()
                if row['status'] == 'queued' and not _receipt_source_qualified(index, row):
                    db.execute("UPDATE analysis_causes SET status='verification_required',changed=? "
                               'WHERE symbol=? AND cause_id=? AND cause_version=?', (now.timestamp(), *key))
                    db.execute("UPDATE followups SET status='verification_required',error='shared_dispatch_source_unqualified',"
                               "defer_reason='shared_dispatch_source_unqualified',next_eligible_at=NULL,changed=? "
                               "WHERE notification_id=? AND status='queued'", (now.timestamp(), row['notification_id']))
                    row = db.execute('SELECT * FROM analysis_causes WHERE symbol=? AND cause_id=? AND cause_version=?', key).fetchone()
                holder = db.execute('SELECT status,next_eligible_at,defer_reason FROM followups WHERE notification_id=?',
                                    (row['notification_id'],)).fetchone()
                if (row['status'] == 'preparing' and row['notification_id'] != ident and holder is not None
                        and holder['status'] in ('queued','partially_cancelled','cancelled','revoked')):
                    # Its owner finished another current version without ever
                    # submitting this prepared cause. A live copy can own it now.
                    db.execute('UPDATE analysis_causes SET notification_id=?,source_event_id=?,source_event_version=?,changed=? '
                               'WHERE symbol=? AND cause_id=? AND cause_version=? AND status=\'preparing\'',
                               (ident, eid, str(live_events[eid].get('_version_id', 0)), now.timestamp(), *key))
                    row = db.execute('SELECT * FROM analysis_causes WHERE symbol=? AND cause_id=? AND cause_version=?', key).fetchone()
                if row["status"] == "queued" and row["task_id"]:
                    shared.setdefault(symbol, {})[eid] = {'cause_id':cause_id, 'cause_version':version,
                        'task_id':row['task_id'], 'notification_id':row['notification_id'],
                        'source_event_id':row['source_event_id'], 'source_event_version':row['source_event_version']}
                elif row["status"] == "preparing" and row["notification_id"] == ident:
                    owned.setdefault(symbol, {})[eid] = version
                else:
                    blocked.setdefault(symbol, {})[eid] = {
                        'status':row['status'] if row['status'] in ('preparing','dispatching') else 'unknown',
                        'reason':'shared_dispatch_source_unqualified' if row['status'] == 'verification_required' else None,
                        'next_eligible_at':holder['next_eligible_at'] if holder else None,
                        'defer_reason':holder['defer_reason'] if holder else None}
    return shared, blocked, owned


def _update_followup(db, ident, token, assignments, values=(), *, preparing=False):
    suffix = " AND status='preparing'" if preparing else ''
    return db.execute('UPDATE followups SET ' + assignments
        + ' WHERE notification_id=? AND preparation_token=?' + suffix, (*values, ident, token)).rowcount


def _begin_dispatch(outbox, ident, token, admitted, owned, live_events, reserved, now):
    """Commit every unique cause and the preparation token together, or neither."""
    claims = {}
    for symbol in admitted:
        for eid, version in owned.get(symbol, {}).items():
            key = (symbol, _cause_id(eid, live_events[eid]), str(version))
            claims.setdefault(key, (eid, str(live_events[eid].get('_version_id', 0))))
    with outbox.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        for key, source in claims.items():
            changed = db.execute("UPDATE analysis_causes SET status='dispatching',source_event_id=?,"
                "source_event_version=?,changed=? WHERE symbol=? AND cause_id=? AND cause_version=? "
                "AND notification_id=? AND status='preparing' AND task_id IS NULL",
                (*source, now.timestamp(), *key, ident)).rowcount
            if changed != 1:
                db.rollback()
                return False
        if _update_followup(db, ident, token, "reserved=?,status='dispatching',changed=?,next_eligible_at=NULL,"
                "defer_reason=NULL,defer_inputs_sha256=NULL", (json.dumps(reserved), now.timestamp()), preparing=True) != 1:
            db.rollback()
            return False
    return True


def _defer(outbox, ident, *, token, now, candidates, revisions, fingerprint, blocked=None):
    from openinvest.services.event_trigger import analysis_budget_status
    if blocked:
        holds = [value for values in blocked.values() for value in values.values()]
        uncertain = any(value['status'] == 'unknown' for value in holds)
        state = "verification_required" if uncertain else "pending"
        error = "shared_dispatch_outcome_unknown" if uncertain else "shared_dispatch_in_progress"
        if any(value.get('reason') == 'shared_dispatch_source_unqualified' for value in holds):
            error = 'shared_dispatch_source_unqualified'
        due = None if uncertain else now.timestamp() + 300
        reason = error
        waiting_budget = all(value['status'] == 'preparing'
            and value['defer_reason'] in ('cost_budget','cooldown','cost_budget_disabled')
            and (value['next_eligible_at'] is None or value['next_eligible_at'] > now.timestamp()) for value in holds)
        if not uncertain and waiting_budget:
            moments = [value['next_eligible_at'] for value in holds if value['next_eligible_at'] is not None]
            due = min(moments) if moments else None
            error = 'cooldown_or_cost_budget_deferred'
            reason = 'cost_budget' if any(value['defer_reason'] == 'cost_budget' for value in holds) else holds[0]['defer_reason']
    else:
        budget = analysis_budget_status(outbox.store, candidates, now=now, revisions=revisions)
        state, error = "pending", "cooldown_or_cost_budget_deferred"
        due, reason = budget["next_eligible_at"], budget["defer_reason"]
    with outbox.connection() as db:
        _update_followup(db, ident, token, 'status=?,error=?,changed=?,next_eligible_at=?,defer_reason=?,defer_inputs_sha256=?',
            (state, error, now.timestamp(), due, reason, fingerprint))
    return state


def dispatch_followups(outbox, *, now, only_id=None, event_store=None, max_seconds=45):
    from openinvest.utils.llm import reasoning_by_agent
    from openinvest.services.agent_queue import enqueue_committee
    from openinvest.services.event_trigger import _trigger_committee, reserve_analysis
    from openinvest.core.evidence_eligibility import digest
    results = []
    started = time.monotonic()
    fingerprint = _defer_inputs(outbox.store)
    with outbox.connection() as db:
        db.execute("UPDATE followups SET status='pending',error='preparation_interrupted',changed=? "
                   "WHERE status='preparing' AND changed<?", (now.timestamp(), now.timestamp()-300))
        # There may have been a real model/API submission before interruption.
        db.execute("UPDATE followups SET status='unknown',error='dispatch_interrupted',changed=? "
                   "WHERE status='dispatching' AND changed<?", (now.timestamp(), now.timestamp()-300))
        db.execute("""UPDATE analysis_causes SET status='unknown',changed=?
            WHERE status='dispatching' AND notification_id IN
                (SELECT notification_id FROM followups WHERE status='unknown')""", (now.timestamp(),))
        rows = db.execute("""SELECT n.id,n.payload FROM notifications n JOIN followups f ON f.notification_id=n.id
            WHERE f.status IN ('pending','verification_required') AND (? IS NULL OR n.id=?)
                AND (f.next_eligible_at<=? OR f.defer_inputs_sha256 IS NULL OR f.defer_inputs_sha256!=?)
                AND EXISTS(SELECT 1 FROM deliveries d WHERE d.notification_id=n.id
                    AND (d.attempts>0 OR d.status IN ('disabled','expired')))
            ORDER BY f.attempts,f.changed,n.created LIMIT 20""",
            (only_id, only_id, now.timestamp(), fingerprint)).fetchall()
    for row in rows:
        if time.monotonic()-started >= max_seconds:
            break
        ident = row["id"]
        token = uuid.uuid4().hex
        with outbox.connection() as db:
            if not db.execute("""UPDATE followups SET status='preparing',attempts=attempts+1,changed=?,preparation_token=?
                WHERE notification_id=? AND status IN ('pending','verification_required')
                    AND (next_eligible_at<=? OR defer_inputs_sha256 IS NULL OR defer_inputs_sha256!=?)""",
                (now.timestamp(), token, ident, now.timestamp(), fingerprint)).rowcount:
                continue
        payload = json.loads(row["payload"])
        spec = payload["followup"]
        if not reasoning_by_agent() and payload.get("instance_environment_bound") is not True:
            with outbox.connection() as db:
                _update_followup(db, ident, token, "status='unknown',error='instance_dispatch_unverified',changed=?", (now.timestamp(),))
            continue
        from openinvest.db.event_store import EventStore
        index = event_store
        try:
            if index is None:
                index = EventStore(str(outbox.store.data_root / "db/events.db"))
            for event in payload.get("events", []):
                if index.get_event(event["event_id"], purpose="audit") is None:
                    index.upsert_event(event)
            live_events = {eid: index.get_event(eid) for eid in spec["event_ids"]}
            live_ids = [eid for eid, event in live_events.items() if event is not None]
            from openinvest.core.evidence_eligibility import Eligibility
            denied = Eligibility(outbox.store.data_root).excluded_ids()
            pending_sources = [eid for eid, event in live_events.items()
                               if event is None and not index._source_excluded(eid, denied)]
            targets = current_targets(outbox.store)
        except Exception as exc:
            with outbox.connection() as db:
                _update_followup(db, ident, token, "status='pending',error=?,changed=?,next_eligible_at=NULL,defer_inputs_sha256=NULL",
                    (type(exc).__name__, now.timestamp()))
            continue
        finally:
            if index is not None and event_store is None:
                index.conn.close()
        with outbox.connection() as db:
            prior = db.execute("SELECT submitted,reserved,closed,submitted_causes,shared_causes FROM followups "
                               "WHERE notification_id=?", (ident,)).fetchone()
        submitted, reserved = json.loads(prior["submitted"]), json.loads(prior["reserved"])
        closed, covered = json.loads(prior["closed"]), json.loads(prior["submitted_causes"])
        shared_refs = json.loads(prior['shared_causes'])
        blocked = {}
        event_symbols = spec.get("event_symbols", {})

        def required(symbol):
            return [eid for eid in spec["event_ids"] if not event_symbols or symbol in event_symbols.get(eid, [])]

        def covered_cause(symbol, eid):
            return (eid in covered.get(symbol, {}) and live_events.get(eid) is not None
                    and eid not in blocked.get(symbol, {})
                    and covered[symbol][eid] == live_events[eid].get('_version_id', 0))

        def settled(symbol):
            return (symbol in closed or (bool(required(symbol)) and all(covered_cause(symbol, eid) for eid in required(symbol)))
                    or (not required(symbol) and symbol in submitted))

        if spec["event_ids"] and any(symbol in submitted and symbol not in covered for symbol in spec["symbols"]):
            with outbox.connection() as db:
                _update_followup(db, ident, token, "status='verification_required',error='legacy_submission_coverage_unknown',"
                    "changed=?,next_eligible_at=NULL,defer_inputs_sha256=NULL", (now.timestamp(),))
            continue
        if spec["event_ids"] and not live_ids:
            with outbox.connection() as db:
                _update_followup(db, ident, token, "status=?,error=?,changed=?,next_eligible_at=NULL,defer_inputs_sha256=NULL",
                    ("verification_required" if pending_sources else "revoked",
                     "source_acquisition_verification_pending" if pending_sources else "all_sources_revoked", now.timestamp()))
            continue
        waiting_symbols, ready_causes = set(), {}
        for symbol in spec["symbols"]:
            if symbol in closed:
                continue
            if symbol not in targets:
                closed[symbol] = "target_removed"
            else:
                # Previously reused per-row coverage also needs its original
                # receipt checked when another cause keeps this followup active.
                ready_causes[symbol] = [eid for eid in required(symbol) if eid in live_ids]
                if any(eid in pending_sources for eid in required(symbol)):
                    waiting_symbols.add(symbol)
                if required(symbol) and not ready_causes[symbol] and symbol not in waiting_symbols:
                    closed[symbol] = "sources_revoked"
        causes = {symbol: {eid: _cause_version(live_events[eid]) for eid in ids} for symbol, ids in ready_causes.items()}
        shared, blocked, owned = _claim_causes(outbox, ident, causes, now, live_events, event_store=event_store)
        for symbol, events in shared.items():
            for eid, reference in events.items():
                covered.setdefault(symbol, {})[eid] = live_events[eid].get('_version_id', 0)
                submitted[symbol] = reference['task_id']
                shared_refs.setdefault(symbol, {})[eid] = reference
        remaining = [symbol for symbol in spec["symbols"] if not settled(symbol)
                     and (owned.get(symbol) or not required(symbol)) and symbol in targets]
        valid_reserved = [symbol for symbol in reserved if symbol not in closed and symbol in targets]
        with outbox.connection() as db:
            if not _update_followup(db, ident, token, 'closed=?,submitted=?,submitted_causes=?,reserved=?,shared_causes=?',
                    (json.dumps(closed), json.dumps(submitted), json.dumps(covered), json.dumps(valid_reserved), json.dumps(shared_refs))):
                continue
            db.execute("DELETE FROM analysis_causes WHERE notification_id=? AND status='preparing' AND symbol IN "
                       "(SELECT value FROM json_each(?))", (ident, json.dumps(list(closed))))
        if not remaining:
            if blocked:
                _defer(outbox, ident, token=token, now=now, candidates=[], revisions=(), fingerprint=fingerprint, blocked=blocked)
                continue
            if waiting_symbols:
                state, error = "verification_required", "source_acquisition_verification_pending"
            else:
                state = ("partially_cancelled" if submitted else "cancelled") if closed else "queued" if submitted else "cancelled"
                error = "target_removed_or_sources_revoked" if closed else None
            with outbox.connection() as db:
                _update_followup(db, ident, token, 'status=?,error=?,changed=?,next_eligible_at=NULL,defer_reason=NULL,defer_inputs_sha256=NULL',
                    (state, error, now.timestamp()))
            continue
        ranks = {"low": 1, "mid": 2, "high": 3}
        observed_rank = max((ranks.get(event.get("severity"), 0) for event in live_events.values() if event), default=0)
        candidates = [(symbol, spec.get("severity_by_symbol", {}).get(symbol, observed_rank)) for symbol in remaining]
        cause_key = digest({symbol: owned.get(symbol, {}) for symbol in remaining})
        reusable = [symbol for symbol in valid_reserved if symbol in remaining]
        admitted = reusable or reserve_analysis(outbox.store, candidates, now=now, revisions=spec.get("revisions", []),
            reservation_key="followup:" + ident + ":" + ",".join(sorted(remaining)) + ":" + cause_key)
        if not admitted:
            _defer(outbox, ident, token=token, now=now, candidates=candidates, revisions=spec.get("revisions", []), fingerprint=fingerprint)
            results.append({"committee_symbols": [], "committee_task_id": None, "analysis_status": "deferred"})
            continue
        retained = sorted(set(valid_reserved) | set(admitted))
        if not _begin_dispatch(outbox, ident, token, admitted, owned, live_events, retained, now):
            # Keep the durable budget ticket. A subsequent preparation reads
            # the latest shared receipt; it must never submit this stale view.
            with outbox.connection() as db:
                db.execute("UPDATE followups SET reserved=?,status='pending',error='shared_dispatch_claim_changed',"
                    "changed=?,next_eligible_at=?,defer_reason='shared_dispatch_in_progress',defer_inputs_sha256=NULL "
                    "WHERE notification_id=? AND preparation_token=? AND status IN ('preparing','pending')",
                    (json.dumps(retained), now.timestamp(), now.timestamp(), ident, token))
            results.append({'committee_symbols':[], 'committee_task_id':None, 'analysis_status':'deferred'})
            continue
        fed_ids = sorted({eid for symbol in admitted for eid in owned.get(symbol, {})})
        try:
            if reasoning_by_agent():
                submission_key = digest({symbol: owned.get(symbol, {}) for symbol in admitted})
                task = enqueue_committee(admitted, fed_ids, reason="market_observation_or_revision; prepare fresh current inputs",
                    request_key="notification:" + ident + ":" + ",".join(sorted(admitted)) + ":" + submission_key,
                    memory_root=outbox.store.root)
            else:
                task = _trigger_committee(symbols=admitted, event_ids=fed_ids)
            if not isinstance(task, str) or not task.strip():
                task = None
            if task:
                for symbol in admitted:
                    submitted[symbol] = task
                    covered.setdefault(symbol, {}).update({eid:live_events[eid].get('_version_id', 0)
                                                           for eid in owned.get(symbol, {})})
                if all(settled(symbol) for symbol in spec["symbols"]):
                    state, error = ("partially_cancelled" if closed else "queued"), None
                elif waiting_symbols:
                    state, error = "verification_required", "source_acquisition_verification_pending"
                else:
                    state, error = "pending", "cooldown_or_cost_budget_deferred"
            else:
                state, error = "unknown", "committee_dispatch_not_confirmed"
        except Exception as exc:
            task, state, error = None, "unknown", type(exc).__name__
        retained_reserved = set(valid_reserved) | set(admitted)
        if task:
            retained_reserved.difference_update(admitted)
        with outbox.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for symbol in admitted:
                for eid, version in owned.get(symbol, {}).items():
                    db.execute("UPDATE analysis_causes SET status=?,task_id=?,changed=? "
                               "WHERE symbol=? AND cause_id=? AND cause_version=? AND notification_id=?",
                               ("queued" if task else "unknown", task, now.timestamp(), symbol, _cause_id(eid, live_events[eid]), str(version), ident))
            _update_followup(db, ident, token, 'status=?,task_id=?,error=?,changed=?,submitted=?,submitted_causes=?,reserved=?',
                (state, task, error, now.timestamp(), json.dumps(submitted), json.dumps(covered), json.dumps(sorted(retained_reserved))))
        if state == "pending":
            if blocked:
                _defer(outbox, ident, token=token, now=now, candidates=[], revisions=(), fingerprint=fingerprint, blocked=blocked)
            else:
                waiting = [(symbol, rank) for symbol, rank in candidates if not settled(symbol)]
                _defer(outbox, ident, token=token, now=now, candidates=waiting, revisions=spec.get("revisions", []), fingerprint=fingerprint)
        if task:
            index = event_store or EventStore(str(outbox.store.data_root / "db/events.db"))
            try:
                for eid in fed_ids:
                    index.mark_committee_task(eid, task)
            finally:
                if event_store is None:
                    index.conn.close()
        results.append({"committee_symbols": admitted if task else [], "committee_task_id": task, "analysis_status": state})
    return results
