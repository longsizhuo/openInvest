"""utils/fx.py 测试"""
from __future__ import annotations

import pandas as pd
import pytest

from utils import fx


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
