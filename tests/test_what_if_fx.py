"""what_if 不再编汇率（多币种第 0 步，2026-10）

以前：外币持仓拉不到汇率按 1:1 当人民币加进总额；AUDCNY 拉不到按写死 4.9；
CNY/AUD 以外的现金直接不算。现在：拉不到的条目列进 excluded_from_total，
总额只含算得出来的部分；其他币种现金照常折算。
"""
from __future__ import annotations

import pandas as pd

from openinvest.core.memory_store import MemoryStore
from openinvest.services import skill_views
from openinvest.utils import fx, gold_price


def _seed(cash, holdings):
    s = MemoryStore()
    s.write("user", "user", {"display_name": "T", "risk_tolerance": "Balanced"}, "")
    s.write("strategy", "strategy", {"target_assets": []}, "")
    s.write("portfolio", "state", {"cash": cash, "holdings": holdings, "schema_version": 2}, "")


def _wire(monkeypatch, prices, rates):
    fx._RATE_CACHE.clear()
    monkeypatch.setattr(skill_views, "_safe_close", lambda s: prices.get(s, 0.0))
    monkeypatch.setattr(gold_price, "get_gold_snapshot", lambda **k: None)
    monkeypatch.setattr(fx, "get_history_data", lambda s, p="5d", **kw: (
        pd.DataFrame({"Close": [rates[s]]}) if s in rates else None))


def _h(sym, units, ccy):
    return {"symbol": sym, "kind": "equity", "units": units, "avg_cost": 1.0,
            "unit_label": "股", "cost_currency": ccy, "proxy_kind": "direct"}


def test_missing_fx_holding_is_excluded_not_counted_one_to_one(monkeypatch):
    _seed({"CNY": 1000.0}, [_h("AAPL", 10, "USD"), _h("510300.SS", 100, "CNY")])
    _wire(monkeypatch, {"AAPL": 200.0, "510300.SS": 4.0}, rates={})  # 美元汇率全拉不到
    out = skill_views.build_what_if_view(symbol="510300.SS", pct=-10)
    assert out["excluded_from_total"] == ["AAPL"]
    assert out["current_total_cny"] == 1400.0          # 1000 现金 + 400 A 股，不含 2000 美元
    assert out["scenario_total_cny"] == 1360.0
    assert out["breakdown"]["AAPL"]["unavailable"]


def test_aud_rate_unavailable_is_not_replaced_by_constant(monkeypatch):
    _seed({"CNY": 1000.0, "AUD": 100.0}, [])
    _wire(monkeypatch, {}, rates={})
    out = skill_views.build_what_if_view()
    assert out["excluded_from_total"] == ["AUD 现金"]
    assert out["current_total_cny"] == 1000.0
    assert out["fx"]["audcny_cur"] is None


def test_other_currency_cash_is_counted(monkeypatch):
    _seed({"CNY": 1000.0, "USD": 100.0}, [])
    _wire(monkeypatch, {}, rates={"USDCNY=X": 7.0})
    out = skill_views.build_what_if_view()
    assert "excluded_from_total" not in out
    assert out["current_total_cny"] == 1700.0
