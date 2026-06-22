"""get_history_data 首次回填深度：新 symbol 必须拉 2y（修 5d 数据深度 bug）

bug：新加 target_asset 时取价/指标只触发 5d 增量刷新 → DB 仅 5 天数据 →
RSI/MA120/MA250/regime 全 N/A、REGIME=unknown（510500.SS / SPY 复现）。
修法：DB 无该 symbol 历史时首次全量回填 2y，已有历史才走 5d 增量。
"""
from __future__ import annotations

import pandas as pd
import pytest

import utils.exchange_fee as ef


class _RecordingTicker:
    """记录 .history(period=...) 收到的 period，返回空 df（不写库）"""
    last_period = None

    def __init__(self, symbol):
        self.symbol = symbol

    def history(self, period=None, **kwargs):
        _RecordingTicker.last_period = period
        return pd.DataFrame()


@pytest.fixture
def recording_yf(monkeypatch):
    _RecordingTicker.last_period = None
    monkeypatch.setattr(ef.yf, "Ticker", _RecordingTicker)
    return _RecordingTicker


def test_fresh_symbol_backfills_2y(recording_yf, monkeypatch):
    """DB 无该 symbol 历史（empty）→ 首次拉 2y 全量回填"""
    monkeypatch.setattr(ef._STORE, "get_history_df", lambda s: pd.DataFrame())
    ef.get_history_data("FRESH.SS")
    assert recording_yf.last_period == "2y"


def test_shallow_db_backfills_2y(recording_yf, monkeypatch):
    """DB 非空但仅几天（浅）→ 也触发 2y 回填自愈（510500/SPY 复现场景）"""
    # 用明确过去的日期，保证 needs_update 永远为真（不依赖机器"今天"）
    df = pd.DataFrame({"Close": [1.0] * 5},
                      index=pd.to_datetime(["2020-01-01", "2020-01-02",
                                            "2020-01-03", "2020-01-06", "2020-01-07"]))
    monkeypatch.setattr(ef._STORE, "get_history_df", lambda s: df)
    ef.get_history_data("SHALLOW.SS")
    assert recording_yf.last_period == "2y"


def test_deep_symbol_incremental_5d(recording_yf, monkeypatch):
    """DB 已有足够历史（≥250 根）但今天没更新 → 5d 增量刷新即可（不浪费全量拉取）"""
    idx = pd.date_range("2024-01-01", periods=300, freq="D")
    df = pd.DataFrame({"Close": list(range(1, 301))}, index=idx)
    monkeypatch.setattr(ef._STORE, "get_history_df", lambda s: df)
    ef.get_history_data("OLD.SS")
    assert recording_yf.last_period == "5d"


def test_mid_depth_still_backfills_2y(recording_yf, monkeypatch):
    """60~249 根（够 RSI 不够 MA250）→ 仍触发 2y，避免 MA250/regime 残缺"""
    idx = pd.date_range("2025-01-01", periods=100, freq="D")
    df = pd.DataFrame({"Close": list(range(1, 101))}, index=idx)
    monkeypatch.setattr(ef._STORE, "get_history_df", lambda s: df)
    ef.get_history_data("MID.SS")
    assert recording_yf.last_period == "2y"
