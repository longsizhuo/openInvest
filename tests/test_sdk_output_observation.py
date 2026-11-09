from types import SimpleNamespace as NS
from unittest.mock import Mock
import pytest
from openinvest.capabilities.sdk_agent import SDKAgent
from openinvest.core.llm_telemetry import TelemetryMeta
from openinvest.core.committee.agent_io import _ask, worker_output_status

@pytest.mark.parametrize('finish,content,state',[('stop','','empty'),('length','partial','truncated'),('stop','valid','text'),('content_filter','','refused')])
def test_billed_empty_or_truncated_is_visible_without_retry(monkeypatch,finish,content,state):
    from openinvest.capabilities import sdk_agent
    agent=object.__new__(SDKAgent)
    agent.system_prompt='test';agent.model='test-model';agent.temperature=.2
    agent.enable_thinking=False;agent.enable_tools=False;agent.response_format=None;agent.max_tool_iterations=0
    agent.telemetry_meta=TelemetryMeta()
    response=NS(id='response-id',model='test-model',choices=[NS(finish_reason=finish,message=NS(content=content,tool_calls=None))],
        usage=NS(prompt_tokens=100,completion_tokens=8192,completion_tokens_details=NS(reasoning_tokens=8192)))
    create=Mock(return_value=response);agent.client=NS(chat=NS(completions=NS(create=create)))
    records=[];monkeypatch.setattr(sdk_agent,'record_llm_call',lambda *a,**k:records.append(k))
    result=_ask(agent,'test')
    assert create.call_count==1 and len(records)==1
    assert records[0]['output_tokens']==8192 and records[0]['response_status']['output_state']==state
    assert records[0]['ok'] is (state=='text')
    assert worker_output_status(result)['unavailable'] is (state!='text')


@pytest.mark.parametrize('finish',['length','content_filter'])
def test_tool_budget_final_response_uses_the_same_output_guard(monkeypatch,finish):
    from openinvest.capabilities import sdk_agent
    agent=object.__new__(SDKAgent)
    agent.system_prompt='test';agent.model='test-model';agent.temperature=.2
    agent.enable_thinking=False;agent.enable_tools=True;agent.response_format=None;agent.max_tool_iterations=0
    agent.telemetry_meta=TelemetryMeta()
    tool=NS(id='tool-1',function=NS(name='synthetic',arguments='{}'))
    def response(reason,text,tools):
        return NS(id='response-id',model='test-model',choices=[NS(finish_reason=reason,message=NS(content=text,tool_calls=tools))],usage=None)
    create=Mock(side_effect=[response('tool_calls','',[tool]),response(finish,'incomplete output',[])])
    agent.client=NS(chat=NS(completions=NS(create=create)))
    monkeypatch.setattr(sdk_agent,'record_llm_call',lambda *a,**k:None)
    monkeypatch.setattr(sdk_agent,'record_tool_call',lambda *a,**k:None)
    monkeypatch.setattr(sdk_agent,'execute_tool_call',lambda *a,**k:'{}')
    result=_ask(agent,'test')
    assert create.call_count==2 and worker_output_status(result)['unavailable']


def test_disabled_tools_are_a_contract_not_just_an_omitted_request_parameter(monkeypatch):
    from openinvest.capabilities import sdk_agent
    agent=object.__new__(SDKAgent)
    agent.system_prompt='test';agent.model='test-model';agent.temperature=.2
    agent.enable_thinking=False;agent.enable_tools=False;agent.response_format=None;agent.max_tool_iterations=0
    agent.telemetry_meta=TelemetryMeta()
    tool=NS(id='tool-1',function=NS(name='synthetic',arguments='{}'))
    response=NS(id='r',model='test-model',usage=None,choices=[NS(finish_reason='tool_calls',message=NS(content='',tool_calls=[tool]))])
    agent.client=NS(chat=NS(completions=NS(create=Mock(return_value=response))))
    monkeypatch.setattr(sdk_agent,'record_llm_call',lambda *a,**k:None)
    execute=Mock();monkeypatch.setattr(sdk_agent,'execute_tool_call',execute)
    result=_ask(agent,'synthetic')
    assert worker_output_status(result)['unavailable'] and agent.client.chat.completions.create.call_count==1
    execute.assert_not_called()
