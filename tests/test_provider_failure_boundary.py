"""Permanent provider failures stop synthesis and never become a market vote."""
from types import SimpleNamespace
import pytest

from openinvest.core.committee import agent_io,debate


class APIStatusError(Exception):
    def __init__(self,status):super().__init__('synthetic provider error');self.status_code=status


@pytest.mark.parametrize('status,retry',[(400,False),(401,False),(402,False),(403,False),(404,False),(408,True),(409,True),(422,False),(429,True),(500,True),(503,True)])
def test_http_meaning_takes_precedence_over_generic_api_error_class(status,retry):
    assert agent_io._is_transient(APIStatusError(status)) is retry


def test_failed_macro_does_not_start_more_roles_or_create_a_proposal(monkeypatch):
    calls=[]
    def failed(context):calls.append('macro');raise APIStatusError(402)
    reply=agent_io._ask(SimpleNamespace(run=failed),'synthetic context')
    assert calls==['macro']
    monkeypatch.setattr(debate,'_create_agent',lambda *args,**kwargs:pytest.fail('no further model construction expected'))
    result=debate.run_committee(asset={'symbol':'SYNTHETIC'},market_data='market',macro_view=reply,portfolio_summary='account',persist_to_memory=False)
    assert result['verdict']['verdict']=='UNCLEAR' and result['verdict']['decision_status']=='DATA_UNTRUSTED'
    assert result['verdict']['raw_market_verdict'] is None and not result['verdict']['proposal_available']
    assert result['report'].cio_attempts==[] and result['report'].macro_view==reply


@pytest.mark.parametrize('stage',['analysts','cio'])
def test_auth_or_balance_failure_stops_before_cio_fallback_or_verifier(monkeypatch,stage):
    calls=[]
    class Agent:
        def __init__(self,role):self.role=role
        def run(self,context):
            calls.append(self.role)
            if stage=='analysts' or self.role=='cio':raise APIStatusError(402)
            return 'SIGNAL: neutral\nSTRENGTH: 4'
    monkeypatch.setattr(debate,'_create_agent',lambda prompt,role='unknown',**kwargs:Agent(role))
    monkeypatch.setenv('INVEST_FORCE_JSON_OUTPUT','1')
    result=debate.run_committee(asset={'symbol':'SYNTHETIC'},market_data='market',macro_view='provided macro',portfolio_summary='account',persist_to_memory=False,max_debate_rounds=2)
    assert result['verdict']['hold_reason']=='provider_unavailable'
    assert not result['verdict']['proposal_available'] and result['verdict']['raw_market_verdict'] is None
    assert 'reasoning_verifier' not in calls
    if stage=='analysts':
        assert sorted(calls)==['quant','risk'] and result['report'].quant_view.startswith('[WORKER_UNAVAILABLE] reason=')
    else:
        assert calls.count('cio')==1 and len(result['report'].cio_attempts)==1
        assert result['report'].cio_raw_response.startswith('[WORKER_UNAVAILABLE] reason=')


@pytest.mark.parametrize('status',[400,422])
def test_json_format_rejection_still_allows_one_text_fallback(monkeypatch,status):
    calls=[]
    class Agent:
        def __init__(self,role,format):self.role=role;self.format=format
        def run(self,context):
            calls.append((self.role,bool(self.format)))
            if self.role!='cio':return 'SIGNAL: neutral\nSTRENGTH: 4'
            if self.format:raise APIStatusError(status)
            return 'VERDICT: HOLD\nCONFIDENCE: 0.55\nDOMINANT_VIEW: risk\nSUGGESTED_ALLOC_CNY: 0'
    monkeypatch.setattr(debate,'_create_agent',lambda prompt,role='unknown',response_format=None,**kwargs:Agent(role,response_format))
    monkeypatch.setattr('openinvest.services.reasoning_review.review_once',lambda *args,**kwargs:{'status':'passed','origin':'synthetic_unit_fixture'})
    monkeypatch.setenv('INVEST_FORCE_JSON_OUTPUT','1')
    result=debate.run_committee(asset={'symbol':'SYNTHETIC'},market_data='market',macro_view='macro',portfolio_summary='account',persist_to_memory=False)
    assert [format for role,format in calls if role=='cio']==[True,False]
    assert result['verdict']['confidence']==.55 and result['verdict'].get('decision_status')!='DATA_UNTRUSTED'
    assert result['debate']['worker_calls'][-2]['used'] is False


def test_worker_visible_error_does_not_copy_provider_exception_secrets(monkeypatch,caplog):
    secret='SYNTHETIC_PRIVATE_CREDENTIAL'
    class FailingAgent:
        def run(self,context):raise RuntimeError(secret)
    monkeypatch.setattr(agent_io,'LLM_MAX_ATTEMPTS',2)
    monkeypatch.setattr(agent_io.time,'sleep',lambda delay:None)
    reply=agent_io._ask(FailingAgent(),'synthetic context')
    assert secret not in reply and secret not in caplog.text
    assert agent_io.worker_output_status(reply)['unavailable']


def test_late_provider_abort_retains_all_rounds_in_returned_and_immutable_records(tmp_path,monkeypatch):
    from pathlib import Path
    import json,pandas as pd
    from openinvest.core.decision_ledger import parse_committee_text
    monkeypatch.setattr('openinvest.core.memory_store.MEMORY_ROOT',tmp_path/'memory')
    monkeypatch.setattr('openinvest.paths.INVEST_ROOT',tmp_path)
    monkeypatch.setattr('openinvest.utils.exchange_fee.get_history_data',lambda *args,**kwargs:pd.DataFrame())
    class Agent:
        def __init__(self,role,round_label):self.role=role;self.round=round_label
        def run(self,context):
            if self.round=='round_3' and self.role=='risk':raise APIStatusError(402)
            strength={'opening':4,'round_2':6,'round_3':8}[self.round]
            return f'SIGNAL: neutral\nADJUSTED_SIGNAL: neutral\nSTRENGTH: {strength}\nSYNTHETIC_{self.role}_{self.round}'
    monkeypatch.setattr(debate,'_create_agent',lambda prompt,role='unknown',round_label='unknown',**kwargs:Agent(role,round_label))
    result=debate.run_committee(asset={'symbol':'SYNTHETIC'},market_data='market',macro_view='macro',portfolio_summary='account',max_debate_rounds=4)
    assert result['debate']['final_round']==3
    assert len(result['debate']['quant_history'])==len(result['debate']['risk_history'])==3
    assert 'SYNTHETIC_quant_round_2' in result['debate']['quant_history'][1]
    record=json.loads((tmp_path/'memory/.evidence/decisions'/f"{result['verdict']['decision_id']}.json").read_text())['payload']
    assert any('SYNTHETIC_quant_round_2' in row['text'] for row in record['raw_proposal']['worker_outputs'])
    transcript=next((tmp_path/'memory/.evidence/transcripts').glob('*.md')).read_text()
    second=transcript.split('## Round 2 —')[1].split('## Extended Debate')[0]
    assert 'SYNTHETIC_quant_round_2' in second and 'SYNTHETIC_quant_round_3' not in second
    assert parse_committee_text(transcript)['input_health']['worker_unavailable']==['risk']


def test_persisted_provider_abort_keeps_risk_cause_pending_without_phantom_review(tmp_path,monkeypatch):
    from datetime import datetime,timezone
    import pandas as pd
    from openinvest.core.memory_store import MemoryStore
    from openinvest.core.decision_evidence import prepare_context
    from openinvest.core.runner.event_format import EventBrief
    from openinvest.services import agent_queue as queue
    monkeypatch.setattr('openinvest.core.memory_store.MEMORY_ROOT',tmp_path/'memory')
    monkeypatch.setattr('openinvest.paths.INVEST_ROOT',tmp_path)
    monkeypatch.setattr('openinvest.utils.exchange_fee.get_history_data',lambda *args,**kwargs:pd.DataFrame())
    store=MemoryStore()
    queue.enqueue_committee(['SYNTHETIC'],[],'synthetic cause',memory_root=store.root)
    bindings=queue.bind_context_requests(store.root,'SYNTHETIC',EventBrief('',{}),datetime.now(timezone.utc).isoformat())
    context=prepare_context(store.root,'SYNTHETIC',{'trigger_requests':bindings})
    def fail(context):raise APIStatusError(402)
    macro=agent_io._ask(SimpleNamespace(run=fail),'context')
    result=debate.run_committee(asset={'symbol':'SYNTHETIC'},market_data='',macro_view=macro,portfolio_summary='',
        input_health={'context_id':context['context_id'],'has_df':False,'macro_data_empty':True,'empty_inputs':['portfolio_summary']})
    assert result['verdict']['verdict']=='UNCLEAR' and result['verdict']['hold_reason']=='provider_unavailable'
    assert result['verdict']['proposal_available'] is False
    pending=queue.pending_committee_items(memory_root=store.root)
    assert len(pending)==1 and pending[0]['status']=='pending'
    assert not pending[0].get('review_requests')
    assert not list((store.root/'.evidence/reasoning/drafts').glob('*.json'))
