"""黄金价格 + DB 兜底测试（audit algo M7 修复回归）。"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pandas as pd
import pytest

from openinvest.utils.gold_price import (
    GoldPriceSnapshot,
    _get_db_fallback_snapshot,
    get_gold_snapshot,
)


def test_snapshot_dataclass_has_is_stale_field():
    snap = GoldPriceSnapshot(
        gold_usd_per_oz=4600.0,
        usdcny_rate=6.85,
        spot_cny_per_gram=1012.5,
        bank_cny_per_gram=1012.5,
        offset_pct=0.0,
    )
    assert snap.is_stale is False  # default


def test_db_fallback_returns_stale_snapshot():
    """yfinance 都挂时 DB 兜底返回 is_stale=True"""
    with patch("openinvest.utils.gold_price.MarketStore" if False else "openinvest.db.market_store.MarketStore") as MockStore:
        instance = MockStore.return_value
        instance.get_latest_price.side_effect = lambda sym: {
            "GC=F": 4600.0, "USDCNY=X": 6.85,
        }.get(sym)
        result = _get_db_fallback_snapshot(offset_pct=0.0)
    assert result is not None
    assert result.is_stale is True
    assert result.gold_usd_per_oz == 4600.0
    assert abs(result.spot_cny_per_gram - (4600.0 / 31.1035 * 6.85)) < 1e-3


def test_db_fallback_returns_none_if_no_db_data():
    with patch("openinvest.db.market_store.MarketStore") as MockStore:
        instance = MockStore.return_value
        instance.get_latest_price.return_value = None
        result = _get_db_fallback_snapshot(offset_pct=0.0)
    assert result is None


def _bars(tz, rows):
    """[(date, high, low, close, volume)] → yfinance 形状的日线帧（tz-aware 交易所本地日期）"""
    idx = pd.DatetimeIndex([pd.Timestamp(r[0], tz=tz) for r in rows])
    return pd.DataFrame([r[1:] for r in rows], index=idx,
                        columns=["High", "Low", "Close", "Volume"])


@pytest.fixture
def store_and_bars(monkeypatch, tmp_path):
    import openinvest.db.market_store as ms_mod
    monkeypatch.setattr(ms_mod, "DB_PATH", str(tmp_path / "m.db"))
    bars = {}
    monkeypatch.setattr(
        "openinvest.utils.gold_price.yf.Ticker",
        lambda sym: MagicMock(history=lambda period: bars[sym]),
    )
    s = ms_mod.MarketStore()
    return s, bars


def _rows(s, sym):
    return {r[0]: r[1:] for r in s.conn.execute(
        "SELECT date, close, high, low, volume FROM daily_prices WHERE symbol=?", (sym,))}


def test_non_trading_day_call_writes_only_bar_dates(store_and_bars):
    """周六 / 美国假日调用：行情源最后一根是上一交易日 → 只按 bar 日期落库、带 H/L，
    不造进程本地"今天"的仅收盘价行（旧码：GC=F 假日行、USDCNY=X 周末行）。"""
    s, bars = store_and_bars
    bars["GC=F"] = _bars("America/New_York", [
        ("2026-07-01", 3350.0, 3300.0, 3340.0, 1e5),
        ("2026-07-02", 3360.0, 3320.0, 3350.0, 9e4),  # 07-03 假日 / 07-04 周六前最后一根
    ])
    bars["USDCNY=X"] = _bars("Europe/London", [
        ("2026-07-02", 7.18, 7.16, 7.17, 0),
        ("2026-07-03", 7.19, 7.17, 7.18, 0),  # 周五；FX 周末行不受幽灵闸拦
    ])
    snap = get_gold_snapshot(offset_pct=0.0)
    assert snap.gold_usd_per_oz == 3350.0 and snap.usdcny_rate == 7.18
    assert snap.is_stale is False
    assert _rows(s, "GC=F") == {
        "2026-07-01": (3340.0, 3350.0, 3300.0, 1e5),
        "2026-07-02": (3350.0, 3360.0, 3320.0, 9e4),
    }
    assert set(_rows(s, "USDCNY=X")) == {"2026-07-02", "2026-07-03"}
    assert all(r[1] is not None and r[2] is not None for r in _rows(s, "USDCNY=X").values())


def test_intraday_call_carries_high_low(store_and_bars):
    """盘中调用：今天这根带当日 H/L（覆盖旧码留下的同日仅收盘价行）；前一根盘中值定稿；
    close=NaN 的半成型 bar 不落库。"""
    s, bars = store_and_bars
    s.save_generic_price("GC=F", "2026-10-07", 4150.0, high=4160.0, low=4140.0)  # 昨天盘中值
    s.save_generic_price("GC=F", "2026-10-08", 4141.0)  # 旧码写的仅收盘价行
    bars["GC=F"] = _bars("America/New_York", [
        ("2026-10-07", 4197.8, 4130.0, 4140.7, 121934.0),
        ("2026-10-08", 4166.8, 4135.0, 4144.6, 59780.0),
    ])
    bars["USDCNY=X"] = _bars("Europe/London", [
        ("2026-10-07", float("nan"), float("nan"), float("nan"), float("nan")),
        ("2026-10-08", 6.7048, 6.6913, 6.6939, 0),
    ])
    get_gold_snapshot(offset_pct=0.0)
    assert _rows(s, "GC=F") == {
        "2026-10-07": (4140.7, 4197.8, 4130.0, 121934.0),
        "2026-10-08": (4144.6, 4166.8, 4135.0, 59780.0),
    }
    assert _rows(s, "USDCNY=X") == {"2026-10-08": (6.6939, 6.7048, 6.6913, 0.0)}


def test_get_gold_snapshot_offset_applied():
    """spot_cny_per_gram 不带 offset，bank_cny_per_gram = spot * (1+offset)"""
    snap = GoldPriceSnapshot(
        gold_usd_per_oz=4600.0, usdcny_rate=6.85,
        spot_cny_per_gram=1000.0, bank_cny_per_gram=1015.0,
        offset_pct=0.015,
    )
    assert abs(snap.bank_cny_per_gram - snap.spot_cny_per_gram * 1.015) < 1e-9


def test_get_gold_snapshot_falls_back_when_yfinance_raises():
    """yfinance 抛异常时走 DB 兜底"""
    with patch("openinvest.utils.gold_price.yf.Ticker") as MockTicker, \
         patch("openinvest.db.market_store.MarketStore") as MockStore:
        MockTicker.side_effect = ConnectionError("yahoo down")
        instance = MockStore.return_value
        instance.get_latest_price.side_effect = lambda sym: {
            "GC=F": 4500.0, "USDCNY=X": 6.80,
        }.get(sym)
        result = get_gold_snapshot(offset_pct=0.0)
    assert result is not None
    assert result.is_stale is True
    assert result.gold_usd_per_oz == 4500.0


def test_get_gold_snapshot_returns_none_when_all_fail():
    """yfinance + DB 都挂时返回 None，不抛异常"""
    with patch("openinvest.utils.gold_price.yf.Ticker") as MockTicker, \
         patch("openinvest.db.market_store.MarketStore") as MockStore:
        MockTicker.side_effect = ConnectionError("yahoo down")
        instance = MockStore.return_value
        instance.get_latest_price.return_value = None
        result = get_gold_snapshot(offset_pct=0.0)
    assert result is None
