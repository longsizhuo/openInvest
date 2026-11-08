"""Unobserved projections cannot become holdings, fills or real return labels."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from openinvest.calc.ledger_replay import execution_basis,replay_ledger,external_flow
from openinvest.core.portfolio_manager import PortfolioManager
from openinvest.services.ledger_evidence import account_source_status,record_tracking_estimate
from tests.test_dca_daily import seeded,_fixed_quote


def test_scheduled_dca_creates_only_a_projection_without_any_ledger_transaction(seeded,monkeypatch):
    from openinvest.jobs import dca_daily
    monkeypatch.setenv('INVEST_DCA_AUTO_DCA_ENABLED','true')
    monkeypatch.setenv('INVEST_DCA_AUTO_DCA_SYMBOLS','510300.SS')
    monkeypatch.setenv('INVEST_DCA_AUTO_DCA_AMOUNT_CNY','100')
    today=datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
    monkeypatch.setattr(dca_daily,'get_quote',_fixed_quote(5.0,bar_date=today))
    monkeypatch.setattr(PortfolioManager,'buy',lambda *a,**k:pytest.fail('projection cannot call buy'))
    monkeypatch.setattr(PortfolioManager,'with_portfolio_tx',lambda *a,**k:pytest.fail('projection cannot open a ledger write transaction'))
    portfolio=seeded.path_of('portfolio');before=portfolio.read_bytes()
    result=dca_daily.run();again=dca_daily.run()
    assert result['results'][0]['status']=='estimated'
    assert again['results'][0]['reason']=='already_projected_this_plan_today'
    assert portfolio.read_bytes()==before and seeded.read_history()==[]
    assert len(list((seeded.root/'.evidence/tracking_estimates').glob('*.json')))==1


def test_projection_retry_preserves_original_quote_and_does_not_invent_actual_quantity(seeded):
    kwargs=dict(symbol='SYNTHETIC',day='2026-11-06',plan={'amount':100})
    first=record_tracking_estimate(seeded,**kwargs,quote={'price':5,'projected_quantity':20})
    again=record_tracking_estimate(seeded,**kwargs,quote={'price':4,'projected_quantity':25})
    assert again['replayed'] and again['estimate_id']==first['estimate_id']
    assert again['quote']==first['quote'] and again['actual_quantity'] is None
    assert again['execution_observed'] is False and again['cash_flow_recorded'] is False


def test_source_classification_is_exact_and_survives_execution_replay():
    rows=[{'action':'buy','source':'dca_daily','symbol':'SYNTHETIC','units':20,'price':5,'funding_source':'external_funding','ts':'2026-10-01'},
          {'action':'dca_reconcile_reverse','source':'reconcile_dca_phantom','symbol':'SYNTHETIC','units':10,'price':5,'ts':'2026-10-02'}]
    activities,state=replay_ledger(rows,alias={})
    assert state.unobserved_execution_rows==2 and all(not a.execution_eligible for a in activities)
    assert all(a.replayable for a in activities)  # The original mathematical projection is retained for audit.
    assert activities[0].evidence_basis=='unobserved_dca_projection'
    for source in ('mcp:existing_position','actual_user_confirmed_dca','other'):
        assert execution_basis({'action':'buy','source':source,'funding_source':'external_funding'})=='client_reported_not_independently_verified'
    assert execution_basis({'action':'buy','source':'drip'})=='legacy_drip_price_basis_unverified'
    assert execution_basis({'action':'buy','source':'drip','source_reference':{'price_basis':'client_reported_fill_price'}})=='client_reported_not_independently_verified'


def test_legacy_estimate_is_not_proof_of_wrong_balance_or_actual_performance(seeded,monkeypatch):
    seeded.append_history({'action':'buy','source':'dca_daily','symbol':'510300.SS','units':20,'price':5,'ts':'2026-10-01'})
    pm=PortfolioManager(seeded);before=dict(pm.find_holding('510300.SS'))
    grade=account_source_status(pm)
    assert grade['status']=='requires_reconciliation' and not grade['historical_execution_eligible']
    assert pm.find_holding('510300.SS')==before
    from openinvest.services import account_performance
    monkeypatch.setattr(account_performance,'MarketStore',lambda:pytest.fail('unverified fills cannot trigger a real-performance calculation'))
    report=account_performance.build_account_performance(pm)
    assert report['status']=='unverified_execution_history' and report['financial_performance_eligible'] is False


def test_source_gap_blocks_only_execution_that_needs_the_unknown_quantity():
    from openinvest.core.decision_evidence import enforce_input_health
    health={'target_symbol':'SYNTHETIC','ledger_evidence':{'status':'requires_reconciliation','holdings':{
        'SYNTHETIC':{'current_quantity':'estimated_or_proxy_unreconciled'}}}}
    trim={'verdict':'TRIM','alloc_cny':100,'decision_status':'READY'}
    enforce_input_health(trim,dict(health))
    assert trim['verdict']=='TRIM' and trim['alloc_cny']==0 and trim['decision_status']=='REVIEW_REQUIRED'
    hold={'verdict':'HOLD','alloc_cny':0,'decision_status':'READY'}
    enforce_input_health(hold,dict(health));assert hold['verdict']=='HOLD' and hold['decision_status']=='READY'
    unrelated={'verdict':'TRIM','alloc_cny':100,'decision_status':'READY'}
    enforce_input_health(unrelated,{**health,'target_symbol':'OTHER'})
    assert unrelated['alloc_cny']==100 and unrelated['decision_status']=='READY'


def test_actual_external_funded_projection_does_not_deny_unrelated_known_cash(seeded):
    from openinvest.core.decision_evidence import enforce_input_health
    seeded.append_history({'action':'buy','source':'dca_daily','symbol':'510300.SS','units':20,'price':5,
        'funding_source':'external_funding','currency':'CNY','ts':'2026-10-01'})
    grade=account_source_status(PortfolioManager(seeded))
    assert grade['allocation_cash_eligible'] and not grade['historical_execution_eligible']
    verdict={'verdict':'BUY','alloc_cny':100,'alloc_currency':'CNY','decision_status':'READY'}
    enforce_input_health(verdict,{'target_symbol':'OTHER','ledger_evidence':grade})
    assert verdict['verdict']=='BUY' and verdict['alloc_cny']==100 and verdict['decision_status']=='READY'


def test_reported_broker_fee_is_not_proof_that_the_expense_was_applied(seeded):
    reference={'account':'SYNTHETIC-ACCOUNT','exec_id':'synthetic-execution','commission':1.25,'commission_currency':'CNY'}
    seeded.append_history({'action':'buy','source':'ibkr_autopilot','symbol':'510300.SS','units':1,'price':5,
        'currency':'CNY','trade_date':'2026-10-01','evidence_basis':'broker_observed_fill','execution_domain':'broker_live',
        'source_reference':reference,'fee_status':'reported'})
    grade=account_source_status(PortfolioManager(seeded))
    assert grade['unobserved_rows']==0  # It remains a genuine broker-observed quantity/price.
    assert grade['broker_commission_pending'] and not grade['historical_execution_eligible']
    assert not grade['allocation_cash_eligible']


def test_removing_all_observation_files_cannot_hide_a_retained_journal_reference(seeded):
    import shutil
    from datetime import timezone
    from openinvest.services.balance_observations import record_observation
    pm=PortfolioManager(seeded)
    record_observation(pm,'510300.SS',as_of=datetime.now(timezone.utc).isoformat(),
        source_text='合成客户声明：仅报告当前市值。',source_locator='synthetic:statement',
        idempotency_key='source-journal-integrity',reported_value=1500,value_currency='CNY')
    assert account_source_status(pm)['status']=='client_recorded'
    shutil.rmtree(seeded.root/'.evidence/account_observations')
    (seeded.root/'.state/account_observation_heads.json').unlink()
    grade=account_source_status(PortfolioManager(seeded))
    assert grade['status']=='unavailable' and not grade['historical_execution_eligible']


def test_new_balance_qualification_cannot_be_attached_to_an_old_pm_snapshot(seeded):
    from datetime import timezone
    from openinvest.services.balance_observations import record_observation
    from openinvest.utils.portfolio_summary import portfolio_summary_text
    old_pm=PortfolioManager(seeded)
    original_quantity=old_pm.find_holding('510300.SS')['units']
    new_quantity=original_quantity-1
    record_observation(PortfolioManager(seeded),'510300.SS',as_of=datetime.now(timezone.utc).isoformat(),
        source_text='合成客户原始份额声明。',source_locator='synthetic:updated-quantity',
        idempotency_key='new-quantity-snapshot',reported_quantity=new_quantity,
        quantity_unit=old_pm.find_holding('510300.SS').get('unit_label','share'),native_instrument='510300.SS')
    stale=account_source_status(old_pm)
    assert stale['status']=='unavailable' and stale['reason']=='account_snapshot_changed_requires_refresh'
    assert not stale['allocation_cash_eligible'] and stale['holdings']=={}
    text=portfolio_summary_text(old_pm,total_assets=1000,current_prices={'510300.SS':5})
    assert f'{original_quantity:.4f}' not in text
    fresh=PortfolioManager(seeded)
    assert fresh.find_holding('510300.SS')['units']==new_quantity
    grade=account_source_status(fresh)['holdings']['510300.SS']
    assert grade['current_quantity']=='client_observed_balance'
    assert grade['balance_observations']['current_quantity']['reported']['value']==new_quantity


@pytest.mark.parametrize('field,value',[('native_instrument','FUND:123456'),('yfinance_proxy','OTHER-PROXY')])
def test_native_or_quote_proxy_mismatch_needs_mapping_even_without_any_dca_history(seeded,monkeypatch,field,value):
    from openinvest.services import account_performance
    from openinvest.utils.portfolio_summary import portfolio_summary_text
    pm=PortfolioManager(seeded)
    with pm.with_portfolio_tx() as portfolio:portfolio['holdings'][0][field]=value
    pm._reload()
    pm.deposit_cash('CNY',100,source='skill_cli')
    grade=account_source_status(pm)
    assert grade['unobserved_rows']==0 and grade['pending_estimates']==0
    assert grade['unproven_valuation_mappings']==['510300.SS']
    assert not grade['historical_execution_eligible'] and grade['allocation_cash_eligible']
    assert grade['holdings']['510300.SS']['current_quantity']=='estimated_or_proxy_unreconciled'
    monkeypatch.setattr(account_performance,'MarketStore',lambda:pytest.fail('An unproved quote mapping cannot value actual returns'))
    assert account_performance.build_account_performance(pm)['financial_performance_eligible'] is False
    text=portfolio_summary_text(pm,10000,{'510300.SS':6})
    assert '300.0000' not in text and '浮盈 +20.00%' not in text


def test_native_fund_nav_route_ignores_unused_yfinance_proxy(seeded):
    pm=PortfolioManager(seeded)
    with pm.with_portfolio_tx() as portfolio:
        portfolio['holdings']=[{'symbol':'FUND:123456','kind':'fund','units':10,'unit_label':'份',
            'avg_cost':2,'cost_currency':'CNY','proxy_kind':'eastmoney_fund','yfinance_proxy':'UNUSED-PROXY'}]
    pm._reload()
    grade=account_source_status(pm)
    assert grade['unproven_valuation_mappings']==[]
    assert grade['holdings']['FUND:123456']['current_quantity']=='client_recorded'
    assert grade['historical_execution_eligible']


@pytest.mark.parametrize('change',[{}, {'unit_label':'oz'}, {'cost_currency':'USD'}, {'proxy_kind':'direct'}])
def test_gold_reserved_identity_cannot_silently_cross_quote_and_booking_units(change):
    from openinvest.services.ledger_evidence import valuation_unit_mapping
    correct={'symbol':'GC=F','kind':'metal','unit_label':'克','cost_currency':'CNY',
        'proxy_kind':'gold_cny_per_gram','yfinance_proxy':'GC=F'}
    assert valuation_unit_mapping(correct)==(True,None)
    if not change:
        invalid={'symbol':'GC=F','cost_currency':'CNY'}
    else:invalid={**correct,**change}
    assert not valuation_unit_mapping(invalid)[0]


def test_cost_only_confirmation_during_grading_cannot_qualify_the_old_cost(seeded,monkeypatch):
    from datetime import timezone
    from openinvest.services import balance_observations
    pm=PortfolioManager(seeded)
    balance_observations.record_observation(pm,'510300.SS',as_of=datetime.now(timezone.utc).isoformat(),
        source_text='合成初始市值声明。',source_locator='synthetic:first-value',idempotency_key='before-interleave',
        reported_value=1500,value_currency='CNY')
    old=PortfolioManager(seeded);real=balance_observations.current_confirmations;changed=[]
    def interleave(*args,**kwargs):
        if not changed:
            changed.append(True)
            balance_observations.record_observation(PortfolioManager(seeded),'510300.SS',
                as_of=datetime.now(timezone.utc).isoformat(),source_text='合成后到的实际平均成本声明。',
                source_locator='synthetic:cost-change',idempotency_key='interleaved-cost',
                reported_cost=4,cost_currency='CNY',quantity_unit='股',native_instrument='510300.SS')
        return real(*args,**kwargs)
    monkeypatch.setattr(balance_observations,'current_confirmations',interleave)
    grade=account_source_status(old)
    assert old.find_holding('510300.SS')['avg_cost']==5
    assert grade['status']=='unavailable' and grade['reason']=='account_snapshot_changed_requires_refresh'
    fresh=PortfolioManager(seeded)
    assert fresh.find_holding('510300.SS')['avg_cost']==4
    assert account_source_status(fresh)['holdings']['510300.SS']['cost_basis']=='client_observed_balance'


def test_different_unparseable_parent_receipt_bytes_invalidate_a_prepared_context(seeded):
    from datetime import timezone
    from openinvest.services.balance_observations import record_observation
    from openinvest.core.decision_evidence import prepare_context,load_context
    pm=PortfolioManager(seeded)
    record_observation(pm,'510300.SS',as_of=datetime.now(timezone.utc).isoformat(),
        source_text='合成原始市值声明。',source_locator='synthetic:corrupt-parent',idempotency_key='corrupt-parent',
        reported_value=1500,value_currency='CNY')
    receipt,=(seeded.root/'.evidence/account_observations').glob('*.json')
    receipt.write_text('{broken-version-one')
    before=account_source_status(pm)
    assert before['status']=='unavailable' and before['content_sha256']
    context=prepare_context(seeded.root,'510300.SS',{'ledger_evidence':before})
    assert load_context(seeded.root,context['context_id'],'510300.SS')['context_id']==context['context_id']
    receipt.write_text('{broken-version-two')
    after=account_source_status(pm)
    assert after['status']=='unavailable' and after['content_sha256']!=before['content_sha256']
    with pytest.raises(ValueError,match='account evidence changed'):
        load_context(seeded.root,context['context_id'],'510300.SS')
