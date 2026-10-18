"""ADR-027 PR2：行情库 close_raw 列 + 抓取口径 + backfill_raw_close。全 mock，不联网。

守的契约：
- ``close`` 语义不变：抓取改成 auto_adjust=False 后落库的 close / high / low 与改前（auto_adjust=True）逐位相等
  （regime / 技术指标 / forward_return 一路读 close，改它就是改生产行为）；close_raw 落 yfinance 的原始 Close
- 老库迁移补 close_raw 列，幂等；只写 close 的写入者不抹掉已有 close_raw
- backfill_raw_close 只 UPDATE 已有行，场外基金 / 黄金克价代理不回填，库里缺的日子报 null_rows
"""
from __future__ import annotations

import sqlite3

import numpy as np
import pandas as pd
import pytest
from yfinance import utils as yf_utils

import openinvest.db.market_store as ms_mod
import openinvest.utils.exchange_fee as ef
from openinvest.db.market_store import MarketStore
from openinvest.utils.exchange_fee import adjusted_with_raw

IDX = pd.to_datetime(["2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09"])


def _raw_frame(adj_close_missing_on=None) -> pd.DataFrame:
    """auto_adjust=False 的 yfinance 帧：07-08 起有一次分红（Adj Close / Close 比值在分红前 0.98）。"""
    df = pd.DataFrame({
        "Open": [100.0, 101.3, 99.9, 100.2],
        "High": [102.1, 103.7, 101.0, 101.9],
        "Low": [99.3, 100.1, 98.6, 99.0],
        "Close": [101.0, 100.7 + 1 / 3, 100.1, 101.3],
        "Adj Close": [98.98, 98.69, 100.1, 101.3],
        "Volume": [1000, 2000, 3000, 4000],
        "Dividends": [0.0, 0.0, 0.0, 0.0],
        "Stock Splits": [0.0, 0.0, 0.0, 0.0],
    }, index=IDX)
    if adj_close_missing_on is not None:
        df.loc[adj_close_missing_on, "Adj Close"] = np.nan
    return df


# ---------- adjusted_with_raw ----------

def test_adjusted_part_is_bit_identical_to_yfinance_auto_adjust():
    """「旧 close」= history(auto_adjust=True)。同一份 auto_adjust=False 帧，我们的复权部分必须和
    yfinance.utils.auto_adjust 逐位相同（含 NaN 行），close_raw = 原始 Close。"""
    for df in (_raw_frame(), _raw_frame(adj_close_missing_on=IDX[1])):
        out = adjusted_with_raw(df)
        pd.testing.assert_frame_equal(out.drop(columns=["CloseRaw"]), yf_utils.auto_adjust(df), check_exact=True)
        pd.testing.assert_series_equal(out["CloseRaw"], df["Close"], check_names=False, check_exact=True)


def test_frame_without_adj_close_is_not_adjusted_and_raw_equals_close():
    """指数 / 期货 / FX 的 Yahoo 响应没有 adjclose → 不复权，CloseRaw == Close。空帧原样返回。"""
    df = _raw_frame().drop(columns=["Adj Close"])
    out = adjusted_with_raw(df)
    pd.testing.assert_series_equal(out["Close"], df["Close"], check_exact=True)
    pd.testing.assert_series_equal(out["CloseRaw"], df["Close"], check_names=False, check_exact=True)
    assert adjusted_with_raw(pd.DataFrame()).empty


# ---------- close 改前改后逐位相等（落库） ----------

class _FakeYfTicker:
    """像 yfinance：auto_adjust=True → 复权帧；False → 原始帧（带 Adj Close）。记录收到的 kwargs。"""
    calls: list = []

    def __init__(self, symbol):
        self.symbol = symbol

    def history(self, period=None, auto_adjust=True, **kw):
        _FakeYfTicker.calls.append({"period": period, "auto_adjust": auto_adjust})
        raw = _raw_frame()
        return yf_utils.auto_adjust(raw) if auto_adjust else raw


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(ms_mod, "DB_PATH", str(tmp_path / "market_data.db"))
    s = MarketStore()
    monkeypatch.setattr(ef, "_STORE", s)
    _FakeYfTicker.calls = []
    monkeypatch.setattr(ef.yf, "Ticker", _FakeYfTicker)
    return s


def test_fetch_path_close_unchanged_and_close_raw_written(store):
    sym = "ZZRAW.AX"
    ef.get_history_data(sym, "2y")
    assert _FakeYfTicker.calls and all(c["auto_adjust"] is False for c in _FakeYfTicker.calls)

    old = yf_utils.auto_adjust(_raw_frame())          # 改前落库的就是这份（history 默认 auto_adjust=True）
    df = store.get_history_df(sym, days=100000)
    for col in ("Close", "High", "Low"):
        assert (df[col].to_numpy() == old[col].to_numpy()).all(), f"{col} 与改前不逐位相等"
    pair = store.get_close_pair_df(sym)
    assert (pair["CloseRaw"].to_numpy() == _raw_frame()["Close"].to_numpy()).all()
    assert (pair["Close"].to_numpy() == old["Close"].to_numpy()).all()


# ---------- close_raw 列：迁移 / 写入语义 ----------

def test_migration_adds_close_raw_to_old_db_idempotently(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE daily_prices (symbol TEXT, date TEXT, close REAL, source TEXT, "
                "high REAL, low REAL, volume REAL, PRIMARY KEY (symbol, date))")
    con.execute("INSERT INTO daily_prices VALUES ('OLD', '2026-01-05', 10.0, 'yfinance', 11, 9, 5)")
    con.commit()
    con.close()
    monkeypatch.setattr(ms_mod, "DB_PATH", str(path))

    s = MarketStore()
    cols = [r[1] for r in s.conn.execute("PRAGMA table_info(daily_prices)")]
    assert "close_raw" in cols
    pair = s.get_close_pair_df("OLD")
    assert float(pair["Close"].iloc[0]) == 10.0 and pd.isna(pair["CloseRaw"].iloc[0])   # 老行 NULL，close 没动
    MarketStore()   # 再启动一次：列已在，不报错
    s._migrate_ohlcv_columns()


def test_close_only_writer_keeps_existing_close_raw(store):
    store.save_generic_price("ZZ.AX", "2026-07-07", 100.0, close_raw=101.0)
    store.save_generic_price("ZZ.AX", "2026-07-07", 99.0, source="betashares_fallback")   # 只知道 close 的写入者
    row = store.get_close_pair_df("ZZ.AX").iloc[0]
    assert float(row["Close"]) == 99.0 and float(row["CloseRaw"]) == 101.0


def test_backfill_ohlcv_row_sets_close_raw_without_touching_close(store):
    store.save_generic_price("ZZ.AX", "2026-07-07", 100.0)
    assert store.backfill_ohlcv_row("ZZ.AX", "2026-07-07", 55.0, 1, 2, 3, close_raw=101.0) == "updated"
    row = store.get_close_pair_df("ZZ.AX").iloc[0]
    assert float(row["Close"]) == 100.0 and float(row["CloseRaw"]) == 101.0
    assert store.backfill_ohlcv_row("ZZ.AX", "2026-07-08", 98.0, 1, 2, 3, close_raw=99.0) == "inserted"
    assert float(store.get_close_pair_df("ZZ.AX")["CloseRaw"].iloc[-1]) == 99.0


def test_get_history_df_columns_unchanged(store):
    """close_raw 不进 get_history_df：regime / 指标层的列契约（列存在 ⟺ 有数据）不变。"""
    store.save_generic_price("ZZ.AX", "2026-07-07", 100.0, high=101.0, low=99.0, volume=5.0, close_raw=101.0)
    assert list(store.get_history_df("ZZ.AX").columns) == ["Close", "High", "Low", "Volume"]


# ---------- backfill_raw_close ----------

def test_backfill_raw_close_updates_existing_rows_only(store, monkeypatch):
    from openinvest.services import raw_close

    for d, c in (("2026-07-06", 98.98), ("2026-07-07", 98.69), ("2026-07-08", 100.1)):
        store.save_generic_price("ZZ.AX", d, c)
    store.save_generic_price("ZZ.AX", "2026-07-09", 77.0, source="betashares_scraper")   # yfinance 没有这天
    monkeypatch.setattr(raw_close, "_fetch_raw", lambda s: [
        ("2026-07-01", 100.0),     # 库里没有 → 不新增
        ("2026-07-06", 101.0), ("2026-07-07", 100.7), ("2026-07-08", 100.1)])

    out = raw_close.backfill_raw_close(["ZZ.AX", "FUND:000001"], store=store)
    assert out["symbols"]["ZZ.AX"] == {"status": "ok", "db_rows": 4, "updated": 3,
                                      "yfinance_days_not_in_db": 1, "null_rows": 1}
    assert "FUND:000001" in out["skipped"]
    pair = store.get_close_pair_df("ZZ.AX")
    assert list(pair["Close"]) == [98.98, 98.69, 100.1, 77.0]        # close 一个字没动
    assert list(pair["CloseRaw"].iloc[:3]) == [101.0, 100.7, 100.1] and pd.isna(pair["CloseRaw"].iloc[3])
    assert raw_close.backfill_raw_close(["ZZ.AX"], store=store)["symbols"]["ZZ.AX"]["updated"] == 3   # 幂等


def test_backfill_raw_close_reports_failures_and_continues(store, monkeypatch):
    from openinvest.services import raw_close

    store.save_generic_price("GOOD", "2026-07-06", 1.0)

    def fetch(sym):
        if sym == "BAD":
            raise RuntimeError("boom")
        return [] if sym == "EMPTY" else [("2026-07-06", 1.5)]

    monkeypatch.setattr(raw_close, "_fetch_raw", fetch)
    out = raw_close.backfill_raw_close(["BAD", "EMPTY", "GOOD"], store=store)
    assert out["status"] == "partial"
    assert out["symbols"]["BAD"]["status"] == "error" and out["symbols"]["EMPTY"]["status"] == "no_data"
    assert out["symbols"]["GOOD"]["status"] == "ok"


def test_default_symbols_skip_funds_and_gold_proxy(tmp_path):
    from openinvest.core.memory_store import MemoryStore
    from openinvest.core.portfolio_manager import PortfolioManager
    from openinvest.services.raw_close import SKIP_FUND, SKIP_GOLD, default_symbols

    s = MemoryStore(tmp_path / "memory")
    s.write("user", "user", {"display_name": "T", "risk_tolerance": "Balanced"}, "")
    s.write("strategy", "strategy", {"target_assets": [
        {"symbol": "SPY", "max_single_invest_cny": 1000},
        {"symbol": "NDQ.AX", "max_single_invest_cny": 1000},       # 持仓里已有，去重
        {"symbol": "GC=F", "max_single_invest_cny": 1000},         # 同名 target 行没有 proxy_kind：仍按持仓判的黄金跳过
    ]}, "")
    s.write("portfolio", "state", {"schema_version": 2, "cash": {"CNY": 1}, "holdings": [
        {"symbol": "NDQ.AX", "units": 1, "avg_cost": 1, "cost_currency": "AUD"},
        {"symbol": "GC=F", "units": 1, "avg_cost": 1, "cost_currency": "CNY", "proxy_kind": "gold_cny_per_gram"},
        {"symbol": "FUND:000001", "kind": "fund", "units": 1, "avg_cost": 1, "cost_currency": "CNY"},
        {"symbol": "TRACK", "is_tracking_only": True},
    ]}, "")
    wanted, skipped = default_symbols(PortfolioManager(s))
    assert wanted == ["NDQ.AX", "SPY"]
    assert skipped == {"GC=F": SKIP_GOLD, "FUND:000001": SKIP_FUND}


def test_cli_subcommand_passes_symbols_through(monkeypatch, capsys):
    from openinvest import cli
    from openinvest.services import raw_close

    seen = {}
    monkeypatch.setattr(raw_close, "backfill_raw_close", lambda symbols=None, **kw: (seen.update(s=symbols), {"status": "ok"})[1])
    args = cli.build_parser().parse_args(["backfill_raw_close", "--symbols", "NDQ.AX,SPY"])
    args.func(args)
    assert seen["s"] == ["NDQ.AX", "SPY"]
    args = cli.build_parser().parse_args(["backfill_raw_close"])
    args.func(args)
    assert seen["s"] is None
