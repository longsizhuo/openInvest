"""scripts.purge_close_only_rows：只删行情源没有 bar 的仅收盘价行；默认 dry-run 不改库。"""
from __future__ import annotations

import sqlite3

import scripts.purge_close_only_rows as purge


def _db(tmp_path):
    p = tmp_path / "m.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE daily_prices (symbol TEXT, date TEXT, close REAL, source TEXT, "
              "high REAL, low REAL, volume REAL, PRIMARY KEY (symbol, date))")
    c.executemany("INSERT INTO daily_prices VALUES (?, ?, ?, 'yfinance', ?, ?, NULL)", [
        ("FX=X", "2026-07-03", 7.1, 7.2, 7.0),   # 周五 OHLC
        ("FX=X", "2026-07-04", 7.1, None, None),  # 周六仅收盘 → 删
        ("FX=X", "2026-07-06", 7.1, None, None),  # 周一仅收盘，源有 bar → 留
        ("FUT=F", "2026-07-02", 3.0, 3.1, 2.9),
        ("FUT=F", "2026-07-03", 3.0, None, None),  # 假日 → 删，但源拉不到 → 整个跳过
        ("NAV.AX", "2026-07-04", 9.0, None, None),  # 整列无 H/L 的纯收盘价序列 → 不碰
    ])
    c.commit()
    c.close()
    return p


def _left(p):
    with sqlite3.connect(p) as c:
        return sorted(c.execute("SELECT symbol, date FROM daily_prices"))


def test_dry_run_then_apply(tmp_path, monkeypatch):
    p = _db(tmp_path)
    monkeypatch.setattr(purge, "source_dates", lambda sym, dates: (
        {"2026-07-03", "2026-07-06"} if sym == "FX=X" else None))
    before = _left(p)
    purge.main(["--db", str(p)])
    assert _left(p) == before
    purge.main(["--db", str(p), "--apply"])
    assert set(before) - set(_left(p)) == {("FX=X", "2026-07-04")}
