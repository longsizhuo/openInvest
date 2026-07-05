

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
