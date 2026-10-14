"""场外基金（FUND:xxxxxx）在委员会 / 日报估值路径上与 status 同口径。

yfinance 没有场外基金：service layer 的 portfolio_summary 与 cron 日报的总资产都必须
经 utils.quotes 的东方财富净值估值，否则 status 显示按净值、委员会看到按成本价 / 被剔除，
两边总资产对不上（CLAUDE.md "跨 entry 漂移"同类）。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd

import openinvest.utils.quotes as quotes
from openinvest.utils.eastmoney_fund import FundNavSnapshot

FUND = {"symbol": "FUND:123456", "kind": "fund", "units": 100, "avg_cost": 1.5,
        "cost_currency": "CNY", "unit_label": "份"}


def _fake_nav(symbol):
    return FundNavSnapshot(code="123456", nav=2.0, nav_date="2026-01-05", is_stale=False)


def test_service_portfolio_summary_prices_fund_via_quote(monkeypatch):
    import openinvest.utils.exchange_fee as ef
    import openinvest.utils.fx as fx
    import openinvest.utils.portfolio_summary as ps
    from openinvest.core.runner.loaders import _build_default_portfolio_summary

    asked = []

    def fake_history(symbol, period="5d", **kw):
        asked.append(symbol)
        return pd.DataFrame({"Close": [10.0]}, index=pd.date_range("2026-01-05", periods=1))

    seen = {}
    monkeypatch.setattr(ef, "get_history_data", fake_history)
    monkeypatch.setattr(quotes, "fetch_fund_nav", _fake_nav)
    monkeypatch.setattr(fx, "total_portfolio_value_cny", lambda pm, prices, **kw: (0.0, {}))
    monkeypatch.setattr(ps, "portfolio_summary_text",
                        lambda pm, total, prices, **kw: seen.update(prices) or "PS")

    pm = SimpleNamespace(holdings=[
        dict(FUND),
        {"symbol": "TEST.AX", "kind": "etf", "units": 10, "cost_currency": "AUD"},
    ])
    assert _build_default_portfolio_summary(pm) == "PS"
    assert seen == {"FUND:123456": 2.0, "TEST.AX": 10.0}
    assert asked == ["TEST.AX"]          # 基金不去问 yfinance


def test_daily_report_total_assets_includes_fund_holding(monkeypatch):
    import openinvest.capabilities.sdk_agent as sdk
    import openinvest.core.committee_runner as cr
    import openinvest.jobs.daily_report as dr
    import openinvest.services.discipline as disc

    def session(**kw):
        verdict = {"verdict": "HOLD", "confidence": 0.6, "dominant_view": "neutral", "alloc_cny": 0}
        report = SimpleNamespace(cio_memo="memo", quant_view="q", risk_view="r")
        return {"asset_committees": {"TEST.AX": {"verdict": verdict, "report": report}},
                "macro_view": "MV", "event_brief": "", "errors": {}}

    class Translator:
        def __init__(self, **kw):
            pass

        def run(self, prompt):
            return ""

    captured = {}
    pm = MagicMock()
    pm.strategy = {"target_assets": [{"symbol": "TEST.AX"}]}
    pm.cash = {}
    pm.holdings = [dict(FUND)]
    pm.get_user_status.return_value = SimpleNamespace(cash_cny=0.0, disposable_for_invest=0.0)
    monkeypatch.setattr(dr, "PortfolioManager", lambda: pm)
    monkeypatch.setattr(dr, "_get_last_close", lambda s, label: (100.0, 0))
    monkeypatch.setattr(dr, "get_gold_snapshot", lambda **k: None)
    monkeypatch.setattr(dr, "_load_asset_events", lambda *a, **k: {})
    monkeypatch.setattr(
        dr, "_portfolio_summary",
        lambda pm_, total, prices, **kw: captured.update(total=total, prices=dict(prices)) or "PS",
    )
    monkeypatch.setattr(dr, "get_macro_data", lambda: "MD")
    monkeypatch.setattr(dr, "_run_gemini_cli_review", lambda p: "G")
    monkeypatch.setattr(dr, "send_gmail_notification", lambda c: "me@x")
    monkeypatch.setattr(cr, "run_committee_session", session)
    monkeypatch.setattr(sdk, "SDKAgent", Translator)
    monkeypatch.setattr(disc, "render_discipline_md", lambda: "")
    monkeypatch.setattr(quotes, "fetch_fund_nav", _fake_nav)

    dr.run()
    assert captured["prices"]["FUND:123456"] == 2.0
    assert captured["total"] == 200.0    # 100 份 × 净值 2.0，没被当缺价剔除
