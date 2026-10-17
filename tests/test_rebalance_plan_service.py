"""services/rebalance_plan.py —— 临时 memory + 假行情 / 假汇率，不联网。

固定账户（基准币种 CNY，总资产 124,000）：
    现金 CNY 53,000 + USD 1,000（=7,000）                     60,000   目标现金 30%
    510300.SS  5,000 股 × 4.0                                  20,000   target_pct 30%
    AAPL       50 股 × 100 USD × 7                             35,000   target_pct 20%
    0700.HK    100 股 × 100 HKD × 0.9（不在 target_assets）     9,000
    FUND:110020 目标里有、没持仓，净值 2.0                          0      target_pct 20%
手算（hybrid）：多余现金 22,800；缺口 17,200 + 24,800 = 42,000；AAPL 超配 10,200 → 全卖，
一股 700 → 14 股 = 9,800；预算 32,600 按目标权重 .3:.2 分，510300 封顶 17,200（43 手），基金拿 15,400。
"""
from __future__ import annotations

import argparse
import json

import pytest

from openinvest.core.config import set_config_override
from openinvest.core.memory_store import MemoryStore
from openinvest.core.portfolio_manager import PortfolioManager
from openinvest.services import rebalance_plan as rp
from openinvest.utils import fx
from openinvest.utils.quotes import QuoteSnapshot

PRICES = {"510300.SS": 4.0, "AAPL": 100.0, "0700.HK": 100.0, "FUND:110020": 2.0}
CNY_PER = {"CNY": 1.0, "USD": 7.0, "HKD": 0.9}


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    prices = dict(PRICES)

    def fake_quote(h):
        p = prices.get(h["symbol"])
        ccy = "CNY" if h["symbol"].startswith("FUND:") else h["cost_currency"]    # 与真实 eastmoney 行情一致
        return None if p is None else QuoteSnapshot(h["symbol"], p, ccy, "share", "2026-10-16")

    def fake_rate(quote, base="CNY", as_of_date=None):
        q, b = quote.upper(), base.upper()
        if q == b:
            return 1.0
        return CNY_PER[q] / CNY_PER[b] if q in CNY_PER and b in CNY_PER else None

    monkeypatch.setattr(rp, "get_quote", fake_quote)
    monkeypatch.setattr(fx, "get_fx_rate", fake_rate)
    return prices


def _pm(tmp_path, *, target_pct=True, holdings=None, assets=None):
    def asset(sym, pct, **kw):
        return {"symbol": sym, "max_single_invest_cny": 10_000, **({"target_pct": pct} if target_pct else {}), **kw}

    store = MemoryStore(tmp_path / "memory")
    store.write("user", "user", {"display_name": "T", "risk_tolerance": "Balanced"}, "")
    store.write("strategy", "strategy", {
        "target_allocation_stock": 0.7, "target_allocation_cash": 0.3,
        "target_assets": assets if assets is not None else [
            asset("510300.SS", 0.30, display_name="沪深300ETF"), asset("AAPL", 0.20), asset("FUND:110020", 0.20)],
    }, "")
    store.write("portfolio", "state", {
        "schema_version": 2, "cash": {"CNY": 53_000.0, "USD": 1_000.0},
        "holdings": holdings if holdings is not None else [
            {"symbol": "510300.SS", "kind": "etf", "units": 5_000.0, "avg_cost": 4.0, "cost_currency": "CNY"},
            {"symbol": "AAPL", "kind": "equity", "units": 50.0, "avg_cost": 90.0, "cost_currency": "USD"},
            {"symbol": "0700.HK", "kind": "equity", "units": 100.0, "avg_cost": 90.0, "cost_currency": "HKD"},
        ]}, "")
    return PortfolioManager(store)


def _rows(plan):
    return {r["symbol"]: r for r in plan["rows"]}


def test_hybrid_plan_matches_hand_computation(tmp_path):
    plan = rp.build_rebalance_plan("hybrid", 5, pm=_pm(tmp_path))
    assert plan["status"] == "ok" and plan["base_currency"] == "CNY" and plan["total_value"] == 124_000.0
    r = _rows(plan)

    assert (r["AAPL"]["action"], r["AAPL"]["units"], r["AAPL"]["amount"]) == ("SELL", 14, 9_800.0)
    assert r["AAPL"]["currency"] == "USD" and r["AAPL"]["price"] == 100.0       # 价格是报价币种，金额是基准币种
    assert (r["AAPL"]["current_pct"], r["AAPL"]["target_pct"], r["AAPL"]["drift_pct"]) == (28.23, 20.0, 8.23)

    etf = r["510300.SS"]
    assert (etf["action"], etf["units"], etf["amount"]) == ("BUY", 4_300, 17_200.0)
    assert etf["units"] % 100 == 0 and etf["lot_size"] == 100                    # A 股整手
    assert etf["name"] == "沪深300ETF" and etf["target_source"] == "strategy"

    fund = r["FUND:110020"]                                                      # 没持仓的目标标的也给建议
    assert (fund["action"], fund["amount"], fund["units"]) == ("BUY", 15_400.0, 7_700.0)
    assert fund["lot_size"] is None and "资金不足" in fund["reason"]

    t = plan["totals"]
    assert (t["n_trades"], t["buy_total"], t["sell_total"], t["turnover"]) == (3, 32_600.0, 9_800.0, 42_400.0)
    assert t["cash_after"] == t["cash_before"] + t["sell_total"] - t["buy_total"] == 37_200.0   # 回到 30%
    assert plan["rows"][0]["action"] == "SELL"
    assert [u["symbol"] for u in plan["unmanaged"]] == ["0700.HK"] and plan["unmanaged"][0]["current_pct"] == 7.26
    assert "不下单" in plan["note"]


def test_modes_differ_in_selling(tmp_path):
    pm = _pm(tmp_path)
    cash_only = rp.build_rebalance_plan("cash_only", 5, pm=pm)
    assert cash_only["totals"]["sell_total"] == 0 and _rows(cash_only)["AAPL"]["action"] == "NONE"
    # 只花多余现金 22,800：按权重 .3:.2 分 13,680 / 9,120，ETF 取整到 34 手 = 13,600，零头留现金
    assert cash_only["totals"]["buy_total"] == 22_720.0 and cash_only["totals"]["cash_after"] == 37_280.0
    assert rp.build_rebalance_plan("sell_to_rebalance", 5, pm=pm)["totals"]["sell_total"] == 9_800.0


def test_band_widening_silences_everything(tmp_path):
    plan = rp.build_rebalance_plan("hybrid", 40, pm=_pm(tmp_path))
    assert plan["totals"]["n_trades"] == 0 and all("带宽内" in r["reason"] for r in plan["rows"])


def test_missing_target_pct_falls_back_to_equal_split_and_says_so(tmp_path):
    plan = rp.build_rebalance_plan("hybrid", 5, pm=_pm(tmp_path, target_pct=False))
    assert {r["target_source"] for r in plan["rows"]} == {"equal_split"}
    assert {r["target_pct"] for r in plan["rows"]} == {23.33}                     # 70% / 3


def test_invalid_target_pct_is_ignored_with_warning(tmp_path):
    assets = [{"symbol": "510300.SS", "max_single_invest_cny": 1, "target_pct": 30},   # 写成 30 而不是 0.3
              {"symbol": "AAPL", "max_single_invest_cny": 1, "target_pct": 0.2}]
    plan = rp.build_rebalance_plan("hybrid", 5, pm=_pm(tmp_path, assets=assets))
    assert any("target_pct" in w and "0~1" in w for w in plan["warnings"])
    assert _rows(plan)["510300.SS"]["target_source"] == "equal_split"


def test_quote_failure_degrades_to_cost_and_no_trade(tmp_path, _offline):
    _offline.pop("AAPL")      # 拉不到现价：总资产按成本价计，但不对它出数量
    plan = rp.build_rebalance_plan("sell_to_rebalance", 5, pm=_pm(tmp_path))
    aapl = _rows(plan)["AAPL"]
    assert aapl["action"] == "NONE" and aapl["units"] == 0
    assert any("AAPL" in w and "成本价" in w for w in plan["warnings"])


def test_unvalued_holding_is_dropped_not_zeroed(tmp_path, _offline):
    _offline.pop("510300.SS")      # 没现价、成本也是 0 → 估不出值：从清单剔除并说明，不当 0 算
    holdings = [{"symbol": "510300.SS", "kind": "etf", "units": 5_000.0, "avg_cost": 0.0, "cost_currency": "CNY"},
                {"symbol": "AAPL", "kind": "equity", "units": 50.0, "avg_cost": 90.0, "cost_currency": "USD"}]
    plan = rp.build_rebalance_plan("hybrid", 5, pm=_pm(tmp_path, holdings=holdings))
    assert "510300.SS" not in _rows(plan)
    assert any("510300.SS" in w and "剔除" in w for w in plan["warnings"])


def test_base_currency_is_respected(tmp_path, monkeypatch):
    monkeypatch.setitem(CNY_PER, "AUD", 4.7)
    set_config_override({"portfolio": {"base_currency": "aud"}})
    plan = rp.build_rebalance_plan("hybrid", 5, pm=_pm(tmp_path))
    assert plan["base_currency"] == "AUD" and plan["total_value"] == round(124_000 / 4.7, 2)
    r = _rows(plan)
    # 占比 / 数量与 CNY 口径一致，金额按 AUD 折算
    assert (r["AAPL"]["drift_pct"], r["AAPL"]["units"], r["510300.SS"]["units"]) == (8.23, 14, 4_300)
    assert r["510300.SS"]["amount"] == pytest.approx(17_200 / 4.7, abs=0.01)


def test_errors_are_structured_not_raised(tmp_path):
    pm = _pm(tmp_path)
    assert rp.build_rebalance_plan("yolo", 5, pm=pm)["status"] == "error"
    assert rp.build_rebalance_plan("hybrid", -1, pm=pm)["status"] == "error"
    assert rp.build_rebalance_plan("hybrid", 101, pm=pm)["status"] == "error"
    assert rp.build_rebalance_plan("hybrid", 5, pm=_pm(tmp_path, assets=[]))["status"] == "error"
    out = rp.build_rebalance_plan("hybrid", 5)       # 未初始化的 memory（conftest 把根目录指到空 tmp）
    assert out["status"] == "error" and "init" in out["hint"]


def test_cli_prints_json_and_exits_nonzero_on_error(monkeypatch, capfd):
    from openinvest import cli
    from openinvest.skill_cmds.strategy_cmds import cmd_rebalance_plan

    args = cli.build_parser().parse_args(["rebalance_plan", "--mode", "cash_only", "--band-pct", "3"])
    assert (args.mode, args.band_pct, args.func) == ("cash_only", 3.0, cmd_rebalance_plan)
    seen = {}
    monkeypatch.setattr(rp, "build_rebalance_plan", lambda **kw: seen.update(kw) or {"status": "ok", "rows": []})
    cmd_rebalance_plan(args)
    assert json.loads(capfd.readouterr().out) == {"status": "ok", "rows": []}
    assert seen == {"mode": "cash_only", "band_pct": 3.0}

    monkeypatch.setattr(rp, "build_rebalance_plan", lambda **kw: {"status": "error", "error": "x"})
    with pytest.raises(SystemExit) as e:
        cmd_rebalance_plan(argparse.Namespace(mode="hybrid", band_pct=5.0))
    assert e.value.code == 1
