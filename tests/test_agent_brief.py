"""`openinvest brief`：插件钩子注入的离线账本摘要。

守三件事：不联网（钩子可能每个会话都调）；不 mark_drained（否则看门狗看不到没人取的待办）；
顾问模式不列持仓。"""
from __future__ import annotations

import json
from argparse import Namespace
from datetime import datetime

import pytest

from openinvest.core.memory_store import MemoryStore
from openinvest.services import agent_queue
from openinvest.services.agent_brief import build_brief, render_brief_text


class _Store:
    def __init__(self, *a, **kw):
        pass

    def is_seen_url(self, url):
        return False


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    import yfinance

    def boom(*a, **kw):
        raise AssertionError("brief must not hit the network")

    monkeypatch.setattr(yfinance, "Ticker", boom)
    monkeypatch.setattr(yfinance, "download", boom)
    monkeypatch.setattr("openinvest.db.event_store.EventStore", _Store)
    monkeypatch.setattr("openinvest.scheduler.lock.INVEST_ROOT", tmp_path)


def _seed():
    s = MemoryStore()
    s.write("user", "user", {"display_name": "t", "risk_tolerance": "Balanced"}, "")
    s.write("strategy", "strategy", {"target_assets": [{"symbol": "NVDA"}, {"symbol": "GC=F"}]}, "")
    s.write("portfolio", "state", {"schema_version": 2, "cash": {"CNY": 1.0}, "holdings": [
        {"symbol": "NVDA", "display_name": "英伟达", "units": 10, "currency": "USD"},
        {"symbol": "510300.SS", "units": 2000, "currency": "CNY"},
    ]}, "")
    day = s.root / ".committee" / datetime.now().strftime("%Y-%m-%d")
    day.mkdir(parents=True)
    (day / "GC_F.md").write_text("**Symbol**: GC=F\n**Verdict**: HOLD (confidence 0.60)\n", encoding="utf-8")
    return s


def test_brief_lists_ledger_and_pending_without_draining(tmp_path):
    s = _seed()
    agent_queue.enqueue_committee(["NVDA"], [], "price spike")
    (s.root / ".agent_queue" / "news.jsonl").write_text(json.dumps(
        {"url": "https://x.co/1", "title": "t", "queued_at": datetime.now().astimezone().isoformat()}) + "\n")

    b = build_brief(tmp_path)
    assert [h["symbol"] for h in b["holdings"]] == ["NVDA", "510300.SS"]
    assert b["watchlist"] == ["GC=F"]                       # 已持有的 NVDA 不重复
    assert b["verdicts_today"] == [{"symbol": "GC=F", "verdict": "HOLD"}]
    assert b["pending"] == {"news": 1, "committees": 1}
    assert b["scheduler"] is None
    assert agent_queue.last_drained() is None              # 看门狗的"没人取"信号不能被吞

    text = render_brief_text(b)
    assert "NVDA (英伟达)" in text and "510300.SS" in text and "GC=F HOLD" in text
    assert "1 news item(s)" in text and "scheduler is not running" in text
    assert len(text) < 1200


def test_brief_needs_setup(tmp_path):
    b = build_brief(tmp_path)
    assert b == {"status": "needs_setup"}
    assert "invest-setup" in render_brief_text(b)


def test_brief_hides_holdings_in_advisory_mode(tmp_path, monkeypatch):
    _seed()
    monkeypatch.setenv("INVEST_ADVISORY_MODE", "1")
    text = render_brief_text(build_brief(tmp_path))
    assert "NVDA" not in text and "510300" not in text
    assert "openinvest tools first" in text


def test_cli_brief_text_goes_to_real_stdout(capfd):
    from openinvest import cli

    _seed()
    cli.cmd_brief(Namespace(text=True))
    out = capfd.readouterr().out
    assert out.startswith("[openInvest]") and "NVDA" in out


def test_brief_disabled_in_remote_mode(capfd):
    from openinvest import remote_dispatch as rd

    with pytest.raises(SystemExit):
        rd.maybe_dispatch_remote(Namespace(cmd="brief", text=True))
    assert "hub" in capfd.readouterr().out


# ---------- INVEST_MCP_SCOPE：Hermes 拆两条配置（agent 受信 / ledger untrusted） ----------

@pytest.mark.parametrize("scope", ["agent", "ledger"])
def test_mcp_scope_splits_tools_by_destructive_hint(scope, monkeypatch):
    import asyncio

    from openinvest.connectors import mcp_server

    mgr = mcp_server.mcp._tool_manager
    monkeypatch.setattr(mgr, "_tools", dict(mgr._tools))  # 移除只动副本，测完还原（别 reload 模块：会换掉别的测试持有的单例）
    before = asyncio.run(mcp_server.mcp.list_tools())
    mcp_server._apply_scope(scope)
    after = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    destructive = {t.name for t in before if t.annotations and t.annotations.destructiveHint}
    assert after == (destructive if scope == "ledger" else {t.name for t in before} - destructive)
    assert ("buy" in after) == (scope == "ledger")
    assert ("pending_news" in after) == (scope == "agent") and ("submit_committee" in after) == (scope == "agent")
