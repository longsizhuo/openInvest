from datetime import datetime, timedelta, timezone
import json

import pytest

from openinvest.core.memory_store import MemoryStore
from openinvest.services.review_plans import update_plan, check_conditions, plan_brief
from openinvest.services.decision_quality import findings
from tests.integrity_helpers import reviewed_event_storage


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


def test_plan_id_cannot_be_transplanted_between_review_and_risk_purposes(tmp_path):
    store=MemoryStore(tmp_path/'memory')
    ordinary=update_plan(store,'SYNTHETIC',plan(),evidence_id='synthetic-original',now=NOW)
    attempted=update_plan(store,'SYNTHETIC',{**plan(),'plan_id':ordinary['plan_id']},purpose='risk',now=NOW)
    assert 'plan_id' not in attempted and attempted['last_error']=='plan_id_not_owned_by_symbol_and_purpose'
    assert store.state_get('review_plan_SYNTHETIC')['plan_id']==ordinary['plan_id']
    same=update_plan(store,'SYNTHETIC',{**plan(),'plan_id':ordinary['plan_id']},now=NOW)
    assert same['plan_id']==ordinary['plan_id']


def test_unknown_parent_target_never_reenters_model_account_context(tmp_path):
    store=MemoryStore(tmp_path/'memory')
    old=update_plan(store,'SYNTHETIC',plan(price=876.54321),evidence_id='nonexistent-parent',now=NOW)
    brief=plan_brief(store,'SYNTHETIC')
    assert '[QUALITY_ALERT]' in brief and '876.54321' not in brief
    assert old['plan_id'] in brief and store.state_get('review_plan_SYNTHETIC')['spec']==old['spec']
    assert any('父决策' in row['text'] for row in findings(store.root))


def test_same_conditions_can_be_explicitly_rejustified_without_moving_target(tmp_path):
    store=MemoryStore(tmp_path/'memory')
    old=update_plan(store,'SYNTHETIC',plan(),evidence_id='old-parent',now=NOW)
    duplicate=update_plan(store,'SYNTHETIC',plan(),evidence_id='another-parent',now=NOW)
    assert duplicate['plan_id']==old['plan_id'] and duplicate['evidence_id']=='old-parent'
    new=update_plan(store,'SYNTHETIC',{**plan(),'replaces_plan_id':old['plan_id'],'change_reason':'Independent current inputs support the same condition'},
                    evidence_id='new-parent',now=NOW)
    assert new['plan_id']!=old['plan_id'] and new['spec']==old['spec'] and new['evidence_id']=='new-parent'


def test_independent_quality_monitor_reads_current_parent_not_cached_qualification(tmp_path):
    store=MemoryStore(tmp_path/'memory')
    saved=update_plan(store,'SYNTHETIC',plan(),evidence_id='unknown-parent',now=NOW)
    saved['qualification']={'allowed':True}
    store.state_set('review_plan_SYNTHETIC',saved)
    assert any('父决策' in row['text'] for row in findings(store.root))


def test_sealed_plan_parent_matches_purpose_spec_and_revocation_but_outlives_advice_cache(tmp_path,monkeypatch):
    from openinvest.core import decision_evidence as evidence
    from openinvest.core.evidence_eligibility import digest
    from openinvest.services.review_plans import plan_qualification
    store=MemoryStore(tmp_path/'memory');monkeypatch.setattr(evidence,'code_version',lambda:'synthetic-version')
    context_id='a'*32;decision_id='b'*32
    spec=plan()
    parent={'symbol':'SYNTHETIC','decision_id':decision_id,'origin':'prospective','code_version':'synthetic-version','context_id':context_id,
            'validated_proposal':{'verdict':'HOLD','decision_status':'READY','reasoning_review':{'status':'passed'},'review_plan':spec}}
    context={'symbol':'SYNTHETIC','context_id':context_id,'prepared_at':(NOW-timedelta(days=2)).isoformat(),'source_event_versions':{},
             'source_plan_lineage_captured':True,
             'source_event_lineage_captured':True,'inputs':{'event_brief':'','prior_insights':''}}
    def seal(kind,ident,payload):
        p=store.root/'.evidence'/kind/(ident+'.json');p.parent.mkdir(parents=True,exist_ok=True)
        p.write_text(json.dumps({'payload':payload,'sha256':digest(payload)}))
    seal('decisions',decision_id,parent);seal('contexts',context_id,context)
    saved=update_plan(store,'SYNTHETIC',spec,evidence_id=decision_id,now=NOW)
    assert plan_qualification(store,'SYNTHETIC',saved)['allowed']
    assert not plan_qualification(store,'SYNTHETIC',saved,purpose='risk')['allowed']
    tampered=json.loads(json.dumps(saved));tampered['spec']['conditions'][0]['value']=999
    assert not plan_qualification(store,'SYNTHETIC',tampered)['allowed']
    bad={**parent,'validated_proposal':{**parent['validated_proposal'],'reasoning_review':{'status':'failed'}}}
    seal('decisions',decision_id,bad)
    assert not plan_qualification(store,'SYNTHETIC',saved)['allowed']
    seal('decisions',decision_id,parent)
    folder=store.data_root/'integrity';folder.mkdir()
    (folder/'eligibility.json').write_text(json.dumps({'artifacts':{}}))
    (folder/'exclusions.jsonl').write_text(json.dumps({'artifact_id':'decision:'+decision_id})+'\n')
    assert not plan_qualification(store,'SYNTHETIC',saved)['allowed']


def _bound_family_plan(store,spec,inputs,*,purpose='review'):
    from openinvest.core.decision_evidence import prepare_context,record_decision
    ctx=prepare_context(store.root,'SYNTHETIC',inputs,model='synthetic')
    field='risk_review_plan' if purpose=='risk' else 'review_plan'
    verdict={'verdict':'HOLD','decision_status':'READY','reasoning_review':{'status':'passed'},field:spec}
    parent=record_decision(store.root,'SYNTHETIC',{},verdict,{},context_id=ctx['context_id'])
    return update_plan(store,'SYNTHETIC',spec,evidence_id=parent['decision_id'],now=NOW,purpose=purpose)


def test_second_generation_plan_cannot_wash_a_revoked_original_event(tmp_path,monkeypatch,reviewed_event_storage):
    from openinvest.db.event_store import EventStore
    from openinvest.core.runner.event_format import EventBrief
    from openinvest.core.memory_store import append_jsonl
    from openinvest.services.review_plans import plan_qualification
    monkeypatch.setattr('openinvest.core.decision_evidence.code_version',lambda:'synthetic-version')
    store=MemoryStore(tmp_path/'memory');database=EventStore(str(tmp_path/'db/events.db'))
    _,eid=database.upsert_event({'one_line_claim':'Original observed port closure','stance':'risk','severity':'high','affected_symbols':['SYNTHETIC']})
    version=database.get_event(eid)['_version_id'];database.conn.close()
    first=_bound_family_plan(store,plan(876.54321),{'event_brief':EventBrief('Original observed port closure',{eid:version})})
    brief=plan_brief(store,'SYNTHETIC')
    second=_bound_family_plan(store,{**plan(876.54321),'expires_at':(NOW+timedelta(days=8)).isoformat(),
        'replaces_plan_id':first['plan_id'],'change_reason':'Current sources support a revised horizon'},
        {'event_brief':'','portfolio_summary':str(brief),'source_plan_refs':brief.plan_refs})
    assert plan_qualification(store,'SYNTHETIC',second)['allowed']
    append_jsonl(tmp_path/'integrity/exclusions.jsonl',{'artifact_id':'event:'+eid})
    assert not plan_qualification(store,'SYNTHETIC',second)['allowed']
    assert '876.54321' not in plan_brief(store,'SYNTHETIC')


@pytest.mark.parametrize('revoked',['model:historical_path_generator','dataset:synthetic-history','evaluation:synthetic-temporal-test','protocol:synthetic-preregistered'])
def test_plan_rechecks_forecast_generator_and_all_evaluation_parents(tmp_path,monkeypatch,revoked):
    from tests.integrity_helpers import approve_historical_generator
    from openinvest.core.memory_store import append_jsonl
    from openinvest.services.historical_evidence import qualification
    from openinvest.services.review_plans import plan_qualification
    monkeypatch.setattr('openinvest.core.decision_evidence.code_version',lambda:'synthetic-version')
    store=MemoryStore(tmp_path/'memory');approve_historical_generator(tmp_path)
    proof=qualification(tmp_path);assert proof['forecast_use_allowed']
    saved=_bound_family_plan(store,plan(),{'historical_qualification':proof,'event_brief':''})
    assert plan_qualification(store,'SYNTHETIC',saved)['allowed']
    append_jsonl(tmp_path/'integrity/exclusions.jsonl',{'artifact_id':revoked})
    assert not plan_qualification(store,'SYNTHETIC',saved)['allowed']


def test_shared_plan_ancestors_are_memoized_and_budget_failure_is_not_cached(tmp_path,monkeypatch):
    from openinvest.services.review_plans import plan_qualification
    from openinvest.core import decision_evidence
    monkeypatch.setattr(decision_evidence,'code_version',lambda:'synthetic-version')
    store=MemoryStore(tmp_path/'memory');leaf=None
    for generation in range(8):
        brief=plan_brief(store,'SYNTHETIC')
        inputs={'event_brief':'','portfolio_summary':str(brief),'source_plan_refs':getattr(brief,'plan_refs',[])}
        for purpose in ('review','risk'):
            # Each write changes the current plan. Capture the actual current
            # references for the next request instead of reusing a stale brief.
            brief=plan_brief(store,'SYNTHETIC')
            inputs={'event_brief':'','portfolio_summary':str(brief),'source_plan_refs':getattr(brief,'plan_refs',[])}
            previous=store.state_get('review_plan_'+('risk_' if purpose=='risk' else '')+'SYNTHETIC')
            spec={**plan(),'expires_at':(NOW+timedelta(days=7,hours=generation)).isoformat()}
            if previous:spec.update(replaces_plan_id=previous['plan_id'],change_reason='Independent refreshed sources')
            saved=_bound_family_plan(store,spec,inputs,purpose=purpose)
            if leaf is None:leaf=saved
    calls=[]
    monkeypatch.setattr(decision_evidence,'code_version',lambda:calls.append(1) or 'synthetic-version')
    assert plan_qualification(store,'SYNTHETIC',saved,purpose='risk')['allowed'] and len(calls)==1
    audit={'memo':{},'nodes':128,'code_version':None}
    assert not plan_qualification(store,'SYNTHETIC',saved,purpose='risk',_audit=audit)['allowed']
    audit['nodes']=0
    assert plan_qualification(store,'SYNTHETIC',leaf,_audit=audit)['allowed']
