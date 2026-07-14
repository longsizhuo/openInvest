"""SDKAgent 缺 LLM key 时的报错契约

2026-07-14：裸抛 openai.OpenAIError 对 agentic 调用方（Hermes cron 等）是个
死胡同——与其读懂那句话去配置密钥，agent 更可能自行拼凑一个看不懂的变通方案
把它跑起来，从此这个用户的委员会跑在没人能诊断/维护的路径上（用户指出的真实
风险）。这个测试守：错误必须是 ValueError（不是 openai 库的异常类型），消息
里必须给出两条正规出路，不能让 agent 觉得"自己想办法"是个选项。
跑：uv run pytest tests/test_sdk_agent_key_guard.py -q
"""
from __future__ import annotations

import openai
import pytest

from openinvest.capabilities.sdk_agent import SDKAgent


def test_missing_key_raises_value_error_not_openai_error(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(ValueError) as exc_info:
        SDKAgent(
            system_prompt="test",
            api_key=None,
            base_url="https://api.deepseek.com",
            provider="deepseek",
        )

    assert not isinstance(exc_info.value, openai.OpenAIError)


def test_error_message_gives_two_concrete_paths_not_room_to_improvise(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(ValueError) as exc_info:
        SDKAgent(
            system_prompt="test",
            api_key=None,
            base_url="https://api.deepseek.com",
            provider="deepseek",
        )

    msg = str(exc_info.value)
    assert "DEEPSEEK_API_KEY" in msg
    assert "committee-protocol" in msg  # 指明 Coordinator 协议出路
    assert "不要尝试绕过" in msg  # 明确禁止自行变通，不给 agent 留发挥空间


def test_explicit_api_key_still_works(monkeypatch):
    """有 key 时行为不变——防御性检查不能误伤正常路径。"""
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    agent = SDKAgent(
        system_prompt="test",
        api_key="sk-fake-test-key",
        base_url="https://api.deepseek.com",
        provider="deepseek",
    )
    assert agent.client is not None


def test_env_fallback_key_still_works(monkeypatch):
    """caller 不传 key，env 里有 DEEPSEEK_API_KEY 时仍走通——回落链路不变。"""
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-fake-env-key")

    agent = SDKAgent(
        system_prompt="test",
        api_key=None,
        base_url="https://api.deepseek.com",
        provider="deepseek",
    )
    assert agent.client is not None
