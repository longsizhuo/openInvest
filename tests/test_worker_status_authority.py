"""Transport outcomes are caller facts; quoting an error marker is report text."""
from copy import deepcopy
import json
from types import SimpleNamespace
from pathlib import Path
import pytest

from openinvest.core.committee.agent_io import _ask,worker_output_status,legacy_worker_brief_unavailable
from openinvest.core.committee.cio_parse import parse_cio_memo
from openinvest.core.committee.persist import build_input_health


NEGATED="三方报告完整，没有出现完整的[WORKER_UNAVAILABLE] reason=失败标记。"


def test_actual_successful_reply_can_quote_the_complete_failure_marker():
    reply=_ask(SimpleNamespace(run=lambda context:NEGATED),'context')
    assert worker_output_status(reply)=={'transport':'succeeded','unavailable':False,'reason':None}
    assert worker_output_status(deepcopy(reply))==worker_output_status(reply)
    assert json.loads(json.dumps({'text':reply}))=={'text':NEGATED}
    assert not legacy_worker_brief_unavailable(reply)
    result=parse_cio_memo(reply,fields={'verdict':'ACCUMULATE','confidence':.62,'suggested_alloc_cny':1000})
    assert result['verdict']=='ACCUMULATE' and result['confidence']==.62 and result['alloc_cny']==1000
    assert '_original_confidence_unavailable' not in result


def test_real_failure_cannot_be_overridden_by_cio_claiming_everyone_succeeded():
    failure=_ask(None,'context')
    status=worker_output_status(failure)
    assert status['transport']=='failed' and status['unavailable']
    result=parse_cio_memo(NEGATED,fields={'verdict':'ACCUMULATE','confidence':.62,'suggested_alloc_cny':1000},unavailable_workers=['risk'])
    assert result['decision_status']=='DATA_UNTRUSTED' and result['alloc_cny']==0
    report=SimpleNamespace(macro_view='healthy',quant_view='healthy',risk_view='formatted later',cio_memo=NEGATED)
    health=build_input_health(report,result,{'worker_calls':[{'role':'risk','round':'opening',**status}]})
    assert health['worker_unavailable']==['risk']


def test_quoted_error_example_is_not_an_executed_failed_call():
    text='Example only:\n```text\n[WORKER_UNAVAILABLE] reason=retry_exhausted\n```\nVERDICT: ACCUMULATE\nCONFIDENCE: 0.62\nSUGGESTED_ALLOC_CNY: 1000'
    result=parse_cio_memo(text)
    assert result['verdict']=='ACCUMULATE' and '_original_confidence_unavailable' not in result
    report=SimpleNamespace(macro_view='healthy',quant_view='healthy',risk_view='healthy',cio_memo=text)
    assert build_input_health(report,result)['worker_unavailable']==[]


def test_direct_pipeline_preserves_caller_status_after_json_memo_extraction(monkeypatch):
    from openinvest.core.committee import debate
    class Agent:
        def __init__(self,role):self.role=role
        def run(self,context):
            if self.role=='cio':return json.dumps({'verdict':'HOLD','confidence':.55,'dominant_view':'risk','alloc_cny':0,'memo':NEGATED})
            return 'SIGNAL: neutral\nSTRENGTH: 4\n'+NEGATED
    monkeypatch.setattr(debate,'_create_agent',lambda prompt,role='unknown',**kwargs:Agent(role))
    monkeypatch.setattr('openinvest.services.reasoning_review.review_once',lambda *args,**kwargs:{'status':'passed','origin':'synthetic_unit_fixture'})
    monkeypatch.setenv('INVEST_FORCE_JSON_OUTPUT','1')
    result=debate.run_committee(asset={'symbol':'SYNTHETIC'},market_data='synthetic market',macro_view=NEGATED,
                                portfolio_summary='synthetic account',persist_to_memory=False,max_debate_rounds=2)
    assert result['verdict']['confidence']==.55 and result['verdict'].get('decision_status')!='DATA_UNTRUSTED'
    health=build_input_health(result['report'],result['verdict'],result['debate'])
    assert health['worker_unavailable']==[]
    assert all(not row['unavailable'] for row in health['worker_calls'])
    assert {row['transport'] for row in health['worker_calls'] if row['role']!='macro'}=={'succeeded'}


@pytest.mark.parametrize('failed_role',[False,True])
def test_coordinator_complete_sections_distinguish_error_body_from_negated_cio_quote(tmp_path,monkeypatch,failed_role):
    from datetime import datetime,timezone
    from tests.test_reasoning_review_flow import transcript
    from openinvest.core.memory_store import MemoryStore
    from openinvest.core.decision_evidence import prepare_context
    from openinvest.core.runner.coordinator import save_committee_transcript
    from openinvest.core.committee.reasoning_quality import CHECKS
    from openinvest.core.decision_ledger import parse_committee_text
    import pandas as pd
    monkeypatch.setattr('openinvest.utils.exchange_fee.get_history_data',lambda *args,**kwargs:pd.DataFrame())
    monkeypatch.setattr('openinvest.core.memory_store.MEMORY_ROOT',tmp_path/'memory')
    monkeypatch.setattr('openinvest.paths.INVEST_ROOT',tmp_path)
    monkeypatch.setenv('INVEST_REASONING_BY','agent')
    store=MemoryStore()
    facts={'market_data':'synthetic reference 100 CNY/unit','current_price':100,'price_last_bar':datetime.now(timezone.utc).isoformat(),
           'macro_data':'synthetic macro','portfolio_summary':'synthetic account: 1 unit and 1000 CNY',
           'required_roles':['MACRO','QUANT_R1','RISK_R1','QUANT_R2','RISK_R2','CIO']}
    context=prepare_context(store.root,'SYNTHETIC',facts,model='synthetic-model')
    raw=transcript().replace('=== CIO ===\n','=== CIO ===\n'+NEGATED+'\n')
    if failed_role:raw=raw.replace('=== RISK_R1 ===\nSIGNAL: concerned\nSTRENGTH: 4\nMARKET_RISK: elevated\nLIQUIDITY: available','=== RISK_R1 ===\nRISK: [WORKER_UNAVAILABLE] reason=retry_exhausted')
    args={'provider':'unit','model':'synthetic-model','context_id':context['context_id']}
    first=save_committee_transcript('SYNTHETIC',raw,**args)
    assert first['status']=='verification_required'
    checks=json.dumps({'request_sha256':first['request_sha256'],'checks':{key:'passed' for key in CHECKS},'issues':[]})
    result=save_committee_transcript('SYNTHETIC',raw,reasoning_review_json=checks,**args)
    health=parse_committee_text(Path(result['saved']).read_text())['input_health']
    if failed_role:
        assert result['verdict']['decision_status']=='DATA_UNTRUSTED'
        assert health['worker_unavailable']==['risk']
    else:
        assert result['verdict']['confidence']==.6
        assert health['worker_unavailable']==[]
