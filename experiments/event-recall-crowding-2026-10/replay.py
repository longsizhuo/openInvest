"""事件召回挤占回放：旧 EventStore.recall（全库 LIMIT 200 后才按 symbol 过滤）vs 新版
（symbol 过滤先于 LIMIT + 未来 ts 按入库时刻算），逐工作日 × 关注 symbol 对比召回结果。

零 LLM、零网络。--db 以 sqlite uri mode=ro 打开，backup 到临时目录再跑（EventStore 构造会写
PRAGMA）。新旧两版都按生产口径调：embed_text(symbol) 向量精排（默认 hash provider，确定性）、
aliases = proxy_symbols_for(symbol)、7 天窗 / mid / top_k 8、as_of = D 当天 --hour-utc 点。
旧版从 git --old-rev 取 src/openinvest/db/event_store.py 源码现场加载。

**输出去标识**：关注 symbol 按 --watched 给定顺序记为 S1..Sn，输出只有计数、没有 ticker /
事件文本（公开仓库红线：关注集合 = 持仓 ∪ 目标资产）。映射只在跑的人手里。

用法（仓库根）：
    uv run python experiments/event-recall-crowding-2026-10/replay.py \\
        --db <INVEST_HOME>/db/events.db --watched SYM1,SYM2,... \\
        --start 2026-09-01 --end 2026-10-07 [--hour-utc 2] [--old-rev a4d6fc2] [--default-path] [--no-rerank]

--no-rerank：对照组，不做向量精排（纯 SQL 时间倒序取 top_k）。
--default-path：额外跑一遍 as_of=None（生产现行路径，锚 now、不截断 created_at）——结果随
运行时刻和库增长变化，不可逐字节复现，输出里带 run_at。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import tempfile
import types
from statistics import median
from datetime import date, datetime, timedelta, timezone

from openinvest.db.event_store import EventStore
from openinvest.services.embeddings import embed_text
from openinvest.services.symbol_map import proxy_symbols_for


def load_old_store_cls(rev: str):
    src = subprocess.check_output(
        ["git", "show", f"{rev}:src/openinvest/db/event_store.py"], text=True)
    mod = types.ModuleType("event_store_old")
    exec(compile(src, f"event_store@{rev}", "exec"), mod.__dict__)
    return mod.EventStore


def snapshot(db: str, dst: str) -> None:
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    out = sqlite3.connect(dst)
    src.backup(out)
    out.close()
    src.close()


def _age_h(ts: str, anchor: datetime) -> float:
    """anchor - ts（小时）；负数 = ts 在 anchor 之后（未来 ts）"""
    t = datetime.fromisoformat(ts)
    return round((anchor - (t if t.tzinfo else t.replace(tzinfo=timezone.utc))).total_seconds() / 3600, 1)


def _median(xs):
    return round(median(xs), 1) if xs else None


def compare(old, new, sym: str, as_of, args) -> dict:
    kw = dict(time_window_days=args.window_days, min_severity=args.min_severity,
              top_k=args.top_k, query_embedding=None if args.no_rerank else embed_text(sym),
              aliases=sorted(proxy_symbols_for(sym)), as_of=as_of)
    o, n = old.recall(sym, **kw), new.recall(sym, **kw)
    anchor = as_of or datetime.now(timezone.utc)
    o_ids, n_ids = {e["event_id"] for e in o}, {e["event_id"] for e in n}
    o_age, n_age = [_age_h(e["ts"], anchor) for e in o], [_age_h(e["ts"], anchor) for e in n]
    return {"old": len(o), "new": len(n), "changed": len(n_ids - o_ids),
            "old_future_ts": sum(a < 0 for a in o_age), "new_future_ts": sum(a < 0 for a in n_age),
            "old_age_h": o_age, "new_age_h": n_age}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--watched", required=True, help="逗号分隔，输出按顺序记 S1..Sn")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--hour-utc", type=int, default=2, help="as_of 时刻（默认 02:00Z = 北京 10:00 daily_report）")
    ap.add_argument("--old-rev", default="a4d6fc2")
    ap.add_argument("--window-days", type=int, default=7)
    ap.add_argument("--min-severity", default="mid")
    ap.add_argument("--top-k", type=int, default=8)
    ap.add_argument("--default-path", action="store_true")
    ap.add_argument("--no-rerank", action="store_true", help="对照：不传 query_embedding，纯按时间倒序取 top_k")
    args = ap.parse_args()

    watched = [s.strip() for s in args.watched.split(",") if s.strip()]
    label = {s: f"S{i + 1}" for i, s in enumerate(watched)}
    days = []
    d, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "events.db")
        snapshot(args.db, path)
        old, new = load_old_store_cls(args.old_rev)(db_path=path), EventStore(db_path=path)
        assert old.vec_loaded and new.vec_loaded, "sqlite-vec 未加载，向量精排路径跑不到"

        rows = []
        for d in days:
            as_of = datetime(d.year, d.month, d.day, args.hour_utc, tzinfo=timezone.utc)
            for sym in watched:
                rows.append({"date": d.isoformat(), "sym": label[sym],
                             **compare(old, new, sym, as_of, args)})

        summary = {}
        for sym in watched:
            rs = [r for r in rows if r["sym"] == label[sym]]
            summary[label[sym]] = {
                "days": len(rs),
                "events_old": sum(r["old"] for r in rs),
                "events_new": sum(r["new"] for r in rs),
                "days_0_to_pos": sum(r["old"] == 0 < r["new"] for r in rs),
                "days_pos_to_0": sum(r["new"] == 0 < r["old"] for r in rs),
                "days_changed": sum(r["changed"] > 0 for r in rs),
                "events_changed": sum(r["changed"] for r in rs),
                "future_ts_old": sum(r["old_future_ts"] for r in rs),
                "future_ts_new": sum(r["new_future_ts"] for r in rs),
                # 召回事件距 as_of 的小时数中位（未来 ts 记负）——新版池子覆盖整个 7 天窗
                "median_age_h_old": _median([a for r in rs for a in r["old_age_h"]]),
                "median_age_h_new": _median([a for r in rs for a in r["new_age_h"]]),
            }
        out = {"params": {k: v for k, v in vars(args).items() if k not in ("db", "watched")},
               "summary": summary, "rows": rows}
        if args.default_path:
            out["default_path"] = {
                "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                **{label[s]: compare(old, new, s, None, args) for s in watched}}
        old.conn.close()
        new.conn.close()
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
