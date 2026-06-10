"""test_ta 实验臂等价性 + SENTINEL 守门。

关键契约：INVEST_TA_ANALYSTS 未设时，实验分支行为与 main **完全等价**
（arm A 用同一份代码跑才能保证唯一变量是 flag）。
"""
from __future__ import annotations

import pytest


def _fake_agents(captured):
    class FakeAgent:
        def __init__(self, role):
            self._role = role

        def run(self, ctx):
            captured[self._role] = ctx
            if self._role == "quant":
                return "REGIME: uptrend\nSIGNAL: neutral\nSTRENGTH: 4\nONE_LINER: x\n"
            if self._role == "risk":
                return "SIGNAL: ok\nSTRENGTH: 3\nONE_LINER: x\n"
            if self._role == "cio":
                return ("VERDICT: HOLD\nCONFIDENCE: 0.5\nDOMINANT_VIEW: macro\n"
                        "SUGGESTED_ALLOC_CNY: 0\n")
            return ""
    return lambda _p, **kw: FakeAgent(kw.get("role"))


def _run(monkeypatch):
    from core import committee as cmt
    captured: dict = {}
    monkeypatch.setattr(cmt, "_create_agent", _fake_agents(captured))
    monkeypatch.setattr(cmt, "_persist", lambda *a, **kw: None)
    result = cmt.run_committee(
        asset={"symbol": "NDQ.AX", "display_name": "Test"},
        market_data="fake market",
        macro_view="fake macro",
        portfolio_summary="用户风险偏好: Balanced\n",
        max_debate_rounds=1,
    )
    return result, captured


def test_flag_off_no_ta_section(monkeypatch):
    """flag 未设：CIO 输入无实验段、report 字段为空 dict（= main 行为）"""
    monkeypatch.delenv("INVEST_TA_ANALYSTS", raising=False)
    result, captured = _run(monkeypatch)
    assert "TradingAgents 式分析师报告" not in captured.get("cio", "")
    assert result["report"].ta_analyst_reports == {}


def test_flag_on_injects_reports(monkeypatch):
    """flag 开：分析师报告 SENTINEL 进 CIO 输入 + report 字段"""
    SENT = "TA_REPORT_SENTINEL_abc"
    monkeypatch.setenv("INVEST_TA_ANALYSTS", "sentiment")
    import scripts.ta_integration as ti
    monkeypatch.setattr(ti, "run_ta_analysts", lambda sym, flag: {"sentiment": SENT})
    result, captured = _run(monkeypatch)
    assert SENT in captured.get("cio", ""), "分析师报告没进 CIO brief"
    assert result["report"].ta_analyst_reports == {"sentiment": SENT}


def test_flag_on_analyst_failure_graceful(monkeypatch):
    """实验臂抛异常 → 委员会照常完成（graceful）"""
    monkeypatch.setenv("INVEST_TA_ANALYSTS", "sentiment")
    import scripts.ta_integration as ti
    monkeypatch.setattr(ti, "run_ta_analysts",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    result, captured = _run(monkeypatch)
    assert result["verdict"]["verdict"] == "HOLD"
    assert "TradingAgents 式分析师报告" not in captured.get("cio", "")
