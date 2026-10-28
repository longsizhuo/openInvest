"""agent 模式日报：只读当天 Agent 交卷，不跑后端委员会、不调翻译官；没交卷的标的响亮列出。"""
from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import openinvest.jobs.daily_report as dr
from openinvest.calc.gold import GoldPriceSnapshot

# 金价不能真去 yfinance 拉（conftest 已断网）：拿不到就整块跳过 GC=F，断言里的 GC=F 交卷就验不到
_GOLD = GoldPriceSnapshot(gold_usd_per_oz=2400.0, usdcny_rate=7.2, spot_cny_per_gram=555.6,
                          bank_cny_per_gram=563.9, offset_pct=0.015)


def test_agent_mode_reads_submissions_and_flags_missing(monkeypatch):
    import openinvest.capabilities.sdk_agent as sdk
    import openinvest.core.committee_runner as cr
    import openinvest.services.discipline as disc
    from openinvest.core.memory_store import MemoryStore

    monkeypatch.setenv("INVEST_REASONING_BY", "agent")
    today = datetime.now().strftime("%Y-%m-%d")
    d = MemoryStore().root / ".committee" / today
    d.mkdir(parents=True, exist_ok=True)
    (d / "GC_F_committee.json").write_text(json.dumps({
        "symbol": "GC=F", "verdict": {"verdict": "HOLD", "confidence": 0.6,
                                      "dominant_view": "macro", "alloc_cny": 0},
        "macro_view": "宏观偏紧", "quant_view": "q", "risk_view": "r", "cio_memo": "CIO 说明",
        "path_reference": "", "path_profile": None,
    }), encoding="utf-8")

    pm = MagicMock()
    pm.strategy = {"target_assets": [{"symbol": "GC=F"}, {"symbol": "NDQ.AX"}]}
    pm.cash = {}
    pm.get_user_status.return_value = SimpleNamespace(cash_cny=0.0, disposable_for_invest=0.0)
    monkeypatch.setattr(dr, "PortfolioManager", lambda: pm)
    monkeypatch.setattr(dr, "_get_last_close", lambda s, label: (100.0, 0))
    monkeypatch.setattr(dr, "get_gold_snapshot", lambda **k: _GOLD)
    monkeypatch.setattr(dr, "_portfolio_summary", lambda *a: "PS")
    monkeypatch.setattr(dr, "get_macro_data", lambda: "MD")
    monkeypatch.setattr(dr, "_run_gemini_cli_review", lambda p: pytest.fail("agent 模式不该发起 Gemini 第二意见"))
    monkeypatch.setattr(cr, "run_committee_session", lambda **kw: pytest.fail("agent 模式不该跑后端委员会"))
    monkeypatch.setattr(sdk, "SDKAgent", lambda **kw: pytest.fail("agent 模式不该调翻译官"))
    monkeypatch.setattr(disc, "render_discipline_md", lambda: "")

    out = dr.run(send_email=False, include_report=True)
    assert out["verdicts"]["GC=F"]["verdict"] == "HOLD"
    assert out["missing_verdicts"] == ["NDQ.AX"] and out["status"] == "degraded"
    assert out["full_report"].startswith("> ⚠️") and "NDQ.AX" in out["full_report"].splitlines()[0]
    assert "CIO 说明" in out["full_report"]


def _wire(monkeypatch, targets):
    import openinvest.capabilities.sdk_agent as sdk
    import openinvest.core.committee_runner as cr
    import openinvest.services.discipline as disc
    monkeypatch.setenv("INVEST_REASONING_BY", "agent")
    pm = MagicMock()
    pm.strategy = {"target_assets": [{"symbol": s} for s in targets]}
    pm.cash = {}
    pm.get_user_status.return_value = SimpleNamespace(cash_cny=0.0, disposable_for_invest=0.0)
    monkeypatch.setattr(dr, "PortfolioManager", lambda: pm)
    monkeypatch.setattr(dr, "_get_last_close", lambda s, label: (100.0, 0))
    monkeypatch.setattr(dr, "get_gold_snapshot", lambda **k: _GOLD)
    monkeypatch.setattr(dr, "_portfolio_summary", lambda *a: "PS")
    monkeypatch.setattr(dr, "get_macro_data", lambda: "MD")
    monkeypatch.setattr(dr, "_run_gemini_cli_review", lambda p: pytest.fail("agent 模式不该发起 Gemini 第二意见"))
    monkeypatch.setattr(cr, "run_committee_session", lambda **kw: pytest.fail("不该跑后端委员会"))
    monkeypatch.setattr(sdk, "SDKAgent", lambda **kw: pytest.fail("不该调翻译官"))
    monkeypatch.setattr(disc, "render_discipline_md", lambda: "")


def test_all_missing_and_corrupt_sidecar_count_as_missing(monkeypatch):
    from openinvest.core.memory_store import MemoryStore
    _wire(monkeypatch, ["GC=F", "NDQ.AX"])
    d = MemoryStore().root / ".committee" / datetime.now().strftime("%Y-%m-%d")
    d.mkdir(parents=True, exist_ok=True)
    (d / "GC_F_committee.json").write_text("{not json", encoding="utf-8")       # 损坏
    (d / "NDQ_AX_committee.json").write_text('{"symbol": "NDQ.AX"}', encoding="utf-8")  # 缺 verdict
    out = dr.run(send_email=False, include_report=True)
    assert out["verdicts"] == {} and out["missing_verdicts"] == ["GC=F", "NDQ.AX"]
    first = out["full_report"].splitlines()[0]
    assert "GC=F" in first and "NDQ.AX" in first
