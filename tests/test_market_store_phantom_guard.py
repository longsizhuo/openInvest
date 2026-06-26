"""写库 chokepoint 的幽灵周末 bar 后挡(数据准确性 bug 修复 2026-06):
weekday 交易标的(股/ETF/指数/期货)的周末 bar 是 tz 错位幽灵(收盘=次日价→未来泄漏),
必须在写库入口拦掉;FX(=X,~24/5)/加密(-USD,24/7)真实有周末报价,放行。
"""
from db.market_store import _is_phantom_weekend, MarketStore


def test_classification():
    # weekday 标的的周末 = 幽灵
    assert _is_phantom_weekend("510300.SS", "2025-01-04")   # Sat
    assert _is_phantom_weekend("NDQ.AX", "2025-01-05")       # Sun
    assert _is_phantom_weekend("GC=F", "2025-01-04")         # 期货也不周末交易
    assert _is_phantom_weekend("AAPL", "2025-01-05")
    # weekday 正常日 = 放行
    assert not _is_phantom_weekend("510300.SS", "2025-01-03")  # Fri
    # FX / 加密豁免(周末合法)
    assert not _is_phantom_weekend("USDCNY=X", "2025-01-04")
    assert not _is_phantom_weekend("AUDCNY=X", "2025-01-05")
    assert not _is_phantom_weekend("BTC-USD", "2025-01-04")
    # 脏输入不炸
    assert not _is_phantom_weekend("X.SS", "not-a-date")


def test_write_methods_reject_phantom_keep_fx():
    ms = MarketStore()
    eq, fx, sat = "ZZGUARD.SS", "ZZGUARD=X", "2025-01-04"  # Saturday
    try:
        # 股票周末 → backfill 跳过、不写
        assert ms.backfill_ohlcv_row(eq, sat, 1.0, 1.0, 1.0, 0.0) == "skipped_weekend"
        df = ms.get_history_df(eq, days=100000)
        assert df is None or df.empty, "股票周末幽灵 bar 不该写进库"
        # 股票周末 → save_generic_price 也跳过
        ms.save_generic_price(eq, sat, 1.0)
        df = ms.get_history_df(eq, days=100000)
        assert df is None or df.empty
        # FX 周末 → 放行(写进去)
        ms.save_generic_price(fx, sat, 7.0)
        df2 = ms.get_history_df(fx, days=100000)
        assert df2 is not None and not df2.empty, "FX 周末报价应放行"
    finally:
        ms.conn.execute("DELETE FROM daily_prices WHERE symbol IN (?, ?)", (eq, fx))
        ms.conn.commit()
