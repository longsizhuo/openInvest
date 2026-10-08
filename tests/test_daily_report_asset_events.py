"""D17：日报邮件在单股资产裁决旁列"近 7 天已入库、severity≥mid 的资产专属事件"。

契约：这张列表只进邮件（HTML + 纯文本），不进委员会 / Gemini / 翻译官的任何输入。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import openinvest.jobs.daily_report as dr
from openinvest.jobs.daily_report_builder import ASSET_EVENT_LINES, assemble_full_report
from openinvest.services.symbol_map import is_single_stock

CLAIM = "Regulator opens probe into platform's advertising practices"


@pytest.fixture
def events_db(monkeypatch, tmp_path):
    monkeypatch.setattr("openinvest.db.event_store.DB_PATH", str(tmp_path / "events.db"))
    from openinvest.db.event_store import EventStore
    store = EventStore(embedding_dim=4)
    ts = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()

    def add(claim, syms, *, etype="regulatory", sev="mid"):
        store.upsert_event({"one_line_claim": claim, "event_type": etype, "stance": "risk",
                            "severity": sev, "source_reliability": "high", "ts": ts,
                            "entities": [], "affected_symbols": syms}, embedding=None)
    return add


def test_is_single_stock():
    for s in ("0700.HK", "TCEHY", "AAPL", "600519.SS", "000001.SZ", "300750.SZ"):
        assert is_single_stock(s), s
    for s in ("^NDX", "GC=F", "AUDCNY=X", "BTC-USD", "QQQ", "159919.SZ", "000300.SS",
              "399001.SZ", "512890.SS", ""):
        assert not is_single_stock(s), s
    assert not is_single_stock("ABCD", tracks="^TEST")  # 用户 tracks 声明 = 跟踪型


def test_loader_picks_asset_specific_events(events_db):
    events_db(CLAIM, ["TCEHY"])                                         # ADR 打标 → 算本标的
    events_db(CLAIM + " amid scrutiny", ["0700.HK"], sev="high")        # 同一新闻转述 → 合并
    events_db("Hong Kong stocks slide on rate fears", ["0700.HK", "^HSI"], etype="macro")
    events_db("Three platforms report results", ["0700.HK", "9988.HK", "3690.HK"], etype="earnings")
    events_db("Two platforms sign content deal", ["0700.HK", "TCEHY", "9988.HK"], etype="ma")
    events_db("Minor product update", ["0700.HK"], sev="low")
    events_db("Gold miner output rises", ["GC=F"], etype="other")
    out = dr._load_asset_events([{"symbol": "0700.HK"}, {"symbol": "GC=F"}, {"symbol": "159919.SZ"}])
    assert set(out) == {"0700.HK"}                                      # 商品 / ETF 不列
    evs = out["0700.HK"]
    assert [e["one_line_claim"] for e in evs] == [CLAIM + " amid scrutiny", "Two platforms sign content deal"]
    assert evs[0]["severity"] == "high" and evs[0]["similar"] == 2 and evs[1]["similar"] == 1


def test_loader_graceful_on_store_failure(monkeypatch):
    monkeypatch.setattr("openinvest.db.event_store.EventStore",
                        MagicMock(side_effect=RuntimeError("db gone")))
    assert dr._load_asset_events([{"symbol": "0700.HK"}]) == {}


def _committee(sym):
    v = {"verdict": "HOLD", "confidence": 0.6, "dominant_view": "neutral", "alloc_cny": 0}
    rep = SimpleNamespace(cio_memo="memo", quant_view="q", risk_view="r")
    return {sym: {"verdict": v, "report": rep}}


def _render(asset_events, target="email"):
    return assemble_full_report(
        today="2026-10-08", macro_view="m", gold_snapshot_text="g", friction_report="f",
        target_assets=[{"symbol": "0700.HK"}], asset_committees=_committee("0700.HK"),
        skipped_assets=set(), total_assets_cny=0, final_decision_gemini="x",
        render_target=target, asset_events=asset_events)


def test_builder_renders_list_with_cap_in_html_and_plain():
    from openinvest.services.notifier import render_markdown_email
    evs = [{"ts": f"2026-10-0{i}T01:00:00+00:00", "severity": "mid", "one_line_claim": f"claim-{i}",
            "similar": 3 if i == 1 else 1} for i in range(1, ASSET_EVENT_LINES + 3)]
    for target in ("email", "chat"):
        md = _render({"0700.HK": evs}, target)
        assert "**近 7 天资产专属事件**" in md
        assert "- 2026-10-01 [mid] claim-1（相似报道 ×3）" in md
        assert f"claim-{ASSET_EVENT_LINES}" in md and f"claim-{ASSET_EVENT_LINES + 1}" not in md
        assert "- …另有 2 条" in md
    html = render_markdown_email(_render({"0700.HK": evs}))
    assert "<li>2026-10-01 [mid] claim-1（相似报道 ×3）</li>" in html
    assert "近 7 天资产专属事件" not in _render({})                         # 无事件不留空壳


def test_builder_escapes_untrusted_claim():
    """claim 是外部新闻文本：<img> 追踪像素 / javascript: 链接 / 外链都只能以文字出现；
    chat 变体不做 HTML 转义（"S&P" 原样），但 [x](url) 也不能变成可点链接。"""
    from openinvest.services.notifier import render_markdown_email
    bad = ('S&P probe <img src="http://track.example/p.gif"> see [details](javascript:alert(1)) '
           'and [site](http://evil.example)')
    evs = [{"ts": "2026-10-07T01:00:00+00:00", "severity": "high", "one_line_claim": bad, "similar": 1}]
    html = render_markdown_email(_render({"0700.HK": evs}))
    assert "<img" not in html and 'href="javascript' not in html and 'href="http://evil' not in html
    assert "&lt;img" in html and "[details](javascript:alert(1))" in html  # 文字还在，只是不生效
    chat = _render({"0700.HK": evs}, "chat")
    assert "[details](" not in chat and "[site](" not in chat
    assert "S&P probe <img" in chat and "&amp;" not in chat


def test_contract_list_reaches_email_but_no_llm_input(events_db, monkeypatch):
    """跑真 daily_report.run（外部依赖全 mock）：列表进邮件；委员会 session 参数、Gemini
    prompt、翻译官 prompt 都不含它，且 session 参数与"库里没事件"时逐字相同。"""
    import openinvest.capabilities.sdk_agent as sdk
    import openinvest.core.committee_runner as cr
    import openinvest.services.discipline as disc

    captured = {"session": [], "llm": [], "email": []}

    def session(**kw):
        captured["session"].append(kw)
        return {"asset_committees": _committee("0700.HK"), "macro_view": "MV",
                "event_brief": "", "errors": {}}

    class Translator:
        def __init__(self, **kw):
            pass

        def run(self, prompt):
            captured["llm"].append(prompt)
            return ""

    pm = MagicMock()
    pm.strategy = {"target_assets": [{"symbol": "0700.HK"}]}
    pm.cash = {}
    pm.get_user_status.return_value = SimpleNamespace(cash_cny=0.0, disposable_for_invest=0.0)
    monkeypatch.setattr(dr, "PortfolioManager", lambda: pm)
    monkeypatch.setattr(dr, "_get_last_close", lambda s, label: (100.0, 0))
    monkeypatch.setattr(dr, "_portfolio_summary", lambda *a: "PS")
    monkeypatch.setattr(dr, "get_macro_data", lambda: "MD")
    monkeypatch.setattr(dr, "_run_gemini_cli_review", lambda p: captured["llm"].append(p) or "G")
    monkeypatch.setattr(dr, "send_gmail_notification", lambda c: captured["email"].append(c) or "me@x")
    monkeypatch.setattr(cr, "run_committee_session", session)
    monkeypatch.setattr(sdk, "SDKAgent", Translator)
    monkeypatch.setattr(disc, "render_discipline_md", lambda: "")

    assert dr.run()["email"]["sent"] is True            # 库里还没事件
    events_db(CLAIM, ["TCEHY"])
    assert dr.run()["email"]["sent"] is True            # 库里有资产专属事件

    from openinvest.services.notifier import render_markdown_email
    assert CLAIM not in captured["email"][0] and CLAIM in captured["email"][1]
    assert CLAIM in render_markdown_email(captured["email"][1])
    assert len(captured["llm"]) == 4 and not any(CLAIM in p for p in captured["llm"])
    assert captured["llm"][0] == captured["llm"][2] and captured["llm"][1] == captured["llm"][3]
    assert captured["session"][0] == captured["session"][1]
    assert CLAIM not in repr(captured["session"])
