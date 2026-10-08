"""今天这根只有 close（close-only 写入者先写）→ get_history_data 仍要补刷一次 OHLCV。

bug（2026-10）：gold_price 现价缓存先给 USDCNY=X / GC=F 写了今天的 close-only 行 →
freshness 判"已是今天"，5d 刷新整天跳过，H/L/Volume 一直 NULL（生产 USDCNY=X 近 60 天
22 根），ATR/RVOL 静默退化成收盘价差。修：今天这根缺 H/L 也刷，每进程每 symbol 每天一次。

真 tmp SQLite MarketStore；用 FX symbol（豁免周末幽灵闸，不依赖今天星期几）。
"""
from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd
import pytest

import openinvest.utils.exchange_fee as ef

SYM = "USDCNY=X"


class _FakeTicker:
    calls = 0
    bars: pd.DataFrame = pd.DataFrame()

    def __init__(self, symbol):
        pass

    def history(self, period=None, **kwargs):
        _FakeTicker.calls += 1
        return _FakeTicker.bars


@pytest.fixture
def store(monkeypatch, tmp_path):
    import openinvest.db.market_store as ms_mod

    monkeypatch.setattr(ms_mod, "DB_PATH", str(tmp_path / "market.db"))
    s = ms_mod.MarketStore()
    monkeypatch.setattr(ef, "_STORE", s)
    monkeypatch.setattr(ef, "_OHLCV_GAP_TRIED", {})
    monkeypatch.setattr(ef.yf, "Ticker", _FakeTicker)
    _FakeTicker.calls = 0
    today = pd.Timestamp(datetime.now().strftime("%Y-%m-%d"))
    # 300 根带 H/L 的历史（≥250 → 走 5d 增量）+ 今天 close-only（gold_price 先写的）
    for d in pd.date_range(end=today - pd.Timedelta(days=1), periods=300):
        s.save_generic_price(SYM, d.strftime("%Y-%m-%d"), 7.0, high=7.1, low=6.9, volume=0.0)
    s.save_generic_price(SYM, today.strftime("%Y-%m-%d"), 7.2)
    return s, today


def _today_row(s, today):
    return s.conn.execute(
        "SELECT close, high, low, volume FROM daily_prices WHERE symbol=? AND date=?",
        (SYM, today.strftime("%Y-%m-%d")),
    ).fetchone()


def test_close_only_today_refreshed_once(store):
    s, today = store
    _FakeTicker.bars = pd.DataFrame(
        {"Close": [7.0, 7.25], "High": [7.1, 7.3], "Low": [6.9, 7.18], "Volume": [0.0, 0.0]},
        index=[today - pd.Timedelta(days=1), today],
    )
    df = ef.get_history_data(SYM, "5d")
    assert _FakeTicker.calls == 1
    assert _today_row(s, today) == (7.25, 7.3, 7.18, 0.0)
    assert df["High"].iloc[-1] == 7.3
    assert df.attrs["yf_fetch_failed"] is False

    ef.get_history_data(SYM, "5d")  # 同进程再调：已是今天且有 H/L → 不再拉
    assert _FakeTicker.calls == 1


def test_unfillable_gap_does_not_loop(store):
    """yfinance 给不出今天的 H/L（NaN）→ 补刷一次后当天不再重拉，不刷屏。"""
    s, today = store
    _FakeTicker.bars = pd.DataFrame(
        {"Close": [7.25], "High": [np.nan], "Low": [np.nan], "Volume": [np.nan]},
        index=[today],
    )
    for _ in range(3):
        df = ef.get_history_data(SYM, "5d")
    assert _FakeTicker.calls == 1
    assert _today_row(s, today)[1] is None
    # 只补 H/L 那次没补到：今天的 close 仍是新的，不标 stale
    assert df.attrs["yf_fetch_failed"] is False


def test_gap_refresh_failure_not_flagged_stale(store):
    s, today = store
    _FakeTicker.bars = pd.DataFrame()
    df = ef.get_history_data(SYM, "5d")
    assert _FakeTicker.calls == 1
    assert df["Close"].iloc[-1] == 7.2
    assert df.attrs["yf_fetch_failed"] is False
