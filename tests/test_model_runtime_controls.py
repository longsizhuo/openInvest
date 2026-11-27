from openinvest.utils.llm import get_thinking_disable_kwargs,needs_thinking_disabled


def test_official_flash_alias_honors_thinking_disabled():
    for name in ("deepseek-flash","deepseek-v4-flash","deepseek/deepseek-flash"):
        assert needs_thinking_disabled(name)
        assert get_thinking_disable_kwargs(name)=={"extra_body":{"thinking":{"type":"disabled"}}}


def test_other_providers_do_not_receive_deepseek_specific_flags():
    assert get_thinking_disable_kwargs("claude-sonnet-5-5")=={}
    assert get_thinking_disable_kwargs("gpt-6")=={}


def test_final_decision_and_verifier_reason_independently_with_explicit_legacy_override(monkeypatch):
    from openinvest.utils.llm import committee_role_thinking,get_thinking_enable_kwargs
    monkeypatch.delenv("INVEST_CIO_THINKING",raising=False)
    assert committee_role_thinking("cio","deepseek-flash")
    assert committee_role_thinking("reasoning_verifier","deepseek-flash")
    assert not committee_role_thinking("quant","deepseek-flash")
    assert not committee_role_thinking("cio","claude-sonnet-5-5")
    assert get_thinking_enable_kwargs("deepseek-flash")=={"extra_body":{"thinking":{"type":"enabled"}}}
    assert get_thinking_enable_kwargs("gpt-6")=={}
    monkeypatch.setenv("INVEST_CIO_THINKING","0")
    assert not committee_role_thinking("cio","deepseek-flash")
    assert committee_role_thinking("reasoning_verifier","deepseek-flash")


def test_sdk_sends_explicit_thinking_and_json_together_without_tools():
    import json
    import httpx
    from openinvest.capabilities.sdk_agent import SDKAgent
    captured=[]
    def reply(request):
        body=json.loads(request.content);captured.append(body)
        return httpx.Response(200,json={"id":"synthetic-response","object":"chat.completion","created":1,
            "model":"deepseek-flash","choices":[{"index":0,"message":{"role":"assistant","content":"{}"},"finish_reason":"stop"}]})
    agent=SDKAgent(system_prompt="synthetic contract",model="deepseek-flash",api_key="synthetic-test-key",
        base_url="https://synthetic.invalid",enable_tools=False,max_tool_iterations=0,
        enable_thinking=True,response_format={"type":"json_object"})
    old=agent.client
    agent.client=old.with_options(http_client=httpx.Client(transport=httpx.MockTransport(reply)))
    try:assert agent.run("synthetic input")=="{}"
    finally:agent.client.close();old.close()
    assert len(captured)==1
    assert captured[0]["thinking"]=={"type":"enabled"}
    assert captured[0]["response_format"]=={"type":"json_object"}
    assert "tools" not in captured[0]


def test_verifier_limits_are_sent_and_sdk_has_no_hidden_retries(monkeypatch):
    import json,httpx
    from openinvest.capabilities.sdk_agent import SDKAgent
    from openinvest.core.config import set_config_override,reset_config
    from openinvest.utils.llm import verifier_generation_limits
    set_config_override({'verdict':{'reasoning_verifier_max_tokens':32768}})
    seen=[]
    def respond(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200,json={'id':'synthetic','object':'chat.completion','created':1,'model':'deepseek-flash',
           'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'{}'}}]})
    try:
        limits=verifier_generation_limits('deepseek-flash')
        agent=SDKAgent(system_prompt='json',model='deepseek-flash',api_key='synthetic',base_url='https://synthetic.invalid',
            enable_tools=False,enable_thinking=True,response_format={'type':'json_object'},**limits)
        assert agent.client.max_retries==0
        original=agent.client;agent.client=original.with_options(http_client=httpx.Client(transport=httpx.MockTransport(respond)))
        try:assert agent.run('synthetic')=='{}'
        finally:agent.client.close();original.close()
        assert len(seen)==1 and seen[0]['max_tokens']==32768 and seen[0]['reasoning_effort']=='high'
        assert seen[0]['thinking']=={'type':'enabled'}
        assert verifier_generation_limits('gpt-6')=={'max_output_tokens':None,'reasoning_effort':None}
    finally:reset_config()


def test_sdk_rate_limit_response_is_one_wire_not_nested_sdk_retries():
    import httpx,openai
    from openinvest.capabilities.sdk_agent import SDKAgent
    seen=[]
    def respond(request):
        seen.append(1)
        return httpx.Response(429,json={'error':{'message':'synthetic limit','type':'rate_limit_error'}})
    agent=SDKAgent(system_prompt='synthetic',model='deepseek-flash',api_key='synthetic',base_url='https://synthetic.invalid',enable_tools=False)
    original=agent.client;agent.client=original.with_options(http_client=httpx.Client(transport=httpx.MockTransport(respond)))
    try:
        import pytest
        with pytest.raises(openai.RateLimitError):agent.run('synthetic')
    finally:agent.client.close();original.close()
    assert seen==[1]
