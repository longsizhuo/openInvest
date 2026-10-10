"""无裁决模式（INVEST_NO_VERDICT_MODE）：委员会只出正反理由，任何出口都不能带 verdict/金额/置信度。

公开演示实例用（未经许可不得向公众提供金融领域的确定性结论）。闸在服务端：
书记员替代 CIO + 逐字闸 + 出口白名单；web_api 端点太多没逐个加闸，直接拒绝启动。
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from openinvest.core.committee import debate


@pytest.mark.parametrize("text", [
    "建议分批买入",
    "宏观角色裁决HOLD",                 # 汉字紧挨英文，\b 失效的情形
    "accumulate on dips",
    "首笔 ¥2,700",
    "仓位上限 10%",
    "支撑3.90，阻力4.30",
    "价格下方有 4.30 支撑参考",         # 实跑 DeepSeek 时漏过的语序
    "期末低于现价概率 48%",
    "有 60% 的概率反弹",
    "适合长期持有",
    "可以考虑逢低加仓",
    "投入 2000 元",
    "You should buy now",
])
def test_gate_blocks_conclusions(text):
    assert debate.find_verdict_language(text)


@pytest.mark.parametrize("text", [
    "Quant 指出近两年价格分位 3%，RSI 40.6，处于低位",
    "值得注意的是，买方力量偏弱",
    "利率应该会继续上行，对成长股估值是压力",
    "美债规模 36万亿",
    "the household budget",
    "Risk Officer 提醒：这只基金过去两年最大回撤 -48%",
])
def test_gate_passes_plain_analysis(text):
    assert debate.find_verdict_language(text) == []


def _fake_llm(monkeypatch, scribe_replies):
    created = []

    def fake_create(system_prompt, **kw):
        created.append(kw.get("role"))
        return SimpleNamespace(role=kw.get("role"))

    replies = list(scribe_replies)

    def fake_ask(agent, ctx):
        assert agent.role == "scribe", f"no-verdict 模式不该问 {agent.role}"
        return replies.pop(0)

    monkeypatch.setenv("INVEST_NO_VERDICT_MODE", "1")
    monkeypatch.setattr(debate, "_create_agent", fake_create)
    monkeypatch.setattr(debate, "_ask", fake_ask)
    monkeypatch.setattr(debate, "_parallel_ask", lambda pairs: [
        "SIGNAL: neutral\nSTRENGTH: 4\nONE_LINER: 支撑3.90阻力4.30",
        "SIGNAL: concerned\nSTRENGTH: 5\nONE_LINER: 建议建仓比例上限 10%",
    ])
    monkeypatch.setattr(debate, "_persist", lambda *a, **k: pytest.fail("no-verdict 模式不落盘"))
    return created


def _run():
    return debate.run_committee(
        {"symbol": "FUND:123456", "display_name": "示例基金"},
        market_data="m", macro_view="SIGNAL: risk_off\nONE_LINER: 建议减仓",
        portfolio_summary="p",
    )


def test_scribe_replaces_cio_and_rewrites_once(monkeypatch):
    created = _fake_llm(monkeypatch, [
        "## 支持的理由\n- 建议分批买入",                       # 第 1 版命中闸
        "## 支持的理由\n- 价格分位 3%\n## 反对的理由\n- 宏观利率上行",
    ])
    out = _run()
    assert "cio" not in created and created.count("scribe") == 2
    assert set(out) == {"asset", "debate_summary", "debate"}
    assert "价格分位 3%" in out["debate_summary"]
    assert "不构成投资建议" in out["debate_summary"]


def test_scribe_fails_closed_after_second_hit(monkeypatch):
    _fake_llm(monkeypatch, ["- 建议买入", "- 首笔 ¥2,700"])
    out = _run()
    assert "debate_summary" not in out
    assert "拦截" in out["error"]


def test_scribe_unavailable_is_blocked(monkeypatch):
    marker = debate.AGENT_UNAVAILABLE_MARKER
    _fake_llm(monkeypatch, [f"{marker} reason=x", f"{marker} reason=x"])
    assert "error" in _run()


def test_mcp_run_committee_returns_only_summary(monkeypatch):
    monkeypatch.setenv("INVEST_NO_VERDICT_MODE", "1")
    from openinvest.connectors import mcp_server as m

    monkeypatch.setattr("openinvest.core.decision_ledger.parse_committee_file",
                        lambda p: pytest.fail("no-verdict 模式不读当天缓存（缓存含 verdict）"))

    def fake_session(*, symbols, max_debate_rounds, progress_callback):
        return {"asset_committees": {symbols[0]: {
            "debate_summary": "## 支持的理由\n- x",
            "path_reference": "期末低于现价概率 48%",
            "regime_probability": "p",
        }}}

    monkeypatch.setattr("openinvest.core.committee_runner.run_committee_session", fake_session)
    out = asyncio.run(m.run_committee(symbol="GC=F"))
    assert out == {"symbol": "GC=F", "debate_summary": "## 支持的理由\n- x"}

    monkeypatch.setattr("openinvest.core.committee_runner.run_committee_session",
                        lambda **kw: {"asset_committees": {"GC=F": {"error": "辩论纪要未通过无裁决检查，已拦截",
                                                                     "path_reference": "概率 48%"}}})
    out = asyncio.run(m.run_committee(symbol="GC=F"))
    assert out == {"status": "error", "error": "辩论纪要未通过无裁决检查，已拦截"}


def test_mcp_verdict_tools_disabled(monkeypatch):
    monkeypatch.setenv("INVEST_NO_VERDICT_MODE", "1")
    from openinvest.connectors import mcp_server as m

    with pytest.raises(RuntimeError, match="INVEST_NO_VERDICT_MODE"):
        m.explain_decision("2026-01-01/GC=F")
    with pytest.raises(RuntimeError, match="INVEST_NO_VERDICT_MODE"):
        m.decisions()


def test_web_api_refuses_to_start():
    env = {**os.environ, "INVEST_NO_VERDICT_MODE": "1"}
    r = subprocess.run([sys.executable, "-c", "import openinvest.connectors.web_api"],
                       env=env, capture_output=True, text=True)
    assert r.returncode != 0
    assert "INVEST_NO_VERDICT_MODE" in r.stderr
