"""Decision quality monitoring independent of trade signals and HOLD silence."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
import json
import re

from openinvest.core.decision_ledger import parse_committee_file


def findings(memory_root: Path, *, hold_window: int = 20) -> list[dict]:
    hold_window = max(2, int(hold_window))
    by_symbol = defaultdict(list)
    root = Path(memory_root)
    for day in sorted((root / ".committee").glob("????-??-??")):
        if not day.is_dir() or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day.name):
            continue
        try:
            dt = datetime.fromisoformat(day.name).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        for path in day.glob("*.md"):
            row = parse_committee_file(path)
            if row:
                by_symbol[row.get("symbol") or path.stem].append((dt, row))
    out = []
    from openinvest.core.evidence_eligibility import Eligibility, EvidenceIntegrityError
    try:
        Eligibility(root.parent).excluded_ids()
    except EvidenceIntegrityError:
        files = [root.parent / "integrity" / name for name in ("eligibility.json", "exclusions.jsonl")]
        anchor = max((p.stat().st_mtime for p in files if p.exists()), default=datetime.now(timezone.utc).timestamp())
        out.append({"key": "quality:evidence_governance_unavailable", "since": datetime.fromtimestamp(anchor, timezone.utc),
                    "text": "[QUALITY_ALERT] 数据排除/撤销状态无法核验，受影响建议必须停止使用；不能将此故障当作没有新闻。"})
    notification_db=root.parent/"db/notification_outbox.sqlite"
    event_db=root.parent/"db/events.db"
    if event_db.exists():
        import sqlite3
        from contextlib import closing
        from openinvest.db.event_store import EventStore
        try:
            reader=EventStore(str(event_db),readonly=True)
            try:source_state=reader.source_reporting_summary()
            finally:reader.conn.close()
            if source_state["current_unqualified_events"]:
                out.append({"key":"quality:source_authentication_pending:"+source_state["incident_sha256"],"since":datetime.fromtimestamp(event_db.stat().st_mtime,timezone.utc),"one_shot":True,
                    "text":f"[QUALITY_ALERT] {source_state['current_unqualified_events']} 条近期事件未具备来源用途资格（来源或输入关联待核验，或资格已撤销），已限制为审计，不作为当前来源事实；这不是没有新闻，也不是判定这些报道或预测全错。"})
        except (ValueError,OSError,sqlite3.Error):
            out.append({"key":"quality:source_verification_unavailable","since":datetime.fromtimestamp(event_db.stat().st_mtime,timezone.utc),
                        "text":"[QUALITY_ALERT] 来源真实性核验状态无法读取，需要处理；不能当作正常空新闻。"})
        with closing(sqlite3.connect(event_db.resolve().as_uri()+"?mode=ro",uri=True)) as connection:
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name='news_observation_conflicts'").fetchone():
                for fingerprint,observed,payload in connection.execute("SELECT fingerprint,observed_at,payload_json FROM news_observation_conflicts WHERE status='review_required' ORDER BY observed_at DESC LIMIT 20"):
                    reason=json.loads(payload).get("reason")
                    out.append({"key":"quality:source_conflict:"+fingerprint,"since":datetime.fromisoformat(observed),"one_shot":True,
                                "text":"[QUALITY_ALERT] 新输入的来源采集真实性尚未核验，已保留审计并阻止它覆盖已有可信报道。" if reason=="unverified_source_cannot_replace_verified_current_report" else
                                       "[QUALITY_ALERT] 新收到的报道与同一故事已更正版本冲突；已隔离冲突观察，未覆盖当前事实，需要核对来源。"})
    if notification_db.exists():
        import sqlite3
        from contextlib import closing
        try:
            with closing(sqlite3.connect(notification_db.resolve().as_uri()+"?mode=ro",uri=True)) as connection:
                uncertain=connection.execute("""SELECT DISTINCT n.id,n.category,n.created FROM notifications n JOIN deliveries d ON d.notification_id=n.id
                    WHERE d.status IN ('unknown','expired') AND n.category!='job_watchdog' LIMIT 20""").fetchall()
                pending=connection.execute("""SELECT n.id,n.category,n.created FROM notifications n JOIN followups f ON f.notification_id=n.id
                    WHERE f.status IN ('unknown','expired','verification_required') OR (f.status IN ('pending','dispatching') AND n.created<? AND f.error IS NOT NULL) LIMIT 20""",
                    (datetime.now(timezone.utc).timestamp()-900,)).fetchall()
            for ident,category,created in uncertain:
                out.append({"key":f"quality:notification_uncertain:{ident}","since":datetime.fromtimestamp(created,timezone.utc),"one_shot":True,
                            "text":f"[QUALITY_ALERT] {category} 通知存在未确认送达或已过期通道，请核对回执；系统没有把它计为已送达。"})
            for ident,category,created in pending:
                out.append({"key":f"quality:followup_uncertain:{ident}","since":datetime.fromtimestamp(created,timezone.utc),"one_shot":True,
                            "text":f"[QUALITY_ALERT] {category} 风险已记录，但后续分析尚未确认排队或持续受阻；需要复核，不能当作已经完成。"})
        except sqlite3.Error:
            out.append({"key":"quality:notification_state_unavailable","since":datetime.fromtimestamp(notification_db.stat().st_mtime,timezone.utc),
                        "text":"[QUALITY_ALERT] 通知回执存储无法核验，需要修复。"})
    try:
        delivered = set(json.loads((root / ".state/job_watchdog_alerts.json").read_text()))
    except (OSError, ValueError, TypeError):
        delivered = set()
    for symbol, rows in by_symbol.items():
        hold_rows=[row for row in rows if row[0].weekday()<5]
        tail = hold_rows[-hold_window:]
        if len(tail) == hold_window and all(r.get("verdict") == "HOLD" for _, r in tail):
            # Anchor at the start of the entire streak so reruns do not bypass dedup.
            start = len(hold_rows) - 1
            while start > 0 and hold_rows[start - 1][1].get("verdict") == "HOLD":
                start -= 1
            since = hold_rows[start + hold_window - 1][0]
            out.append({"key": f"quality:long_hold:{symbol}:{hold_rows[start][0].date()}", "since": since,
                        "text": f"[QUALITY_ALERT] {symbol} 连续 {len(hold_rows)-start} 个有记录工作日为 HOLD。请复核买卖两侧证据、规则改写和等待条件；这不是交易指令。"})
        last = rows[-1][1]
        health = last.get("input_health") or {}
        if health.get("decision_status") == "DATA_UNTRUSTED":
            out.append({"key": f"quality:untrusted:{symbol}:{rows[-1][0].date()}", "since": rows[-1][0],
                        "text": f"[QUALITY_ALERT] {symbol} 输入或版本未通过核验，当前无可执行建议，不能当作普通 HOLD。"})
        elif health.get("decision_status")=="REVIEW_REQUIRED":
            review=health.get("reasoning_review") or {}
            ident=review.get("request_sha256") or rows[-1][0].date().isoformat()
            out.append({"key":f"quality:reasoning_review:{symbol}:{ident}","since":rows[-1][0],"one_shot":True,
                        "text":f"[QUALITY_ALERT] {symbol} 原提案仍待核验（{review.get('status','execution_or_reasoning_review_required')}）。原始判断已保留，当前不具备执行资格；这不是普通 HOLD，也不是已证实的预测错误。"})
    for path in (root / ".state").glob("review_plan_*.json"):
        try:
            p = json.loads(path.read_text())
            if p.get("state") == "active" and datetime.fromisoformat(p["spec"]["expires_at"]) <= datetime.now(timezone.utc):
                out.append({"key": f"quality:plan_expiry:{p['plan_id']}", "since": datetime.fromisoformat(p["spec"]["expires_at"]),
                            "text": f"[QUALITY_ALERT] {p.get('symbol',path.stem)} 等待计划已到期；报价不可用也必须复核。"})
            for event in p.get("alerts", []):
                key = f"quality:plan_event:{event['id']}"
                if key + "#once" not in delivered:
                    out.append({"key": key, "since": datetime.fromisoformat(event["at"]), "one_shot": True,
                                "text": f"[QUALITY_ALERT] {event['symbol']} 等待计划发生 {event['kind']}（计划 {event.get('plan_id')}）。旧条件及变更记录已保存，需要复核；不自动下单。"})
            if p.get("state") in ("triggered", "expired") or p.get("last_error"):
                out.append({"key": f"quality:plan:{p.get('plan_id', path.stem)}:{p.get('state')}:{p.get('last_error','')}",
                            "since": datetime.fromisoformat(p.get("first_error_at") or p["changed_at"]),
                            "text": f"[QUALITY_ALERT] {p.get('symbol',path.stem)} 等待计划需要复核：{p.get('last_error') or p.get('state')}；不得静默把门槛移到下一价位。"})
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            out.append({"key": f"quality:plan_corrupt:{path.name}",
                        "since": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc),
                        "text": f"[QUALITY_ALERT] 复核计划 {path.name} 无法读取，需要修复。"})
    return out


def render(memory_root: Path) -> str:
    from openinvest.core.config import load_config
    rows = findings(memory_root, hold_window=load_config().scheduler.decision_hold_review_days)
    return "\n".join(r["text"] for r in rows)
