"""Scheduled scripts use shared model primitives once; no host-agent memory or synthesis."""
from datetime import datetime,timezone,timedelta
import pytest

from openinvest.jobs import fixed_invest as jobs


@pytest.fixture
def owner(tmp_path,monkeypatch):
    monkeypatch.setattr('openinvest.core.memory_store.MEMORY_ROOT',tmp_path/'memory')
    monkeypatch.setattr('openinvest.paths.INVEST_ROOT',tmp_path)
    monkeypatch.setenv('INVEST_REASONING_BY','backend')
    return tmp_path


def test_daily_uses_shared_backend_once_and_skips_optional_model_calls(owner,monkeypatch):
    calls=[]
    monkeypatch.setattr('openinvest.jobs.daily_report.run',lambda **kwargs:calls.append(kwargs) or {'status':'degraded','verdicts':{}})
    assert jobs.daily(send_email=False)['status']=='degraded'
    assert jobs.daily(send_email=False)['status']=='degraded'
    assert calls==[{'send_email':False,'include_report':True,'max_debate_rounds':2,'optional_model_reviews':False}]


def test_uncertain_interrupted_daily_is_not_regenerated_automatically(owner,monkeypatch):
    calls=[]
    def interrupted(**kwargs):calls.append('started');raise RuntimeError('synthetic crash after a potentially paid operation')
    monkeypatch.setattr('openinvest.jobs.daily_report.run',interrupted)
    with pytest.raises(RuntimeError):jobs.daily(send_email=False)
    result=jobs.daily(send_email=False)
    assert result['status']=='recovery_required' and result['new_model_calls']==0
    assert calls==['started']


def test_script_refuses_host_agent_reasoning_mode(owner,monkeypatch):
    monkeypatch.setenv('INVEST_REASONING_BY','agent')
    with pytest.raises(ValueError,match='backend reasoning'):jobs.daily(send_email=False)


def test_restore_barrier_precedes_every_model_or_collection_call(owner,monkeypatch):
    marker=owner/'integrity/restore_pending.json';marker.parent.mkdir();marker.write_text('{}')
    monkeypatch.setattr('openinvest.jobs.daily_report.run',lambda **kwargs:pytest.fail('no model work before reconciliation'))
    monkeypatch.setattr('openinvest.services.news_collection.collect',lambda **kwargs:pytest.fail('no capture work before reconciliation'))
    assert jobs.daily(send_email=False)['reason']==jobs.sentinel()['reason']=='restore_pending'


def test_actual_daily_entry_can_disable_auxiliary_models_without_skipping_committee(monkeypatch):
    from tests.test_holdings_digest import _run_daily_report
    from openinvest.jobs import daily_report as daily
    original=daily.run;calls=[]
    def execute(**kwargs):
        monkeypatch.setattr(daily,'_run_gemini_cli_review',lambda *args:pytest.fail('optional reviewer must not run'))
        monkeypatch.setattr('openinvest.capabilities.sdk_agent.SDKAgent',lambda *args,**kwargs:pytest.fail('optional translator must not run'))
        kwargs.update(optional_model_reviews=False,max_debate_rounds=2)
        calls.append('entry')
        return original(**kwargs)
    monkeypatch.setattr(daily,'run',execute)
    result=_run_daily_report(monkeypatch,lambda *args,**kwargs:{'status':'ok','markdown':'synthetic digest'})
    assert calls==['entry'] and 'TEST.AX' in result['verdicts']


def test_sentinel_does_not_pay_to_normalize_unqualified_or_out_of_window_data(owner,monkeypatch):
    now=datetime.now(timezone.utc)
    def row(ident,when):return {'source_snapshot_id':ident,'source':'synthetic','url':'https://example.invalid/'+ident,
        'title':ident,'snippet':'source text','fetched_at':when.isoformat(),'published_at':when.isoformat(),'raw_meta':{'response_capture_id':'synthetic-only'}}
    items=[row('unknown',now),row('old',now-timedelta(days=60)),row('current',now)]
    monkeypatch.setattr('openinvest.services.news_collection.collect',lambda **kwargs:{'status':'ok','model_calls':0})
    monkeypatch.setattr('openinvest.services.agent_queue.pending_news_items',lambda **kwargs:items)
    monkeypatch.setattr('openinvest.services.source_reporting.reporting_use',lambda root,item:{'allowed':item['title']!='unknown'})
    sent=[]
    monkeypatch.setattr('openinvest.services.event_ingest.ingest_events',lambda data,**kwargs:sent.extend(data) or {'status':'ok','ingested':len(data)})
    monkeypatch.setattr('openinvest.jobs.event_watch.run',lambda:pytest.fail('RSS/search producer must not run twice'))
    result=jobs.sentinel()
    assert [row['source_snapshot_id'] for row in sent]==['current']
    assert result['unqualified_sources_not_sent_to_models']==1 and result['historical_sources_retained_for_audit']==1
    assert result['host_agent_calls']==0


def test_daily_retries_delivery_without_regenerating_models_and_keeps_channel_receipts(owner,monkeypatch):
    from openinvest.services.notification_outbox import NotificationOutbox
    calls=[];deliveries=[]
    monkeypatch.setattr('openinvest.jobs.daily_report.run',lambda **kwargs:calls.append(kwargs) or
        {'status':'success','date':'synthetic-date','verdicts':{},'full_report':'Synthetic test report'})
    def send(channel,payload):
        deliveries.append(channel)
        return {'accepted':channel=='email','status':'accepted' if channel=='email' else 'unknown','target':'synthetic-only'}
    monkeypatch.setattr('openinvest.services.notification_outbox.send_channel',send)
    first=jobs.daily();second=jobs.daily()
    assert first['status']==second['status']=='success' and len(calls)==1
    assert first['delivery']['channels']['discord']['status']=='unknown'
    assert second['new_model_calls']==0 and deliveries==['discord','email']
    assert NotificationOutbox().receipt(first['delivery']['notification_id'])['accepted']


def test_corrupt_daily_receipt_is_not_treated_as_permission_for_another_paid_run(owner,monkeypatch):
    path=owner/'memory/.state'/f'fixed_invest_daily_{datetime.now().date().isoformat()}.json'
    path.parent.mkdir(parents=True);path.write_text('{truncated')
    monkeypatch.setattr('openinvest.jobs.daily_report.run',lambda **kwargs:pytest.fail('corrupt receipt must not regenerate models'))
    with pytest.raises(ValueError):jobs.daily()


def test_daily_no_delivery_is_a_failure_but_can_retry_only_delivery(owner,monkeypatch):
    calls=[]
    monkeypatch.setattr('openinvest.jobs.daily_report.run',lambda **kwargs:calls.append(kwargs) or
        {'status':'success','date':'synthetic-date','verdicts':{},'full_report':'Synthetic report'})
    monkeypatch.setattr('openinvest.services.notification_outbox.send_channel',lambda *args:False)
    assert jobs.daily()['status']==jobs.daily()['status']=='degraded'
    assert len(calls)==1


def test_discord_summary_does_not_prove_delivery_of_full_daily_report(owner,monkeypatch):
    monkeypatch.setattr('openinvest.jobs.daily_report.run',lambda **kwargs:
        {'status':'success','date':'synthetic-date','verdicts':{},'full_report':'Synthetic full body'})
    monkeypatch.setattr('openinvest.services.notification_outbox.send_channel',lambda channel,payload:channel=='discord')
    result=jobs.daily()
    assert result['status']=='degraded' and result['delivery']['accepted']
    assert result['failure_reasons']==['daily_report_email_body_not_accepted']


def test_actual_captured_old_publication_is_not_a_new_market_fact(owner,monkeypatch):
    from openinvest.services import news_collection as collection
    monkeypatch.setattr(collection,'SOURCES',('sina:152',))
    now=datetime.now(timezone.utc);old=now-timedelta(days=60)
    body=__import__('json').dumps({'result':{'data':{'feed':{'list':[
        {'id':'old-story','create_time':old.astimezone(collection.CST).strftime('%Y-%m-%d %H:%M:%S'),'rich_text':'Old original publication'},
        {'id':'boundary','create_time':(old-timedelta(days=4)).astimezone(collection.CST).strftime('%Y-%m-%d %H:%M:%S'),'rich_text':'Outside interval'}]}}}}).encode()
    original=collection.collect
    monkeypatch.setattr(collection,'collect',lambda **kwargs:original(**{**kwargs,'since':(old-timedelta(days=2)).isoformat(),'now':now,
        'fetcher':lambda *args:('https://source.invalid/feed',body)}))
    monkeypatch.setattr('openinvest.services.event_ingest.ingest_events',lambda *args,**kwargs:pytest.fail('old first-observed report stays audit-only, not paid current normalization'))
    result=jobs.sentinel()
    assert result['historical_sources_retained_for_audit']==1


def test_actual_captured_batch_is_not_paid_again_after_interruption_or_partial_completion(owner,monkeypatch):
    from tests.test_news_source_snapshots import captured_sina
    from openinvest.services import agent_queue
    item=captured_sina(monkeypatch)
    monkeypatch.setattr('openinvest.services.news_collection.collect',lambda **kwargs:{'status':'ok'})
    calls=[]
    def interrupted(items,**kwargs):calls.append(items);raise RuntimeError('synthetic interruption after paid normalization')
    monkeypatch.setattr('openinvest.services.event_ingest.ingest_events',interrupted)
    with pytest.raises(RuntimeError):jobs.sentinel()
    result=jobs.sentinel()
    assert len(calls)==1 and result['status']=='degraded' and result['unresolved_sources_not_regenerated']==1
    assert agent_queue.pending_news_items()[0]['source_snapshot_id']==item['source_snapshot_id']


def test_partial_success_does_not_claim_pending_sources_completed(owner,monkeypatch):
    from tests.test_news_source_snapshots import captured_sina
    captured_sina(monkeypatch)
    monkeypatch.setattr('openinvest.services.news_collection.collect',lambda **kwargs:{'status':'ok'})
    calls=[]
    monkeypatch.setattr('openinvest.services.event_ingest.ingest_events',lambda *args,**kwargs:calls.append(args) or {'status':'ok','ingested':0})
    result=jobs.sentinel()
    assert result['status']=='degraded' and result['unresolved_sources_not_regenerated']==1
    assert jobs.sentinel()['status']=='degraded' and len(calls)==1


def test_sentinel_real_canonical_queue_contract_without_permissive_stub(owner,monkeypatch):
    monkeypatch.setattr('openinvest.services.news_collection.collect',lambda **kwargs:{'status':'ok','model_calls':0})
    monkeypatch.setattr('openinvest.services.event_ingest.ingest_events',lambda *args,**kwargs:pytest.fail('empty queue must not invoke normalization'))
    assert jobs.sentinel()['status']=='ok'


def test_source_failure_exits_nonzero_for_native_failure_delivery(owner,monkeypatch,capsys):
    monkeypatch.setattr('openinvest.services.news_collection.collect',lambda **kwargs:{'status':'coverage_incomplete','model_calls':0})
    monkeypatch.setattr('sys.argv',['fixed_invest','sentinel'])
    with pytest.raises(SystemExit) as stopped:jobs.main()
    assert stopped.value.code==2 and 'source_coverage_incomplete_or_uninitialized' in capsys.readouterr().out
