"""agent 能写 target_pct（#449）：track_asset / set_allocations 带 target_pct，写入时校验 Σ ≤ 股票目标，
rebalance_plan 读到新的 target_pct（不再靠手改 strategy.md、不再走平分）。

不联网：行情 / 汇率 patch 成假的（同 test_rebalance_plan_service）。
"""
from __future__ import annotations

import argparse
import json

import pytest

from openinvest.core.memory_store import MemoryStore
from openinvest.core.portfolio_manager import PortfolioManager
from openinvest.services import rebalance_plan as rp
from openinvest.services import strategy_write as svc
from openinvest.utils import fx
from openinvest.utils.quotes import QuoteSnapshot


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    prices = {"510300.SS": 4.0, "AAPL": 100.0, "FUND:110020": 2.0}
    monkeypatch.setattr(rp, "get_quote", lambda h: QuoteSnapshot(
        h["symbol"], prices[h["symbol"]], "CNY" if h["symbol"] != "AAPL" else "USD", "share", "2026-10-16"))
    monkeypatch.setattr(fx, "get_fx_rate", lambda q, b="CNY", as_of_date=None: 1.0 if q == b else 7.0)


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(tmp_path / "memory")
    s.write("user", "user", {"display_name": "T", "risk_tolerance": "Balanced"}, "")
    s.write("strategy", "strategy", {
        "target_allocation_stock": 0.7, "target_allocation_cash": 0.3,
        "target_assets": [{"symbol": sym, "max_single_invest_cny": 10_000}
                          for sym in ("510300.SS", "AAPL", "FUND:110020")],
    }, "# body")
    s.write("portfolio", "state", {"schema_version": 2, "cash": {"CNY": 53_000.0, "USD": 1_000.0}, "holdings": [
        {"symbol": "510300.SS", "kind": "etf", "units": 5_000.0, "avg_cost": 4.0, "cost_currency": "CNY"},
        {"symbol": "AAPL", "kind": "equity", "units": 50.0, "avg_cost": 90.0, "cost_currency": "USD"}]}, "")
    return s


def _pcts(store):
    return {a["symbol"]: a.get("target_pct") for a in store.read("strategy").metadata["target_assets"]}


def test_track_asset_writes_target_pct_on_new_and_existing(store):
    svc.upsert_target_asset("510300.SS", {"target_pct": 0.3}, store=store)                    # 已有 → 只改这个字段
    out = svc.upsert_target_asset("0700.HK", {"max_single_invest_cny": 5000, "target_pct": 0.1}, store=store)
    assert out["status"] == "ok" and _pcts(store) == {"510300.SS": 0.3, "AAPL": None, "FUND:110020": None, "0700.HK": 0.1}
    assert "写操作" not in store.read("strategy").body and store.read("strategy").body.strip() == "# body"


def test_sum_over_stock_target_rejected_with_current_total_and_rolled_back(store):
    svc.upsert_target_asset("510300.SS", {"target_pct": 0.4}, store=store)
    svc.upsert_target_asset("AAPL", {"target_pct": 0.2}, store=store)
    with pytest.raises(ValueError) as e:
        svc.upsert_target_asset("FUND:110020", {"target_pct": 0.2}, store=store)              # 0.8 > 0.7
    msg = str(e.value)
    assert "80.0%" in msg and "70.0%" in msg and "510300.SS 40.0%" in msg                     # 报出当前合计 + 上限 + 构成
    assert _pcts(store)["FUND:110020"] is None                                                  # 回滚：没写半截


def test_sum_exactly_at_stock_target_passes_despite_float_noise(store):
    for sym, pct in (("510300.SS", 0.3), ("AAPL", 0.2), ("FUND:110020", 0.2)):                # 0.3+0.2+0.2 = 0.7000000000000001
        svc.upsert_target_asset(sym, {"target_pct": pct}, store=store)
    assert sum(v for v in _pcts(store).values()) == pytest.approx(0.7)


def test_single_target_pct_out_of_range_rejected_by_schema(store):
    for bad in (1.5, -0.1):
        with pytest.raises(ValueError, match="validation failed"):
            svc.upsert_target_asset("AAPL", {"target_pct": bad}, store=store)
    assert _pcts(store)["AAPL"] is None


def test_lowering_stock_target_below_sum_is_rejected_unless_pcts_move_with_it(store):
    svc.set_allocations(target_pcts={"510300.SS": 0.4, "AAPL": 0.2}, store=store)
    with pytest.raises(ValueError, match="60.0%"):
        svc.set_allocations(0.5, 0.5, store=store)                                             # 单独降 → Σ 0.6 > 0.5
    assert store.read("strategy").metadata["target_allocation_stock"] == 0.7
    svc.set_allocations(0.5, 0.5, target_pcts={"510300.SS": 0.3, "AAPL": 0.2}, store=store)  # 同次一起调 → 通过
    meta = store.read("strategy").metadata
    assert meta["target_allocation_stock"] == 0.5 and _pcts(store)["510300.SS"] == 0.3


def test_set_allocations_per_symbol_clear_unknown_and_arg_shape(store):
    svc.set_allocations(target_pcts={"AAPL": 0.25}, store=store)                               # 不动 stock/cash，只设标的
    meta = store.read("strategy").metadata
    assert (meta["target_allocation_stock"], meta["target_allocation_cash"]) == (0.7, 0.3) and _pcts(store)["AAPL"] == 0.25
    svc.set_allocations(target_pcts={"AAPL": None}, store=store)                               # null = 清掉
    assert "target_pct" not in next(a for a in store.read("strategy").metadata["target_assets"] if a["symbol"] == "AAPL")
    with pytest.raises(svc.StrategyNotFound, match="track_asset"):
        svc.set_allocations(target_pcts={"NOPE": 0.1}, store=store)
    with pytest.raises(ValueError, match="要么都传"):
        svc.set_allocations(0.6, store=store)
    with pytest.raises(ValueError, match="至少传"):
        svc.set_allocations(store=store)


def test_rebalance_plan_reads_agent_written_target_pct(store):
    """端到端：写之前整份没填 → 平分（equal_split + 警告）；agent 经 track_asset / set_allocations 写完 → 按显式权重。"""
    before = rp.build_rebalance_plan("hybrid", 5, pm=PortfolioManager(store))
    assert {r["target_source"] for r in before["rows"]} == {"equal_split"} and {r["target_pct"] for r in before["rows"]} == {23.33}

    svc.upsert_target_asset("510300.SS", {"target_pct": 0.40}, store=store)                    # track_asset 路径
    svc.set_allocations(target_pcts={"AAPL": 0.10, "FUND:110020": 0.20}, store=store)           # set_allocations 路径
    after = rp.build_rebalance_plan("hybrid", 5, pm=PortfolioManager(store))
    rows = {r["symbol"]: r for r in after["rows"]}
    assert {s: r["target_pct"] for s, r in rows.items()} == {"510300.SS": 40.0, "AAPL": 10.0, "FUND:110020": 20.0}
    assert {r["target_source"] for r in rows.values()} == {"strategy"}
    assert not any("没写 target_pct" in w for w in after["warnings"])


def test_cli_wiring(store, monkeypatch, capfd):
    import openinvest.services.strategy_write as svc_mod
    from openinvest import cli
    from openinvest.skill_cmds import strategy_cmds as sc

    monkeypatch.setattr(svc_mod, "_store", lambda s: store if s is None else s)

    # argparse 接线：--target-pct 在 track_asset 是单值 float，在 set_allocations 是可重复的 SYMBOL=PCT
    parser = cli.build_parser()
    ns = parser.parse_args(["track_asset", "--symbol", "AAPL", "--target-pct", "0.25"])
    assert ns.target_pct == 0.25
    sc.cmd_track_asset(ns)
    assert json.loads(capfd.readouterr().out)["status"] == "ok" and _pcts(store)["AAPL"] == 0.25

    ns = parser.parse_args(["set_allocations", "--target-pct", "510300.SS=0.3", "--target-pct", "FUND:110020=0.1"])
    assert ns.stock is None and ns.cash is None
    sc.cmd_set_allocations(ns)
    capfd.readouterr()
    assert _pcts(store) == {"510300.SS": 0.3, "AAPL": 0.25, "FUND:110020": 0.1}

    ns = parser.parse_args(["set_allocations", "--target-pct", "GC=F=0.5"])                     # symbol 本身含 '='
    assert sc._parse_target_pcts(ns.target_pct) == {"GC=F": 0.5}

    # 错误路径：超了 → exit 1 + error JSON 报合计；格式错 → 同样
    with pytest.raises(SystemExit):
        sc.cmd_track_asset(parser.parse_args(["track_asset", "--symbol", "AAPL", "--target-pct", "0.6"]))
    assert "超过股票目标" in json.loads(capfd.readouterr().out)["error"]
    with pytest.raises(SystemExit):
        sc.cmd_set_allocations(parser.parse_args(["set_allocations", "--target-pct", "AAPL"]))
    assert "SYMBOL=" in json.loads(capfd.readouterr().out)["error"]


def test_mcp_tools_pass_target_pct_through(store, monkeypatch):
    import openinvest.services.strategy_write as svc_mod
    from openinvest.connectors import mcp_server as m

    monkeypatch.setattr(svc_mod, "_store", lambda s: store if s is None else s)
    assert m.track_asset(symbol="510300.SS", target_pct=0.3)["status"] == "ok"
    assert m.set_allocations(target_pcts={"AAPL": 0.2, "FUND:110020": None})["status"] == "ok"
    assert _pcts(store) == {"510300.SS": 0.3, "AAPL": 0.2, "FUND:110020": None}
    bad = m.track_asset(symbol="AAPL", target_pct=0.5)                                          # 0.3 + 0.5 > 0.7
    assert bad["status"] == "error" and "80.0%" in bad["error"]
    # schema 层：target_pct / target_pcts 都出现在 MCP 工具的 inputSchema 里（agent 靠它发现参数）
    import asyncio
    tools = {t.name: t for t in asyncio.run(m.mcp.list_tools())}
    assert "target_pct" in tools["track_asset"].inputSchema["properties"]
    assert "target_pcts" in tools["set_allocations"].inputSchema["properties"]
    assert not tools["set_allocations"].inputSchema.get("required")                              # stock/cash 不再必填
