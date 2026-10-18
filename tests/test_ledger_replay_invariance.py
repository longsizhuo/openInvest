"""calc/ledger_replay.py —— 账本流水唯一读法的回放不变量（ADR-027 PR1：纯重构，行为逐字不变）。

oracle = PR1 之前 ``services/account_performance._ledger_events`` / 内联的"账本里有、持仓里没有的 symbol"循环 /
``core/decision_calc.history_as_trades`` 的**原样拷贝**（下面 ``_old_*``，不要"顺手修"——它们的价值就是冻结旧解释）。
守三件事：
1. 事件级：脱敏合成流水（覆盖全部历史 action × 两种价格字段形状）+ 种子随机流水，新旧事件逐字段相等；
2. NAV 级：同一份流水喂整条 ``build_account_performance``（旧事件+旧 symbol 登记 vs 新路径），日度 NAV 行与整份 perf 相等；
3. 决议匹配：``history_as_trades`` 新旧输出相等。
另守 Activity 形状 / LedgerState 数值 / "没人再自抄别名表"。
"""
from __future__ import annotations

import random
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pandas as pd
import pytest

from openinvest.calc.ledger_replay import external_flow, replay_ledger
from openinvest.core.decision_calc import history_as_trades
from openinvest.core.memory_store import MemoryStore
from openinvest.core.portfolio_manager import PortfolioManager
from openinvest.services import account_performance as ap

# ===================================================================================
# oracle：PR1 之前的旧实现，原样冻结
# ===================================================================================

_OLD_BUY_ACTIONS = {"buy", "bought", "dca_reconcile_backfill", "dca_reconcile_reverse", "gift"}
_OLD_SELL_ACTIONS = {"sell", "sold"}


def _old_ledger_events(records: List[Dict[str, Any]], alias: Dict[str, str]) -> Tuple[List[Dict[str, Any]], int]:
    events: List[Dict[str, Any]] = []
    skipped = 0
    units_from_zero: Dict[str, float] = defaultdict(float)
    for r in sorted(records, key=lambda x: (x.get("trade_date") or x.get("ts_origin") or x.get("ts") or "")[:10]):
        day = str(r.get("trade_date") or r.get("ts_origin") or r.get("ts") or "")[:10]
        action = str(r.get("action") or "").lower()
        if len(day) != 10:
            skipped += 1
            continue
        sym = alias.get(str(r.get("symbol") or ""), str(r.get("symbol") or ""))
        ccy = str(r.get("currency") or "CNY").upper()
        units = float(r.get("units") or 0)
        price = float(r.get("price") or r.get("price_per_unit") or 0)
        amount = float(r.get("total_amount") or abs(units) * price)
        ev: Dict[str, Any] = {"date": day, "symbol": sym, "units": 0.0, "cash_ccy": ccy, "cash": 0.0,
                              "flow_ccy": ccy, "flow": 0.0, "correction": False}
        if action in _OLD_BUY_ACTIONS:
            sign = 1.0 if units >= 0 else -1.0
            ev["units"] = units
            if r.get("funding_source") == "external_funding" or action == "gift":
                ev["flow"] = sign * amount
            else:
                ev["cash"] = -sign * amount
        elif action in _OLD_SELL_ACTIONS:
            ev["units"] = -abs(units)
            net = r.get("net_amount")
            ev["cash"] = float(net) if net is not None else amount - float(r.get("fee") or 0)
        elif action == "deposit":
            ev["cash"] = abs(units)
            ev["flow"] = abs(units)
        elif action == "withdraw":
            ev["cash"] = -abs(units)
            ev["flow"] = -abs(units)
        elif action == "manual_override":
            ev["units"] = units - units_from_zero[sym]
            ev["correction"] = True
        elif action == "delete_holding":
            ev["units"] = -units_from_zero[sym]
            ev["correction"] = True
        else:
            skipped += 1
            continue
        units_from_zero[sym] += ev["units"]
        events.append(ev)
    return events, skipped


def _old_register_ledger_symbols(by_sym: Dict[str, Dict[str, Any]], records: List[Dict[str, Any]],
                                 alias: Dict[str, str]) -> None:
    for r in sorted(records, key=lambda x: (x.get("trade_date") or x.get("ts_origin") or x.get("ts") or "")[:10]):
        sym = alias.get(str(r.get("symbol") or ""), str(r.get("symbol") or ""))
        if not sym or sym in by_sym or str(r.get("action") or "").lower() in ("deposit", "withdraw"):
            continue
        if sym == ap.GOLD_LEDGER_SYMBOL:
            by_sym[sym] = dict(ap._GOLD_LEDGER_HOLDING)
        else:
            by_sym[sym] = {"symbol": sym, "cost_currency": str(r.get("currency") or "CNY").upper(),
                           "_from_ledger": True}


_OLD_HISTORY_DIRECTION = {"buy": "BUY", "bought": "BUY", "sell": "SELL", "sold": "SELL"}
_OLD_AUTO_SOURCES = frozenset({"dca_daily"})


def _old_history_as_trades(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for i, r in enumerate(records):
        direction = _OLD_HISTORY_DIRECTION.get(str(r.get("action", "")).lower())
        source = str(r.get("source") or "")
        if not direction or ":existing_position" in source or source in _OLD_AUTO_SOURCES:
            continue
        out.append({
            "id": f"history:{i}", "ts": r.get("ts_origin") or r.get("ts"), "verdict_id": None,
            "symbol": r.get("symbol"), "direction": direction, "units": r.get("units"),
            "price": r.get("price"), "status": "executed",
        })
    return out


# ===================================================================================
# 脱敏合成流水：全部历史 action × 两种价格字段形状；文件顺序故意与日期顺序不一致
# ===================================================================================

def _ts(day: str, hh: str = "10:00:00") -> str:
    return f"{day}T{hh}"


def synthetic_ledger() -> List[Dict[str, Any]]:
    return [
        # 现金存入（PortfolioManager 形状：symbol=币种，units=金额）
        {"ts": _ts("2026-01-05", "09:00:01"), "ts_origin": _ts("2026-01-05", "09:00:00"), "action": "deposit",
         "symbol": "CNY", "units": 10000.0, "currency": "CNY", "source": "skill_cli"},
        # buy · price 形状 · 子弹池现金
        {"ts": _ts("2026-01-05", "09:30:01"), "ts_origin": _ts("2026-01-05", "09:30:00"), "action": "buy",
         "symbol": "AAA", "units": 100.0, "price": 10.0, "currency": "CNY", "source": "mcp",
         "funding_source": "cash_deduct"},
        # buy · 外部资金（定投）
        {"ts": _ts("2026-01-06", "07:30:01"), "ts_origin": _ts("2026-01-06", "07:30:00"), "action": "buy",
         "symbol": "AAA", "units": 50.0, "price": 10.5, "currency": "CNY", "source": "dca_daily",
         "funding_source": "external_funding"},
        # bought · price_per_unit+total_amount 形状（CommSec 邮件回灌，无 funding_source，AUD）
        {"ts": _ts("2026-01-06", "11:00:01"), "ts_origin": _ts("2026-01-06", "11:00:00"), "action": "bought",
         "symbol": "BBB.AX", "units": 20.0, "price_per_unit": 50.0, "total_amount": 1000.0, "fee": 0,
         "currency": "AUD", "channel": "commsec", "kind": "etf", "email_id": "e1"},
        # 对账：先漏记补入，再冲掉幻影（trade_date 回填到更早的日子，文件顺序在后）
        {"ts": _ts("2026-01-12", "20:00:01"), "ts_origin": _ts("2026-01-12", "20:00:00"), "trade_date": "2026-01-06",
         "action": "dca_reconcile_backfill", "symbol": "AAA", "units": 10.0, "price": 10.4, "currency": "CNY",
         "source": "reconcile_dca_phantom", "funding_source": "external_funding"},
        {"ts": _ts("2026-01-12", "20:00:02"), "ts_origin": _ts("2026-01-12", "20:00:01"), "trade_date": "2026-01-06",
         "action": "dca_reconcile_reverse", "symbol": "AAA", "units": -50.0, "price": 10.5, "currency": "CNY",
         "source": "reconcile_dca_phantom", "funding_source": "external_funding"},
        # 赠股
        {"ts": _ts("2026-01-07", "12:00:01"), "ts_origin": _ts("2026-01-07", "12:00:00"), "action": "gift",
         "symbol": "BBB.AX", "units": 0.5, "price_per_unit": 52.0, "total_amount": 26.0, "fee": 0,
         "currency": "AUD", "channel": "commsec", "kind": "etf"},
        # 黄金账本符号（GOLD-CNY → GC=F 持仓）
        {"ts": _ts("2026-01-07", "13:00:01"), "ts_origin": _ts("2026-01-07", "13:00:00"), "action": "bought",
         "symbol": "GOLD-CNY", "units": 10.0, "price_per_unit": 650.0, "total_amount": 6500.0, "currency": "CNY"},
        # sold · 带 net_amount / fee
        {"ts": _ts("2026-01-08", "10:00:01"), "ts_origin": _ts("2026-01-08", "10:00:00"), "action": "sold",
         "symbol": "BBB.AX", "units": 5.0, "price_per_unit": 55.0, "total_amount": 275.0, "fee": 2.0,
         "net_amount": 273.0, "currency": "AUD", "channel": "commsec"},
        # sell · price 形状 · 无 net_amount
        {"ts": _ts("2026-01-08", "14:00:01"), "ts_origin": _ts("2026-01-08", "14:00:00"), "action": "sell",
         "symbol": "AAA", "units": 20.0, "price": 11.0, "currency": "CNY", "source": "skill_cli"},
        # 已卖光 + delete_holding 的外币标的（持仓里已没有行）
        {"ts": _ts("2026-01-08", "15:00:01"), "ts_origin": _ts("2026-01-08", "15:00:00"), "action": "buy",
         "symbol": "DDD", "units": 10.0, "price": 5.0, "currency": "AUD", "source": "mcp",
         "funding_source": "cash_deduct"},
        # 取款两种符号形状：web_api 写正数，PortfolioManager 写负数
        {"ts": _ts("2026-01-09", "10:00:01"), "ts_origin": _ts("2026-01-09", "10:00:00"), "action": "withdraw",
         "symbol": "CNY", "units": 300.0, "currency": "CNY", "source": "web_api"},
        {"ts": _ts("2026-01-12", "10:00:01"), "ts_origin": _ts("2026-01-12", "10:00:00"), "action": "withdraw",
         "symbol": "CNY", "units": -200.0, "currency": "CNY", "source": "skill_cli"},
        # 周六记的存款（挂到下一交易日估值）
        {"ts": _ts("2026-01-10", "10:00:01"), "ts_origin": _ts("2026-01-10", "10:00:00"), "action": "deposit",
         "symbol": "CNY", "units": 500.0, "currency": "CNY", "source": "skill_cli"},
        # 手工修正：绝对值 30（此前 BBB.AX 回放持仓 = 20+0.5−5 = 15.5 → 增量 14.5）
        {"ts": _ts("2026-01-12", "09:00:01"), "ts_origin": _ts("2026-01-12", "09:00:00"), "action": "manual_override",
         "symbol": "BBB.AX", "units": 30, "price_per_unit": 54.0, "total_amount": 1620.0, "currency": "AUD",
         "source": "correction", "note": "补录漏掉的一笔"},
        {"ts": _ts("2026-01-13", "10:00:01"), "ts_origin": _ts("2026-01-13", "10:00:00"), "action": "sell",
         "symbol": "DDD", "units": 10.0, "price": 6.0, "currency": "AUD", "source": "mcp"},
        {"ts": _ts("2026-01-14", "10:00:01"), "ts_origin": _ts("2026-01-14", "10:00:00"), "action": "delete_holding",
         "symbol": "DDD", "source": "skill_cli"},
        # 大小写 / 小写币种 / int 数量 / 负数量 buy（旧实现的符号逻辑）
        {"ts": _ts("2026-01-15", "10:00:01"), "ts_origin": _ts("2026-01-15", "10:00:00"), "action": "BUY",
         "symbol": "AAA", "units": 4, "price": 12.0, "currency": "cny", "source": "mcp",
         "funding_source": "cash_deduct"},
        {"ts": _ts("2026-01-16", "10:00:01"), "ts_origin": _ts("2026-01-16", "10:00:00"), "action": "buy",
         "symbol": "AAA", "units": -2.0, "price": 12.5, "currency": "CNY", "source": "mcp",
         "funding_source": "cash_deduct"},
        # 无法识别的 action / 没有可用日期：保留、计数、不进回放
        {"ts": _ts("2026-01-09", "11:00:01"), "action": "frobnicate", "symbol": "ZZZ", "units": 1.0,
         "currency": "USD", "source": "future"},
        {"action": "buy", "symbol": "NODATE", "units": 1.0, "price": 1.0, "currency": "HKD"},
    ]


SYNTH_ALIAS = {"GOLD-CNY": "GC=F"}
SYNTH_PM_DAYS = [f"2026-01-{d:02d}" for d in (5, 6, 7, 8, 9, 12, 13, 14, 15, 16, 19, 20, 21, 22, 23)]


# ===================================================================================
# 1. 事件级
# ===================================================================================

def test_events_equal_oracle_on_synthetic_ledger():
    recs = synthetic_ledger()
    assert ap._ledger_events(recs, SYNTH_ALIAS) == _old_ledger_events(recs, SYNTH_ALIAS)
    assert ap._ledger_events(recs, {}) == _old_ledger_events(recs, {})
    events, skipped = ap._ledger_events(recs, SYNTH_ALIAS)
    assert skipped == 2 and len(events) == len(recs) - 2     # frobnicate + NODATE


def _random_records(rng: random.Random, n: int) -> List[Dict[str, Any]]:
    actions = ["buy", "bought", "sell", "sold", "deposit", "withdraw", "gift", "manual_override", "delete_holding",
               "dca_reconcile_backfill", "dca_reconcile_reverse", "BUY", "Sold", "dividend", "", None, "weird"]
    out = []
    for i in range(n):
        r: Dict[str, Any] = {"ts": f"2026-02-{rng.randint(1, 20):02d}T0{rng.randint(0, 9)}:00:00"}
        a = rng.choice(actions)
        if a is not None:
            r["action"] = a
        if rng.random() < 0.8:
            r["ts_origin"] = f"2026-02-{rng.randint(1, 20):02d}T1{rng.randint(0, 9)}:30:00"
        if rng.random() < 0.25:
            r["trade_date"] = f"2026-02-{rng.randint(1, 20):02d}"
        if rng.random() < 0.05:
            r.pop("ts"); r.pop("ts_origin", None); r.pop("trade_date", None)   # 无日期
        if rng.random() < 0.05:
            r["ts_origin"] = "bad"                                              # 日期长度不对
        if rng.random() < 0.9:
            r["symbol"] = rng.choice(["AAA", "BBB.AX", "GOLD-CNY", "CNY", "AUD", "DDD"])
        if rng.random() < 0.95:
            r["units"] = rng.choice([0, 1, 5, 12.5, -3, -7.25, 100.0, 0.003])
        shape = rng.random()
        if shape < 0.4:
            r["price"] = rng.choice([0.0, 1.5, 10.0, 99.99])
        elif shape < 0.8:
            r["price_per_unit"] = rng.choice([0.0, 2.5, 50.0])
            if rng.random() < 0.7:
                r["total_amount"] = rng.choice([0.0, 12.34, 1000.0, 275.0])
        if rng.random() < 0.3:
            r["total_amount"] = rng.choice([0.0, 33.3, 500.0])
        if rng.random() < 0.3:
            r["fee"] = rng.choice([None, 0, 1.5, 2.0])
        if rng.random() < 0.2:
            r["net_amount"] = rng.choice([None, 0.0, 99.5, 273.0])
        if rng.random() < 0.7:
            r["currency"] = rng.choice(["CNY", "AUD", "usd", "HKD", "", None])
        if rng.random() < 0.6:
            r["funding_source"] = rng.choice(["external_funding", "cash_deduct", None])
        if rng.random() < 0.7:
            # 不含 "dca_reconcile"：那是 replay 给对账行起的归一 source，真实 buy/sell 行不会写它
            r["source"] = rng.choice(["dca_daily", "mcp", "skill_cli", None, "mcp:existing_position",
                                      "reconcile_dca_phantom", "commsec", ""])
        out.append(r)
    return out


@pytest.mark.parametrize("seed", range(8))
def test_random_ledgers_events_and_trades_equal_oracle(seed):
    rng = random.Random(seed)
    recs = _random_records(rng, 300)
    for alias in ({}, SYNTH_ALIAS):
        assert ap._ledger_events(recs, alias) == _old_ledger_events(recs, alias)
    assert history_as_trades(recs) == _old_history_as_trades(recs)


@pytest.mark.parametrize("seed", range(3))
def test_random_ledgers_symbol_registration_equals_oracle(seed):
    recs = _random_records(random.Random(100 + seed), 200)
    old: Dict[str, Dict[str, Any]] = {"AAA": {"symbol": "AAA"}}
    new: Dict[str, Dict[str, Any]] = {"AAA": {"symbol": "AAA"}}
    _old_register_ledger_symbols(old, recs, SYNTH_ALIAS)
    ap._register_ledger_symbols(new, replay_ledger(recs, alias=SYNTH_ALIAS)[0])
    assert new == old


def test_history_as_trades_equals_oracle_on_synthetic_ledger():
    recs = synthetic_ledger()
    new = history_as_trades(recs)
    assert new == _old_history_as_trades(recs)
    # 对账 / 定投 / 赠股 / 修正都不是"执行了决议"；bought 行的 price 仍是 None（旧行为，改它是可见变化）
    assert [t["id"] for t in new] == [f"history:{i}" for i in (1, 3, 7, 8, 9, 10, 15, 17, 18, 20)]
    assert next(t for t in new if t["id"] == "history:3")["price"] is None


# ===================================================================================
# 2. NAV 级：整条 build_account_performance
# ===================================================================================

def _df(closes: Dict[str, float]) -> pd.DataFrame:
    df = pd.DataFrame({"Close": list(closes.values())}, index=pd.to_datetime(list(closes.keys())))
    df.index.name = "Date"
    return df


class _FakeStore:
    def __init__(self, table):
        self.table = table

    def get_history_df(self, symbol, days=730):
        return _df(self.table[symbol]) if symbol in self.table else pd.DataFrame()


def _prices(base: float, step: float) -> Dict[str, float]:
    return {d: round(base + step * i, 4) for i, d in enumerate(SYNTH_PM_DAYS)}


def _synthetic_pm(tmp_path, history) -> PortfolioManager:
    store = MemoryStore(tmp_path / "memory")
    store.write("user", "user", {"display_name": "T", "risk_tolerance": "Balanced"}, "")
    store.write("strategy", "strategy", {"target_assets": []}, "")
    store.write("portfolio", "state", {"schema_version": 2, "cash": {"CNY": 8000.0, "AUD": 150.0}, "holdings": [
        {"symbol": "AAA", "units": 140.0, "avg_cost": 10.2, "cost_currency": "CNY"},
        {"symbol": "BBB.AX", "units": 30.0, "avg_cost": 51.0, "cost_currency": "AUD"},
        {"symbol": "GC=F", "units": 10.0, "avg_cost": 650.0, "cost_currency": "CNY",
         "proxy_kind": "gold_cny_per_gram", "yfinance_proxy": "GC=F"},
    ]}, "")
    for h in history:
        store.append_history(h)
    return PortfolioManager(store)


def _run_perf(monkeypatch, tmp_path, history, *, old_path: bool):
    """跑整条 build_account_performance；old_path=True 时事件与 symbol 登记换成 PR1 之前的旧实现。
    返回 (perf, 送进 account_metrics 的日度 NAV 行 + 开盘 NAV)。"""
    monkeypatch.setattr(ap, "MarketStore", lambda: _FakeStore({
        "AAA": _prices(10.0, 0.1), "BBB.AX": _prices(50.0, 0.3), "DDD": _prices(5.0, 0.05),
        "GC=F": _prices(3110.35, 3.0), "USDCNY=X": _prices(7.0, 0.01), "AUDCNY=X": _prices(4.5, 0.02),
    }))
    monkeypatch.setattr(ap, "_benchmark_symbol", lambda: "")
    captured: Dict[str, Any] = {}
    real_metrics = ap.account_metrics

    def _spy(rows, opening_nav, *a, **kw):
        captured["rows"], captured["opening_nav"] = list(rows), opening_nav
        return real_metrics(rows, opening_nav, *a, **kw)

    monkeypatch.setattr(ap, "account_metrics", _spy)
    pm = _synthetic_pm(tmp_path, history)
    if old_path:
        alias = {ap.GOLD_LEDGER_SYMBOL: "GC=F"}
        monkeypatch.setattr(ap, "_events_of", lambda _acts: _old_ledger_events(pm.store.read_history(), alias)[0])
        monkeypatch.setattr(ap, "_register_ledger_symbols",
                            lambda by_sym, _acts: _old_register_ledger_symbols(by_sym, pm.store.read_history(), alias))
    perf = ap.build_account_performance(pm, today="2026-01-23", base="CNY")
    return perf, captured


def test_nav_series_and_perf_equal_old_path(monkeypatch, tmp_path):
    history = synthetic_ledger()
    old_perf, old_cap = _run_perf(monkeypatch, tmp_path / "old", history, old_path=True)
    new_perf, new_cap = _run_perf(monkeypatch, tmp_path / "new", history, old_path=False)
    assert old_perf["status"] == "ok" and new_perf["status"] == "ok"
    assert len(new_cap["rows"]) == len(SYNTH_PM_DAYS) and len(new_cap["rows"]) == len(old_cap["rows"])
    # 日度 NAV / 外部流入流出逐日相等，不是近似
    assert new_cap["rows"] == old_cap["rows"]
    assert new_cap["opening_nav"] == old_cap["opening_nav"]
    # 整份 perf（TWR / XIRR / 回撤 / data_quality：events 数、skipped、unreconciled 持仓与现金）逐字相等
    assert new_perf == old_perf
    # append_history 给每行补 ts，所以 NODATE 在这条链路上有日期（挂到末日）；只剩 frobnicate 被跳过
    assert new_perf["data_quality"]["skipped_events"] == 1
    # 流水确实在动 NAV 与外部流（不是两边都空转）
    assert any(r[2] for r in new_cap["rows"]) and any(r[3] for r in new_cap["rows"])
    assert new_perf["twr_all"] is not None and new_perf["data_quality"]["events"] == len(synthetic_ledger()) - 1


# ===================================================================================
# 3. Activity 形状 / LedgerState / external_flow
# ===================================================================================

def test_every_record_becomes_exactly_one_activity_in_date_order():
    recs = synthetic_ledger()
    acts, state = replay_ledger(recs, alias=SYNTH_ALIAS)
    assert len(acts) == len(recs) and sorted(a.seq for a in acts) == list(range(len(recs)))
    days = [a.trade_date or "" for a in acts]
    assert days == sorted(days)
    assert state.unknown == 2
    by_seq = {a.seq: a for a in acts}
    assert by_seq[19].action == "unknown" and by_seq[19].symbol == "ZZZ" and by_seq[19].raw["action"] == "frobnicate"
    assert by_seq[20].trade_date is None and by_seq[20].symbol == "NODATE"


def test_historical_actions_normalise_to_activity_fields():
    by_seq = {a.seq: a for a in replay_ledger(synthetic_ledger(), alias=SYNTH_ALIAS)[0]}
    deposit, buy, dca, bought, backfill, reverse, gift = (by_seq[i] for i in (0, 1, 2, 3, 4, 5, 6))
    assert (deposit.action, deposit.cash_effect, deposit.external, deposit.units, deposit.amount) == \
        ("deposit", 10000.0, True, None, 10000.0)
    assert (buy.action, buy.units_effect, buy.cash_effect, buy.external, buy.price, buy.amount) == \
        ("buy", 100.0, -1000.0, False, 10.0, 1000.0)
    assert (dca.cash_effect, dca.external, dca.source) == (0.0, True, "dca_daily")
    # bought → buy；price_per_unit / total_amount → price / amount；币种沿用
    assert (bought.action, bought.price, bought.amount, bought.cash_effect, bought.currency, bought.fee) == \
        ("buy", 50.0, 1000.0, -1000.0, "AUD", 0.0)
    # 对账行：source 归一为 dca_reconcile，external=True；冲回 = sell 且 units_effect<0
    assert (backfill.action, backfill.source, backfill.external, backfill.trade_date) == \
        ("buy", "dca_reconcile", True, "2026-01-06")
    assert (reverse.action, reverse.source, reverse.external, reverse.units_effect) == \
        ("sell", "dca_reconcile", True, -50.0)
    assert external_flow(backfill) == 104.0 and external_flow(reverse) == -525.0
    # gift 自成一类（ADR-027 D2），不并进 buy
    assert (gift.action, gift.external, gift.cash_effect, gift.units_effect) == ("gift", True, 0.0, 0.5)
    assert external_flow(gift) == 26.0 and external_flow(buy) == 0.0 and external_flow(deposit) == 10000.0
    # 黄金账本符号按 alias 落到持仓符号
    assert by_seq[7].symbol == "GC=F" and replay_ledger([synthetic_ledger()[7]], alias={})[0][0].symbol == "GOLD-CNY"
    # sold：units_effect 为负，cash = net_amount；sell 无 net_amount → 总额 − fee
    assert (by_seq[8].action, by_seq[8].units_effect, by_seq[8].cash_effect, by_seq[8].fee) == ("sell", -5.0, 273.0, 2.0)
    assert (by_seq[9].units_effect, by_seq[9].cash_effect) == (-20.0, 220.0)
    # 两种取款符号形状都归一成负的 cash_effect
    assert by_seq[11].cash_effect == -300.0 and by_seq[12].cash_effect == -200.0
    assert external_flow(by_seq[11]) == -300.0


def test_stateful_corrections_become_deltas():
    by_seq = {a.seq: a for a in replay_ledger(synthetic_ledger(), alias=SYNTH_ALIAS)[0]}
    mo, delete = by_seq[14], by_seq[16]
    # 回放到修正前 BBB.AX = 20 + 0.5 − 5 = 15.5；绝对值 30 → 增量 14.5
    assert (mo.action, mo.correction, mo.units, mo.units_effect, mo.cash_effect, mo.external) == \
        ("correction", True, 30.0, 14.5, 0.0, False)
    # DDD 买 10 卖 10 后 delete_holding：此刻回放持仓 0 → 增量 0
    assert (delete.action, delete.correction, delete.units_effect) == ("delete_holding", True, 0.0)
    assert external_flow(mo) == 0.0      # 修正的现金流在估值时按当日市价补，不在这里


def test_ledger_state_units_avg_cost_cash():
    recs = [
        {"ts": "2026-03-02T10:00:00", "action": "deposit", "symbol": "CNY", "units": 1000.0, "currency": "CNY"},
        {"ts": "2026-03-02T11:00:00", "action": "buy", "symbol": "AAA", "units": 10.0, "price": 10.0,
         "currency": "CNY", "funding_source": "cash_deduct"},
        {"ts": "2026-03-03T11:00:00", "action": "buy", "symbol": "AAA", "units": 10.0, "price": 20.0,
         "currency": "CNY", "funding_source": "external_funding"},
        {"ts": "2026-03-04T11:00:00", "action": "sell", "symbol": "AAA", "units": 5.0, "price": 30.0, "currency": "CNY"},
        {"ts": "2026-03-05T11:00:00", "action": "buy", "symbol": "BBB", "units": 1.0, "price": 7.0, "currency": "AUD",
         "funding_source": "cash_deduct"},
        {"ts": "2026-03-06T11:00:00", "action": "sell", "symbol": "BBB", "units": 1.0, "price": 8.0, "currency": "AUD"},
        {"ts": "2026-03-07T11:00:00", "action": "mystery", "symbol": "AAA"},
    ]
    _, state = replay_ledger(recs, alias={})
    assert state.units["AAA"] == 15.0 and state.units["BBB"] == 0.0
    assert state.avg_cost == {"AAA": 15.0}                # (10×10 + 10×20) / 20；BBB 清仓归零；卖出不改均价
    assert state.cash == {"CNY": 1000.0 - 100.0 + 150.0, "AUD": -7.0 + 8.0}   # external_funding 买入不动现金
    assert state.unknown == 1


def test_empty_and_replay_does_not_mutate_input():
    assert replay_ledger([], alias={})[0] == [] and replay_ledger([], alias={})[1].unknown == 0
    recs = synthetic_ledger()
    snapshot = [dict(r) for r in recs]
    replay_ledger(recs, alias=SYNTH_ALIAS)
    assert recs == snapshot


# ===================================================================================
# 4. 没人再自抄别名表
# ===================================================================================

# 写入侧 / 无关用途的合法出现处（读流水解释只许在 calc/ledger_replay.py）
_LEGACY_LITERAL_OK = {
    "calc/ledger_replay.py",
    "services/commsec_reader.py",    # 写 price_per_unit（CommSec 邮件 → 成交 dict）
    "core/portfolio_manager.py",     # record_external_trade 写入 bought/sold
    "jobs/dreaming.py",              # 叙事用的动词表，不是流水
    "jobs/dca_daily.py",             # job 返回的 status 字符串
}


def test_no_private_ledger_alias_tables_outside_replay():
    root = Path(__file__).resolve().parents[1] / "src" / "openinvest"
    pat = re.compile(r"""["'](bought|sold|manual_override|dca_reconcile_[a-z]+|price_per_unit)["']""")
    offenders = sorted(
        str(p.relative_to(root)) for p in root.rglob("*.py")
        if str(p.relative_to(root)) not in _LEGACY_LITERAL_OK and pat.search(p.read_text(encoding="utf-8"))
    )
    assert offenders == [], f"这些模块在自己解析老流水动作/字段——改读 calc.ledger_replay.replay_ledger: {offenders}"
