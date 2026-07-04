"""Backfill 最长可得 OHLC 历史到 MarketStore（几十年级，给纯算术概率表/阈值验证用）。

为什么：regime → forward return 分布、crash/recovery 阈值验证、regime sweep ground
truth 都是 **纯算术**（regime 用 MA/ATR/分位，return 用价格），不需要 committee 跑过，
不该被 276 条 verdict_review 限制。几十年 OHLC 直算样本量从几十/几百 → 数千。

非破坏式：已存在的 (symbol, date) 只补 high/low/volume，保留权威 close（如 NDQ.AX
的 betashares NAV）；缺失的历史日整行 insert yfinance close。

用法:
    uv run python scripts/backfill_ohlc.py                # 默认全集（投资标的 + 宏观）
    uv run python scripts/backfill_ohlc.py GC=F NDQ.AX    # 指定 symbol
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yfinance as yf

from openinvest.db.market_store import MarketStore

# 默认 backfill 全集：可投资产 + 宏观因子。period="max" = yfinance 能给的最长。
DEFAULT_SYMBOLS = ["GC=F", "NDQ.AX", "^VIX", "^TNX"]


def _nan_to_none(v):
    try:
        import math
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return None
    except Exception:
        pass
    return None if v is None else float(v)


def backfill_symbol(store: MarketStore, symbol: str) -> dict:
    symbol = symbol.upper()
    print(f"🔄 [yfinance] {symbol} period=max ...")
    df = yf.Ticker(symbol).history(period="max")
    if df.empty:
        print(f"❌ {symbol}: yfinance 空")
        return {"symbol": symbol, "rows": 0}
    inserted = updated = 0
    for idx, row in df.iterrows():
        res = store.backfill_ohlcv_row(
            symbol,
            idx.strftime("%Y-%m-%d"),
            float(row["Close"]),
            high=_nan_to_none(row.get("High")),
            low=_nan_to_none(row.get("Low")),
            volume=_nan_to_none(row.get("Volume")),
        )
        if res == "inserted":
            inserted += 1
        else:
            updated += 1
    stat = {
        "symbol": symbol,
        "rows": len(df),
        "inserted": inserted,
        "updated": updated,
        "first": df.index[0].strftime("%Y-%m-%d"),
        "last": df.index[-1].strftime("%Y-%m-%d"),
    }
    print(f"✅ {symbol}: {len(df)} 行 ({stat['first']} → {stat['last']}) "
          f"insert={inserted} update={updated}")
    return stat


def main(argv: list[str]) -> None:
    symbols = argv or DEFAULT_SYMBOLS
    store = MarketStore()
    print(f"Backfill: {symbols}\n" + "=" * 60)
    for sym in symbols:
        try:
            backfill_symbol(store, sym)
        except Exception as e:  # noqa: BLE001
            print(f"❌ {sym}: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main(sys.argv[1:])
