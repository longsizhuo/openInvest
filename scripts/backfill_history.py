#!/usr/bin/env python
"""Backfill full price history into MarketStore for committee assets.

The committee's path-profile (the downtrend forward-return distribution that informs
trim/hold) is only as rich as the stored history. For **most** assets this needs no
special handling: yfinance ``period='max'`` already returns the asset's entire life
— 510300.SS from its 2012 inception, NDQ.AX from 2015, AAPL from 1980 — automatically
and server-side. There is no earlier data to fetch; nothing is hacky here.

The **one** exception is continuous commodities. The tradable futures contract
(GC=F / SI=F, inception 2000) is far younger than the underlying's daily price
(gold trades back to the 1960s-70s). A 2000+ window both under-samples long horizons
(90d independent-n ~4) and is bull-biased (2000-2026 was a secular gold bull, which
flatters even its "downtrend" windows). For those few symbols we splice a bundled
long daily CSV in front of the futures series — shipped in the repo, so a self-hosted
user never downloads anything per-asset.

Both paths are idempotent and non-destructive: ``MarketStore.backfill_ohlcv_row``
inserts missing dates and only ever updates high/low on existing rows, never the
authoritative close. Safe to run at setup and periodically.

  uv run python -m scripts.backfill_history              # all stored symbols
  uv run python -m scripts.backfill_history GC=F 510300.SS   # specific symbols

pandas-datareader / stooq are deliberately NOT used: pandas-datareader is unmaintained
(imports the removed ``distutils``, broken on Python 3.12+), and stooq IP-blocks
datacenter ranges — both are residential-IP tools, unusable from a hub. Deep commodity
history therefore ships as a bundled CSV (or, if you want auto-refresh, a keyed API
like Tiingo/EOD that permits server access).
"""
import sys
from pathlib import Path

import pandas as pd
import yfinance as yf

from db.market_store import MarketStore

ROOT = Path(__file__).resolve().parent.parent
INPUTS = ROOT / "experiments" / "ta-analysts" / "inputs"

# Commodity deep-history splice: symbol -> (bundled CSV, start, before-futures cutoff).
# Only rows in [start, before) are spliced — the gap before yfinance's futures history.
# CSV is a long daily series (Date,Open,High,Low,Close) in the symbol's own quote unit.
COMMODITY_DEEP_HISTORY = {
    "GC=F": (INPUTS / "xauusd_daily_1966_2026.csv", "1969-01-01", "2000-08-30"),
    # "SI=F": (INPUTS / "xagusd_daily.csv", "1969-01-01", "2000-08-30"),  # add when needed
}


def _yf_max(symbol):
    df = yf.Ticker(symbol).history(period="max")
    if df is None or df.empty:
        return None
    if df.index.tz is not None:
        df.index = df.index.tz_convert(None)
    return df


def backfill(symbol: str, ms: MarketStore) -> None:
    cur = ms.get_history_df(symbol, days=100000)
    n0 = len(cur) if cur is not None else 0
    ins = upd = 0

    # 1) yfinance max — the general, automated path (full life for equities/ETF/crypto/FX)
    df = _yf_max(symbol)
    if df is not None:
        for r in df.itertuples():
            res = ms.backfill_ohlcv_row(
                symbol, r.Index.strftime("%Y-%m-%d"),
                float(r.Close), float(r.High), float(r.Low),
                float(getattr(r, "Volume", 0) or 0), source="yfinance_max",
            )
            ins += res == "inserted"; upd += res == "updated"
    else:
        print(f"  {symbol:10} yfinance returned nothing (check symbol)")

    # 2) commodity deep-history splice — the bounded exception
    deep = COMMODITY_DEEP_HISTORY.get(symbol)
    if deep:
        csv, start, before = deep
        if not csv.exists():
            print(f"  {symbol:10} deep-history CSV missing: {csv}")
        else:
            cd = pd.read_csv(csv, parse_dates=["Date"]).sort_values("Date")
            cd = cd[(cd["Date"] >= start) & (cd["Date"] < before)]
            for r in cd.itertuples():
                res = ms.backfill_ohlcv_row(
                    symbol, r.Date.strftime("%Y-%m-%d"),
                    float(r.Close), float(r.High), float(r.Low), 0.0,
                    source=f"deep_history:{csv.name}",
                )
                ins += res == "inserted"; upd += res == "updated"

    after = ms.get_history_df(symbol, days=100000)
    span = (f"{after.index.min().date()}..{after.index.max().date()}"
            if after is not None and len(after) else "-")
    print(f"  {symbol:10} {n0:5d} -> {len(after) if after is not None else 0:5d} rows  {span}  (+{ins} ins, {upd} upd)")


def main() -> None:
    ms = MarketStore()
    symbols = sys.argv[1:] or ms.distinct_symbols()
    print(f"backfilling {len(symbols)} symbol(s)...")
    for s in symbols:
        try:
            backfill(s, ms)
        except Exception as e:  # one bad symbol shouldn't abort the batch
            print(f"  {s:10} ERR {type(e).__name__}: {str(e)[:80]}")


if __name__ == "__main__":
    main()
