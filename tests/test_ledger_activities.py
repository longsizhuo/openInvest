"""ADR-027 PR2：账本活动 dividend / interest / fee / tax / split / return_of_capital。临时 memory + 假行情库，不联网。

守的契约：
- 每种活动的快照效果（cash / units / avg_cost）+ 流水行（schema_version=2，cash_effect / units_effect / external 写入方算好）
- 幂等重放（ADR-016）：同 idempotency_key 只入账一次，含 20 路并发；DRIP 产生两条记录
- 回放：新动作进 Activity / LedgerState；split 换算拆股后口径；内部流（不进 TWR 分母）
- 估值：原始价 + 账本分红不双计；复权价回退时收入类事件不入账（PR1 规则）
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List

import pandas as pd
import pytest

import openinvest.db.market_store as ms_mod
from openinvest.calc.ledger_replay import later_split_factors, recent_income, replay_ledger
from openinvest.core.decision_calc import history_as_trades
from openinvest.core.memory_store import MemoryStore
from openinvest.core.portfolio_manager import PortfolioManager
from openinvest.services import account_performance as ap


def _pm(root: Path, cash=None, holdings=None, history: List[dict] = ()) -> PortfolioManager:
    s = MemoryStore(root)
    s.write("user", "user", {"display_name": "t", "risk_tolerance": "Balanced"}, "# user")
    s.write("strategy", "strategy", {"target_assets": []}, "# strategy")
    s.write("portfolio", "state", {
        "schema_version": 2, "cash": dict(cash if cash is not None else {"AUD": 100.0, "CNY": 1000.0}),
        "holdings": list(holdings if holdings is not None else [
            {"symbol": "NDQ.AX", "kind": "etf", "units": 100.0, "avg_cost": 50.0, "unit_label": "股",
             "cost_currency": "AUD", "proxy_kind": "direct"}]),
    }, "# portfolio")
    for h in history:
        s.append_history(h)
    return PortfolioManager(s)


def _rows(pm: PortfolioManager) -> List[dict]:
    return pm.store.read_history()


def _h(pm, symbol="NDQ.AX"):
    return pm.holdings.find(symbol)


# ============ dividend ============

def test_dividend_snapshot_effect_and_ledger_row(tmp_path):
    pm = _pm(tmp_path)
    out = pm.record_dividend("NDQ.AX", 30.0, "aud", tax_withheld=3.0, trade_date="2026-07-01", note="H2 distribution")
    assert out["status"] == "ok" and out["net_cash"] == 27.0 and out["new_balance"] == 127.0
    assert pm.cash_amount("AUD") == 127.0 and pm.cash_amount("CNY") == 1000.0
    assert _h(pm)["units"] == 100.0 and _h(pm)["avg_cost"] == 50.0           # 持仓数量 / 成本不变
    (row,) = _rows(pm)
    assert row["schema_version"] == 2 and row["action"] == "dividend" and row["external"] is False
    assert (row["amount"], row["tax_withheld"], row["cash_effect"], row["units_effect"]) == (30.0, 3.0, 27.0, 0.0)
    assert (row["symbol"], row["currency"], row["trade_date"], row["note"]) == ("NDQ.AX", "AUD", "2026-07-01", "H2 distribution")
    (a,), state = replay_ledger(_rows(pm), alias={})
    assert (a.action, a.cash_effect, a.amount, a.tax_withheld, a.external, a.units_effect) == ("dividend", 27.0, 30.0, 3.0, False, 0.0)
    assert a.replayable and state.cash == {"AUD": 27.0}


def test_dividend_validation_and_symbol_typo_guard(tmp_path):
    pm = _pm(tmp_path, history=[{"ts_origin": "2026-01-05T10:00:00", "action": "buy", "symbol": "SOLD.AX",
                                 "units": 1.0, "price": 1.0, "currency": "AUD"}])
    for kw in ({"gross": 0}, {"gross": -1}, {"gross": 10, "tax_withheld": 11}, {"gross": 10, "tax_withheld": -1},
               {"gross": 10, "trade_date": "2026/07/01"}, {"gross": 10, "currency": "A"}, {"gross": 10, "symbol": ""},
               {"gross": 10, "symbol": "NDQ.AY"}):          # 最后一个：既不持有、账本也没有 → 疑似手滑
        args = {"symbol": "NDQ.AX", "gross": 10, "currency": "AUD", **kw}
        with pytest.raises(ValueError):
            pm.record_dividend(args.pop("symbol"), args.pop("gross"), args.pop("currency"), **args)
    assert len(_rows(pm)) == 1 and pm.cash_amount("AUD") == 100.0     # 失败不落账
    out = pm.record_dividend("SOLD.AX", 5.0, "AUD")                   # 已清仓但账本里有：合法
    assert out["status"] == "ok" and "已无持仓" in out["note"]


def test_dividend_same_key_records_once(tmp_path):
    pm = _pm(tmp_path)
    r1 = pm.record_dividend("NDQ.AX", 10.0, "AUD", idempotency_key="d-1")
    r2 = pm.record_dividend("NDQ.AX", 10.0, "AUD", idempotency_key="d-1")
    assert r1["status"] == "ok" and r2["status"] == "duplicate"
    assert pm.cash_amount("AUD") == 110.0 and [r["action"] for r in _rows(pm)] == ["dividend"]
    assert _rows(pm)[0]["idempotency_key"] == "d-1"


def test_concurrent_same_key_exactly_one_dividend(tmp_path):
    root = tmp_path
    _pm(root)

    def go(_):
        return PortfolioManager(MemoryStore(root)).record_dividend("NDQ.AX", 10.0, "AUD", idempotency_key="race")["status"]

    with ThreadPoolExecutor(max_workers=20) as ex:
        got = list(ex.map(go, range(20)))
    assert got.count("ok") == 1 and got.count("duplicate") == 19
    pm = PortfolioManager(MemoryStore(root))
    assert pm.cash_amount("AUD") == 110.0 and len(_rows(pm)) == 1


# ============ DRIP ============

def test_drip_writes_dividend_and_buy_rows(tmp_path):
    pm = _pm(tmp_path, cash={"AUD": 0.0})
    out = pm.record_dividend("NDQ.AX", 105.0, "AUD", tax_withheld=5.0, reinvest=True, reinvest_price=20.0,
                             trade_date="2026-07-01", idempotency_key="drip-1")
    assert out["reinvested"] == {"units": 5.0, "price": 20.0, "cost": 100.0}
    assert pm.cash_amount("AUD") == 0.0                                  # 净额 100 全部再投资
    assert _h(pm)["units"] == 105.0
    assert _h(pm)["avg_cost"] == pytest.approx((100 * 50 + 100) / 105, abs=1e-6)
    div, buy = _rows(pm)                                                 # 两条记录
    assert (div["action"], div["cash_effect"], div["reinvest"]) == ("dividend", 100.0, True)
    assert (buy["action"], buy["source"], buy["external"], buy["schema_version"]) == ("buy", "drip", False, 2)
    assert (buy["units_effect"], buy["cash_effect"], buy["trade_date"]) == (5.0, -100.0, "2026-07-01")
    assert buy["idempotency_key"] == "drip-1#drip"
    acts, state = replay_ledger(_rows(pm), alias={})
    assert [a.action for a in acts] == ["dividend", "buy"] and not any(a.external for a in acts)
    assert state.units == {"NDQ.AX": 5.0} and state.cash["AUD"] == pytest.approx(0.0)
    assert history_as_trades(_rows(pm)) == []                           # DRIP 买入不是"执行了某条决议"
    # 同 key 重放：持仓 / 现金 / 两条记录都只有一份
    assert pm.record_dividend("NDQ.AX", 105.0, "AUD", tax_withheld=5.0, reinvest=True, reinvest_price=20.0,
                              idempotency_key="drip-1")["status"] == "duplicate"
    assert _h(pm)["units"] == 105.0 and len(_rows(pm)) == 2


def test_drip_default_price_is_stored_raw_close_and_errors_leave_no_trace(tmp_path, monkeypatch):
    monkeypatch.setattr(ms_mod, "DB_PATH", str(tmp_path / "market_data.db"))
    pm = _pm(tmp_path / "mem", cash={"AUD": 0.0})
    with pytest.raises(ValueError, match="reinvest 需要单价"):           # 行情库没有原始价
        pm.record_dividend("NDQ.AX", 100.0, "AUD", reinvest=True, trade_date="2026-07-01")
    store = ms_mod.MarketStore()
    store.save_generic_price("NDQ.AX", "2026-06-30", 24.0, close_raw=25.0)
    store.save_generic_price("NDQ.AX", "2026-05-01", 1.0, close_raw=1.0)    # 太老（> 7 天）不用
    store.conn.close()
    out = pm.record_dividend("NDQ.AX", 100.0, "AUD", reinvest=True, trade_date="2026-07-01")
    assert out["reinvested"]["price"] == 25.0 and out["reinvested"]["units"] == 4.0   # 用原始价 25，不是复权价 24
    # 业务错误不落账、不烧 key
    pm2 = _pm(tmp_path / "mem2", cash={"AUD": 0.0}, holdings=[
        {"symbol": "NDQ.AX", "kind": "etf", "units": 100.0, "avg_cost": 50.0, "cost_currency": "AUD", "proxy_kind": "direct"}])
    with pytest.raises(ValueError, match="同为 AUD"):
        pm2.record_dividend("NDQ.AX", 100.0, "USD", reinvest=True, reinvest_price=10.0, idempotency_key="k")
    pm3 = _pm(tmp_path / "mem3", cash={"AUD": 0.0}, holdings=[], history=[
        {"ts_origin": "2026-01-05T10:00:00", "action": "buy", "symbol": "NDQ.AX", "units": 1.0, "price": 1.0, "currency": "AUD"}])
    with pytest.raises(ValueError, match="没有持仓"):
        pm3.record_dividend("NDQ.AX", 100.0, "AUD", reinvest=True, reinvest_price=10.0, idempotency_key="k")
    for p_ in (pm2, pm3):
        assert p_.cash_amount("AUD") == 0.0 and "ledger_idempotency_keys" not in p_.store.read("portfolio").metadata
    assert [r["action"] for r in _rows(pm3)] == ["buy"]


# ============ interest / fee / tax ============

def test_interest_credits_cash_with_optional_symbol(tmp_path):
    pm = _pm(tmp_path)
    out = pm.record_interest(3.2, "cny", trade_date="2026-07-02", idempotency_key="i-1")
    assert out["new_balance"] == 1003.2
    (row,) = _rows(pm)
    assert row["action"] == "interest" and "symbol" not in row and row["cash_effect"] == 3.2 and row["external"] is False
    assert pm.record_interest(3.2, "CNY", idempotency_key="i-1")["status"] == "duplicate" and pm.cash_amount("CNY") == 1003.2
    pm.record_interest(1.0, "CNY", symbol="MMF1")
    assert _rows(pm)[-1]["symbol"] == "MMF1"
    with pytest.raises(ValueError):
        pm.record_interest(0, "CNY")


def test_fee_and_tax_debit_cash_and_guard_overdraft(tmp_path):
    pm = _pm(tmp_path)
    assert pm.record_fee(15.0, "AUD", note="account fee", idempotency_key="f-1")["new_balance"] == 85.0
    assert pm.record_fee(15.0, "AUD", idempotency_key="f-1")["status"] == "duplicate" and pm.cash_amount("AUD") == 85.0
    out = pm.record_fee(7.5, "AUD", symbol="NDQ.AX", kind="tax")
    assert out["action"] == "tax" and pm.cash_amount("AUD") == 77.5
    fee, tax = _rows(pm)
    assert (fee["action"], fee["cash_effect"], fee["amount"]) == ("fee", -15.0, 15.0)
    assert (tax["action"], tax["cash_effect"], tax["symbol"]) == ("tax", -7.5, "NDQ.AX")
    with pytest.raises(ValueError, match="现金"):
        pm.record_fee(1000.0, "AUD")                                  # 超过余额：和 withdraw 一样挡住
    with pytest.raises(ValueError):
        pm.record_fee(1.0, "AUD", kind="levy")
    assert len(_rows(pm)) == 2 and pm.cash_amount("AUD") == 77.5
    acts, _ = replay_ledger(_rows(pm), alias={})
    assert [(a.action, a.cash_effect, a.external) for a in acts] == [("fee", -15.0, False), ("tax", -7.5, False)]


# ============ split ============

def test_split_multiplies_units_divides_cost_keeps_cash(tmp_path):
    pm = _pm(tmp_path)
    out = pm.record_split("NDQ.AX", 10, trade_date="2026-06-10", idempotency_key="s-1")
    assert (out["units"], out["avg_cost"], out["units_effect"]) == (1000.0, 5.0, 900.0)
    assert pm.cash_amount("AUD") == 100.0
    (row,) = _rows(pm)
    assert (row["action"], row["split_ratio"], row["units_effect"], row["cash_effect"]) == ("split", 10.0, 900.0, 0.0)
    assert pm.record_split("NDQ.AX", 10, trade_date="2026-06-10", idempotency_key="s-1")["status"] == "duplicate"
    assert _h(pm)["units"] == 1000.0 and len(_rows(pm)) == 1
    pm.record_split("NDQ.AX", 0.1)                                    # 10 合 1
    assert (_h(pm)["units"], _h(pm)["avg_cost"]) == (100.0, 50.0)
    (a, _), state = replay_ledger(_rows(pm), alias={})
    assert (a.action, a.split_ratio, a.units_effect, a.cash_effect) == ("split", 10.0, 900.0, 0.0)
    assert state.units["NDQ.AX"] == pytest.approx(0.0)                # +900 −900 → 回放增量和


def test_split_validation(tmp_path):
    pm = _pm(tmp_path, history=[{"ts_origin": "2026-01-05T10:00:00", "action": "buy", "symbol": "GONE.AX",
                                 "units": 1.0, "price": 1.0, "currency": "AUD"}])
    for ratio in (0, -2, 1):
        with pytest.raises(ValueError):
            pm.record_split("NDQ.AX", ratio)
    with pytest.raises(ValueError, match="核对 symbol"):
        pm.record_split("NOPE", 2)
    out = pm.record_split("GONE.AX", 2, trade_date="2026-03-01")      # 已清仓：快照不变，只补流水（补记历史拆股用）
    assert out["units_effect"] == 0.0 and "已无持仓" in out["note"]
    assert _rows(pm)[-1]["action"] == "split" and _h(pm)["units"] == 100.0


# ============ return_of_capital ============

def test_return_of_capital_lowers_cost_not_units(tmp_path):
    pm = _pm(tmp_path, cash={"AUD": 0.0})
    out = pm.record_return_of_capital("NDQ.AX", 200.0, "AUD", idempotency_key="r-1")
    assert pm.cash_amount("AUD") == 200.0 and _h(pm)["units"] == 100.0
    assert _h(pm)["avg_cost"] == 48.0 and out["avg_cost"] == 48.0       # 50 − 200/100
    assert pm.record_return_of_capital("NDQ.AX", 200.0, "AUD", idempotency_key="r-1")["status"] == "duplicate"
    assert pm.cash_amount("AUD") == 200.0 and _h(pm)["avg_cost"] == 48.0
    (row,) = _rows(pm)
    assert (row["action"], row["amount"], row["cash_effect"], row["units_effect"], row["external"]) == ("return_of_capital", 200.0, 200.0, 0.0, False)
    out = pm.record_return_of_capital("NDQ.AX", 6000.0, "AUD")           # 超过剩余成本：成本归零，超出部分报出来
    assert _h(pm)["avg_cost"] == 0.0 and out["excess_over_cost"] == 1200.0
    with pytest.raises(ValueError, match="同为 AUD"):
        pm.record_return_of_capital("NDQ.AX", 1.0, "USD")
    with pytest.raises(ValueError, match="没有持仓"):
        pm.record_return_of_capital("NOPE", 1.0, "AUD")


# ============ 回放 ============

def test_replay_state_for_new_actions_and_legacy_rows_unchanged():
    recs = [
        {"ts_origin": "2026-01-05T10:00:00", "action": "buy", "symbol": "AAA", "units": 100.0, "price": 10.0, "currency": "CNY"},
        {"ts_origin": "2026-01-06T10:00:00", "schema_version": 2, "action": "return_of_capital", "symbol": "AAA",
         "amount": 100.0, "currency": "CNY", "cash_effect": 100.0, "trade_date": "2026-01-06"},
        {"ts_origin": "2026-01-07T10:00:00", "schema_version": 2, "action": "split", "symbol": "AAA",
         "split_ratio": 2.0, "units_effect": 100.0, "cash_effect": 0.0, "trade_date": "2026-01-07", "currency": "CNY"},
        {"ts_origin": "2026-01-08T10:00:00", "schema_version": 2, "action": "interest", "amount": 2.0, "currency": "CNY",
         "cash_effect": 2.0, "trade_date": "2026-01-08"},
        # 缺 cash_effect 的手写行：按 amount 推导
        {"ts_origin": "2026-01-09T10:00:00", "action": "dividend", "symbol": "AAA", "amount": 10.0, "tax_withheld": 1.0, "currency": "CNY"},
        {"ts_origin": "2026-01-09T11:00:00", "action": "tax", "amount": 4.0, "currency": "CNY"},
        {"ts_origin": "2026-01-10T10:00:00", "action": "split", "symbol": "AAA", "currency": "CNY"},          # 缺 split_ratio
        {"ts_origin": "2026-01-10T11:00:00", "action": "split", "symbol": "AAA", "split_ratio": 0, "currency": "CNY"},
    ]
    acts, state = replay_ledger(recs, alias={})
    by = {a.seq: a for a in acts}
    assert by[1].cash_effect == 100.0 and by[2].units_effect == 100.0 and by[3].symbol is None and by[3].cash_effect == 2.0
    assert by[4].cash_effect == 9.0 and by[5].cash_effect == -4.0           # 10 − 1 税 / −4
    assert by[6].unparsed and by[7].unparsed and state.unknown == 2          # 脏行不挡读方
    assert state.cash["CNY"] == pytest.approx(-1000 + 100 + 2 + 9 - 4)
    assert state.units["AAA"] == 200.0                                       # 100 + 拆股增量 100
    # avg_cost：10 →(ROC −100/100)→ 9 →(拆 2)→ 4.5
    assert state.avg_cost["AAA"] == pytest.approx(4.5)
    assert "" not in state.units                                             # 无 symbol 的利息 / 费用不污染持仓


def test_later_split_factors_restate_trades_to_post_split_basis():
    recs = [
        {"ts_origin": "2026-01-02T10:00:00", "action": "buy", "symbol": "AAA", "units": 10.0, "price": 100.0, "currency": "CNY"},
        {"ts_origin": "2026-01-03T10:00:00", "action": "buy", "symbol": "BBB", "units": 5.0, "price": 10.0, "currency": "CNY"},
        {"ts_origin": "2026-01-05T10:00:00", "schema_version": 2, "action": "split", "symbol": "AAA", "split_ratio": 10.0,
         "units_effect": 90.0, "trade_date": "2026-01-05", "currency": "CNY"},
        {"ts_origin": "2026-01-06T10:00:00", "action": "buy", "symbol": "AAA", "units": 100.0, "price": 10.0, "currency": "CNY"},
        {"ts_origin": "2026-01-07T10:00:00", "schema_version": 2, "action": "split", "symbol": "AAA", "split_ratio": 0.5,
         "units_effect": -100.0, "trade_date": "2026-01-07", "currency": "CNY"},
    ]
    acts, _ = replay_ledger(recs, alias={})
    f = later_split_factors(acts)
    assert f == {0: 5.0, 3: 0.5}      # 首笔买入：之后 ×10 再 ×0.5；拆股后的买入：只受 0.5 影响；BBB / 拆股本身不在表里


def test_recent_income_window():
    recs = [
        {"ts_origin": "2026-09-01T10:00:00", "schema_version": 2, "action": "dividend", "symbol": "A", "amount": 10, "cash_effect": 9,
         "currency": "AUD", "trade_date": "2026-09-01"},
        {"ts_origin": "2026-10-01T10:00:00", "schema_version": 2, "action": "dividend", "symbol": "A", "amount": 10, "cash_effect": 9,
         "currency": "AUD", "trade_date": "2026-10-01"},
        {"ts_origin": "2026-10-05T10:00:00", "schema_version": 2, "action": "interest", "amount": 2, "cash_effect": 2,
         "currency": "CNY", "trade_date": "2026-10-05"},
        {"ts_origin": "2026-10-06T10:00:00", "schema_version": 2, "action": "fee", "amount": 2, "cash_effect": -2,
         "currency": "CNY", "trade_date": "2026-10-06"},
    ]
    acts, _ = replay_ledger(recs, alias={})
    assert recent_income(acts, "2026-09-18") == {"dividend": {"AUD": 9.0}, "interest": {"CNY": 2.0}}
    assert recent_income(acts, "2026-11-01") == {}


def test_status_income_30d_line(tmp_path, monkeypatch):
    from datetime import date, timedelta

    from openinvest.services.skill_views import _income_30d

    pm = _pm(tmp_path)
    assert _income_30d(pm) is None                                      # 没有记录 → status 不出这个键
    today = date.today()
    pm.record_dividend("NDQ.AX", 30.0, "AUD", tax_withheld=3.0, trade_date=(today - timedelta(days=5)).isoformat())
    pm.record_dividend("NDQ.AX", 99.0, "AUD", trade_date=(today - timedelta(days=45)).isoformat())   # 窗口外
    pm.record_interest(3.2, "CNY", trade_date=today.isoformat())
    got = _income_30d(pm)
    assert got["dividend"] == {"AUD": 27.0} and got["interest"] == {"CNY": 3.2}
    assert got["line"] == "最近 30 天：分红（税后） AUD 27.00；利息 CNY 3.20"


# ============ 业绩口径：内部流 / 价格口径 / 拆股 ============

DAYS = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"]


def _df(closes: Dict[str, float]) -> pd.DataFrame:
    df = pd.DataFrame({"Close": list(closes.values())}, index=pd.to_datetime(list(closes.keys())))
    df.index.name = "Date"
    return df


class _PairStore:
    """table[sym] = 复权价序列，raw[sym] = {date: close_raw 或 None}（缺省 = 库里没有 close_raw）。"""

    def __init__(self, table, raw=None):
        self.table, self.raw = table, raw or {}

    def get_history_df(self, symbol, days=730):
        return _df(self.table[symbol]) if symbol in self.table else pd.DataFrame()

    def get_close_pair_df(self, symbol):
        if symbol not in self.table:
            return pd.DataFrame()
        df = _df(self.table[symbol])
        df["CloseRaw"] = pd.to_numeric([self.raw.get(symbol, {}).get(d) for d in self.table[symbol]])
        return df


@pytest.fixture(autouse=True)
def _no_benchmark(monkeypatch):
    monkeypatch.setattr(ap, "_benchmark_symbol", lambda: "")


def _perf(tmp_path, monkeypatch, table, raw, cash, holdings, history):
    monkeypatch.setattr(ap, "MarketStore", lambda: _PairStore(table, raw))
    pm = _pm(tmp_path, cash=cash, holdings=holdings, history=history)
    cap = {}
    orig = ap.account_metrics
    monkeypatch.setattr(ap, "account_metrics", lambda rows, o, t, **kw: (cap.update(rows=rows), orig(rows, o, t, **kw))[1])
    perf = ap.build_account_performance(pm, today="2026-01-09")
    return perf, cap["rows"]


AAA = {"symbol": "AAA", "units": 100.0, "avg_cost": 10.0, "cost_currency": "CNY"}
BUY_AAA = {"ts_origin": "2026-01-05T10:00:00", "action": "buy", "symbol": "AAA", "units": 100.0,
           "price": 10.0, "currency": "CNY", "funding_source": "external_funding"}


def _income(action, day, amount, **kw):
    return {"ts_origin": f"{day}T10:00:00", "schema_version": 2, "action": action, "amount": amount, "currency": "CNY",
            "trade_date": day, "external": False, "units_effect": 0.0, **kw}


def test_income_and_costs_are_internal_flows(tmp_path, monkeypatch):
    """dividend / interest / fee / tax 进 NAV、不进 TWR 分母（flow 全 0）；deposit 才是外部流入。"""
    px = dict(zip(DAYS, [10.0] * 5))
    history = [
        BUY_AAA,
        _income("dividend", "2026-01-06", 50.0, symbol="AAA", tax_withheld=5.0, cash_effect=45.0),
        _income("interest", "2026-01-07", 2.0, cash_effect=2.0),
        _income("fee", "2026-01-08", 3.0, cash_effect=-3.0),
        _income("tax", "2026-01-09", 1.0, symbol="AAA", cash_effect=-1.0),
    ]
    perf, rows = _perf(tmp_path, monkeypatch, {"AAA": px}, {"AAA": dict(px)}, {"CNY": 43.0}, [AAA], history)
    assert perf["price_basis"] == {"AAA": "raw"} and perf["data_quality"]["skipped_events"] == 0
    flows = {d: (i, o) for d, _, i, o in rows}
    assert flows["2026-01-05"] == (1000.0, 0.0)                         # 外部流入只有 external_funding 那笔
    assert all(flows[d] == (0.0, 0.0) for d in DAYS[1:])                # 分红 / 利息 / 费用 / 税：内部流
    navs = [round(n, 2) for _, n, *_ in rows]
    assert navs == [1000.0, 1045.0, 1047.0, 1044.0, 1043.0]            # 现金 0→45→47→44→43，持仓恒 1000
    assert perf["nav"] == 1043.0 and perf["twr_all"] == pytest.approx(4.3)   # 收益 = 分红 45 + 利息 2 − 费用 3 − 税 1


def test_dividend_is_not_double_counted_with_raw_prices(tmp_path, monkeypatch):
    """除息日原始价 10→9.5（分红 0.5/股 ×100 = 50），账本记现金 +50：NAV 连续、TWR=0。
    复权价把分红折进历史（恒 9.5），再记现金就是算两次——raw 基准下用原始价所以不会。"""
    adj = dict(zip(DAYS, [9.5] * 5))
    raw = dict(zip(DAYS, [10.0, 10.0, 9.5, 9.5, 9.5]))
    history = [BUY_AAA, _income("dividend", "2026-01-07", 50.0, symbol="AAA", cash_effect=50.0)]
    perf, rows = _perf(tmp_path, monkeypatch, {"AAA": adj}, {"AAA": raw}, {"CNY": 50.0}, [AAA], history)
    assert perf["price_basis"] == {"AAA": "raw"}
    assert [round(n, 2) for _, n, *_ in rows] == [1000.0] * 5
    assert perf["twr_all"] == 0.0 and "income_in_adjusted_price" not in perf["data_quality"]


def test_adjusted_fallback_skips_priced_in_income_instead_of_double_counting(tmp_path, monkeypatch):
    """close_raw 缺一天 → 整只回退复权价（PR1 规则）：分红 / DRIP 已含在价格里，不进回放，data_quality 报出来。"""
    adj = dict(zip(DAYS, [9.5] * 5))
    raw = {**dict(zip(DAYS, [10.0, 10.0, 9.5, 9.5, 9.5])), "2026-01-08": None}
    history = [BUY_AAA, _income("dividend", "2026-01-07", 50.0, symbol="AAA", cash_effect=50.0)]
    perf, rows = _perf(tmp_path, monkeypatch, {"AAA": adj}, {"AAA": raw}, {"CNY": 50.0}, [AAA], history)
    assert perf["price_basis"] == {"AAA": "adjusted"}
    assert perf["data_quality"]["income_in_adjusted_price"] == {"AAA": 1}
    assert [round(n, 2) for _, n, *_ in rows] == [1000.0] * 5            # 没有那笔 +50 的凭空跳升
    assert "未重复入账" in ap.render_performance_md(perf)
    # 与根本没记这笔分红的老账本逐位相同（= 回到 PR1 规则）
    base, base_rows = _perf(tmp_path / "base", monkeypatch, {"AAA": adj}, {"AAA": raw}, {"CNY": 50.0}, [AAA], [BUY_AAA])
    assert rows == base_rows and perf["twr_all"] == base["twr_all"]


def test_drip_on_adjusted_symbol_is_skipped_as_a_pair(tmp_path, monkeypatch):
    adj = dict(zip(DAYS, [10.0] * 5))
    history = [
        BUY_AAA,
        _income("dividend", "2026-01-07", 50.0, symbol="AAA", cash_effect=50.0, reinvest=True),
        {"ts_origin": "2026-01-07T10:00:01", "schema_version": 2, "action": "buy", "symbol": "AAA", "units": 5.0,
         "price": 10.0, "currency": "CNY", "source": "drip", "funding_source": "cash_deduct", "external": False,
         "cash_effect": -50.0, "units_effect": 5.0, "trade_date": "2026-01-07"},
    ]
    holding = {**AAA, "units": 105.0}
    perf, rows = _perf(tmp_path, monkeypatch, {"AAA": adj}, {}, {"CNY": 0.0}, [holding], history)
    assert perf["price_basis"] == {"AAA": "adjusted"} and perf["data_quality"]["income_in_adjusted_price"] == {"AAA": 2}
    assert perf["data_quality"]["unreconciled"]["units"] == {"AAA": 5.0}  # 这 5 份算成"账本之前就有"（复权价已含再投资）


def test_split_units_restated_to_post_split_basis(tmp_path, monkeypatch):
    """拆股前买 10 股 @100，除权日 10 拆 1 → 100 股。价格序列（复权价和 close_raw 都）已按拆股后口径：恒 10。
    数量必须全程拆股后口径：NAV 恒 1000，TWR 0；不换算的话拆股前 NAV 只有 100，拆股日凭空跳 ×10。"""
    px = dict(zip(DAYS, [10.0] * 5))
    history = [
        {"ts_origin": "2026-01-05T10:00:00", "action": "buy", "symbol": "AAA", "units": 10.0, "price": 100.0,
         "currency": "CNY", "funding_source": "cash_deduct"},
        {"ts_origin": "2026-01-07T10:00:00", "schema_version": 2, "action": "split", "symbol": "AAA", "split_ratio": 10.0,
         "units_effect": 90.0, "cash_effect": 0.0, "external": False, "currency": "CNY", "trade_date": "2026-01-07"},
    ]
    holding = {**AAA, "units": 100.0, "avg_cost": 10.0}
    for raw in ({"AAA": dict(px)}, {}):                                  # raw 基准 / 复权价回退，同一规则
        perf, rows = _perf(tmp_path / ("raw" if raw else "adj"), monkeypatch, {"AAA": px}, raw, {"CNY": 0.0}, [holding], history)
        assert perf["price_basis"] == {"AAA": "raw" if raw else "adjusted"}
        assert [round(n, 2) for _, n, *_ in rows] == [1000.0] * 5
        assert perf["twr_all"] == 0.0 and perf["max_drawdown"]["pct"] == 0.0
        assert perf["data_quality"]["unreconciled"]["units"] == {}
        assert all(i == 0 and o == 0 for _, _, i, o in rows)            # split 不是现金流


def test_ledger_without_new_activities_is_unaffected(tmp_path, monkeypatch):
    """没有任何新活动的老账本：数量 / 现金 / 流量与改前逐字相同（later_split_factors 空表）。"""
    px = dict(zip(DAYS, [10.0, 11.0, 11.0, 12.0, 12.0]))
    history = [{"ts_origin": "2026-01-05T10:00:00", "action": "buy", "symbol": "AAA", "units": 100.0, "price": 10.0,
                "currency": "CNY", "funding_source": "cash_deduct"}]
    perf, rows = _perf(tmp_path, monkeypatch, {"AAA": px}, {}, {"CNY": 0.0}, [AAA], history)
    assert [round(n, 2) for _, n, *_ in rows] == [1000.0, 1100.0, 1100.0, 1200.0, 1200.0]
    assert perf["price_basis"] == {"AAA": "adjusted"} and "income_in_adjusted_price" not in perf["data_quality"]


# ============ 入口：MCP / CLI ============

def test_mcp_tools_roundtrip(tmp_path):
    from openinvest.connectors import mcp_server as m

    _pm(tmp_path / "_memory_root")        # conftest 把 MemoryStore() 的默认目录指到这里
    ok = m.record_dividend(amount=30, currency="AUD", symbol="NDQ.AX", tax_withheld=3, trade_date="2026-07-01",
                           idempotency_key="m1")
    assert ok["status"] == "ok" and ok["net_cash"] == 27.0
    assert m.record_dividend(amount=30, currency="AUD", symbol="NDQ.AX", idempotency_key="m1")["status"] == "duplicate"
    assert m.record_dividend(amount=2, currency="CNY", kind="interest")["action"] == "interest"
    assert m.record_dividend(amount=2, currency="CNY", kind="interest", tax_withheld=1)["status"] == "error"
    assert m.record_dividend(amount=2, currency="CNY")["status"] == "error"          # 分红必须有 symbol
    assert m.record_fee(amount=1, currency="AUD", kind="tax")["action"] == "tax"
    assert m.record_fee(amount=1e6, currency="AUD")["status"] == "error"
    assert m.record_split(symbol="NDQ.AX", ratio=2)["units"] == 200.0
    assert m.record_split(symbol="NDQ.AX", ratio=1)["status"] == "error"
    assert m.record_return_of_capital(symbol="NDQ.AX", amount=10, currency="AUD")["status"] == "ok"
    rows = PortfolioManager(MemoryStore(tmp_path / "_memory_root")).store.read_history()
    assert [r["action"] for r in rows] == ["dividend", "interest", "tax", "split", "return_of_capital"]
    assert {r["source"] for r in rows} == {"mcp"}


def test_cli_subcommands_roundtrip(tmp_path, capfd):
    import json

    from openinvest import cli

    _pm(tmp_path / "_memory_root")
    parser = cli.build_parser()

    def run(*argv):
        args = parser.parse_args(list(argv))
        try:
            args.func(args)
            code = 0
        except SystemExit as e:
            code = e.code
        return code, json.loads(capfd.readouterr().out)

    code, out = run("record_dividend", "--symbol", "NDQ.AX", "--amount", "30", "-c", "AUD", "--tax-withheld", "3",
                    "--trade-date", "2026-07-01", "--idempotency-key", "c1")
    assert code == 0 and out["net_cash"] == 27.0
    assert run("record_dividend", "--symbol", "NDQ.AX", "--amount", "30", "-c", "AUD", "--idempotency-key", "c1")[1]["status"] == "duplicate"
    assert run("record_dividend", "--kind", "interest", "--amount", "2", "-c", "CNY")[1]["action"] == "interest"
    assert run("record_dividend", "--amount", "2", "-c", "CNY")[0] == 1               # 分红没给 symbol
    assert run("record_fee", "--kind", "tax", "--amount", "1", "-c", "AUD")[1]["action"] == "tax"
    assert run("record_split", "--symbol", "NDQ.AX", "--ratio", "2")[1]["units"] == 200.0
    assert run("record_split", "--symbol", "NDQ.AX", "--ratio", "1")[0] == 1
    assert run("record_return_of_capital", "--symbol", "NDQ.AX", "--amount", "10", "-c", "AUD")[1]["status"] == "ok"
    rows = PortfolioManager(MemoryStore(tmp_path / "_memory_root")).store.read_history()
    assert [r["action"] for r in rows] == ["dividend", "interest", "tax", "split", "return_of_capital"]
