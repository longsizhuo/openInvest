"""Typed position sources stay independent, exact, and shared by both entries."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import json

import pandas as pd
import pytest

from openinvest.core.evidence_eligibility import digest
from openinvest.services.position_facts import build_position_facts
from openinvest.utils.market_observation import history_observation


def inputs(symbol='TEST', *, currency='CNY', kind='EQUITY', unit='份', price=99.7565):
    now = datetime.now(timezone.utc)
    frame = pd.DataFrame({'Close': [price]}, index=[pd.Timestamp(now.date())])
    frame.attrs['source_observation'] = {**history_observation(frame, now=now, metadata={
        'currency': currency, 'symbol': symbol, 'instrumentType': kind, 'exchangeTimezoneName': 'UTC',
        'regularMarketTime': (now-timedelta(seconds=1)).timestamp(),
        'currentTradingPeriod': {'regular': {'start': (now-timedelta(hours=1)).timestamp(),
                                             'end': (now+timedelta(hours=1)).timestamp()}}}),
        'symbol': symbol, 'latest_price_observed': True}
    holding = {'symbol': symbol, 'kind': 'equity', 'units': 20, 'avg_cost': 95,
               'cost_currency': currency, 'unit_label': unit}
    grade = {'current_quantity': 'client_recorded', 'cost_basis': 'client_recorded',
             'valuation_unit_mapping': {'qualified': True}}
    evidence = {'status': 'client_recorded', 'ledger_domain': 'client_reported',
                'content_sha256': digest({'account': 'synthetic'}), 'holdings': {symbol: grade}}
    return holding, evidence, frame


def facts(holding, evidence, frame, **kwargs):
    return build_position_facts(holding['symbol'], holding=holding, ledger_evidence=evidence,
                                frame=frame, current_price=kwargs.pop('current_price', frame['Close'].iloc[-1]), **kwargs)


def test_position_snapshot_uses_exact_native_observation_and_content_identity():
    holding, evidence, frame = inputs()
    result = facts(holding, evidence, frame)
    assert result['quantity']['value'] == 20 and result['quantity']['unit'] == 'share'
    assert result['quote']['price'] == 99.7565 and result['quote']['currency'] == 'CNY'
    assert result['cost']['value'] == 95 and result['cost']['unit'] == 'share'
    assert all(result[key]['status'] == 'qualified' for key in ('quantity', 'quote', 'cost'))
    assert result['position_ref'] == digest({k: v for k, v in result.items() if k != 'position_ref'})
    assert result == facts(holding, evidence, frame)
    assert result['loss_budget'] == {'status': 'unknown', 'value': None, 'source_ref': None}
    assert evidence['content_sha256'] in result['quantity']['source_ref']
    assert result['quote']['source_ref'].startswith('market_observation:')


def test_cost_gap_does_not_hide_independently_qualified_quantity_or_quote():
    holding, evidence, frame = inputs()
    evidence['holdings']['TEST']['cost_basis'] = 'broker_commission_pending'
    result = facts(holding, evidence, frame)
    assert result['quantity']['status'] == result['quote']['status'] == 'qualified'
    assert result['cost']['status'] == 'unknown' and result['cost']['value'] is None
    holding['avg_cost'] = None
    assert facts(holding, evidence, frame)['cost']['value'] is None


@pytest.mark.parametrize('field', ['units', 'avg_cost'])
@pytest.mark.parametrize('value', [None, True, float('nan'), float('inf'), -1])
def test_missing_or_invalid_balance_is_unknown_not_zero(field, value):
    holding, evidence, frame = inputs()
    holding[field] = value
    result = facts(holding, evidence, frame)
    name = 'quantity' if field == 'units' else 'cost'
    assert result[name]['status'] == 'unknown' and result[name]['value'] is None


def test_explicit_zero_is_distinguished_from_a_missing_balance():
    holding, evidence, frame = inputs()
    holding.update(units=0, avg_cost=0)
    result = facts(holding, evidence, frame)
    assert result['quantity']['value'] == result['cost']['value'] == 0
    assert result['quantity']['status'] == result['cost']['status'] == 'qualified'


@pytest.mark.parametrize('unit', [None, 'BANANA', 'USD'])
def test_an_unverified_position_unit_is_not_invented_from_the_price_source(unit):
    holding, evidence, frame = inputs()
    holding['unit_label'] = unit
    result = facts(holding, evidence, frame)
    assert result['quantity']['unit'] is None and result['cost']['unit'] is None
    assert result['quantity']['status'] == result['cost']['status'] == 'unknown'


def test_shared_loader_reuses_account_and_market_inputs_without_another_fetch(monkeypatch):
    from openinvest.core.runner.loaders import load_position_facts
    holding, evidence, frame = inputs()
    seen = []
    class Holdings:
        def find(self, symbol): seen.append(symbol); return holding
    monkeypatch.setattr('openinvest.utils.quotes.get_history_data', lambda *a, **k: pytest.fail('extra market fetch'))
    monkeypatch.setattr('openinvest.services.ledger_evidence.account_source_status', lambda *a: pytest.fail('account reread'))
    result = load_position_facts('TEST', SimpleNamespace(holdings=Holdings()), evidence, frame, 99.7565)
    assert result['quote']['status'] == 'qualified' and seen == ['TEST']


@pytest.mark.parametrize('change', ['unobserved', 'wrong_bar', 'wrong_provider', 'old_quote', 'wrong_current', 'minor_currency', 'missing_asof'])
def test_unqualified_or_mismatched_quote_never_borrows_current_price(change):
    holding, evidence, frame = inputs()
    source = frame.attrs['source_observation']
    current = frame['Close'].iloc[-1]
    if change == 'unobserved': source['latest_price_observed'] = False
    elif change == 'wrong_bar': source['bar_asof'] = '2000-01-01'
    elif change == 'wrong_provider': source['provider_symbol'] = 'OTHER'
    elif change == 'old_quote': source['quote_asof'] = (datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()
    elif change == 'wrong_current': current += .01
    elif change == 'missing_asof': source.pop('quote_asof')
    else: source['currency'] = 'GBp'
    result = facts(holding, evidence, frame, current_price=current)
    assert result['quote']['status'] == 'unknown' and result['quote']['price'] is None
    assert result['quantity']['status'] == 'qualified'


def test_native_currency_is_not_relabelled_to_an_accounting_currency():
    holding, evidence, frame = inputs(currency='USD')
    holding['cost_currency'] = 'CNY'
    result = facts(holding, evidence, frame)
    assert result['quote']['currency'] == 'USD' and result['quote']['status'] == 'qualified'
    assert result['cost']['currency'] == 'CNY'  # A later calc must explicitly handle the mismatch.


def test_bank_gold_quantity_and_cost_cannot_borrow_a_futures_ounce_quote():
    holding, evidence, frame = inputs('GC=F', currency='USD', kind='FUTURE', unit='克', price=2200)
    holding.update(kind='metal', proxy_kind='gold_cny_per_gram', cost_currency='CNY', avg_cost=500)
    result = facts(holding, evidence, frame)
    assert result['quantity']['unit'] == result['cost']['unit'] == 'g'
    assert result['quantity']['status'] == result['cost']['status'] == 'qualified'
    assert result['quote']['status'] == 'unknown'


def test_economic_proxy_and_unverified_quantity_do_not_become_position_facts():
    holding, evidence, frame = inputs()
    holding['yfinance_proxy'] = 'OTHER'
    result = facts(holding, evidence, frame)
    assert all(result[key]['status'] == 'unknown' for key in ('quantity', 'quote', 'cost'))
    holding.pop('yfinance_proxy')
    evidence['holdings']['TEST']['current_quantity'] = 'estimated_or_proxy_unreconciled'
    assert facts(holding, evidence, frame)['quantity']['value'] is None


def test_advisory_loader_does_not_read_private_account_or_invent_zero_holdings():
    from openinvest.core.runner.loaders import load_position_facts
    class PrivateHoldings:
        def find(self, symbol): pytest.fail('advisory read private holdings')
    _, _, frame = inputs()
    result = load_position_facts('TEST', SimpleNamespace(holdings=PrivateHoldings()),
        {'private': 'must_not_be_used'}, frame, 99.7565, advisory=True)
    assert result['quantity']['status'] == result['cost']['status'] == 'unknown'
    assert result['quantity']['value'] is None and result['ledger_domain'] == 'unknown'
    assert 'private' not in json.dumps(result)


def test_primitive_risk_and_cio_receive_the_same_typed_position_source(monkeypatch):
    from openinvest.core.committee import debate
    holding, evidence, frame = inputs()
    snapshot = facts(holding, evidence, frame)
    contexts = {}
    class Agent:
        last_tool_calls = []
        def __init__(self, role): self.role = role
        def run(self, context):
            contexts[self.role] = context
            if self.role == 'cio': return 'VERDICT: HOLD\nCONFIDENCE: 0.5\nSUGGESTED_ALLOC_CNY: 0'
            return 'SIGNAL: unknown\nMARKET_RISK: unknown\nLIQUIDITY: unknown'
        def tool_call_summary(self): return ''
    monkeypatch.setattr(debate, '_create_agent', lambda prompt, role='unknown', **kw: Agent(role))
    monkeypatch.setattr('openinvest.services.reasoning_review.review_once', lambda *a, **k: {'status': 'passed'})
    result = debate.run_committee(asset={'symbol': 'TEST'}, market_data='provided', macro_view='provided',
        portfolio_summary='provided', position_facts=snapshot, persist_to_memory=False)
    for role in ('risk', 'cio'):
        assert snapshot['position_ref'] in contexts[role] and 'TYPED POSITION FACTS' in contexts[role]
    assert result['report'].position_facts == snapshot


def test_direct_entry_seals_and_passes_the_shared_position_loader_result(monkeypatch):
    from openinvest.core.memory_store import MemoryStore
    from openinvest.core.runner import session
    from tests.test_committee_contract import _seed_minimal_memory, _mock_for_symbol_path
    root = MemoryStore().root
    _seed_minimal_memory(root)
    captured = _mock_for_symbol_path(monkeypatch)
    snapshot = build_position_facts('TEST.AX')
    calls = []
    monkeypatch.setattr(session, 'load_position_facts', lambda *a, **k: calls.append((a, k)) or snapshot)
    session.run_committee_for_symbol('TEST.AX', portfolio_summary_override='supplied account')
    assert len(calls) == 1 and captured[0]['position_facts'] == snapshot
    stored = json.loads(next((root/'.evidence/contexts').glob('*.json')).read_text())['payload']
    assert stored['inputs']['position_facts'] == snapshot


from tests.test_prepare_committee import _mock_world


def test_coordinator_seals_and_discloses_the_same_shared_position_loader_result(_mock_world, monkeypatch):
    from openinvest.core.memory_store import MemoryStore
    from openinvest.core.runner import coordinator
    snapshot = build_position_facts('TEST.AX')
    calls = []
    monkeypatch.setattr(coordinator, 'load_position_facts', lambda *a, **k: calls.append((a, k)) or snapshot)
    result = coordinator.prepare_committee_brief('TEST.AX')
    assert len(calls) == 1 and result['position_facts'] == snapshot
    stored = json.loads((MemoryStore().root/'.evidence/contexts'/f"{result['context_id']}.json").read_text())['payload']
    assert stored['inputs']['position_facts'] == snapshot
    for role in ('risk_round1', 'risk_round2_after_quant', 'cio'):
        assert snapshot['position_ref'] in result['prompts'][role]
