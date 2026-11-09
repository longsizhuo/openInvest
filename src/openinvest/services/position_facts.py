"""Bind typed position inputs to supplied account and market observations.

This module never fetches prices, reads an account, or infers a balance from
prose. Qualification describes individual supplied fields, not permission to
trade, learn, or treat a reference proxy as a native account quotation.
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping

from openinvest.core.evidence_eligibility import digest
from openinvest.calc.position_scenarios import canonical_unit as _unit


SCHEMA = 'openinvest.position_facts.v1'


def _number(value, *, positive=False):
    if isinstance(value, bool) or value is None or value == '':
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) and (value > 0 if positive else value >= 0) else None


def _currency(value):
    return value if isinstance(value, str) and re.fullmatch(r'[A-Z]{3}', value) and value not in {'GBX', 'ZAC', 'ILA'} else None


def _native_quote(symbol, frame, current_price, holding):
    """Use only the latest price actually observed in this supplied frame."""
    from openinvest.services.symbol_map import canonical_symbol
    from openinvest.utils.market_observation import frame_observation
    from openinvest.utils.quotes import _native_contract, _fx_contract
    unknown = {'price': None, 'currency': None, 'unit': None, 'asof': None,
               'status': 'unknown', 'source_ref': None, 'price_basis': None}
    if frame is None or frame.empty or 'Close' not in frame:
        return unknown
    price = _number(frame['Close'].iloc[-1], positive=True)
    supplied = _number(current_price, positive=True)
    if price is None or supplied is None or supplied != price:
        return unknown
    source = frame_observation(frame)
    asof = source.get('quote_asof')
    if (source.get('quote_fresh') is not True or source.get('latest_price_observed') is not True
            or not asof
            or not source.get('symbol') or canonical_symbol(source['symbol']) != canonical_symbol(symbol)):
        return unknown
    try:
        import pandas as pd
        if not source.get('bar_asof') or pd.Timestamp(source['bar_asof']) != pd.Timestamp(frame.index[-1]):
            return unknown
    except (TypeError, ValueError):
        return unknown
    if holding:
        if (canonical_symbol(holding.get('native_instrument') or symbol) != canonical_symbol(symbol)
                or canonical_symbol(holding.get('yfinance_proxy') or symbol) != canonical_symbol(symbol)):
            return unknown
    contract = _native_contract(source, symbol)
    fx = _fx_contract(symbol)
    if fx is not None:
        base, currency = fx
        if _currency(source.get('currency')) != currency:
            return unknown
        if any(source.get(key) and canonical_symbol(source[key]) != canonical_symbol(symbol)
               for key in ('symbol', 'provider_symbol')):
            return unknown
        contract = (currency, base, {base})
    if contract is None:
        return unknown
    currency, unit, _ = contract
    unit = _unit(unit)
    if _currency(currency) is None or unit is None:
        return unknown
    # A bank-gold gram balance cannot borrow a futures USD/oz mark, even
    # though both happen to use the runtime's GC=F booking symbol.
    declared = _unit(holding.get('unit_label')) if holding else None
    if declared is not None and declared != unit:
        return unknown
    try:
        reference = 'market_observation:' + digest({'symbol': symbol, 'price': price,
            'bar': str(frame.index[-1]), 'source_observation': source})
    except (TypeError, ValueError):
        return unknown
    return {'price': price, 'currency': currency, 'unit': unit, 'asof': asof,
            'status': 'qualified', 'source_ref': reference, 'price_basis': source.get('price_basis')}


def build_position_facts(symbol, *, holding=None, ledger_evidence=None, frame=None, current_price=None):
    """Build one content-addressed snapshot; unknown values are never zero-filled."""
    from openinvest.services.symbol_map import canonical_symbol
    from openinvest.services.ledger_evidence import valuation_unit_mapping
    from openinvest.utils.quotes import _fx_contract
    from openinvest.utils.portfolio_summary import balance_field_qualified, source_status_readable
    symbol = str(symbol)
    holding = dict(holding) if isinstance(holding, Mapping) else None
    evidence = ledger_evidence if isinstance(ledger_evidence, Mapping) else {}
    readable = source_status_readable(evidence)
    fingerprint = evidence.get('content_sha256')
    bound = readable and isinstance(fingerprint, str) and re.fullmatch(r'[a-f0-9]{64}', fingerprint)
    grades = evidence.get('holdings') or {}
    grade = (grades.get(symbol) or {}) if isinstance(grades, Mapping) else {}
    grade = grade if isinstance(grade, Mapping) else {}
    mapping = grade.get('valuation_unit_mapping') or {}
    quantity = {'value': None, 'unit': None, 'status': 'unknown', 'source_ref': None}
    cost = {'value': None, 'currency': None, 'unit': None, 'status': 'unknown', 'source_ref': None}
    if (holding and bound and not holding.get('is_tracking_only')
            and canonical_symbol(holding.get('symbol') or '') == canonical_symbol(symbol)
            and isinstance(mapping, Mapping) and mapping.get('qualified') is True
            and valuation_unit_mapping(holding)[0]):
        unit = _unit(holding.get('unit_label'))
        pair = _fx_contract(symbol)
        if (unit not in {'share', 'g', 'oz'} and not (pair and unit == pair[0])
                and not (unit and symbol.upper().startswith(unit + '-'))):
            unit = None
        value = _number(holding.get('units'))
        if unit is not None and value is not None and balance_field_qualified(grade, 'current_quantity'):
            quantity = {'value': value, 'unit': unit, 'status': 'qualified',
                        'source_ref': f'ledger:{fingerprint}:holdings:{symbol}:current_quantity'}
        value = _number(holding.get('avg_cost'))
        currency = _currency(holding.get('cost_currency'))
        if unit is not None and currency is not None and value is not None and balance_field_qualified(grade, 'cost_basis'):
            cost = {'value': value, 'currency': currency, 'unit': unit, 'status': 'qualified',
                    'source_ref': f'ledger:{fingerprint}:holdings:{symbol}:cost_basis'}
    snapshot = {'schema': SCHEMA, 'symbol': symbol,
                'ledger_domain': (evidence.get('ledger_domain') or 'unknown') if readable else 'unknown',
                'quantity': quantity, 'quote': _native_quote(symbol, frame, current_price, holding),
                'cost': cost, 'loss_budget': {'status': 'unknown', 'value': None, 'source_ref': None}}
    return {**snapshot, 'position_ref': digest(snapshot)}


def build_market_reference(symbol, *, frame=None, current_price=None):
    """Describe a supplied native market snapshot, independently of a wallet.

    A completed close is a dated reference, not a fresh executable quotation.
    No holdings, costs, account currency, source grants or network IO enter this
    snapshot. It is deliberately outside the position content-address.
    """
    from openinvest.services.symbol_map import canonical_symbol
    from openinvest.utils.market_observation import frame_observation, observation_now, utc_time
    from openinvest.utils.quotes import _native_contract, _fx_contract
    unknown = {'price': None, 'currency': None, 'unit': None, 'asof': None,
               'status': 'unknown', 'source_ref': None,
               'scope': 'market_snapshot_not_position_quote', 'price_basis': None,
               'freshness': 'unknown', 'market_state': 'unknown', 'is_fresh': False}
    symbol = str(symbol)
    if frame is None or frame.empty or 'Close' not in frame:
        return unknown
    price = _number(frame['Close'].iloc[-1], positive=True)
    if price is None or _number(current_price, positive=True) != price:
        return unknown
    source = frame_observation(frame)
    if (source.get('contract') != 'openinvest.market_observation.v1'
            or not isinstance(source.get('fetch_status'), str) or source['fetch_status'] not in {'ok', 'cached'}
            or source.get('latest_price_observed') is not True
            or source.get('history_basis_qualified') is not True
            or not isinstance(source.get('price_basis'), str) or source['price_basis'] not in {'adjusted_close', 'raw_close', 'fx_rate'}
            or not source.get('symbol') or canonical_symbol(source['symbol']) != canonical_symbol(symbol)):
        return unknown
    try:
        import pandas as pd
        if not source.get('bar_asof') or pd.Timestamp(source['bar_asof']) != pd.Timestamp(frame.index[-1]):
            return unknown
    except (TypeError, ValueError):
        return unknown
    contract = _native_contract(source, symbol)
    fx = _fx_contract(symbol)
    if fx is not None:
        base, currency = fx
        if _currency(source.get('currency')) != currency:
            return unknown
        if any(source.get(key) and canonical_symbol(source[key]) != canonical_symbol(symbol)
               for key in ('symbol', 'provider_symbol')):
            return unknown
        contract = (currency, base, {base})
    if contract is None:
        return unknown
    currency, unit, _ = contract
    unit = _unit(unit)
    if _currency(currency) is None or unit is None:
        return unknown
    now, received = utc_time(observation_now()), utc_time(source.get('received_at'))
    if now is None or received is None or received > now:
        return unknown
    state = source.get('market_state')
    state = state if isinstance(state, str) and state in {'open', 'closed', 'unknown'} else 'unknown'
    fresh = source.get('quote_fresh') is True and state == 'open'
    stamp = utc_time(source.get('quote_asof')) if fresh else None
    if not fresh:
        label = frame.index[-1].date().isoformat()
        dates = source.get('completed_session_dates')
        ends = source.get('completion_ends')
        if (source.get('interval') != '1d' or source.get('latest_bar_completed') is not True
                or source.get('session_date') != label or not isinstance(dates, list) or label not in dates
                or not isinstance(ends, Mapping)):
            return unknown
        stamp = utc_time(ends.get(label))
    if stamp is None or not stamp <= received:
        return unknown
    from openinvest.calc.timeframe_analysis import display_market_price
    literal=display_market_price(price)
    try:
        reference = 'market_reference:' + digest({'symbol': symbol, 'price': price,
            'display_price_literal':literal,
            'currency': currency, 'unit': unit, 'asof': stamp.isoformat(),
            'scope': unknown['scope'], 'bar': str(frame.index[-1]), 'source_observation': source})
    except (TypeError, ValueError):
        return unknown
    return {**unknown, 'price': price, 'display_price_literal':literal, 'currency': currency, 'unit': unit,
            'asof': stamp.isoformat(), 'status': 'qualified', 'source_ref': reference,
            'price_basis': source['price_basis'], 'is_fresh': fresh,
            'freshness': 'observed_quote' if fresh else 'completed_session_reference',
            'market_state': state}


def market_reference_context(reference):
    """Keep a dated market-price reference separate from position money."""
    import json
    return ('=== TYPED MARKET REFERENCE ===\n' + json.dumps(reference, ensure_ascii=False, sort_keys=True)
            + '\nThis is only an asof market snapshot, not a position/bank quotation, PnL, '
              'loss budget or trade permission. is_fresh=false never means a current executable price; '
              'retain the stated asof when describing a completed-session reference. '
              'Do not infer wallet quantity, cost, currency conversion or realized loss from this price.')


def position_facts_context(facts):
    """Render sealed facts and computed baselines without granting authority."""
    import json
    from openinvest.calc.position_scenarios import no_trade_price_scenario,number
    quote=facts.get('quote') or {};cost=facts.get('cost') or {}
    baselines=[]
    if quote.get('currency') and quote.get('unit'):
        for name,value in [('current_mark',quote.get('price') if quote.get('status')=='qualified' else None),('cost_reference',cost.get('value') if cost.get('status')=='qualified' and cost.get('currency')==quote.get('currency') and cost.get('unit')==quote.get('unit') else None)]:
            if number(value) and value>=0:
                result=no_trade_price_scenario(facts,value,quote['currency'],quote['unit'])
                baselines.append({'reference':name,**result})
    return ('=== TYPED POSITION FACTS ===\n' + json.dumps(facts, ensure_ascii=False, sort_keys=True)
            + '\nThese are field-qualified observations, not a loss budget or trading/learning permission. '
              'Unknown typed budget does not negate an explicit user constraint in another supplied source. '
              'Unknown values are unknown, not zero. Do not substitute a realized-PnL delta for a change '
              'in current market value; a no-trade assumption does not eliminate mark-to-market loss.'
            + '\nBACKEND_FINANCIAL_BASELINES_JSON: '+json.dumps(baselines,ensure_ascii=False,sort_keys=True))
