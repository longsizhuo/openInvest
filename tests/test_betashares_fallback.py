"""BetaShares 官网兜底：yfinance 被墙/抓空时 NDQ.AX 走 scraper 拿现价 NAV。

接线点：exchange_fee.get_history_data 的 yfinance 失败/返回空分支
→ _betashares_fallback → scrape_full_ndq_data → save_generic_price(source="betashares_fallback")。
"""
from __future__ import annotations

import pandas as pd
import pytest

import openinvest.utils.betashares_scraper as scraper
import openinvest.utils.exchange_fee as ef


class _EmptyTicker:
    """yfinance 返回空 df（被墙/被限流的典型表现）"""

    def __init__(self, symbol):
        pass

    def history(self, period=None, **kwargs):
        return pd.DataFrame()


@pytest.fixture
def yf_down(monkeypatch):
    monkeypatch.setattr(ef.yf, "Ticker", _EmptyTicker)


def _fake_store(monkeypatch, saved):
    """_STORE 双向 mock：save_generic_price 记录写入，get_history_df 回读已写行。"""

    def fake_save(symbol, date_str, close, source="yfinance", **kwargs):
        saved.append({"symbol": symbol, "date": date_str, "close": close, "source": source})

    def fake_get(symbol):
        rows = [r for r in saved if r["symbol"] == symbol]
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame(
            {"Close": [r["close"] for r in rows]},
            index=pd.to_datetime([r["date"] for r in rows]),
        )

    monkeypatch.setattr(ef._STORE, "save_generic_price", fake_save)
    monkeypatch.setattr(ef._STORE, "get_history_df", fake_get)


def test_yfinance_empty_ndq_falls_back_to_betashares(yf_down, monkeypatch):
    saved = []
    _fake_store(monkeypatch, saved)
    monkeypatch.setattr(
        scraper, "scrape_full_ndq_data",
        lambda: {"nav": 51.23, "date": "2026-07-03", "stats": {}, "holdings": [], "sectors": []},
    )

    df = ef.get_history_data("NDQ.AX")

    assert saved == [
        {"symbol": "NDQ.AX", "date": "2026-07-03", "close": 51.23, "source": "betashares_fallback"}
    ]
    assert not df.empty and float(df["Close"].iloc[-1]) == 51.23


def test_non_betashares_symbol_never_calls_scraper(yf_down, monkeypatch):
    saved = []
    _fake_store(monkeypatch, saved)
    monkeypatch.setattr(
        scraper, "scrape_full_ndq_data",
        lambda: pytest.fail("scraper must not be called for non-BetaShares symbol"),
    )

    df = ef.get_history_data("AAPL")
    assert df.empty and saved == []


def test_scraper_failure_degrades_silently(yf_down, monkeypatch):
    saved = []
    _fake_store(monkeypatch, saved)
    monkeypatch.setattr(scraper, "scrape_full_ndq_data", lambda: None)

    df = ef.get_history_data("NDQ.AX")
    assert df.empty and saved == []


def test_backtest_cutoff_skips_fallback(yf_down, monkeypatch):
    """as_of_date 回测路径不触发兜底——scraper 只有'现在'一个点，对历史 cutoff 无意义。"""
    saved = []
    _fake_store(monkeypatch, saved)
    monkeypatch.setattr(
        scraper, "scrape_full_ndq_data",
        lambda: pytest.fail("scraper must not be called on backtest path"),
    )

    df = ef.get_history_data("NDQ.AX", as_of_date="2024-05-01")
    assert df.empty and saved == []
