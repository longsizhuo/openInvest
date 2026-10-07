"""事件触发闸回放：新闸 + 冷却 + 日上限 + 越级（services/event_trigger）会从历史事件触发多少次委员会。

零 LLM、零网络、只读 events.db（sqlite uri mode=ro）。闸和频控直接调生产纯函数
`is_triggerable` / `admit`，回放口径 = 生产口径。按 events.created_at（入库时刻 = 触发
时刻）升序逐条喂；假设每次触发都成功（成功才占冷却/额度，同生产）。

**输出去标识**：关注 symbol 按 --watched 给定顺序记为 S1..Sn，输出里不出现真实 ticker
（公开仓库红线：关注集合 = 持仓 ∪ 目标资产，不能进 repo）。映射只在跑的人手里。

用法（仓库根）：
    uv run python experiments/event-gate-replay-2026-10/replay.py \\
        --db <INVEST_HOME>/db/events.db --watched SYM1,SYM2,... \\
        --start 2026-08-20 --end 2026-10-07 [--source hermes-sentinel|crawler|all] \\
        [--warmup-from 2026-08-12] [--no-escalation-bypass] \\
        [--committee-dir <INVEST_HOME>/memory/.committee] [--trace SYM --trace-from ISO --trace-to ISO]

--warmup-from：从更早开始喂事件把冷却/额度状态"预热"，但只统计 --start 之后的——
频控有路径依赖（谁先占了额度），冷启动会让窗口头一两天的结论失真。

--committee-dir 给了就额外统计窗口内**实际发生过**的事件触发委员会（旧码，无频控），
从 <task_id>/status.json 读 symbols 做对照。--trace 输出某 symbol 在时间段内每条过闸
事件的结局（run / run-escalation / cooldown / cap）。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from openinvest.services.event_trigger import _last, admit, is_triggerable

_SEV = {1: "low", 2: "mid", 3: "high"}
_CN = ZoneInfo("Asia/Shanghai")  # 按日统计口径：北京自然日（event_watch.yml 同时区）


def _day(ts: datetime) -> str:
    return ts.astimezone(_CN).date().isoformat()


def _bj(ts: datetime) -> str:
    return ts.astimezone(_CN).isoformat(timespec="minutes")


def load_rows(db: str, start: str, end: str, source: str):
    where = {"hermes-sentinel": "ingested_by = 'hermes-sentinel'",
             "crawler": "ingested_by IS NULL", "all": "1=1"}[source]
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    # end：纯日期 = 含当天；带 T 的完整时间戳 = 开区间上界（冻结快照用，库还在长）
    end_excl = end if "T" in end else (datetime.fromisoformat(end) + timedelta(days=1)).date().isoformat()
    # price_action 是 price_sentinel 自己的门（独立冷却，不走 event_trigger），排除
    return con.execute(
        f"SELECT created_at, severity, stance, affected_symbols_json, committee_task_id, "
        f"COALESCE(ingested_by, 'crawler') FROM events WHERE {where} "
        f"AND created_at >= ? AND created_at < ? AND COALESCE(event_type, '') != 'price_action' "
        f"ORDER BY created_at, id", (start, end_excl)).fetchall()


def replay(rows, watched, *, min_severity, cooldown_hours, daily_cap, escalation_bypass,
           count_from="", trace=None, trace_from=None, trace_to=None):
    label = {s: f"S{i + 1}" for i, s in enumerate(watched)}
    canonical = {s.lower(): s for s in watched}
    cooldown = timedelta(hours=cooldown_hours)
    state: dict = {}
    gate_events = 0
    naive = Counter()             # 无频控：每条过闸事件 × 每个命中 symbol 跑一次
    runs: list = []               # (ts, symbol, door) 实际放行
    hits = Counter()              # (sev, outcome) 按 symbol 次
    swallow_windows = set()       # 低 severity 开的冷却吞掉 sev-3 的窗口 (sym, 开冷却时刻)
    trace_out = []
    for created_at, sev, stance, affected_json, _, door in rows:
        ev = {"severity": _SEV.get(sev, "low"), "stance": stance,
              "affected_symbols": json.loads(affected_json or "[]")}
        if not is_triggerable(ev, canonical.keys(), min_severity):
            continue
        syms = sorted({canonical[s.lower()] for s in ev["affected_symbols"] if s.lower() in canonical})
        now = datetime.fromisoformat(created_at)
        last = state.get("last") or {}
        before = {s: _last(last.get(s), now) for s in syms}
        admitted, state = admit(state, [(s, sev) for s in syms], now, cooldown_hours=cooldown_hours,
                                daily_cap=daily_cap, escalation_bypass=escalation_bypass)
        if created_at < count_from:
            continue  # 预热段：只推进状态，不计数
        gate_events += 1
        naive.update(syms)
        for s in syms:
            prev, prev_sev = before[s]
            cooled = prev is not None and now - prev < cooldown
            if s in admitted:
                outcome = "run-escalation" if cooled else "run"
                runs.append((now, s, door))
            elif cooled and not (escalation_bypass and sev > prev_sev):
                outcome = "cooldown"
                if sev == 3 and prev_sev < 3:
                    swallow_windows.add((s, prev))
            else:
                outcome = "cap"
            hits[(sev, outcome)] += 1
            if s == trace and trace_from <= created_at < trace_to:
                trace_out.append({"beijing": _bj(now), "door": door, "sev": sev, "outcome": outcome})
    rows = [r for r in rows if r[0] >= count_from]
    per_day = Counter(_day(t) for t, _, _ in runs)
    first = {}
    for t, s, _ in runs:
        first.setdefault(label[s], _bj(t))

    def _sev_row(k):
        return {o: hits[(k, o)] for o in ("run", "run-escalation", "cooldown", "cap")}

    out = {
        "events_in_window": len(rows),
        "last_created_at": rows[-1][0] if rows else None,
        "gate_pass_events": gate_events,
        "committees_without_throttle": sum(naive.values()),
        "committees": len(runs),
        "by_door": dict(Counter(d for _, _, d in runs).most_common()),
        "per_symbol": {label[s]: n for s, n in Counter(s for _, s, _ in runs).most_common()},
        "per_day_max": max(per_day.values(), default=0),
        "days_with_committee": len(per_day),
        "days_at_cap": sum(1 for n in per_day.values() if n >= daily_cap),
        "symbol_hits_by_severity": {"sev2": _sev_row(2), "sev3": _sev_row(3)},
        "lower_sev_cooldowns_swallowing_sev3": len(swallow_windows),
        "first_trigger_beijing": first,
        "per_day": dict(sorted(per_day.items())),
    }
    if trace:
        out["trace"] = {"symbol": label[trace], "events": trace_out}
    return out, label


def actual_history(rows, committee_dir: Path, label, count_from):
    """窗口内旧码真实触发过的委员会（按 symbol 计），对照用。"""
    tasks = {r[4] for r in rows if r[4] and r[0] >= count_from}
    syms: Counter = Counter()
    per_day: Counter = Counter()
    for tid in tasks:
        p = committee_dir / tid / "status.json"
        if not p.exists():
            continue
        d = json.loads(p.read_text(encoding="utf-8"))
        ss = (d.get("result") or {}).get("symbols") or d.get("symbols") or []
        syms.update(label.get(s, "other") for s in ss)
        if d.get("started_at"):
            per_day[_day(datetime.fromisoformat(d["started_at"]))] += len(ss)
    return {"tasks": len(tasks), "committees": sum(syms.values()),
            "per_symbol": dict(syms.most_common()),
            "per_day_max": max(per_day.values(), default=0), "days_with_committee": len(per_day)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--watched", required=True, help="holdings ∪ target_assets，逗号分隔（输出记为 S1..Sn）")
    ap.add_argument("--start", required=True)
    ap.add_argument("--warmup-from", help="更早的起点，只推进冷却/额度状态不计数（默认 = --start）")
    ap.add_argument("--end", required=True, help="日期=含当天；完整 ISO 时间戳=开区间上界")
    ap.add_argument("--source", default="hermes-sentinel", choices=["hermes-sentinel", "crawler", "all"])
    ap.add_argument("--min-severity", default="mid")
    ap.add_argument("--cooldown-hours", type=int, default=12)
    ap.add_argument("--daily-cap", type=int, default=4)
    ap.add_argument("--no-escalation-bypass", action="store_true")
    ap.add_argument("--committee-dir")
    ap.add_argument("--trace")
    ap.add_argument("--trace-from", default="")
    ap.add_argument("--trace-to", default="9999")
    a = ap.parse_args()
    watched = [s.strip() for s in a.watched.split(",") if s.strip()]
    rows = load_rows(a.db, a.warmup_from or a.start, a.end, a.source)
    rep, label = replay(rows, watched, min_severity=a.min_severity, cooldown_hours=a.cooldown_hours,
                        daily_cap=a.daily_cap, escalation_bypass=not a.no_escalation_bypass,
                        count_from=a.start, trace=a.trace, trace_from=a.trace_from, trace_to=a.trace_to)
    hidden = ("db", "committee_dir", "watched", "trace")
    out = {"params": {k: v for k, v in vars(a).items() if k not in hidden},
           "watched_count": len(watched), "replay": rep}
    if a.committee_dir:
        out["actual_old_code"] = actual_history(rows, Path(a.committee_dir), label, a.start)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
