"""OHLCV 历史回填脚本（B 组：ATR 真 TR 的数据根）

背景
====
daily_prices 历史上只存 close，导致 market_metrics 的 ATR 在生产中退化为
收盘价差（|ΔClose|），真 TR 分支（含跳空）永远走不到，RVOL 也没数据。
schema 扩了 high/low/volume 列后，**旧行仍是 NULL**，必须回填。

做什么
======
对每个已跟踪 symbol，从 yfinance 拉 2 年 OHLCV，**非破坏式**写回：
- 已有日期：只补 high/low/volume，不动既有 close（保留 betashares NAV 等权威价）
- 新日期：整行 INSERT

用法
====
    uv run python -m scripts.archive.backfill_ohlcv                # 回填 DB 里所有 symbol
    uv run python -m scripts.archive.backfill_ohlcv AAPL GC=F      # 只回填指定 symbol
    uv run python -m scripts.archive.backfill_ohlcv --period 5y    # 自定义回溯期

回填后会抽查每个 symbol 的 high/low/volume 非空比例。
"""
from __future__ import annotations

import sys
import time

import numpy as np
import yfinance as yf

from openinvest.db.market_store import MarketStore


def _nan_to_none(v):
    try:
        fv = float(v)
    except (TypeError, ValueError):
        return None
    return None if np.isnan(fv) else fv


def backfill_symbol(store: MarketStore, symbol: str, period: str = "2y") -> dict:
    """回填单个 symbol 的 OHLCV，返回统计 dict。"""
    stats = {"symbol": symbol, "rows": 0, "updated": 0, "inserted": 0,
             "high_low_filled": 0, "volume_filled": 0, "error": None}
    try:
        df = yf.Ticker(symbol).history(period=period)
    except Exception as e:  # noqa: BLE001
        stats["error"] = f"{type(e).__name__}: {e}"
        return stats
    if df is None or df.empty:
        stats["error"] = "empty yfinance response"
        return stats

    for idx, row in df.iterrows():
        date_str = idx.strftime("%Y-%m-%d")
        close = _nan_to_none(row.get("Close"))
        if close is None:
            continue
        high = _nan_to_none(row.get("High"))
        low = _nan_to_none(row.get("Low"))
        volume = _nan_to_none(row.get("Volume"))
        result = store.backfill_ohlcv_row(symbol, date_str, close, high, low, volume)
        stats["rows"] += 1
        stats[result] += 1
        if high is not None and low is not None:
            stats["high_low_filled"] += 1
        if volume is not None:
            stats["volume_filled"] += 1
    return stats


def _audit(store: MarketStore, symbol: str) -> str:
    """抽查回填结果：最近 30 行里 high/low/volume 非空比例。"""
    df = store.get_history_df(symbol, days=30)
    if df.empty:
        return f"  {symbol}: 无数据"
    has_hl = "High" in df.columns and "Low" in df.columns
    has_vol = "Volume" in df.columns
    return (f"  {symbol}: 近 {len(df)} 行 | High/Low 列={'有' if has_hl else '无'}"
            f" | Volume 列={'有' if has_vol else '无'}")


def main(argv: list[str]) -> int:
    period = "2y"
    symbols: list[str] = []
    i = 0
    while i < len(argv):
        if argv[i] == "--period":
            period = argv[i + 1]
            i += 2
        else:
            symbols.append(argv[i])
            i += 1

    store = MarketStore()
    if not symbols:
        symbols = store.distinct_symbols()

    print(f"📦 回填 OHLCV: {len(symbols)} 个 symbol, period={period}")
    print(f"   symbols: {symbols}\n")

    total_rows = 0
    for sym in symbols:
        st = backfill_symbol(store, sym, period=period)
        if st["error"]:
            print(f"❌ {sym}: {st['error']}")
            continue
        total_rows += st["rows"]
        print(f"✅ {sym}: {st['rows']} 行 "
              f"(更新 {st['updated']} / 新增 {st['inserted']}) | "
              f"High/Low 填 {st['high_low_filled']} | Volume 填 {st['volume_filled']}")
        time.sleep(0.5)  # 轻微限速，别打挂 yfinance

    print("\n🔍 抽查（近 30 行列存在性）：")
    for sym in symbols:
        print(_audit(store, sym))

    print(f"\n📊 合计回填 {total_rows} 行 × {len(symbols)} symbol")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
