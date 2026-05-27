"""jobs/event_watch 端到端 mock 单测

mock 链路：news_sources → event_normalizer → event_store → event_notifier + committee POST
重点：触发条件（severity ≥ mid + affected ∩ watched + stance ≠ neutral）是否准确
"""
from __future__ import annotations

import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from jobs import event_watch
from services.event_normalizer import NormalizedEvent
from services.news_sources import RawNewsItem


@pytest.fixture
def tmp_event_db(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "events.db")
        monkeypatch.setattr("db.event_store.DB_PATH", path)
        yield path


def _ne(idx, claim, stance, severity, affected, entities=None):
    return NormalizedEvent(
        raw_idx=idx,
        event={
            "one_line_claim": claim,
            "event_type": "earnings",
            "stance": stance,
            "severity": severity,
            "source_reliability": "high",
            "ts": "2026-05-13T10:00:00Z",
            "entities": entities or [],
            "affected_symbols": affected,
        },
        embedding=[0.0] * 1024,
        raw_item=RawNewsItem(
            src_name="rss:r", title=f"title-{idx}",
            url=f"https://r.co/{idx}", snippet="s",
        ),
    )


def test_run_no_trigger_when_neutral_or_low(tmp_event_db, monkeypatch):
    ctx = {
        "holdings": ["NDQ.AX"],
        "watching": ["GC=F"],
        "macro_tags": ["us_rates"],
        "queries": ["x"],
    }
    monkeypatch.setattr(event_watch, "_load_user_context", lambda: ctx)
    monkeypatch.setattr(event_watch, "fetch_all", lambda **kw: [
        RawNewsItem(src_name="r", title="t1", url="https://r.co/1", snippet="s"),
        RawNewsItem(src_name="r", title="t2", url="https://r.co/2", snippet="s"),
    ])
    monkeypatch.setattr(event_watch, "load_default_feeds", lambda: [])
    monkeypatch.setattr(event_watch, "normalize", lambda items: [
        _ne(0, "low risk claim", "risk", "low", ["NDQ.AX"]),
        _ne(1, "neutral", "neutral", "high", ["NDQ.AX"]),
    ])
    trigger = MagicMock()
    send = MagicMock()
    monkeypatch.setattr(event_watch, "_trigger_committee", trigger)
    monkeypatch.setattr(event_watch, "send_event_alert", send)

    out = event_watch.run()
    assert out["triggered"] == 0
    trigger.assert_not_called()
    send.assert_not_called()


def test_run_triggers_when_affected_high_risk(tmp_event_db, monkeypatch):
    ctx = {
        "holdings": ["NDQ.AX"],
        "watching": [],
        "macro_tags": [],
        "queries": ["x"],
    }
    monkeypatch.setattr(event_watch, "_load_user_context", lambda: ctx)
    monkeypatch.setattr(event_watch, "fetch_all", lambda **kw: [
        RawNewsItem(src_name="r", title="t1", url="https://r.co/1", snippet="s"),
    ])
    monkeypatch.setattr(event_watch, "load_default_feeds", lambda: [])
    monkeypatch.setattr(event_watch, "normalize", lambda items: [
        _ne(0, "Nvidia miss", "risk", "high", ["NDQ.AX"]),
    ])
    monkeypatch.setattr(event_watch, "_holdings_snapshot", lambda syms: {})
    trigger = MagicMock(return_value="task-abc")
    send = MagicMock()
    monkeypatch.setattr(event_watch, "_trigger_committee", trigger)
    monkeypatch.setattr(event_watch, "send_event_alert", send)

    out = event_watch.run()
    assert out["triggered"] == 1
    assert out["committee_task_id"] == "task-abc"
    trigger.assert_called_once()
    send.assert_called_once()
    # 邮件 kwargs 含 committee_task_id
    args, kwargs = send.call_args
    assert kwargs.get("committee_task_id") == "task-abc"


def test_run_dry_run_skips_email_and_committee(tmp_event_db, monkeypatch):
    monkeypatch.setattr(event_watch, "_load_user_context", lambda: {
        "holdings": ["NDQ.AX"], "watching": [], "macro_tags": [], "queries": ["x"],
    })
    monkeypatch.setattr(event_watch, "fetch_all", lambda **kw: [
        RawNewsItem(src_name="r", title="t", url="https://r.co/1", snippet="s"),
    ])
    monkeypatch.setattr(event_watch, "load_default_feeds", lambda: [])
    monkeypatch.setattr(event_watch, "normalize", lambda items: [
        _ne(0, "Nvidia miss", "risk", "high", ["NDQ.AX"]),
    ])
    trigger = MagicMock()
    send = MagicMock()
    monkeypatch.setattr(event_watch, "_trigger_committee", trigger)
    monkeypatch.setattr(event_watch, "send_event_alert", send)

    out = event_watch.run(dry_run=True)
    assert out["triggered"] == 1
    assert out["dry_run"] is True
    trigger.assert_not_called()
    send.assert_not_called()


def test_run_skips_duplicated_urls(tmp_event_db, monkeypatch):
    """第二次 run 同样的 url 不会再进 normalizer / store"""
    ctx = {"holdings": ["NDQ.AX"], "watching": [], "macro_tags": [], "queries": ["x"]}
    monkeypatch.setattr(event_watch, "_load_user_context", lambda: ctx)
    monkeypatch.setattr(event_watch, "load_default_feeds", lambda: [])
    monkeypatch.setattr(event_watch, "_holdings_snapshot", lambda syms: {})

    items = [RawNewsItem(src_name="r", title="t", url="https://r.co/0", snippet="s")]
    monkeypatch.setattr(event_watch, "fetch_all", lambda **kw: items)
    monkeypatch.setattr(event_watch, "normalize", lambda its: [
        _ne(0, "evt-x", "risk", "high", ["NDQ.AX"]),
    ])
    monkeypatch.setattr(event_watch, "_trigger_committee", lambda **kw: "t1")
    monkeypatch.setattr(event_watch, "send_event_alert", lambda *a, **kw: "rcv")

    first = event_watch.run()
    assert first["triggered"] == 1

    # 第二次 run：normalize 不应被调用（unseen 为空）
    normalize_spy = MagicMock(return_value=[])
    monkeypatch.setattr(event_watch, "normalize", normalize_spy)
    second = event_watch.run()
    assert second["triggered"] == 0
    normalize_spy.assert_not_called()


def test_holdings_snapshot_pnl_uses_quote_currency(monkeypatch):
    """回归：黄金积存金 PnL 必须用 get_quote 换算后的 CNY/克 价，不能直接拿原始 GC=F USD/oz。

    历史 bug：_holdings_snapshot 直接用 get_history_data 拿 GC=F 原始价（USD/oz≈4523），
    跟 CNY/克 成本（≈1008）相除，把真实浮亏 -1% 算成了 +348%。
    现在统一走 get_quote(holding)，price 保证与 avg_cost 同币种同单位。
    """
    from utils.quotes import QuoteSnapshot

    gold = {
        "symbol": "GC=F",
        "units": 133.8853,
        "avg_cost": 1008.33,          # CNY/克
        "cost_currency": "CNY",
        "unit_label": "克",
        "proxy_kind": "gold_cny_per_gram",
    }

    fake_pm = MagicMock()
    fake_pm.holdings.find.side_effect = lambda s: gold if s == "GC=F" else None
    monkeypatch.setattr(
        "core.portfolio_manager.PortfolioManager", lambda *a, **kw: fake_pm,
    )
    # get_quote 返回换算后的 CNY/克 现价（不是原始 4523 USD/oz）
    monkeypatch.setattr(
        "utils.quotes.get_quote",
        lambda h: QuoteSnapshot(
            symbol="GC=F", price=1000.28, currency="CNY", unit="克",
        ),
    )

    snap = event_watch._holdings_snapshot(["GC=F"])
    entry = snap["GC=F"]

    assert entry["price"] == pytest.approx(1000.28, abs=0.01)
    assert entry["currency"] == "CNY"
    # 真实浮亏约 -0.8%，绝不该是 +348%
    assert entry["pnl_pct"] == pytest.approx(1000.28 / 1008.33 - 1.0, abs=1e-6)
    assert entry["pnl_pct"] < 0
    assert abs(entry["pnl_pct"]) < 0.05
