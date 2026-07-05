"""backtest_runner._warmup_market_data 回归测试。

Bug（2026-06-23）：warmup 用 save_generic_price 只写 close → high/low 落 NULL，
且 save_generic_price 是 INSERT OR REPLACE → 会把已有行的 OHLC 冲成 NULL。
后果：被 warmup 的资产（含黄金）路径表（core.regime_probability 走 OHLC 滚动窗口）
算不出 → 邮件"样本不足"。修复：warmup 改走非破坏式 backfill_ohlcv_row 写全 OHLC。
"""
from __future__ import annotations

import pandas as pd
import pytest


@pytest.fixture
def _isolated_market_db(monkeypatch, tmp_path):
    import openinvest.db.market_store as ms
    monkeypatch.setattr(ms, "DB_PATH", str(tmp_path / "market_data.db"))
    return ms


def _fake_yf(monkeypatch, df: pd.DataFrame):
    import yfinance

    class _FakeTicker:
        def __init__(self, symbol):
            self.symbol = symbol

        def history(self, period="10y"):
            return df

    monkeypatch.setattr(yfinance, "Ticker", _FakeTicker)


def test_warmup_writes_full_ohlc_not_close_only(_isolated_market_db, monkeypatch):
    """warmup 后，被预热资产的 High/Low 必须有值（非 NULL）——否则路径表算不出。"""
    ms = _isolated_market_db
    idx = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"])
    df = pd.DataFrame(
        {"Close": [5.0, 5.1, 5.2], "High": [5.2, 5.3, 5.4],
         "Low": [4.9, 5.0, 5.1], "Volume": [100, 200, 300]},
        index=idx,
    )
    _fake_yf(monkeypatch, df)

    from scripts.backtest_runner import _warmup_market_data
    _warmup_market_data(["510300.SS"])

    cur = ms.MarketStore().conn.cursor()
    cur.execute("SELECT COUNT(*), COUNT(high), COUNT(low) "
                "FROM daily_prices WHERE symbol = '510300.SS'")
    total, n_high, n_low = cur.fetchone()
    assert total == 3
    assert n_high == total, "warmup 写出的 High 不应有 NULL"
    assert n_low == total, "warmup 写出的 Low 不应有 NULL"


def test_warmup_does_not_nullify_existing_ohlc(_isolated_market_db, monkeypatch):
    """已有 OHLC 行被 warmup 预热时，high/low 不应被冲成 NULL。

    注：backfill_ohlcv_row 会用 yfinance 值 UPDATE high/low/volume（同源，不影响
    路径表正确性），只保证不动权威 close/source —— 这里断言的是"不变 NULL"。
    """
    ms = _isolated_market_db
    store = ms.MarketStore()
    # 先种一行完整 OHLC（模拟 live daily_report 写入的权威行）
    store.backfill_ohlcv_row("510300.SS", "2024-01-02", 5.0, 5.25, 4.85, 999.0)

    idx = pd.to_datetime(["2024-01-02"])
    df = pd.DataFrame({"Close": [5.0], "High": [5.2], "Low": [4.9], "Volume": [100]}, index=idx)
    _fake_yf(monkeypatch, df)

    from scripts.backtest_runner import _warmup_market_data
    _warmup_market_data(["510300.SS"])

    cur = ms.MarketStore().conn.cursor()
    cur.execute("SELECT high, low FROM daily_prices "
                "WHERE symbol = '510300.SS' AND date = '2024-01-02'")
    high, low = cur.fetchone()
    assert high is not None, "已有 OHLC 行被 warmup 冲成 NULL"
    assert low is not None
