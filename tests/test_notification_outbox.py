from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import time

from openinvest.core.memory_store import MemoryStore
from openinvest.services.notification_outbox import NotificationOutbox


NOW = datetime(2026, 11, 7, tzinfo=timezone.utc)
PAYLOAD = {"subject": "test", "plain": "facts", "html": "facts", "discord": "facts"}


def test_partial_success_retries_only_failed_channel(tmp_path):
    box = NotificationOutbox(MemoryStore(tmp_path / "memory"))
    ident = box.enqueue("risk-1", PAYLOAD, category="test", policy="decision", now=NOW)
    calls = []
    def first(channel, payload):
        calls.append(channel)
        return "test@example.invalid" if channel == "email" else False
    box.flush(now=NOW, sender=first)
    assert box.receipt(ident)["accepted"]
    assert box.receipt(ident)["channels"]["discord"]["status"] == "unavailable"
    box.flush(now=NOW + timedelta(minutes=6), sender=lambda channel, payload: calls.append(channel) or True)
    assert calls.count("email") == 1 and calls.count("discord") == 2
    assert box.receipt(ident)["recipient_read_receipt"] == "unknown"


def test_disabled_is_not_success_and_can_be_delivered_after_enable(tmp_path, monkeypatch):
    monkeypatch.delenv("INVEST_EVENT_ALERT", raising=False)
    box = NotificationOutbox(MemoryStore(tmp_path / "memory"))
    ident = box.enqueue("event-1", PAYLOAD, category="test", policy="events", now=NOW)
    def forbidden(*args):
        raise AssertionError("disabled notification sent")
    box.flush(now=NOW, sender=forbidden)
    assert not box.receipt(ident)["accepted"]
    assert all(x["attempts"] == 0 for x in box.receipt(ident)["channels"].values())
    monkeypatch.setenv("INVEST_EVENT_ALERT", "1")
    box.flush(now=NOW, sender=lambda *args: True)
    assert box.receipt(ident)["accepted"]


def test_concurrent_flush_claims_each_channel_once(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    box = NotificationOutbox(store)
    ident = box.enqueue("event-2", PAYLOAD, category="test", policy="decision", now=NOW)
    calls = []
    def sender(channel, payload):
        calls.append(channel); time.sleep(.03)
        return True
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: NotificationOutbox(store).flush(now=NOW, sender=sender), range(2)))
    assert sorted(calls) == ["discord", "email"]
    assert box.receipt(ident)["accepted"]


def test_restore_pending_cannot_send_saved_notifications(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    (tmp_path / "integrity").mkdir()
    (tmp_path / "integrity/restore_pending.json").write_text("{}")
    box = NotificationOutbox(store)
    ident = box.enqueue("event-3", PAYLOAD, category="test", policy="decision", now=NOW)
    assert box.flush(now=NOW, sender=lambda *args: True) == []
    assert not box.receipt(ident)["accepted"]


def test_success_cooldown_crosses_notification_key_and_midnight(tmp_path):
    box = NotificationOutbox(MemoryStore(tmp_path / "memory"))
    payload={**PAYLOAD,"cooldown":{"domain":"same-user-line-v1","seconds":86400}}
    first=NOW.replace(hour=23,minute=59)
    box.enqueue("day-one",payload,category="test",policy="decision",now=first)
    calls=[]
    box.flush(now=first,sender=lambda channel,payload:calls.append(channel) or True)
    second=box.enqueue("day-two",payload,category="test",policy="decision",now=first+timedelta(minutes=2))
    box.flush(now=first+timedelta(minutes=2),sender=lambda channel,payload:calls.append(channel) or True)
    assert len(calls)==2
    assert all(x["status"]=="suppressed" for x in box.receipt(second)["channels"].values())


def test_expired_sending_lease_does_not_repeat_unconfirmed_delivery(tmp_path):
    from threading import Event
    box=NotificationOutbox(MemoryStore(tmp_path/"memory"))
    ident=box.enqueue("slow",PAYLOAD,category="test",policy="decision",now=NOW)
    started=Event();finish=Event();calls=[]
    def slow(channel,payload):
        calls.append(channel)
        if channel=="discord":
            started.set();assert finish.wait(5)
        return True
    with ThreadPoolExecutor(max_workers=1) as pool:
        first=pool.submit(box.flush,now=NOW,sender=slow)
        assert started.wait(2)
        box.flush(now=NOW+timedelta(seconds=301),sender=lambda channel,payload:calls.append(channel) or True)
        assert calls.count("discord")==1
        assert box.receipt(ident)["channels"]["discord"]["status"]=="unknown"
        finish.set();first.result()
    assert sorted(calls)==["discord","email"]


def test_invalid_context_is_not_sent_as_current_buy_advice(tmp_path):
    box=NotificationOutbox(MemoryStore(tmp_path/"memory"))
    old=box.enqueue("old-advice",{**PAYLOAD,"plain":"BUY NOW","discord":"BUY NOW",
                "decision_id":"a"*32,"symbol":"SYNTHETIC"},category="committee_result",policy="decision",now=NOW)
    sent=[]
    box.flush(now=NOW,sender=lambda channel,payload:sent.append(payload) or True)
    assert sent==[]
    assert all(row["status"]=="suppressed" for row in box.receipt(old)["channels"].values())
    box.flush(now=NOW,sender=lambda channel,payload:sent.append(payload) or True)
    assert sent and all("BUY NOW" not in p["plain"] for p in sent)
    assert all(p["decision_quality"] for p in sent)
    assert all(p["notification_id"]!=old and p["supersedes_notification"]==old for p in sent)


def test_one_revoked_daily_decision_suppresses_the_whole_saved_report(tmp_path):
    box=NotificationOutbox(MemoryStore(tmp_path/'memory'))
    ident=box.enqueue('daily-stale',{**PAYLOAD,'plain':'Old buy advice','decision_refs':[
        {'symbol':'SYNTHETIC','decision_id':'b'*32}]},category='daily_report',policy='decision',now=NOW)
    sent=[]
    box.flush(now=NOW,sender=lambda channel,payload:sent.append(payload) or True)
    assert sent==[] and all(row['status']=='suppressed' for row in box.receipt(ident)['channels'].values())
    box.flush(now=NOW,sender=lambda channel,payload:sent.append(payload) or True)
    assert sent and all('Old buy advice' not in payload['plain'] for payload in sent)
