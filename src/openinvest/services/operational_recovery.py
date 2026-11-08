"""Evidence-bound operational closure. Never grants financial qualification."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import sqlite3

from openinvest.core.evidence_eligibility import digest
from openinvest.core.memory_store import MemoryStore, append_jsonl


def _text(source):
    return {key:source.get(key) or '' for key in ('src_name','url','title','snippet','raw_text')}


def bind_confirmed_content_dispatches(store, *, apply=False, now=None):
    """Reviewed upgrade bridge: exact stored input versions, dispatch not success.

    Old integer event versions are compared with their actual stored source
    snapshots. A changing timestamp/model paraphrase does not cause another
    automatic paid submission of the identical retained input.
    """
    from openinvest.db.event_store import EventStore
    from openinvest.services.news_identity import analysis_identity
    from openinvest.services.source_reporting import reporting_use
    from openinvest.core.evidence_eligibility import Eligibility
    now=now or datetime.now(timezone.utc)
    from openinvest.utils.advisory import is_advisory_mode
    if is_advisory_mode():raise ValueError('operational_reconciliation_owner_only')
    if apply:
        from openinvest.services.notification_outbox import NotificationOutbox
        NotificationOutbox(store)
    path=store.data_root/'db/notification_outbox.sqlite'
    connection=sqlite3.connect(path.resolve().as_uri()+('?mode=rw' if apply else '?mode=ro'),uri=True)
    connection.row_factory=sqlite3.Row
    events=EventStore(str(store.data_root/'db/events.db'),readonly=True)
    rows=[]
    try:
        for record in connection.execute("SELECT notification_id,submitted,submitted_causes FROM followups WHERE submitted!='{}' AND submitted_causes!='{}'"):
            submitted=json.loads(record['submitted']);causes=json.loads(record['submitted_causes'])
            for symbol,versions in causes.items():
                task=submitted.get(symbol)
                if not isinstance(task,str) or not task:continue
                for eid,version in versions.items():
                    if type(version) is not int:continue
                    old=events.conn.execute('SELECT payload_json FROM event_versions WHERE id=? AND event_id=?',(version,eid)).fetchone()
                    current=events.get_event(eid)
                    if old is None or current is None:continue
                    saved=json.loads(old['payload_json'])
                    denied=Eligibility(store.data_root).excluded_ids()
                    if events._source_excluded(eid,denied,sources=saved.get('sources',[])):
                        continue
                    if not events._reporting_allowed(saved,sources=saved.get('sources',[]),support=saved.get('reporting_support')):
                        continue
                    trusted=[s for s in saved.get('sources',[]) if reporting_use(store.data_root,s)['allowed']]
                    if not trusted:continue
                    old_cause,old_version=analysis_identity({**saved,'_version_id':version},trusted)
                    if (old_cause,old_version)!=(current['_analysis_cause_id'],current['_analysis_version_id']):continue
                    rows.append({'symbol':symbol,'cause_id':old_cause,'cause_version':str(old_version),
                        'source_event_id':eid,'source_event_version':str(version),'notification_id':record['notification_id'],
                        'task_id':task,'event_version_payload_sha256':hashlib.sha256(old['payload_json'].encode()).hexdigest(),
                        'analysis_completed':False,'basis':'existing_confirmed_dispatch_exact_retained_source_text'})
        if apply:
            with connection:
                for row in rows:
                    cursor=connection.execute("""INSERT INTO analysis_causes(symbol,cause_id,cause_version,source_event_id,source_event_version,
                        notification_id,status,task_id,changed) VALUES(?,?,?,?,?,?,'queued',?,?)
                        ON CONFLICT(symbol,cause_id,cause_version) DO UPDATE SET status='queued',task_id=excluded.task_id,
                        notification_id=excluded.notification_id,source_event_id=excluded.source_event_id,
                        source_event_version=excluded.source_event_version,changed=excluded.changed
                        WHERE analysis_causes.status='preparing' AND analysis_causes.task_id IS NULL AND EXISTS(
                            SELECT 1 FROM followups owner WHERE owner.notification_id=analysis_causes.notification_id
                            AND owner.status='pending' AND (owner.next_eligible_at>? OR owner.defer_reason='cost_budget_disabled'))""",
                        tuple(row[key] for key in ('symbol','cause_id','cause_version','source_event_id','source_event_version','notification_id','task_id'))+(now.timestamp(),now.timestamp()))
                    if cursor.rowcount:
                        row['registry_binding_status']='bound'
                    else:
                        existing=connection.execute('SELECT status,task_id FROM analysis_causes WHERE symbol=? AND cause_id=? AND cause_version=?',
                            (row['symbol'],row['cause_id'],row['cause_version'])).fetchone()
                        row['registry_binding_status']='already_confirmed' if existing and existing['status']=='queued' and existing['task_id']==row['task_id'] else 'deferred_existing_claim'
            append_jsonl(store.root/'.evidence/confirmed_dispatch_content_bindings.jsonl',
                {'at':now.isoformat(),'bindings':rows,'sha256':digest(rows),'analysis_completed':False})
        return {'count':len(rows),'unique_causes':len({(r['symbol'],r['cause_id'],r['cause_version']) for r in rows}),
                'bindings':rows,'applied':apply,'analysis_completed':False}
    finally:events.conn.close();connection.close()


def reconcile(store: MemoryStore, plan: dict, *, apply=False, operator='', now=None):
    """CAS the reviewed objects; preserved originals never become trusted facts."""
    now=now or datetime.now(timezone.utc)
    from openinvest.utils.advisory import is_advisory_mode
    if is_advisory_mode():raise ValueError('operational_reconciliation_owner_only')
    if (plan.get('schema')!='openinvest.operational_reconciliation_plan.v1'
            or plan.get('data_root')!=str(store.data_root.resolve())
            or plan.get('plan_sha256')!=digest({k:v for k,v in plan.items() if k!='plan_sha256'})):
        raise ValueError('reconciliation_plan_binding_invalid')
    if apply and not str(operator).strip():raise ValueError('explicit_operator_required')
    from openinvest.services import agent_queue
    from openinvest.services.review_plans import plan_qualification
    actions=[]
    queue=agent_queue._read('committees.jsonl',store.root)
    completions={(r.get('request_id'),r.get('symbol')) for r in agent_queue._read('committee_completions.jsonl',store.root)}
    for request in queue:
        if 'event_versions' in request or agent_queue._age(request,now)<=agent_queue.COMMITTEE_TTL:continue
        for symbol in request.get('symbols',[]):
            if (request['id'],symbol) not in completions:
                actions.append({'kind':'historical_missed','id':request['id'],'symbol':symbol,'sha256':digest(request)})
    for path in sorted((store.root/'.state').glob('review_plan_*.json')):
        record=json.loads(path.read_text())
        if record and record.get('plan_id') and record.get('state') not in ('retired_audit_only',):
            qualification=plan_qualification(store,record.get('symbol',''),record,purpose=record.get('purpose','review'))
            if not qualification['allowed']:
                actions.append({'kind':'retire_generated_plan','id':path.stem,'sha256':digest(record),'qualification':qualification})
    db=sqlite3.connect((store.data_root/'db/events.db').resolve().as_uri()+('?mode=rw' if apply else '?mode=ro'),uri=True);db.row_factory=sqlite3.Row
    captures=sqlite3.connect((store.data_root/'db/source_snapshots.sqlite').resolve().as_uri()+'?mode=ro',uri=True)
    try:
        db.execute('BEGIN IMMEDIATE' if apply else 'BEGIN')
        captures.execute('BEGIN')
        conflicts=[]
        for expected in plan.get('conflicts',[]):
            row=db.execute('SELECT * FROM news_observation_conflicts WHERE fingerprint=?',(expected['fingerprint'],)).fetchone()
            if row is None or hashlib.sha256(row['payload_json'].encode()).hexdigest()!=expected['payload_sha256']:
                raise ValueError('conflict_original_changed')
            if row['status']=='closed_metadata_duplicate':continue
            if row['status']!='review_required':raise ValueError('conflict_status_changed')
            value=json.loads(row['payload_json']);raw=value['source']
            incoming={**raw,'raw_text':raw.get('text','') or raw.get('raw_text','') or ''}
            current=db.execute('SELECT * FROM sources WHERE id=?',(expected['current_source']['source_row_id'],)).fetchone()
            if current is None or digest(dict(current))!=expected['current_source']['source_row_sha256']:
                raise ValueError('conflict_source_changed_requires_rescan')
            if _text(incoming)!=_text(dict(current)):
                raise ValueError('conflict_observed_text_actually_changed')
            prior=expected['recorded_source_at_or_before_conflict']
            version=db.execute('SELECT observed_at,payload_json FROM event_versions WHERE id=? AND event_id=?',
                (prior['version_id'],expected['event_id'])).fetchone()
            if (version is None or hashlib.sha256(version['payload_json'].encode()).hexdigest()!=prior['payload_bytes_sha256']
                    or datetime.fromisoformat(version['observed_at'])>datetime.fromisoformat(row['observed_at'])):
                raise ValueError('conflict_prior_observation_changed')
            if not any(_text(saved)==_text(incoming) for saved in json.loads(version['payload_json']).get('sources',[])):
                raise ValueError('conflict_was_not_metadata_only_at_observation')
            capture_id=(raw.get('raw_meta') or {}).get('collector_capture_id')
            captured=captures.execute('SELECT sha256,payload FROM adapter_captures WHERE id=?',(capture_id,)).fetchone()
            if captured is None or digest(json.loads(captured[1]))!=captured[0]:
                raise ValueError('conflict_capture_unavailable')
            expected_capture=expected['capture']
            if (capture_id!=expected_capture['id'] or captured[0]!=expected_capture['stored_packet_sha256']
                    or hashlib.sha256(captured[1].encode()).hexdigest()!=expected_capture['stored_payload_bytes_sha256']):
                raise ValueError('conflict_capture_changed_requires_rescan')
            from openinvest.services.source_reporting import source_payload
            if json.loads(captured[1])['source']!=source_payload(incoming):raise ValueError('conflict_capture_content_changed')
            conflicts.append({'kind':'metadata_duplicate','id':expected['fingerprint'],'sha256':expected['payload_sha256']})
        actions+=conflicts
        for expected in plan.get('sources_with_missing_acquisition_evidence',[]):
            row=db.execute('SELECT * FROM sources WHERE id=?',(expected['source_row_id'],)).fetchone()
            if row is None or digest(dict(row))!=expected['source_row_sha256']:
                raise ValueError('historical_source_changed_requires_rescan')
            from openinvest.services.source_reporting import reporting_use
            if reporting_use(store.data_root,dict(row))['allowed']:
                raise ValueError('historical_source_now_qualified_requires_rescan')
            actions.append({'kind':'audit_only_insufficient_evidence','id':expected['source_report_id'],'sha256':expected['source_payload_sha256']})
        for expected in plan.get('events_without_sources',[]):
            row=db.execute('SELECT * FROM events WHERE event_id=?',(expected['event_id'],)).fetchone()
            if row is None or digest(dict(row))!=expected['event_row_sha256']:
                raise ValueError('historical_event_changed_requires_rescan')
            if db.execute('SELECT count(*) FROM sources WHERE event_id=?',(expected['event_id'],)).fetchone()[0]:
                raise ValueError('historical_event_has_new_evidence')
            actions.append({'kind':'audit_only_insufficient_evidence','id':'event:'+expected['event_id'],'sha256':expected['event_row_sha256']})
        if apply:
            with db:
                db.execute('CREATE TABLE IF NOT EXISTS operational_reconciliations(id TEXT PRIMARY KEY,payload TEXT NOT NULL,sha256 TEXT NOT NULL)')
                for action in actions:
                    if action['kind']=='historical_missed':
                        agent_queue.record_legacy_committee_disposition(action['id'],action['symbol'],operator=operator,
                            reason='original_request_expired_without_bound_completion; retained_historical_miss',
                            request_sha256=action['sha256'],memory_root=store.root,now=now,
                            followup_reference='current_source_and_price_monitoring; not a historical completion')
                    elif action['kind']=='retire_generated_plan':
                        def retire(old, expected=action):
                            if digest(old)!=expected['sha256']:raise ValueError('generated_plan_changed_requires_rescan')
                            updated={**old,'state':'retired_audit_only','changed_at':now.isoformat(),
                                'retirement':{'reason':'parent_or_source_unqualified','operator':operator,'original_sha256':expected['sha256']}}
                            append_jsonl(store.root/'.evidence/operational_plan_retirements.jsonl',
                                {'original':old,'sha256':expected['sha256'],'operator':operator,'at':now.isoformat(),'execution_authorized':False})
                            return updated,None
                        store.state_update(action['id'],retire)
                    payload={**action,'operator':operator,'at':now.isoformat(),'plan_sha256':plan['plan_sha256'],
                        'qualification_granted':False,'learning_granted':False,'completed':False}
                    ident=digest([action['kind'],action['id'],action.get('symbol'),action['sha256']])
                    db.execute('INSERT OR IGNORE INTO operational_reconciliations VALUES(?,?,?)',
                        (ident,json.dumps(payload,ensure_ascii=False,sort_keys=True),digest(payload)))
                    if action['kind']=='metadata_duplicate':
                        db.execute("UPDATE news_observation_conflicts SET status='closed_metadata_duplicate' WHERE fingerprint=? AND status='review_required'",(action['id'],))
        return {'status':'applied' if apply else 'review_plan','actions':actions,
            'counts':{kind:sum(a['kind']==kind for a in actions) for kind in sorted({a['kind'] for a in actions})},
            'qualification_granted':False,'learning_granted':False,'financial_values_modified':False}
    finally:
        captures.close();db.close()
