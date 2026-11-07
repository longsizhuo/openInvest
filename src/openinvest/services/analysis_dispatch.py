"""Dispatch durable analysis causes after a notification attempt, under one budget."""
from __future__ import annotations

import json
import time


def current_targets(store):
    strategy=store.read("strategy")
    if strategy is None:raise ValueError("strategy_not_available")
    return {row["symbol"] for row in strategy.metadata.get("target_assets",[]) if row.get("symbol")}

def dispatch_followups(outbox, *, now, only_id=None, event_store=None,max_seconds=45):
    from openinvest.utils.llm import reasoning_by_agent
    from openinvest.services.agent_queue import enqueue_committee
    from openinvest.services.event_trigger import _trigger_committee
    from openinvest.services.event_trigger import reserve_analysis
    results=[]
    started=time.monotonic()
    with outbox.connection() as db:
        db.execute("UPDATE followups SET status='pending',error='preparation_interrupted',changed=? WHERE status='preparing' AND changed<?",(now.timestamp(),now.timestamp()-300))
        db.execute("UPDATE followups SET status=?,error='dispatch_interrupted',changed=? WHERE status='dispatching' AND changed<?",
                   ("pending" if reasoning_by_agent() else "unknown",now.timestamp(),now.timestamp()-300))
        # Notification expiry stops sending an old message. It does not erase a
        # deferred analysis cause: a fresh committee preparation is still owed.
        rows = db.execute("""SELECT n.id,n.payload FROM notifications n JOIN followups f ON f.notification_id=n.id
            WHERE f.status='pending' AND (? IS NULL OR n.id=?) AND EXISTS(
                SELECT 1 FROM deliveries d WHERE d.notification_id=n.id AND (d.attempts>0 OR d.status IN ('disabled','expired')))
            ORDER BY f.attempts,f.changed,n.created LIMIT 20""", (only_id,only_id)).fetchall()
    for row in rows:
        if time.monotonic()-started>=max_seconds:break
        with outbox.connection() as db:
            if not db.execute("UPDATE followups SET status='preparing',attempts=attempts+1,changed=? WHERE notification_id=? AND status='pending'",(now.timestamp(),row["id"])).rowcount:continue
        payload=json.loads(row["payload"])
        spec = payload["followup"]
        if not reasoning_by_agent() and payload.get("instance_environment_bound") is not True:
            with outbox.connection() as db:
                db.execute("UPDATE followups SET status='unknown',error='instance_dispatch_unverified',changed=? WHERE notification_id=?",(now.timestamp(),row["id"]))
            continue
        # Rebuild a failed event index from the already durable observation.
        from openinvest.db.event_store import EventStore
        index=event_store
        try:
            if index is None:index=EventStore(str(outbox.store.data_root/"db/events.db"))
            for event in payload.get("events",[]):
                if index.get_event(event["event_id"],purpose="audit") is None:
                    index.upsert_event(event)
            # Exclusions can change while a request waits for budget. Never
            # dispatch revoked sources or reconstruct a revoked row as valid.
            live_events={eid:index.get_event(eid) for eid in spec["event_ids"]}
            live_ids=[eid for eid,event in live_events.items() if event is not None]
            targets=current_targets(outbox.store)
        except Exception as exc:
            with outbox.connection() as db:
                db.execute("UPDATE followups SET status='pending',error=?,changed=? WHERE notification_id=?",(type(exc).__name__,now.timestamp(),row["id"]))
            continue
        finally:
            if index is not None and event_store is None:index.conn.close()
        with outbox.connection() as db:
            prior=db.execute("SELECT submitted,reserved,closed FROM followups WHERE notification_id=?",(row["id"],)).fetchone()
            submitted=json.loads(prior["submitted"])
            reserved=json.loads(prior["reserved"])
            closed=json.loads(prior["closed"])
        remaining=[s for s in spec["symbols"] if s not in submitted and s not in closed]
        if spec["event_ids"] and not live_ids:
            with outbox.connection() as db:
                db.execute("UPDATE followups SET status='revoked',error='all_sources_revoked',changed=? WHERE notification_id=?",(now.timestamp(),row["id"]))
            continue
        event_symbols=spec.get("event_symbols",{})
        for symbol in remaining:
            if symbol not in targets:closed[symbol]="target_removed"
            elif event_symbols and not any(symbol in event_symbols.get(eid,[]) for eid in live_ids):closed[symbol]="sources_revoked"
        remaining=[s for s in remaining if s not in closed]
        with outbox.connection() as db:
            db.execute("UPDATE followups SET closed=? WHERE notification_id=?",(json.dumps(closed),row["id"]))
        if not remaining:
            state="partially_cancelled" if submitted else "cancelled"
            with outbox.connection() as db:
                db.execute("UPDATE followups SET status=?,error='target_removed_or_sources_revoked',changed=? WHERE notification_id=?",(state,now.timestamp(),row["id"]))
            continue
        ranks={"low":1,"mid":2,"high":3}
        observed_rank=max((ranks.get(event.get("severity"),0) for event in live_events.values() if event),default=0)
        admitted=[s for s in reserved if s in remaining] if reserved else reserve_analysis(
            outbox.store,[(s,spec.get("severity_by_symbol",{}).get(s,observed_rank)) for s in remaining],
            now=now,revisions=spec.get("revisions",[]),
            reservation_key="followup:"+row["id"]+":"+",".join(sorted(remaining)))
        if not admitted:
            with outbox.connection() as db:
                db.execute("UPDATE followups SET status='pending',error='cooldown_or_cost_budget_deferred',changed=? WHERE notification_id=?",(now.timestamp(),row["id"]))
            results.append({"committee_symbols":[],"committee_task_id":None,"analysis_status":"deferred"})
            continue
        with outbox.connection() as db:
            db.execute("UPDATE followups SET reserved=?,status='dispatching',changed=? WHERE notification_id=?",(json.dumps(admitted),now.timestamp(),row["id"]))
        fed_ids=[eid for eid in live_ids if not event_symbols or set(event_symbols.get(eid,[])) & set(admitted)]
        try:
            if reasoning_by_agent():
                task = enqueue_committee(admitted, fed_ids, reason="market_observation_or_revision; prepare fresh current inputs",
                                         request_key="notification:"+row["id"]+":"+",".join(sorted(admitted)),memory_root=outbox.store.root)
            else:
                task = _trigger_committee(symbols=admitted, event_ids=fed_ids)
            if task:
                submitted.update({s:task for s in admitted})
                settled=set(spec["symbols"])<=submitted.keys()|closed.keys()
                state,error=(("partially_cancelled" if closed else "queued"),None) if settled else ("pending","cooldown_or_cost_budget_deferred")
            else:state,error="unknown","committee_dispatch_not_confirmed"
        except Exception as exc:
            task, state, error = None, "unknown", type(exc).__name__
        with outbox.connection() as db:
            db.execute("UPDATE followups SET status=?,task_id=?,error=?,changed=?,submitted=?,reserved=? WHERE notification_id=?",
                       (state,task,error,now.timestamp(),json.dumps(submitted),json.dumps([] if task else admitted),row["id"]))
        if task:
            index=event_store or EventStore(str(outbox.store.data_root/"db/events.db"))
            try:
                for eid in fed_ids:index.mark_committee_task(eid,task)
            finally:
                if event_store is None:index.conn.close()
        results.append({"committee_symbols":admitted if task else [],"committee_task_id":task,"analysis_status":state})
    return results
