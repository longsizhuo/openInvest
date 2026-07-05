"""core/llm_telemetry.py 测试"""
from __future__ import annotations

import json

import pytest

from openinvest.core import llm_telemetry as t


@pytest.fixture(autouse=True)
def temp_telemetry(tmp_path, monkeypatch):
    """每个测试用 tmp_path 替代真实 telemetry 文件"""
    p = tmp_path / "llm_usage.jsonl"
    monkeypatch.setattr(t, "TELEMETRY_FILE", p)
    yield p


def test_estimate_cost_deepseek_chat():
    """1M input + 1M output tokens → v4-flash 价格 1.0 + 2.0 = 3.0 元
    (deepseek-chat 已 deprecated 映射到 v4-flash，pricing 同步)"""
    cost = t.estimate_cost_cny("deepseek-chat", 1_000_000, 1_000_000)
    assert cost == pytest.approx(3.0)


def test_estimate_cost_unknown_model_zero():
    assert t.estimate_cost_cny("nonexistent-model-99", 100, 100) == 0.0


def test_record_llm_call_writes_jsonl(temp_telemetry):
    meta = t.TelemetryMeta(agent_role="quant", asset="NDQ.AX", round="opening")
    rec = t.record_llm_call(
        meta,
        input_tokens=1500,
        output_tokens=300,
        latency_ms=2400,
        tool_calls=2,
        iteration=1,
    )
    assert rec["agent_role"] == "quant"
    assert rec["input_tokens"] == 1500
    assert rec["total_tokens"] == 1800
    # v4-flash: 1500 in * 1.0/1M + 300 out * 2.0/1M = 0.0015 + 0.0006 = 0.0021
    assert rec["cost_cny"] == pytest.approx(0.0021, abs=1e-5)

    # 文件真的写了
    lines = temp_telemetry.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["asset"] == "NDQ.AX"
    assert parsed["round"] == "opening"


def test_record_llm_call_failure_recorded(temp_telemetry):
    meta = t.TelemetryMeta(agent_role="cio")
    rec = t.record_llm_call(
        meta, input_tokens=0, output_tokens=0,
        latency_ms=500, ok=False, error="429 RateLimit",
    )
    assert rec["ok"] is False
    assert "429" in rec["error"]


def test_read_telemetry_returns_recent(temp_telemetry):
    meta = t.TelemetryMeta(agent_role="quant")
    for i in range(5):
        t.record_llm_call(meta, input_tokens=100, output_tokens=50, latency_ms=200)
    recs = t.read_telemetry(since=3)
    assert len(recs) == 3


def test_telemetry_summary_aggregates_by_role(temp_telemetry):
    """跑 1 macro + 2 quant + 1 risk + 1 cio，验证按 role 聚合"""
    for role in ("macro", "quant", "quant", "risk", "cio"):
        meta = t.TelemetryMeta(agent_role=role)
        t.record_llm_call(meta, input_tokens=1000, output_tokens=200, latency_ms=2000)

    s = t.telemetry_summary(since_records=100)
    assert s["total_calls"] == 5
    assert s["total_input_tokens"] == 5000
    assert s["by_role"]["quant"]["calls"] == 2
    assert s["by_role"]["macro"]["calls"] == 1
    assert s["by_role"]["cio"]["calls"] == 1
    # v4-flash: 1000 * 1.0/1M + 200 * 2.0/1M = 0.0014; × 2 quant calls = 0.0028
    assert s["by_role"]["quant"]["cost_cny"] == pytest.approx(0.0028, abs=1e-5)


def test_telemetry_summary_empty(temp_telemetry):
    s = t.telemetry_summary()
    assert s["total_calls"] == 0
    assert s["total_cost_cny"] == 0.0
    assert s["by_role"] == {}
