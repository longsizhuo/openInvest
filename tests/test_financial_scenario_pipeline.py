"""Both committee entries qualify typed arithmetic; generic pass cannot bypass it."""
import copy,json
from datetime import datetime,timedelta,timezone
from pathlib import Path
import pytest
from tests.test_position_scenarios import position,scenario
from tests.integrity_helpers import context_id
from openinvest.core.evidence_eligibility import digest
from openinvest.core.decision_evidence import advice_qualified

SYM='510300.SS'
RISK='SIGNAL: unknown\nMARKET_RISK: ordinary\nLIQUIDITY: available'


def inputs(variant):
    p=position();p['symbol']=SYM;p['position_ref']=digest({k:v for k,v in p.items() if k!='position_ref'})
    s=scenario(p);memo='趋势证据支持有限新增，仍需持续核对反证。'
    if variant=='bad_amount':s['claimed_metrics']=[{'metric':'mark_to_market_change','value':0,'basis':'scenario_mark_minus_current_mark','currency':'CNY'}]
    if variant=='bad_unit':s['unit']='oz'
    if variant=='bad_free_prose':memo+='压力测试：账面回撤约为0 CNY的已实现损失。'
    plan={'conditions':[{'metric':'quote_price','op':'<=','value':95,'unit':'CNY/unit'}],'logic':'all',
          'expires_at':(datetime.now(timezone.utc)+timedelta(days=1)).isoformat(),'replaces_plan_id':None,'change_reason':'synthetic fixture'}
    fields={'verdict':'BUY','confidence':.6,'suggested_alloc_cny':1000,'scenario_analysis':[s],'memo':memo,'review_plan':plan}
    return p,fields

@pytest.fixture
def environment(tmp_path,monkeypatch):
    monkeypatch.setattr('openinvest.core.memory_store.MEMORY_ROOT',tmp_path/'memory')
    monkeypatch.setattr('openinvest.core.committee.persist._capture_macro_context',lambda **kw:{})
    monkeypatch.setattr('openinvest.services.reasoning_review.review_once',lambda *a,**kw:{'status':'passed','origin':'synthetic_contract_fixture'})
    monkeypatch.setenv('INVEST_FORCE_JSON_OUTPUT','1')
    return tmp_path

@pytest.mark.parametrize('entry',['direct_advisory','coordinator'])
@pytest.mark.parametrize('variant',['correct','bad_amount','bad_unit','bad_free_prose'])
def test_shared_gate_controls_qualification_cache_and_plans(environment,monkeypatch,entry,variant):
    p,fields=inputs(variant)
    if entry=='direct_advisory':
        from openinvest.core.committee import debate
        class Agent:
            last_tool_calls=[]
            def __init__(self,role):self.role=role
            def run(self,prompt):return json.dumps(fields,ensure_ascii=False) if self.role=='cio' else RISK if self.role=='risk' else 'SIGNAL: neutral\nSTRENGTH: 4'
            def tool_call_summary(self):return ''
        monkeypatch.setattr(debate,'_create_agent',lambda prompt,role='unknown',**kw:Agent(role))
        result=debate.run_committee(asset={'symbol':SYM},market_data='synthetic market',macro_view='synthetic macro',
            portfolio_summary='synthetic account',position_facts=p,current_price=99.7565,persist_to_memory=False,max_debate_rounds=1)
        verdict=result['verdict'];memo=result['report'].cio_memo
        assert not (environment/'memory/.committee').exists()
    else:
        from openinvest.core.runner.coordinator import save_committee_transcript,load_submitted_committee
        raw='=== MACRO ===\nSynthetic\n=== QUANT_R1 ===\nSIGNAL: neutral\n=== RISK_R1 ===\n'+RISK+'\n=== CIO ===\n'
        raw+='VERDICT: BUY\nCONFIDENCE: 0.6\nSUGGESTED_ALLOC_CNY: 1000\nSCENARIO_ANALYSIS_JSON: '+json.dumps(fields['scenario_analysis'])
        raw+='\nREVIEW_PLAN_JSON: '+json.dumps(fields['review_plan'])+'\n'+fields['memo']
        result=save_committee_transcript(SYM,raw,context_id=context_id(SYM,position_facts=p,current_price=99.7565))
        verdict=result['verdict'];memo=Path(result['saved']).read_text()
        if variant!='correct':
            assert load_submitted_committee(SYM,result['decision_date']) is None
            assert not (environment/'memory/.state/review_plan_510300_SS.json').exists()
    assert verdict['verdict']=='BUY'
    if variant=='correct':
        assert advice_qualified(verdict) and verdict['alloc_cny']==1000
        assert '-95.13 CNY' in memo and '+0.00 CNY' in memo
    else:
        assert not advice_qualified(verdict) and verdict['decision_status']=='REVIEW_REQUIRED' and verdict['alloc_cny']==0
        assert verdict['scenario_validation']['errors'] and not verdict['scenario_validation']['source_validity_changed']
        if variant=='bad_free_prose':
            from openinvest.core.decision_ledger import parse_committee_text
            display=memo if entry=='direct_advisory' else parse_committee_text(memo)['cio_memo']
            assert '回撤约为0 CNY的已实现损失' not in display
            if entry=='direct_advisory':assert fields['memo'] in result['report'].cio_raw_response
            else:
                originals=list((environment/'memory/.evidence/submissions').glob('*.json'))
                assert originals and fields['memo'] in originals[0].read_text()


@pytest.mark.parametrize('entry',['direct_advisory','coordinator'])
@pytest.mark.parametrize('variant',['correct','wrong_price','wrong_unit','wrong_source','account_outside_quote'])
def test_market_citation_source_binding_is_shared_and_cannot_wash_account_figures(environment,monkeypatch,entry,variant):
    from openinvest.core.committee.reasoning_quality import make_request,proposal_sources
    source='高盛称布伦特原油报价117美元/桶。'
    p,fields=inputs('correct')
    fields['market_citations']=[{'citation_id':'energy_report','source_id':'macro','source_quote':source}]
    fields['memo']='市场证据支持有限新增，同时保留宏观反证，参考[energy_report]。'
    if variant=='wrong_price':fields['market_citations'][0]['source_quote']=source.replace('117','118')
    if variant=='wrong_unit':fields['market_citations'][0]['source_quote']=source.replace('/桶','/克')
    if variant=='wrong_source':fields['market_citations'][0]['source_id']='analyst_MACRO'
    if variant=='account_outside_quote':fields['memo']+='账户账面损失为0 CNY。'
    fields['market_assessment']={'direction':'bullish','support':['市场信号有支持'],'counterevidence':['见[energy_report]']}
    if entry=='direct_advisory':
        from openinvest.core.committee import debate
        seen=[]
        class Agent:
            last_tool_calls=[]
            def __init__(self,role):self.role=role
            def run(self,prompt):
                if self.role=='cio':seen.append(prompt);return json.dumps(fields,ensure_ascii=False)
                return RISK if self.role=='risk' else 'SIGNAL: neutral\nSTRENGTH: 4'
            def tool_call_summary(self):return ''
        monkeypatch.setattr(debate,'_create_agent',lambda prompt,role='unknown',**kw:Agent(role))
        result=debate.run_committee(asset={'symbol':SYM},market_data='synthetic market',macro_view='synthetic opinion',
            source_macro_data=source,portfolio_summary='synthetic account',position_facts=p,current_price=99.7565,
            persist_to_memory=False,max_debate_rounds=1)
        assert 'MARKET CITATION CATALOG' in seen[0] and 'macro_input' in seen[0]
        verdict=result['verdict'];display=result['report'].cio_memo;context_ref=None
    else:
        from openinvest.core.runner.coordinator import save_committee_transcript
        raw='=== MACRO ===\nSynthetic opinion\n=== QUANT_R1 ===\nSIGNAL: neutral\n=== RISK_R1 ===\n'+RISK+'\n=== CIO ===\n'
        raw+='VERDICT: BUY\nCONFIDENCE: 0.6\nSUGGESTED_ALLOC_CNY: 1000\nSCENARIO_ANALYSIS_JSON: '+json.dumps(fields['scenario_analysis'])
        raw+='\nMARKET_CITATIONS_JSON: '+json.dumps(fields['market_citations'],ensure_ascii=False)+'\n'+fields['memo']
        context_ref=context_id(SYM,position_facts=p,current_price=99.7565,macro_data=source)
        result=save_committee_transcript(SYM,raw,context_id=context_ref);verdict=result['verdict']
        from openinvest.core.decision_ledger import parse_committee_text
        display=parse_committee_text(Path(result['saved']).read_text())['cio_memo']
        original=json.loads((environment/'memory/.evidence/submissions'/(context_ref+'.json')).read_text())['payload']['transcript']
        assert original==raw
    validation=verdict['scenario_validation'];citation=validation['market_citation_validation']
    request=make_request(sources=proposal_sources({'position_facts':p,'context_id':context_ref,'macro_data':source},{}),
                         proposal={'fields':fields,'memo':fields['memo']},model='synthetic-model')
    recomputed=request['sources']['scenario_math']['value']
    assert recomputed['market_citation_validation']==citation
    if variant=='correct':
        assert advice_qualified(verdict) and not validation['errors']
        assert '来源报道' in display and '117美元/桶' in display and '不是持仓原生报价' in display
        assert citation['native_position_quote_qualification_granted'] is False
    else:
        assert not advice_qualified(verdict) and verdict['alloc_cny']==0 and validation['errors']
        assert '账户账面损失为0 CNY' not in display


def test_json_wire_without_memo_does_not_publish_unknown_financial_fields(environment,monkeypatch):
    from openinvest.core.committee import debate
    p,fields=inputs('correct');fields.pop('memo');fields['stress_scenario_loss_cny']=0
    class Agent:
        last_tool_calls=[]
        def __init__(self,role):self.role=role
        def run(self,context):return json.dumps(fields) if self.role=='cio' else RISK if self.role=='risk' else 'SIGNAL: neutral\nSTRENGTH: 4'
        def tool_call_summary(self):return ''
    monkeypatch.setattr(debate,'_create_agent',lambda prompt,role='unknown',**kw:Agent(role))
    result=debate.run_committee(asset={'symbol':SYM},market_data='synthetic',macro_view='synthetic',portfolio_summary='synthetic',
        position_facts=p,current_price=99.7565,persist_to_memory=False,max_debate_rounds=1)
    assert result['verdict']['verdict']=='BUY' and not advice_qualified(result['verdict'])
    assert result['verdict']['alloc_cny']==0
    assert 'stress_scenario_loss_cny' not in result['report'].cio_memo
    assert 'stress_scenario_loss_cny' in result['report'].cio_raw_response


def test_duplicate_single_value_scenario_fields_cannot_select_first_and_publish_second(environment):
    from openinvest.core.runner.coordinator import save_committee_transcript
    p,fields=inputs('correct');bad=copy.deepcopy(fields['scenario_analysis'])
    bad[0]['scenario_id']='bad_second';bad[0]['claimed_metrics']=[{'metric':'mark_to_market_change','value':0,'basis':'scenario_mark_minus_current_mark','currency':'CNY'}]
    raw='=== RISK_R1 ===\n'+RISK+'\n=== CIO ===\nVERDICT: BUY\nCONFIDENCE: 0.6\nSUGGESTED_ALLOC_CNY: 1000\n'
    raw+='SCENARIO_ANALYSIS_JSON: '+json.dumps(fields['scenario_analysis'])+'\nSCENARIO_ANALYSIS_JSON: '+json.dumps(bad)
    result=save_committee_transcript(SYM,raw,context_id=context_id(SYM,position_facts=p))
    assert not advice_qualified(result['verdict']) and result['verdict']['alloc_cny']==0
    assert any(e['code']=='duplicate_protocol_fields' for e in result['verdict']['scenario_validation']['errors'])


def test_duplicate_json_keys_in_any_structured_protocol_field_are_not_silently_discarded(environment):
    from openinvest.core.runner.coordinator import save_committee_transcript
    p,fields=inputs('correct')
    raw='=== RISK_R1 ===\n'+RISK+'\n=== CIO ===\nVERDICT: BUY\nCONFIDENCE: 0.6\nSUGGESTED_ALLOC_CNY: 1000\n'
    raw+='SCENARIO_ANALYSIS_JSON: '+json.dumps(fields['scenario_analysis'])+'\nMARKET_ASSESSMENT_JSON: {"support":"当前账户损失0 CNY","support":"趋势仍待观察"}'
    result=save_committee_transcript(SYM,raw,context_id=context_id(SYM,position_facts=p))
    assert not advice_qualified(result['verdict']) and result['verdict']['alloc_cny']==0
    assert any(e['code']=='invalid_protocol_json' for e in result['verdict']['scenario_validation']['errors'])


def test_protocol_case_does_not_create_a_second_unvalidated_single_value(environment):
    from openinvest.core.runner.coordinator import save_committee_transcript
    p,fields=inputs('correct');bad=copy.deepcopy(fields['scenario_analysis'])
    bad[0]['scenario_id']='bad_lowercase';bad[0]['claimed_metrics']=[{'metric':'mark_to_market_change','value':0,'basis':'scenario_mark_minus_current_mark','currency':'CNY'}]
    raw='=== RISK_R1 ===\n'+RISK+'\n=== CIO ===\nVERDICT: BUY\nCONFIDENCE: 0.6\nSUGGESTED_ALLOC_CNY: 1000\n'
    raw+='SCENARIO_ANALYSIS_JSON: '+json.dumps(fields['scenario_analysis'])+'\nscenario_analysis_json: '+json.dumps(bad)
    result=save_committee_transcript(SYM,raw,context_id=context_id(SYM,position_facts=p))
    assert not advice_qualified(result['verdict']) and result['verdict']['alloc_cny']==0
    assert any(e['code']=='duplicate_protocol_fields' for e in result['verdict']['scenario_validation']['errors'])
