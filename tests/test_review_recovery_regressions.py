"""Real recovery boundaries, using synthetic evidence inside the OS test runner."""
import json
import sqlite3
import zipfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd
import pytest

from openinvest.core.evidence_eligibility import Eligibility, digest
from openinvest.core.memory_store import MemoryStore


def test_macro_missing_is_unknown_and_blocks_action(monkeypatch):
    from openinvest.utils import exchange_fee as source
    from openinvest.core.decision_evidence import enforce_input_health
    monkeypatch.setattr(source, "get_history_data", lambda *a, **k: pd.DataFrame())
    report=source.get_macro_data()
    assert "0.00" not in report
    health={"macro_data_empty": source.macro_input_health(report)["valid"] is False}
    verdict={"verdict":"BUY","alloc_cny":100,"confidence":.9}
    enforce_input_health(verdict,health)
    assert verdict["decision_status"] == "DATA_UNTRUSTED" and verdict["alloc_cny"] == 0
    assert source.macro_input_health("Error fetching macro data: unavailable")["valid"] is False


def test_invalid_plan_surfaces_and_changed_context_is_not_current_cache():
    from tests.integrity_helpers import context_id
    from openinvest.core.runner.coordinator import save_committee_transcript, load_submitted_committee
    from openinvest.services.decision_quality import findings
    from openinvest.core.config import set_config_override
    cid=context_id("SYNTHETIC")
    raw='=== RISK_R1 ===\nSIGNAL: unknown\nMARKET_RISK: ordinary\nLIQUIDITY: available\n=== CIO ===\nVERDICT: HOLD\nCONFIDENCE: 0.7\nREVIEW_PLAN_JSON: {bad'
    out=save_committee_transcript("SYNTHETIC",raw,context_id=cid)
    assert any("计划" in r["text"] for r in findings(MemoryStore().root))
    assert load_submitted_committee("SYNTHETIC",out["decision_date"]) is None
    set_config_override({"market_rules":{"quantile_low":25}})
    assert load_submitted_committee("SYNTHETIC",out["decision_date"]) is None
    receipt=save_committee_transcript("SYNTHETIC",raw,context_id=cid)
    assert receipt["replayed"] and receipt["decision_id"] == out["decision_id"]
    assert receipt["current_advice_valid"] is False


def test_restore_refuses_live_wal_without_modifying_target(tmp_path):
    from openinvest.services.restore_guard import restore_archive
    root=tmp_path/"target"; (root/"db").mkdir(parents=True)
    old=tmp_path/"old.db"
    with sqlite3.connect(old) as c:c.execute('create table item(value text)');c.execute("insert into item values('archive_old')")
    archive=tmp_path/"old.zip"
    with zipfile.ZipFile(archive,"w") as z:z.write(old,"db/trades.db")
    target=root/"db/trades.db"
    connection=sqlite3.connect(target)
    try:
        connection.execute('pragma journal_mode=wal');connection.execute('create table item(value text)')
        connection.execute("insert into item values('current_new')");connection.commit()
        with pytest.raises(ValueError,match="sidecars"):
            restore_archive(root,archive)
        assert connection.execute('select value from item').fetchone()[0] == 'current_new'
    finally:connection.close()


def test_recent_events_revoke_and_replica_revisions(tmp_path):
    from openinvest.db.event_store import EventStore
    from openinvest.services.event_replication import sync_public_events
    a,b=tmp_path/"source",tmp_path/"target"
    source=EventStore(str(a/"db/events.db")); target=None
    try:
        event={"one_line_claim":"Synthetic revision","stance":"opportunity","severity":2,"affected_symbols":["SYNTHETIC"]}
        _,eid=source.upsert_event(event)
        source.add_source(eid,src_name="test",url="https://example.test/item")
        from tests.integrity_helpers import approve_event_reporting,approve_public_event
        approve_event_reporting(source,eid);approve_public_event(source,eid)
        sync_public_events(a,b,{"example.test"})
        source.upsert_event({**event,"stance":"risk"})
        approve_event_reporting(source,eid);approve_public_event(source,eid)
        sync_public_events(a,b,{"example.test"})
        target=EventStore(str(b/"db/events.db"))
        assert target.get_event(eid) is None
        approve_event_reporting(target,eid)
        assert target.get_event(eid)["stance"] == "risk"
        assert sync_public_events(a,b,{"example.test"})["imported_public_events"] == 0
        Eligibility(b).revoke(f"event:{eid}","synthetic revoked")
        assert target.list_recent() == [] and target.count_recent()["total"] == 0
    finally:
        source.conn.close()
        if target:target.conn.close()


def release_fixture(root):
    from tests.test_evaluation_protocol import protocol, sample
    from tests.integrity_helpers import approve
    from openinvest.core.learning_control import ResearchJournal
    from openinvest.core.evaluation_protocol import evaluate, VERSION_FIELDS
    from openinvest.core.decision_evidence import code_version, config_snapshot, _eligibility_version
    p=protocol();p['designation']='prospective_investment_evaluation'
    for key,value in list(p.items()):
        if isinstance(value,str):p[key]=value.replace('2025-','2030-').replace('2024-','2029-')
    p['expected_observation_keys']=[v.replace('2025-','2030-') for v in p['expected_observation_keys']]
    parameters={'market_rules.quantile_low':25.0}
    candidate={'id':'synthetic-frozen','parameters':parameters,'model_version':p['model_version'],'trained_until':p['train_end']}
    journal=ResearchJournal(root);journal.register(candidate,p)
    rows=[sample(),sample(14)]
    for row in rows:
        for key,value in list(row.items()):
            if isinstance(value,str):row[key]=value.replace('2025-','2030-')
        row.update(candidate_id=candidate['id'],parameters_sha256=digest(parameters))
        approve(root,f"sample:{row['sample_id']}",row)
    evaluation=evaluate(rows,p,Eligibility(root),now=datetime(2031,1,1,tzinfo=timezone.utc),candidate=candidate)
    assert evaluation['status']=='passed'
    bundle={key:'a'*64 for key in VERSION_FIELDS}
    bundle.update(code=code_version(),data=evaluation['data_sha256'],parameters=digest(parameters),protocol=digest(p),
                  consent=digest(_eligibility_version(root)),baseline_config_sha256=digest(config_snapshot()),
                  baseline_overrides_sha256=digest({}),parameter_changes=parameters,author='synthetic-author',
                  shadow_trading_days=5,rollback_target='synthetic-prior',retain_latest_exclusions=True)
    policy={'auto_publish':True,'parameter_allowlist':{'market_rules.quantile_low':[20,30]},
            'approved_runtime':{key:bundle[key] for key in ('code','features','model','prompts','rules')},
            'approved_protocols':[digest(p)]}
    authority=root/'integrity';(authority/'release_policy.json').write_text(json.dumps(policy))
    for directory in ('evaluations','rollback_targets'):(authority/directory).mkdir()
    (authority/'evaluations'/f'{digest(evaluation)}.json').write_text(json.dumps(evaluation))
    (authority/'rollback_targets/synthetic-prior.json').write_text('{}')
    approvals=authority/'approvals'/digest(bundle);approvals.mkdir(parents=True)
    for role in ('engineering','data','statistics'):
        (approvals/f'{role}.json').write_text(json.dumps({'role':role,'reviewer':'synthetic-'+role,'passed':True,'bundle_sha256':digest(bundle)}))
    return bundle,evaluation


@pytest.mark.parametrize('violation',[None,'missing_reservation','changed_candidate','authorization_changed'])
def test_enabled_publication_requires_all_frozen_authority(tmp_path,monkeypatch,violation):
    from openinvest.core.learning_control import publish_parameters
    root=tmp_path/'synthetic';root.mkdir();store=MemoryStore(root/'memory')
    bundle,evaluation=release_fixture(root)
    if violation=='missing_reservation':(root/'integrity/final_set_uses.json').unlink()
    if violation=='changed_candidate':
        p=root/'integrity/candidates/synthetic-frozen.json';r=json.loads(p.read_text())
        r['payload']['candidate']['parameters']['market_rules.quantile_low']=26
        r['sha256']=digest(r['payload']);p.write_text(json.dumps(r))
    if violation=='authorization_changed':
        real=store.state_update
        def update(name, fn):
            p=root/'integrity/release_policy.json';policy=json.loads(p.read_text());policy['auto_publish']=False;p.write_text(json.dumps(policy))
            return real(name,fn)
        monkeypatch.setattr(store,'state_update',update)
        with pytest.raises(ValueError,match='authorization'):
            publish_parameters(store,bundle,evaluation,[])
    else:
        result=publish_parameters(store,bundle,evaluation,[])
        assert result['status'] == ('published' if violation is None else 'blocked'), result
    assert bool(store.state_get('config_overrides')) == (violation is None)


def test_label_maturity_and_entry_do_not_cross_frozen_boundaries(tmp_path):
    from tests.test_evaluation_protocol import sample,protocol,score
    from openinvest.core.evaluation_protocol import validate_sample
    row=sample();row['label_available_at']='2025-02-16T00:00:00+00:00'
    assert score(tmp_path,[row])['excluded'][0]['reason']=='purged_block_overlap'
    row['label_available_at']='2025-05-01T00:00:00+00:00'
    assert validate_sample(row,protocol(),datetime(2026,1,1,tzinfo=timezone.utc),'final')=='purged_boundary_overlap'
    row=sample();row['entry_at']='2025-02-10T00:00:00+00:00';row['exit_at']=row['label_available_at']='2025-02-12T00:00:00+00:00'
    assert validate_sample(row,protocol(),datetime(2026,1,1,tzinfo=timezone.utc),'final')=='execution_precedes_prediction'


def test_failed_decision_and_tampered_plan_are_never_current():
    from tests.integrity_helpers import context_id
    from openinvest.core.runner.coordinator import save_committee_transcript,load_submitted_committee
    from openinvest.core.decision_evidence import current_decision
    cid=context_id("SYNTHETIC")
    raw="=== QUANT_R1 ===\n[WORKER_UNAVAILABLE] reason=synthetic_failure\n=== CIO ===\nVERDICT: BUY\nCONFIDENCE: 0.9\nSUGGESTED_ALLOC_CNY: 1000"
    result=save_committee_transcript("SYNTHETIC",raw,context_id=cid)
    assert result['verdict']['decision_status']=='DATA_UNTRUSTED'
    assert not result['current_advice_valid']
    assert not current_decision(MemoryStore().root,result,"SYNTHETIC")
    assert not save_committee_transcript("SYNTHETIC",raw,context_id=cid)['current_advice_valid']
    good=save_committee_transcript("SYNTHETIC","=== RISK_R1 ===\nSIGNAL: unknown\nMARKET_RISK: ordinary\nLIQUIDITY: available\n=== CIO ===\nVERDICT: HOLD\nCONFIDENCE: 0.7",context_id=context_id("SYNTHETIC"))
    assert load_submitted_committee("SYNTHETIC",good['decision_date'])
    sidecar=Path(good['latest_saved']).with_name('SYNTHETIC_committee.json')
    edited=json.loads(sidecar.read_text());edited['verdict']['review_plan']={'conditions':[{'value':9999}]};edited['cio_memo']='changed waiting target'
    sidecar.write_text(json.dumps(edited))
    assert load_submitted_committee("SYNTHETIC",good['decision_date']) is None


def test_current_cache_rejects_changed_transcript_without_changing_header():
    from tests.integrity_helpers import context_id
    from openinvest.core.runner.coordinator import save_committee_transcript
    from openinvest.core.decision_evidence import current_decision
    from openinvest.core.decision_ledger import parse_committee_file
    good=save_committee_transcript("SYNTHETIC","=== RISK_R1 ===\nSIGNAL: unknown\nMARKET_RISK: ordinary\nLIQUIDITY: available\n=== CIO ===\nVERDICT: HOLD\nCONFIDENCE: 0.7\nORIGINAL_REASON",context_id=context_id("SYNTHETIC"))
    path=Path(good['latest_saved']);original=path.read_text();parsed=parse_committee_file(path)
    assert current_decision(MemoryStore().root,parsed['input_health'],'SYNTHETIC',verdict=parsed,transcript=original)
    path.write_text(original.replace('ORIGINAL_REASON','TAMPERED_ADVICE'));parsed=parse_committee_file(path)
    assert not current_decision(MemoryStore().root,parsed['input_health'],'SYNTHETIC',verdict=parsed)
    assert not current_decision(MemoryStore().root,parsed['input_health'],'SYNTHETIC',verdict=parsed,transcript=path.read_text())


@pytest.fixture(autouse=True)
def _synthetic_review_for_non_review_contract(monkeypatch):
    # These tests isolate storage/cache/format contracts; dedicated review-flow
    # tests exercise actual verification, unavailable capability and failures.
    monkeypatch.setattr("openinvest.services.reasoning_review.review_once",
        lambda *args,**kwargs:{"status":"passed","origin":"synthetic_unit_fixture"})
