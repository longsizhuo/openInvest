"""今天的盘中 bar 要定期再刷；Yahoo 给 NaN 收盘的日子用 60m bar 重建。

生产实例：某 A 股 ETF 早盘第一次调用落库了一根半截 bar（量只有全天的约 3.5%），同一天后面的
调用一直拿它当收盘价；次日 5d 刷新时 Yahoo 的正式日线 Close=NaN，被数据源闸跳过，半截 bar
永久留在库里，委员会读到 RVOL 0.17（"缩量"），真实是 20 日均量的 4.85 倍。
"""
from __future__ import annotations

import time
from datetime import datetime

import pandas as pd
import pytest

import openinvest.utils.exchange_fee as ef

SYM = "510300.SS"
TZ = "Asia/Shanghai"


def _freeze(monkeypatch, y, m, d, hh):
    class _FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(y, m, d, hh, 0)
    monkeypatch.setattr(ef, "datetime", _FakeDT)


@pytest.fixture
def store(monkeypatch, tmp_path):
    import openinvest.db.market_store as ms_mod
    monkeypatch.setattr(ms_mod, "DB_PATH", str(tmp_path / "market.db"))
    s = ms_mod.MarketStore()
    monkeypatch.setattr(ef, "_STORE", s)
    return s


def _seed(store, last_day, last_bar):
    days = pd.bdate_range(end=last_day, periods=300)
    for d in days[:-1]:
        store.save_generic_price(SYM, d.strftime("%Y-%m-%d"), 4.0, high=4.1, low=3.9, volume=7.8e8)
    store.save_generic_price(SYM, days[-1].strftime("%Y-%m-%d"), **last_bar)
    return days


class _Ticker:
    """yf.Ticker 替身：按 (period, interval) 返回给定的 DataFrame，记录调用。"""

    def __init__(self, daily, hourly=None, hourly_error=None, daily_error=None):
        self.daily, self.hourly, self.calls = daily, hourly, []
        self.hourly_error, self.daily_error = hourly_error, daily_error

    def history(self, period="1mo", interval="1d", **kw):
        self.calls.append((period, interval))
        if interval == "60m":
            if self.hourly_error:
                raise self.hourly_error
            return self.hourly
        if self.daily_error:
            raise self.daily_error
        return self.daily


def _daily(days, closes, volumes):
    idx = pd.DatetimeIndex(days).tz_localize(TZ)
    return pd.DataFrame({"Open": closes, "High": [c * 1.01 if c == c else c for c in closes],
                         "Low": [c * 0.99 if c == c else c for c in closes], "Close": closes,
                         "Volume": volumes}, index=idx)


PARTIAL = {"close": 4.252, "high": 4.265, "low": 4.247, "volume": 1.34e8}


def test_today_bar_refreshes_after_ttl(store, monkeypatch):
    _freeze(monkeypatch, 2026, 10, 15, 16)
    days = _seed(store, "2026-10-15", PARTIAL)
    tk = _Ticker(_daily(days[-5:], [4.0, 4.0, 4.0, 4.0, 4.234], [7.8e8] * 4 + [3.8e9]))
    monkeypatch.setattr(ef.yf, "Ticker", lambda s: tk)

    store.mark_fetched(SYM)                                    # 刚刷过 → TTL 内不再拉
    assert ef.get_history_data(SYM)["Close"].iloc[-1] == 4.252 and tk.calls == []

    store.conn.execute("UPDATE fetch_log SET fetched_at = ?", (time.time() - ef._TODAY_BAR_TTL_S - 60,))
    store.conn.commit()
    df = ef.get_history_data(SYM)
    assert tk.calls == [("5d", "1d")]
    assert df["Close"].iloc[-1] == 4.234 and df["Volume"].iloc[-1] == 3.8e9

    ef.get_history_data(SYM)                                   # 刷新时间已更新 → 不再拉
    assert tk.calls == [("5d", "1d")]


@pytest.mark.parametrize("tk", [
    _Ticker(pd.DataFrame()),                                   # 被墙 / 限流：返回空
    _Ticker(None, daily_error=RuntimeError("rate limited")),   # 直接抛
], ids=["empty", "error"])
def test_failed_today_refresh_still_respects_ttl(store, monkeypatch, tk):
    """TTL 刷新失败/拉空也记时：库里今天已有 bar，同一 symbol 不会每次调用都再发一次 5d。"""
    _freeze(monkeypatch, 2026, 10, 15, 16)
    _seed(store, "2026-10-15", PARTIAL)
    monkeypatch.setattr(ef.yf, "Ticker", lambda s: tk)

    for _ in range(5):
        df = ef.get_history_data(SYM)
        assert df["Close"].iloc[-1] == 4.252 and df.attrs["yf_fetch_failed"] is True
        assert df.attrs['source_observation']['quote_fresh'] is False
    assert tk.calls == [("5d", "1d")]

    store.conn.execute("UPDATE fetch_log SET fetched_at = ?", (time.time() - ef._TODAY_BAR_TTL_S - 60,))
    store.conn.commit()
    ef.get_history_data(SYM)                                   # TTL 过了才再试一次
    assert tk.calls == [("5d", "1d")] * 2


def _hourly(day, closes, volumes):
    idx = pd.date_range(f"{day} 10:30", periods=len(closes), freq="h", tz=TZ)
    return pd.DataFrame({"Open": closes, "High": [c + 0.01 for c in closes], "Low": [c - 0.02 for c in closes],
                         "Close": closes, "Volume": volumes}, index=idx)


@pytest.mark.parametrize("daily_volume,expected_volume", [
    (3.8e9, 3.8e9),            # 日线自带全天成交量 → 用它（60m 偶有缺根，合计偏低）
    (float("nan"), 3.79e9),    # 日线连量也没有 → 取 60m 合计
])
def test_nan_daily_close_rebuilt_from_hourly_bars(store, monkeypatch, daily_volume, expected_volume):
    _freeze(monkeypatch, 2026, 10, 19, 9)                      # 周一早上，库里最后一根是周五的半截 bar
    days = _seed(store, "2026-10-16", PARTIAL)
    tk = _Ticker(_daily(days[-5:], [4.0, 4.0, 4.0, 4.0, float("nan")], [7.8e8] * 4 + [daily_volume]),
                 hourly=_hourly("2026-10-16", [4.25, 4.27, 4.22, 4.234], [1.0e9, 1.0e9, 0.9e9, 0.89e9]))
    monkeypatch.setattr(ef.yf, "Ticker", lambda s: tk)

    df = ef.get_history_data(SYM)
    last = df.loc["2026-10-16"]
    assert ("5d", "60m") in tk.calls
    assert last["Close"] == 4.234
    assert last["High"] == pytest.approx(4.28) and last["Low"] == pytest.approx(4.20)
    assert last["Volume"] == pytest.approx(expected_volume)
    assert store.get_close_pair_df(SYM).loc["2026-10-16", "CloseRaw"] == 4.234  # ADR-027：重建也写 close_raw


def test_today_nan_close_rebuilt_from_hourly_bars(store, monkeypatch):
    """盘中常见：今天这根日线 Close=NaN → TTL 刷新多发一次 60m，用它改写今天的 bar；TTL 内不再拉。"""
    _freeze(monkeypatch, 2026, 10, 15, 14)
    days = _seed(store, "2026-10-15", PARTIAL)
    tk = _Ticker(_daily(days[-5:], [4.0, 4.0, 4.0, 4.0, float("nan")], [7.8e8] * 4 + [3.8e9]),
                 hourly=_hourly("2026-10-15", [4.25, 4.27, 4.22, 4.234], [1.0e9, 1.0e9, 0.9e9, 0.89e9]))
    monkeypatch.setattr(ef.yf, "Ticker", lambda s: tk)

    df = ef.get_history_data(SYM)
    assert tk.calls == [("5d", "1d"), ("5d", "60m")]
    today = df.loc["2026-10-15"]
    assert today["Close"] == 4.234 and today["Volume"] == pytest.approx(3.8e9)
    assert today["High"] == pytest.approx(4.28) and today["Low"] == pytest.approx(4.20)

    ef.get_history_data(SYM)
    assert tk.calls == [("5d", "1d"), ("5d", "60m")]              # 刚刷过 → 不再拉


def test_hourly_failure_keeps_old_bar(store, monkeypatch):
    _freeze(monkeypatch, 2026, 10, 19, 9)
    days = _seed(store, "2026-10-16", PARTIAL)
    tk = _Ticker(_daily(days[-5:], [4.0, 4.0, 4.0, 4.0, float("nan")], [7.8e8] * 4 + [3.8e9]),
                 hourly_error=RuntimeError("rate limited"))
    monkeypatch.setattr(ef.yf, "Ticker", lambda s: tk)

    df = ef.get_history_data(SYM)
    assert df.loc["2026-10-16"]["Close"] == 4.252                # 重建失败：保持原样，不崩
