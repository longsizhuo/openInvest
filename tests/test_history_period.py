"""get_history_data 必须按 period 截断（2026-10 根因修复）。

bug：DB 缓存路径无视 period，一律返回 get_history_df 默认 730 行（≈3 年）。取 iloc[0]
的宏观 "MoM"（DXY/TIP/TNX/VIX）实为 ~3 年涨跌——2026-10-07 日报 DXY "-2.29% MoM"
（真 1 月 +3.3%）、TIP "+8.11% → 利好黄金"（真 1 月 -2.6%），黄金货币因素方向整个反了。

真 tmp SQLite MarketStore（不 mock get_history_df），三个数据源都钉：DB 缓存 /
yfinance 刷新后回读 / as_of_date 回测截断。
"""
from __future__ import annotations

import re

import numpy as np
import pandas as pd
import pytest

import openinvest.utils.exchange_fee as ef

# 固定历史日期（不依赖机器"今天"）：last bar 永远 != today → 走 yfinance 刷新分支，
# 被 _NoNetwork 拒掉后回落 DB——DB 缓存路径即被测路径。
_DAYS = pd.bdate_range("2018-01-01", "2023-06-30")


class _NoNetwork:
    def __init__(self, *a, **kw):
        raise RuntimeError("yfinance disabled in test")


@pytest.fixture
def store(monkeypatch, tmp_path):
    import openinvest.db.market_store as ms_mod

    monkeypatch.setattr(ms_mod, "DB_PATH", str(tmp_path / "market.db"))
    s = ms_mod.MarketStore()
    monkeypatch.setattr(ef, "_STORE", s)
    monkeypatch.setattr(ef.yf, "Ticker", _NoNetwork)
    return s


def _seed(store, symbol, closes, days=_DAYS):
    store.conn.executemany(
        "INSERT INTO daily_prices (symbol, date, close, source) VALUES (?, ?, ?, 'test')",
        [(symbol, d.strftime("%Y-%m-%d"), float(c)) for d, c in zip(days, closes)],
    )
    store.conn.commit()


def test_apply_period_semantics():
    from openinvest.calc.timeframe_analysis import _apply_period

    df = pd.DataFrame({"Close": range(len(_DAYS))}, index=_DAYS)
    last = _DAYS[-1]
    assert len(_apply_period(df, "1d")) == 1
    assert list(_apply_period(df, "5d").index) == list(_DAYS[-5:])
    one_mo = _apply_period(df, "1mo")
    assert one_mo.index[0] > last - pd.DateOffset(months=1)
    assert one_mo.index[-1] == last and len(one_mo) <= 23
    assert _apply_period(df, "ytd").index[0] == pd.Timestamp("2023-01-02")
    assert len(_apply_period(df, "max")) == len(df)
    assert _apply_period(df.iloc[:0], "1mo").empty
    with pytest.raises(ValueError):
        _apply_period(df, "3y")


def test_db_cache_path_honors_period(store):
    _seed(store, "AAA", np.arange(len(_DAYS)) + 100.0)
    last = _DAYS[-1]

    assert len(ef.get_history_data("AAA", "1d")) == 1
    assert len(ef.get_history_data("AAA", "5d")) == 5
    one_mo = ef.get_history_data("AAA", "1mo")
    assert one_mo.index[-1] == last
    assert one_mo.index[0] > last - pd.DateOffset(months=1)
    assert len(one_mo) <= 23, f"1mo 返回 {len(one_mo)} 行（旧 bug：730）"
    # 长窗口不再被 730 行封顶（compute_metrics 调用方拉 METRICS_PERIOD=5y 靠这个）
    five_y = ef.get_history_data("AAA", ef.METRICS_PERIOD)
    assert len(five_y) > 730
    assert five_y.index[0] > last - pd.DateOffset(years=5)
    assert len(ef.get_history_data("AAA", "max")) == len(_DAYS)


def test_as_of_path_reads_full_history_then_period(store):
    """as_of 早于"最近 730 行"窗口：旧实现 tail(730) 在 cutoff 之前 → 空 df。"""
    _seed(store, "AAA", np.arange(len(_DAYS)) + 100.0)
    df = ef.get_history_data("AAA", "1y", as_of_date="2019-06-28")
    assert not df.empty
    assert df.index[-1] == pd.Timestamp("2019-06-28")
    assert df.index[0] > pd.Timestamp("2018-06-28")


def test_yfinance_refresh_path_honors_period(store, monkeypatch):
    """yfinance 刷新落库后回读，同样按 period 截断。"""
    old_days = _DAYS[:-2]  # DB 缺最后两天 → 增量拉取
    _seed(store, "AAA", np.arange(len(old_days)) + 100.0, days=old_days)

    class _Ticker:
        def __init__(self, symbol):
            pass

        def history(self, period=None, **kw):
            return pd.DataFrame({"Close": [1.0, 2.0]}, index=_DAYS[-2:])

    monkeypatch.setattr(ef.yf, "Ticker", _Ticker)
    df = ef.get_history_data("AAA", "1mo")
    assert df.index[-1] == _DAYS[-1]
    assert df.index[0] > _DAYS[-1] - pd.DateOffset(months=1)
    assert len(df) <= 23


def _path(start, mid, end, n_tail=22):
    """730 根前=start，→mid，最后 n_tail 根 mid→end：1 月涨跌与旧口径（730 行）方向相反。"""
    n = len(_DAYS)
    return np.concatenate([
        np.full(n - 730, start),
        np.linspace(start, mid, 730 - n_tail),
        np.linspace(mid, end, n_tail),
    ])


def test_macro_block_reports_true_one_month_change(store):
    # 复刻 2026-10-07：DXY 3 年跌、近 1 月涨；TIP 3 年涨、近 1 月跌
    _seed(store, "DX-Y.NYB", _path(104.37, 98.84, 102.07))
    _seed(store, "TIP", _path(96.37, 106.97, 104.18))
    _seed(store, "^TNX", _path(4.57, 4.80, 5.27))
    _seed(store, "^VIX", _path(14.16, 14.53, 15.21))

    def true_mom(sym):
        df = store.get_history_df(sym, days=100000)["Close"]
        base = df[df.index > df.index[-1] - pd.DateOffset(months=1)].iloc[0]
        return df.iloc[-1] / base - 1

    out = ef.get_macro_data()
    dxy = float(re.search(r"DX-Y\.NYB\): [\d.]+ \(MoM: ([+-][\d.]+)%\)", out).group(1)) / 100
    assert dxy > 0, f"DXY 近 1 月在涨，报出 {dxy:+.2%}（旧 bug：~3 年 -2%）"
    assert dxy == pytest.approx(true_mom("DX-Y.NYB"), abs=1e-4)
    tip = float(re.search(r"TIP, MoM\): ([+-][\d.]+)%", out).group(1)) / 100
    assert tip == pytest.approx(true_mom("TIP"), abs=1e-4)
    assert "rising (gold headwind)" in out, "TIP 近 1 月跌 = 实际利率上行 = 黄金逆风"
    for sym, label in (("^TNX", "Treasury Yield"), ("^VIX", "Volatility Index")):
        m = re.search(label + r" \(\^\w+\): [\d.]+%? \(MoM: ([+-][\d.]+)%\)", out)
        assert float(m.group(1)) / 100 == pytest.approx(true_mom(sym), abs=1e-4)

    # MCP / LLM 工具同口径
    from openinvest.capabilities.tools import _impl_get_macro_snapshot
    snap = _impl_get_macro_snapshot()
    assert snap["tip_1mo_pct"] == pytest.approx(true_mom("TIP") * 100, abs=0.01)


def test_macro_agent_tool_reports_requested_window(store):
    """通道 2：Macro agent 的 get_history_data 工具（生产 98% 调 3mo/6mo）按所选窗口返回。"""
    from openinvest.capabilities.tools import _impl_get_history_data

    _seed(store, "TIP", _path(96.37, 106.97, 104.18))
    s = store.get_history_df("TIP", days=100000)["Close"]
    for period, months in (("3mo", 3), ("6mo", 6)):
        win = s[s.index > s.index[-1] - pd.DateOffset(months=months)]
        out = _impl_get_history_data("TIP", period)
        assert out["n_days"] == len(win), f"{period} 返回 {out['n_days']} 根（旧 bug：730）"
        assert out["start_date"] == str(win.index[0].date())
        assert out["cumulative_return_pct"] == pytest.approx((win.iloc[-1] / win.iloc[0] - 1) * 100, abs=0.01)
        assert out["cumulative_return_pct"] < 0, "TIP 近 3/6 月在跌（旧 bug：730 行 +8%）"


def test_verdict_review_forward_return_reaches_old_decisions(store):
    """前向收益价格查询用 "max"：>1 年（也 >730 行）前的决议仍能打分。"""
    from openinvest.jobs.verdict_review import _window_return

    closes = np.arange(len(_DAYS)) + 100.0
    _seed(store, "AAA", closes)
    s = pd.Series(closes, index=_DAYS)
    start = s[s.index >= "2019-01-02"].iloc[0]
    end = s[s.index >= "2019-02-01"].iloc[0]
    assert _window_return("AAA", None, "2019-01-02", 30) == pytest.approx(end / start - 1)
