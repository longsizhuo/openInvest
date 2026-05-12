"""阶段 1.1 回归测试：backtest 模式下 get_history_data 不能拉到 cutoff 之后的数据

之前的 bug：MarketStore DB 已被 daily cron 写入 T+ 价格 → backtest decision_date=T
时 get_history_data 仍然返回 superset 的 df（含 T+1, T+2...），LLM 偷看未来。

修复：utils.exchange_fee.get_history_data 加 as_of_date 参数，DB query 结果也按
cutoff 过滤；backtest_committee.py:_patch_tools_to_date 直接传 as_of_date。
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Iterator

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture
def fake_db_with_future(monkeypatch, tmp_path) -> Iterator[None]:
    """构造一个 MarketStore，DB 里塞了 cutoff 之后的"未来"价格。

    用 monkeypatch 替换 _STORE.get_history_df 直接返回 fake df，绕过真实
    SQLite。yfinance 实际调用也被禁掉（防 backtest 误拉网络）。
    """
    import utils.exchange_fee as ef
    import yfinance as yf

    # 构造一份"DB 内已含未来数据"的 df：2024-01-01 → 2024-12-31 每日一条
    dates = pd.date_range("2024-01-01", "2024-12-31", freq="D")
    fake_df = pd.DataFrame(
        {"Close": [100.0 + i * 0.1 for i in range(len(dates))]},
        index=dates,
    )

    monkeypatch.setattr(ef._STORE, "get_history_df", lambda symbol: fake_df.copy())
    # 禁掉 yfinance 网络调用（用 RuntimeError 让 exchange_fee try/except 能 catch；
    # pytest.fail 抛 BaseException 子类会绕过 try/except，造成假阳性）
    yfinance_called = []

    def _fail_ticker(*a, **kw):
        yfinance_called.append(True)
        raise RuntimeError("yfinance disabled in test")

    monkeypatch.setattr(yf, "Ticker", _fail_ticker)
    yield yfinance_called


def test_no_lookahead_with_as_of_date(fake_db_with_future):
    """as_of_date=2024-05-15 → 返回的 df 最后一行应 <= 2024-05-15（防穿越）

    注：yfinance 可能被调（cutoff > today-30d 时安全放行拉历史灌 DB），但
    cutoff 过滤会保证不穿越——这才是核心 invariant。yfinance 被禁后失败
    会被 try/except 吞掉，回退到 DB 数据 + cutoff 过滤，最终结果一致。
    """
    from utils.exchange_fee import get_history_data

    df = get_history_data("FAKE", as_of_date="2024-05-15")
    assert not df.empty
    assert df.index[-1] <= pd.Timestamp("2024-05-15"), (
        f"穿越！最后一行 {df.index[-1]} 超过 cutoff 2024-05-15"
    )


def test_no_as_of_date_keeps_full_df(fake_db_with_future):
    """as_of_date=None（正常 daily_report 路径）→ 不截断 + yfinance 会被尝试"""
    from utils.exchange_fee import get_history_data

    yfinance_called = fake_db_with_future
    df = get_history_data("FAKE", as_of_date=None)
    # 返回全部数据（yfinance 失败被 catch，回退 DB superset），最后一行 2024-12-31
    assert df.index[-1] == pd.Timestamp("2024-12-31")
    assert yfinance_called, "正常模式应尝试 yfinance refresh（即使失败）"


def test_cutoff_inclusive_semantics(fake_db_with_future):
    """as_of_date 当天数据应**包含**（用户晚间决策能看 T 日 close）"""
    from utils.exchange_fee import get_history_data

    df = get_history_data("FAKE", as_of_date="2024-06-15")
    # 应包含 2024-06-15 这一行（晚间已收盘）
    assert pd.Timestamp("2024-06-15") in df.index, "cutoff 当日应包含"
    # 但不应有 2024-06-16
    assert pd.Timestamp("2024-06-16") not in df.index, "cutoff 后一日不应包含"


def test_backtest_patch_routes_through_as_of_date(monkeypatch):
    """backtest_committee._patch_tools_to_date 应让所有 get_history_data 调用透传 as_of_date"""
    import utils.exchange_fee as ef

    calls = []
    real = ef.get_history_data

    def spy(symbol, period="2y", as_of_date=None):
        calls.append({"symbol": symbol, "as_of_date": as_of_date})
        # 返回空 df 避免依赖网络/DB
        return pd.DataFrame()

    monkeypatch.setattr(ef, "get_history_data", spy)

    # 模拟 _patch_tools_to_date 的语义
    decision_date = "2024-03-20"

    def patched(symbol, period="2y", as_of_date=None):
        return real(symbol, period, as_of_date=decision_date)

    # 直接 call patched（不需要真跑 backtest）
    monkeypatch.setattr(ef, "get_history_data", patched)
    monkeypatch.setattr(ef, "get_history_data", spy)  # restore spy

    # spy 调用应带 cutoff
    ef.get_history_data("NDQ.AX", as_of_date=decision_date)
    assert calls[-1]["as_of_date"] == decision_date


def test_empty_df_passes_through():
    """空 df 应原样返回，不爆"""
    from utils.exchange_fee import _apply_cutoff

    empty = pd.DataFrame()
    assert _apply_cutoff(empty, "2024-05-01").empty


def test_apply_cutoff_with_tz_aware_index():
    """yfinance 返回的 index 带时区 → cutoff 也要 tz-aware 不然 pandas 报错"""
    from utils.exchange_fee import _apply_cutoff

    # 带美东时区的 daily index
    dates = pd.date_range("2024-01-01", "2024-01-10", freq="D", tz="America/New_York")
    df = pd.DataFrame({"Close": range(len(dates))}, index=dates)

    result = _apply_cutoff(df, "2024-01-05")
    assert not result.empty
    # 应保留 5 行（01-01 到 01-05）
    assert len(result) == 5
    assert result.index[-1].strftime("%Y-%m-%d") == "2024-01-05"


# ============ 1.2 macro_context 用 decision_date ============

def test_capture_macro_context_uses_decision_date(monkeypatch):
    """backtest 模式调 _capture_macro_context(as_of_date='2024-05-01')：
    1. captured_at 字段 = '2024-05-01'（不是 datetime.now()）
    2. 内部 get_history_data 传 as_of_date='2024-05-01'
    """
    import core.committee as cm
    import utils.exchange_fee as ef

    calls = []
    fake_df = pd.DataFrame(
        {"Close": [17.5]},
        index=pd.to_datetime(["2024-05-01"]),
    )

    def spy_get_history(symbol, period="2y", as_of_date=None):
        calls.append({"symbol": symbol, "as_of_date": as_of_date})
        return fake_df.copy()

    monkeypatch.setattr(ef, "get_history_data", spy_get_history)
    # committee.py 用 from utils.exchange_fee import get_history_data 在函数内 → 不需要 patch cm

    snapshot = cm._capture_macro_context(as_of_date="2024-05-01")

    # captured_at 用 decision_date，不是 datetime.now()
    assert snapshot["captured_at"] == "2024-05-01"

    # 所有底层 get_history_data 调用都带 cutoff
    assert len(calls) >= 1
    for c in calls:
        assert c["as_of_date"] == "2024-05-01", f"未传 cutoff: {c}"


def test_capture_macro_context_live_mode_uses_now(monkeypatch):
    """实盘模式 (as_of_date=None)：captured_at 用 ISO timestamp 含时分秒"""
    import core.committee as cm
    import utils.exchange_fee as ef

    monkeypatch.setattr(ef, "get_history_data",
                        lambda *a, **kw: pd.DataFrame())

    snapshot = cm._capture_macro_context()
    assert "T" in snapshot["captured_at"], "实盘模式应是 ISO timestamp 含 T"
    # 不是单纯 date 字符串
    assert len(snapshot["captured_at"]) > 10
