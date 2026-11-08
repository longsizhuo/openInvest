"""Content-bound account source status and separate tracking projections.

An estimated purchase must not create a holding, cash flow or observed fill.
Historical records remain readable; source uncertainty is not proof of an error.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime,timezone
import hashlib
import json
import math
from pathlib import Path

from openinvest.calc.ledger_replay import execution_basis,execution_domain,unobserved_execution,absolute_balance_correction,replay_ledger
from openinvest.core.evidence_eligibility import digest
from openinvest.core.memory_store import MemoryStore,_file_lock


def valuation_unit_mapping(holding):
    """A native balance is not interchangeable with an arbitrary quote proxy."""
    from openinvest.utils.quotes import is_eastmoney_fund
    from openinvest.utils.eastmoney_fund import extract_fund_code
    symbol=holding.get('symbol');native=holding.get('native_instrument') or symbol
    proxy=holding.get('yfinance_proxy') or symbol
    gold=(holding.get('proxy_kind')=='gold_cny_per_gram' and holding.get('kind')=='metal'
          and holding.get('unit_label')=='克' and holding.get('cost_currency')=='CNY' and proxy=='GC=F')
    if holding.get('kind')=='metal' and not holding.get('proxy_kind'):
        return False,'native_proxy_unit_mapping_unproven'
    if native!=symbol and not gold:return False,'native_proxy_unit_mapping_unproven'
    # GC=F is the runtime's reserved bank-gold CNY/gram booking identity.
    # A legacy direct/USD/oz row cannot borrow that conversion implicitly.
    if holding.get('proxy_kind')=='gold_cny_per_gram' or symbol=='GC=F':
        return (True,None) if gold else (False,'native_proxy_unit_mapping_unproven')
    if is_eastmoney_fund(holding):
        return (True,None) if extract_fund_code(symbol) else (False,'native_proxy_unit_mapping_unproven')
    return (True,None) if proxy==symbol else (False,'native_proxy_unit_mapping_unproven')


def _raw_input_fingerprint(root):
    """Invalid JSON still has an input identity; missing is different from empty."""
    root=Path(root);files={}
    for relative in ('portfolio.md','portfolio_history.jsonl','.state/account_observation_heads.json'):
        path=root/relative
        files[relative]=hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    for relative in ('.evidence/tracking_estimates','.evidence/account_observations',
                     '.evidence/broker_fee_reports','.evidence/broker_fee_applications'):
        directory=root/relative
        files[relative]={'exists':directory.exists(),'files':{
            path.name:hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(directory.glob('*.json'))}}
    return digest({'kind':'unavailable_account_inputs_v1','files':files})


def _unavailable(store,reason,unobserved=None):
    try:fingerprint=_raw_input_fingerprint(store.root)
    except (OSError,ValueError,TypeError,AttributeError):fingerprint=None
    return {'status':'unavailable','reason':reason,'historical_execution_eligible':False,
            'paper_performance_eligible':False,'allocation_cash_eligible':False,
            'holdings':{},'unobserved_rows':unobserved,'content_sha256':fingerprint}


def _same_portfolio_snapshot(pm,portfolio):
    from openinvest.core.decision_evidence import _plain
    live=pm.store.read('portfolio')
    return live is not None and _plain(portfolio)==_plain(dict(getattr(live,'metadata',live)))


def _fee_identity(value):
    if (not isinstance(value,dict)
            or any(not isinstance(value.get(key),str) or not value[key].strip() for key in ('account','exec_id'))
            or value.get('execution_domain') not in {'broker_live','broker_paper','broker_unknown'}):
        raise ValueError('broker_fee_execution_identity_invalid')
    return {key:value[key] for key in ('account','exec_id','execution_domain')}


def _finite_number(value):
    if not isinstance(value,(int,float)) or isinstance(value,bool):return False
    try:return math.isfinite(value)
    except OverflowError:return False


def _aware_time(value):
    stamp=datetime.fromisoformat(value)
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError('broker_fee_capture_time_unknown')


def _validate_fee_report(value):
    report=value['report'];identity=_fee_identity(report)
    amount=report.get('reported_commission');ccy=report.get('currency');status=report.get('fee_status')
    reference=value.get('fill_source_reference')
    if (report.get('broker')!='ibkr' or not _finite_number(amount)
            or (ccy is not None and (not isinstance(ccy,str) or not ccy or ccy!=ccy.upper()))
            or status not in {'unknown','reported','reported_zero'}
            or (status=='reported_zero' and amount!=0)
            or (status=='reported' and (amount==0 or not ccy))
            or not isinstance(reference,dict) or reference.get('account')!=identity['account']
            or reference.get('exec_id')!=identity['exec_id']
            or reference.get('commission')!=amount or reference.get('commission_currency')!=ccy
            or value.get('cash_applied') is not False
            or value.get('purpose')!='broker_commission_report_not_cash_application'):
        raise ValueError('broker_fee_report_business_binding_invalid')
    _aware_time(value.get('captured_at'))


def account_source_status(pm) -> dict:
    """Read existing evidence; never repair numbers or infer a missing balance."""
    try:
        history=pm.store.read_history()
        holdings=[dict(row) for row in pm.holdings]
        portfolio=dict(getattr(pm.portfolio,'metadata',pm.portfolio))
        from openinvest.core.decision_evidence import _plain
        if not _same_portfolio_snapshot(pm,portfolio):
            return _unavailable(pm.store,'account_snapshot_changed_requires_refresh')
        if not isinstance(history,list) or any(not isinstance(row,dict) for row in history):
            raise ValueError('invalid_history_records')
        estimates=[]
        for path in sorted((pm.store.root/'.evidence/tracking_estimates').glob('*.json')):
            envelope=json.loads(path.read_text());value=envelope['payload']
            if envelope['sha256']!=digest(value):raise ValueError('tracking_estimate_integrity_failure')
            if not isinstance(value.get('identity'),dict) or not value['identity'].get('symbol'):
                raise ValueError('tracking_estimate_identity_missing')
            estimates.append({'sha256':envelope['sha256'],'payload':value})
        heads=pm.store.root/'.state/account_observation_heads.json'
        heads_sha=hashlib.sha256(heads.read_bytes()).hexdigest() if heads.exists() else None
        fee_reports=[];fee_intents=[]
        for directory,target in [('broker_fee_reports',fee_reports),('broker_fee_applications',fee_intents)]:
            for path in sorted((pm.store.root/'.evidence'/directory).glob('*.json')):
                envelope=json.loads(path.read_text());value=envelope['payload']
                if envelope.get('schema')!=1 or not isinstance(value,dict) or envelope['sha256']!=digest(value):raise ValueError('broker_fee_evidence_invalid')
                if directory=='broker_fee_reports':
                    _validate_fee_report(value)
                    report=value['report']
                    if value['report_hash']!=digest(report) or path.stem!=value['report_hash']:raise ValueError('broker_fee_report_binding_invalid')
                else:
                    identity=_fee_identity(value['execution'])
                    if (path.stem!=digest(value['execution']) or value['execution']!=identity
                            or value.get('purpose')!='cash_application_intent_not_proof_of_applied_fee'
                            or value.get('idempotency_key')!='ibkr-fee-'+identity['exec_id']
                            or not _finite_number(value.get('target_total')) or value['target_total']<=0
                            or not isinstance(value.get('currency'),str) or not value['currency']):
                        raise ValueError('broker_fee_intent_identity_invalid')
                    _aware_time(value.get('attempted_at'))
                target.append({'sha256':envelope['sha256'],'payload':value})
        reports_by_hash={item['payload']['report_hash']:item['payload']['report'] for item in fee_reports}
        for envelope in fee_intents:
            intent=envelope['payload'];report=reports_by_hash.get(intent.get('fee_report_hash'))
            if (report is None or intent['execution']!=_fee_identity(report)
                    or intent['target_total']!=report['reported_commission'] or intent['currency']!=report['currency']
                    or report['fee_status']!='reported'):
                raise ValueError('broker_fee_intent_report_binding_invalid')
    except (OSError,ValueError,TypeError,AttributeError,KeyError):
        return _unavailable(pm.store,'account_source_history_unavailable')
    from openinvest.services.estimate_resolution import classify_estimates
    resolution=classify_estimates(estimates,history)
    if resolution['status']!='ok':
        return _unavailable(pm.store,'tracking_estimate_resolution_unavailable')
    pending_estimates=resolution['pending']
    ledger_domain=str(portfolio.get('ledger_domain') or 'client_reported')
    paper_mode=ledger_domain in {'paper_simulation','broker_paper'}
    uncertain=[row for row in history if unobserved_execution(row)
               and not (paper_mode and execution_basis(row)=='paper_simulated_fill')]
    domains=Counter(execution_domain(row) for row in history)
    incompatible_domains=any(domain not in ({'client_reported','paper_simulation','broker_paper'} if paper_mode else {'client_reported','broker_live'}) for domain in domains)
    corrections=[row for row in history if absolute_balance_correction(row)]
    # Domain gaps affecting quantities must not deny independent known cash.
    # External-funded projections have zero cash effect; simulated/mis-scoped
    # cash mutations and unresolved broker costs are different dependencies.
    activities,_=replay_ledger(history,alias={})
    cash_domain_conflicts={a.currency for a in activities if a.replayable and a.cash_effect!=0
        and a.execution_domain not in ({'client_reported','paper_simulation','broker_paper'} if paper_mode else {'client_reported','broker_live'})}
    def broker_key(row):
        reference=row.get('source_reference')
        if not isinstance(reference,dict) or not reference.get('exec_id'):return None
        return (reference.get('account') or reference.get('account_id'),reference['exec_id'],execution_domain(row))
    known_fees={}
    applied_fees={}
    fee_symbols={}
    unknown_report_keys=set()
    missing_fee_evidence=set()
    fee_files={item['payload']['report_hash']:item for item in fee_reports}
    for row in history:
        key=broker_key(row)
        if key is None:continue
        fee_symbols.setdefault(key,set()).add(str(row.get('symbol') or ''))
        if row.get('fee_status')=='unknown':unknown_report_keys.add(key)
        reference=row.get('source_reference') or {}
        report_hash=reference.get('fee_report_hash')
        if report_hash and (report_hash not in fee_files or reference.get('fee_report_sha256')!=fee_files[report_hash]['sha256']):
            missing_fee_evidence.add(key)
        if row.get('action')=='fee':
            amount=row.get('amount')
            effect=row.get('cash_effect')
            ccy=str(row.get('currency') or '').upper()
            if (_finite_number(amount) and amount>=0
                    and _finite_number(effect) and abs(effect+amount)<1e-9):
                applied_fees.setdefault(key,{}).setdefault(ccy,0.0)
                applied_fees[key][ccy]+=float(amount)
        if row.get('fee_status') not in {'reported','reported_zero'}:continue
        amount=reference.get('commission',row.get('amount',row.get('fee')))
        if row.get('fee_status')=='reported_zero':amount=0
        ccy=reference.get('commission_currency')
        if ccy is None and amount!=0:ccy=row.get('currency')
        if _finite_number(amount):known_fees.setdefault(key,set()).add((float(amount),str(ccy or '').upper()))
        else:unknown_report_keys.add(key)
    for envelope in fee_reports:
        value=envelope['payload'];report=value['report']
        key=(report['account'],report['exec_id'],report['execution_domain'])
        fee_symbols.setdefault(key,set()).add(str(value.get('symbol') or ''))
        amount=report.get('reported_commission');ccy=report.get('currency')
        if report.get('fee_status')=='unknown':unknown_report_keys.add(key)
        elif _finite_number(amount):
            known_fees.setdefault(key,set()).add((float(amount),str(ccy or '').upper()))
    fee_conflicts=[key for key,values in known_fees.items() if len(values)>1]
    fee_pending_keys=set(missing_fee_evidence)
    for key in set(known_fees)|unknown_report_keys:
        values=known_fees.get(key,set())
        if len(values)!=1:
            fee_pending_keys.add(key);continue
        amount,ccy=next(iter(values))
        applied=applied_fees.get(key,{})
        if amount<0 or (amount>0 and (not ccy or abs(applied.get(ccy,0.0)-amount)>1e-9)):
            fee_pending_keys.add(key)
        elif amount==0 and any(abs(value)>1e-9 for value in applied.values()):
            fee_pending_keys.add(key)
    for envelope in fee_intents:
        value=envelope['payload'];execution=value['execution']
        key=(execution.get('account'),execution.get('exec_id'),execution.get('execution_domain'))
        target=value.get('target_total');ccy=str(value.get('currency') or '').upper()
        report_hash=value.get('fee_report_hash')
        if (report_hash not in fee_files or not _finite_number(target)
                or abs(applied_fees.get(key,{}).get(ccy,0.0)-target)>1e-9):
            fee_pending_keys.add(key)
    unknown_fee_rows=[{'symbol':symbol} for key in fee_pending_keys for symbol in fee_symbols.get(key,{''})]
    by_symbol=Counter(str(row.get('symbol') or '') for row in uncertain)
    pending=Counter(str(row['payload']['identity']['symbol']) for row in pending_estimates)
    grades={};unproven_mappings=[]
    for row in holdings:
        symbol=str(row.get('symbol') or '')
        if not symbol:continue
        mapped,mapping_reason=valuation_unit_mapping(row)
        if not mapped and not row.get('is_tracking_only'):unproven_mappings.append(symbol)
        count=by_symbol.get(symbol,0)+pending.get(symbol,0)+(0 if mapped else 1)
        row_domains={execution_domain(item) for item in history if item.get('symbol')==symbol}
        quantity_basis=('paper_simulated' if paper_mode else
                        'broker_observed_domain_unresolved' if row_domains & {'broker_paper','broker_unknown','paper_simulation'} else 'client_recorded')
        if count:quantity_basis='estimated_or_proxy_unreconciled'
        fee_pending=any(item.get('symbol')==symbol for item in unknown_fee_rows)
        grades[symbol]={'current_value':'estimated_or_proxy_unreconciled' if count else 'client_recorded',
            'current_quantity':quantity_basis,
            'cost_basis':'estimated_or_proxy_unreconciled' if count else 'broker_commission_pending' if fee_pending else 'client_recorded',
            'historical_execution':'native_quote_unit_mapping_unverified' if not mapped else 'contains_unobserved_projection' if count else 'client_reported',
            'unobserved_rows':by_symbol.get(symbol,0),'pending_estimates':pending.get(symbol,0),
            'valuation_unit_mapping':{'qualified':mapped,'reason':mapping_reason},
            'independent_broker_verification':False}
    from openinvest.core.decision_evidence import _plain
    confirmations={'holdings':{},'parent_ids':[],'fingerprint':None}
    used_parents=set()
    invalid_observations=False
    observation_journal=any(row.get('action')=='balance_observation'
        or (isinstance(row.get('source_reference'),dict) and row['source_reference'].get('observation_id'))
        for row in history)
    if heads.exists() or (pm.store.root/'.evidence/account_observations').exists() or observation_journal:
        from openinvest.services.balance_observations import current_confirmations
        confirmations=current_confirmations(pm,history,estimates)
        if confirmations.get('status')!='ok':
            return _unavailable(pm.store,'balance_observation_integrity_failure',len(uncertain))
        for symbol,fields in confirmations.get('holdings',{}).items():
            if symbol not in grades:continue
            for field,value in fields.items():
                if field in {'current_value','current_quantity','cost_basis'} and value.get('status')=='client_reported_balance_confirmed':
                    grades[symbol][field]='client_observed_balance'
                    grades[symbol].setdefault('balance_observations',{})[field]=value
                    used_parents.update(value.get('parent_ids') or ['account_observation:'+value['observation_id']])
                elif field in {'current_value','current_quantity','cost_basis'}:
                    grades[symbol][field]='balance_observation_'+str(value.get('status','unavailable'))
                    grades[symbol].setdefault('balance_observations',{})[field]=value
                    invalid_observations=True
    try:
        if not _same_portfolio_snapshot(pm,portfolio):
            return _unavailable(pm.store,'account_snapshot_changed_requires_refresh')
    except (OSError,ValueError,TypeError,AttributeError):
        return _unavailable(pm.store,'account_snapshot_changed_requires_refresh')
    payload=_plain({'portfolio':portfolio,'history':history,'estimates':estimates,'fee_reports':fee_reports,'fee_intents':fee_intents,
                    'estimate_resolution_fingerprint':resolution['fingerprint'],
                    'observation_heads_sha256':heads_sha,'confirmation_fingerprint':confirmations.get('fingerprint'),
                    'interpretation':'account_source_basis_v1'})
    qualified=not (uncertain or pending_estimates or incompatible_domains or unknown_fee_rows or fee_conflicts or corrections or invalid_observations or unproven_mappings)
    return {'status':'simulation_only' if paper_mode and qualified else 'requires_reconciliation' if not qualified else 'client_recorded',
            'holdings':grades,'unobserved_rows':len(uncertain),
            'pending_estimates':len(pending_estimates),'retained_estimates':len(estimates),
            'unproven_valuation_mappings':unproven_mappings,
            'resolved_estimates':resolution['resolved'],'unresolved_estimates':resolution['unresolved'],
            'ledger_domain':ledger_domain,'execution_domains':dict(domains),'broker_commission_pending':len(unknown_fee_rows),
            'allocation_cash_eligible':not (cash_domain_conflicts or unknown_fee_rows or fee_conflicts),
            'cash_domain_conflicts':sorted(cash_domain_conflicts),
            'broker_commission_conflicts':len(fee_conflicts),'balance_adjustments_not_executions':len(corrections),
            'parent_ids':sorted(used_parents),'audit_parent_ids':confirmations.get('parent_ids',[]),
            'source_basis_counts':dict(Counter(execution_basis(row) for row in uncertain)),
            'historical_execution_eligible':qualified and not paper_mode,
            'paper_performance_eligible':qualified and paper_mode,'content_sha256':digest(payload),
            'note':'估算/价值代理不等于真实份额或成交；原记录保留，当前数值未因此被判为错误。当前余额确认不能反推旧成交或历史收益。'}


def store_source_status(store: MemoryStore) -> dict:
    """Read a missing/corrupt account without creating portfolio lock files."""
    from types import SimpleNamespace
    import frontmatter
    try:
        portfolio=frontmatter.loads((store.root/'portfolio.md').read_text()).metadata
        return account_source_status(SimpleNamespace(store=store,portfolio=portfolio,holdings=portfolio['holdings']))
    except (OSError,ValueError,TypeError,KeyError):
        return _unavailable(store,'account_source_history_unavailable')


def record_tracking_estimate(store: MemoryStore, *, symbol: str, day: str, plan: dict, quote: dict) -> dict:
    """One immutable estimate per date/instrument/plan, without ledger mutation.

    Quote changes on a retry do not revise the first estimate. A changed plan is
    a different auditable projection and must never be summed as another fill.
    """
    from openinvest.core.decision_evidence import _write_once
    identity={'symbol':symbol,'date':day,'plan':plan,'kind':'tracking_estimate'}
    ident=digest(identity)
    path=store.root/'.evidence/tracking_estimates'/(ident+'.json')
    with _file_lock(path):
        if path.exists():
            envelope=json.loads(path.read_text())
            if envelope['sha256']!=digest(envelope['payload']) or envelope['payload']['identity']!=identity:
                raise ValueError('tracking_estimate_receipt_requires_reconciliation')
            return {**envelope['payload'],'replayed':True}
        value={'estimate_id':ident,'identity':identity,'quote':quote,
               'recorded_at':datetime.now(timezone.utc).isoformat(),
               'purpose':'tracking_projection_not_confirmed_trade','execution_observed':False,
               'actual_fee':None,'actual_quantity':None,'allowed_uses':['audit'],
               'portfolio_mutated':False,'cash_flow_recorded':False}
        _write_once(path,value)
        return {**value,'replayed':False}
