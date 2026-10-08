"""事件触发的委员会按来源计数（只读观察，零 LLM、零网络）。

#261 起，爬虫 event_watch 和 agent 投喂 ingest_event（Hermes 哨兵走这条）共用
services/event_trigger 的闸 + 冷却 + 日上限。本脚本统计窗口内**实际**触发了多少次委员会、
各来自哪道门，并对照上线前的回放 experiments/event-gate-replay-2026-10
（两门合计·越级开，49 天：爬虫 87 : 哨兵 47，已扣除只在持仓的标的——#306 起它们不跑委员会）。

触发记录从哪来：触发成功后，触发方给喂进委员会的事件打 events.committee_task_id。
- 门 = 这些事件的 ingested_by（NULL = 爬虫管道）；event_type=price_action 是
  price_sentinel 自己的门（独立冷却，不走共享闸），单列、不进对照。
- 委员会次数 = memory/.committee/<task>/status.json 的 symbols 个数（多 symbol task
  算 N 次，同回放口径）；status.json 缺失的 task 只计 task 数并单报。
- status.json 的 note 区分代码版本："triggered by event_watch" 是 #261 之前的旧码
  （无频控），单列为 legacy，不进对照。

只读：events.db 走 sqlite mode=ro，status.json 只读。输出只有聚合计数，不含 symbol、
事件、task id。

用法（仓库根；--home 可指向生产数据的副本）：
    uv run python scripts/research/trigger_counts_by_source.py --start 2026-10-08 --end 2026-11-04
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# experiments/event-gate-replay-2026-10/result_all_doors.json（2026-08-20→10-07，49 个北京自然日）
# 对照基准扣掉"只在持仓、不在 target_assets"的运行（#306 起这类事件不再跑委员会，旧基准 95/59 含它们）
REPLAY = {"pipeline": 87, "hermes-sentinel": 47, "days": 49}
DAILY_CAP = 4  # event.committee_daily_cap 默认值；只用于"顶到上限的天数"这一描述量
_CN = ZoneInfo("Asia/Shanghai")  # 按日口径同回放：北京自然日


def _utc(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def count(home: Path, start: str, end: str) -> dict:
    """[start, end] 按 UTC 日期（含两端）统计；end 带 T 时为开区间上界。"""
    end_excl = end if "T" in end else (date.fromisoformat(end) + timedelta(days=1)).isoformat()
    con = sqlite3.connect(f"file:{home / 'db' / 'events.db'}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT committee_task_id, CASE WHEN event_type = 'price_action' THEN 'price-sentinel' "
        "ELSE COALESCE(ingested_by, 'pipeline') END FROM events "
        "WHERE committee_task_id IS NOT NULL AND created_at >= ? AND created_at < ?",
        (start, end_excl)).fetchall()
    doors = defaultdict(set)
    for tid, door in rows:
        doors[tid].add(door)

    out: dict = {}
    per_day: Counter = Counter()
    for tid, ds in doors.items():
        door = ds.pop() if len(ds) == 1 else "mixed"
        p = home / "memory" / ".committee" / tid / "status.json"
        st = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        era = "legacy" if (st.get("note") or "").startswith("triggered by event_watch") else "shared_gate"
        b = out.setdefault(era, {}).setdefault(door, Counter())
        b["tasks"] += 1
        if not st:
            b["tasks_without_status"] += 1
            continue
        n = len((st.get("result") or {}).get("symbols") or st.get("symbols") or [])
        b["committees"] += n
        b[f"status_{st.get('status', 'unknown')}"] += 1
        if era == "shared_gate" and door in REPLAY and st.get("started_at"):
            per_day[datetime.fromisoformat(st["started_at"]).astimezone(_CN).date()] += n

    gate = out.get("shared_gate", {})
    pipe, sent = gate.get("pipeline", Counter())["committees"], gate.get("hermes-sentinel", Counter())["committees"]
    days = round((_utc(end_excl) - _utc(start)).total_seconds() / 86400, 2)
    return {
        "window_utc": [start, end], "days": days,
        "by_era_door": {e: {d: dict(c) for d, c in v.items()} for e, v in out.items()},
        "shared_gate_vs_replay": {
            "observed": {"pipeline": pipe, "hermes-sentinel": sent,
                         "sentinel_share": round(sent / (pipe + sent), 3) if pipe + sent else None,
                         "per_day": {"pipeline": round(pipe / (days or 1), 2), "hermes-sentinel": round(sent / (days or 1), 2)},
                         "per_day_max": max(per_day.values(), default=0),
                         "days_at_cap": sum(n >= DAILY_CAP for n in per_day.values())},
            "replay": {"pipeline": REPLAY["pipeline"], "hermes-sentinel": REPLAY["hermes-sentinel"],
                       "sentinel_share": round(REPLAY["hermes-sentinel"] / (REPLAY["pipeline"] + REPLAY["hermes-sentinel"]), 3),
                       "per_day": {k: round(REPLAY[k] / REPLAY["days"], 2) for k in ("pipeline", "hermes-sentinel")},
                       "days_at_cap": 21, "days": REPLAY["days"]},
        },
    }


def main() -> None:
    from openinvest.paths import INVEST_ROOT
    ap = argparse.ArgumentParser(description="事件触发的委员会按来源计数（只读）")
    ap.add_argument("--home", type=Path, default=INVEST_ROOT, help="INVEST_HOME（默认当前解析的数据目录）")
    ap.add_argument("--start", required=True, help="UTC 日期，含当天")
    ap.add_argument("--end", required=True, help="UTC 日期=含当天；完整 ISO 时间戳=开区间上界")
    a = ap.parse_args()
    print(json.dumps(count(a.home, a.start, a.end), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
