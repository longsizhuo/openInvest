from datetime import datetime,timedelta,timezone
import hashlib,json

import pytest

from openinvest.core.evidence_eligibility import digest
from openinvest.core.memory_store import MemoryStore,append_jsonl
from openinvest.db.event_store import EventStore
from openinvest.services.news_collection import RawCollection
from openinvest.services.operational_recovery import reconcile
from tests.integrity_helpers import reviewed_event_storage


def plan_for(store,**entries):
    value={'schema':'openinvest.operational_reconciliation_plan.v1','data_root':str(store.data_root.resolve()),
           'conflicts':[],'sources_with_missing_acquisition_evidence':[],'events_without_sources':[],**entries}
    value['plan_sha256']=digest(value)
    return value


def setup(tmp_path):
    store=MemoryStore(tmp_path/'memory');events=EventStore(str(tmp_path/'db/events.db'));raw=RawCollection(tmp_path)
    return store,events,raw


def test_preview_and_apply_preserve_legacy_failure_not_fake_completion(tmp_path):
    from openinvest.services import agent_queue
    from openinvest.services.review_plans import update_plan
    store,events,raw=setup(tmp_path);now=datetime.now(timezone.utc)
    request={'id':'legacy-request','symbols':['SYNTHETIC','OTHER'],'event_ids':[],
             'queued_at':(now-timedelta(days=2)).isoformat(),'reason':'original missed request'}
    queue=store.root/'.agent_queue/committees.jsonl';append_jsonl(queue,request);original=queue.read_bytes()
    record=update_plan(store,'SYNTHETIC',{'conditions':[{'metric':'quote_price','op':'<','value':10,'unit':'USD/unit'}],
        'expires_at':(now+timedelta(days=1)).isoformat()},evidence_id='missing',now=now)
    value=plan_for(store)
    preview=reconcile(store,value,now=now)
    assert preview['counts']=={'historical_missed':2,'retire_generated_plan':1}
    assert store.state_get('review_plan_SYNTHETIC')==record and queue.read_bytes()==original
    assert events.conn.execute("SELECT name FROM sqlite_master WHERE name='operational_reconciliations'").fetchone() is None
    applied=reconcile(store,value,apply=True,operator='test-owner',now=now)
    assert not applied['qualification_granted'] and not applied['financial_values_modified']
    assert queue.read_bytes()==original
    assert not agent_queue._read('committee_completions.jsonl',store.root)
    dispositions=agent_queue.committee_dispositions(memory_root=store.root)
    assert len(dispositions)==2 and all(r['payload']['completed'] is False for r in dispositions)
    assert store.state_get('review_plan_SYNTHETIC')['state']=='retired_audit_only'
    assert store.state_get('review_plan_SYNTHETIC')['spec']==record['spec']
    reconcile(store,value,apply=True,operator='test-owner',now=now)
    assert len(agent_queue.committee_dispositions(memory_root=store.root))==2
    assert events.conn.execute('SELECT count(*) FROM operational_reconciliations').fetchone()[0]==3


def test_metadata_conflict_closure_is_cas_bound_and_does_not_grant_truth(tmp_path):
    from openinvest.services.source_reporting import source_payload
    store,events,raw=setup(tmp_path);now=datetime.now(timezone.utc)
    events.upsert_event({'event_id':'original','one_line_claim':'Original extraction','ts':now.isoformat()})
    events.add_source('original',src_name='ddgs:test',url='https://example.invalid/story',title='Title',snippet='Same retained summary')
    source=dict(events.conn.execute('SELECT * FROM sources').fetchone())
    version=events.conn.execute('SELECT id,observed_at,payload_json FROM event_versions ORDER BY id DESC LIMIT 1').fetchone()
    incoming={'src_name':'ddgs:test','url':source['url'],'title':'Title','snippet':source['snippet'],'text':'',
              'published_at':now.isoformat(),'fetched_at':now.isoformat(),'raw_meta':{'collector_capture_id':'capture'}}
    packet={'source':source_payload({**incoming,'raw_text':''}),'collector':{},'acquired_at':now.isoformat()}
    raw.db.execute('INSERT INTO adapter_captures VALUES(?,?,?)',('capture',digest(packet),json.dumps(packet)));raw.db.commit()
    original=json.dumps({'event':{'one_line_claim':'Cosmetic rewrite'},'source':incoming,'reason':'claim_hash_matches_a_different_corrected_story'})
    fingerprint=hashlib.sha256(original.encode()).hexdigest()
    events.conn.execute('INSERT INTO news_observation_conflicts(fingerprint,event_id,observed_at,payload_json) VALUES(?,?,?,?)',
        (fingerprint,'original',(now+timedelta(seconds=1)).isoformat(),original));events.conn.commit()
    entry={'fingerprint':fingerprint,'event_id':'original','payload_sha256':fingerprint,
        'current_source':{'source_row_id':source['id'],'source_row_sha256':digest(source)},
        'capture':{'id':'capture','stored_packet_sha256':digest(packet),
            'stored_payload_bytes_sha256':hashlib.sha256(json.dumps(packet).encode()).hexdigest()},
        'recorded_source_at_or_before_conflict':{'version_id':version['id'],
            'payload_bytes_sha256':hashlib.sha256(version['payload_json'].encode()).hexdigest()}}
    value=plan_for(store,conflicts=[entry])
    altered={**packet,'acquired_at':(now+timedelta(hours=1)).isoformat()}
    raw.db.execute('UPDATE adapter_captures SET sha256=?,payload=? WHERE id=?',
        (digest(altered),json.dumps(altered),'capture'));raw.db.commit()
    with pytest.raises(ValueError,match='capture_changed_requires_rescan'):
        reconcile(store,value,apply=True,operator='test-owner',now=now)
    assert events.conn.execute('SELECT status FROM news_observation_conflicts').fetchone()[0]=='review_required'
    raw.db.execute('UPDATE adapter_captures SET sha256=?,payload=? WHERE id=?',
        (digest(packet),json.dumps(packet),'capture'));raw.db.commit()
    result=reconcile(store,value,apply=True,operator='test-owner',now=now)
    assert result['counts']=={'metadata_duplicate':1}
    after=events.conn.execute('SELECT * FROM news_observation_conflicts').fetchone()
    assert after['payload_json']==original and after['status']=='closed_metadata_duplicate'
    assert dict(events.conn.execute('SELECT * FROM sources').fetchone())==source
    assert events.get_event('original') is None  # Closing a diagnostic is not a source permission grant.


def test_legacy_disposition_cannot_race_event_evidence_write(tmp_path,monkeypatch):
    import sqlite3
    from openinvest.services import agent_queue
    store,events,raw=setup(tmp_path);now=datetime.now(timezone.utc)
    append_jsonl(store.root/'.agent_queue/committees.jsonl',{'id':'legacy','symbols':['SYNTHETIC'],
        'event_ids':[],'queued_at':(now-timedelta(days=2)).isoformat()})
    original=agent_queue.record_legacy_committee_disposition;blocked=[]
    def concurrent_write(*args,**kwargs):
        other=sqlite3.connect(store.data_root/'db/events.db',timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError,match='locked'):
                other.execute("INSERT INTO news_observation_conflicts(fingerprint,event_id,observed_at,payload_json) VALUES('racer','x','now','{}')")
            blocked.append(True)
        finally:other.close()
        return original(*args,**kwargs)
    monkeypatch.setattr(agent_queue,'record_legacy_committee_disposition',concurrent_write)
    reconcile(store,plan_for(store),apply=True,operator='test-owner',now=now)
    assert blocked==[True]


def test_upgrade_coverage_bridge_requires_exact_qualified_saved_inputs(tmp_path,reviewed_event_storage):
    from openinvest.services.operational_recovery import bind_confirmed_content_dispatches
    from openinvest.services.notification_outbox import NotificationOutbox
    from openinvest.services.news_sources import RawNewsItem
    from openinvest.core.evidence_eligibility import Eligibility
    store,events,raw=setup(tmp_path);now=datetime.now(timezone.utc)
    item=RawNewsItem(src_name='ddgs:test',url='https://example.invalid/story',title='Title',snippet='Retained text',published_at='2026-11-03T01:00:00Z')
    first=events.write_news_observation({'one_line_claim':'Original interpretation','stance':'risk','severity':'high','affected_symbols':['SYNTHETIC']},item)['event']
    box=NotificationOutbox(store)
    ident=box.enqueue('original',{'subject':'x','plain':'x','html':'x','discord':'x','followup':{'symbols':['SYNTHETIC'],'event_ids':[first['event_id']]}},category='market_risk',policy='decision')
    with box.connection() as db:
        db.execute("UPDATE followups SET status='queued',submitted=?,submitted_causes=? WHERE notification_id=?",
            (json.dumps({'SYNTHETIC':'original-task'}),json.dumps({'SYNTHETIC':{first['event_id']:first['_version_id']}}),ident))
    item.published_at='2026-11-03T01:30:00Z'
    events.write_news_observation(dict(first),item,reuse_extraction=True)
    result=bind_confirmed_content_dispatches(store,apply=True,now=now)
    assert result['count']==1 and result['unique_causes']==1 and result['analysis_completed'] is False
    with box.connection() as db:
        assert db.execute('SELECT status,task_id FROM analysis_causes').fetchone()[:]==('queued','original-task')
    Eligibility(store.data_root).revoke('event:'+first['event_id'],'synthetic source withdrawal')
    assert bind_confirmed_content_dispatches(store)['count']==0


def test_recovery_is_owner_only_and_scope_bound(tmp_path,monkeypatch):
    store,_,_=setup(tmp_path);value=plan_for(store)
    with pytest.raises(ValueError,match='explicit_operator_required'):
        reconcile(store,value,apply=True)
    wrong=plan_for(store);wrong['data_root']=str(tmp_path/'other');wrong['plan_sha256']=digest({k:v for k,v in wrong.items() if k!='plan_sha256'})
    with pytest.raises(ValueError,match='binding_invalid'):reconcile(store,wrong)
    monkeypatch.setenv('INVEST_ADVISORY_MODE','1')
    with pytest.raises(ValueError,match='owner_only'):reconcile(store,value)
