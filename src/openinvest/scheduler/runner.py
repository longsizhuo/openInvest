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
import logging.handlers
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
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from openinvest.paths import INVEST_ROOT

ROOT = INVEST_ROOT
JOBS_DIR = ROOT / "jobs"
DB_DIR = ROOT / "db"
DB_DIR.mkdir(parents=True, exist_ok=True)
RUN_LOG_DB = DB_DIR / "openinvest.jobs.sqlite"
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
# 不再用 SQLAlchemyJobStore 持久化 APScheduler job：YAML 是唯一事实来源，
# 启动时 register_jobs 会带 replace_existing=True 全量重建。早期版本试图
# 持久化 _wrap_job 的闭包，pickle 报错导致 daemon 启动直接崩 —— 这是用户
# 从未收到自动邮件的根因。run_log（job_runs 表）走另一条手写 sqlite 路径，
# 不受这个改动影响。

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stderr),
        logging.handlers.RotatingFileHandler(
            LOG_DIR / "invest.log",
            maxBytes=10 * 1024 * 1024,  # 10 MB
            backupCount=5,
            encoding="utf-8",
        ),
    ],
)
log = logging.getLogger("openinvest.scheduler.runner")


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
    """解析 'openinvest.jobs.daily_report:run' -> callable"""
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
    # 默认 MemoryJobStore，YAML 每次启动重建，无需持久化
    sched = BackgroundScheduler(timezone="Asia/Shanghai")
    return sched


# schedule 可经 config 覆盖的 job → EventConfig 字段名映射（ADR-017 白名单 cron key）
_CONFIG_SCHEDULES: Dict[str, str] = {
    "event_watch": "watch_schedule",
    "price_sentinel": "sentinel_schedule",
}


def _resolve_schedule(name: str, yml_schedule: str) -> str:
    """job 的 schedule 解析：映射表内的 job 优先读 config override（API/GUI/CLI 可改）。

    _force_reload：scheduler 是长驻进程，不强制重读会命中本进程旧缓存 →
    用户经 API 改了不生效（同 jobs/dca_daily.py 的先例）。
    config 层任何异常都不该拦住调度器启动，一律退回 yml 兜底值。
    """
    attr = _CONFIG_SCHEDULES.get(name)
    if attr is None:
        return yml_schedule
    try:
        from openinvest.core.config import load_config
        sched_str = (getattr(load_config(_force_reload=True).event, attr, "") or "").strip()
        if sched_str:
            CronTrigger.from_crontab(sched_str)  # 白名单写入时已校验；这里兜底防手改 overrides json
            return sched_str
    except Exception as e:
        log.warning(f"[{name}] 读 config {attr} 失败，退回 yml 默认: {e}")
    return yml_schedule


def register_jobs(sched: BackgroundScheduler, quiet: bool = False) -> List[Dict[str, Any]]:
    """从 jobs/*.yml 注册所有 enabled 任务。

    replace_existing=True 使其幂等——定期重跑本函数即可拾取 yml / config 的
    schedule 改动（cron 触发器重算 next fire 不影响正在运行的实例）。
    quiet=True 给周期刷新用，仅在 schedule 真的变了时打 INFO，避免每 10 分钟刷日志。

    disabled 任务会主动 sched.remove_job()——否则"改 yml enabled: false 无需重启
    生效"这个卖点对禁用操作是假的：本函数只 add/replace，从不 remove，之前注册
    过的 job 会在 disabled 之后继续按旧 trigger 跑到天荒地老，直到进程重启。
    """
    configs = _load_job_configs()
    registered = []
    for cfg in configs:
        if not cfg.get("enabled", False):
            if not quiet:
                log.info(f"[{cfg['name']}] disabled，跳过")
            if sched.get_job(cfg["name"]) is not None:
                sched.remove_job(cfg["name"])
                _LAST_SCHEDULES.pop(cfg["name"], None)
                log.info(f"[{cfg['name']}] 已从调度器移除（disabled）")
            continue

        schedule = _resolve_schedule(cfg["name"], cfg["schedule"])
        # 变更检测：仅当该 job 的 cron 表达式与上次注册不同才重注册 + 打日志
        prev = _LAST_SCHEDULES.get(cfg["name"])
        if quiet and prev == schedule:
            registered.append(cfg)
            continue

        trigger = CronTrigger.from_crontab(schedule, timezone=cfg.get("timezone", "Asia/Shanghai"))
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
        _LAST_SCHEDULES[cfg["name"]] = schedule
        registered.append(cfg)
        if prev is not None and prev != schedule:
            log.info(f"[{cfg['name']}] schedule 变更: {prev} → {schedule}")
        elif not quiet:
            log.info(f"[{cfg['name']}] 已注册: {schedule} @ {cfg.get('timezone')}")
    return registered


# 上次注册的 schedule 快照（变更检测用；仅 daemon 进程内有效）
_LAST_SCHEDULES: Dict[str, str] = {}


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
    # 配置热拾取：每 10 分钟重跑 register_jobs（quiet 模式，schedule 没变时零日志零操作）。
    # 用户经 API/GUI 改 event.watch_schedule 后 ≤10 分钟生效，无需重启 daemon（ADR-017 跨进程共读）。
    sched.add_job(
        lambda: register_jobs(sched, quiet=True),
        "interval",
        minutes=10,
        id="_config_refresh",
        name="_config_refresh",
    )
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
