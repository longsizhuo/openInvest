"""同论点转载归并回放：旧（--old-rev 的 event_brief.py，每条事件各算一条）vs 新（同论点最多 1 条）。

零 LLM、零网络。--db 以 sqlite uri mode=ro 打开，backup 到临时目录再跑（EventStore 构造会写 PRAGMA）。
走生产同一条链：resolve_event_brief_multi(watched, as_of=D)（= session 默认召回）→
event_stance_line_for_symbol(merged_brief, sym)（= append_event_stance_line 的实现）。
旧版 event_brief.py 从 git --old-rev 现场加载 + INVEST_EVENT_MAX_CORROBORATION=0；新版 = 当前源码默认值。
召回、排序、top_k 全同（同一个只读库快照、同一个 EventStore）。

as_of = 每天 --hour-utc 点（默认 02:00Z = 北京 10:00）；召回按 created_at <= as_of 截断，过去的日子
同快照重跑逐字节一致。

**输出去标识**：关注 symbol 按 --watched 顺序记 S1..Sn，输出只有计数，没有 ticker / 事件文本
（公开仓库红线）。--with-rows 输出逐日明细，只供本地排查，别提交。

用法（仓库根，INVEST_EMBEDDING_PROVIDER 不设 = 生产默认 hash → 召回按时间倒序）：
    uv run python experiments/event-corroboration-2026-10/replay.py \\
        --db <INVEST_HOME>/db/events.db --watched SYM1,SYM2,... --start 2026-09-19 --end 2026-10-18 \\
        [--old-rev c460c47]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import types
from datetime import date, datetime, timedelta, timezone

import openinvest.core.runner.event_brief as eb
from openinvest.calc.sentiment import _parse_event_brief_entries
from openinvest.core.config import reset_config
from openinvest.core.runner.event_format import collapse_corroboration
from openinvest.db.event_store import EventStore
from openinvest.services.embeddings import DEFAULT_DIM
from openinvest.utils.sentiment import _event_stance_line, event_stance_line_for_symbol

_COUNTS_RE = re.compile(r"risk=(\d+) opportunity=(\d+)")
_NET_RE = re.compile(r"net (risk|opportunity|neutral)")
_HEAD_RE = re.compile(
    r"^\[[^\]]*\] \[(?P<stance>[^/\]]*)/(?P<sev>[^\]]*)\] \[(?P<syms>[^\]]*)\](?: \[agent:(?P<who>[^\]]*)\])?")
_MARK_RE = re.compile(r" \(×(\d+) 来源\)$")


def load_old_event_brief(rev: str):
    src = subprocess.check_output(
        ["git", "show", f"{rev}:src/openinvest/core/runner/event_brief.py"], text=True)
    mod = types.ModuleType("event_brief_old")
    exec(compile(src, f"event_brief@{rev}", "exec"), mod.__dict__)
    return mod


def snapshot(db: str, dst: str) -> None:
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    out = sqlite3.connect(dst)
    src.backup(out)
    out.close()
    src.close()


def _stance(line):
    """EVENT_STANCE 行 → (risk, opportunity, net)；无行 → (0, 0, 'none')"""
    if not line:
        return 0, 0, "none"
    r, o = map(int, _COUNTS_RE.search(line).groups())
    return r, o, _NET_RE.search(line).group(1)


def _paragraphs(brief: str):
    """brief → 事件 dict 列表（只含归并要看的字段）+ 带 ×N 标的行数；给残余重复检查用"""
    evs, marked = [], 0
    for p in (brief or "").split("\n\n"):
        lines = p.strip().split("\n")
        if len(lines) < 2 or not (m := _HEAD_RE.match(lines[0])):
            continue
        marked += bool(_MARK_RE.search(lines[1]))
        evs.append({"affected_symbols": [x.strip() for x in m.group("syms").split(",") if x.strip()],
                    "stance": m.group("stance"), "severity": m.group("sev"),
                    "ingested_by": m.group("who"), "one_line_claim": _MARK_RE.sub("", lines[1])})
    return evs, marked


def measure(mod, watched, as_of, cap: int):
    os.environ["INVEST_EVENT_MAX_CORROBORATION"] = str(cap)
    reset_config()
    merged = mod.resolve_event_brief_multi(watched, as_of=as_of)
    evs, marked = _paragraphs(merged)
    return {
        "brief_entries": len(_parse_event_brief_entries(merged)),
        "marked_lines": marked,
        # 合并 brief 里按同一套归并条件还能再折叠掉几条 = 残余重复（新版应为 0）
        "residual_dup_groups": len(evs) - len(collapse_corroboration(evs, 1)),
        "market": _stance(_event_stance_line(merged)),
        "per_sym": {s: _stance(event_stance_line_for_symbol(merged, s)) for s in watched},
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--watched", required=True, help="逗号分隔，输出按顺序记 S1..Sn")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--hour-utc", type=int, default=2)
    ap.add_argument("--old-rev", default="c460c47", help="旧版 event_brief.py 取自哪个 git rev（本改动前的 main）")
    ap.add_argument("--with-rows", action="store_true", help="逐日明细，只供本地排查，别提交")
    args = ap.parse_args()

    watched = [s.strip() for s in args.watched.split(",") if s.strip()]
    label = {s: f"S{i + 1}" for i, s in enumerate(watched)}
    d, end = date.fromisoformat(args.start), date.fromisoformat(args.end)

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "events.db")
        snapshot(args.db, path)
        store = EventStore(db_path=path, embedding_dim=DEFAULT_DIM)
        old_eb = load_old_event_brief(args.old_rev)
        eb._EVENT_STORE_SINGLETON = old_eb._EVENT_STORE_SINGLETON = store
        rows = []
        while d <= end:
            as_of = datetime(d.year, d.month, d.day, args.hour_utc, tzinfo=timezone.utc)
            rows.append({"date": d.isoformat(),
                         "old": measure(old_eb, watched, as_of, 0), "new": measure(eb, watched, as_of, 1)})
            d += timedelta(days=1)
        store.conn.close()

    def tot(f, k):
        return sum(f(r[k]) for r in rows)

    def delta_days(f):
        return sum(f(r["old"]) != f(r["new"]) for r in rows)

    summary = {
        "days": len(rows),
        "days_with_brief_old": sum(r["old"]["brief_entries"] > 0 for r in rows),
        "brief_entries_old": tot(lambda m: m["brief_entries"], "old"),
        "brief_entries_new": tot(lambda m: m["brief_entries"], "new"),
        "days_brief_count_changed": delta_days(lambda m: m["brief_entries"]),
        "max_daily_reduction": max(r["old"]["brief_entries"] - r["new"]["brief_entries"] for r in rows),
        "marked_lines_new": tot(lambda m: m["marked_lines"], "new"),
        "residual_dup_groups_new": tot(lambda m: m["residual_dup_groups"], "new"),
        "market_stance": {
            "risk_old": tot(lambda m: m["market"][0], "old"), "risk_new": tot(lambda m: m["market"][0], "new"),
            "opp_old": tot(lambda m: m["market"][1], "old"), "opp_new": tot(lambda m: m["market"][1], "new"),
            "days_counts_changed": delta_days(lambda m: m["market"][:2]),
            "days_net_label_changed": delta_days(lambda m: m["market"][2]),
        },
        "per_symbol_stance": {
            label[s]: {
                "risk_old": tot(lambda m: m["per_sym"][s][0], "old"), "risk_new": tot(lambda m: m["per_sym"][s][0], "new"),
                "opp_old": tot(lambda m: m["per_sym"][s][1], "old"), "opp_new": tot(lambda m: m["per_sym"][s][1], "new"),
                "days_counts_changed": delta_days(lambda m: m["per_sym"][s][:2]),
                "days_net_label_changed": delta_days(lambda m: m["per_sym"][s][2]),
            } for s in watched
        },
    }
    out = {"params": {k: v for k, v in vars(args).items() if k not in ("db", "watched", "with_rows")},
           "summary": summary}
    if args.with_rows:
        out["rows"] = rows
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
