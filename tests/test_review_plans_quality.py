from datetime import datetime, timedelta, timezone
import json

import pytest

from openinvest.core.memory_store import MemoryStore
from openinvest.services.review_plans import update_plan, check_conditions, plan_brief
from openinvest.services.decision_quality import findings


NOW = datetime(2026,11,6,1,tzinfo=timezone.utc)


def plan(price=105):
    return {"conditions":[{"metric":"quote_price","op":">=","value":price,"unit":"USD/unit"}],
            "logic":"all","expires_at":(NOW+timedelta(days=7)).isoformat()}


def test_reached_condition_survives_replacement_and_never_authorizes_execution(tmp_path):
    store=MemoryStore(tmp_path/"memory")
    first=update_plan(store,"SYNTHETIC",plan(),evidence_id="first",now=NOW)
    replacement={**plan(110),"replaces_plan_id":first["plan_id"],"change_reason":"new evidence requires review"}
    second=update_plan(store,"SYNTHETIC",replacement,
                       observations={"quote_price":{"value":106,"unit":"USD/unit"}},evidence_id="second",now=NOW+timedelta(hours=1))
    assert second["state"] == "active" and not second["execution_authorized"]
    assert second["alerts"][0]["plan_id"] == first["plan_id"]
    assert second["alerts"][0]["spec"]["conditions"][0]["value"] == 105
    rows=findings(store.root)
    assert any(r.get("one_shot") and "conditions_met" in r["text"] for r in rows)
    assert '"alerts"' not in plan_brief(store,"SYNTHETIC")  # no accumulating old prompt context


def test_silent_moving_target_rejected_and_wrong_unit_does_not_trigger(tmp_path):
    store=MemoryStore(tmp_path/"memory")
    first=update_plan(store,"SYNTHETIC",plan(),now=NOW)
    rejected=update_plan(store,"SYNTHETIC",plan(120),now=NOW)
    assert rejected["plan_id"] == first["plan_id"]
    assert rejected["spec"]["conditions"][0]["value"] == 105
    assert rejected["last_error"] == "replacement_requires_old_plan_id_and_reason"
    assert not check_conditions(first["spec"],{"quote_price":{"value":106,"unit":"CNY/g"}})
    assert not check_conditions(first["spec"],{"quote_price":{"value":106,"unit":"USD/unit","stale":True}})


def test_missing_observation_and_expiry_are_not_a_buy_signal(tmp_path):
    store=MemoryStore(tmp_path/"memory")
    p=plan();p["conditions"].append({"metric":"vix","op":"<","value":18,"unit":"points"})
    first=update_plan(store,"SYNTHETIC",p,now=NOW)
    unchanged=update_plan(store,"SYNTHETIC",observations={"quote_price":{"value":106,"unit":"USD/unit"}},now=NOW)
    assert unchanged["state"] == "active"
    expired=update_plan(store,"SYNTHETIC",now=NOW+timedelta(days=8))
    assert expired["state"] == "expired" and not expired["execution_authorized"]


def test_constant_hold_has_independent_quality_alert(tmp_path):
    store=MemoryStore(tmp_path/"memory")
    count=0;day=NOW
    while count<20:
        if day.weekday()<5:
            p=store.root/".committee"/day.date().isoformat()/"SYNTHETIC.md"
            p.parent.mkdir(parents=True)
            p.write_text(f"# Committee: Synthetic\n**Date**: {day.date()}\n**Symbol**: SYNTHETIC\n**Verdict**: HOLD (confidence 0.7)\n")
            count+=1
        day-=timedelta(days=1)
    rows=findings(store.root)
    assert len(rows)==1 and "[QUALITY_ALERT]" in rows[0]["text"]
    assert "20" in rows[0]["text"] and "不是交易指令" in rows[0]["text"]


@pytest.mark.parametrize("symbol,currency", [("1024.HK","HKD"),("NDQ.AX","AUD")])
def test_legacy_bare_currency_plan_triggers_without_moving_target(tmp_path,symbol,currency):
    store=MemoryStore(tmp_path/"memory")
    old={"symbol":symbol,"plan_id":"original-plan","state":"active","created_at":NOW.isoformat(),"changed_at":NOW.isoformat(),
         "spec":{**plan(),"intent":"review_only"},"execution_authorized":False}
    old['spec']['conditions'][0]['unit']=currency
    store.state_set('review_plan_'+symbol.replace('.','_'),old)
    reached=update_plan(store,symbol,observations={'quote_price':{'value':106,'unit':currency+'/unit'}},now=NOW)
    assert reached['state']=='triggered' and reached['plan_id']=='original-plan'
    assert reached['spec']==old['spec'] and reached['spec']['conditions'][0]['value']==105


@pytest.mark.parametrize("symbol,unit", [("GC=F","USD"),("1024.HK","USD/unit"),("NDQ.AX","CNY/g")])
def test_invalid_plan_unit_is_an_immediate_quality_alert(tmp_path,symbol,unit):
    store=MemoryStore(tmp_path/"memory");spec=plan();spec['conditions'][0]['unit']=unit
    result=update_plan(store,symbol,spec,now=NOW)
    assert 'unit/asset mismatch' in result['last_error']
    assert any('QUALITY_ALERT' in row['text'] for row in findings(store.root))


def test_equivalent_unit_resubmission_keeps_identity_and_clears_resolved_error(tmp_path):
    store=MemoryStore(tmp_path/"memory");spec=plan();spec['conditions'][0]['unit']='AUD'
    original=update_plan(store,'NDQ.AX',spec,now=NOW)
    assert original['spec']['conditions'][0]['unit']=='AUD/unit'
    update_plan(store,'NDQ.AX',{**spec,'expires_at':'bad'},now=NOW)
    fixed=update_plan(store,'NDQ.AX',spec,now=NOW)
    assert fixed['plan_id']==original['plan_id'] and 'last_error' not in fixed



def test_uncertain_native_scale_is_not_equated_to_a_currency(tmp_path):
    from openinvest.services.review_plans import observe_quote
    store=MemoryStore(tmp_path/"memory");old=plan(price=30);old['conditions'][0]['unit']='GBP'
    original={"symbol":"SHEL.L","plan_id":"original-pence-unknown","state":"active","created_at":NOW.isoformat(),
              "changed_at":NOW.isoformat(),"spec":{**old,"intent":"review_only"},"execution_authorized":False}
    store.state_set('review_plan_SHEL_L',original)
    result=observe_quote(store,'SHEL.L',3000,now=NOW)
    assert result['state']=='active' and result['spec']==original['spec']
    assert 'native_quote_unit_unverified' in result['last_error']
    repeated=observe_quote(store,'SHEL.L',3001,now=NOW+timedelta(minutes=5))
    assert repeated['alerts']==result['alerts'] and len(repeated['alerts'])==1
    assert repeated['spec']==original['spec']
    expired=observe_quote(store,'SHEL.L',3001,now=NOW+timedelta(days=8))
    assert expired['state']=='expired' and len(expired['alerts'])==2
    assert not check_conditions(old,{'quote_price':{'value':3000,'unit':'GBP/unit'}})
    assert any('native_quote_unit_unverified' in row['text'] for row in findings(store.root))
