"""D3 运维加固：per-job 墙钟超时 + 外部心跳（评审路线 D3）

事故背景：event_watch 2026-07-30 卡死在新闻抓取线程池 join，APScheduler max_instances=1
让之后 21 天的触发全被跳过。线程杀不掉，所以超时的语义是"标 timeout + 放弃该线程 +
释放 max_instances 槽位"，并设每 job 的泄漏线程上限防无限堆积。
全部 hermetic：tmp sqlite、假 job、假 urlopen，不联网。
"""
from __future__ import annotations

import sqlite3
import threading
import time
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from apscheduler.schedulers.background import BackgroundScheduler

from openinvest.jobs.job_watchdog import find_problems
from openinvest.scheduler import runner

TINY = 0.01  # 分钟 = 0.6 秒


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "RUN_LOG_DB", tmp_path / "jobs.sqlite")
    monkeypatch.setattr(runner, "_ORPHANS", {})
    runner._ensure_run_log_table()
    conn = sqlite3.connect(runner.RUN_LOG_DB, check_same_thread=False)
    yield conn
    conn.close()


def _rows(db):
    return db.execute("SELECT status, error, output_excerpt FROM job_runs ORDER BY id").fetchall()


def _slow_then_fast(release: threading.Event):
    """第一次调用阻塞到 release（模拟线程池 join 卡死），之后的调用立即返回。"""
    calls = []

    def job():
        calls.append(1)
        if len(calls) == 1:
            release.wait(30)
            return {"late": True}
        return {"status": "ok"}

    return job, calls


def test_sleepy_job_marked_timeout_and_next_trigger_runs(db, monkeypatch):
    release = threading.Event()
    job, calls = _slow_then_fast(release)
    monkeypatch.setattr(runner, "_resolve_entry", lambda entry: job)
    wrapped = runner._wrap_job("probe", "x:y", TINY)
    try:
        t0 = time.monotonic()
        wrapped()                                   # 卡住的第一次：超时返回
        assert time.monotonic() - t0 < 10           # 没被 job 拖住
        wrapped()                                   # 下一次触发：能进来并跑完
    finally:
        release.set()

    rows = _rows(db)
    assert [r[0] for r in rows] == ["timeout", "success"]
    assert "TimeoutError" in rows[0][1] and "0.01" in rows[0][1]
    # 超时行附线程栈，直接指出卡在哪（job() 里的 release.wait）
    assert "release.wait(30)" in rows[0][2]
    assert len(calls) == 2


def test_apscheduler_max_instances_slot_freed_after_timeout(db, monkeypatch):
    """真 APScheduler + max_instances=1：没有超时则第二次触发被永久跳过（21 天事故原样）。"""
    release = threading.Event()
    job, calls = _slow_then_fast(release)
    monkeypatch.setattr(runner, "_resolve_entry", lambda entry: job)
    sched = BackgroundScheduler(timezone="Asia/Shanghai")
    sched.add_job(runner._wrap_job("probe", "x:y", TINY), "interval", seconds=0.2,
                  id="probe", max_instances=1, coalesce=True)
    sched.start()
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            statuses = [r[0] for r in _rows(db)]
            if "timeout" in statuses and "success" in statuses:
                break
            time.sleep(0.1)
    finally:
        sched.shutdown(wait=False)
        release.set()
    statuses = [r[0] for r in _rows(db)]
    assert "timeout" in statuses and "success" in statuses
    assert statuses.index("timeout") < statuses.index("success")


def test_failed_job_still_recorded_failed(db, monkeypatch):
    def boom():
        raise ValueError("nope")

    monkeypatch.setattr(runner, "_resolve_entry", lambda entry: boom)
    runner._wrap_job("probe", "x:y", 1)()
    [(status, error, out)] = _rows(db)
    assert status == "failed" and error == "ValueError: nope" and "Traceback" in out


def test_orphan_cap_refuses_start_without_row(db, monkeypatch):
    release = threading.Event()
    stuck = [threading.Thread(target=release.wait, args=(30,), daemon=True)
             for _ in range(runner.MAX_ORPHANS)]
    for t in stuck:
        t.start()
    runner._ORPHANS["probe"] = list(stuck)
    ran = []
    monkeypatch.setattr(runner, "_resolve_entry", lambda entry: (lambda: ran.append(1)))
    try:
        runner._wrap_job("probe", "x:y", 1)()
        assert ran == [] and _rows(db) == []        # 拒绝启动，不泄漏第 4 个线程
        release.set()
        for t in stuck:
            t.join(5)
        runner._wrap_job("probe", "x:y", 1)()       # 孤儿都退出后恢复
        assert ran == [1] and [r[0] for r in _rows(db)] == ["success"]
    finally:
        release.set()


def test_job_timeout_minutes_parsing():
    assert runner._job_timeout_minutes({"name": "a"}) == runner.DEFAULT_TIMEOUT_MINUTES == 30
    assert runner._job_timeout_minutes({"name": "a", "timeout_minutes": 20}) == 20
    for bad in (0, -5, "abc", None):
        assert runner._job_timeout_minutes({"name": "a", "timeout_minutes": bad}) == 30


def test_shipped_job_ymls_carry_sane_timeouts():
    cfgs = {c["name"]: c for c in runner._load_job_configs()}
    assert runner._job_timeout_minutes(cfgs["event_watch"]) == 20
    for c in cfgs.values():
        assert 0 < runner._job_timeout_minutes(c) <= 120, c["name"]


# ---------- --once：退出码要让外层 cron/CI 看得出超时 ----------

def _once_cfg(monkeypatch):
    monkeypatch.setattr(runner, "_load_job_configs", lambda: [
        {"name": "probe", "schedule": "* * * * *", "entry": "x:y", "timeout_minutes": TINY}])


def test_once_returns_nonzero_on_timeout_and_zero_on_success(db, monkeypatch):
    _once_cfg(monkeypatch)
    release = threading.Event()
    job, _ = _slow_then_fast(release)
    monkeypatch.setattr(runner, "_resolve_entry", lambda entry: job)
    try:
        assert runner.cmd_once("probe") == 1          # 第一次卡住 → timeout → 非 0
        assert runner.cmd_once("probe") == 0          # 第二次正常 → 0
    finally:
        release.set()
    assert [r[0] for r in _rows(db)] == ["timeout", "success"]


def test_once_returns_nonzero_on_failed_job(db, monkeypatch):
    _once_cfg(monkeypatch)

    def boom():
        raise RuntimeError("x")

    monkeypatch.setattr(runner, "_resolve_entry", lambda entry: boom)
    assert runner.cmd_once("probe") == 1


# ---------- cmd_daemon 注册心跳，且启动即触发 ----------

def test_daemon_registers_heartbeat_firing_immediately(db, monkeypatch):
    added = []

    class FakeSched:
        def add_job(self, fn, trigger=None, **kw):
            added.append((fn, trigger, kw))

        def start(self):
            pass

        def shutdown(self, wait=True):
            pass

    handlers = {}
    monkeypatch.setattr(runner, "build_scheduler", lambda: FakeSched())
    monkeypatch.setattr(runner, "register_jobs", lambda sched, quiet=False: [])
    monkeypatch.setattr(runner.signal, "signal", lambda sig, h: handlers.setdefault(sig, h))
    # 主循环的 sleep(1) 里模拟收到 SIGTERM，让 cmd_daemon 正常返回
    monkeypatch.setattr(runner.time, "sleep", lambda s: handlers[runner.signal.SIGTERM](runner.signal.SIGTERM, None))
    t0 = datetime.now(ZoneInfo("UTC"))
    assert runner.cmd_daemon() == 0
    [(fn, trigger, kw)] = [a for a in added if a[2].get("id") == "_heartbeat"]
    assert fn is runner._heartbeat and trigger == "interval"
    assert kw["minutes"] == runner.HEARTBEAT_INTERVAL_MIN == 5
    assert abs((kw["next_run_time"] - t0).total_seconds()) < 5   # 启动即 ping，不是等 5 分钟


# ---------- 看门狗：timeout 行按失败告警 ----------

SH = ZoneInfo("Asia/Shanghai")


def _add_timeout(conn, job, started: datetime, *, secs=1200):
    conn.execute(
        "INSERT INTO job_runs (job_name, started_at, finished_at, status, error) VALUES (?,?,?, 'timeout', ?)",
        (job, started.isoformat(timespec="seconds"),
         (started + timedelta(seconds=secs)).isoformat(timespec="seconds"), "TimeoutError: 超过 20 分钟未结束"),
    )
    conn.commit()


def test_watchdog_reports_timeout_even_after_job_recovered(db):
    """超时后下一次触发已 success：最新一行是 success，但 24h 内的 timeout 仍要报一次。"""
    job = ("event_watch", "*/30 * * * *", "Asia/Shanghai")
    t0 = datetime(2026, 10, 18, 8, 0, tzinfo=SH)
    _add_timeout(db, "event_watch", t0)
    db.execute("INSERT INTO job_runs (job_name, started_at, finished_at, status) VALUES (?,?,?, 'success')",
               ("event_watch", (t0 + timedelta(minutes=30)).isoformat(), (t0 + timedelta(minutes=31)).isoformat()))
    db.commit()
    found = find_problems(db, [job], t0 + timedelta(minutes=70))
    assert [f["key"] for f in found if ":timeout:" in f["key"]] == ["event_watch:timeout:1"]
    assert "执行超时" in found[0]["text"]
    # 24h 之后不再报（每条 timeout 只在窗口内报一次，持续卡死由 stale 接手）
    later = find_problems(db, [job], t0 + timedelta(hours=26))
    assert not any(":timeout:" in f["key"] for f in later)


# ---------- 外部心跳 ----------

class _Resp:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        return b"OK"


@pytest.fixture
def pings(monkeypatch):
    calls = []

    def fake_urlopen(url, timeout=None):
        calls.append((url, timeout))
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return calls


def test_heartbeat_noop_when_url_empty(pings):
    runner._heartbeat()
    assert pings == []


def test_heartbeat_pings_configured_url_from_env(pings, monkeypatch):
    monkeypatch.setenv("INVEST_SCHEDULER_HEARTBEAT_URL", "https://hc-ping.com/abc-123")
    runner._heartbeat()
    assert pings == [("https://hc-ping.com/abc-123", 10)]


def test_heartbeat_url_with_comma_survives_env_parsing(pings, monkeypatch):
    monkeypatch.setenv("INVEST_SCHEDULER_HEARTBEAT_URL", "https://ping.example/x?a=1,2")
    runner._heartbeat()
    assert pings[0][0] == "https://ping.example/x?a=1,2"


def test_heartbeat_failure_is_logged_not_raised(monkeypatch, caplog):
    monkeypatch.setenv("INVEST_SCHEDULER_HEARTBEAT_URL", "https://hc-ping.com/abc")

    def boom(url, timeout=None):
        raise OSError("network down")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with caplog.at_level("WARNING", logger="openinvest.scheduler.runner"):
        runner._heartbeat()  # 不抛
    assert "ping 失败" in caplog.text and "network down" in caplog.text


def test_heartbeat_rejects_non_http_scheme(pings, monkeypatch):
    monkeypatch.setenv("INVEST_SCHEDULER_HEARTBEAT_URL", "file:///etc/passwd")
    runner._heartbeat()
    assert pings == []
