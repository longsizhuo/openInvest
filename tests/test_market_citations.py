"""Source-bound report display must not create account facts or permissions."""
import copy
import hashlib
import json

import pytest

from openinvest.core.committee.cio_parse import parse_cio_memo
from openinvest.core.committee.scenarios import (
    apply_scenario_validation,
    decision_display_memo,
    market_citation_catalog,
    render_market_citations,
    validate_market_citations,
    validate_scenarios,
)


CONTEXT='synthetic-analysis-context'
REPORT='高盛称布伦特原油周内涨约6%至117美元。'


def catalog(text=REPORT):
    return market_citation_catalog({'event_brief':text})


def citation(**changes):
    return {'citation_id':'oil_report','source_id':'events','source_quote':REPORT,**changes}


def validate(items=None,**kwargs):
    return validate_scenarios(None,[],fields={'market_citations':[citation()] if items is None else items},
                              context_id=CONTEXT,market_sources=catalog(),**kwargs)


def test_catalog_selects_only_original_market_inputs_and_hashes_exact_bytes():
    facts={'market_data':'  Shares rose to USD 117.\n','macro_data':'DXY -0.8%',
           'event_brief':REPORT,'portfolio_summary':'账户损失117美元',
           'analyst_MACRO':'SIGNAL: risk_on','model_plans':'purchase USD 117'}
    sources=market_citation_catalog(facts)
    assert set(sources)=={'market','macro','events'}
    assert sources['market']['text']==facts['market_data']
    assert sources['market']['source_sha256']==hashlib.sha256(facts['market_data'].encode()).hexdigest()
    assert sources['events']['kind']=='normalized_reports_not_verified_truth'
    assert market_citation_catalog({'market_data':None,'macro_data':' ','event_brief':'not_provided'})=={}


@pytest.mark.parametrize('key,source_id,text',[
    ('event_brief','events',REPORT),
    ('market_data','market','Company shares rose to AUD 23.40.'),
    ('macro_data','macro','Gold was quoted at USD 2000/oz.'),
    ('event_brief','events','Bank of America reported gold at USD 2000/oz.'),
    ('event_brief','events','银行报道黄金现价2000美元/盎司。'),
])
def test_report_prices_are_generic_and_never_native_position_quotes(key,source_id,text):
    result=validate_scenarios(None,[],fields={'market_citations':[citation(source_id=source_id,source_quote=text)]},
                              context_id=CONTEXT,market_sources=market_citation_catalog({key:text}))
    assert not result['errors'] and result['results']==[]
    receipt=result['market_citation_validation'];row=receipt['citations'][0]
    assert row['context_ref']==CONTEXT and row['source_quote']==text
    assert row['source_sha256']==hashlib.sha256(text.encode()).hexdigest()
    assert row['quote_sha256']==hashlib.sha256(text.encode()).hexdigest()
    assert receipt['native_position_quote_qualification_granted'] is False
    assert receipt['source_validity_changed'] is False
    assert receipt['trading_or_learning_permission_granted'] is False
    assert result['trading_or_learning_permission_granted'] is False


@pytest.mark.parametrize('override',[
    {'source_quote':REPORT.replace('117','118')},
    {'source_quote':REPORT.replace('美元','澳元')},
    {'source_id':'market'},
    {'source_id':'account'},
    {'source_id':'analyst_MACRO'},
    {'source_id':'model_plans'},
    {'source_id':[]},
    {'context_ref':'different-context'},
    {'context_ref':{}},
    {'source_quote':''},
    {'source_quote':True},
    {'citation_id':'117'},
    {'citation_id':'money USD 117'},
    {'citation_id':'a'*65},
    {'price':117},
    {'status':'qualified'},
    {'source_sha256':'model-provided-hash'},
])
def test_bad_citation_is_rejected_without_formal_display(override):
    result=validate([citation(**override)])
    assert result['errors'] and not render_market_citations(result)
    verdict={'verdict':'ACCUMULATE','alloc_cny':1000}
    apply_scenario_validation(verdict,result)
    assert verdict['verdict']=='ACCUMULATE' and verdict['alloc_cny']==0
    assert verdict['decision_status']=='REVIEW_REQUIRED'


@pytest.mark.parametrize('items',[None,{},'117美元',[None],[{}]])
def test_citation_collection_schema_is_strict(items):
    result=validate_scenarios(None,[],fields={'market_citations':items},market_sources=catalog())
    assert result['errors']


def test_duplicate_ids_grant_no_exemptions_or_display():
    result=validate([citation(),citation()])
    assert any(e['code']=='market_citation_id_duplicate' for e in result['errors'])
    assert not result['market_citation_validation']['citations']
    assert not render_market_citations(result)


@pytest.mark.parametrize('memo',[
    '账户现金为[oil_report]。',
    '我的持仓账面损失为[oil_report]。',
    'Account cash is oil_report.',
    'My unrealized loss: [OIL_REPORT].',
    '账户现金：\n[oil_report]',
    '我的持仓账面损失，等于[oil_report]。',
    'Cash balance:\n[oil_report]',
    '[oil_report] equals account cash.',
])
def test_market_citation_id_cannot_substitute_for_a_personal_amount(memo):
    result=validate(memo=memo)
    assert any(e['code']=='market_citation_account_reference_forbidden' for e in result['errors'])
    assert not validate(memo='市场能源报道见[oil_report]。')['errors']


@pytest.mark.parametrize('memo',[
    '当前持仓的市场风险来自[oil_report]所述能源冲击。',
    'Current holdings face market risk from [oil_report].',
    '账面波动受到[oil_report]所述市场冲击影响。',
    'Account cash is not reported; market risk comes from [oil_report].',
])
def test_qualitative_market_causality_does_not_assign_a_report_price_to_the_account(memo):
    assert not validate(memo=memo)['errors']


def test_account_value_relationship_across_support_items_stays_visible():
    fields={'market_citations':[citation()], 'market_assessment':{'support':['账户现金：','[oil_report]']}}
    result=validate_scenarios(None,[],fields=fields,market_sources=catalog())
    assert any(e['code']=='market_citation_account_reference_forbidden' for e in result['errors'])


def test_overlapping_value_words_are_scanned_without_exponential_backtracking():
    from openinvest.core.committee.scenarios import _value_link
    assert _value_link('约为'*2000) is True
    assert _value_link('约为'*2000+'市场风险来自') is False
    assert _value_link('equal '*2000+'because') is False


def test_text_protocol_citation_id_has_the_same_account_scope_boundary():
    memo='MARKET_CITATIONS_JSON: '+json.dumps([citation()],ensure_ascii=False)+'\n账户现金为[oil_report]。'
    result=validate_scenarios(None,[],memo=memo,market_sources=catalog())
    assert any(e['code']=='market_citation_account_reference_forbidden' for e in result['errors'])


@pytest.mark.parametrize('text',[
    '布伦特117美元，账户现金为1000美元。',
    '油价117美元，账面亏损为零。',
    '持仓数量20份，成本95美元。',
    '预算117美元，单笔金额100美元。',
    '个人止损为117美元。',
    'Oil USD 117; my account PnL is zero.',
    'Gold USD 2000/oz; position quantity 20.',
])
def test_original_source_cannot_launder_account_numbers(text):
    result=validate_scenarios(None,[],fields={'market_citations':[citation(source_quote=text)]},
                              market_sources=catalog(text))
    assert any(e['code']=='market_citation_account_scope_forbidden' for e in result['errors'])
    assert not render_market_citations(result)


def test_only_exact_source_quote_slot_is_exempt():
    for fields in [
        {'market_citations':[citation()],'market_assessment':{'support':REPORT}},
        {'market_citations':[citation()],'alternatives':[{'counterevidence':REPORT}]},
        {'market_citations':[citation()],'memo':REPORT},
        {'market_citations':[citation(extra={'source_quote':'账户亏损0 CNY'})]},
    ]:
        result=validate_scenarios(None,[],fields=fields,market_sources=catalog())
        assert result['errors']
    assert validate(memo='账户亏损为0 CNY。')['errors']
    assert validate(memo=REPORT)['errors']
    assert not validate(memo='油价报道见[oil_report]；RSI 35、市场波动2%。')['errors']


def test_source_kind_and_hash_are_bound_and_no_extra_authority_is_accepted():
    for changes in [{'kind':'model_assessment_not_raw_fact_or_user_instruction'},
                    {'source_sha256':'forged'}, {'text':'different report'}]:
        sources=copy.deepcopy(catalog());sources['events'].update(changes)
        result=validate_market_citations([citation()],market_sources=sources)
        assert result['errors'] and not result['citations']


@pytest.mark.parametrize('context_ref',[None,CONTEXT])
def test_optional_context_is_derived_even_when_not_provided_by_model(context_ref):
    result=validate([citation(context_ref=context_ref)])
    assert not result['errors']
    assert result['market_citation_validation']['citations'][0]['context_ref']==CONTEXT
    engineering=validate_scenarios(None,[],fields={'market_citations':[citation()]},market_sources=catalog())
    assert not engineering['errors']
    assert engineering['market_citation_validation']['context_ref'] is None
    assert engineering['market_citation_validation']['trading_or_learning_permission_granted'] is False


def test_json_and_text_protocol_retain_and_validate_the_same_citations():
    items=[citation(context_ref=CONTEXT)]
    fields={'verdict':'HOLD','confidence':.6,'suggested_alloc_cny':0,'market_citations':items}
    text='VERDICT: HOLD\nCONFIDENCE: 0.6\nSUGGESTED_ALLOC_CNY: 0\nMARKET_CITATIONS_JSON: '+json.dumps(items,ensure_ascii=False)
    json_parsed=parse_cio_memo('市场报道见[oil_report]',fields=fields)
    text_parsed=parse_cio_memo(text)
    receipts=[]
    for memo,parsed in [('市场报道见[oil_report]',json_parsed),(text,text_parsed)]:
        assert parsed['market_citations']==items
        assert '_unrecognized_json_fields' not in parsed
        result=validate_scenarios(None,[],memo=memo,fields=parsed['_raw_proposal'],
                                  context_id=CONTEXT,market_sources=catalog())
        assert not result['errors']
        receipts.append(result['market_citation_validation'])
    assert receipts[0]==receipts[1]
    primitive=validate_scenarios(None,[],memo=text,context_id=CONTEXT,market_sources=catalog())
    assert not primitive['errors'] and primitive['market_citation_validation']==receipts[0]


@pytest.mark.parametrize('payload',['{"source_quote":"117美元"}','not-json','[]\nMARKET_CITATIONS_JSON: []'])
def test_bad_or_duplicate_text_protocol_is_not_displayable(payload):
    text='VERDICT: HOLD\nMARKET_CITATIONS_JSON: '+payload
    parsed=parse_cio_memo(text)
    result=validate_scenarios(None,[],memo=text,fields=parsed['_raw_proposal'],market_sources=catalog())
    assert result['errors'] and not render_market_citations(result)


def test_legacy_output_without_citations_keeps_its_old_checks():
    for memo,fields in [('VERDICT: HOLD',None),('qualitative',{'verdict':'HOLD'})]:
        parsed=parse_cio_memo(memo,fields=fields)
        assert 'market_citations' not in parsed
        result=validate_scenarios(None,[],memo=memo,fields=parsed['_raw_proposal'])
        assert not result['errors'] and not render_market_citations(result)
    assert validate_scenarios(None,[],memo=REPORT)['errors']


def test_display_separates_reports_from_advice_and_suppresses_untrusted_inputs():
    result=validate()
    for status in ['READY','REVIEW_REQUIRED','DATA_UNTRUSTED']:
        verdict={'verdict':'HOLD','decision_status':status,'reasoning_review':{'status':'passed'}}
        display=decision_display_memo(verdict,'油价报道见[oil_report]',result)
        if status=='DATA_UNTRUSTED':assert REPORT not in display
        else:
            assert REPORT in display and '仅核对原文存在' in display
            assert '不是持仓原生报价' in display
        if status=='REVIEW_REQUIRED':assert '油价报道见[oil_report]' not in display


def test_source_markdown_is_displayed_as_report_text_not_executable_formatting():
    text='Oil USD 117 [click](https://invalid.example)\n# heading <script>'
    result=validate_scenarios(None,[],fields={'market_citations':[citation(source_quote=text)]},
                              market_sources=catalog(text))
    assert not result['errors']
    rendered=render_market_citations(result)
    assert '[click](' not in rendered and '<script>' not in rendered
    assert '\n  > # heading' in rendered


@pytest.mark.parametrize('lang',['zh','en'])
def test_cio_prompt_declares_currency_report_channel_in_both_languages(monkeypatch,lang):
    monkeypatch.setenv('INVEST_LANG',lang)
    from openinvest.capabilities.committee.cio.cio import build_cio_prompt
    prompt=build_cio_prompt({'symbol':'TEST','display_name':'Synthetic'},json_mode=True)
    assert 'market_citations' in prompt and 'source_quote' in prompt and '[citation_id]' in prompt
    assert 'Market statistics may be cited' not in prompt
