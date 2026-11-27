"""A native market snapshot never upgrades an unknown wallet quotation."""
from datetime import datetime, timedelta, timezone
import json

import pandas as pd
import pytest

from openinvest.core.evidence_eligibility import digest
from openinvest.services.position_facts import build_market_reference, build_position_facts, market_reference_context
from openinvest.utils import market_observation


NOW = datetime(2026, 11, 9, 15, 5, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(market_observation, 'observation_now', lambda: NOW)


def frame(symbol='GC=F', *, price=3954.5, currency='USD', kind='FUTURE', closed=False):
    rows = pd.DataFrame({'Close': [price]}, index=[pd.Timestamp(NOW.date())])
    end = NOW-timedelta(minutes=5) if closed else NOW+timedelta(hours=1)
    rows.attrs['source_observation'] = {**market_observation.history_observation(rows, now=NOW, metadata={
        'symbol': symbol, 'currency': currency, 'instrumentType': kind, 'exchangeTimezoneName': 'UTC',
        'regularMarketTime': (end if closed else NOW-timedelta(seconds=1)).timestamp(),
        'marketState': 'CLOSED' if closed else 'REGULAR',
        'currentTradingPeriod': {'regular': {'start': (NOW-timedelta(hours=6)).timestamp(), 'end': end.timestamp()}}}),
        'symbol': symbol, 'history_basis_qualified': True, 'latest_price_observed': True}
    return rows


def reference(rows, symbol='GC=F', current_price=None):
    return build_market_reference(symbol, frame=rows,
        current_price=rows['Close'].iloc[-1] if current_price is None else current_price)


def test_market_reference_is_exact_native_price_and_does_not_change_unknown_wallet():
    rows = frame()
    holding = {'symbol': 'GC=F', 'kind': 'metal', 'proxy_kind': 'gold_cny_per_gram',
               'units': 20, 'avg_cost': 95, 'cost_currency': 'CNY', 'unit_label': '克'}
    evidence = {'status': 'requires_reconciliation', 'content_sha256': digest({'account': 'synthetic'}),
                'ledger_domain': 'client_reported', 'holdings': {'GC=F': {
                    'current_quantity': 'estimated_or_proxy_unreconciled',
                    'cost_basis': 'estimated_or_proxy_unreconciled', 'valuation_unit_mapping': {'qualified': True}}}}
    before = build_position_facts('GC=F', holding=holding, ledger_evidence=evidence, frame=rows, current_price=3954.5)
    result = reference(rows)
    after = build_position_facts('GC=F', holding=holding, ledger_evidence=evidence, frame=rows, current_price=3954.5)
    assert result['status'] == 'qualified' and result['price'] == 3954.5
    assert (result['currency'], result['unit']) == ('USD', 'oz')
    assert result['asof'] == (NOW-timedelta(seconds=1)).isoformat() and result['is_fresh'] is True
    assert result['scope'] == 'market_snapshot_not_position_quote'
    assert before == after and 'market_reference' not in after
    assert all(after[key]['value' if key != 'quote' else 'price'] is None for key in ('quantity', 'cost', 'quote'))
    assert after['position_ref'] == digest({k: v for k, v in after.items() if k != 'position_ref'})


def test_completed_reference_retains_original_close_time_without_claiming_freshness():
    rows = frame(closed=True)
    result = reference(rows)
    assert result['status'] == 'qualified' and result['is_fresh'] is False
    assert result['freshness'] == 'completed_session_reference' and result['market_state'] == 'closed'
    assert result['asof'] == (NOW-timedelta(minutes=5)).isoformat()
    text = market_reference_context(result)
    assert result['asof'] in text and 'not a position/bank quotation' in text
    assert 'is_fresh=false never means a current executable price' in text


def test_source_display_round_trip_does_not_create_a_price_tolerance():
    from openinvest.calc.timeframe_analysis import display_market_price
    from openinvest.core.committee.scenarios import validate_scenarios
    rows=frame('AAPL',price=202.50999450683594,currency='USD',kind='EQUITY')
    ref=reference(rows,'AAPL')
    assert ref['display_price_literal']==display_market_price(rows.Close.iloc[-1])=='202.5100'
    assert not validate_scenarios(None,[],memo='市场价格202.5100USD/share',market_reference=ref)['errors']
    assert validate_scenarios(None,[],memo='市场价格202.5149USD/share',market_reference=ref)['errors']
    rows=frame('AAPL',price=.00000004,currency='USD',kind='EQUITY')
    tiny=reference(rows,'AAPL')
    assert float(tiny['display_price_literal'])>0
    assert validate_scenarios(None,[],memo='市场价格0USD/share',market_reference=tiny)['errors']


@pytest.mark.parametrize('change', ['failed', 'unobserved', 'mixed', 'basis', 'wrong_source', 'wrong_provider',
                                  'wrong_bar', 'wrong_price', 'missing_currency', 'minor_currency', 'contract'])
def test_bad_source_or_price_binding_is_unknown(change):
    rows = frame();source = rows.attrs['source_observation'];price = 3954.5
    if change == 'failed': rows.attrs['yf_fetch_failed'] = True
    elif change == 'unobserved': source['latest_price_observed'] = False
    elif change == 'mixed': source['history_basis_qualified'] = False
    elif change == 'basis': source['price_basis'] = 'published_nav_reference'
    elif change == 'wrong_source': source['symbol'] = 'OTHER'
    elif change == 'wrong_provider': source['provider_symbol'] = 'OTHER'
    elif change == 'wrong_bar': source['bar_asof'] = '2000-01-01'
    elif change == 'wrong_price': price = 3954.51
    elif change == 'missing_currency': source.pop('currency')
    elif change == 'minor_currency': source['currency'] = 'GBp'
    else: source['contract'] = 'unverified-contract'
    result = reference(rows, current_price=price)
    assert result['status'] == 'unknown' and result['price'] is None and result['source_ref'] is None


@pytest.mark.parametrize('change', ['missing_end', 'future_end', 'before_end', 'not_completed', 'wrong_session', 'bad_end_shape', 'bad_dates_shape'])
def test_completed_reference_requires_an_end_observed_by_the_original_receipt(change):
    rows = frame(closed=True);source = rows.attrs['source_observation'];label = NOW.date().isoformat()
    if change == 'missing_end': source['completion_ends'] = {}
    elif change == 'future_end': source['completion_ends'][label] = (NOW+timedelta(minutes=1)).isoformat()
    elif change == 'before_end': source['received_at'] = (NOW-timedelta(hours=1)).isoformat()
    elif change == 'not_completed': source['latest_bar_completed'] = False
    elif change == 'bad_end_shape': source['completion_ends'] = ['not-an-end-mapping']
    elif change == 'bad_dates_shape': source['completed_session_dates'] = None
    else: source['session_date'] = '2000-01-01'
    assert reference(rows)['status'] == 'unknown'


def test_a_dated_but_uncompleted_old_quote_is_not_promoted_to_a_closed_reference():
    rows = frame();rows.attrs['source_observation']['quote_asof'] = (NOW-timedelta(hours=1)).isoformat()
    assert reference(rows)['status'] == 'unknown'


def test_reference_uses_native_unit_aliases_and_same_listing_symbol_aliases():
    from openinvest.calc.position_scenarios import canonical_unit
    assert reference(frame())['unit'] == canonical_unit('盎司') == canonical_unit('troy_oz')
    rows = frame('00005.HK', currency='HKD', kind='EQUITY')
    rows.attrs['source_observation']['provider_symbol'] = '0005.HK'
    result = reference(rows, '00005.HK')
    assert result['status'] == 'qualified' and result['unit'] == canonical_unit('份') == 'share'


def test_native_fx_reference_keeps_pair_orientation_and_never_infers_an_account_currency():
    rows = frame('USDCNY=X', price=7.2, currency='CNY', kind='CURRENCY')
    result = reference(rows, 'USDCNY=X')
    assert result['status'] == 'qualified' and (result['currency'], result['unit']) == ('CNY', 'USD')


def test_reference_loader_has_no_account_or_fetch_dependency(monkeypatch):
    from openinvest.core.runner.loaders import load_market_reference
    monkeypatch.setattr('openinvest.utils.quotes.get_history_data', lambda *a, **k: pytest.fail('extra price fetch'))
    monkeypatch.setattr('openinvest.services.ledger_evidence.account_source_status', lambda *a: pytest.fail('private account read'))
    monkeypatch.setattr('openinvest.services.position_facts.build_position_facts', lambda *a, **k: pytest.fail('position dependency'))
    assert load_market_reference('GC=F', frame(), 3954.5)['status'] == 'qualified'


def test_primitive_carries_reference_to_risk_cio_and_report(monkeypatch):
    from openinvest.core.committee import debate
    market = reference(frame());seen = {}
    class Agent:
        last_tool_calls = []
        def __init__(self, role): self.role = role
        def run(self, text):
            seen[self.role] = text
            if self.role == 'cio': return 'VERDICT: HOLD\nCONFIDENCE: 0.5\nSUGGESTED_ALLOC_CNY: 0'
            return 'SIGNAL: unknown\nMARKET_RISK: unknown\nLIQUIDITY: unknown'
        def tool_call_summary(self): return ''
    monkeypatch.setattr(debate, '_create_agent', lambda prompt, role='unknown', **kw: Agent(role))
    monkeypatch.setattr('openinvest.services.reasoning_review.review_once', lambda *a, **k: {'status': 'passed'})
    result = debate.run_committee(asset={'symbol': 'GC=F'}, market_data='source', macro_view='source',
        portfolio_summary='unknown account', market_reference=market, persist_to_memory=False)
    for role in ('risk', 'cio'):
        assert market['source_ref'] in seen[role] and 'TYPED MARKET REFERENCE' in seen[role]
    assert result['report'].market_reference == market


def test_direct_entry_seals_and_passes_one_shared_reference_loader_result(monkeypatch):
    from openinvest.core.memory_store import MemoryStore
    from openinvest.core.runner import session
    from tests.test_committee_contract import _seed_minimal_memory, _mock_for_symbol_path
    root = MemoryStore().root;_seed_minimal_memory(root);captured = _mock_for_symbol_path(monkeypatch)
    snapshot = build_market_reference('TEST.AX');calls = []
    monkeypatch.setattr(session, 'load_market_reference', lambda *a, **k: calls.append(a) or snapshot)
    session.run_committee_for_symbol('TEST.AX', portfolio_summary_override='account')
    assert len(calls) == 1 and captured[0]['market_reference'] == snapshot
    sealed = json.loads(next((root/'.evidence/contexts').glob('*.json')).read_text())['payload']
    assert sealed['inputs']['market_reference'] == snapshot


from tests.test_prepare_committee import _mock_world


def test_coordinator_seals_and_discloses_one_shared_reference_loader_result(_mock_world, monkeypatch):
    from openinvest.core.memory_store import MemoryStore
    from openinvest.core.runner import coordinator
    snapshot = build_market_reference('TEST.AX');calls = []
    monkeypatch.setattr(coordinator, 'load_market_reference', lambda *a, **k: calls.append(a) or snapshot)
    result = coordinator.prepare_committee_brief('TEST.AX')
    assert len(calls) == 1 and result['market_reference'] == snapshot
    sealed = json.loads((MemoryStore().root/'.evidence/contexts'/f"{result['context_id']}.json").read_text())['payload']
    assert sealed['inputs']['market_reference'] == snapshot
    for role in ('risk_round1', 'risk_round2_after_quant', 'cio'):
        assert 'TYPED MARKET REFERENCE' in result['prompts'][role]
