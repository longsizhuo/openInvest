"""APScheduler 调度器入口

替代旧的 scheduler.py（while True + sleep）。
- jobs/*.yml 自动发现并注册
- 持久化到 db/jobs.sqlite（崩了重启状态不丢）
- 每次任务执行写 run_log 表，供 weekly_review 复盘命中率
- 支持 --once <job_name> 单次执行模式（cli 触发）
- 支持 --list 列出所有任务

使用：
    python -m scheduler.runner               # 后台跑所有 enabled job
    python -m scheduler.runner --once daily_report
    python -m scheduler.runner --list
"""
from __future__ import annotations

import argparse
import importlib
import logging
import os
import signal
import sqlite3
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

ROOT = Path(__file__).parent.parent
JOBS_DIR = ROOT / "jobs"
DB_DIR = ROOT / "db"
DB_DIR.mkdir(parents=True, exist_ok=True)
JOBS_DB_URL = f"sqlite:///{DB_DIR / 'jobs.sqlite'}"
RUN_LOG_DB = DB_DIR / "jobs.sqlite"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("scheduler.runner")


# ---------- run_log 表（命中率复盘用） ----------

def _ensure_run_log_table() -> None:
    conn = sqlite3.connect(RUN_LOG_DB, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS job_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_name TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT,
            error TEXT,
            output_excerpt TEXT
        )
    """)
    conn.commit()
    conn.close()


def _record_run(job_name: str, started_at: str,
                finished_at: str, status: str,
                error: Optional[str], output: Optional[str]) -> None:
    conn = sqlite3.connect(RUN_LOG_DB, check_same_thread=False)
    excerpt = (output or "")[:2000]
    conn.execute(
        "INSERT INTO job_runs (job_name, started_at, finished_at, status, error, output_excerpt) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (job_name, started_at, finished_at, status, error, excerpt),
    )
    conn.commit()
    conn.close()


# ---------- job 加载 ----------

def _load_job_configs() -> List[Dict[str, Any]]:
    """从 jobs/*.yml 加载所有任务配置"""
    configs = []
    for yml in sorted(JOBS_DIR.glob("*.yml")):
        with open(yml, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        cfg["_source"] = yml.name
        configs.append(cfg)
    return configs


def _resolve_entry(entry: str) -> Callable[[], Any]:
    """解析 'jobs.daily_report:run' -> callable"""
    module_name, _, fn_name = entry.partition(":")
    if not fn_name:
        raise ValueError(f"Invalid entry format: {entry} (expected 'pkg.mod:fn')")
    module = importlib.import_module(module_name)
    fn = getattr(module, fn_name, None)
    if fn is None:
        raise AttributeError(f"{module_name} has no attribute {fn_name}")
    return fn


def _wrap_job(job_name: str, entry: str) -> Callable[[], None]:
    """把 entry 包装成一个会写 run_log + 异常隔离的可执行函数"""
    def wrapped() -> None:
        started = datetime.now().astimezone().isoformat(timespec="seconds")
        log.info(f"[{job_name}] 启动 (started={started})")
        status, error, output = "running", None, None
        try:
            fn = _resolve_entry(entry)
            result = fn()
            status = "success"
            output = str(result) if result is not None else ""
            log.info(f"[{job_name}] 成功")
        except Exception as e:
            status = "failed"
            error = f"{type(e).__name__}: {e}"
            output = traceback.format_exc()
            log.exception(f"[{job_name}] 失败")
        finally:
            finished = datetime.now().astimezone().isoformat(timespec="seconds")
            _record_run(job_name, started, finished, status, error, output)
    return wrapped


# ---------- scheduler 管理 ----------

def build_scheduler() -> BackgroundScheduler:
    sched = BackgroundScheduler(
        jobstores={"default": SQLAlchemyJobStore(url=JOBS_DB_URL)},
        timezone="Asia/Shanghai",
    )
    return sched


def register_jobs(sched: BackgroundScheduler) -> List[Dict[str, Any]]:
    """从 jobs/*.yml 注册所有 enabled 任务"""
    configs = _load_job_configs()
    registered = []
    for cfg in configs:
        if not cfg.get("enabled", False):
            log.info(f"[{cfg['name']}] disabled，跳过")
            continue

        trigger = CronTrigger.from_crontab(cfg["schedule"], timezone=cfg.get("timezone", "Asia/Shanghai"))
        sched.add_job(
            _wrap_job(cfg["name"], cfg["entry"]),
            trigger=trigger,
            id=cfg["name"],
            name=cfg["name"],
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=600,  # 重启后 10 分钟内的 misfire 也补跑
        )
        registered.append(cfg)
        log.info(f"[{cfg['name']}] 已注册: {cfg['schedule']} @ {cfg.get('timezone')}")
    return registered


# ---------- CLI ----------

def cmd_list() -> None:
    configs = _load_job_configs()
    print(f"{'name':<20} {'schedule':<20} {'enabled':<8} entry")
    print("-" * 80)
    for c in configs:
        print(f"{c['name']:<20} {c['schedule']:<20} {str(c.get('enabled', False)):<8} {c['entry']}")


def cmd_once(job_name: str) -> int:
    configs = _load_job_configs()
    cfg = next((c for c in configs if c["name"] == job_name), None)
    if cfg is None:
        log.error(f"未知 job: {job_name}")
        return 1
    log.info(f"[{job_name}] 单次执行模式")
    _ensure_run_log_table()
    _wrap_job(cfg["name"], cfg["entry"])()
    return 0


def cmd_daemon() -> int:
    _ensure_run_log_table()
    sched = build_scheduler()
    register_jobs(sched)
    sched.start()
    log.info("调度器已启动。Ctrl+C 退出。")

    stopping = {"flag": False}

    def _stop(signum, frame):
        log.info(f"收到信号 {signum}，关闭调度器...")
        stopping["flag"] = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    try:
        while not stopping["flag"]:
            time.sleep(1)
    finally:
        sched.shutdown(wait=True)
        log.info("调度器已关闭。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="invest scheduler runner")
    parser.add_argument("--list", action="store_true", help="列出所有 job")
    parser.add_argument("--once", metavar="JOB_NAME", help="单次执行某个 job")
    args = parser.parse_args()

    if args.list:
        cmd_list()
        return 0
    if args.once:
        return cmd_once(args.once)
    return cmd_daemon()


if __name__ == "__main__":
    sys.exit(main())
