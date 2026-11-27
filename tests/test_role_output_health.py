from openinvest.core.committee.role_health import risk_output_health
from openinvest.core.committee.debate import CommitteeReport
from openinvest.core.committee.persist import build_input_health
from openinvest.core.decision_evidence import enforce_input_health,advice_qualified

FULL='SIGNAL: unknown\nMARKET_RISK: ordinary\nLIQUIDITY: insufficient\nRISK_EVIDENCE: synthetic supplied facts'

def test_missing_liquidity_is_model_incompleteness_not_bad_market_source_or_hold():
    text=FULL.replace('LIQUIDITY: insufficient\n','')
    report=CommitteeReport(asset={'symbol':'TEST'},risk_view=text)
    verdict={'verdict':'ACCUMULATE','alloc_cny':1000,'reasoning_review':{'status':'passed'}}
    health=build_input_health(report,verdict)
    enforce_input_health(verdict,health)
    assert health['worker_unavailable']==[] and health['role_health'][0]['missing_fields']==['LIQUIDITY']
    assert verdict['verdict']=='ACCUMULATE' and verdict['raw_market_verdict']=='ACCUMULATE'
    assert verdict['decision_status']=='REVIEW_REQUIRED' and verdict['alloc_cny']==0 and not advice_qualified(verdict)
    assert health['role_health'][0]['source_validity_changed'] is False

def test_complete_unknown_capacity_does_not_select_action_or_create_an_error():
    row=risk_output_health(FULL,'RISK_R1')
    assert row['structure']=='complete' and not row['issue_refs']
    assert row['fields']=={'SIGNAL':'unknown','MARKET_RISK':'ordinary','LIQUIDITY':'insufficient'}
    assert row['reasoning']=='needs_reasoning_review' and not row['market_action_selected']

def test_generic_cio_review_does_not_clear_a_role_output_error_or_change_source_validity():
    report=CommitteeReport(asset={'symbol':'TEST'},risk_view=FULL,risk_adjusted='SIGNAL: ok\nMARKET_RISK: ordinary')
    verdict={'verdict':'BUY','alloc_cny':1000,'reasoning_review':{'status':'passed'}}
    health=build_input_health(report,verdict);enforce_input_health(verdict,health)
    assert len(health['role_health'])==2 and health['model_output_errors'][0]['round']=='RISK_R2'
    assert verdict['verdict']=='BUY' and verdict['decision_status']=='REVIEW_REQUIRED' and verdict['alloc_cny']==0
    revised=risk_output_health(FULL,'RISK_R2')
    assert revised['output_sha256']!=health['role_health'][1]['output_sha256']


def test_rebuttal_schema_accepts_adjusted_signal_without_inventing_a_round_one_field():
    r2=FULL.replace('SIGNAL:','ADJUSTED_SIGNAL:')
    row=risk_output_health(r2,'RISK_R2')
    assert row['structure']=='complete' and row['fields']['SIGNAL']=='unknown'
    assert risk_output_health(r2,'RISK_R1')['missing_fields']==['SIGNAL']


def test_absent_coordinator_role_is_an_output_gap_not_a_fake_transport_failure():
    row=risk_output_health('','RISK_R1')
    assert row['structure']=='incomplete' and len(row['missing_fields'])==3


def test_direct_advisory_call_keeps_buy_but_blocks_incomplete_risk_output(monkeypatch):
    import json
    from openinvest.core.committee import debate
    class Agent:
        last_tool_calls=[]
        def __init__(self,role):self.role=role
        def run(self,context):
            if self.role=='cio':return json.dumps({'verdict':'BUY','confidence':.6,'suggested_alloc_cny':1000,'memo':'Synthetic proposal'})
            return FULL.replace('LIQUIDITY: insufficient\n','') if self.role=='risk' else 'SIGNAL: neutral\nSTRENGTH: 4'
        def tool_call_summary(self):return ''
    monkeypatch.setattr(debate,'_create_agent',lambda prompt,role='unknown',**kwargs:Agent(role))
    monkeypatch.setattr('openinvest.services.reasoning_review.review_once',lambda *a,**k:{'status':'passed','origin':'synthetic_fixture'})
    monkeypatch.setenv('INVEST_FORCE_JSON_OUTPUT','1')
    result=debate.run_committee(asset={'symbol':'TEST'},market_data='provided synthetic market',macro_view='provided synthetic macro',
        portfolio_summary='synthetic account',persist_to_memory=False,max_debate_rounds=1)
    verdict=result['verdict']
    assert verdict['verdict']=='BUY' and verdict['decision_status']=='REVIEW_REQUIRED' and verdict['alloc_cny']==0
    assert not advice_qualified(verdict) and verdict['raw_market_verdict']=='BUY'


def test_coordinator_persisted_incomplete_role_cannot_enter_current_cache_or_activate_plan(tmp_path,monkeypatch):
    import json
    from pathlib import Path
    from tests.integrity_helpers import context_id
    from openinvest.core.runner.coordinator import save_committee_transcript,load_submitted_committee
    from openinvest.core.decision_ledger import parse_committee_text
    from openinvest.core.memory_store import MemoryStore
    monkeypatch.setattr('openinvest.core.memory_store.MEMORY_ROOT',tmp_path/'memory')
    plan={'conditions':[{'metric':'quote_price','op':'<=','value':95,'unit':'USD/unit'}],
        'logic':'all','expires_at':'2099-01-01T00:00:00Z','replaces_plan_id':None,'change_reason':'synthetic fixture'}
    raw='=== RISK_R1 ===\n'+FULL.replace('LIQUIDITY: insufficient\n','')+'\n=== CIO ===\nVERDICT: BUY\nCONFIDENCE: 0.6\nSUGGESTED_ALLOC_CNY: 1000\nREVIEW_PLAN_JSON: '+json.dumps(plan)
    result=save_committee_transcript('TEST',raw,context_id=context_id('TEST'))
    assert result['verdict']['verdict']=='BUY' and result['verdict']['alloc_cny']==0
    health=parse_committee_text(Path(result['saved']).read_text())['input_health']
    assert health['model_output_errors'][0]['reason']=='model_output_incomplete' and not health['worker_unavailable']
    assert load_submitted_committee('TEST',result['decision_date']) is None
    assert not (MemoryStore().root/'.state/review_plan_TEST.json').exists()


def test_markdown_field_emphasis_is_presentation_not_an_output_defect():
    for round_label,prefix in [('RISK_R1','SIGNAL'),('RISK_R2','ADJUSTED_SIGNAL')]:
        plain=f'{prefix}: unknown\nMARKET_RISK: ordinary\nLIQUIDITY: insufficient'
        for text in [f'**{prefix}:** unknown\n**MARKET_RISK:** ordinary\n**LIQUIDITY:** insufficient',
                     f'**{prefix}**: **unknown**\n**MARKET_RISK**: **ordinary**\n**LIQUIDITY**: **insufficient**']:
            row=risk_output_health(text,round_label)
            assert row['structure']=='complete' and row['fields']==risk_output_health(plain,round_label)['fields']
            assert row['output_sha256']!=risk_output_health(plain,round_label)['output_sha256']
