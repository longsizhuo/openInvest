"""services/account_performance.py —— 临时 memory + 假行情库，不联网。

守的契约：
- 序列末日 NAV = 当前 portfolio.md 按同口径估值（账本早于系统的部分进 unreconciled）
- deposit / external_funding 买入是外部现金流（不算收益）；buy(cash_deduct) 是内部转换
- 黄金 GOLD-CNY 账本符号映射到 proxy_kind=gold_cny_per_gram 的持仓，按 GC=F/31.1035×USDCNY 估值
- 外币持仓按行情库汇率逐日折算；历史不足 → insufficient_history
"""
from __future__ import annotations

from typing import Dict, List

import pandas as pd
import pytest

from openinvest.core.memory_store import MemoryStore
from openinvest.core.portfolio_manager import PortfolioManager
from openinvest.services import account_performance as ap

DAYS = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"]


def _df(closes: Dict[str, float]) -> pd.DataFrame:
    df = pd.DataFrame({"Close": list(closes.values())}, index=pd.to_datetime(list(closes.keys())))
    df.index.name = "Date"
    return df


class _FakeStore:
    def __init__(self, table: Dict[str, Dict[str, float]]):
        self.table = table

    def get_history_df(self, symbol, days=730):
        return _df(self.table[symbol]) if symbol in self.table else pd.DataFrame()


def _pm(tmp_path, cash, holdings, history: List[dict]) -> PortfolioManager:
    store = MemoryStore(tmp_path / "memory")
    store.write("user", "user", {"display_name": "T", "risk_tolerance": "Balanced"}, "")
    store.write("strategy", "strategy", {"target_assets": []}, "")
    store.write("portfolio", "state", {"schema_version": 2, "cash": cash, "holdings": holdings}, "")
    for h in history:
        store.append_history(h)
    return PortfolioManager(store)


@pytest.fixture(autouse=True)
def _no_benchmark(monkeypatch):
    monkeypatch.setattr(ap, "_benchmark_symbol", lambda: "")


def test_single_asset_cny_twr_and_flows(tmp_path, monkeypatch):
    """开仓 100 股@10 现金扣（内部），第 3 天存 500（外部），价格 10→11→11→12→12。"""
    monkeypatch.setattr(ap, "MarketStore", lambda: _FakeStore({
        "AAA": dict(zip(DAYS, [10.0, 11.0, 11.0, 12.0, 12.0])),
    }))
    pm = _pm(tmp_path, cash={"CNY": 500.0},
             holdings=[{"symbol": "AAA", "units": 100.0, "avg_cost": 10.0, "cost_currency": "CNY"}],
             history=[
                 {"ts_origin": "2026-01-05T10:00:00", "action": "buy", "symbol": "AAA", "units": 100.0,
                  "price": 10.0, "currency": "CNY", "funding_source": "cash_deduct"},
                 {"ts_origin": "2026-01-07T10:00:00", "action": "deposit", "symbol": "CNY", "units": 500.0,
                  "currency": "CNY"},
             ])
    perf = ap.build_account_performance(pm, today="2026-01-09")
    assert perf["status"] == "ok"
    # 当前现金 500 − 账本现金增量(−1000 + 500) = 开盘现金 1000；开盘持仓 0 → 开盘 NAV 1000
    assert perf["opening_nav"] == 1000.0
    assert perf["data_quality"]["unreconciled"] == {"units": {}, "cash": {"CNY": 1000.0}}
    # 末日 NAV = 500 现金 + 100 × 12 = 1700（= status 同口径）
    assert perf["nav"] == 1700.0
    # TWR：01-06 +10%（1000→1100），01-07 存 500 当天 0%，01-08 +100/1600 = 6.25%（现金稀释）
    # → 1.1 × 1.0625 − 1 = 16.875%；存款本身不算收益
    assert perf["twr_all"] == pytest.approx(16.88)
    assert perf["max_drawdown"]["pct"] == 0.0
    assert perf["xirr"] is None            # 跨度 4 天
    assert perf["data_quality"]["days"] == 5 and perf["data_quality"]["filled_days"] == 0


def test_external_funding_buy_is_inflow_not_return(tmp_path, monkeypatch):
    monkeypatch.setattr(ap, "MarketStore", lambda: _FakeStore({
        "AAA": dict(zip(DAYS, [10.0, 10.0, 10.0, 10.0, 10.0])),
    }))
    pm = _pm(tmp_path, cash={"CNY": 0.0},
             holdings=[{"symbol": "AAA", "units": 200.0, "avg_cost": 10.0, "cost_currency": "CNY"}],
             history=[
                 {"ts_origin": "2026-01-05T10:00:00", "action": "buy", "symbol": "AAA", "units": 100.0,
                  "price": 10.0, "currency": "CNY", "funding_source": "external_funding"},
                 {"ts_origin": "2026-01-07T10:00:00", "action": "buy", "symbol": "AAA", "units": 100.0,
                  "price": 10.0, "currency": "CNY", "funding_source": "external_funding"},
             ])
    perf = ap.build_account_performance(pm, today="2026-01-09")
    assert perf["opening_nav"] == 0.0 and perf["nav"] == 2000.0
    assert perf["twr_all"] == 0.0


def test_gold_ledger_symbol_maps_to_proxy_holding_and_fx_converts(tmp_path, monkeypatch):
    """GOLD-CNY 账本 → GC=F 持仓；AUD 持仓按 AUDCNY=X 折算；黄金克价 = GC=F/31.1035×USDCNY。"""
    monkeypatch.setattr(ap, "MarketStore", lambda: _FakeStore({
        "GC=F": dict(zip(DAYS, [3110.35] * 5)),          # USD/oz → 100 USD/g
        "USDCNY=X": dict(zip(DAYS, [7.0] * 5)),         # → 700 CNY/g
        "NDQ.AX": dict(zip(DAYS, [50.0, 50.0, 55.0, 55.0, 55.0])),
        "AUDCNY=X": dict(zip(DAYS, [4.0, 4.0, 4.0, 5.0, 5.0])),
    }))
    pm = _pm(tmp_path, cash={"CNY": 0.0, "AUD": 0.0},
             holdings=[
                 {"symbol": "GC=F", "units": 10.0, "avg_cost": 650.0, "cost_currency": "CNY",
                  "proxy_kind": "gold_cny_per_gram", "yfinance_proxy": "GC=F"},
                 {"symbol": "NDQ.AX", "units": 100.0, "avg_cost": 50.0, "cost_currency": "AUD"},
             ],
             history=[
                 {"ts_origin": "2026-01-05T10:00:00", "action": "bought", "symbol": "GOLD-CNY",
                  "units": 10.0, "price_per_unit": 650.0, "total_amount": 6500.0, "currency": "CNY"},
             ])
    perf = ap.build_account_performance(pm, today="2026-01-09")
    # 开盘：CNY 6500 现金（账本早于系统）+ 100 NDQ × 50 × 4.0 = 20000 → 26500
    assert perf["opening_nav"] == 26500.0
    assert perf["data_quality"]["unreconciled"]["units"] == {"NDQ.AX": 100.0}
    # 末日：10 g × 700 + 100 × 55 × 5.0 = 7000 + 27500 = 34500；无外部现金流 → TWR = 34500/26500 − 1
    assert perf["nav"] == 34500.0
    assert perf["twr_all"] == pytest.approx((34500 / 26500 - 1) * 100, abs=0.01)
    assert perf["data_quality"]["cost_fallback"] == [] and perf["data_quality"]["unvalued"] == []


def test_benchmark_from_price_path(tmp_path, monkeypatch):
    monkeypatch.setattr(ap, "MarketStore", lambda: _FakeStore({
        "AAA": dict(zip(DAYS, [10.0, 11.0, 11.0, 12.0, 12.0])),
    }))
    monkeypatch.setattr(ap, "_benchmark_symbol", lambda: "BENCH")
    seen = []
    monkeypatch.setattr(ap, "get_history_data",
                        lambda s, p="2y", **kw: (seen.append((s, p)), _df({"2026-01-02": 100.0, "2026-01-09": 110.0}))[1])
    pm = _pm(tmp_path, cash={"CNY": 0.0},
             holdings=[{"symbol": "AAA", "units": 100.0, "avg_cost": 10.0, "cost_currency": "CNY"}],
             history=[{"ts_origin": "2026-01-05T10:00:00", "action": "buy", "symbol": "AAA", "units": 100.0,
                       "price": 10.0, "currency": "CNY", "funding_source": "external_funding"}])
    perf = ap.build_account_performance(pm, today="2026-01-09")
    assert seen == [("BENCH", "max")]
    assert perf["benchmark"]["symbol"] == "BENCH"
    assert perf["benchmark"]["return_same_window"] == pytest.approx(10.0)
    assert perf["benchmark"]["excess"] == pytest.approx(20.0 - 10.0, abs=0.01)


def test_insufficient_history(tmp_path, monkeypatch):
    monkeypatch.setattr(ap, "MarketStore", lambda: _FakeStore({"AAA": {"2026-01-05": 10.0}}))
    pm = _pm(tmp_path, cash={"CNY": 0.0},
             holdings=[{"symbol": "AAA", "units": 1.0, "avg_cost": 10.0, "cost_currency": "CNY"}],
             history=[{"ts_origin": "2026-01-05T10:00:00", "action": "buy", "symbol": "AAA", "units": 1.0,
                       "price": 10.0, "currency": "CNY", "funding_source": "external_funding"}])
    assert ap.build_account_performance(pm, today="2026-01-09")["status"] == "insufficient_history"
    # 没有账本
    pm2 = _pm(tmp_path / "b", cash={"CNY": 1.0}, holdings=[], history=[])
    assert ap.build_account_performance(pm2, today="2026-01-09") == {
        "status": "insufficient_history", "reason": "no_ledger", "base_currency": "CNY"}


def test_render_performance_md_lines():
    md = ap.render_performance_md({
        "status": "ok", "since": "2026-01-05", "twr_30d": None, "twr_ytd": 1.5, "twr_all": 1.5,
        "xirr": None, "max_drawdown": {"pct": -2.0, "peak": "2026-01-06", "trough": "2026-01-08"},
        "volatility_annual": 9.9,
        "benchmark": {"symbol": "510300.SS", "return_same_window": 0.5, "excess": 1.0, "beat_days_pct": 60.0},
        "data_quality": {"days": 5, "filled_days": 1, "cost_fallback": [], "unvalued": [],
                         "unreconciled": {"units": {}, "cash": {}}},
    })
    assert "30 天 n/a" in md and "今年 +1.50%" in md and "XIRR" in md and "< 30 天" in md
    assert "-2.00%（2026-01-06 → 2026-01-08）" in md and "510300.SS" in md and "超额 +1.00%" in md
    assert "1 天缺价前值填充" in md
    assert "历史不足" in ap.render_performance_md({"status": "insufficient_history", "reason": "no_ledger"})


def test_status_view_carries_performance(tmp_path, monkeypatch):
    """status["performance"] 与 performance 子命令同一块；业绩失败不阻断 status。"""
    from openinvest.services import skill_views
    monkeypatch.setattr(skill_views, "_safe_close", lambda s: 0.0)
    monkeypatch.setattr("openinvest.utils.fx.get_fx_rate", lambda q, b="CNY", as_of_date=None: 1.0)
    pm = _pm(tmp_path, cash={"CNY": 10.0}, holdings=[], history=[])
    monkeypatch.setattr("openinvest.core.portfolio_manager.PortfolioManager", lambda: pm)
    view = skill_views.build_status_view()
    assert view["performance"]["status"] == "insufficient_history"

    def boom(pm):
        raise RuntimeError("db gone")
    monkeypatch.setattr(ap, "build_account_performance", boom)
    view = skill_views.build_status_view()
    assert view["performance"]["status"] == "error" and "db gone" in view["performance"]["error"]
