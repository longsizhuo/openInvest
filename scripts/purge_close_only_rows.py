#!/usr/bin/env python3
"""删除非交易日的"仅收盘价行"——默认 dry-run 只读，--apply 才删。

为什么存在（2026-10）：utils/gold_price 现价缓存曾用进程本地"今天"只写 close，美国假日、
周末（FX ``=X`` 豁免幽灵周末闸）留下无 H/L 的行，ATR 退化成收盘价差。写入端已改为按
bar 日期带 H/L 落库，本脚本清存量。

口径：
  - 仅收盘价行 = high、low 都是 NULL，且该 symbol 别处有 H/L（整列 NULL 的纯收盘价序列不碰）。
  - 非交易日 = 行情源（yfinance，与写库同一 tz 口径）在该日期没有 bar → 删。
  - 行情源有 bar 的仅收盘价行 = 交易日缺 H/L → 不删，只计数；补 H/L 不在本脚本范围。
  - 行情源拉不到 / 返回空的 symbol 整个跳过，不删。

    uv run python -m scripts.purge_close_only_rows                    # dry-run
    uv run python -m scripts.purge_close_only_rows --db /path/copy.db # 对副本回算
    uv run python -m scripts.purge_close_only_rows --apply            # 签字后
"""
from __future__ import annotations

import argparse
import sqlite3
from contextlib import closing
from datetime import date, timedelta

from openinvest.paths import INVEST_ROOT

_CLOSE_ONLY = "high IS NULL AND low IS NULL"


def close_only_rows(conn):
    """{symbol: [date, ...]}"""
    out = {}
    for sym, d in conn.execute(
        f"SELECT symbol, date FROM daily_prices WHERE {_CLOSE_ONLY} AND symbol IN "
        "(SELECT DISTINCT symbol FROM daily_prices WHERE high IS NOT NULL) "
        "ORDER BY symbol, date"
    ):
        out.setdefault(sym, []).append(d)
    return out


def source_dates(symbol, dates):
    """行情源在 [最早-7d, 最晚+7d] 里有 bar 的日期；拉不到 → None（调用方跳过该 symbol）。
    窗口两头各放 7 天：只有一根周末行时窗口里也有真 bar，空结果才可判为拉取失败。"""
    import yfinance as yf

    lo = date.fromisoformat(min(dates)) - timedelta(days=7)
    hi = date.fromisoformat(max(dates)) + timedelta(days=8)  # end 不含
    try:
        df = yf.Ticker(symbol).history(start=lo.isoformat(), end=hi.isoformat())
    except Exception as e:
        print(f"  {symbol}: 行情源拉取失败（{e}），跳过")
        return None
    if df is None or df.empty:
        print(f"  {symbol}: 行情源返回空，跳过")
        return None
    return {ts.strftime("%Y-%m-%d") for ts in df.index}


def plan(rows, fetch):
    """→ (delete [(sym, date)], keep [(sym, date)])"""
    delete, keep = [], []
    for sym, dates in rows.items():
        bars = fetch(sym, dates)
        if bars is None:
            continue
        for d in dates:
            (keep if d in bars else delete).append((sym, d))
    return delete, keep


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default=str(INVEST_ROOT / "db" / "market_data.db"))
    ap.add_argument("--apply", action="store_true", help="真删（默认只列出）")
    a = ap.parse_args(argv)

    uri = f"file:{a.db}" + ("" if a.apply else "?mode=ro")
    with closing(sqlite3.connect(uri, uri=True, timeout=5.0)) as conn:
        rows = close_only_rows(conn)
        delete, keep = plan(rows, source_dates)
        for sym in rows:
            dd = [d for s, d in delete if s == sym]
            kk = [d for s, d in keep if s == sym]
            print(f"{sym}: 仅收盘价行 {len(rows[sym])}，非交易日（删）{len(dd)}，"
                  f"交易日缺 H/L（留）{len(kk)}")
            for d in dd:
                print(f"  - {d}")
        print(f"合计删 {len(delete)} 行" + ("" if a.apply else "（dry-run，未改库；--apply 执行）"))
        if a.apply and delete:
            # 再判一次仅收盘价：dry-run 之后被日线补齐 H/L 的行不删
            n = sum(conn.execute(
                f"DELETE FROM daily_prices WHERE symbol=? AND date=? AND {_CLOSE_ONLY}", r
            ).rowcount for r in delete)
            conn.commit()
            print(f"已删 {n} 行")


if __name__ == "__main__":
    main()
