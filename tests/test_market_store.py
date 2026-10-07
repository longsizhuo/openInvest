

def test_wal_truncated_on_new_connection(tmp_path, monkeypatch):
    """#104：进程启动（新 MarketStore）自动 wal_checkpoint(TRUNCATE) 回收膨胀。"""
    import os
    import openinvest.db.market_store as ms_mod
    db = tmp_path / "m.db"
    monkeypatch.setattr(ms_mod, "DB_PATH", str(db))
    s1 = ms_mod.MarketStore()
    for i in range(300):
        s1.save_generic_price("T", f"2026-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}", 1.0 + i)
    wal = db.with_name(db.name + "-wal")
    assert wal.exists() and wal.stat().st_size > 0
    s2 = ms_mod.MarketStore()  # 新连接 init 应截断 WAL
    assert wal.stat().st_size == 0


def test_close_only_write_keeps_backfilled_ohlcv(tmp_path, monkeypatch):
    """#231：只传 close 的写入（gold_price 现价缓存 / betashares NAV 兜底）不得把
    同日已回填的 high/low/volume 冲成 NULL（旧 INSERT OR REPLACE 会，ATR/RVOL 静默退化）。"""
    import openinvest.db.market_store as ms_mod
    monkeypatch.setattr(ms_mod, "DB_PATH", str(tmp_path / "m.db"))
    s = ms_mod.MarketStore()
    s.save_generic_price("GC=F", "2026-10-06", 4189.6, high=4212.4, low=4130.7, volume=119331.0)
    s.save_generic_price("GC=F", "2026-10-06", 4190.0)  # gold_price 同日 close-only 刷新
    s.save_ndq_snapshot("2026-10-06", 55.0, {}, [], [])  # betashares NAV 同款
    s.save_generic_price("NDQ.AX", "2026-10-06", 54.9, high=55.5, low=54.1, volume=1e5)
    s.save_ndq_snapshot("2026-10-06", 55.0, {}, [], [])
    rows = dict(
        (r[0], r[1:]) for r in s.conn.execute(
            "SELECT symbol, close, source, high, low, volume FROM daily_prices")
    )
    assert rows["GC=F"] == (4190.0, "yfinance", 4212.4, 4130.7, 119331.0)  # close 后写者赢，OHLCV 保留
    assert rows["NDQ.AX"] == (55.0, "betashares_scraper", 55.5, 54.1, 1e5)
    # 新值非 NULL 时照常覆盖（日常全 OHLCV 刷新）
    s.save_generic_price("GC=F", "2026-10-06", 4191.0, high=4220.0, low=4100.0, volume=2.0)
    assert s.conn.execute(
        "SELECT close, high, low, volume FROM daily_prices WHERE symbol='GC=F'"
    ).fetchone() == (4191.0, 4220.0, 4100.0, 2.0)
