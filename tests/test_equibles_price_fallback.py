"""Equibles 价格兜底：yfinance 被限流/抓空时 A 股 / 港股个股走 Equibles 日线。

接线点：exchange_fee.get_history_data 的 yfinance 失败/返回空分支
→ _equibles_fallback → equibles.fetch_daily_prices → save_generic_price(source="equibles")。
"""
from __future__ import annotations

import pandas as pd
import pytest

import openinvest.utils.equibles as eq
import openinvest.utils.exchange_fee as ef

_TABLE = """Daily prices for XSHG:600519 (Kweichow Moutai Co.,Ltd.), quoted in CNY:

| Date | Open | High | Low | Close | Adj Close | Volume |
|------|------|------|-----|-------|-----------|--------|
| 2026-06-25 | 1207.00 | 1227.00 | 1200.00 | 1212.10 | 1184.08 | 4,844,649 |
| 2026-06-26 | 1199.00 | 1199.00 | 1168.10 | 1168.63 | 1168.63 | 5,006,647 |

_Adj Close is the provider-adjusted close._

Data: Equibles - https://equibles.com/stocks/xshg/600519/summary"""


class _EmptyTicker:
    """yfinance 返回空 df（被墙/被限流的典型表现）"""

    def __init__(self, symbol):
        pass

    def history(self, period=None, **kwargs):
        return pd.DataFrame()


@pytest.fixture
def yf_down(monkeypatch):
    monkeypatch.setattr(ef.yf, "Ticker", _EmptyTicker)


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv("EQUIBLES_API_KEY", "eq_test")


def _fake_store(monkeypatch, saved, preset=None):
    """_STORE 双向 mock：save_generic_price 记录写入（同日覆盖），get_history_df 回读。"""
    rows = dict(preset or {})  # date -> close

    def fake_save(symbol, date_str, close, source="yfinance", **kwargs):
        saved.append({"symbol": symbol, "date": date_str, "close": close, "source": source, **kwargs})
        rows[date_str] = close

    def fake_get(symbol, days=730):
        if not rows:
            return pd.DataFrame()
        ds = sorted(rows)
        return pd.DataFrame({"Close": [rows[d] for d in ds]}, index=pd.to_datetime(ds))

    monkeypatch.setattr(ef._STORE, "save_generic_price", fake_save)
    monkeypatch.setattr(ef._STORE, "get_history_df", fake_get)


def _fake_tool(monkeypatch, calls, text=_TABLE):
    def fake_call(name, args, api_key):
        calls.append((name, args))
        return text
    monkeypatch.setattr(eq, "call_tool", fake_call)


def test_fetch_daily_prices_parses_table(key, monkeypatch):
    calls = []
    _fake_tool(monkeypatch, calls)
    df = eq.fetch_daily_prices("600519.SS", "2026-06-01")

    assert calls == [("GetStockPrices", {"ticker": "XSHG:600519", "startDate": "2026-06-01", "maxResults": 500})]
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Adj Close", "Volume"]
    assert [d.strftime("%Y-%m-%d") for d in df.index] == ["2026-06-25", "2026-06-26"]
    assert df["Close"].iloc[0] == 1212.10 and df["Adj Close"].iloc[0] == 1184.08
    assert df["Volume"].iloc[0] == 4844649


def test_fetch_daily_prices_not_covered_returns_none(key, monkeypatch):
    _fake_tool(monkeypatch, [], text="Listing 'XSHG:510300' not found. Use a bare ticker ...")
    assert eq.fetch_daily_prices("510300.SS", "2026-06-01") is None


def test_yfinance_down_a_share_falls_back_to_equibles(yf_down, key, monkeypatch):
    saved, calls = [], []
    _fake_store(monkeypatch, saved)
    _fake_tool(monkeypatch, calls)

    df = ef.get_history_data("600519.SS")

    assert len(calls) == 1 and calls[0][1]["ticker"] == "XSHG:600519"
    first = saved[0]
    # 与 yfinance 路径同口径：Close=含分红复权（Adj Close），close_raw=只按拆股复权（Close）
    assert first["source"] == "equibles" and first["date"] == "2026-06-25"
    assert first["close"] == pytest.approx(1184.08) and first["close_raw"] == pytest.approx(1212.10)
    assert first["high"] == pytest.approx(1227.00 * 1184.08 / 1212.10)
    assert first["volume"] == 4844649
    assert not df.empty and df.attrs["yf_fetch_failed"] is True   # Equibles 晚两天，照样标陈旧


def test_fallback_throttled_across_calls(yf_down, key, monkeypatch):
    saved, calls = [], []
    _fake_store(monkeypatch, saved)
    _fake_tool(monkeypatch, calls, text="Listing not found")   # 没覆盖也要节流，不能每次烧额度

    ef.get_history_data("600519.SS")
    ef.get_history_data("600519.SS")
    assert len(calls) == 1


def test_no_key_or_unmapped_symbol_never_calls(yf_down, monkeypatch):
    monkeypatch.delenv("EQUIBLES_API_KEY", raising=False)
    saved = []
    _fake_store(monkeypatch, saved)
    monkeypatch.setattr(eq, "call_tool", lambda *a, **k: pytest.fail("不该调 Equibles"))

    assert ef.get_history_data("600519.SS").empty
    monkeypatch.setenv("EQUIBLES_API_KEY", "eq_test")
    assert ef.get_history_data("AAPL").empty and saved == []


def test_deep_history_increments_and_refetches_on_basis_shift(yf_down, key, monkeypatch):
    """库里够深 → 只拉最后一根往前 7 天；重叠日整体偏移 >1%（分红后全历史重算）→ 改拉 2 年重写。"""
    preset = {d.strftime("%Y-%m-%d"): 1300.0 for d in pd.bdate_range(end="2026-06-26", periods=300)}
    saved, calls = [], []
    _fake_store(monkeypatch, saved, preset)
    _fake_tool(monkeypatch, calls)   # 重叠日 06-25 1184.08 vs 库里 1300；06-26 1168.63 vs 1300

    ef.get_history_data("600519.SS")

    assert calls[0][1]["startDate"] == "2026-06-19"
    assert len(calls) == 2 and calls[1][1]["startDate"] < "2025-01-01"
