"""Bound governance IO while preserving replication's negative qualifications."""
import cProfile
import io
import json
from pathlib import Path
import pstats
import time

import pytest

from openinvest.core.evidence_eligibility import Eligibility,EvidenceIntegrityError,digest
from openinvest.db.event_store import EventStore
from openinvest.services.event_replication import publication_id,publication_payload,sync_public_events
from openinvest.services.source_reporting import event_review_id,event_review_payload,source_fingerprint,source_payload


def reviewed_batch(root,count,*,filler=0):
    store=EventStore(str(root/'db/events.db'))
    records={f'irrelevant:{number}':{'parents':[],'origin':'unverified','allowed_uses':['audit']}
             for number in range(filler)}
    def record(key,payload,parents=()):
        records[key]={'sha256':digest(payload),'source_validity':'valid','origin':'prospective',
            'allowed_uses':['advice','publish'],'review_id':'synthetic-performance-protocol',
            'reviewed_at':'2026-11-01T00:00:00+00:00','parents':list(parents),
            'parent_hashes':{parent:records[parent]['sha256'] for parent in parents}}
    for number in range(count):
        eid=f'public-{number}'
        store.upsert_event({'event_id':eid,'one_line_claim':f'Synthetic publication {number}',
            'stance':'risk','severity':'high','affected_symbols':['SYNTHETIC']})
        store.add_source(eid,src_name='synthetic-fixture',url=f'https://reuters.com/synthetic/{number}',
                         title='Synthetic source',snippet=f'Observed synthetic body {number}')
        event=store.get_event(eid,purpose='audit');sources=store.get_sources(eid,purpose='audit')
        source_key='source_report:'+source_fingerprint(sources[0])
        record(source_key,source_payload(sources[0]))
        review_key=event_review_id(event,sources)
        record(review_key,event_review_payload(event,sources),[source_key])
        record(publication_id(event,sources),publication_payload(event,sources),[review_key])
    directory=root/'integrity';directory.mkdir(exist_ok=True)
    (directory/'eligibility.json').write_text(json.dumps({'schema':1,'artifacts':records}))
    (directory/'exclusions.jsonl').write_text('')
    store.conn.close()


@pytest.mark.parametrize('count,filler',[(30,600),(60,1200)])
def test_stable_governance_is_not_reparsed_for_each_event(tmp_path,monkeypatch,count,filler):
    source,target=tmp_path/'source',tmp_path/'target'
    reviewed_batch(source,count,filler=filler);reviewed_batch(target,0,filler=filler)
    reads={'source':0,'target':0};original=Path.read_text
    def counted(path,*args,**kwargs):
        if path.name=='eligibility.json' and path.parent.parent in {source,target}:
            reads[path.parent.parent.name]+=1
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'read_text',counted)
    profile=cProfile.Profile();start=time.perf_counter();profile.enable()
    first=sync_public_events(source,target,{'reuters.com'})
    second=sync_public_events(source,target,{'reuters.com'})
    profile.disable();elapsed=time.perf_counter()-start
    stream=io.StringIO();pstats.Stats(profile,stream=stream).sort_stats('cumulative').print_stats(12)
    print(json.dumps({'events':count,'filler_per_registry':filler,'elapsed_seconds':elapsed,'registry_reads':reads}))
    print(stream.getvalue())
    assert first['imported_public_events']==count and second['imported_public_events']==0
    assert sum(reads.values())<=12,reads


def test_long_parent_chain_retains_exact_exclusion_closure(tmp_path):
    directory=tmp_path/'integrity';directory.mkdir()
    count=1500
    rows={f'node:{index}':{'parents':[f'node:{index-1}']} for index in range(1,count)}
    rows['node:0']={'parents':[]};rows['unrelated']={'parents':[]}
    (directory/'eligibility.json').write_text(json.dumps({'schema':1,'artifacts':rows}))
    (directory/'exclusions.jsonl').write_text(json.dumps({'artifact_id':'node:0'})+'\n')
    assert Eligibility(tmp_path).excluded_ids()=={f'node:{index}' for index in range(count)}


@pytest.mark.parametrize('change',['event_revoked','parent_revoked','publication_deleted','publish_removed','preserved_mtime',
                                  'broken_registry','missing_exclusions'])
def test_permission_changes_during_a_batch_are_observed_before_the_next_copy(tmp_path,monkeypatch,change):
    source,target=tmp_path/'source',tmp_path/'target'
    reviewed_batch(source,2)
    reader=EventStore(str(source/'db/events.db'))
    event=reader.get_event('public-1',purpose='audit');sources=reader.get_sources('public-1',purpose='audit')
    publication=publication_id(event,sources)
    reader.conn.close()
    add_source=EventStore.add_source
    changed=False
    def after_first_copy(self,eid,*args,**kwargs):
        nonlocal changed
        result=add_source(self,eid,*args,**kwargs)
        if self.data_root==target and eid=='public-0' and not changed:
            changed=True
            if change=='event_revoked':Eligibility(source).revoke('event:public-1','synthetic mid-sync revocation')
            elif change=='parent_revoked':Eligibility(source).revoke('source_report:'+source_fingerprint(sources[0]),'synthetic parent revoked')
            elif change=='missing_exclusions':(source/'integrity/exclusions.jsonl').unlink()
            elif change=='broken_registry':(source/'integrity/eligibility.json').write_text('{')
            else:
                path=source/'integrity/eligibility.json';state=json.loads(path.read_text())
                if change=='publication_deleted':state['artifacts'][publication]['deleted']=True
                else:state['artifacts'][publication]['allowed_uses']=['advice']
                if change=='preserved_mtime':
                    import os
                    before=path.stat();text=json.dumps(state,separators=(',',':'))
                    assert len(text)<before.st_size
                    path.write_text(text+' '*(before.st_size-len(text)))
                    os.utime(path,ns=(before.st_atime_ns,before.st_mtime_ns))
                else:path.write_text(json.dumps(state))
        return result
    monkeypatch.setattr(EventStore,'add_source',after_first_copy)
    if change in {'broken_registry','missing_exclusions'}:
        with pytest.raises(EvidenceIntegrityError):sync_public_events(source,target,{'reuters.com'})
    else:
        assert sync_public_events(source,target,{'reuters.com'})['imported_public_events']==1
    replica=EventStore(str(target/'db/events.db'))
    try:
        assert replica.get_event('public-0',purpose='audit') is not None
        assert replica.get_event('public-1',purpose='audit') is None
    finally:replica.conn.close()


def test_revocation_after_a_cached_publication_check_aborts_before_writing(tmp_path,monkeypatch):
    source,target=tmp_path/'source',tmp_path/'target';reviewed_batch(source,1)
    allows=Eligibility.allows
    revoked=False
    def revoke_after_validation(self,key,payload,use):
        nonlocal revoked
        result=allows(self,key,payload,use)
        if result and self.root==source and key.startswith('public_event:') and use=='publish' and not revoked:
            revoked=True
            Eligibility(source).revoke(key,'synthetic interleaving after permission check')
        return result
    monkeypatch.setattr(Eligibility,'allows',revoke_after_validation)
    with pytest.raises(EvidenceIntegrityError,match='governance changed'):
        sync_public_events(source,target,{'reuters.com'})
    replica=EventStore(str(target/'db/events.db'))
    try:assert replica.get_event('public-0',purpose='audit') is None
    finally:replica.conn.close()


def test_preparsed_policy_cannot_be_applied_to_another_owner(tmp_path):
    from openinvest.services.source_reporting import ReportingPolicy
    source,target=tmp_path/'source',tmp_path/'target'
    reviewed_batch(source,1);reviewed_batch(target,0)
    reader=EventStore(str(source/'db/events.db'))
    try:
        with pytest.raises(ValueError,match='another data root'):
            reader.get_event_with_sources('public-0',policy=ReportingPolicy(target))
    finally:reader.conn.close()


def test_windows_identity_checks_bytes_when_ctime_cannot_prove_a_change(tmp_path,monkeypatch):
    import sys
    from openinvest.services.event_replication import _GovernanceReads
    directory=tmp_path/'integrity';directory.mkdir()
    path=directory/'eligibility.json';path.write_text('first')
    fixed=path.stat();stat=Path.stat
    with monkeypatch.context() as scoped:
        scoped.setattr(sys,'platform','win32')
        scoped.setattr(Path,'stat',lambda self,*a,**kw:fixed if self==path else stat(self,*a,**kw))
        first=_GovernanceReads.identity(tmp_path)
        path.write_text('other')
        assert _GovernanceReads.identity(tmp_path)!=first
