"""utils/quotes.py 测试

3 种 proxy_kind 的分支 + yfinance 失败容忍。
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
import pytest

from openinvest.utils import quotes


@dataclass
class FakeGoldSnap:
    gold_usd_per_oz: float = 2400.0
    usdcny_rate: float = 7.2
    spot_cny_per_gram: float = 1031.0
    bank_cny_per_gram: float = 1031.5
    offset_pct: float = 0.0005
    is_stale: bool = False


def _fake_df():
    return pd.DataFrame(
        {"Close": [50.0, 51.0, 52.0, 51.5, 52.5]},
        index=pd.date_range("2026-04-28", periods=5, freq="D"),
    )


def test_get_quote_direct(monkeypatch):
    """direct: 直接 yfinance 拉"""
    monkeypatch.setattr(quotes, "get_history_data", lambda s, p="5d": _fake_df())
    h = {"symbol": "AAPL", "kind": "equity", "cost_currency": "USD",
         "unit_label": "股", "proxy_kind": "direct"}
    q = quotes.get_quote(h)
    assert q is not None
    assert q.symbol == "AAPL"
    assert q.price == 52.5
    assert q.currency == "USD"
    assert q.unit == "股"
    assert q.extra["prev_close"] == 51.5
    assert q.extra["day_change_pct"] == pytest.approx(1.9417, rel=1e-3)


def test_get_quote_gold(monkeypatch):
    """gold_cny_per_gram: 走 get_gold_snapshot 反推"""
    monkeypatch.setattr(quotes, "get_gold_snapshot", lambda offset_pct=0.0: FakeGoldSnap(offset_pct=offset_pct))
    h = {"symbol": "GC=F", "kind": "metal", "cost_currency": "CNY",
         "unit_label": "克", "proxy_kind": "gold_cny_per_gram",
         "price_offset_pct": 0.0005}
    q = quotes.get_quote(h)
    assert q is not None
    assert q.symbol == "GC=F"
    assert q.price == 1031.0
    assert q.currency == "CNY"
    assert q.unit == "克"
    assert q.extra["bank_cny_per_gram"] == 1031.5
    assert q.extra["offset_pct"] == 0.0005
    assert q.is_stale is False


def test_get_quote_gold_stale(monkeypatch):
    """gold proxy 数据库兜底时 is_stale=True"""
    monkeypatch.setattr(quotes, "get_gold_snapshot",
                        lambda offset_pct=0.0: FakeGoldSnap(is_stale=True))
    q = quotes.get_quote({
        "symbol": "GC=F", "kind": "metal", "cost_currency": "CNY",
        "unit_label": "克", "proxy_kind": "gold_cny_per_gram",
    })
    assert q.is_stale is True


def test_get_quote_gold_returns_none_when_snapshot_fails(monkeypatch):
    """get_gold_snapshot 返回 None → quote 也返回 None"""
    monkeypatch.setattr(quotes, "get_gold_snapshot", lambda offset_pct=0.0: None)
    q = quotes.get_quote({
        "symbol": "GC=F", "kind": "metal", "cost_currency": "CNY",
        "unit_label": "克", "proxy_kind": "gold_cny_per_gram",
    })
    assert q is None


def test_get_quote_fx(monkeypatch):
    """fx_pair: yfinance 拉汇率"""
    monkeypatch.setattr(quotes, "get_history_data", lambda s, p="5d": _fake_df())
    h = {"symbol": "USDCNY", "yfinance_proxy": "USDCNY=X",
         "kind": "fund", "cost_currency": "CNY",
         "unit_label": "rate", "proxy_kind": "fx_pair"}
    q = quotes.get_quote(h)
    assert q is not None
    assert q.unit == "rate"


def test_get_quote_uses_yfinance_proxy(monkeypatch):
    """holding.symbol 是用户自定义名（如 ZSGOLD），但 yfinance_proxy=GC=F 是真实拉取目标"""
    captured = []

    def _spy(symbol, period="5d"):
        captured.append(symbol)
        return _fake_df()

    monkeypatch.setattr(quotes, "get_history_data", _spy)
    quotes.get_quote({
        "symbol": "ZSGOLD-CUSTOM", "yfinance_proxy": "GC=F",
        "kind": "metal", "cost_currency": "CNY",
        "unit_label": "克", "proxy_kind": "direct",   # 强制走 direct
    })
    assert captured == ["GC=F"]   # 用 proxy 拉，不是 ZSGOLD-CUSTOM


def test_get_quote_direct_failure_returns_none(monkeypatch):
    """yfinance 抛异常时静默返回 None，不让上层崩"""
    def _boom(s, p="5d"):
        raise RuntimeError("yfinance API down")
    monkeypatch.setattr(quotes, "get_history_data", _boom)
    q = quotes.get_quote({
        "symbol": "AAPL", "kind": "equity", "cost_currency": "USD",
        "unit_label": "股", "proxy_kind": "direct",
    })
    assert q is None


def test_get_quote_direct_empty_df_returns_none(monkeypatch):
    """空 DataFrame（休市/无数据）→ None"""
    monkeypatch.setattr(quotes, "get_history_data",
                        lambda s, p="5d": pd.DataFrame(columns=["Close"]))
    q = quotes.get_quote({
        "symbol": "AAPL", "kind": "equity", "cost_currency": "USD",
        "unit_label": "股", "proxy_kind": "direct",
    })
    assert q is None


def test_get_quote_default_proxy_kind_is_direct(monkeypatch):
    """holding 没 proxy_kind 字段 → 默认 direct"""
    monkeypatch.setattr(quotes, "get_history_data", lambda s, p="5d": _fake_df())
    q = quotes.get_quote({"symbol": "AAPL", "cost_currency": "USD", "unit_label": "股"})
    assert q is not None
    assert q.symbol == "AAPL"
