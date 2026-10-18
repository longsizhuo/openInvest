"""每标的报警线（C8）：求值 / schema / 写入 / CLI 解析 / MCP 面 / status 可见性。

哨兵判定与冷却、总开关的集成测试在 test_price_sentinel.py::TestUserAlerts。
全部不联网：行情 patch 源模块属性。
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from openinvest.calc.price_alerts import describe, evaluate
from openinvest.core.memory_store import MemoryStore
from openinvest.core.schemas import SymbolAlerts, validate_strategy
from openinvest.services import strategy_write as svc
from openinvest.utils.quotes import QuoteSnapshot


# ---------- calc：求值与距离 ----------

def _by_rule(hits):
    return {h["rule"]: h for h in hits}


def test_evaluate_distance_and_breach_per_rule():
    hits = _by_rule(evaluate(
        {"price_above": 1100, "price_below": 900, "day_drop_pct": 3, "day_rise_pct": 4,
         "drawdown_from_cost_pct": 10},
        price=930.0, avg_cost=1000.0, day_change_pct=-0.8))
    # 价格线：距离 = 现价还要涨 / 跌多少 %（相对现价）
    assert hits["price_below"]["distance_pct"] == pytest.approx((930 - 900) / 930 * 100, abs=0.01)
    assert hits["price_below"]["text"] == "距 price_below 还有 3.2%"
    assert hits["price_above"]["distance_pct"] == pytest.approx((1100 - 930) / 930 * 100, abs=0.01)
    # 百分点规则：阈值 − 已走幅度
    assert hits["day_drop_pct"]["distance_pct"] == pytest.approx(2.2)
    assert hits["day_rise_pct"]["distance_pct"] == pytest.approx(4.8)
    assert hits["drawdown_from_cost_pct"]["current"] == pytest.approx(-7.0)
    assert hits["drawdown_from_cost_pct"]["distance_pct"] == pytest.approx(3.0)
    assert not any(h["breached"] for h in hits.values())


def test_evaluate_breach_each_rule():
    a = {"price_above": 1100, "price_below": 900, "day_drop_pct": 3, "day_rise_pct": 4, "drawdown_from_cost_pct": 10}
    down = _by_rule(evaluate(a, price=880.0, avg_cost=1000.0, day_change_pct=-3.4))
    assert {r for r, h in down.items() if h["breached"]} == {"price_below", "day_drop_pct", "drawdown_from_cost_pct"}
    assert down["day_drop_pct"]["text"] == "已触及 day_drop_pct 3（今日 -3.40%）"
    up = _by_rule(evaluate(a, price=1120.0, avg_cost=1000.0, day_change_pct=4.5))
    assert {r for r, h in up.items() if h["breached"]} == {"price_above", "day_rise_pct"}
    # 恰好等于阈值也算触及（≥ / ≤）
    assert _by_rule(evaluate({"price_below": 900}, price=900.0))["price_below"]["breached"] is True


def test_evaluate_missing_inputs_are_undecidable_not_false_alarm():
    hits = _by_rule(evaluate({"day_drop_pct": 3, "drawdown_from_cost_pct": 10, "price_below": 900},
                             price=50.0, avg_cost=None, day_change_pct=None))
    assert hits["day_drop_pct"]["distance_pct"] is None and not hits["day_drop_pct"]["breached"]
    assert hits["drawdown_from_cost_pct"]["distance_pct"] is None
    assert "暂无法判定" in hits["drawdown_from_cost_pct"]["text"]
    assert hits["price_below"]["breached"] is True  # 价格线不依赖成本 / 日涨跌
    # 价格本身拉不到：全部无法判定，绝不触发
    assert not any(h["breached"] for h in evaluate({"price_below": 900}, price=None))
    assert not any(h["breached"] for h in evaluate({"price_below": 900}, price=float("nan")))
    assert describe(evaluate({"price_above": 1}, price=None)[0]).endswith("（缺现价）")


# ---------- schema ----------

def test_schema_accepts_valid_and_rejects_bad_alerts():
    SymbolAlerts(price_below=900, price_above=1100, day_drop_pct=3, drawdown_from_cost_pct=10.5)
    for bad in (
        {"price_below": 0}, {"price_below": -1}, {"price_above": float("inf")},
        {"day_drop_pct": 0}, {"day_drop_pct": 101}, {"drawdown_from_cost_pct": 150},
        {"price_blow": 900},                      # 拼错的键不能静默放过
        {"price_below": 1100, "price_above": 900},  # 两条线反了
        {"price_below": 900, "price_above": 900},
    ):
        with pytest.raises(ValueError):
            SymbolAlerts(**bad)


def test_strategy_schema_validates_nested_alerts_and_stays_optional():
    base = {"target_allocation_stock": 0.7, "target_allocation_cash": 0.3}
    ok = validate_strategy({**base, "target_assets": [
        {"symbol": "GC=F", "max_single_invest_cny": 1, "alerts": {"price_below": 900}},
        {"symbol": "AAPL", "max_single_invest_cny": 1},  # 没设 alerts 照旧合法
    ]})
    assert ok["target_assets"][0]["alerts"] == {"price_below": 900}
    with pytest.raises(ValueError):
        validate_strategy({**base, "target_assets": [
            {"symbol": "GC=F", "max_single_invest_cny": 1, "alerts": {"day_drop_pct": -2}}]})


# ---------- 写入（strategy_write） ----------

@pytest.fixture
def store(tmp_path):
    s = MemoryStore(tmp_path / "memory")
    s.write("strategy", "strategy", {
        "target_allocation_stock": 0.7,
        "target_allocation_cash": 0.3,
        "max_single_invest_cny": 5000,
        "target_assets": [
            {"symbol": "NDQ.AX", "max_single_invest_cny": 10000},
            {"symbol": "GC=F", "max_single_invest_cny": 5000},
        ],
    }, "# body")
    return s


def _asset(store, sym):
    return next(a for a in store.read("strategy").metadata["target_assets"] if a["symbol"] == sym)


def test_upsert_writes_alerts_merges_and_new_symbol(store):
    svc.upsert_target_asset("GC=F", {"alerts": {"price_below": 900}}, store=store)
    svc.upsert_target_asset("GC=F", {"alerts": {"day_drop_pct": 2}}, store=store)  # 只加一条，不覆盖已有
    assert _asset(store, "GC=F")["alerts"] == {"price_below": 900, "day_drop_pct": 2}
    svc.upsert_target_asset("GC=F", {"alerts": {"price_below": 950}}, store=store)  # 同键覆盖
    assert _asset(store, "GC=F")["alerts"] == {"price_below": 950, "day_drop_pct": 2}
    # 新建一个标的并同时设线
    svc.upsert_target_asset("AAPL", {"alerts": {"price_above": 250}}, store=store)
    assert _asset(store, "AAPL")["alerts"] == {"price_above": 250}
    # 其它字段原样保留，body 不动
    assert _asset(store, "GC=F")["max_single_invest_cny"] == 5000
    assert store.read("strategy").body.strip() == "# body"


def test_upsert_rejects_bad_alerts_and_rolls_back(store):
    for bad in ({"price_blow": 1}, {"day_drop_pct": 0}, {"price_below": 1100, "price_above": 900}):
        with pytest.raises(ValueError, match="validation failed"):
            svc.upsert_target_asset("GC=F", {"alerts": bad}, store=store)
    assert "alerts" not in _asset(store, "GC=F")


def test_set_allocations_alerts_merge_clear_one_and_clear_all(store):
    svc.set_allocations(alerts={"GC=F": {"price_below": 900, "day_drop_pct": 2}, "NDQ.AX": {"price_above": 30}}, store=store)
    assert _asset(store, "GC=F")["alerts"] == {"price_below": 900, "day_drop_pct": 2}
    out = svc.set_allocations(alerts={"GC=F": {"price_below": None}}, store=store)  # null 清一条
    assert _asset(store, "GC=F")["alerts"] == {"day_drop_pct": 2}
    assert "alerts" in out["message"]
    svc.set_allocations(alerts={"GC=F": None, "NDQ.AX": {"price_above": None}}, store=store)  # 清空
    assert "alerts" not in _asset(store, "GC=F") and "alerts" not in _asset(store, "NDQ.AX")  # 不留 alerts: {}


def test_set_allocations_alerts_unknown_symbol_and_empty_call(store):
    with pytest.raises(svc.StrategyNotFound):
        svc.set_allocations(alerts={"NOPE": {"price_below": 1}}, store=store)
    with pytest.raises(ValueError, match="至少传"):
        svc.set_allocations(store=store)


# ---------- CLI ----------

def test_cli_parse_alerts():
    from openinvest.skill_cmds.strategy_cmds import _parse_alerts

    assert _parse_alerts(["GC=F:price_below=900", "GC=F:day_drop_pct=2.5", "0700.HK:price_above=none", "AAPL=none"]) == {
        "GC=F": {"price_below": 900.0, "day_drop_pct": 2.5},
        "0700.HK": {"price_above": None},
        "AAPL": None,
    }
    for bad in ("GC=F:price_below", "GC=F:price_below=abc", "GC=F=900", "=none"):
        with pytest.raises(ValueError):
            _parse_alerts([bad])
    with pytest.raises(ValueError, match="整体清空"):
        _parse_alerts(["AAPL=none", "AAPL:price_below=1"])


def test_cli_track_asset_and_set_allocations_write_alerts(store, monkeypatch, capfd):
    import json

    from openinvest import cli
    monkeypatch.setattr(svc, "_store", lambda s=None: store)
    p = cli.build_parser()
    a = p.parse_args(["track_asset", "--symbol", "GC=F", "--alert-price-below", "900", "--alert-day-drop-pct", "2"])
    a.func(a)
    assert json.loads(capfd.readouterr().out)["status"] == "ok"
    assert _asset(store, "GC=F")["alerts"] == {"price_below": 900.0, "day_drop_pct": 2.0}
    a = p.parse_args(["set_allocations", "--alert", "GC=F:price_below=none"])
    a.func(a)
    assert _asset(store, "GC=F")["alerts"] == {"day_drop_pct": 2.0}


# ---------- MCP ----------

def test_mcp_track_asset_and_set_allocations_expose_alerts(store, monkeypatch):
    from openinvest.connectors import mcp_server as m

    tools = {t.name: t for t in asyncio.run(m.mcp.list_tools())}
    props = tools["track_asset"].inputSchema["properties"]
    for k in ("alert_price_above", "alert_price_below", "alert_day_drop_pct",
              "alert_day_rise_pct", "alert_drawdown_from_cost_pct"):
        assert k in props, k
    assert "alerts" in tools["set_allocations"].inputSchema["properties"]
    assert "INVEST_EVENT_ALERT" in tools["track_asset"].description  # 默认关要让 agent 知道

    monkeypatch.setattr(svc, "_store", lambda s=None: store)
    out = m.track_asset("GC=F", alert_price_below=900, alert_day_drop_pct=2)
    assert out["status"] == "ok"
    assert _asset(store, "GC=F")["alerts"] == {"price_below": 900, "day_drop_pct": 2}
    out = m.set_allocations(alerts={"GC=F": {"price_below": None}})
    assert _asset(store, "GC=F")["alerts"] == {"day_drop_pct": 2}
    assert m.track_asset("GC=F", alert_price_below=1, alert_price_above=0.5)["status"] == "error"  # 两条线反了


# ---------- status 可见性 ----------

def _seed(holdings, target_assets):
    s = MemoryStore()
    s.write("user", "user", {"display_name": "U", "risk_tolerance": "Balanced"}, "")
    s.write("strategy", "strategy", {"target_allocation_stock": 0.7, "target_allocation_cash": 0.3,
                                      "target_assets": target_assets}, "")
    s.write("portfolio", "state", {"cash": {"CNY": 0.0}, "holdings": holdings, "schema_version": 2}, "")


def test_status_shows_alerts_with_distance_for_held_and_tracked_only(monkeypatch):
    from openinvest.services import skill_views

    monkeypatch.setattr("openinvest.services.account_performance.build_account_performance",
                        lambda pm=None, **kw: {"status": "insufficient_history"})
    _seed(
        [{"symbol": "GC=F", "kind": "metal", "units": 10.0, "avg_cost": 1000.0, "cost_currency": "CNY",
          "proxy_kind": "gold_cny_per_gram"},
         {"symbol": "510300.SS", "kind": "etf", "units": 100.0, "avg_cost": 4.0, "cost_currency": "CNY"}],
        [{"symbol": "GC=F", "max_single_invest_cny": 1, "alerts": {"price_below": 900, "drawdown_from_cost_pct": 10}},
         {"symbol": "510300.SS", "max_single_invest_cny": 1},
         {"symbol": "AAPL", "max_single_invest_cny": 1, "alerts": {"price_above": 250}}],  # 无持仓行
    )
    seen = []

    def fake_quote(h):
        seen.append((h["symbol"], h.get("proxy_kind")))
        price = {"GC=F": 930.0, "510300.SS": 4.5, "AAPL": 200.0}[h["symbol"]]
        return QuoteSnapshot(symbol=h["symbol"], price=price, currency="CNY", unit="u", extra={})
    monkeypatch.setattr("openinvest.utils.quotes.get_quote", fake_quote)

    rows = {h["symbol"]: h for h in skill_views.build_status_view()["all_holdings"]}

    gold = {a["rule"]: a for a in rows["GC=F"]["alerts"]}
    assert gold["price_below"]["text"] == "距 price_below 还有 3.2%"          # 与 get_quote 的克价口径同价
    assert gold["drawdown_from_cost_pct"]["distance_pct"] == pytest.approx(3.0)  # 930 vs 成本 1000 = -7%，线 -10%
    assert ("GC=F", "gold_cny_per_gram") in seen
    assert rows["510300.SS"]["alerts"] == []                                   # 没设线的持仓：字段在、为空
    aapl = rows["AAPL"]                                                        # 只跟踪未持仓：作为追踪仓行出现
    assert aapl["is_tracking_only"] is True and aapl["units"] == 0
    assert aapl["alerts"][0]["text"] == "距 price_above 还有 25.0%"
    assert ("AAPL", None) in seen  # 只对设了线的无持仓标的补拉价
