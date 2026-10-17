"""status / live_prices 对任何持仓一视同仁（评审路线 Sprint 2 · C4）

以前 build_status_view 按 symbol 名特判 NDQ.AX / GC=F / AUD；现在：
- 每个持仓走同一条定价路径 utils.quotes.get_quote（黄金克价口径按 proxy_kind / kind=metal 认）
- 外币段按现金 / 持仓的币种出现与否判断
- _safe_close 拉不到返回 None（不是 0.0 哨兵）——live_prices 对外契约是 null

全部不联网：get_quote / get_fx_rate / get_history_data / get_gold_snapshot 全 patch，
patch 的是源模块属性（skill_views 的 builder 走函数内延迟 import）。
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pandas as pd
import pytest

from openinvest.core.memory_store import MemoryStore
from openinvest.services import skill_views
from openinvest.utils.quotes import QuoteSnapshot


def _seed(cash, holdings):
    s = MemoryStore()
    s.write("user", "user", {"display_name": "Fork", "risk_tolerance": "Balanced"}, "")
    s.write("strategy", "strategy", {"target_assets": []}, "")
    s.write("portfolio", "state", {"cash": cash, "holdings": holdings, "schema_version": 2}, "")


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """业绩块另有测试（test_account_performance）；这里只看持仓定价，且不碰真实行情库。"""
    monkeypatch.setattr("openinvest.services.account_performance.build_account_performance",
                        lambda pm=None, **kw: {"status": "insufficient_history"})


def _quote(price, **extra):
    return QuoteSnapshot(symbol="x", price=price, currency="CNY", unit="u",
                         last_updated="2026-10-09", extra=extra)


def test_status_a_share_etf_and_otc_fund_without_any_author_asset(monkeypatch):
    """只含 A 股 ETF + 场外基金（没有 NDQ / GC / AUD）：跑通、市值与总资产正确，且一次都没去拉作者标的。"""
    _seed({"CNY": 1000.0}, [
        {"symbol": "510300.SS", "kind": "etf", "units": 1000.0, "avg_cost": 4.0, "cost_currency": "CNY"},
        {"symbol": "FUND:110020", "kind": "fund", "units": 2000.0, "avg_cost": 1.5, "cost_currency": "CNY",
         "proxy_kind": "eastmoney_fund"},
    ])
    prices = {"510300.SS": 4.5, "FUND:110020": 1.8}
    get_quote = MagicMock(side_effect=lambda h: _quote(prices[h["symbol"]]))
    hist = MagicMock(return_value=pd.DataFrame({"Close": [55.0]}))
    gold = MagicMock(return_value=None)
    monkeypatch.setattr("openinvest.utils.quotes.get_quote", get_quote)
    monkeypatch.setattr("openinvest.utils.exchange_fee.get_history_data", hist)
    monkeypatch.setattr("openinvest.utils.gold_price.get_gold_snapshot", gold)

    out = skill_views.build_status_view()

    mv = {h["symbol"]: h["market_value"] for h in out["all_holdings"]}
    assert mv == {"510300.SS": 4500.0, "FUND:110020": 3600.0}
    assert {h["market_value_cny"] for h in out["all_holdings"]} == {4500.0, 3600.0}   # CNY 持仓 = 原值
    assert out["total_assets_cny"] == 1000.0 + 4500.0 + 3600.0
    assert not {"ndq", "gold"} & set(out)
    assert out["fx"] == {} and out["live_prices"] == {}
    assert set(out["cash"]) == {"cny", "all_currencies"}
    assert not {c.args[0] for c in hist.call_args_list} & {"NDQ.AX", "AUDCNY=X", "GC=F"}
    gold.assert_not_called()
    assert get_quote.call_count == 2


def test_status_gold_gram_recognised_by_proxy_kind_not_symbol(monkeypatch):
    """克价口径看 proxy_kind（老行没写 proxy_kind 的 kind=metal 同口径），symbol 叫什么都行；
    多个克价持仓合并成 gold 汇总块，金价 / USDCNY 来自 get_quote 的 extra（不再单独拉快照）。"""
    _seed({"CNY": 0.0}, [
        {"symbol": "ABC-GOLD", "kind": "metal", "units": 10.0, "avg_cost": 1000.0, "cost_currency": "CNY",
         "proxy_kind": "gold_cny_per_gram", "unit_label": "克"},
        {"symbol": "OLD-ROW", "kind": "metal", "units": 5.0, "avg_cost": 1200.0, "cost_currency": "CNY"},
    ])
    seen = {}

    def fake_quote(h):
        seen[h["symbol"]] = h.get("proxy_kind")
        return _quote(1100.0, gold_usd_per_oz=2400.0, usdcny_rate=7.2)
    monkeypatch.setattr("openinvest.utils.quotes.get_quote", fake_quote)
    monkeypatch.setattr("openinvest.utils.gold_price.get_gold_snapshot",
                        MagicMock(side_effect=AssertionError("status 不该再单独拉金价快照")))

    out = skill_views.build_status_view()

    assert seen == {"ABC-GOLD": "gold_cny_per_gram", "OLD-ROW": "gold_cny_per_gram"}
    g = out["gold"]
    assert g["grams"] == 15.0
    assert g["avg_cost_cny_per_gram"] == pytest.approx((10 * 1000 + 5 * 1200) / 15)
    assert g["now_cny_per_gram"] == 1100.0 and g["value_cny"] == 16500.0
    assert g["pnl_cny"] == pytest.approx((1100 - (16000 / 15)) * 15, abs=0.01)
    assert out["live_prices"] == {"gold_usd_per_oz": 2400.0, "usdcny": 7.2}
    assert out["total_assets_cny"] == 16500.0


def test_status_gold_quote_unavailable_is_null_not_zero(monkeypatch):
    """金价拉不到：gold 块的价格字段是 null（以前 0.0 → 市值 0、pnl 亏光），持仓视图同样 null。"""
    _seed({"CNY": 0.0}, [
        {"symbol": "G", "kind": "metal", "units": 10.0, "avg_cost": 1000.0, "cost_currency": "CNY",
         "proxy_kind": "gold_cny_per_gram"},
    ])
    monkeypatch.setattr("openinvest.utils.quotes.get_quote", lambda h: None)
    out = skill_views.build_status_view()
    assert out["gold"]["now_cny_per_gram"] is None and out["gold"]["value_cny"] is None
    assert out["gold"]["pnl_cny"] is None and out["gold"]["pnl_pct"] is None
    assert out["all_holdings"][0]["current_price"] is None
    assert out["live_prices"] == {"gold_usd_per_oz": None, "usdcny": None}


def test_status_foreign_currency_sections_follow_currencies_in_use(monkeypatch):
    """汇率段按现金 / 持仓里出现的非 CNY 币种出，不再只认 AUD；用了 AUD 现金才出 cash.aud。"""
    _seed({"CNY": 100.0, "USD": 10.0}, [
        {"symbol": "0700.HK", "kind": "equity", "units": 100.0, "avg_cost": 300.0, "cost_currency": "HKD"},
    ])
    rates = {"USD": 7.0, "HKD": 0.9}
    monkeypatch.setattr("openinvest.utils.fx.get_fx_rate",
                        lambda q, b="CNY", as_of_date=None: 1.0 if q == b else rates.get(q))
    monkeypatch.setattr("openinvest.utils.quotes.get_quote", lambda h: _quote(320.0))

    out = skill_views.build_status_view()

    assert out["fx"] == {"hkdcny": 0.9, "usdcny": 7.0}
    assert "aud" not in out["cash"]
    # 外币持仓都带折人民币市值（以前只有 NDQ.AX 专属块有）
    assert out["all_holdings"][0]["market_value_cny"] == pytest.approx(100 * 320.0 * 0.9)
    assert out["total_assets_cny"] == pytest.approx(100 + 10 * 7.0 + 100 * 320.0 * 0.9)


# ---------- _safe_close：拉不到 → None ----------

def test_safe_close_returns_none_when_unavailable(monkeypatch):
    calls = []

    def empty(symbol, period="1d"):
        calls.append(period)
        return pd.DataFrame()
    monkeypatch.setattr("openinvest.utils.exchange_fee.get_history_data", empty)
    assert skill_views._safe_close("^VIX") is None
    assert calls == ["1d", "5d"]   # 先 1d，空再退 5d


def test_safe_close_returns_none_on_exception_and_nan(monkeypatch):
    def boom(symbol, period="1d"):
        raise ConnectionError("network down")
    monkeypatch.setattr("openinvest.utils.exchange_fee.get_history_data", boom)
    assert skill_views._safe_close("^VIX") is None

    monkeypatch.setattr("openinvest.utils.exchange_fee.get_history_data",
                        lambda s, p="1d": pd.DataFrame({"Close": [float("nan")]}))
    assert skill_views._safe_close("^VIX") is None


def test_safe_close_returns_latest_close(monkeypatch):
    monkeypatch.setattr("openinvest.utils.exchange_fee.get_history_data",
                        lambda s, p="1d": pd.DataFrame({"Close": [1.0, 2.5]}))
    assert skill_views._safe_close("X") == 2.5


def test_helpers_safe_close_is_the_same_implementation():
    """#236：skill_cmds/_helpers 不再有第二份 0.0 哨兵副本。"""
    from openinvest.skill_cmds import _helpers
    assert _helpers._safe_close is skill_views._safe_close


def test_live_prices_unavailable_fields_are_null(monkeypatch):
    """MCP live_prices docstring 说拉不到是 null——实际也必须是 null，不是 0.0。"""
    from openinvest.connectors import mcp_server
    monkeypatch.setattr("openinvest.utils.exchange_fee.get_history_data", lambda s, p="1d": pd.DataFrame())
    monkeypatch.setattr("openinvest.utils.gold_price.get_gold_snapshot", lambda offset_pct=0.0: None)
    out = mcp_server.live_prices()
    assert {k: v for k, v in out.items() if k != "as_of"} == {
        "GC_F_usd_per_oz": None, "gold_cny_per_gram_spot": None, "USDCNY": None,
        "AUDCNY": None, "NDQ_AX": None, "VIX": None, "TNX": None,
    }


def test_what_if_still_treats_unavailable_price_as_excluded(monkeypatch):
    """_safe_close 改返回 None 后，what_if 自己的「0 = 拉不到」口径不变：列进未计入，不抛 TypeError。"""
    _seed({"CNY": 1000.0}, [
        {"symbol": "AAA", "kind": "equity", "units": 10.0, "avg_cost": 1.0, "cost_currency": "CNY"},
    ])
    monkeypatch.setattr(skill_views, "_safe_close", lambda s: None)
    monkeypatch.setattr("openinvest.utils.gold_price.get_gold_snapshot", lambda **k: None)
    out = skill_views.build_what_if_view(symbol="AAA", pct=-10)
    assert out["excluded_from_total"] == ["AAA"]
    assert out["current_total_cny"] == 1000.0


def test_status_gold_price_is_spot_regardless_of_price_offset(monkeypatch):
    """走 get_quote 后金价口径不变：now_cny_per_gram 是现货价，持仓的 price_offset_pct 只影响
    extra.bank_cny_per_gram（银行点差价），不会让设了点差的用户 status 里的现价 / pnl_pct 跳变。"""
    from openinvest.calc.gold import GoldPriceSnapshot

    def fake_snapshot(offset_pct=0.0):   # 与 utils.gold_price.get_gold_snapshot 同公式
        spot = 2400.0 / 31.1035 * 7.2
        return GoldPriceSnapshot(gold_usd_per_oz=2400.0, usdcny_rate=7.2, spot_cny_per_gram=spot,
                                 bank_cny_per_gram=spot * (1 + offset_pct), offset_pct=offset_pct, is_stale=False)
    monkeypatch.setattr("openinvest.utils.quotes.get_gold_snapshot", fake_snapshot)

    def status_with_offset(offset):
        _seed({"CNY": 0.0}, [{"symbol": "G", "kind": "metal", "units": 10.0, "avg_cost": 500.0,
                              "cost_currency": "CNY", "proxy_kind": "gold_cny_per_gram",
                              **({"price_offset_pct": offset} if offset is not None else {})}])
        return skill_views.build_status_view()

    plain, with_offset = status_with_offset(None), status_with_offset(0.012)
    assert plain["gold"]["now_cny_per_gram"] == with_offset["gold"]["now_cny_per_gram"]
    assert plain["gold"]["pnl_pct"] == with_offset["gold"]["pnl_pct"]
    assert with_offset["all_holdings"][0]["extra"]["offset_pct"] == 0.012   # 点差只在 extra 里
