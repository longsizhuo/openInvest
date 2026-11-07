from datetime import datetime, timezone
import smtplib

from openinvest.core.memory_store import MemoryStore
from openinvest.services.notification_outbox import NotificationOutbox


def test_one_corrupt_decision_cannot_block_queued_risk_delivery(tmp_path,monkeypatch):
    from openinvest.jobs import notification_delivery
    monkeypatch.setattr("openinvest.core.memory_store.MEMORY_ROOT",tmp_path/"memory")
    store=MemoryStore();directory=store.root/".evidence/decisions";directory.mkdir(parents=True)
    (directory/"broken.json").write_text("{broken")
    box=NotificationOutbox(store)
    ident=box.enqueue("risk",{"subject":"risk","plain":"facts","html":"facts","discord":"facts"},
                      category="test",policy="decision",now=datetime.now(timezone.utc))
    sent=[]
    monkeypatch.setattr("openinvest.services.notification_outbox.send_channel",lambda channel,payload:sent.append(channel) or True)
    notification_delivery.run()
    assert sorted(sent)==["discord","email"]
    assert box.receipt(ident)["accepted"]
    assert "broken.json" in store.state_get("notification_reconcile_errors")


def test_smtp_quit_failure_after_data_does_not_repeat_mail(monkeypatch):
    from openinvest.services.notifier import send_email_html
    monkeypatch.setenv("EMAIL_SENDER","test@example.invalid")
    monkeypatch.setenv("EMAIL_PASSWORD","synthetic-not-a-secret")
    calls=[]
    class SMTP:
        def __init__(self,*a,**kw):pass
        def __enter__(self):return self
        def __exit__(self,*args):raise smtplib.SMTPServerDisconnected("QUIT failed after acceptance")
        def ehlo(self):pass
        def starttls(self,**kw):pass
        def login(self,*args):pass
        def sendmail(self,*args):calls.append("accepted");return {}
    monkeypatch.setattr(smtplib,"SMTP",SMTP)
    assert send_email_html(subject="test",html_body="test",plain_body="test",receiver="target@example.invalid")=="target@example.invalid"
    assert calls==["accepted"]


def _sealed_result(store,verdict="BUY"):
    import json
    from openinvest.core.decision_evidence import prepare_context,record_decision,_write_once
    from openinvest.core.evidence_eligibility import digest
    ctx=prepare_context(store.root,"SYNTHETIC",{"market_data":"synthetic fixture"})
    final={"verdict":verdict,"alloc_cny":100 if verdict=="BUY" else 0,"decision_status":"READY"}
    result=record_decision(store.root,"SYNTHETIC",{"cio_memo":"synthetic supported rationale"},final,{},context_id=ctx["context_id"])
    path=store.root/".evidence/decisions"/(result["decision_id"]+".json")
    record=json.loads(path.read_text())
    # Emulate a production-origin record only inside the OS-isolated fixture.
    record["payload"]["origin"]="prospective";record["sha256"]=digest(record["payload"])
    path.write_text(json.dumps(record))
    folder=store.root/".evidence/transcripts";folder.mkdir(exist_ok=True)
    (folder/(result["decision_id"]+".md")).write_text("synthetic transcript")
    _write_once(folder/(result["decision_id"]+".json"),{"text":"synthetic transcript"})
    return {**final,**result}


def test_web_and_agent_completion_paths_share_one_canonical_notice(tmp_path,monkeypatch):
    from openinvest.services.event_notifier import send_committee_verdict_email
    from openinvest.jobs import notification_delivery
    monkeypatch.setattr("openinvest.core.memory_store.MEMORY_ROOT",tmp_path/"memory")
    store=MemoryStore();verdict=_sealed_result(store)
    calls=[]
    monkeypatch.setattr("openinvest.services.notification_outbox.send_channel",lambda channel,payload:calls.append((channel,payload)) or True)
    send_committee_verdict_email(task_id="web-task",symbols=["SYNTHETIC"],by_asset={"SYNTHETIC":{"verdict":verdict}})
    assert calls==[]  # A producer never claims synchronous delivery.
    notification_delivery.run(store=store)
    notification_delivery.run(store=store)
    assert [c[0] for c in calls]==["discord","email"]
    assert all(c[1]["decision_id"]==verdict["decision_id"] for c in calls)
    with NotificationOutbox(store).connection() as db:
        assert db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]==1


def test_starting_delivery_does_not_replay_historical_advice(tmp_path):
    import json
    from datetime import timedelta
    from openinvest.core.evidence_eligibility import digest
    from openinvest.jobs.notification_delivery import reconcile_decisions
    store=MemoryStore(tmp_path/"memory");now=datetime.now(timezone.utc)
    folder=store.root/".evidence/decisions";folder.mkdir(parents=True)
    payload={"origin":"prospective","recorded_at":(now-timedelta(hours=3)).isoformat()}
    (folder/"old.json").write_text(json.dumps({"payload":payload,"sha256":digest(payload)}))
    box=NotificationOutbox(store)
    assert reconcile_decisions(store,box,now=now)==0
    with box.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]==0


def test_custom_store_followup_cannot_dispatch_to_global_http_runtime(tmp_path,monkeypatch):
    from openinvest.jobs.notification_delivery import dispatch_followups
    monkeypatch.setenv("INVEST_REASONING_BY","backend")
    box=NotificationOutbox(MemoryStore(tmp_path/"private-tenant/memory"))
    now=datetime.now(timezone.utc)
    ident=box.enqueue("risk",{"subject":"risk","plain":"facts","html":"facts","discord":"facts",
                    "followup":{"symbols":["SYNTHETIC"],"event_ids":[]}},category="test",policy="decision",now=now)
    box.flush(now=now,sender=lambda *args:True)
    monkeypatch.setattr("openinvest.services.event_trigger._trigger_committee",lambda *a,**kw:(_ for _ in ()).throw(AssertionError("cross-instance dispatch")))
    dispatch_followups(box,now=now)
    with box.connection() as db:
        row=db.execute("SELECT status,error FROM followups WHERE notification_id=?",(ident,)).fetchone()
        assert tuple(row)==("unknown","instance_dispatch_unverified")
