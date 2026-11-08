from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from openinvest.core.config import set_config_override
from openinvest.core.memory_store import MemoryStore
from openinvest.services import email_budget

NOW = datetime(2026, 11, 8, 12, tzinfo=timezone.utc)


def reserve(store, ident, **kwargs):
    return email_budget.reserve(receiver='owner@example.invalid', subject='summary', plain_body=ident,
        notification_id=ident, store=store, now=NOW, **kwargs)


def test_all_senders_share_atomic_recipient_budget(tmp_path):
    set_config_override({'event':{'email_daily_cap':2}})
    store = MemoryStore(tmp_path/'memory')
    def try_one(index):
        try:
            return reserve(store, str(index))[1]
        except email_budget.EmailDeferred:
            return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        admitted = [x for x in pool.map(try_one, range(31)) if x]
    assert len(admitted) == 2


def test_accepted_duplicate_and_uncertain_attempt_are_not_resent(tmp_path):
    store = MemoryStore(tmp_path/'memory')
    store, admission = reserve(store, 'one')
    email_budget.finish(store, admission, 'accepted')
    assert reserve(store, 'one')[1]['already_accepted']
    _, second = reserve(store, 'two')
    email_budget.finish(store, second, 'unknown')
    with pytest.raises(email_budget.EmailAcceptanceUnknown):
        reserve(store, 'two')


def test_daily_policy_and_risk_identity(tmp_path):
    set_config_override({'event':{'email_mode':'daily_and_risk'}})
    store = MemoryStore(tmp_path/'memory')
    for category in ('job_watchdog', 'decision_quality', 'event', 'market_risk'):
        with pytest.raises(email_budget.EmailDeferred):
            reserve(store, category, category=category)
    _, daily = reserve(store, 'daily', category='daily_report', report_date=NOW.astimezone().date().isoformat())
    email_budget.finish(store, daily, 'accepted')
    with pytest.raises(email_budget.EmailDeferred):
        reserve(store, 'second-daily', category='daily_digest', report_date=NOW.astimezone().date().isoformat())
    _, risk = reserve(store, 'risk', category='market_risk', urgency_key='price:fall:high')
    email_budget.finish(store, risk, 'accepted')
    with pytest.raises(email_budget.EmailDeferred):
        reserve(store, 'paraphrased-risk', category='market_risk', urgency_key='price:fall:high')
    assert reserve(store, 'new-risk', category='market_risk', urgency_key='price:other:high')[1]


def test_muting_email_does_not_claim_delivery(tmp_path):
    set_config_override({'event':{'email_enabled':False}})
    store = MemoryStore(tmp_path/'memory')
    with pytest.raises(email_budget.EmailDeferred):
        reserve(store, 'one')
    assert not store.state_get(email_budget.STATE)


def test_actual_smtp_entry_obeys_shared_budget(monkeypatch):
    from openinvest.services import notifier
    set_config_override({'event':{'email_daily_cap':2}})
    monkeypatch.setenv('EMAIL_SENDER','sender@example.invalid')
    monkeypatch.setenv('EMAIL_PASSWORD','synthetic-password')
    sent=[]
    class SMTP:
        def __init__(self,*args,**kwargs):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def ehlo(self):pass
        def starttls(self,**kwargs):pass
        def login(self,*args):pass
        def sendmail(self,*args):sent.append(args)
    monkeypatch.setattr(notifier.smtplib,'SMTP',SMTP)
    for index in range(2):
        assert notifier.send_email_html(subject=str(index),html_body='facts',plain_body='facts',receiver='owner@example.invalid')
    with pytest.raises(email_budget.EmailDeferred):
        notifier.send_email_html(subject='overflow',html_body='facts',plain_body='facts',receiver='owner@example.invalid')
    assert len(sent)==2


def test_corrupt_saved_email_policy_cannot_fall_back_to_sending(tmp_path):
    store=MemoryStore(tmp_path/'memory')
    (store.root/'.state').mkdir(parents=True)
    (store.root/'.state/config_overrides.json').write_text('{broken')
    with pytest.raises(email_budget.EmailDeferred,match='email_policy_unreadable'):
        reserve(store,'one')


@pytest.mark.parametrize('phase',['accepted','unknown'])
def test_identity_protection_outlives_the_24h_budget_window(tmp_path,phase):
    store=MemoryStore(tmp_path/'memory')
    _, admission=reserve(store,'permanent')
    email_budget.finish(store,admission,phase)
    kwargs=dict(receiver='owner@example.invalid',subject='summary',plain_body='permanent',
                notification_id='permanent',store=store,now=NOW+timedelta(days=2))
    if phase=='accepted':
        assert email_budget.reserve(**kwargs)[1]['already_accepted']
    else:
        with pytest.raises(email_budget.EmailAcceptanceUnknown):
            email_budget.reserve(**kwargs)


def test_old_daily_cannot_consume_todays_summary_slot(tmp_path):
    set_config_override({'event':{'email_mode':'daily'}})
    store=MemoryStore(tmp_path/'memory')
    with pytest.raises(email_budget.EmailDeferred,match='report_day_unverified_or_stale'):
        reserve(store,'old-report',category='daily_report',report_date=(NOW-timedelta(days=1)).date().isoformat())
    assert reserve(store,'current-report',category='daily_report',report_date=NOW.astimezone().date().isoformat())[1]


def test_deferred_email_is_retained_with_retry_time(tmp_path, monkeypatch):
    from openinvest.services.notification_outbox import NotificationOutbox
    box = NotificationOutbox(MemoryStore(tmp_path/'memory'))
    ident = box.enqueue('one', {'subject':'x','plain':'x','html':'x','discord':'x'},category='job_watchdog',policy='decision',now=NOW)
    def sender(channel, payload):
        if channel == 'email':
            raise email_budget.EmailDeferred('summary_only', NOW.timestamp()+3600)
        return True
    box.flush(now=NOW, sender=sender)
    receipt = box.receipt(ident)
    assert receipt['channels']['email']['status'] == 'deferred'
    assert receipt['channels']['discord']['status'] == 'accepted'
    with box.connection() as db:
        assert db.execute("SELECT retry_at FROM deliveries WHERE notification_id=? AND channel='email'",(ident,)).fetchone()[0] >= NOW.timestamp()+3600


def test_cooldown_cannot_use_discord_receipt_to_suppress_email(tmp_path):
    from openinvest.services.notification_outbox import NotificationOutbox
    box = NotificationOutbox(MemoryStore(tmp_path/'memory'))
    payload={'subject':'x','plain':'x','html':'x','discord':'x','cooldown':{'domain':'condition','seconds':86400}}
    box.enqueue('first',payload,category='market_risk',policy='decision',now=NOW)
    box.flush(now=NOW,sender=lambda channel,payload: channel=='discord')
    second=box.enqueue('second',payload,category='market_risk',policy='decision',now=NOW+timedelta(minutes=1))
    calls=[]
    box.flush(only_id=second,now=NOW+timedelta(minutes=1),sender=lambda channel,payload:calls.append(channel) or True)
    assert calls==['email']
    assert box.receipt(second)['channels']['email']['status']=='accepted'


def test_31_internal_findings_are_one_private_summary(tmp_path, monkeypatch):
    from openinvest.jobs import job_watchdog
    from openinvest.scheduler import runner
    from openinvest.services import decision_quality
    import sqlite3
    store=MemoryStore(tmp_path/'memory')
    db=tmp_path/'jobs.sqlite'
    with sqlite3.connect(db) as connection:
        connection.execute('CREATE TABLE job_runs(id INTEGER, job_name TEXT, started_at TEXT, finished_at TEXT, status TEXT, error TEXT, output_excerpt TEXT)')
    monkeypatch.setattr(runner,'RUN_LOG_DB',db)
    monkeypatch.setattr(runner,'_load_job_configs',lambda:[])
    monkeypatch.setattr(job_watchdog,'MemoryStore',lambda:store)
    monkeypatch.setattr(job_watchdog,'find_failed_verdicts',lambda *args:[])
    monkeypatch.setattr(job_watchdog,'find_agent_backlog',lambda *args:[])
    findings=[{'key':f'quality:plan_parent:{i}','since':NOW-timedelta(hours=1),'one_shot':True,'text':'PRIVATE_CONFIG jobs.sqlite secret error'} for i in range(31)]
    monkeypatch.setattr(decision_quality,'findings',lambda *args,**kwargs:findings)
    sent=[]
    monkeypatch.setattr('openinvest.services.notification_outbox.send_channel',lambda channel,payload:sent.append((channel,payload)) or True)
    result=job_watchdog.run(now=NOW)
    assert len(sent)==2 and len(result['notifications_queued'])==31
    assert all('PRIVATE_CONFIG' not in row[1]['plain'] and 'jobs.sqlite' not in row[1]['plain'] for row in sent)
    findings[:]=[{**row,'key':row['key']+'-changed'} for row in findings]
    job_watchdog.run(now=NOW+timedelta(hours=1))
    assert len(sent)==2
    assert len(job_watchdog.run(now=NOW+timedelta(hours=25))['notifications_queued'])==31


def test_news_mail_does_not_publish_unverified_units_or_cost(tmp_path, monkeypatch):
    from openinvest.services import event_trigger, event_notifier
    holding={'symbol':'SYNTHETIC','units':900,'avg_cost':100}
    pm=SimpleNamespace(holdings=SimpleNamespace(find=lambda symbol:holding))
    monkeypatch.setattr('openinvest.core.portfolio_manager.PortfolioManager',lambda:pm)
    monkeypatch.setattr('openinvest.services.ledger_evidence.account_source_status',lambda pm:{'content_sha256':'same','holdings':{'SYNTHETIC':{'current_quantity':'estimated_or_proxy_unreconciled'}}})
    monkeypatch.setattr('openinvest.utils.quotes.get_quote',lambda *args:pytest.fail('must not quote unverified native units'))
    snapshot=event_trigger._holdings_snapshot(['SYNTHETIC'])
    assert snapshot['SYNTHETIC']['units'] is None
    text=event_notifier._build_markdown([{'affected_symbols':['SYNTHETIC'],'one_line_claim':'news','sources':[]}],committee_task_id=None,api_base_url='http://localhost',holdings_snapshot=snapshot)
    assert '900' not in text and 'pnl=' not in text and '尚待核对' in text


def test_old_queued_news_loses_account_numbers_but_retains_market_alert(tmp_path,monkeypatch):
    from openinvest.services.notification_outbox import NotificationOutbox
    box=NotificationOutbox(MemoryStore(tmp_path/'memory'))
    event={'event_id':'synthetic','_version_id':1,'stance':'risk','severity':'high',
           'affected_symbols':['SYNTHETIC'],'one_line_claim':'Observed market risk','sources':[]}
    monkeypatch.setattr('openinvest.db.event_store.EventStore',lambda *args:SimpleNamespace(
        get_event=lambda eid:{'_version_id':1},conn=SimpleNamespace(close=lambda:None)))
    original=box.enqueue('old-news',{'subject':'old','plain':'units=900 pnl=+25.00%',
        'html':'units=900 pnl=+25.00%','discord':'risk','events':[event]},category='market_risk',policy='decision',now=NOW)
    sent=[]
    box.flush(now=NOW,sender=lambda channel,payload:sent.append(payload) or True)
    assert sent==[]
    box.flush(now=NOW,sender=lambda channel,payload:sent.append(payload) or True)
    assert len(sent)==2
    assert all('900' not in p['plain'] and 'pnl=' not in p['plain'] for p in sent)
    assert all(p['_notification_category']=='market_risk' and p['events']==[event] for p in sent)
    assert all(p['supersedes_notification']==original for p in sent)


@pytest.mark.parametrize('email_phase',['accepted','unknown'])
def test_sanitizing_old_news_does_not_resend_confirmed_or_unknown_email(tmp_path,monkeypatch,email_phase):
    from openinvest.services.notification_outbox import NotificationOutbox
    box=NotificationOutbox(MemoryStore(tmp_path/'memory'))
    event={'event_id':'synthetic','_version_id':1,'stance':'risk','severity':'high',
           'affected_symbols':['SYNTHETIC'],'one_line_claim':'market risk','sources':[]}
    monkeypatch.setattr('openinvest.db.event_store.EventStore',lambda *args:SimpleNamespace(
        get_event=lambda eid:{'_version_id':1},conn=SimpleNamespace(close=lambda:None)))
    parent=box.enqueue('legacy',{'subject':'old','plain':'units=900','html':'units=900',
        'discord':'risk','events':[event]},category='market_risk',policy='decision',now=NOW)
    with box.connection() as db:
        db.execute("UPDATE deliveries SET status=? WHERE notification_id=? AND channel='email'",(email_phase,parent))
    sent=[]
    box.flush(now=NOW,sender=lambda channel,payload:sent.append(channel) or True)
    box.flush(now=NOW,sender=lambda channel,payload:sent.append(channel) or True)
    assert sent==['discord']
