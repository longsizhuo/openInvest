from openinvest.services.price_alerts import evaluate_symbol

HOLDING={'symbol':'NDQ.AX','units':20,'avg_cost':100,'cost_currency':'AUD','unit_label':'份'}
GRADE={'cost_basis':'client_recorded','current_quantity':'client_recorded','valuation_unit_mapping':{'qualified':True}}

def values(**kwargs):
    return {r['rule']:r for r in evaluate_symbol('NDQ.AX',{'drawdown_from_cost_pct':5,'price_below':95},HOLDING,price=90,**kwargs)}

def test_estimated_or_missing_cost_never_triggers_cost_based_exit_line():
    result=values(native_quote_qualified=True,quote_currency='AUD')
    assert result['drawdown_from_cost_pct']['current'] is None and not result['drawdown_from_cost_pct']['breached']
    assert result['price_below']['breached']
    bad={**GRADE,'cost_basis':'estimated_or_proxy_unreconciled'}
    assert not values(cost_qualification=bad,native_quote_qualified=True,quote_currency='AUD')['drawdown_from_cost_pct']['breached']

def test_qualified_same_unit_cost_can_be_observed_without_claiming_execution():
    result=values(cost_qualification=GRADE,native_quote_qualified=True,quote_currency='AUD')
    assert result['drawdown_from_cost_pct']['breached'] and result['drawdown_from_cost_pct']['current']==-10

def test_foreign_currency_or_reference_quote_cannot_borrow_native_cost():
    assert not values(cost_qualification=GRADE,native_quote_qualified=True,quote_currency='CNY')['drawdown_from_cost_pct']['breached']
    assert not values(cost_qualification=GRADE,native_quote_qualified=False,quote_currency='AUD')['drawdown_from_cost_pct']['breached']


def test_verified_major_currency_listing_is_not_rejected_by_exchange_suffix():
    holding={**HOLDING,'symbol':'SHEL.L','cost_currency':'GBP','unit_label':'share'}
    result=evaluate_symbol('SHEL.L',{'drawdown_from_cost_pct':5},holding,price=90,
        cost_qualification=GRADE,native_quote_qualified=True,quote_currency='GBP')
    assert result[0]['breached']
    assert not evaluate_symbol('SHEL.L',{'drawdown_from_cost_pct':5},holding,price=9000,
        cost_qualification=GRADE,native_quote_qualified=False,quote_currency='GBp')[0]['breached']
