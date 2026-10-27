"""调度器常驻（2026-10-27 hermes-clone 实测：插件用户没有任何东西在跑 jobs/*.yml）。

- 单实例锁：同一 INVEST_HOME 只能一个调度器；持有者退出（fd 关闭）即释放
- daemon 优先：锁在内嵌实例手里时 SIGTERM 请它退场再接手（只在同一 pid 命名空间）；内嵌实例抢不到锁就退
- openinvest-mcp（stdio）兜底拉起内嵌子进程的条件
- doctor 的 scheduler 检查是 warning 级，不改变 ready
- service install 生成的 systemd / launchd 单元
全部 hermetic：tmp 目录、假 Popen / subprocess.run，不起真服务。
"""
from __future__ import annotations

import os
import plistlib
import signal
import sqlite3
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timedelta

import pytest

from openinvest.scheduler import lock


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(lock, "INVEST_ROOT", tmp_path)
    return tmp_path


# ---------- 锁 ----------

def test_second_acquire_fails_until_holder_releases(root):
    f = lock.try_acquire("embedded")
    assert f is not None
    assert lock.try_acquire("daemon") is None        # flock 按打开的文件描述冲突，同进程也互斥
    h = lock.holder()
    assert h["mode"] == "embedded" and h["pid"] == os.getpid()
    f.close()                                         # = 持有进程退出
    assert lock.holder() is None
    assert lock.try_acquire("daemon") is not None


def test_holder_probe_does_not_take_the_lock(root):
    assert lock.holder() is None                      # 没有锁文件
    lock.lock_path().parent.mkdir(parents=True)
    lock.lock_path().write_text("{}")
    assert lock.holder() is None                      # 有文件没人持有
    assert lock.try_acquire("daemon") is not None     # 探测过后照样抢得到


def test_daemon_evicts_embedded_holder(root, monkeypatch):
    """装了系统服务后不用等宿主（Hermes gateway 可能常驻数天）退出才生效。"""
    from openinvest.scheduler import runner

    holder_proc = subprocess.Popen([sys.executable, "-c", textwrap.dedent(f"""
        import time
        from pathlib import Path
        import openinvest.scheduler.lock as lock
        lock.INVEST_ROOT = Path({str(root)!r})
        f = lock.try_acquire("embedded")
        print("held", flush=True)
        time.sleep(60)
    """)], stdout=subprocess.PIPE, text=True)
    try:
        assert holder_proc.stdout.readline().strip() == "held"
        real_sleep = time.sleep  # runner.time 就是 time 模块，patch 是全局的
        monkeypatch.setattr(runner.time, "sleep", lambda s: real_sleep(0.05))
        f = runner._acquire_lock(embedded_parent=None)
        assert f is not None and lock.holder()["mode"] == "daemon"
        assert holder_proc.wait(timeout=10) == -signal.SIGTERM
    finally:
        holder_proc.kill()


def _rewrite_body(f, **fields):
    import json

    f.seek(0)
    body = json.loads(f.read())
    body.update(fields)
    f.seek(0)
    f.truncate()
    json.dump(body, f)
    f.flush()


@pytest.mark.parametrize("other_side", [False, True])
def test_daemon_signals_embedded_holder_only_in_its_own_pid_namespace(root, monkeypatch, other_side):
    """compose 把 db/ bind mount 进容器：锁里的 pid 是宿主机命名空间的，容器里对它发信号会打错进程。"""
    from openinvest.scheduler import runner

    held = lock.try_acquire("embedded")
    _rewrite_body(held, pid=424242, **({"host": "the-docker-host"} if other_side else {}))
    killed = []
    monkeypatch.setattr(runner.os, "kill", lambda pid, sig: killed.append(pid))
    monkeypatch.setattr(runner.time, "sleep", lambda s: (_ for _ in ()).throw(_Stop()))
    with pytest.raises(_Stop):                        # 抢不到锁，进入等待
        runner._acquire_lock(embedded_parent=None)
    assert killed == ([] if other_side else [424242])


def test_embedded_lock_records_env_only_job_keys(root, monkeypatch):
    """装成服务后宿主 MCP env: 里的 key 带不过去——内嵌实例把名字记在锁里，service install 据此提示。"""
    for k in list(os.environ):
        if k.startswith("INVEST_") or k in lock._JOB_ENV_KEYS:
            monkeypatch.delenv(k)
    (root / ".env").write_text("DEEPSEEK_API_KEY=from-file\n")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "from-file")         # .env 里有：服务读得到
    monkeypatch.setenv("SEARXNG_URL", "http://searx.mcp-only.example")
    monkeypatch.setenv("INVEST_EVENT_MIN_SEVERITY", "3")
    monkeypatch.setenv("INVEST_EMBEDDED_SCHEDULER", "1")        # 只管本进程怎么起，不算
    held = lock.try_acquire("embedded")                         # noqa: F841
    assert lock.holder()["env_only"] == ["INVEST_EVENT_MIN_SEVERITY", "SEARXNG_URL"]
    assert "mcp-only.example" not in lock.lock_path().read_text()     # 只记名字


def test_embedded_gives_up_when_daemon_holds(root):
    from openinvest.scheduler import runner

    held = lock.try_acquire("daemon")
    assert held is not None
    assert runner._acquire_lock(embedded_parent=os.getppid()) is None
    assert runner.cmd_daemon(embedded_parent=os.getppid()) == 0   # 退出码 0：不是故障


# ---------- MCP 兜底拉起内嵌调度器 ----------

class _Stop(BaseException):
    pass


@pytest.fixture
def spawn(root, monkeypatch):
    """同步跑一轮 _start_embedded_scheduler 的循环，记录 Popen 调用。"""
    import threading

    from openinvest.connectors import mcp_server
    from openinvest.core import memory_store

    calls, children = [], []
    state = {"left": 1, "between": None}

    class Child:
        rc = None

        def poll(self):
            return self.rc

    def fake_popen(args, **kw):
        calls.append(args)
        children.append(Child())
        return children[-1]

    def fake_sleep(s):  # 每轮末尾 sleep 一次：跑满 rounds 轮就停，轮与轮之间可以改子进程状态
        state["left"] -= 1
        if state["left"] <= 0:
            raise _Stop()
        if state["between"]:
            state["between"](len(calls), children)

    monkeypatch.setattr(memory_store, "MEMORY_ROOT", root / "memory")
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(time, "sleep", fake_sleep)

    class SyncThread:
        def __init__(self, target, **kw):
            self.target = target

        def start(self):
            with pytest.raises(_Stop):
                self.target()

    monkeypatch.setattr(threading, "Thread", SyncThread)

    def run(rounds=1, between=None):
        state.update(left=rounds, between=between)
        mcp_server._start_embedded_scheduler()
        return calls
    return run


def _onboard(root):
    (root / "memory").mkdir(exist_ok=True)
    (root / "memory" / "user.md").write_text("x")


def test_mcp_spawns_embedded_scheduler_when_nobody_runs_one(root, spawn):
    _onboard(root)
    calls = spawn()
    assert len(calls) == 1
    assert calls[0][1:] == ["-m", "openinvest.scheduler.runner", "--embedded-parent", str(os.getpid())]


def test_mcp_respawns_only_after_child_exits(root, spawn):
    _onboard(root)
    seen = []

    def between(n_spawned, children):
        seen.append(n_spawned)
        if len(seen) == 2:
            children[0].rc = 0                        # 子进程退了（被 daemon 请走 / 崩了）
    calls = spawn(rounds=3, between=between)
    assert seen == [1, 1]                             # 子进程活着：第二轮不重复拉
    assert len(calls) == 2                            # 退出后下一轮重新拉起


@pytest.mark.parametrize("why", ["not_onboarded", "lock_held", "advisory", "opt_out", "remote"])
def test_mcp_does_not_spawn(root, spawn, monkeypatch, why):
    _onboard(root)
    held = lock.try_acquire("daemon") if why == "lock_held" else None  # noqa: F841  持有到测试结束
    if why == "not_onboarded":
        (root / "memory" / "user.md").unlink()
    elif why == "advisory":
        monkeypatch.setenv("INVEST_ADVISORY_MODE", "1")
    elif why == "remote":                             # 账本在 hub 上，本地 memory/ 是过期副本
        monkeypatch.setenv("INVEST_API_BASE", "https://hub.example")
    else:
        monkeypatch.setenv("INVEST_EMBEDDED_SCHEDULER", "0")
    assert spawn() == []


# ---------- doctor ----------

def _job_runs(root, started_at):
    (root / "db").mkdir(exist_ok=True)
    conn = sqlite3.connect(root / "db" / "jobs.sqlite")
    conn.execute("CREATE TABLE job_runs (id INTEGER PRIMARY KEY, job_name TEXT, started_at TEXT)")
    conn.execute("INSERT INTO job_runs (job_name, started_at) VALUES ('price_sentinel', ?)", (started_at,))
    conn.commit()
    conn.close()


def _ago(minutes):
    return (datetime.now().astimezone() - timedelta(minutes=minutes)).isoformat(timespec="seconds")


def test_doctor_scheduler_check(root, monkeypatch):
    from openinvest.services.skill_views import _scheduler_check

    assert _scheduler_check(root)["status"] == "warning"           # 没人在跑
    held = lock.try_acquire("embedded")                             # noqa: F841
    assert _scheduler_check(root)["status"] == "ok"                 # 刚起来，还没轮到 job
    _job_runs(root, _ago(5))
    assert _scheduler_check(root)["status"] == "ok"


def test_doctor_flags_stuck_scheduler(root, monkeypatch):
    from openinvest.services.skill_views import _scheduler_check

    lock.lock_path().parent.mkdir(parents=True)
    held = lock.try_acquire("embedded")
    held.seek(0)
    held.truncate()
    held.write(f'{{"pid": 1, "mode": "embedded", "started_at": "{_ago(300)}"}}')
    held.flush()
    _job_runs(root, _ago(200))
    c = _scheduler_check(root)
    assert c["status"] == "warning" and "卡住" in c["detail"]


def test_doctor_scheduler_warning_keeps_status_ready(tmp_path):
    from openinvest.core.memory_store import MemoryStore
    from openinvest.services.skill_views import build_doctor_view

    s = MemoryStore()
    s.write("user", "user", {"display_name": "t", "risk_tolerance": "Balanced"}, "")
    s.write("strategy", "strategy", {"target_assets": []}, "")
    s.write("portfolio", "state", {"schema_version": 2, "cash": {"CNY": 1.0}, "holdings": []}, "")
    v = build_doctor_view(tmp_path)
    assert {c["name"]: c["status"] for c in v["checks"]}["scheduler"] == "warning"
    assert v["status"] == "ready"


# ---------- service install ----------

def test_service_units(root, monkeypatch):
    from openinvest.skill_cmds import service_cmds as sc

    monkeypatch.setattr(sc, "INVEST_ROOT", root)
    args = ["/u/bin/uvx", "--from", "openinvest==1.2.3", "python", "-m", sc.RUNNER]
    unit = sc._systemd_unit(args)
    assert "ExecStart=/u/bin/uvx --from openinvest==1.2.3 python -m openinvest.scheduler.runner" in unit
    assert f'Environment="INVEST_HOME={root}"' in unit
    assert "WantedBy=default.target" in unit
    plist = plistlib.loads(sc._launchd_plist(args))
    assert plist["ProgramArguments"] == args and plist["EnvironmentVariables"] == {"INVEST_HOME": str(root)}


def test_service_exec_args_pin_version_only_for_uvx_ephemeral_env(monkeypatch):
    from openinvest import __version__
    from openinvest.skill_cmds import service_cmds as sc

    monkeypatch.setattr(sc.shutil, "which", lambda name: "/u/bin/uvx")
    monkeypatch.setattr(sys, "prefix", "/home/u/.cache/uv/archive-v0/AbC")
    assert sc._exec_args()[:3] == ["/u/bin/uvx", "--from", f"openinvest=={__version__}"]
    monkeypatch.setenv("OPENINVEST_SPEC", "/opt/openinvest-1.0.0-py3-none-any.whl")   # run.sh 导出的同一个 spec
    assert sc._exec_args()[:3] == ["/u/bin/uvx", "--from", "/opt/openinvest-1.0.0-py3-none-any.whl"]
    monkeypatch.setattr(sys, "prefix", "/home/u/projects/invest/.venv")
    assert sc._exec_args() == [sys.executable, "-m", sc.RUNNER]


@pytest.mark.parametrize("rc,status", [(0, "ok"), (1, "error")])
def test_service_install_linux(root, monkeypatch, tmp_path, rc, status):
    from openinvest.skill_cmds import service_cmds as sc

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    ran = []

    def fake_run(cmd, **kw):
        ran.append(cmd)
        out = "yes" if cmd[:2] == ["loginctl", "show-user"] else ""
        return subprocess.CompletedProcess(cmd, rc if cmd[0] == "systemctl" else 0, out, "")

    monkeypatch.setattr(sc.subprocess, "run", fake_run)
    r = sc._install()
    assert r["status"] == status
    assert (tmp_path / "home" / ".config" / "systemd" / "user" / sc.UNIT).exists()
    assert ["systemctl", "--user", "enable", "--now", sc.UNIT] in ran


def test_service_install_hints_keys_only_the_host_mcp_had(root, monkeypatch, tmp_path):
    from openinvest.skill_cmds import service_cmds as sc

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(sc.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "yes", ""))
    held = lock.try_acquire("embedded")
    _rewrite_body(held, env_only=["SEARXNG_URL"])     # 内嵌实例从宿主 MCP env: 继承来的
    r = sc._install()
    assert r["status"] == "ok" and "SEARXNG_URL" in r["env_hint"]


def test_service_disabled_in_remote_mode(capfd):
    from argparse import Namespace

    from openinvest import remote_dispatch as rd

    with pytest.raises(SystemExit):
        rd.maybe_dispatch_remote(Namespace(cmd="service", service_cmd="install"))
    assert "hub" in capfd.readouterr().out
