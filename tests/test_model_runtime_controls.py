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
