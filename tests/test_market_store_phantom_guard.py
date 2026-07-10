"""写库 chokepoint 的幽灵周末 bar 后挡(数据准确性 bug 修复 2026-06):
weekday 交易标的(股/ETF/指数/期货)的周末 bar 是 tz 错位幽灵(收盘=次日价→未来泄漏),
必须在写库入口拦掉;FX(=X,~24/5)/加密(-USD,24/7)真实有周末报价,放行。
"""
from openinvest.db.market_store import _is_phantom_weekend, MarketStore


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


def test_splice_sentinel_full_refetch_on_adjust_change(monkeypatch):
    """issue #179 P1-A④：5d 增量与 DB 重叠日 close 差 >1%（分红/拆股复权基准变化）
    ⇒ 升级 2y 全量重取，绝不在缝合点留下断裂序列。"""
    import pandas as pd
    import openinvest.utils.exchange_fee as ef

    ms = MarketStore()
    sym = "ZZSPLICE.AX"  # 同文件惯例：真 DB + 一次性 symbol + finally 清理
    calls = []

    class FakeTicker:
        def __init__(self, s):
            pass

        def history(self, period):
            calls.append(period)
            if period == "5d":
                # 两个重叠日（07-06/07-07）的 close 都按新复权基准掉了 ~5%
                # （复权变化 = 全部重叠日同时偏移；单日偏移是 NAV 兜底混写，不触发）
                return pd.DataFrame(
                    {"Close": [95.0, 95.95, 96.9]},
                    index=pd.to_datetime(["2026-07-06", "2026-07-07", "2026-07-08"]),
                )
            return pd.DataFrame(  # 2y 全量：整段新基准
                {"Close": [94.0, 95.95, 96.9]},
                index=pd.to_datetime(["2026-07-06", "2026-07-07", "2026-07-08"]),
            )

    try:
        # DB 旧复权基准：两个工作日（避开周末闸）
        ms.save_generic_price(sym, "2026-07-06", 100.0)
        ms.save_generic_price(sym, "2026-07-07", 101.0)
        monkeypatch.setattr(ef, "_STORE", ms)
        monkeypatch.setattr(ef.yf, "Ticker", FakeTicker)
        monkeypatch.setattr(ef, "_MIN_HISTORY_ROWS", 2)  # 2 行 DB 即走 5d 增量分支

        ef.get_history_data(sym, "2y")
        assert calls == ["5d", "2y"], f"哨兵未触发 2y 重取: {calls}"
        df = ms.get_history_df(sym, days=100000)
        # 全序列已自愈到新基准（2026-07-06 被 2y 数据覆盖为 94.0）
        assert float(df["Close"].loc["2026-07-06"]) == 94.0
    finally:
        ms.conn.execute("DELETE FROM daily_prices WHERE symbol = ?", (sym,))
        ms.conn.commit()
