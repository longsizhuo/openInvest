from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
import sys

import pytest

from openinvest.db.event_store import EventStore
from openinvest.services.news_sources import RawNewsItem
from openinvest.services.event_normalizer import normalize,NormalizedEvent
from tests.integrity_helpers import reviewed_event_storage


def source(published='2026-11-03T10:00:00+00:00',snippet='Original retained source summary'):
    return RawNewsItem(src_name='ddgs:example.invalid',url='https://example.invalid/story',
        title='Source title',snippet=snippet,published_at=published)


def event(claim='Normalized opinion',eid=None):
    value={'one_line_claim':claim,'event_type':'macro','stance':'risk','severity':'mid',
        'ts':'2026-11-03T10:00:00+00:00','entities':['us_rates'],'affected_symbols':['SYNTHETIC']}
    if eid:value['event_id']=eid
    return value


def test_metadata_only_repeat_uses_no_model_and_preserves_lineage(tmp_path,monkeypatch,reviewed_event_storage):
    store=EventStore(str(tmp_path/'db/events.db'))
    first=store.write_news_observation(event(),source())['event']
    later=source('2026-11-03T10:30:00+00:00')
    monkeypatch.setattr('openinvest.services.event_normalizer._call_flash_batch',lambda *a,**k:pytest.fail('same input billed again'))
    reused=normalize([later],reuse_store=store,skip_embedding=True)
    assert len(reused)==1 and reused[0].reused
    result=store.write_news_observation(reused[0].event,later,reuse_extraction=True)
    current=result['event']
    assert not result['changed'] and current['event_id']==first['event_id']
    assert current['_analysis_cause_id']==first['_analysis_cause_id']
    assert current['_analysis_version_id']==first['_analysis_version_id']
    assert current['_version_id']>first['_version_id'] and current['ts']==first['ts']
    assert store.conn.execute('SELECT count(*) FROM event_versions').fetchone()[0]>=2


def test_actual_content_change_still_calls_normalizer(tmp_path,monkeypatch,reviewed_event_storage):
    store=EventStore(str(tmp_path/'db/events.db'))
    first=store.write_news_observation(event(),source())['event']
    calls=[]
    def model(items,**kwargs):
        calls.append(items)
        return [NormalizedEvent(0,event('Actual corrected source statement'))]
    monkeypatch.setattr('openinvest.services.event_normalizer._call_flash_batch',model)
    changed=source(snippet='New factual statement; prior report corrected')
    values=normalize([changed],reuse_store=store,api_key='synthetic-key',skip_embedding=True)
    assert len(calls)==1 and not values[0].reused
    result=store.write_news_observation(values[0].event,changed)
    assert result['changed'] and result['event']['_analysis_cause_id']!=first['_analysis_cause_id']


def test_existing_unverified_extraction_cannot_be_promoted_by_reuse(tmp_path):
    store=EventStore(str(tmp_path/'db/events.db'))
    store.write_news_observation(event('Unverified host judgment'),source())
    assert store.normalization_input(source())['status']=='source_verification_required'
    with pytest.raises(ValueError,match='qualified_identical_input'):
        store.write_news_observation(event(),source(),reuse_extraction=True)


def test_duplicate_ids_and_channel_metadata_share_one_cause(tmp_path,reviewed_event_storage):
    store=EventStore(str(tmp_path/'db/events.db'))
    for ident in ('first','second'):
        store.upsert_event(event(eid=ident))
        store.add_source(ident,src_name='provider',url='https://example.invalid/story',title='Source title',snippet='same observed summary')
    first=store.get_event('first');second=store.get_event('second')
    assert first['event_id']!=second['event_id'] and first['_analysis_cause_id']==second['_analysis_cause_id']


@pytest.mark.parametrize('message,expected_error',[('No results found.',False),('No results found. timed out',True)])
def test_query_local_empty_ddgs_is_not_a_provider_outage(monkeypatch,message,expected_error):
    from openinvest.services.news_sources.ddgs_news import fetch_ddgs_news
    class DDGSException(Exception):pass
    class Fake:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def news(self,*args,**kwargs):raise DDGSException(message)
    monkeypatch.setitem(sys.modules,'ddgs',SimpleNamespace(DDGS=Fake))
    result=fetch_ddgs_news('SYNTHETIC news')
    assert bool(result.errors) is expected_error


@pytest.mark.parametrize('index',[-1,1,True,'0'])
def test_invalid_normalizer_index_never_borrows_a_real_source(index):
    import json
    from openinvest.services.event_normalizer import _parse_events_json
    with pytest.raises(ValueError,match='no_bound_source_index'):
        _parse_events_json(json.dumps({'events':[{'idx':index,'one_line_claim':'Unbound interpretation'}]}),expected_size=1,offset=0)
