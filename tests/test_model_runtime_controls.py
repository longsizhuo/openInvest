from openinvest.utils.llm import get_thinking_disable_kwargs,needs_thinking_disabled


def test_official_flash_alias_honors_thinking_disabled():
    for name in ("deepseek-flash","deepseek-v4-flash","deepseek/deepseek-flash"):
        assert needs_thinking_disabled(name)
        assert get_thinking_disable_kwargs(name)=={"extra_body":{"thinking":{"type":"disabled"}}}


def test_other_providers_do_not_receive_deepseek_specific_flags():
    assert get_thinking_disable_kwargs("claude-sonnet-5-5")=={}
    assert get_thinking_disable_kwargs("gpt-6")=={}
