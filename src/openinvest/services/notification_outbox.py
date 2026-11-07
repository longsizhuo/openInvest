"""Durable channel receipts. Detection and channel acceptance are separate events."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from openinvest.core.memory_store import MemoryStore


class NotificationOutbox:
    def __init__(self, store: MemoryStore | None = None):
        self.store = store or MemoryStore()
        self.path = self.store.data_root / "db/notification_outbox.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS notification_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS notifications (
                    id TEXT PRIMARY KEY, category TEXT NOT NULL, policy TEXT NOT NULL,
                    payload TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS deliveries (
                    notification_id TEXT NOT NULL, channel TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                    accepted_at REAL, lease_token TEXT, lease_until REAL, retry_at REAL NOT NULL DEFAULT 0,
                    last_error TEXT, target TEXT, payload_sha256 TEXT, payload_json TEXT,
                    PRIMARY KEY(notification_id, channel));
                CREATE TABLE IF NOT EXISTS followups (
                    notification_id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'pending',
                    task_id TEXT, error TEXT, changed REAL, submitted TEXT NOT NULL DEFAULT '{}',
                    reserved TEXT NOT NULL DEFAULT '[]',closed TEXT NOT NULL DEFAULT '{}',attempts INTEGER NOT NULL DEFAULT 0);
            """)
            columns={r[1] for r in db.execute("PRAGMA table_info(followups)").fetchall()}
            if "submitted" not in columns:
                db.execute("ALTER TABLE followups ADD COLUMN submitted TEXT NOT NULL DEFAULT '{}'")
            if "reserved" not in columns:
                db.execute("ALTER TABLE followups ADD COLUMN reserved TEXT NOT NULL DEFAULT '[]'")
            if "closed" not in columns:
                db.execute("ALTER TABLE followups ADD COLUMN closed TEXT NOT NULL DEFAULT '{}'")
            if "attempts" not in columns:
                db.execute("ALTER TABLE followups ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
            if "submitted_causes" not in columns:
                db.execute("ALTER TABLE followups ADD COLUMN submitted_causes TEXT NOT NULL DEFAULT '{}'")
            db.execute("INSERT OR IGNORE INTO notification_meta VALUES('scope',?)",(uuid.uuid4().hex,))
            self.scope=db.execute("SELECT value FROM notification_meta WHERE key='scope'").fetchone()[0]
        self.path.chmod(0o600)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def enqueue(self, key: str, payload: dict, *, category: str, policy: str, now=None, lifetime_seconds=86400) -> str:
        now = now or datetime.now(timezone.utc)
        ident = hashlib.sha256((self.scope+":"+key).encode()).hexdigest()
        from openinvest.paths import INVEST_ROOT
        payload = {**payload, "notification_id":ident,
                   "instance_environment_bound":self.store.data_root.resolve()==INVEST_ROOT.resolve()}
        with self.connection() as db:
            db.execute("INSERT OR IGNORE INTO notifications VALUES(?,?,?,?,?,?)",
                       (ident, category, policy, json.dumps(payload, ensure_ascii=False, allow_nan=False),
                        now.timestamp(), now.timestamp() + lifetime_seconds))
            for channel in ("discord", "email"):
                db.execute("INSERT OR IGNORE INTO deliveries(notification_id,channel,status) VALUES(?,?,?)",
                           (ident, channel, "pending" if policy_enabled(policy) else "disabled"))
            if payload.get("followup"):
                db.execute("INSERT OR IGNORE INTO followups(notification_id,changed) VALUES(?,?)", (ident, now.timestamp()))
        return ident

    def receipt(self, ident: str) -> dict:
        with self.connection() as db:
            rows = db.execute("SELECT channel,status,attempts,accepted_at,last_error,target FROM deliveries WHERE notification_id=?", (ident,)).fetchall()
        channels = {r["channel"]: dict(r) for r in rows}
        return {"notification_id": ident, "accepted": any(r["status"] == "accepted" for r in rows),
                "channels": channels, "recipient_read_receipt": "unknown"}

    def last_accepted(self, domain: str, *, bar=None):
        with self.connection() as db:
            row=db.execute("""SELECT MAX(d.accepted_at) FROM deliveries d JOIN notifications n ON n.id=d.notification_id
                WHERE d.status='accepted' AND json_extract(n.payload,'$.cooldown.domain')=?
                  AND (? IS NULL OR json_extract(n.payload,'$.cooldown.bar')=?)""", (domain,bar,bar)).fetchone()
        return row[0] if row else None

    def status_counts(self):
        with self.connection() as db:
            return dict(db.execute("SELECT status,COUNT(*) FROM deliveries GROUP BY status").fetchall())

    def recover_discord_receipts(self,*,now,limit=20,max_seconds=20):
        from openinvest.services.discord_notify import lookup_discord_alert_receipt
        from openinvest.paths import INVEST_ROOT
        if self.store.data_root.resolve()!=INVEST_ROOT.resolve():return 0
        recovered=0
        started=time.monotonic()
        with self.connection() as db:
            rows=db.execute("SELECT notification_id,payload_json FROM deliveries WHERE channel='discord' AND status='unknown' AND payload_json IS NOT NULL AND retry_at<=? ORDER BY retry_at,notification_id LIMIT ?",(now.timestamp(),limit)).fetchall()
        for row in rows:
            if time.monotonic()-started>=max_seconds:break
            with self.connection() as db:
                if not db.execute("UPDATE deliveries SET retry_at=? WHERE notification_id=? AND channel='discord' AND status='unknown' AND retry_at<=?",(now.timestamp()+300,row["notification_id"],now.timestamp())).rowcount:continue
            payload=json.loads(row["payload_json"])
            receipt=lookup_discord_alert_receipt(row["notification_id"],payload["discord"])
            if not receipt["accepted"]:continue
            try:
                accepted=datetime.fromisoformat(receipt["accepted_at"]).timestamp()
                if accepted>now.timestamp()+300:continue
            except (TypeError,ValueError,KeyError):continue
            with self.connection() as db:
                recovered+=db.execute("""UPDATE deliveries SET status='accepted',accepted_at=?,target=?,last_error=NULL,
                    lease_token=NULL,lease_until=NULL WHERE notification_id=? AND channel='discord' AND status='unknown'""",
                    (accepted,receipt["target"],row["notification_id"])).rowcount
        return recovered

    def flush(self, *, now=None, limit=20, only_id: str | None = None, sender=None,max_seconds=45) -> list[dict]:
        now = now or datetime.now(timezone.utc)
        started = time.monotonic()
        clock = now.timestamp()
        if (self.store.data_root / "integrity/restore_pending.json").exists():
            return []
        enabled_policies = [p for p in ("events", "risk", "decision") if policy_enabled(p)]
        with self.connection() as db:
            db.execute("""UPDATE deliveries SET status='unknown',last_error='interrupted_delivery_acceptance_unknown'
                WHERE status='sending' AND lease_until<=?""", (clock,))
        if sender is None:self.recover_discord_receipts(now=now)
        with self.connection() as db:
            db.execute("""UPDATE deliveries SET status='expired',last_error='delivery_expired'
                WHERE status NOT IN ('accepted','expired') AND COALESCE(lease_until,0)<=?
                  AND notification_id IN (SELECT id FROM notifications WHERE expires<=?)""", (clock, clock))
            placeholders = ",".join("?" for _ in enabled_policies)
            rows = db.execute(f"""SELECT n.* FROM notifications n WHERE n.policy IN ({placeholders})
                AND (? IS NULL OR n.id=?) AND n.expires>?
                AND EXISTS(SELECT 1 FROM deliveries d WHERE d.notification_id=n.id
                    AND d.status NOT IN ('accepted','expired','unknown','suppressed') AND d.retry_at<=? AND COALESCE(d.lease_until,0)<=?)
                ORDER BY n.created LIMIT ?""", (*enabled_policies, only_id, only_id, clock, clock, clock, limit)).fetchall() if enabled_policies else []
        results = []
        delivery_started=time.monotonic()
        for item in rows:
            if len(results) >= limit or time.monotonic()-delivery_started>=max_seconds:
                break
            ident = item["id"]
            payload = json.loads(item["payload"])
            cooldown = payload.get("cooldown") or {}
            receipt = self.receipt(ident)
            if all(r["status"] in ("accepted", "expired") for r in receipt["channels"].values()):
                continue
            enabled = policy_enabled(item["policy"])
            for channel in ("discord", "email"):
                if time.monotonic()-delivery_started>=max_seconds:break
                clock = now.timestamp() + time.monotonic() - started
                enabled = policy_enabled(item["policy"])
                token = uuid.uuid4().hex
                with self.connection() as db:
                    db.execute("BEGIN IMMEDIATE")
                    if item["expires"] <= clock:
                        db.execute("UPDATE deliveries SET status='expired',last_error='delivery_expired' WHERE notification_id=? AND status!='accepted'", (ident,))
                        continue
                    if not enabled:
                        db.execute("UPDATE deliveries SET status='disabled',last_error='notification_policy_disabled' WHERE notification_id=? AND channel=? AND status!='accepted' AND COALESCE(lease_until,0)<=?", (ident, channel, clock))
                        continue
                    if cooldown.get("domain"):
                        other = db.execute("""SELECT d.status,d.accepted_at,d.lease_until FROM deliveries d
                            JOIN notifications n ON n.id=d.notification_id WHERE n.id!=?
                            AND json_extract(n.payload,'$.cooldown.domain')=?
                            AND (? IS NULL OR json_extract(n.payload,'$.cooldown.bar')=?)
                            AND n.expires>?""", (ident,cooldown["domain"],cooldown.get("bar"),cooldown.get("bar"),clock)).fetchall()
                        delivered = any(r["status"]=="accepted" and (cooldown.get("bar") or clock-r["accepted_at"] < cooldown.get("seconds",0)) for r in other)
                        if delivered:
                            db.execute("UPDATE deliveries SET status='suppressed',last_error='same_condition_already_accepted' WHERE notification_id=? AND status!='accepted'", (ident,))
                            db.execute("UPDATE followups SET status='suppressed',changed=? WHERE notification_id=? AND status='pending'", (clock,ident))
                            continue
                        if any(r["status"]=="unknown" or (r["status"]=="sending" and (r["lease_until"] or 0)>clock) for r in other):
                            continue
                    claimed = db.execute("""UPDATE deliveries SET status='sending', lease_token=?,lease_until=?,attempts=attempts+1
                        WHERE notification_id=? AND channel=? AND status NOT IN ('accepted','expired','unknown','suppressed')
                          AND COALESCE(lease_until,0)<=? AND retry_at<=?""",
                        (token, clock+300, ident, channel, clock, clock)).rowcount
                if not claimed:
                    continue
                status, error = "failed", None
                delivery_payload = validated_delivery_payload(self.store,payload)
                if delivery_payload!=payload:
                    child={key:value for key,value in delivery_payload.items() if key not in ("notification_id","cooldown","followup","events","decision_id","context_id")}
                    child.update(supersedes_notification=ident,decision_quality=True)
                    child_key="supersede:"+ident+":"+hashlib.sha256(json.dumps(child,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
                    replacement=self.enqueue(child_key,child,category="decision_quality",policy="decision",now=now)
                    with self.connection() as db:
                        db.execute("""UPDATE deliveries SET status='suppressed',last_error=?,lease_token=NULL,lease_until=NULL
                            WHERE notification_id=? AND status NOT IN ('accepted','unknown') AND (lease_token=? OR COALESCE(lease_until,0)<=?)""",
                            ("superseded_by:"+replacement,ident,token,clock))
                    break
                serialized = json.dumps(delivery_payload,ensure_ascii=False,sort_keys=True)
                with self.connection() as db:
                    db.execute("UPDATE deliveries SET payload_sha256=?,payload_json=? WHERE notification_id=? AND channel=? AND lease_token=?",
                               (hashlib.sha256(serialized.encode()).hexdigest(),serialized,ident,channel,token))
                try:
                    result = (sender or send_channel)(channel, delivery_payload)
                    if isinstance(result,dict):
                        accepted=result.get("accepted") is True and result.get("status")=="accepted"
                        status="accepted" if accepted else result.get("status","unknown")
                        if status not in ("accepted","failed","unknown","unavailable"):
                            status="unknown"
                        target=result.get("target") if accepted else None
                    else:
                        accepted=result
                        status="accepted" if accepted else "unavailable"
                        target=result if isinstance(result,str) else None
                    if not accepted:
                        error = "channel_not_accepted_or_not_configured"
                except Exception as exc:
                    accepted = False
                    target = None
                    error = type(exc).__name__  # never persist credentials or SMTP response bodies
                    if getattr(exc,"delivery_uncertain",False) or isinstance(exc,TimeoutError):
                        status = "unknown"
                with self.connection() as db:
                    finished = now.timestamp() + time.monotonic() - started
                    db.execute("""UPDATE deliveries SET status=?,accepted_at=?,target=?,payload_sha256=?,payload_json=?,lease_token=NULL,lease_until=NULL,
                        retry_at=?,last_error=? WHERE notification_id=? AND channel=? AND lease_token=?""",
                        (status, finished if status == "accepted" else None, target,
                         hashlib.sha256(serialized.encode()).hexdigest(),serialized,
                         finished+300, error, ident, channel, token))
            results.append(self.receipt(ident))
        return results


def validated_delivery_payload(store, payload):
    if payload.get("decision_quality"):return payload
    if payload.get("events"):
        from openinvest.core.evidence_eligibility import Eligibility, EvidenceIntegrityError
        from openinvest.db.event_store import EventStore
        database=None
        invalid=False
        try:
            excluded=Eligibility(store.data_root).excluded_ids()
            database=EventStore(str(store.data_root/"db/events.db"))
            for event in payload["events"]:
                eid=event.get("event_id")
                current=database.get_event(eid)
                if "event:"+str(eid) in excluded or (event.get("_version_id") and (not current or current.get("_version_id")!=event["_version_id"])):
                    invalid=True;break
        except (EvidenceIntegrityError,OSError,sqlite3.Error):
            invalid=True
        finally:
            if database is not None:database.conn.close()
        if invalid:
            from openinvest.services.notifier import render_markdown_email
            message="此前待发送的事件已更正、撤销或无法核验，原通知内容已停止使用。请复核最新来源；这里没有新的买卖建议。"
            return {**payload,"subject":"openInvest 事件状态需要复核","plain":message,"discord":message,
                    "html":render_markdown_email(message,footer_label="Evidence quality"),"decision_quality":True}
    if not payload.get("decision_id") or payload.get("decision_quality"):
        return payload
    from openinvest.core.decision_evidence import current_decision
    if current_decision(store.root,{"decision_id":payload["decision_id"]},payload.get("symbol","")):
        return payload
    from openinvest.services.notifier import render_markdown_email
    message=f"{payload.get('symbol','')} 此前的委员会记录已失效或未通过当前输入核验，请重新复核。这里没有新的可执行买卖建议。"
    return {**payload,"subject":"openInvest 建议状态需要复核","plain":message,"discord":message,
            "html":render_markdown_email(message,footer_label="Investment Committee"),"decision_quality":True}


def policy_enabled(policy: str) -> bool:
    if policy == "events":
        return os.getenv("INVEST_EVENT_ALERT", "0") == "1"
    if policy == "risk":
        from openinvest.core.config import load_config
        return bool(load_config(_force_reload=True).event.risk_alerts_enabled)
    return policy == "decision"  # non-HOLD or data-quality result only; ordinary HOLD stays quiet


def send_channel(channel: str, payload: dict) -> bool | str:
    if payload.get("instance_environment_bound") is not True:
        return {"accepted":False,"status":"unavailable","reason":"notification_instance_credentials_unverified"}
    if channel == "discord":
        from openinvest.services.discord_notify import send_discord_alert_receipt
        return send_discord_alert_receipt(payload["discord"],notification_id=payload.get("notification_id"))
    if channel == "email":
        from openinvest.services.notifier import send_email_html
        return send_email_html(subject=payload["subject"], html_body=payload["html"], plain_body=payload["plain"], max_retries=1)
    raise ValueError("unsupported notification channel")
