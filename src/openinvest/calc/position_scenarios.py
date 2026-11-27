"""Point-price scenarios in native units; no forecasts, trades or account IO."""
from __future__ import annotations

import math
import re

UNIT_ALIASES={'share':'share','shares':'share','per_share':'share','股':'share','份':'share','unit':'share','units':'share',
              'g':'g','gram':'g','grams':'g','克':'g',
              'oz':'oz','troy_oz':'oz','盎司':'oz'}

def canonical_unit(value):
    if not isinstance(value,str):return None
    value=value.strip()
    return UNIT_ALIASES.get(value) or (value if re.fullmatch(r'[A-Z][A-Z0-9]{1,11}',value) else None)


def number(value):
    if not isinstance(value,(int,float)) or isinstance(value,bool):return False
    try:return math.isfinite(value)
    except (OverflowError,ValueError):return False


def no_trade_price_scenario(position, target_price, currency, unit):
    """Each monetary metric retains its own baseline and source dependencies.

    Unknown cost does not invalidate an observed quantity/current-price change.
    A no-trade increment of zero never establishes a historical realized total
    or a zero current-value decline. A point-price scenario is not a path MDD.
    """
    quantity=position.get('quantity') or {};quote=position.get('quote') or {};cost=position.get('cost') or {}
    if not number(target_price) or target_price<0:
        raise ValueError('scenario_target_price_invalid')
    q=quantity.get('value');p=quote.get('price');c=cost.get('value')
    q_ok=quantity.get('status')=='qualified' and number(q) and bool(quantity.get('source_ref')) and quantity.get('unit')==unit
    p_ok=(quote.get('status')=='qualified' and number(p) and p>0 and bool(quote.get('source_ref'))
          and quote.get('currency')==currency and quote.get('unit')==unit)
    c_ok=(cost.get('status')=='qualified' and number(c) and c>=0 and bool(cost.get('source_ref'))
          and cost.get('currency')==currency and cost.get('unit')==unit)
    def product(*values):
        try:
            value=math.prod(values)
            return value if math.isfinite(value) else None
        except (OverflowError,ValueError):return None
    values={'current_market_value':product(q,p) if q_ok and p_ok else None,
            'scenario_market_value':product(q,target_price) if q_ok and p_ok else None,
            'mark_to_market_change':product(q,target_price-p) if q_ok and p_ok else None,
            'current_unrealized_pnl':product(q,p-c) if q_ok and p_ok and c_ok else None,
            'scenario_unrealized_pnl':product(q,target_price-c) if q_ok and p_ok and c_ok else None,
            'realized_pnl_change':0.0,'realized_pnl_total':None,'maximum_drawdown':None}
    basis={'current_market_value':'quantity_times_current_mark','scenario_market_value':'quantity_times_scenario_mark',
           'mark_to_market_change':'scenario_mark_minus_current_mark','current_unrealized_pnl':'current_mark_minus_cost',
           'scenario_unrealized_pnl':'scenario_mark_minus_cost','realized_pnl_change':'assumed_no_trade_increment',
           'realized_pnl_total':'execution_history_required','maximum_drawdown':'price_path_required'}
    reasons=[]
    if not q_ok:reasons.append('native_quantity_unverified')
    if not p_ok:reasons.append('native_current_mark_unverified')
    if not c_ok:reasons.append('cost_not_available_in_scenario_units')
    return {'schema':'openinvest.no_trade_price_scenario.v1','currency':currency,'unit':unit,
            'target_price':target_price,'assumption':'no_trade','quote_asof':quote.get('asof'),
            'metrics':{name:{'value':value,'basis':basis[name],
                            'status':'unknown' if value is None else 'assumption' if name=='realized_pnl_change' else 'computed'}
                       for name,value in values.items()},
            'source_refs':{'quantity':quantity.get('source_ref'),'quote':quote.get('source_ref'),'cost':cost.get('source_ref')},
            'unknown_dependencies':reasons,'forecast_probability':None,'execution_authorized':False,
            'assumptions':['fixed_quantity','no_trades','no_fees_dividends_or_fx_changes'],
            'not_a_worst_loss_bound':True}
