import copy
import pytest
from openinvest.core.evidence_eligibility import digest
from openinvest.calc.position_scenarios import no_trade_price_scenario
from openinvest.core.committee.scenarios import validate_scenarios,apply_scenario_validation,render_scenarios


def position():
    value={'schema':'openinvest.position_facts.v1','symbol':'TEST','ledger_domain':'synthetic_fixture',
        'quantity':{'value':20,'unit':'unit','status':'qualified','source_ref':'fixture.quantity'},
        'quote':{'price':99.7565,'currency':'CNY','unit':'unit','asof':'2025-01-02T00:00:00Z','status':'qualified','source_ref':'fixture.price'},
        'cost':{'value':95,'currency':'CNY','unit':'unit','status':'qualified','source_ref':'fixture.cost'}}
    value['position_ref']=digest(value);return value

def scenario(p):return {'scenario_id':'price_at_cost','position_ref':p['position_ref'],'target_price':95,'currency':'CNY','unit':'unit','assumption':'no_trade'}

def test_point_scenario_separates_current_value_cost_and_realization():
    p=position();v=no_trade_price_scenario(p,95,'CNY','unit');m=v['metrics']
    assert m['current_market_value']['value']==pytest.approx(1995.13)
    assert m['scenario_market_value']['value']==1900
    assert m['mark_to_market_change']['value']==pytest.approx(-95.13)
    assert m['current_unrealized_pnl']['value']==pytest.approx(95.13)
    assert m['scenario_unrealized_pnl']['value']==0 and m['realized_pnl_change']['value']==0
    assert m['realized_pnl_total']['value'] is None and m['maximum_drawdown']['value'] is None

@pytest.mark.parametrize('target,expected',[(105,104.87),(90,-195.13),(0,-1995.13)])
def test_scenario_signs_come_from_current_price_not_cost(target,expected):
    assert no_trade_price_scenario(position(),target,'CNY','unit')['metrics']['mark_to_market_change']['value']==pytest.approx(expected)

def test_unknown_cost_does_not_hide_known_current_value_loss():
    p=position();p['cost']={'value':None,'status':'unknown','unit':'unit','currency':'CNY'}
    m=no_trade_price_scenario(p,95,'CNY','unit')['metrics']
    assert m['mark_to_market_change']['value']==pytest.approx(-95.13) and m['scenario_unrealized_pnl']['value'] is None

@pytest.mark.parametrize('key',['quantity','quote'])
def test_unverified_dependency_is_unknown_not_zero(key):
    p=position();p[key]['status']='unknown'
    m=no_trade_price_scenario(p,95,'CNY','unit')['metrics']
    assert m['mark_to_market_change']['value'] is None and m['scenario_market_value']['value'] is None

@pytest.mark.parametrize('override,code',[({'currency':'USD'},'scenario_currency_mismatch'),({'unit':'oz'},'scenario_unit_mismatch'),({'position_ref':'forged'},'scenario_position_binding_invalid'),({'context_ref':'other'},'scenario_context_binding_invalid')])
def test_wrong_reference_currency_or_unit_is_an_output_error(override,code):
    p=position();s={**scenario(p),**override};v=validate_scenarios(p,[s],context_id='sealed-context')
    assert code in [e['code'] for e in v['errors']]

@pytest.mark.parametrize('claim',[{'metric':'mark_to_market_change','value':0,'basis':'scenario_mark_minus_current_mark','currency':'CNY'},
 {'metric':'mark_to_market_change','value':0,'basis':'scenario_mark_minus_cost','currency':'CNY'},
 {'metric':'maximum_drawdown','value':0,'basis':'price_path_required','currency':'CNY'}])
def test_wrong_amount_baseline_or_unobserved_path_never_becomes_qualified(claim):
    p=position();s=scenario(p);s['claimed_metrics']=[claim];v=validate_scenarios(p,[s])
    proposal={'verdict':'ACCUMULATE','alloc_cny':1000,'reasoning_review':{'status':'passed'}}
    apply_scenario_validation(proposal,v)
    assert proposal['verdict']=='ACCUMULATE' and proposal['alloc_cny']==0 and proposal['decision_status']=='REVIEW_REQUIRED'
    assert not v['source_validity_changed']

def test_correct_typed_table_cannot_wash_an_untyped_freeform_pressure_claim():
    p=position();s=scenario(p)
    v=validate_scenarios(p,[s],memo='stress_scenario_loss_cny: opaque freeform financial arithmetic')
    assert v['errors'][0]['code']=='untyped_financial_statement' and v['results'][0]['metrics']['mark_to_market_change']['value']==pytest.approx(-95.13)

def test_genuinely_zero_and_unknown_are_not_interchanged_in_rendering():
    p=position();s=scenario(p);v=validate_scenarios(p,[s]);text=render_scenarios(v)
    assert '-95.13 CNY' in text and '+0.00 CNY' in text
    assert not v['errors']


@pytest.mark.parametrize('memo',['压力测试：若价格回落至成本，最大账面回撤约为0 CNY的已实现损失。',
    '若回到成本，账面回撤为零。','Unrealized drawdown is zero CNY, because no sale means realized PnL is zero.'])
def test_correct_table_and_wrong_ordinary_financial_prose_are_still_unqualified(memo):
    p=position();v=validate_scenarios(p,[scenario(p)],memo=memo)
    assert v['errors'] and v['errors'][0]['code']=='untyped_financial_narrative'
    verdict={'verdict':'BUY','alloc_cny':1000,'reasoning_review':{'status':'passed'}}
    apply_scenario_validation(verdict,v)
    assert verdict['verdict']=='BUY' and verdict['alloc_cny']==0 and verdict['decision_status']=='REVIEW_REQUIRED'

def test_market_statistics_and_unknown_account_information_are_not_financial_claims():
    p=position()
    for text in ['ATR1.5%、RSI60，MA120上方，但波动已有变化。','一旦市场变化应复核；持仓成本未知。','成本未知，未来2周应复核；VIX 25，ATR 1.5%。',
                 'SIGNAL: unknown\nSUGGESTED_ALLOC_CNY: 1000\nSCENARIO_ANALYSIS_JSON: []']:
        assert not validate_scenarios(p,[],memo=text)['errors']

def test_aliases_do_not_convert_grams_to_ounces():
    p=position();s=scenario(p);s['unit']='份'
    assert not validate_scenarios(p,[s])['errors']
    s['unit']='g';assert validate_scenarios(p,[s])['errors']


@pytest.mark.parametrize('text',['当前持仓风险来自VIX 16和RSI 60.24','资金成本受10年期美债收益率影响','Try 2 market horizons before drawing a conclusion.'])
def test_market_metric_numbers_do_not_become_owned_pnl_claims(text):
    assert not validate_scenarios(position(),[],memo=text)['errors']

@pytest.mark.parametrize('text',['EXPECTED_PATH: 当前账户回撤为0 CNY。','REENTRY_CONDITION: 账面损失0 CNY时安全。',
 'CONFIDENCE: 0.6 当前账户回撤为0 CNY。','0%的浮亏说明无需担心'])
def test_narrative_headers_or_reversed_pnl_word_order_cannot_bypass_typed_coverage(text):
    p=position();assert validate_scenarios(p,[scenario(p)],memo=text)['errors']


def test_every_retained_text_leaf_and_wrong_json_shape_are_checked():
    p=position();s=scenario(p);wrong='账户损失为0 CNY'
    for fields in [{'market_assessment':wrong},{'market_assessment':{'horizon':wrong}},
                   {'alternatives':[{'constraint_refs':[wrong]}]}, {'expected_path':wrong},
                   {'review_plan':{'change_reason':wrong,'conditions':[]}}]:
        assert validate_scenarios(p,[s],fields=fields)['errors']
    assert validate_scenarios(p,[s],memo='MARKET_ASSESSMENT_JSON: "'+wrong+'"')['errors']
    s['claimed_metrics']=[{'metric':'mark_to_market_change','value':-95.13,'basis':'scenario_mark_minus_current_mark','currency':'CNY','statement':wrong}]
    assert validate_scenarios(p,[s])['errors']


def test_combined_price_unit_is_a_lexical_alias_only_when_currency_agrees():
    p=position();s=scenario(p);s['unit']='CNY/unit'
    result=validate_scenarios(p,[s])
    assert not result['errors'] and result['results'][0]['metrics']['mark_to_market_change']['value']==pytest.approx(-95.13)
    s['unit']='USD/unit'
    assert any(e['code']=='scenario_unit_currency_prefix_mismatch' for e in validate_scenarios(p,[s])['errors'])


def test_only_same_market_price_and_unit_may_reference_the_supplied_mark():
    ref={'price':3954.5,'currency':'USD','unit':'oz','status':'qualified','source_ref':'sealed-market-source','scope':'market_snapshot_not_position_quote','asof':'2026-10-30T13:15:55Z'}
    assert not validate_scenarios(None,[],memo='市场判断：现价约3954.50美元/盎司',market_reference=ref)['errors']
    for text in ['市场判断：现价约3954.50人民币/克','市场判断：现价约3954.50美元/克',
                 '账户账面亏损为3954.50美元','市场判断：现价约3955美元/盎司',
                 '市场现价约3954.50美元/盎司，但账户亏损0 CNY']:
        assert validate_scenarios(None,[],memo=text,market_reference=ref)['errors']


def test_per_share_is_an_explicit_same_unit_alias_not_a_currency_conversion():
    p=position();s=scenario(p);s['unit']='per_share'
    assert not validate_scenarios(p,[s])['errors']


def test_market_reference_consumes_unknown_units_and_requires_the_same_positive_price():
    ref={'status':'qualified','price':3954.5,'currency':'USD','unit':'oz','asof':'2026-10-30',
         'scope':'market_snapshot_not_position_quote','source_ref':'source:supplied'}
    for text in ('现价3954.50美元/公斤','现价3954.50美元/100g','现价3954.50美元/oz/kg',
                 '现价3954.5049美元/盎司'):
        assert validate_scenarios(None,[],memo=text,market_reference=ref)['errors'],text
    ref.update(price=.004,unit='share')
    assert validate_scenarios(None,[],memo='现价0 USD/share',market_reference=ref)['errors']
    assert not validate_scenarios(None,[],memo='现价0.004 USD/share',market_reference=ref)['errors']


@pytest.mark.parametrize('text',[
    'HOLD 在本情境中完全适用且成本不为零：账面市值变化不会因为不卖而消失',
    'Waiting cost is not zero: mark-to-market losses persist without selling',
])
def test_qualitative_nonzero_cost_is_not_a_zero_pnl_amount(text):
    assert not validate_scenarios(position(),[],memo=text)['errors']


@pytest.mark.parametrize('text',[
    '没有卖出，所以账面损失为零',
    '成本不为零：账户亏损为0 CNY',
    '成本不为零：账面损失为零',
    'Waiting cost is not zero: mark-to-market loss is zero',
    'Not zero: position value 100 CNY',
])
def test_negated_cost_does_not_exempt_a_separate_numeric_account_claim(text):
    assert validate_scenarios(position(),[],memo=text)['errors']
