"""ADR-027 PR3：场外基金两段式（下单扣现金记在途 → 定价日净值公布后按净值确认）。临时 memory + 假净值，不联网。

守的契约：
- place：现金 −amount、订单 placed 进 holding.extra.pending_orders、流水 fund_order_placed；总资产在这一步前后不变（在途按面值计入）
- confirm：units += (amount−fee)/nav、avg_cost 加权；CAS——并发两次只有一个生效，另一个 already_confirmed；
  净值未公布 → pending_nav，账本一个字节都不动；休市日顺延用之后第一个净值日
- cancel：只有 placed 能撤，已确认拒绝；幂等键重放 duplicate；删有在途单的持仓被拒
- 估值：fx.total_portfolio_value / account_performance 回放 / rebalance_plan / portfolio_summary / status 都经唯一读口
- job fund_confirm：只碰 nav_date ≤ 今天的单，净值没出就跳过，单笔失败不挡其余、批末抛
- 入口：MCP 三个工具 + CLI 子命令
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pandas as pd
import pytest

import openinvest.utils.eastmoney_fund as emf
from openinvest.calc.ledger_replay import replay_ledger
from openinvest.core.config import load_config, set_config_override
from openinvest.core.decision_calc import history_as_trades
from openinvest.core.memory_store import MemoryStore
from openinvest.core.portfolio_manager import PortfolioManager
from openinvest.jobs import fund_confirm
from openinvest.services import account_performance as ap
from openinvest.utils import fx

FUND = "FUND:123456"
MON, TUE, WED = "2026-01-05", "2026-01-06", "2026-01-07"           # 2026-01-05 是周一
ORDER_MON_AM = f"{MON}T10:00:00"                                   # 15:00 前 → T = 周一


@pytest.fixture
def net(monkeypatch):
    """东方财富假数据：profile（基金概况）与净值历史；记录调用，未联网。"""
    n = SimpleNamespace(profile={"基金简称": "测试混合A", "基金类型": "混合型-偏股"},
                        navs={FUND: [(MON, 2.0)]}, nav_calls=[], profile_calls=[])

    def profile(symbol, **kw):
        n.profile_calls.append(symbol)
        return n.profile

    def history(symbol, **kw):
        n.nav_calls.append(symbol)
        return n.navs.get(emf.canonical_fund_symbol(symbol))

    monkeypatch.setattr(emf, "fetch_fund_profile", profile)
    monkeypatch.setattr(emf, "fetch_fund_nav_history", history)
    return n


def _pm(root: Path, cash=None, holdings=()) -> PortfolioManager:
    s = MemoryStore(root)
    s.write("user", "user", {"display_name": "t", "risk_tolerance": "Balanced"}, "# user")
    s.write("strategy", "strategy", {"target_assets": []}, "# strategy")
    s.write("portfolio", "state", {
        "schema_version": 2, "cash": dict(cash if cash is not None else {"CNY": 10000.0}),
        "holdings": list(holdings),
    }, "# portfolio")
    return PortfolioManager(s)


def _fund(pm, symbol=FUND):
    return pm.holdings.find(symbol)


def _orders(pm) -> List[Dict[str, Any]]:
    return list((_fund(pm) or {}).get("extra", {}).get("pending_orders", []))


def _rows(pm, action=None):
    return [r for r in pm.store.read_history() if action is None or r["action"] == action]


def _total(pm, prices=None) -> float:
    return fx.total_portfolio_value_cny(pm, prices or {}, base="CNY")[0]


# ============ calc 层规则在 place 里的落地 ============

def test_place_deducts_cash_records_pending_and_ledger_row(tmp_path, net):
    pm = _pm(tmp_path)
    out = pm.place_fund_order("123456", 5000, "cny", order_ts=ORDER_MON_AM, note="周一定投", idempotency_key="k1")
    assert out["status"] == "ok" and out["symbol"] == FUND                       # 裸六位 → 规范化
    assert (out["order_date"], out["nav_date"], out["expected_confirm_date"]) == (MON, MON, TUE)
    assert (out["calendar"], out["fund_kind"], out["lag_days"], out["lag_source"]) == ("weekday_only", "default", 1, "profile")
    assert out["new_balance"] == 5000.0 and pm.cash_amount("CNY") == 5000.0
    h = _fund(pm)
    assert (h["kind"], h["units"], h["avg_cost"], h["cost_currency"], h["proxy_kind"]) == ("fund", 0.0, 0.0, "CNY", "eastmoney_fund")
    (o,) = _orders(pm)
    assert o["order_id"] == out["order_id"] and o["status"] == "placed" and o["amount"] == 5000.0
    assert (o["order_date"], o["nav_date"], o["expected_confirm_date"], o["note"]) == (MON, MON, TUE, "周一定投")
    (row,) = _rows(pm)
    assert row["schema_version"] == 2 and row["action"] == "fund_order_placed" and row["external"] is False
    assert (row["cash_effect"], row["units_effect"], row["amount"], row["trade_date"]) == (-5000.0, 0.0, 5000.0, MON)
    assert (row["settle_date"], row["nav_date"], row["order_id"], row["idempotency_key"]) == (TUE, MON, o["order_id"], "k1")
    (a,), state = replay_ledger(pm.store.read_history(), alias={})
    assert a.replayable and (a.action, a.cash_effect, a.units_effect, a.external) == ("fund_order_placed", -5000.0, 0.0, False)
    assert state.cash == {"CNY": -5000.0} and state.pending == {"CNY": 5000.0} and state.units == {}
    assert "在途申购" in (pm.store.root / "portfolio.md").read_text(encoding="utf-8")   # 人读的 body 里也看得见


def test_place_after_cutoff_weekend_and_fund_type_lag(tmp_path, net):
    pm = _pm(tmp_path)
    fri_pm = pm.place_fund_order(FUND, 100, order_ts="2026-01-09T16:00:00")      # 周五 15:00 后 → T=周一，+1 = 周二
    assert (fri_pm["nav_date"], fri_pm["expected_confirm_date"]) == ("2026-01-12", "2026-01-13")
    sat = pm.place_fund_order(FUND, 100, order_ts="2026-01-10T10:00:00")
    assert sat["nav_date"] == "2026-01-12"
    net.profile = {"基金全称": "某纳斯达克100联接基金(QDII)", "基金类型": "指数型-海外股票"}
    qdii = pm.place_fund_order(FUND, 100, order_ts=ORDER_MON_AM)
    assert (qdii["fund_kind"], qdii["lag_days"], qdii["expected_confirm_date"]) == ("qdii", 2, WED)
    net.profile = {"基金简称": "某全天候(FOF)A", "基金类型": "FOF-稳健型"}
    assert pm.place_fund_order(FUND, 100, order_ts=ORDER_MON_AM)["fund_kind"] == "fof"


def test_place_profile_unavailable_falls_back_to_default_and_says_so(tmp_path, net):
    net.profile = None
    pm = _pm(tmp_path)
    out = pm.place_fund_order(FUND, 100, order_ts=ORDER_MON_AM)
    assert (out["fund_kind"], out["lag_days"], out["lag_source"]) == ("default", 1, "default")
    assert "没取到" in out["note"]
    assert _rows(pm)[0]["lag_source"] == "default" and _orders(pm)[0]["lag_source"] == "default"


def test_confirm_lag_days_is_configurable(tmp_path, net):
    assert load_config().fund.confirm_lag_days == {"default": 1, "qdii": 2, "fof": 2}
    set_config_override({"fund": {"confirm_lag_days": {"qdii": 3}}})        # 只写想改的键，与默认深合并
    assert load_config().fund.confirm_lag_days == {"default": 1, "qdii": 3, "fof": 2}
    net.profile = {"基金简称": "某(QDII)A"}
    out = _pm(tmp_path).place_fund_order(FUND, 100, order_ts=ORDER_MON_AM)
    assert (out["lag_days"], out["expected_confirm_date"]) == (3, "2026-01-08")


def test_place_validation_rejects_without_touching_the_ledger(tmp_path, net):
    pm = _pm(tmp_path, cash={"CNY": 1000.0, "USD": 500.0})
    for kw in ({"symbol": "510300.SS"}, {"symbol": "AAPL"}, {"amount": 0}, {"amount": -5},
               {"amount": 1000.01}, {"currency": "USD"}, {"currency": "X"}, {"order_ts": "not-a-time"}):
        args = {"symbol": FUND, "amount": 100, "currency": "CNY", **kw}
        with pytest.raises(ValueError):
            pm.place_fund_order(args.pop("symbol"), args.pop("amount"), args.pop("currency"), **args)
    assert pm.cash_amount("CNY") == 1000.0 and pm.holdings.find(FUND) is None and _rows(pm) == []


def test_place_insufficient_cash_does_not_burn_the_key(tmp_path, net):
    pm = _pm(tmp_path, cash={"CNY": 100.0})
    with pytest.raises(ValueError, match="子弹不足"):
        pm.place_fund_order(FUND, 500, order_ts=ORDER_MON_AM, idempotency_key="k")
    pm.deposit_cash("CNY", 1000)
    assert pm.place_fund_order(FUND, 500, order_ts=ORDER_MON_AM, idempotency_key="k")["status"] == "ok"   # 同 key 仍可用


def test_place_into_existing_fund_row_and_tracking_only_flip(tmp_path, net):
    held = {"symbol": FUND, "kind": "fund", "units": 100.0, "avg_cost": 1.5, "cost_currency": "CNY",
            "proxy_kind": "eastmoney_fund", "is_tracking_only": True}
    pm = _pm(tmp_path, holdings=[held])
    pm.place_fund_order("FUND:123456", 300, order_ts=ORDER_MON_AM)
    h = _fund(pm)
    assert h["units"] == 100.0 and h["avg_cost"] == 1.5 and h["is_tracking_only"] is False   # 钱进去了，不再是追踪仓
    assert len(pm.holdings) == 1 and len(_orders(pm)) == 1
    # 老行用 .SZ 误标的写法（kind=fund）也认成同一只，不会建出第二行
    legacy = _pm(tmp_path / "b", holdings=[{"symbol": "123456.SZ", "kind": "fund", "units": 5.0, "avg_cost": 1.0,
                                            "cost_currency": "CNY"}])
    legacy.place_fund_order("FUND:123456", 100, order_ts=ORDER_MON_AM)
    assert [h["symbol"] for h in legacy.holdings] == ["123456.SZ"]
    assert _rows(legacy)[0]["symbol"] == "123456.SZ" and legacy.pending_fund_orders()[0]["symbol"] == "123456.SZ"
    legacy.confirm_fund_order(legacy.pending_fund_orders()[0]["order_id"])             # 确认时按基金代码取净值，不管行上写的后缀
    assert legacy.holdings.find("123456.SZ")["units"] == 55.0


def test_place_idempotency_key_replay_is_duplicate(tmp_path, net):
    pm = _pm(tmp_path)
    r1 = pm.place_fund_order(FUND, 1000, order_ts=ORDER_MON_AM, idempotency_key="p-1")
    r2 = pm.place_fund_order(FUND, 1000, order_ts=ORDER_MON_AM, idempotency_key="p-1")
    assert r1["status"] == "ok" and r2["status"] == "duplicate"
    assert pm.cash_amount("CNY") == 9000.0 and len(_orders(pm)) == 1 and len(_rows(pm)) == 1


def test_concurrent_same_key_places_exactly_one_order(tmp_path, net):
    _pm(tmp_path)

    def go(_):
        return PortfolioManager(MemoryStore(tmp_path)).place_fund_order(
            FUND, 1000, order_ts=ORDER_MON_AM, idempotency_key="race")["status"]

    with ThreadPoolExecutor(max_workers=20) as ex:
        got = list(ex.map(go, range(20)))
    assert got.count("ok") == 1 and got.count("duplicate") == 19
    pm = PortfolioManager(MemoryStore(tmp_path))
    assert pm.cash_amount("CNY") == 9000.0 and len(_orders(pm)) == 1 and len(_rows(pm)) == 1


# ============ 估值：总资产在 placed 前后不变 ============

def test_total_assets_unchanged_across_place_confirm_and_cancel(tmp_path, net):
    held = {"symbol": "AAA", "kind": "equity", "units": 100.0, "avg_cost": 10.0, "cost_currency": "CNY"}
    pm = _pm(tmp_path, holdings=[held])
    prices = {"AAA": 12.0, FUND: 2.0}
    before = _total(pm, prices)
    assert before == 10000.0 + 1200.0

    placed = pm.place_fund_order(FUND, 5000, order_ts=ORDER_MON_AM)
    assert pm.cash_amount("CNY") == 5000.0
    total, status = fx.total_portfolio_value_cny(pm, prices, base="CNY")
    assert total == before and status["pending_fund:CNY"] == "ok" and fx.unvalued(status) == []

    pm.confirm_fund_order(placed["order_id"], nav=2.0, confirm_date=MON)           # 5000/2.0 = 2500 份 × 2.0 = 5000
    assert _total(pm, prices) == before

    again = pm.place_fund_order(FUND, 1000, order_ts=f"{TUE}T10:00:00")
    assert _total(pm, prices) == before
    pm.cancel_fund_order(again["order_id"])
    assert _total(pm, prices) == before and pm.cash_amount("CNY") == 5000.0


def test_confirm_fee_is_the_only_thing_that_moves_total_assets(tmp_path, net):
    pm = _pm(tmp_path)
    o = pm.place_fund_order(FUND, 5000, order_ts=ORDER_MON_AM)
    assert _total(pm) == 10000.0
    pm.confirm_fund_order(o["order_id"], nav=2.0, fee=10.0)
    assert _fund(pm)["units"] == pytest.approx(2495.0)                              # (5000−10)/2
    assert _total(pm, {FUND: 2.0}) == pytest.approx(10000.0 - 10.0)                 # 申购费是成本，别的不变


def test_total_value_marks_unconvertible_pending_as_missing_fx_not_zero():
    usd = SimpleNamespace(cash={}, holdings=[],
                          pending_fund_orders=lambda: [{"currency": "USD", "amount": 100.0, "status": "placed"}])
    total, status = fx.total_portfolio_value(usd, {}, "CNY", rate_override={})
    assert total == 0.0 and status == {"pending_fund:USD": "missing_fx"} and fx.unvalued(status) == ["pending_fund:USD"]
    total2, status2 = fx.total_portfolio_value(usd, {}, "CNY", rate_override={"USD": 7.0})
    assert total2 == 700.0 and status2["pending_fund:USD"] == "ok"
    # 没有 pending_fund_orders 的替身（老测试 / 回测快照）= 没有在途，行为不变
    assert fx.total_portfolio_value(SimpleNamespace(cash={"CNY": 5.0}, holdings=[]), {}, "CNY")[0] == 5.0


# ============ confirm ============

def test_confirm_computes_units_weighted_cost_and_ledger_row(tmp_path, net):
    held = {"symbol": FUND, "kind": "fund", "units": 100.0, "avg_cost": 1.5, "cost_currency": "CNY",
            "proxy_kind": "eastmoney_fund", "unit_label": "份"}
    pm = _pm(tmp_path, holdings=[held])
    o = pm.place_fund_order(FUND, 5000, order_ts=ORDER_MON_AM)
    net.navs[FUND] = [(MON, 2.0), (TUE, 2.5)]
    out = pm.confirm_fund_order(o["order_id"], confirm_date=TUE)
    assert out["status"] == "ok" and out["nav"] == 2.0 and out["nav_date"] == MON       # 用定价日 T 的净值，不是最新净值
    assert out["units"] == 2500.0 and out["total_units"] == 2600.0
    h = _fund(pm)
    assert h["units"] == 2600.0
    assert h["avg_cost"] == pytest.approx((100 * 1.5 + 5000) / 2600, abs=1e-6)         # 成本含申购费（这里 0），按订单金额
    (order,) = _orders(pm)
    assert (order["status"], order["units"], order["nav"], order["confirmed_on"]) == ("confirmed", 2500.0, 2.0, TUE)
    row = _rows(pm, "fund_order_confirmed")[0]
    assert (row["cash_effect"], row["units_effect"], row["price"], row["amount"]) == (0.0, 2500.0, 2.0, 5000.0)
    assert (row["trade_date"], row["nav_date"], row["settle_date"], row["order_id"]) == (TUE, MON, TUE, o["order_id"])
    assert pm.cash_amount("CNY") == 5000.0 and pm.pending_fund_orders() == []
    (p, c), state = replay_ledger(pm.store.read_history(), alias={})
    assert (c.action, c.units_effect, c.cash_effect, c.external) == ("fund_order_confirmed", 2500.0, 0.0, False)
    assert state.units == {FUND: 2500.0} and state.pending == {"CNY": 0.0} and state.cash == {"CNY": -5000.0}
    assert state.avg_cost[FUND] == pytest.approx(2.0)


def test_confirm_with_explicit_nav_skips_the_network(tmp_path, net):
    pm = _pm(tmp_path)
    o = pm.place_fund_order(FUND, 1000, order_ts=ORDER_MON_AM)
    net.nav_calls.clear()
    out = pm.confirm_fund_order(o["order_id"], nav=2.5)
    assert out["units"] == 400.0 and net.nav_calls == []
    for bad in (0, -1, float("nan")):
        with pytest.raises(ValueError):
            pm.confirm_fund_order(o["order_id"], nav=bad)


def test_confirm_validates_fee_and_unknown_order(tmp_path, net):
    pm = _pm(tmp_path)
    o = pm.place_fund_order(FUND, 1000, order_ts=ORDER_MON_AM)
    with pytest.raises(ValueError):
        pm.confirm_fund_order(o["order_id"], nav=2.0, fee=-1)
    with pytest.raises(ValueError, match="必须小于"):
        pm.confirm_fund_order(o["order_id"], nav=2.0, fee=1000)
    with pytest.raises(ValueError, match="找不到在途单"):
        pm.confirm_fund_order("nope", nav=2.0)
    assert _orders(pm)[0]["status"] == "placed" and _fund(pm)["units"] == 0.0           # 失败不落账
    assert pm.cash_amount("CNY") == 9000.0 and len(_rows(pm)) == 1


def test_confirm_nav_not_published_leaves_every_byte_untouched(tmp_path, net):
    pm = _pm(tmp_path)
    o = pm.place_fund_order(FUND, 1000, order_ts=f"{TUE}T10:00:00")                 # T = 周二；库里最新净值只到周一
    path = pm.store.root / "portfolio.md"
    snapshot, history = path.read_text(encoding="utf-8"), (pm.store.root / "portfolio_history.jsonl").read_text()
    out = pm.confirm_fund_order(o["order_id"])
    assert out["status"] == "pending_nav" and out["reason"] == "nav_not_published" and out["nav_date"] == TUE
    assert path.read_text(encoding="utf-8") == snapshot                               # 连 updated 时间戳都没动
    assert (pm.store.root / "portfolio_history.jsonl").read_text() == history
    net.navs.pop(FUND)                                                                # 接口取不到（None）≠ 没公布
    assert pm.confirm_fund_order(o["order_id"])["reason"] == "nav_fetch_failed"
    assert path.read_text(encoding="utf-8") == snapshot
    net.navs[FUND] = [(MON, 2.0), (TUE, 2.2)]                                         # 公布了 → 下一次就确认
    assert pm.confirm_fund_order(o["order_id"])["status"] == "ok"


def test_confirm_shifts_to_next_nav_day_when_pricing_day_is_a_holiday(tmp_path, net):
    """交易日历只有周末：国庆这种休市日的 T 没有净值，但之后的净值已公布 → 顺延，并如实标出。"""
    pm = _pm(tmp_path)
    o = pm.place_fund_order(FUND, 1000, order_ts="2026-10-01T10:00:00")             # 周四，A 股休市
    assert o["nav_date"] == "2026-10-01"
    net.navs[FUND] = [("2026-09-30", 2.0), ("2026-10-08", 2.5)]
    out = pm.confirm_fund_order(o["order_id"])
    assert (out["nav_date"], out["nav_date_planned"], out["nav"], out["units"]) == ("2026-10-08", "2026-10-01", 2.5, 400.0)
    assert "休市" in out["note"] and _orders(pm)[0]["nav_date_planned"] == "2026-10-01"
    assert _rows(pm, "fund_order_confirmed")[0]["nav_date"] == "2026-10-08"


def test_confirm_cas_sequential_second_call_is_already_confirmed(tmp_path, net):
    pm = _pm(tmp_path)
    o = pm.place_fund_order(FUND, 1000, order_ts=ORDER_MON_AM)
    first = pm.confirm_fund_order(o["order_id"])
    path = pm.store.root / "portfolio.md"
    snapshot = path.read_text(encoding="utf-8")
    second = pm.confirm_fund_order(o["order_id"])
    assert first["status"] == "ok" and second["status"] == "already_confirmed"
    assert second["units"] == first["units"] and second["nav"] == 2.0
    assert path.read_text(encoding="utf-8") == snapshot and len(_rows(pm, "fund_order_confirmed")) == 1
    assert _fund(pm)["units"] == 500.0


def test_confirm_cas_inside_the_lock_when_another_process_wins_the_race(tmp_path, net):
    """A 先读到 placed、去取净值（锁外）；这期间 B 把单子确认了；A 进锁后读到 confirmed → already_confirmed，不二次入账。"""
    a = _pm(tmp_path)
    o = a.place_fund_order(FUND, 1000, order_ts=ORDER_MON_AM)
    b = PortfolioManager(MemoryStore(tmp_path))
    real = emf.fetch_fund_nav_history
    raced = []

    def history_then_b_confirms(symbol, **kw):
        if not raced:
            raced.append(b.confirm_fund_order(o["order_id"], nav=2.0, source="B"))
        return real(symbol, **kw)

    emf.fetch_fund_nav_history = history_then_b_confirms
    try:
        out = a.confirm_fund_order(o["order_id"])
    finally:
        emf.fetch_fund_nav_history = real
    assert raced[0]["status"] == "ok" and out["status"] == "already_confirmed"
    fresh = PortfolioManager(MemoryStore(tmp_path))
    assert _fund(fresh)["units"] == 500.0 and len(_rows(fresh, "fund_order_confirmed")) == 1


def test_confirm_concurrent_threads_exactly_one_wins(tmp_path, net):
    pm = _pm(tmp_path)
    oid = pm.place_fund_order(FUND, 1000, order_ts=ORDER_MON_AM)["order_id"]
    barrier = threading.Barrier(8)

    def go(_):
        mine = PortfolioManager(MemoryStore(tmp_path))
        barrier.wait()
        return mine.confirm_fund_order(oid)["status"]

    with ThreadPoolExecutor(max_workers=8) as ex:
        got = list(ex.map(go, range(8)))
    assert got.count("ok") == 1 and got.count("already_confirmed") == 7
    fresh = PortfolioManager(MemoryStore(tmp_path))
    assert _fund(fresh)["units"] == 500.0 and len(_rows(fresh, "fund_order_confirmed")) == 1


# ============ cancel ============

def test_cancel_only_placed_orders(tmp_path, net):
    pm = _pm(tmp_path)
    o = pm.place_fund_order(FUND, 1000, order_ts=ORDER_MON_AM)
    out = pm.cancel_fund_order(o["order_id"], cancel_date=MON)
    assert out["status"] == "ok" and out["new_balance"] == 10000.0
    assert _orders(pm)[0]["status"] == "cancelled" and pm.pending_fund_orders() == []
    row = _rows(pm, "fund_order_cancelled")[0]
    assert (row["cash_effect"], row["amount"], row["order_id"], row["trade_date"]) == (1000.0, 1000.0, o["order_id"], MON)
    again = pm.cancel_fund_order(o["order_id"])                                     # 幂等：已撤销原样返回
    assert again["status"] == "cancelled" and pm.cash_amount("CNY") == 10000.0 and len(_rows(pm, "fund_order_cancelled")) == 1
    assert pm.confirm_fund_order(o["order_id"])["status"] == "cancelled"            # 撤销后不能再确认
    _, state = replay_ledger(pm.store.read_history(), alias={})
    assert state.cash == {"CNY": 0.0} and state.pending == {"CNY": 0.0}

    done = pm.place_fund_order(FUND, 500, order_ts=ORDER_MON_AM)
    pm.confirm_fund_order(done["order_id"])
    with pytest.raises(ValueError, match="已确认"):
        pm.cancel_fund_order(done["order_id"])
    assert pm.cash_amount("CNY") == 9500.0 and _fund(pm)["units"] == 250.0
    with pytest.raises(ValueError, match="找不到在途单"):
        pm.cancel_fund_order("nope")


def test_delete_holding_refused_while_orders_are_in_transit(tmp_path, net):
    pm = _pm(tmp_path)
    o = pm.place_fund_order(FUND, 1000, order_ts=ORDER_MON_AM)
    for force in (False, True):
        with pytest.raises(ValueError, match="在途申购单"):
            pm.delete_holding(FUND, force=force)
    assert _total(pm) == 10000.0
    pm.cancel_fund_order(o["order_id"])
    assert pm.delete_holding(FUND)["status"] == "ok"                                   # 没有在途单了才能删（units=0）


def test_finished_orders_are_pruned_but_in_transit_ones_never(tmp_path, net):
    pm = _pm(tmp_path, cash={"CNY": 1_000_000.0})
    live = pm.place_fund_order(FUND, 10, order_ts=ORDER_MON_AM)["order_id"]
    for _ in range(25):
        pm.confirm_fund_order(pm.place_fund_order(FUND, 10, order_ts=ORDER_MON_AM)["order_id"], nav=2.0)
    orders = _orders(pm)
    assert sum(o["status"] == "placed" for o in orders) == 1 and orders[0]["order_id"] == live
    assert sum(o["status"] == "confirmed" for o in orders) == 20


# ============ buy 对场外基金的提醒 ============

def test_buy_warns_for_off_exchange_fund_but_still_books_it(tmp_path, net):
    pm = _pm(tmp_path)
    out = pm.buy(FUND, 100, 2.0, kind="fund", unit_label="份")
    assert out["status"] == "ok" and "place_fund_order" in out["warning"]
    assert pm.cash_amount("CNY") == 9800.0 and _fund(pm)["units"] == 100.0           # 不拒绝，照旧记账
    plain = pm.buy("AAA", 10, 2.0)
    assert "warning" not in plain
    assert "warning" not in pm.buy(FUND, 10, 2.0, kind="fund", source_type="external_funding")   # 外部新钱不扣现金，没有假跌问题
    assert "warning" not in _pm(tmp_path / "x").buy("510300.SS", 10, 4.0, kind="etf")             # 六位代码的 ETF 不是场外基金


# ============ 账本回放 / 决议匹配 ============

def test_replay_derives_effects_when_a_row_lacks_them():
    rows = [
        {"ts_origin": "2026-01-05T10:00:00", "action": "fund_order_placed", "symbol": FUND, "amount": 100.0, "order_id": "a"},
        {"ts_origin": "2026-01-06T10:00:00", "action": "fund_order_confirmed", "symbol": FUND, "amount": 100.0,
         "units": 50.0, "price": 2.0, "order_id": "a"},
        {"ts_origin": "2026-01-06T11:00:00", "action": "fund_order_placed", "symbol": FUND, "amount": 30.0, "order_id": "b"},
        {"ts_origin": "2026-01-07T11:00:00", "action": "fund_order_cancelled", "symbol": FUND, "amount": 30.0, "order_id": "b"},
    ]
    acts, state = replay_ledger(rows, alias={})
    assert [a.action for a in acts] == ["fund_order_placed", "fund_order_confirmed", "fund_order_placed", "fund_order_cancelled"]
    assert [a.cash_effect for a in acts] == [-100.0, 0.0, -30.0, 30.0] and not any(a.external for a in acts)
    assert state.cash == {"CNY": -100.0} and state.pending == {"CNY": 0.0}
    assert state.units == {FUND: 50.0} and state.avg_cost[FUND] == pytest.approx(2.0) and state.unknown == 0


def test_history_as_trades_counts_placed_orders_but_not_cancelled_ones(tmp_path, net):
    pm = _pm(tmp_path)
    kept = pm.place_fund_order(FUND, 100, order_ts=ORDER_MON_AM)
    dropped = pm.place_fund_order(FUND, 200, order_ts=ORDER_MON_AM)
    pm.cancel_fund_order(dropped["order_id"])
    pm.confirm_fund_order(kept["order_id"])
    trades = history_as_trades(pm.store.read_history())
    assert [(t["symbol"], t["direction"]) for t in trades] == [(FUND, "BUY")]          # 下单那一刻算执行；确认 / 取消不另算
    assert trades[0]["id"] == "history:0"


# ============ 账户业绩回放：placed 当日 NAV 不变，confirmed 当日用份额 × 净值替换在途面值 ============

DAYS = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"]


class _FakeStore:
    def __init__(self, table):
        self.table = table

    def get_history_df(self, symbol, days=730):
        if symbol not in self.table:
            return pd.DataFrame()
        df = pd.DataFrame({"Close": list(self.table[symbol].values())}, index=pd.to_datetime(list(self.table[symbol])))
        df.index.name = "Date"
        return df


def _replay_pm(tmp_path) -> PortfolioManager:
    """快照现金 10000，流水里只有 01-05 一笔 10000 的存款（外部流入）→ 开盘状态全 0，后面的场外基金单都从它出。"""
    pm = _pm(tmp_path, cash={"CNY": 10000.0})
    pm.store.append_history({"ts_origin": "2026-01-05T10:00:00", "action": "deposit", "symbol": "CNY",
                             "units": 10000.0, "currency": "CNY"})
    return pm


def _capture_rows(monkeypatch, prices):
    monkeypatch.setattr(ap, "MarketStore", lambda: _FakeStore({FUND: dict(zip(DAYS, prices))}))
    monkeypatch.setattr(ap, "_benchmark_symbol", lambda: "")
    got: Dict[str, Any] = {}
    monkeypatch.setattr(ap, "account_metrics", lambda rows, opening, today, **kw: got.update(rows=rows, opening=opening) or {})
    return got


def test_nav_replay_placed_day_unchanged_and_confirm_swaps_face_for_units(tmp_path, net, monkeypatch):
    got = _capture_rows(monkeypatch, [2.0, 2.0, 2.2, 2.2, 2.2])
    pm = _replay_pm(tmp_path)
    o = pm.place_fund_order(FUND, 5000, order_ts=f"{TUE}T10:00:00")
    pm.confirm_fund_order(o["order_id"], nav=2.0, confirm_date=WED)
    perf = ap.build_account_performance(pm, today="2026-01-09")
    nav = {d: n for d, n, *_ in got["rows"]}
    flows = {d: (i, o_) for d, _, i, o_ in got["rows"]}
    assert nav["2026-01-05"] == 10000.0
    assert nav["2026-01-06"] == 10000.0                      # placed 当日：现金 5000 + 在途面值 5000，NAV 不变
    assert nav["2026-01-07"] == 5000.0 + 2500 * 2.2          # confirmed 当日：份额 × 当日净值 替换在途面值
    assert nav["2026-01-09"] == _total(pm, {FUND: 2.2}) == 10500.0       # 序列末日 = status 同口径
    assert flows["2026-01-05"] == (10000.0, 0.0)
    assert flows["2026-01-06"] == (0.0, 0.0) and flows["2026-01-07"] == (0.0, 0.0)   # 内部流：不进 TWR 分母
    assert got["opening"] == 0.0 and perf["data_quality"]["unreconciled"] == {"units": {}, "cash": {}}


def test_nav_replay_unconfirmed_order_keeps_nav_flat_to_the_last_day(tmp_path, net, monkeypatch):
    got = _capture_rows(monkeypatch, [2.0] * 5)
    pm = _replay_pm(tmp_path)
    pm.place_fund_order(FUND, 4000, order_ts="2026-01-08T10:00:00")
    ap.build_account_performance(pm, today="2026-01-09")
    assert {n for _, n, *_ in got["rows"]} == {10000.0}      # 一直没确认 → 在途面值顶着，没有任何一天假跌


def test_nav_replay_cancelled_order_returns_to_cash(tmp_path, net, monkeypatch):
    got = _capture_rows(monkeypatch, [2.0] * 5)
    pm = _replay_pm(tmp_path)
    o = pm.place_fund_order(FUND, 4000, order_ts="2026-01-06T10:00:00")
    pm.cancel_fund_order(o["order_id"], cancel_date="2026-01-08")
    ap.build_account_performance(pm, today="2026-01-09")
    assert {n for _, n, *_ in got["rows"]} == {10000.0}


# ============ 下游读方：rebalance_plan / portfolio_summary / status ============

def test_portfolio_summary_names_the_in_transit_order(tmp_path, net):
    from openinvest.utils.portfolio_summary import portfolio_summary_text
    pm = _pm(tmp_path)
    assert "在途申购" not in portfolio_summary_text(pm, 10000.0, {})
    pm.place_fund_order(FUND, 5000, order_ts=ORDER_MON_AM)
    text = portfolio_summary_text(pm, 10000.0, {})
    assert f"在途申购 {FUND}" in text and "¥5,000" in text and "2026-01-06" in text and "已按面值含在总资产里" in text


def test_status_view_shows_in_transit_orders_and_total_does_not_dip(tmp_path, net, monkeypatch):
    from openinvest.services import skill_views
    from openinvest.utils.quotes import QuoteSnapshot
    monkeypatch.setattr("openinvest.services.account_performance.build_account_performance",
                        lambda pm=None, **kw: {"status": "insufficient_history"})
    monkeypatch.setattr("openinvest.utils.quotes.get_quote",
                        lambda h: QuoteSnapshot(h["symbol"], 2.0, "CNY", "份", MON, extra={}))
    s = MemoryStore()                                       # conftest 把默认数据根指到本测试的 tmp
    s.write("user", "user", {"display_name": "t", "risk_tolerance": "Balanced"}, "")
    s.write("strategy", "strategy", {"target_assets": []}, "")
    s.write("portfolio", "state", {"schema_version": 2, "cash": {"CNY": 10000.0}, "holdings": []}, "")
    pm = PortfolioManager()

    before = skill_views.build_status_view()
    assert "pending_fund_orders" not in before and "pending_fund_orders_line" not in before
    pm.place_fund_order(FUND, 5000, order_ts=ORDER_MON_AM)
    after = skill_views.build_status_view()
    assert after["total_assets_cny"] == before["total_assets_cny"] == 10000.0        # 中间那天不假跌
    (o,) = after["pending_fund_orders"]
    assert o["symbol"] == FUND and o["amount"] == 5000.0 and o["status"] == "placed" and o["expected_confirm_date"] == TUE
    assert after["pending_fund_orders_line"].startswith("在途待确认 1 笔（¥5,000.00），预计 2026-01-06")
    assert "total_assets_excludes" not in after
    pm.confirm_fund_order(o["order_id"], nav=2.0)
    done = skill_views.build_status_view()
    assert "pending_fund_orders" not in done and done["total_assets_cny"] == 10000.0
    assert {h["symbol"]: h["market_value"] for h in done["all_holdings"]} == {FUND: 5000.0}


def test_what_if_totals_include_in_transit_face_value(tmp_path, net, monkeypatch):
    from openinvest.services import skill_views
    from openinvest.utils import gold_price
    monkeypatch.setattr(skill_views, "_safe_close", lambda s: {"AAA": 12.0, FUND: 2.0}.get(s, 0.0))
    monkeypatch.setattr(gold_price, "get_gold_snapshot", lambda **k: None)
    s = MemoryStore()
    s.write("user", "user", {"display_name": "t", "risk_tolerance": "Balanced"}, "")
    s.write("strategy", "strategy", {"target_assets": []}, "")
    s.write("portfolio", "state", {"schema_version": 2, "cash": {"CNY": 10000.0}, "holdings": [
        {"symbol": "AAA", "kind": "equity", "units": 100.0, "avg_cost": 10.0, "cost_currency": "CNY"}]}, "")
    pm = PortfolioManager()
    before = skill_views.build_what_if_view(symbol="AAA", pct=-10)
    pm.place_fund_order(FUND, 5000, order_ts=ORDER_MON_AM)
    after = skill_views.build_what_if_view(symbol="AAA", pct=-10)
    assert before["current_total_cny"] == after["current_total_cny"] == 11200.0
    assert before["scenario_total_cny"] == after["scenario_total_cny"] == 11080.0
    assert after["breakdown"][f"pending:{FUND}"] == {"in_transit_fund_order": True, "cur_value_cny": 5000.0,
                                                     "scenario_value_cny": 5000.0, "delta_cny": 0.0}
    assert "excluded_from_total" not in after


# ============ job: fund_confirm ============

def test_job_only_touches_due_orders_and_skips_unpublished_nav(tmp_path, net):
    pm = _pm(tmp_path)
    due = pm.place_fund_order(FUND, 1000, order_ts=ORDER_MON_AM)["order_id"]
    later = pm.place_fund_order(FUND, 500, order_ts="2026-01-09T16:00:00")["order_id"]       # T = 下周一 01-12

    out = fund_confirm.run(today=MON, pm=pm)                                                 # 周一晚：只有第一笔到期，净值已公布
    assert (out["status"], out["confirmed"], out["not_due"], out["pending_nav"]) == ("success", 1, 1, 0)
    assert out["confirmed_orders"][0]["order_id"] == due and out["confirmed_orders"][0]["units"] == 500.0
    assert {o["order_id"] for o in pm.pending_fund_orders()} == {later}
    assert net.nav_calls == [FUND]                                                           # 没到期的那笔没去联网

    net.nav_calls.clear()
    out = fund_confirm.run(today="2026-01-09", pm=pm)                                        # 周五：第二笔还没到 T
    assert (out["confirmed"], out["not_due"]) == (0, 1) and net.nav_calls == []
    out = fund_confirm.run(today="2026-01-12", pm=pm)                                        # 到 T 了，但净值还没出 → 跳过，不是失败
    assert (out["confirmed"], out["pending_nav"]) == (0, 1) and len(pm.pending_fund_orders()) == 1
    net.navs[FUND] = [(MON, 2.0), ("2026-01-12", 2.5)]
    out = fund_confirm.run(today="2026-01-12", pm=pm)
    assert out["confirmed"] == 1 and pm.pending_fund_orders() == []
    assert _fund(pm)["units"] == 500.0 + 200.0
    rows = _rows(pm, "fund_order_confirmed")
    assert [r["source"] for r in rows] == ["fund_auto_confirm"] * 2
    assert fund_confirm.run(today="2026-01-12", pm=pm)["confirmed"] == 0                     # 重跑：没有在途单了，幂等


def test_job_rerun_and_manual_confirm_race_has_one_winner(tmp_path, net):
    pm = _pm(tmp_path)
    oid = pm.place_fund_order(FUND, 1000, order_ts=ORDER_MON_AM)["order_id"]
    stale = PortfolioManager(MemoryStore(tmp_path))                                          # job 拿着一份旧视图
    assert pm.confirm_fund_order(oid)["status"] == "ok"
    out = fund_confirm.run(today=MON, pm=stale)
    assert out["confirmed"] == 0 and out["already"] == 1 and _fund(pm)["units"] == 500.0


def test_job_failure_does_not_block_other_orders_and_raises_at_the_end(tmp_path, net):
    pm = _pm(tmp_path)
    bad = pm.place_fund_order(FUND, 1000, order_ts=ORDER_MON_AM)["order_id"]
    ok = pm.place_fund_order(FUND, 500, order_ts=ORDER_MON_AM)["order_id"]
    real = pm.confirm_fund_order

    def flaky(order_id, **kw):
        if order_id == bad:
            raise ValueError("账本坏了")
        return real(order_id, **kw)

    pm.confirm_fund_order = flaky
    with pytest.raises(RuntimeError, match="1 笔在途单确认失败") as exc:
        fund_confirm.run(today=MON, pm=pm)
    assert bad in str(exc.value) and "账本坏了" in str(exc.value)
    assert _orders(pm)[0]["status"] == "placed" and [o["order_id"] for o in pm.pending_fund_orders()] == [bad]
    assert _orders(pm)[1]["order_id"] == ok and _orders(pm)[1]["status"] == "confirmed"      # 另一笔照常确认了


def test_job_no_orders_and_uninitialised_memory_are_not_failures(tmp_path, net):
    assert fund_confirm.run(today=MON, pm=_pm(tmp_path))["confirmed"] == 0 and net.nav_calls == []
    assert fund_confirm.run(today=MON)["reason"] == "memory_not_initialized"                  # 默认数据根是空 tmp


def test_job_yml_is_discovered_with_a_sane_schedule_and_timeout():
    from openinvest.scheduler import runner
    from openinvest.scheduler.cron import crontab_trigger
    cfg = {c["name"]: c for c in runner._load_job_configs()}["fund_confirm"]
    assert cfg["enabled"] is True and cfg["entry"] == "openinvest.jobs.fund_confirm:run"
    assert runner._job_timeout_minutes(cfg) == 10
    assert callable(runner._resolve_entry(cfg["entry"]))
    from datetime import datetime
    from zoneinfo import ZoneInfo
    trig = crontab_trigger(cfg["schedule"], timezone=cfg["timezone"])
    now, fires = datetime(2026, 1, 9, 0, 0, 1, tzinfo=ZoneInfo("Asia/Shanghai")), []      # 周五
    for _ in range(8):
        now = trig.get_next_fire_time(None, now)
        fires.append(now)
        now = now.replace(second=1)
    assert {f.weekday() for f in fires} <= {0, 1, 2, 3, 4}                                  # 不跑周末
    assert {(f.hour, f.minute) for f in fires} >= {(21, 30), (22, 0), (22, 30), (23, 0)}    # 需求的 4 个点都在


# ============ rebalance_plan：在途金额算在这只基金头上 ============

def test_rebalance_plan_counts_in_transit_value_toward_the_fund(tmp_path, net, monkeypatch):
    from openinvest.services import rebalance_plan as rp
    from openinvest.utils.quotes import QuoteSnapshot
    monkeypatch.setattr(rp, "get_quote", lambda h: QuoteSnapshot(h["symbol"], 2.0, "CNY", "份", MON))
    pm = _pm(tmp_path, cash={"CNY": 10000.0})
    pm.store.write("strategy", "strategy", {
        "target_allocation_stock": 0.5, "target_allocation_cash": 0.5,
        "target_assets": [{"symbol": FUND, "target_pct": 0.5, "max_single_invest_cny": 10000}]}, "")
    pm = PortfolioManager(pm.store)
    flat = {r["symbol"]: r for r in rp.build_rebalance_plan("hybrid", 5, pm=pm)["rows"]}
    assert flat[FUND]["current_pct"] == 0.0 and flat[FUND]["action"] == "BUY"
    pm.place_fund_order(FUND, 5000, order_ts=ORDER_MON_AM)
    plan = rp.build_rebalance_plan("hybrid", 5, pm=pm)
    row = {r["symbol"]: r for r in plan["rows"]}[FUND]
    assert plan["total_value"] == 10000.0                                                    # 总资产没少
    assert row["current_pct"] == 50.0 and row["action"] == "NONE"                            # 刚下的单已经算它的仓位，不会建议再买


# ============ 入口：MCP / CLI ============

def test_mcp_tools_round_trip(tmp_path, net, monkeypatch):
    from openinvest.connectors import mcp_server as m
    pm = _pm(tmp_path)
    monkeypatch.setattr(m, "_pm", lambda: pm)
    placed = m.place_fund_order(FUND, 1000, order_ts=ORDER_MON_AM, idempotency_key="mcp-1")
    assert placed["status"] == "ok" and _rows(pm)[0]["source"] == "mcp"
    assert m.place_fund_order(FUND, 1000, order_ts=ORDER_MON_AM, idempotency_key="mcp-1")["status"] == "duplicate"
    oid = placed["order_id"]
    ok = m.confirm_fund_order(oid, fee=2.0)
    assert ok["status"] == "ok" and ok["units"] == 499.0
    assert m.confirm_fund_order(oid)["status"] == "already_confirmed"
    assert m.cancel_fund_order(oid)["status"] == "error"                                    # 已确认的撤销被拒（error dict，不抛）
    other = m.place_fund_order(FUND, 100, order_ts=ORDER_MON_AM)["order_id"]
    assert m.cancel_fund_order(other)["status"] == "ok" and m.cancel_fund_order(other)["status"] == "cancelled"
    assert m.place_fund_order("AAPL", 10)["status"] == "error"
    assert m.confirm_fund_order("nope")["status"] == "error"


def test_cli_subcommands_parse_and_dispatch(tmp_path, net, monkeypatch, capsys):
    from openinvest import cli
    from openinvest.skill_cmds import portfolio_cmds
    parser = cli.build_parser()
    args = parser.parse_args(["place_fund_order", "--symbol", FUND, "--amount", "800", "--order-ts", ORDER_MON_AM,
                              "--idempotency-key", "cli-1"])
    assert (args.symbol, args.amount, args.currency, args.order_ts, args.idempotency_key) == (FUND, 800.0, "CNY", ORDER_MON_AM, "cli-1")
    assert parser.parse_args(["confirm_fund_order", "--order-id", "x", "--nav", "2", "--fee", "1"]).fee == 1.0
    assert parser.parse_args(["cancel_fund_order", "--order-id", "x"]).order_id == "x"

    pm = _pm(tmp_path)
    monkeypatch.setattr(portfolio_cmds, "_resolve_pm", lambda: pm)
    printed: List[Dict[str, Any]] = []
    monkeypatch.setattr(portfolio_cmds, "_print_json", printed.append)
    portfolio_cmds.cmd_place_fund_order(args)
    oid = printed[-1]["order_id"]
    assert printed[-1]["status"] == "ok" and _rows(pm)[0]["source"] == "skill_cli"
    portfolio_cmds.cmd_confirm_fund_order(parser.parse_args(["confirm_fund_order", "--order-id", oid]))
    assert printed[-1]["status"] == "ok" and printed[-1]["units"] == 400.0
    with pytest.raises(SystemExit):                                                          # 出错：JSON error + exit 1，同 record_*
        portfolio_cmds.cmd_cancel_fund_order(parser.parse_args(["cancel_fund_order", "--order-id", oid]))
    assert printed[-1]["status"] == "error"
