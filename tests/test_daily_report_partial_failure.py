"""单资产委员会失败不该让整份日报丢失。

session 把失败资产记成 asset_committees[sym] = {"error": ...}；daily_report 把它并进
skipped_assets，下游各处都跳过了，唯独末尾组装 result["verdicts"] 没跳 → KeyError 'verdict'，
邮件已发但宿主 agent cron 拿到的是 traceback 而不是报告。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import openinvest.jobs.daily_report as dr


def test_one_failed_asset_keeps_the_report(monkeypatch):
    import openinvest.capabilities.sdk_agent as sdk
    import openinvest.core.committee_runner as cr
    import openinvest.services.discipline as disc

    ok = {"verdict": {"verdict": "HOLD", "confidence": 0.6, "dominant_view": "macro", "alloc_cny": 0},
          "report": SimpleNamespace(cio_memo="memo", quant_view="q", risk_view="r")}
    monkeypatch.setattr(cr, "run_committee_session", lambda **kw: {
        "asset_committees": {"0700.HK": ok, "AAPL": {"error": "timeout"}},
        "macro_view": "MV", "event_brief": "", "errors": {"AAPL": "timeout"}})

    pm = MagicMock()
    pm.strategy = {"target_assets": [{"symbol": "0700.HK"}, {"symbol": "AAPL"}]}
    pm.cash = {}
    pm.get_user_status.return_value = SimpleNamespace(cash_cny=0.0, disposable_for_invest=0.0)
    monkeypatch.setattr(dr, "PortfolioManager", lambda: pm)
    monkeypatch.setattr(dr, "_get_last_close", lambda s, label: (100.0, 0))
    monkeypatch.setattr(dr, "_portfolio_summary", lambda *a: "PS")
    monkeypatch.setattr(dr, "get_macro_data", lambda: "MD")
    monkeypatch.setattr(dr, "_run_gemini_cli_review", lambda p: "G")
    monkeypatch.setattr(dr, "_load_asset_events", lambda targets: {})
    sent = []
    monkeypatch.setattr(dr, "send_gmail_notification", lambda c: sent.append(c) or "me@x")
    monkeypatch.setattr(sdk, "SDKAgent", lambda **kw: SimpleNamespace(run=lambda p: ""))
    monkeypatch.setattr(disc, "render_discipline_md", lambda: "")

    out = dr.run(include_report=True)

    assert set(out["verdicts"]) == {"0700.HK"}
    assert "AAPL" in out["skipped_assets"]
    assert out["status"] == "degraded"  # 部分失败 = 降级，对外语义钉死
    assert out["full_report"] and sent


def test_failed_reasoning_never_enters_second_opinion_or_loses_qualification_in_memory(monkeypatch,tmp_path):
    import openinvest.capabilities.sdk_agent as sdk
    import openinvest.core.committee_runner as cr
    import openinvest.services.discipline as disc
    failed={"verdict":{"verdict":"HOLD","confidence":0.6,"dominant_view":"risk","alloc_cny":0,
                        "decision_status":"REVIEW_REQUIRED","reasoning_review":{"status":"failed"}},
            "report":SimpleNamespace(cio_memo="AUDIT_ONLY_FAILED_PROPOSAL",quant_view="q",risk_view="r")}
    monkeypatch.setattr(cr,"run_committee_session",lambda **kwargs:{"asset_committees":{"SYNTHETIC":failed},"macro_view":"MV","event_brief":"","errors":{}})
    pm=MagicMock();pm.strategy={"target_assets":[{"symbol":"SYNTHETIC"}]};pm.cash={}
    from openinvest.core.memory_store import MemoryStore
    pm.store=MemoryStore(tmp_path/"memory")
    pm.get_user_status.return_value=SimpleNamespace(cash_cny=0.0,disposable_for_invest=0.0)
    monkeypatch.setattr(dr,"PortfolioManager",lambda:pm)
    monkeypatch.setattr(dr,"_get_last_close",lambda *args:(100.0,0))
    monkeypatch.setattr(dr,"_portfolio_summary",lambda *args:"PS")
    monkeypatch.setattr(dr,"get_macro_data",lambda:"MD")
    monkeypatch.setattr(dr,"_load_asset_events",lambda targets:{})
    calls=[]
    def prohibited(*args,**kwargs):
        calls.append(True)
        raise AssertionError("Unqualified proposal must not enter secondary model advice")
    monkeypatch.setattr(dr,"_run_gemini_cli_review",prohibited)
    monkeypatch.setattr(sdk,"SDKAgent",prohibited)
    monkeypatch.setattr(disc,"render_discipline_md",lambda:"")
    out=dr.run(send_email=False,include_report=True)
    assert calls==[]
    assert out["status"]=="degraded" and not out["verdicts"]["SYNTHETIC"]["current_advice_valid"]
    assert out["verdicts"]["SYNTHETIC"]["decision_status"]=="REVIEW_REQUIRED"
    assert out["verdicts"]["SYNTHETIC"]["reasoning_review"]["status"]=="failed"
    summary=(pm.store.root/"daily"/(out["date"]+".md")).read_text()
    assert "原提案仅供审计" in summary and "decision_status=REVIEW_REQUIRED" in summary and "reasoning_review=failed" in summary
