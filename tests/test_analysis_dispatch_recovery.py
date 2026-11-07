"""Whole request recovery, fairness and withdrawal, without provider calls."""
from datetime import datetime,timedelta,timezone
import json

import pytest

from openinvest.core.memory_store import MemoryStore
from openinvest.services.notification_outbox import NotificationOutbox
from openinvest.services import analysis_dispatch as dispatch
from openinvest.services.event_trigger import reserve_analysis


NOW=datetime(2026,11,7,tzinfo=timezone.utc)


@pytest.fixture
def context(tmp_path,monkeypatch):
    monkeypatch.setattr("openinvest.paths.INVEST_ROOT",tmp_path)
    monkeypatch.setattr("openinvest.core.memory_store.MEMORY_ROOT",tmp_path/"memory")
    monkeypatch.setenv("INVEST_REASONING_BY","backend")
    store=MemoryStore()
    store.write("strategy","strategy",{"target_assets":[{"symbol":s} for s in ["GC=F","NDQ.AX"]]},"")
    calls=[]
    monkeypatch.setattr("openinvest.services.event_trigger._trigger_committee",lambda **kw:calls.append(kw) or "task")
    return store,NotificationOutbox(store),calls


def enqueue(box,key,symbols,**extra):
    return box.enqueue(key,{"subject":"test","plain":"facts","html":"facts","discord":"facts",
        "followup":{"symbols":symbols,"event_ids":[],**extra}},category="test",policy="decision",now=NOW)


def test_preparation_crash_reclaims_same_budget_reservation(context,monkeypatch):
    from openinvest.services.agent_queue import pending_committee_items
    store,box,calls=context
    monkeypatch.setenv("INVEST_REASONING_BY","agent")
    monkeypatch.setattr("openinvest.services.agent_queue._now",lambda:NOW+timedelta(minutes=6))
    enqueue(box,"cause",["GC=F"]);box.flush(now=NOW,sender=lambda *a:True)
    from openinvest.services import event_trigger
    original=event_trigger.reserve_analysis
    class SimulatedCrash(BaseException):pass
    def reserve_then_crash(*args,**kwargs):
        original(*args,**kwargs)
        raise SimulatedCrash()
    monkeypatch.setattr(event_trigger,"reserve_analysis",reserve_then_crash)
    with pytest.raises(SimulatedCrash):dispatch.dispatch_followups(box,now=NOW)
    monkeypatch.setattr(event_trigger,"reserve_analysis",original)
    dispatch.dispatch_followups(box,now=NOW+timedelta(minutes=6))
    assert pending_committee_items(now=NOW+timedelta(minutes=6),memory_root=store.root)[0]["symbol"]=="GC=F"
    assert len(store.state_get("event_committee_triggers")["runs"])==1
    with box.connection() as db:assert db.execute("SELECT status FROM followups").fetchone()[0]=="queued"
    assert calls==[]


def test_twenty_cooling_requests_do_not_starve_an_eligible_new_asset(context):
    store,box,calls=context
    reserve_analysis(store,[("GC=F",3)],now=NOW-timedelta(hours=1))
    for i in range(20):enqueue(box,f"old-{i}",["GC=F"])
    enqueue(box,"new-risk",["NDQ.AX"])
    box.flush(now=NOW,limit=100,sender=lambda *a:True)
    dispatch.dispatch_followups(box,now=NOW)
    dispatch.dispatch_followups(box,now=NOW)
    assert len(calls)==1 and calls[0]["symbols"]==["NDQ.AX"]


def test_partially_withdrawn_request_closes_without_claiming_submission(context):
    from openinvest.core.config import set_config_override
    from openinvest.core.evidence_eligibility import Eligibility
    from openinvest.db.event_store import EventStore
    store,box,calls=context
    set_config_override({"event":{"committee_daily_cap":1}})
    database=EventStore(str(store.data_root/"db/events.db"))
    for eid,symbol in [("a","GC=F"),("b","NDQ.AX")]:
        database.upsert_event({"event_id":eid,"one_line_claim":eid,"severity":"mid","stance":"risk",
                               "affected_symbols":[symbol],"ts":NOW.isoformat()})
    ident=enqueue(box,"both",["GC=F","NDQ.AX"],event_ids=["a","b"],event_symbols={"a":["GC=F"],"b":["NDQ.AX"]})
    box.flush(now=NOW,sender=lambda *a:True);dispatch.dispatch_followups(box,now=NOW)
    Eligibility(store.data_root).revoke("event:b","synthetic withdrawal")
    dispatch.dispatch_followups(box,now=NOW+timedelta(minutes=1))
    with box.connection() as db:
        row=db.execute("SELECT status,submitted,closed FROM followups WHERE notification_id=?",(ident,)).fetchone()
    assert row["status"]=="partially_cancelled"
    assert set(json.loads(row["submitted"]))=={"GC=F"}
    assert json.loads(row["closed"])=={"NDQ.AX":"sources_revoked"}
    assert len(calls)==1
    database.conn.close()
