"""scripts/research/trigger_counts_by_source：按门 / 代码版本分桶，price_action 单列，多 symbol task 算 N 次。"""
import json
import sqlite3

from scripts.research.trigger_counts_by_source import count


def _task(home, tid, note, symbols, status="done", started="2026-10-08T15:00:00+08:00"):
    d = home / "memory" / ".committee" / tid
    d.mkdir(parents=True)
    (d / "status.json").write_text(json.dumps({"note": note, "status": status, "started_at": started,
                                               "result": {"symbols": symbols}}))


def test_counts_by_door_and_era(tmp_path):
    (tmp_path / "db").mkdir()
    con = sqlite3.connect(tmp_path / "db" / "events.db")
    con.execute("CREATE TABLE events (created_at TEXT, event_type TEXT, ingested_by TEXT, committee_task_id TEXT)")
    con.executemany("INSERT INTO events VALUES (?,?,?,?)", [
        ("2026-10-08T01:00:00+00:00", "macro", None, "old1"),               # 旧码爬虫
        ("2026-10-08T06:00:00+00:00", "macro", None, "new1"),               # 共享闸·爬虫，同 task 两条事件
        ("2026-10-08T06:00:00+00:00", "macro", None, "new1"),
        ("2026-10-08T07:00:00+00:00", "other", "hermes-sentinel", "s1"),    # 共享闸·哨兵，2 个 symbol
        ("2026-10-08T08:00:00+00:00", "other", "hermes-sentinel", "gone"),  # status.json 缺失
        ("2026-10-08T09:00:00+00:00", "price_action", None, "p1"),          # price_sentinel 自己的门
        ("2026-10-08T10:00:00+00:00", "macro", None, None),                 # 没触发
        ("2026-10-09T01:00:00+00:00", "macro", None, "late"),               # 窗口外
    ])
    con.commit()
    con.close()
    _task(tmp_path, "old1", "triggered by event_watch event_ids=a", ["X"])
    _task(tmp_path, "new1", "triggered by event_trigger event_ids=b", ["X"])
    _task(tmp_path, "s1", "triggered by event_trigger event_ids=c", ["X", "Y"], status="error")
    _task(tmp_path, "p1", "triggered by event_trigger event_ids=d", ["X"])
    _task(tmp_path, "late", "triggered by event_trigger event_ids=e", ["X"])

    r = count(tmp_path, "2026-10-08", "2026-10-08")
    by = r["by_era_door"]
    assert by["legacy"] == {"pipeline": {"tasks": 1, "committees": 1, "status_done": 1}}
    gate = by["shared_gate"]
    assert gate["pipeline"] == {"tasks": 1, "committees": 1, "status_done": 1}
    assert gate["hermes-sentinel"] == {"tasks": 2, "committees": 2, "status_error": 1, "tasks_without_status": 1}
    assert gate["price-sentinel"]["committees"] == 1
    obs = r["shared_gate_vs_replay"]["observed"]
    assert (obs["pipeline"], obs["hermes-sentinel"], obs["sentinel_share"]) == (1, 2, 0.667)
    assert r["days"] == 1 and obs["per_day_max"] == 3
