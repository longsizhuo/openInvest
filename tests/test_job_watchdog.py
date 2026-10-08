"""job_watchdog + runner running 行的回归测试（2026-10-07）

事故：event_watch 2026-07-30 18:00 卡死在新闻抓取线程池 join，max_instances=1 让之后
21 天（→ 08-20）的触发全被跳过；runner 只在 job 跑完才写 job_runs → 库里零行、零报警。
守的行为：
- runner 开跑即写 status='running' 行，收尾 UPDATE 同一行（不再"跑完才有行"）
- 看门狗三类判据：stale（错过 2 次触发 +15min）/ hung（running 超 max(60min, 3×中位耗时)）
  / event_watch 连续 6 次 fetched=0
- 24h 去重：21 天停摆只在识别时报一次 + 每 24h 提醒一次，绝不每小时刷屏
- 投递失败回滚 claim，下次重试
全部 hermetic：tmp sqlite + tmp MemoryStore，告警发送一律 mock。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from apscheduler.triggers.cron import CronTrigger

from openinvest.core.memory_store import MemoryStore
from openinvest.jobs import job_watchdog
from openinvest.jobs.job_watchdog import find_problems
from openinvest.scheduler import runner

SH = ZoneInfo("Asia/Shanghai")
EW_CRON = "*/30 0-2,8-23 * * *"
EW_JOB = ("event_watch", EW_CRON, "Asia/Shanghai")


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=SH)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "RUN_LOG_DB", tmp_path / "jobs.sqlite")
    runner._ensure_run_log_table()
    conn = sqlite3.connect(runner.RUN_LOG_DB)
    yield conn
    conn.close()


def _add(conn, job, started, *, secs=15, status="success", output=None):
    conn.execute(
        "INSERT INTO job_runs (job_name, started_at, finished_at, status, output_excerpt) "
        "VALUES (?, ?, ?, ?, ?)",
        (job, started.isoformat(timespec="seconds"),
         None if status == "running" else (started + timedelta(seconds=secs)).isoformat(timespec="seconds"),
         status, output),
    )
    conn.commit()


def _seed_event_watch_until(conn, last: datetime, fetched=200):
    """按真实 cron 种 event_watch 正常运行行，最后一次 = last（含）。"""
    trig = CronTrigger.from_crontab(EW_CRON, timezone="Asia/Shanghai")
    t = trig.get_next_fire_time(None, last - timedelta(days=1))
    while t <= last:
        _add(conn, "event_watch", t, output=f"{{'status': 'ok', 'fetched': {fetched}, 'new_events': 3}}")
        t = trig.get_next_fire_time(t, t)


# ---------- runner：开跑即落 running 行 ----------

def test_wrap_job_records_running_row_before_job_finishes(db, monkeypatch):
    seen = {}

    def _job():
        seen["rows"] = db.execute("SELECT job_name, status, finished_at FROM job_runs").fetchall()
        return {"status": "ok"}

    monkeypatch.setattr(runner, "_resolve_entry", lambda entry: _job)
    runner._wrap_job("probe", "x:y")()

    # job 执行中库里已有 running 行（旧代码此刻零行——卡死的 job 永远不可见）
    assert seen["rows"] == [("probe", "running", None)]
    # 收尾 UPDATE 同一行，不是再插一行
    rows = db.execute("SELECT job_name, status, finished_at IS NOT NULL FROM job_runs").fetchall()
    assert rows == [("probe", "success", 1)]


def test_wrap_job_failure_updates_same_row(db, monkeypatch):
    def _boom():
        raise RuntimeError("x")

    monkeypatch.setattr(runner, "_resolve_entry", lambda entry: _boom)
    runner._wrap_job("probe", "x:y")()
    rows = db.execute("SELECT status, error FROM job_runs").fetchall()
    assert rows == [("failed", "RuntimeError: x")]


# ---------- (a) stale：21 天停摆原场景 ----------

def test_21_day_gap_detected_after_two_missed_fires(db):
    """真实时间线：最后一行 07-30 17:30，18:00 那次卡死且旧 runner 不落行。"""
    _seed_event_watch_until(db, _t("2026-07-30T17:30:00"))
    # 18:00 错过 1 次、18:30 还没到 → 不报（单次抖动不算）
    assert find_problems(db, [EW_JOB], _t("2026-07-30T18:40:00")) == []
    # 18:00 + 18:30 都错过，过 18:45 宽限 → 报
    found = find_problems(db, [EW_JOB], _t("2026-07-30T19:07:00"))
    assert [f["key"] for f in found] == ["event_watch:stale:2026-07-30T17:30:00+08:00"]
    assert found[0]["since"] == _t("2026-07-30T18:45:00")
    # 21 天后依旧在报（同一锚点）
    assert len(find_problems(db, [EW_JOB], _t("2026-08-20T15:07:00"))) == 1


def test_overnight_cron_gap_is_not_stale(db):
    """02:30 → 08:00 是 cron 本身的夜间空窗，不是停摆。"""
    _seed_event_watch_until(db, _t("2026-07-30T02:30:00"))
    assert find_problems(db, [EW_JOB], _t("2026-07-30T08:40:00")) == []


def test_job_never_run_is_skipped(db):
    assert find_problems(db, [("dca_daily", "0 15 * * 1-5", "Asia/Shanghai")],
                         _t("2026-07-30T19:07:00")) == []


# ---------- (b) hung：running 行超时 ----------

def test_hung_running_row_detected_and_stale_not_double_reported(db):
    """新 runner 下同一事故的样子：18:00 落了 running 行就再没结束。"""
    _seed_event_watch_until(db, _t("2026-07-30T17:30:00"))
    _add(db, "event_watch", _t("2026-07-30T18:00:00"), status="running")
    assert find_problems(db, [EW_JOB], _t("2026-07-30T18:50:00")) == []  # < 60min 地板
    found = find_problems(db, [EW_JOB], _t("2026-07-30T19:07:00"))
    assert len(found) == 1 and found[0]["key"].startswith("event_watch:hung:")


def test_hung_limit_scales_with_median_duration(db):
    job = ("slow", "0 3 * * *", "Asia/Shanghai")
    for d in range(1, 6):  # 历史中位耗时 40 分钟 → 阈值 120 分钟
        _add(db, "slow", _t(f"2026-07-0{d}T03:00:00"), secs=40 * 60)
    _add(db, "slow", _t("2026-07-06T03:00:00"), status="running")
    assert find_problems(db, [job], _t("2026-07-06T04:30:00")) == []
    assert len(find_problems(db, [job], _t("2026-07-06T05:05:00"))) == 1


def test_orphan_running_row_superseded_by_newer_run_is_ignored(db):
    """daemon 重启杀掉的 running 行，之后又正常跑了 → 不算卡死。"""
    _add(db, "event_watch", _t("2026-07-30T17:30:00"), status="running")
    _add(db, "event_watch", _t("2026-07-30T18:00:00"))
    assert find_problems(db, [EW_JOB], _t("2026-07-30T18:20:00")) == []


# ---------- (c) event_watch 连续 fetched=0 ----------

def _zero(conn, n, start="2026-07-30T08:00:00"):
    for i in range(n):
        _add(conn, "event_watch", _t(start) + timedelta(minutes=30 * i),
             output="{'status': 'ok', 'fetched': 0, 'new_events': 0, 'triggered': 0}")


def test_zero_fetch_six_in_a_row(db):
    _seed_event_watch_until(db, _t("2026-07-30T02:30:00"))
    _zero(db, 5)
    now = _t("2026-07-30T10:20:00")
    assert find_problems(db, [EW_JOB], now) == []
    _zero(db, 1, start="2026-07-30T10:30:00")
    found = find_problems(db, [EW_JOB], _t("2026-07-30T10:40:00"))
    assert [f["key"].split(":")[1] for f in found] == ["zero_fetch"]


def test_zero_fetch_streak_ignores_failed_but_breaks_on_nonzero(db):
    _zero(db, 3)
    _add(db, "event_watch", _t("2026-07-30T09:40:00"), status="failed", output="Traceback")
    _zero(db, 3, start="2026-07-30T10:00:00")
    now = _t("2026-07-30T11:10:00")
    assert len(find_problems(db, [EW_JOB], now)) == 1  # failed 不断连击
    _add(db, "event_watch", _t("2026-07-30T11:30:00"), output="{'status': 'ok', 'fetched': 5}")
    assert find_problems(db, [EW_JOB], _t("2026-07-30T11:40:00")) == []


# ---------- run()：24h 去重 + 投递失败回滚 ----------

@pytest.fixture
def wired(db, tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(runner, "_load_job_configs", lambda: [
        {"name": "event_watch", "schedule": EW_CRON, "timezone": "Asia/Shanghai", "enabled": True},
        {"name": "dreaming", "schedule": "0 3 * * *", "timezone": "Asia/Shanghai", "enabled": False},
    ])
    monkeypatch.setattr(runner, "_resolve_schedule", lambda name, s: s)
    store = MemoryStore(root=tmp_path / "memory")
    monkeypatch.setattr(job_watchdog, "MemoryStore", lambda: store)
    monkeypatch.setattr(job_watchdog, "_send", lambda findings: sent.append(findings))
    return db, sent


def test_21_day_gap_alerts_once_per_24h_not_hourly(wired):
    db, sent = wired
    _seed_event_watch_until(db, _t("2026-07-30T17:30:00"))
    now, end = _t("2026-07-30T18:07:00"), _t("2026-08-20T15:07:00")
    alert_times = []
    while now <= end:  # 按 yml 的每小时第 7 分钟巡检，整整 21 天
        before = len(sent)
        job_watchdog.run(now=now)
        if len(sent) > before:
            alert_times.append(now)
        now += timedelta(hours=1)
    assert alert_times[0] == _t("2026-07-30T19:07:00")  # 停摆约 1 小时内首报
    assert len(alert_times) == 21  # 首报 + 每 24h 提醒一次，而不是 ~500 封
    assert all(b - a >= timedelta(hours=24) for a, b in zip(alert_times, alert_times[1:]))


def test_send_failure_unclaims_so_next_run_retries(wired, monkeypatch):
    db, sent = wired
    _seed_event_watch_until(db, _t("2026-07-30T17:30:00"))

    def _down(findings):
        raise RuntimeError("smtp down")

    monkeypatch.setattr(job_watchdog, "_send", _down)
    with pytest.raises(RuntimeError):
        job_watchdog.run(now=_t("2026-07-30T19:07:00"))
    monkeypatch.setattr(job_watchdog, "_send", lambda f: sent.append(f))
    out = job_watchdog.run(now=_t("2026-07-30T20:07:00"))
    assert len(sent) == 1 and out["alerted"]


def test_disabled_jobs_not_checked(wired):
    db, sent = wired
    _add(db, "dreaming", _t("2026-07-01T03:00:00"))  # 早停了，但 disabled
    out = job_watchdog.run(now=_t("2026-07-30T19:07:00"))
    assert out["jobs_checked"] == 1 and out["findings"] == [] and sent == []


# ---------- 调度接入：--once job_watchdog 能跑 ----------

def test_job_watchdog_yml_registered_and_runs_once(db, tmp_path, monkeypatch):
    cfg = next(c for c in runner._load_job_configs() if c["name"] == "job_watchdog")
    assert cfg["enabled"] is True
    CronTrigger.from_crontab(cfg["schedule"], timezone=cfg["timezone"])
    assert runner._resolve_entry(cfg["entry"]) is job_watchdog.run

    monkeypatch.setattr(job_watchdog, "MemoryStore", lambda: MemoryStore(root=tmp_path / "memory"))
    monkeypatch.setattr(job_watchdog, "_send", lambda f: pytest.fail("空库不该告警"))
    assert runner.cmd_once("job_watchdog") == 0
    rows = db.execute("SELECT job_name, status FROM job_runs").fetchall()
    assert rows == [("job_watchdog", "success")]



def test_interrupted_run_with_finished_at_is_not_hung(db):
    """KeyboardInterrupt/SystemExit 绕过 except Exception：status 停在 running 但 finished_at 已落。"""
    _seed_event_watch_until(db, _t("2026-07-30T17:30:00"))
    db.execute(
        "INSERT INTO job_runs (job_name, started_at, finished_at, status) VALUES (?, ?, ?, 'running')",
        ("event_watch", _t("2026-07-30T18:00:00").isoformat(timespec="seconds"),
         _t("2026-07-30T18:00:20").isoformat(timespec="seconds")),
    )
    db.commit()
    found = find_problems(db, [EW_JOB], _t("2026-07-30T19:07:00"))
    assert not any(":hung:" in f["key"] for f in found)


def test_email_failure_after_dm_delivered_does_not_repeat_dm(db, tmp_path, monkeypatch):
    """DM 已送达、邮件抛错 → 不回滚 claim，下个小时不再重发 DM。"""
    monkeypatch.setattr(runner, "_load_job_configs", lambda: [
        {"name": "event_watch", "schedule": EW_CRON, "timezone": "Asia/Shanghai", "enabled": True},
    ])
    monkeypatch.setattr(runner, "_resolve_schedule", lambda name, s: s)
    store = MemoryStore(root=tmp_path / "memory")
    monkeypatch.setattr(job_watchdog, "MemoryStore", lambda: store)
    dms = []
    monkeypatch.setattr(job_watchdog, "send_discord_alert", lambda text: dms.append(text) or True)

    def _boom(**kw):
        raise RuntimeError("smtp down")

    monkeypatch.setattr(job_watchdog, "send_email_html", _boom)
    _seed_event_watch_until(db, _t("2026-07-30T17:30:00"))
    job_watchdog.run(now=_t("2026-07-30T19:07:00"))
    job_watchdog.run(now=_t("2026-07-30T20:07:00"))
    assert len(dms) == 1


def test_schedule_change_does_not_count_old_cron_misses_as_stale(db):
    """2026-10-08 实际误报：dca_daily 从 15:00 改成 30 15,18,21 并于 12:18 部署，看门狗拿新 cron
    去套旧 cron 下 10-07 15:00 的最后一次运行，报"应在 10-07 15:30/18:30 启动"。"""
    job = ("dca_daily", "30 15,18,21 * * mon-fri", "Asia/Shanghai")
    _add(db, "dca_daily", _t("2026-10-07T15:00:00"))
    since = {"dca_daily": _t("2026-10-08T12:18:00")}
    # 不传生效时刻（--once / 旧行为）→ 误报
    assert find_problems(db, [job], _t("2026-10-08T14:07:00"))
    # 传了 → 新 cron 生效后还没到该跑的时刻，不报
    assert find_problems(db, [job], _t("2026-10-08T14:07:00"), since) == []
    # 新 cron 生效后 15:30、18:30 都没跑 → 照样报停摆
    late = find_problems(db, [job], _t("2026-10-08T22:00:00"), since)
    assert len(late) == 1 and ":stale:" in late[0]["key"]
