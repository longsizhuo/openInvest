"""Claude 4.7+（含 Sonnet 5.5）temperature<1 直接 400，Anthropic 兼容层收
response_format=json_object 也 400 → 委员会静默全 HOLD。

守：claude-* 模型的请求里不带 temperature / response_format；DeepSeek 照旧带。
跑：uv run pytest tests/test_claude_sampling_params.py -q
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import openinvest.capabilities.sdk_agent as sdk_agent_mod
from openinvest.capabilities.sdk_agent import SDKAgent


@pytest.mark.parametrize("model,expect_temp", [
    ("claude-sonnet-5-5", False),
    ("deepseek-v4-flash", True),
])
def test_temperature_and_json_mode_only_sent_to_non_claude(monkeypatch, model, expect_temp):
    monkeypatch.setattr(sdk_agent_mod, "record_llm_call", lambda *a, **k: None)
    agent = SDKAgent(system_prompt="t", model=model, api_key="sk-fake",
                     base_url="https://example.invalid/v1", enable_tools=False,
                     response_format={"type": "json_object"})
    sent = {}

    def fake_create(**kwargs):
        sent.update(kwargs)
        msg = SimpleNamespace(content="ok", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=None)

    monkeypatch.setattr(agent.client.chat.completions, "create", fake_create)
    assert agent.run("hi") == "ok"
    assert ("temperature" in sent) is expect_temp
    assert ("response_format" in sent) is expect_temp


@pytest.mark.parametrize("model,claude", [
    ("claude-sonnet-5-5", True),
    ("Claude-Opus-5-5", True),
    ("anthropic/claude-sonnet-5.5", True),   # OpenRouter / litellm 前缀
    ("anthropic.claude-haiku-5-5", True),    # Bedrock 前缀
    ("deepseek-v4-flash", False),
])
def test_helpers_by_model_name(model, claude):
    from openinvest.utils.llm import is_claude, json_object_kwargs, sampling_kwargs
    assert is_claude(model) is claude
    assert ("temperature" in sampling_kwargs(model, 0.2)) is not claude
    assert ("response_format" in json_object_kwargs(model)) is not claude


def test_model_none_falls_back_to_env(monkeypatch):
    from openinvest.utils.llm import sampling_kwargs
    monkeypatch.setenv("LLM_MODEL", "claude-sonnet-5-5")
    assert sampling_kwargs(None, 0.2) == {}


def test_direct_call_sites_route_through_helpers():
    """直连 chat.completions.create 的调用里不许再写死 temperature= / json_object——
    回退了 Claude 会 400 且静默。"""
    import re
    from pathlib import Path
    src = Path(__file__).resolve().parents[1] / "src" / "openinvest"
    bad = []
    for p in src.rglob("*.py"):
        text = p.read_text(encoding="utf-8")
        for m in re.finditer(r"chat\.completions\.create\(", text):
            call = text[m.end():m.end() + 800].split("\n    )", 1)[0]
            if re.search(r"\btemperature\s*=|\"type\":\s*\"json_object\"", call):
                bad.append(f"{p.relative_to(src)}:{text[:m.start()].count(chr(10)) + 1}")
    assert not bad, bad
