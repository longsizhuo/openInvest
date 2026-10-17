"""utils/fx.py 测试"""
from __future__ import annotations

import math

import pandas as pd
import pytest

from openinvest.utils import fx


@pytest.fixture(autouse=True)
def clear_cache():
    fx._RATE_CACHE.clear()
    yield


def _fake_df(close: float):
    return pd.DataFrame(
        {"Close": [close]},
        index=pd.date_range("2026-05-06", periods=1),
    )


def test_same_currency_returns_one():
    assert fx.get_fx_rate("CNY", "CNY") == 1.0
    assert fx.get_fx_rate("AUD", "AUD") == 1.0


def test_get_fx_rate_via_yfinance(monkeypatch):
    captured = []
    monkeypatch.setattr(fx, "get_history_data",
                        lambda s, p="5d", **kw: (captured.append(s), _fake_df(7.1))[1])
    rate = fx.get_fx_rate("USD", "CNY")
    assert rate == 7.1
    assert captured == ["USDCNY=X"]


def test_cache_hit_no_second_call(monkeypatch):
    calls = []
    monkeypatch.setattr(fx, "get_history_data",
                        lambda s, p="5d", **kw: (calls.append(s), _fake_df(4.9))[1])
    fx.get_fx_rate("AUD", "CNY")
    fx.get_fx_rate("AUD", "CNY")
    assert len(calls) == 1, "第二次应命中 cache"


def test_failure_returns_none(monkeypatch):
    def _boom(s, p="5d", **kw):
        raise RuntimeError("yfinance down")
    monkeypatch.setattr(fx, "get_history_data", _boom)
    assert fx.get_fx_rate("USD", "CNY") is None


def test_to_base(monkeypatch):
    monkeypatch.setattr(fx, "get_history_data",
                        lambda s, p="5d", **kw: _fake_df(7.0))
    # USD 100 → CNY 700
    assert fx.to_base("USD", 100, "CNY") == 700.0


def test_to_base_threads_as_of_date(monkeypatch):
    """回测：to_base(as_of_date=...) 必须把日期透传到 get_history_data（防前视偏差）"""
    captured: dict = {}
    monkeypatch.setattr(
        fx, "get_history_data",
        lambda s, p="5d", **kw: (captured.update(kw), _fake_df(6.5))[1],
    )
    fx.to_base("USD", 100, "CNY", as_of_date="2024-03-15")
    assert captured.get("as_of_date") == "2024-03-15"


def test_total_portfolio_value_threads_as_of_date(monkeypatch):
    """total_portfolio_value_cny(as_of_date=...) 对 cash + holdings 折算都按历史汇率拉"""
    seen_dates: list = []

    def _capture(symbol, p="5d", **kw):
        seen_dates.append(kw.get("as_of_date"))
        return _fake_df(7.0)

    monkeypatch.setattr(fx, "get_history_data", _capture)

    class _StubPM:
        cash = {"USD": 100}
        holdings = [{"symbol": "AAPL", "units": 2, "avg_cost": 50, "cost_currency": "USD"}]

    total, status = fx.total_portfolio_value_cny(
        _StubPM(), {"AAPL": 50}, base="CNY", as_of_date="2024-03-15",
    )
    # cash 100 USD * 7 = 700；holding 2*50=100 USD * 7 = 700 → 1400
    assert total == 1400.0
    # 两次折算（cash + holding）都按历史日期拉，没有任何 None（实时）泄漏
    assert seen_dates and all(d == "2024-03-15" for d in seen_dates)


def test_to_base_live_default_passes_none_as_of_date(monkeypatch):
    """无 as_of_date 调用 = 实盘路径：get_fx_rate 必须收到 as_of_date=None。

    锁住 "不传日期 => 实时 FX"，防未来 refactor 误注入一个 stale/default 日期
    （那会让实盘估值悄悄用历史汇率，正是 PR#53 fix(fx) 要防的前视/陈旧漂移）。
    """
    seen: list = []
    monkeypatch.setattr(
        fx, "get_fx_rate",
        lambda quote, base="CNY", **kw: (seen.append(kw.get("as_of_date", "MISSING")), 7.0)[1],
    )
    assert fx.to_base("USD", 100, "CNY") == 700.0
    assert seen == [None], "to_base 无 as_of_date 应以 as_of_date=None 调 get_fx_rate（实盘路径）"


def test_total_portfolio_value_live_default_passes_none_as_of_date(monkeypatch):
    """total_portfolio_value_cny 无 as_of_date：cash + holding 两腿都 as_of_date=None。"""
    seen: list = []
    monkeypatch.setattr(
        fx, "get_fx_rate",
        lambda quote, base="CNY", **kw: (seen.append(kw.get("as_of_date", "MISSING")), 7.0)[1],
    )

    class _StubPM:
        cash = {"USD": 100}
        holdings = [{"symbol": "AAPL", "units": 2, "avg_cost": 50, "cost_currency": "USD"}]

    total, _status = fx.total_portfolio_value_cny(_StubPM(), {"AAPL": 50}, base="CNY")
    assert total == 1400.0
    # 两腿（cash + holding）都走实盘路径，没有任何 stale/default 日期泄漏进来
    assert seen == [None, None], "两腿都应以 as_of_date=None 调 get_fx_rate（实盘路径）"


def test_total_portfolio_value_isolates_nan_priced_holding(monkeypatch):
    """单个持仓 current_price 为 NaN 不得污染 total，也不得让其余 holding 集中度归零。

    根因回归：510300.SS 当日 close=NULL → current_prices['510300.SS']=NaN(float)。
    NaN 穿过所有 `is None` 守卫，`total += NaN` → round(NaN)=NaN，污染整个总资产，
    使每个 holding 的 conc_pct 走 `total>0=False` 的 else 分支静默归零。

    正确语义：NaN 价等同缺价 → 标 missing_price 并排除，total 永远 finite，
    其余可解析持仓正常计入。
    """
    monkeypatch.setattr(
        fx, "get_history_data",
        lambda s, p="5d", **kw: _fake_df(7.0),  # 任意 FX：CNY=1 恒等不查
    )

    class _StubPM:
        cash = {}  # 纯持仓，隔离 cash 腿
        holdings = [
            {"symbol": "GOLD", "units": 10, "avg_cost": 100, "cost_currency": "CNY"},
            # avg_cost=0 → 无 cost 兜底，NaN 价后必须落 missing_price（不得静默归 0）
            {"symbol": "510300.SS", "units": 1000, "avg_cost": 0.0, "cost_currency": "CNY"},
        ]

    total, status = fx.total_portfolio_value_cny(
        _StubPM(),
        {"GOLD": 134.0, "510300.SS": float("nan")},
        base="CNY",
    )

    # total 必须 finite（不被 NaN 污染）
    assert math.isfinite(total), f"total 被 NaN 污染: {total!r}"
    # total 只含 GOLD 那条：10 * 134 = 1340（CNY，FX 恒等）
    assert total == 1340.0
    # NaN 价等同 None → missing_price，排除出 total
    assert status["510300.SS"] == "missing_price"
    # GOLD 正常计入
    assert status["GOLD"] == "ok"


def test_cash_total_in_base_mixed(monkeypatch):
    rates = {"USDCNY=X": 7.0, "AUDCNY=X": 4.9}
    monkeypatch.setattr(
        fx, "get_history_data",
        lambda s, p="5d", **kw: _fake_df(rates.get(s, 0.0)) if s in rates else _fake_df(0.0),
    )
    total, per_rate = fx.cash_total_in_base({"CNY": 100, "USD": 50, "AUD": 200}, "CNY")
    # 100 + 50*7 + 200*4.9 = 100 + 350 + 980 = 1430
    assert total == 1430.0
    assert per_rate["CNY"] == 1.0
    assert per_rate["USD"] == 7.0


def test_cash_total_in_base_handles_missing_rate(monkeypatch):
    """某币种汇率拉不到时跳过它，total 不含它"""
    def _spotty(symbol, p="5d", **kw):
        if symbol == "USDCNY=X":
            return _fake_df(7.0)
        return pd.DataFrame()    # 其他都拉不到
    monkeypatch.setattr(fx, "get_history_data", _spotty)
    total, rates = fx.cash_total_in_base({"CNY": 100, "USD": 10, "EUR": 5}, "CNY")
    # 只算上 CNY (100) + USD (10*7=70) = 170；EUR 失败被跳过
    assert total == 170.0
    assert rates["EUR"] is None


# ---------- 多币种第 0 步：直接对拉不到时试反向对 / 经 USD 交叉，都不行才 None ----------

def _pairs(monkeypatch, table):
    calls = []

    def fake(s, p="5d", **kw):
        calls.append(s)
        return _fake_df(table[s]) if s in table else None
    monkeypatch.setattr(fx, "get_history_data", fake)
    return calls


def test_fx_falls_back_to_inverse_pair(monkeypatch):
    calls = _pairs(monkeypatch, {"CNYHKD=X": 1.25})
    assert fx.get_fx_rate("HKD", "CNY") == pytest.approx(0.8)
    assert calls[:2] == ["HKDCNY=X", "CNYHKD=X"]


def test_fx_falls_back_to_usd_cross(monkeypatch):
    _pairs(monkeypatch, {"XAFUSD=X": 0.0016, "USDCNY=X": 7.0})
    assert fx.get_fx_rate("XAF", "CNY") == pytest.approx(0.0112)


def test_fx_unavailable_and_nan_pairs_return_none(monkeypatch):
    _pairs(monkeypatch, {"ZZZCNY=X": float("nan")})
    assert fx.get_fx_rate("ZZZ", "CNY") is None


def test_unvalued_lists_entries_left_out_of_total():
    status = {"CNY": "ok", "AAPL": "missing_fx", "X": "missing_price", "T": "tracking_only"}
    assert fx.unvalued(status) == ["AAPL", "X"]


# ---------- 基准币种第 1 步：可投现金方案 A + 总资产单一实现 ----------

class _CashPM:
    def __init__(self, cash, holdings=()):
        self.cash = cash
        self.holdings = list(holdings)


def test_dry_powder_converts_every_currency_and_names_the_missing(monkeypatch):
    _pairs(monkeypatch, {"AUDCNY=X": 4.7})
    assert fx.dry_powder(_CashPM({"CNY": 50589.0, "AUD": 2599.0})) == (pytest.approx(62804.3), [])
    total, missing = fx.dry_powder(_CashPM({"CNY": 1000.0, "ZZZ": 5.0}))
    assert total == 1000.0 and missing == ["ZZZ"]
    assert fx.dry_powder(_CashPM({"CNY": -300.0})) == (0.0, [])


def test_total_value_excludes_listed_symbols_and_flags_cost_fallback(monkeypatch):
    _pairs(monkeypatch, {})
    h = lambda s: {"symbol": s, "units": 10, "avg_cost": 2.0, "cost_currency": "CNY"}
    pm = _CashPM({"CNY": 100.0}, [h("A"), h("B"), h("C")])
    total, status = fx.total_portfolio_value(pm, {"A": 3.0, "B": None}, "CNY", exclude=("C",))
    assert total == 100.0 + 30.0 + 20.0       # A 现价、B 成本价兜底、C 被排除
    assert status["A"] == "ok" and status["B"] == "cost_fallback" and status["C"] == "excluded"
    assert fx.total_portfolio_value_cny is fx.total_portfolio_value


def test_dry_powder_upper_cases_skips_zero_and_honors_override(monkeypatch):
    asked = []
    monkeypatch.setattr(fx, "get_fx_rate",
                        lambda q, b="CNY", as_of_date=None: asked.append(q) or (1.0 if q == b else None))
    total, missing = fx.dry_powder(_CashPM({"cny": 100.0, "aud": 10.0, "usd": 0}))
    assert (total, missing) == (100.0, ["AUD"])          # 键统一大写；余额 0 的 USD 不查汇率
    assert "USD" not in asked
    assert fx.dry_powder(_CashPM({"CNY": 100.0, "AUD": 10.0}), rate_override={"AUD": 5.0}) == (150.0, [])


def test_duplicate_symbol_rows_status_follows_last_row(monkeypatch):
    _pairs(monkeypatch, {})
    rows = [{"symbol": "A", "units": 1, "avg_cost": 2.0, "cost_currency": "CNY"},
            {"symbol": "A", "units": 1, "avg_cost": 2.0, "cost_currency": "CNY"}]
    _, status = fx.total_portfolio_value(_CashPM({}, rows), {"A": 3.0}, "CNY")
    assert status["A"] == "ok"


def test_total_portfolio_value_rate_override_skips_network(monkeypatch):
    """账户业绩日度重放：给了 rate_override 就只查表，不碰 get_fx_rate；缺的币种标 missing_fx。"""
    from types import SimpleNamespace
    monkeypatch.setattr(fx, "get_fx_rate", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network")))
    pm = SimpleNamespace(
        cash={"CNY": 100.0, "AUD": 10.0, "JPY": 5.0},
        holdings=[{"symbol": "NDQ.AX", "units": 2.0, "avg_cost": 50.0, "cost_currency": "AUD"}],
    )
    total, status = fx.total_portfolio_value(pm, {"NDQ.AX": 60.0}, "CNY", rate_override={"AUD": 4.5})
    assert total == pytest.approx(100 + 10 * 4.5 + 2 * 60 * 4.5)
    assert status == {"CNY": "ok", "AUD": "ok", "JPY": "missing_fx", "NDQ.AX": "ok"}
