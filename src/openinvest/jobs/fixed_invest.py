"""Script-only scheduling: model judgment stays in the shared backend committee."""
from __future__ import annotations

import argparse
from datetime import datetime,timezone,timedelta
import json,os

from openinvest.core.memory_store import MemoryStore,_file_lock


def _backend_mode():
    from openinvest.utils.llm import reasoning_by_agent
    if reasoning_by_agent():raise ValueError('Fixed investment tasks require configured backend reasoning')


def _receipt(store,key):
    path=store.root/'.state'/f'{key}.json'
    if not path.exists():return None
    value=json.loads(path.read_text())
    if not isinstance(value,dict) or not value.get('phase'):raise ValueError('Scheduled attempt receipt requires reconciliation')
    return value


def _daily_delivery(store, key, receipt, *, reused=True):
    """A saved report is delivered through channel receipts, never regenerated."""
    from openinvest.services.notification_outbox import NotificationOutbox
    from openinvest.services.notifier import render_markdown_email
    result=receipt['result'];report=result.get('full_report')
    if not report:return {**result,'status':'degraded','failure_reasons':['daily_report_body_missing'],'new_model_calls':0}
    box=NotificationOutbox(store)
    from openinvest.services.email_budget import summary as delivery_summary
    report += delivery_summary(store)
    summary=[f"投资委员会日报 {result.get('date','')}（摘要）",f"报告状态：{result['status']}"]
    refs=[]
    for symbol,verdict in result.get('verdicts',{}).items():
        summary.append(f"{symbol}: {verdict.get('verdict','UNCLEAR')}；资格 {verdict.get('decision_status','unverified')}；核验 {(verdict.get('reasoning_review') or {}).get('status','not_provided')}")
        if verdict.get('current_advice_valid'):
            refs.append({'symbol':symbol,'decision_id':verdict.get('decision_id')})
    summary.append('完整正文的邮件投递另有独立回执；未通过核验的提案仅供审计。')
    ident=box.enqueue(key,{'subject':f"openInvest 投资委员会日报 {result.get('date','')}",
        'report_date':result.get('date'),
        'plain':report,'html':render_markdown_email(report,footer_label='Investment Committee'),
        'discord':'\n'.join(summary),'decision_refs':refs},category='daily_report',policy='decision')
    box.flush(only_id=ident)
    delivery=box.receipt(ident)
    store.state_update(key,lambda old:({**old,'delivery':delivery},None))
    status=result['status']
    reasons=[]
    email_state=(delivery['channels'].get('email') or {}).get('status')
    if email_state not in ('accepted','deferred'):
        status='degraded';reasons.append('daily_report_email_body_not_accepted')
    answer={**{k:v for k,v in result.items() if k!='full_report'},'status':status,
            'delivery':delivery,'failure_reasons':reasons,'model_work_reused':reused}
    answer['email_deferred_to_user_policy'] = email_state == 'deferred'
    if reused:answer['new_model_calls']=0
    return answer


def daily(*,send_email=True):
    """One scheduled daily run per local date; uncertain runs require inspection."""
    _backend_mode();store=MemoryStore();day=datetime.now().date().isoformat()
    if (store.data_root/'integrity/restore_pending.json').exists():return {'status':'blocked','reason':'restore_pending','new_model_calls':0}
    key='fixed_invest_daily_'+day
    with _file_lock(store.root/'.state/fixed_invest_daily'):
        previous=_receipt(store,key)
        if previous is not None:
            if previous.get('phase')!='done':
                return {'status':'recovery_required','date':day,'new_model_calls':0,
                        'reason':'previous_run_outcome_unknown_no_automatic_regeneration'}
            if send_email:return _daily_delivery(store,key,previous)
            return {**{k:v for k,v in previous['result'].items() if k!='full_report'},'new_model_calls':0}
        store.state_update(key,lambda old:({'phase':'started','date':day,'started_at':datetime.now(timezone.utc).isoformat(),'pid':os.getpid()},None))
        from openinvest.jobs.daily_report import run
        result=run(send_email=False,include_report=True,max_debate_rounds=2,optional_model_reviews=False)
        receipt=store.state_update(key,lambda old:({**old,'phase':'done','finished_at':datetime.now(timezone.utc).isoformat(),'result':result},
                                                  {**old,'phase':'done','result':result}))
        if send_email:return _daily_delivery(store,key,receipt,reused=False)
        return {k:v for k,v in result.items() if k!='full_report'}


def sentinel(*,since=None,max_news=20):
    """Capture sources and normalize a bounded current batch, without a host agent."""
    _backend_mode();store=MemoryStore()
    if (store.data_root/'integrity/restore_pending.json').exists():return {'status':'blocked','reason':'restore_pending','new_model_calls':0}
    if isinstance(max_news,bool) or not isinstance(max_news,int) or not 1<=max_news<=100:raise ValueError('max_news must be between 1 and 100')
    with _file_lock(store.root/'.state/fixed_invest_sentinel'):
        from openinvest.services.news_collection import collect
        coverage=collect(store=store,since=since)
        from openinvest.services.agent_queue import pending_news_items
        from openinvest.services.source_reporting import from_input,reporting_use
        from openinvest.core.config import load_config
        now=datetime.now(timezone.utc);cutoff=now-timedelta(days=load_config().event.rag_window_days)
        pending=pending_news_items(memory_root=store.root,include_expired=True)
        eligible=[];unqualified=0;historical=0;time_unknown=0;unresolved=0
        for item in pending:
            if not (item.get('raw_meta') or {}).get('response_capture_id'):continue
            source={**from_input(item),'provenance_json':json.dumps({**item.get('raw_meta',{}),'source_snapshot_id':item['source_snapshot_id']})}
            if not reporting_use(store.data_root,source)['allowed']:unqualified+=1;continue
            acquired=datetime.fromisoformat(item['fetched_at'].replace('Z','+00:00'))
            if acquired.tzinfo is None:raise ValueError('Source acquisition timezone unavailable')
            if acquired<cutoff:historical+=1;continue
            # Capturing an old article now does not make its publication current.
            # These API decoders have no verified revision timestamp: keep old or
            # unknown publications for audit/review, including possible corrections.
            # Never infer a recent revision merely from a mutable raw_meta field.
            try:
                published=datetime.fromisoformat(str(item.get('published_at')).replace('Z','+00:00'))
                if published.tzinfo is None or published>acquired:raise ValueError('publication time unavailable')
            except (ValueError,TypeError):time_unknown+=1;continue
            if published<cutoff:historical+=1;continue
            attempt_key='fixed_invest_source_'+item['source_snapshot_id']
            if _receipt(store,attempt_key) is not None:unresolved+=1;continue
            eligible.append(item)
        eligible.sort(key=lambda item:item['fetched_at'])
        selected=eligible[:max_news]
        outcome={'status':'ok','ingested':0}
        if selected:
            from openinvest.services.event_ingest import ingest_events
            for item in selected:
                store.state_set('fixed_invest_source_'+item['source_snapshot_id'],{'phase':'started','started_at':now.isoformat(),
                    'source_snapshot_id':item['source_snapshot_id']})
            # Claim before a potentially paid call. Interrupted/partial batches
            # stay visible for reconciliation, never automatic paid regeneration.
            outcome=ingest_events([{'title':item['title'],'url':item['url'],'source_snapshot_id':item['source_snapshot_id']} for item in selected],ingested_by='scheduled-backend')
            still_pending={item['source_snapshot_id'] for item in pending_news_items(memory_root=store.root,include_expired=True)}
            for item in selected:
                pending_id=item['source_snapshot_id']
                phase='unresolved' if pending_id in still_pending else 'acknowledged'
                store.state_update('fixed_invest_source_'+pending_id,lambda old,phase=phase:({**old,'phase':phase,'result_status':outcome.get('status')},None))
                if phase=='unresolved':unresolved+=1
        # The main scheduler owns RSS/search adapters and the independent price
        # sentinel. This task owns only the captured provider queue above.
        status='ok' if coverage.get('status')=='ok' and outcome.get('status')=='ok' and not unresolved and not time_unknown else 'degraded'
        reasons=[]
        if coverage.get('status')!='ok':reasons.append('source_coverage_incomplete_or_uninitialized')
        if outcome.get('status')!='ok':reasons.append('source_normalization_failed')
        if unresolved:reasons.append('source_normalization_requires_reconciliation_no_automatic_regeneration')
        if time_unknown:reasons.append('source_publication_time_unknown_requires_review')
        return {'status':status,'coverage':coverage,'normalized':outcome,'failure_reasons':reasons,
                'remaining_current_sources':max(0,len(eligible)-len(selected)),
                'unqualified_sources_not_sent_to_models':unqualified,'historical_sources_retained_for_audit':historical,
                'unknown_publication_time_retained_for_review':time_unknown,'unresolved_sources_not_regenerated':unresolved,
                'host_agent_calls':0}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('task',choices=['daily','sentinel']);parser.add_argument('--since')
    parser.add_argument('--no-email',action='store_true');args=parser.parse_args()
    result=daily(send_email=not args.no_email) if args.task=='daily' else sentinel(since=args.since)
    print(json.dumps(result,ensure_ascii=False,default=str))
    if result.get('status') not in {'ok','success'}:raise SystemExit(2)


if __name__=='__main__':main()
