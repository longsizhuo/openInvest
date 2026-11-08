"""Reconcile completed decisions and retry pending notification channels."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

from openinvest.core.evidence_eligibility import digest
from openinvest.core.memory_store import MemoryStore
from openinvest.services.notification_outbox import NotificationOutbox


from openinvest.services.analysis_dispatch import dispatch_followups


def reconcile_decisions(store, outbox, *, now):
    queued = 0
    def activation(previous):
        since=previous or (now-timedelta(minutes=5)).isoformat()
        return since,since
    since=datetime.fromisoformat(store.state_update("notification_activation",activation))
    for path in (store.root / ".evidence/decisions").glob("*.json"):
        try:
            record = json.loads(path.read_text())
            p = record["payload"]
            if record["sha256"] != digest(p) or p.get("origin") != "prospective":
                continue
            recorded = datetime.fromisoformat(p["recorded_at"])
            if not max(since,now-timedelta(days=1)) <= recorded <= now:
                continue
            # The decision is committed before its transcript; wait for completion.
            transcript = store.root / ".evidence/transcripts" / (p["decision_id"] + ".json")
            if not transcript.exists():
                continue
            sealed = json.loads(transcript.read_text())
            if sealed["sha256"] != digest(sealed["payload"]):
                continue
            final = p["validated_proposal"]
            verdict = str(final.get("verdict") or "UNCLEAR").upper()
            quality = final.get("decision_status") in ("DATA_UNTRUSTED","REVIEW_REQUIRED") or verdict == "UNCLEAR"
            if verdict == "HOLD" and not quality:
                continue
            from openinvest.core.decision_evidence import current_decision
            current = current_decision(store.root,{"decision_id":p["decision_id"]},p["symbol"])
            quality = quality or not current
            symbol = p["symbol"]
            message = (f"{symbol} 委员会输入/执行未通过核验，当前没有可执行建议。" if quality else
                       f"{symbol} 委员会结果：{verdict}。这是建议，不代表已下单。")
            reason = final.get("hold_reason") if quality else (p.get("raw_proposal") or {}).get("cio_memo")
            plain = f"{message}\n\n分析记录：{recorded.isoformat()}\n{str(reason or '')[:3500]}\n\n决策编号：{p['decision_id']}"
            from openinvest.services.notifier import render_markdown_email
            payload = {"subject": f"openInvest {'质量提醒' if quality else '委员会结果'} — {symbol}",
                       "plain": plain, "html": render_markdown_email(plain, footer_label="Investment Committee"),
                       "discord": plain[:1800], "decision_id": p["decision_id"], "context_id": p.get("context_id"), "symbol":symbol, "decision_quality":quality}
            outbox.enqueue("decision:"+p["decision_id"], payload, category="decision_quality" if quality else "committee_result",
                           policy="decision", now=recorded)
            queued += 1
        except (OSError,ValueError,KeyError,TypeError) as exc:
            store.state_update("notification_reconcile_errors",lambda old,k=path.name,v=type(exc).__name__: ({**(old or {}),k:v},None))
            continue
    return queued


def run(*, store=None, now=None):
    store = store or MemoryStore()
    if (store.data_root / "integrity/restore_pending.json").exists():
        return {"status": "blocked", "reason": "restore_pending"}
    outbox = NotificationOutbox(store)
    now = now or datetime.now(timezone.utc)
    from openinvest.services.email_budget import queue_daily_fallback
    queue_daily_fallback(outbox, now=now)
    try:
        queued = reconcile_decisions(store, outbox, now=now)
    finally:
        receipts = outbox.flush(now=now)
    dispatch_followups(outbox, now=now)
    counts=outbox.status_counts()
    return {"status": "ok", "eligible_decisions": queued, "channel_states":counts,
            "processed": len(receipts), "accepted_by_channel": sum(r["accepted"] for r in receipts),
            "accepted_notifications":sum(r["accepted"] for r in receipts),
            "accepted_channels":sum(c["status"]=="accepted" for r in receipts for c in r["channels"].values()),
            "pending_channels": sum(v for k,v in counts.items() if k not in ("accepted","expired","suppressed","disabled"))}
