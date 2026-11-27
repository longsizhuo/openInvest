"""Future news audit lineage binds the stored event, never input/projection extras."""
from datetime import datetime,timedelta,timezone
import json

from openinvest.core.evidence_eligibility import Eligibility,digest
from openinvest.db.event_store import EventStore
from openinvest.services.news_sources import RawNewsItem
from openinvest.services.source_reporting import event_payload,from_item,source_fingerprint,source_payload
from tests.test_evidence_eligibility import approve


def item(parent,minutes=0):
    return RawNewsItem(src_name='synthetic-native',url='https://example.invalid/audit-story',
        title='Original source',snippet='Retained source text',text='Original retained source text',
        published_at='2026-11-01T00:00:00+00:00',
        fetched_at=(datetime(2026,11,1,tzinfo=timezone.utc)+timedelta(minutes=minutes)).isoformat(),
        raw_meta={'source_snapshot_id':parent})


def event():
    return {'event_id':'audit-story','one_line_claim':'Synthetic normalized report','event_type':'macro',
        'stance':'risk','severity':'mid','ts':'2026-11-01T00:00:00+00:00','entities':['synthetic'],
        'affected_symbols':['SYNTHETIC']}


def audit_record(root):return json.loads((root/'integrity/eligibility.json').read_text())['artifacts']['event:audit-story']


def test_fresh_input_extras_do_not_enter_current_event_audit_hash(tmp_path,monkeypatch):
    store=EventStore(str(tmp_path/'db/events.db'))
    incoming={**event(),'extra_source_projection':'not a canonical event field','_version_id':999,
        '_analysis_cause_id':'input-only-cause','reporting_evidence':{'allowed':True}}
    original=Eligibility.record_audit_lineage;seen=[]
    def captured(self,key,payload,parents):
        if key=='event:audit-story':
            assert store.conn.in_transaction
            seen.append(payload)
        return original(self,key,payload,parents)
    monkeypatch.setattr(Eligibility,'record_audit_lineage',captured)
    try:
        store.write_news_observation(incoming,item('first'))
        current=event_payload(store.get_event('audit-story',purpose='audit'))
        record=audit_record(tmp_path)
        assert seen==[current] and record['sha256']==digest(current)
        assert record['sha256']!=digest(incoming)
        assert record['parents']==['source_snapshot:first']
        assert record['origin']=='unverified' and record['source_validity']=='pending_review' and record['allowed_uses']==['audit']
        assert Eligibility(tmp_path).allows('event:audit-story',current,'advice') is False
    finally:store.conn.close()


def test_reused_projection_and_new_acquisition_metadata_keep_canonical_sha_and_all_parents(tmp_path):
    store=EventStore(str(tmp_path/'db/events.db'))
    first=item('first');later=item('second',30)
    try:
        for source in (first,later):
            value=from_item(source)
            approve(tmp_path,'source_report:'+source_fingerprint(value),source_payload(value))
        initial=store.write_news_observation(event(),first)['event']
        projected=store.get_event('audit-story')
        assert projected['_version_id'] and projected['_analysis_cause_id']
        original_sha=audit_record(tmp_path)['sha256']
        result=store.write_news_observation(projected,later,reuse_extraction=True)
        current=store.get_event('audit-story',purpose='audit');record=audit_record(tmp_path)
        assert result['changed'] is False and current['_version_id']>initial['_version_id']
        assert record['sha256']==digest(event_payload(current))==original_sha
        assert record['sha256']!=digest(projected)
        assert record['parents']==['source_snapshot:first','source_snapshot:second']
        assert record['origin']=='unverified' and record['source_validity']=='pending_review' and record['allowed_uses']==['audit']
        assert Eligibility(tmp_path).allows('event:audit-story',event_payload(current),'learn') is False
    finally:store.conn.close()
