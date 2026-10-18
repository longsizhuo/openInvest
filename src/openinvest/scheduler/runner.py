"""APScheduler 调度器入口

替代旧的 scheduler.py（while True + sleep）。
- jobs/*.yml 自动发现并注册
- 持久化到 db/jobs.sqlite（崩了重启状态不丢）
- 每次任务执行写 run_log 表（job_runs），供 job_watchdog 巡检
- 支持 --once <job_name> 单次执行模式（cli 触发）
- 支持 --list 列出所有任务

daemon 以 `python -m` 启动，本模块在进程里叫 __main__：job 里 `import runner` 拿到的是
另一份空副本。跨 job 共享的进程级状态别放这里，放 scheduler/cron.py 这类按名导入的模块。

每个 job 有墙钟超时（jobs/*.yml `timeout_minutes`，默认 30）；外部心跳 `scheduler.heartbeat_url`
每 5 分钟 GET 一次（dead-man switch）。两者的设计取舍见 `_wrap_job` / `_heartbeat` 注释。

使用：
    python -m openinvest.scheduler.runner               # 后台跑所有 enabled job
    python -m openinvest.scheduler.runner --once daily_report
    python -m openinvest.scheduler.runner --list
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
import threading
import time
import traceback
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from openinvest.paths import INVEST_ROOT
from openinvest.scheduler.cron import SCHEDULED_SINCE, crontab_trigger  # 标准 crontab 星期编号（0/7=周日）

ROOT = INVEST_ROOT
import openinvest.jobs as _jobs_pkg
JOBS_DIR = Path(_jobs_pkg.__file__).parent  # job yml 是包资源，跟包走
DB_DIR = ROOT / "db"
DB_DIR.mkdir(parents=True, exist_ok=True)
RUN_LOG_DB = DB_DIR / "jobs.sqlite"
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


# ---------- run_log 表（job_watchdog 巡检用） ----------

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


def _record_start(job_name: str, started_at: str) -> Optional[int]:
    """开跑即落一行 status='running'，返回 row id 供收尾时 UPDATE 同一行。

    2026-10-07：此前只在 finally 里 INSERT（跑完才有行）——event_watch 7/30 卡死在
    线程池 join，max_instances=1 让之后 21 天的触发全被跳过，job_runs 一行没有，
    "开始了但没结束"在库里不可见。running 行是 jobs/job_watchdog 判卡死的依据。
    记录失败只打 warning 返回 None（收尾退回整行 INSERT）——监控写入不能拦住 job 本身。
    """
    try:
        conn = sqlite3.connect(RUN_LOG_DB, check_same_thread=False)
        try:
            cur = conn.execute(
                "INSERT INTO job_runs (job_name, started_at, status) VALUES (?, ?, 'running')",
                (job_name, started_at),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()
    except Exception as e:
        log.warning(f"[{job_name}] 记录 running 行失败（不影响 job 执行）: {e}")
        return None


def _record_run(job_name: str, started_at: str,
                finished_at: str, status: str,
                error: Optional[str], output: Optional[str],
                run_id: Optional[int] = None) -> None:
    conn = sqlite3.connect(RUN_LOG_DB, check_same_thread=False)
    excerpt = (output or "")[:2000]
    if run_id is not None:
        conn.execute(
            "UPDATE job_runs SET finished_at = ?, status = ?, error = ?, output_excerpt = ? "
            "WHERE id = ?",
            (finished_at, status, error, excerpt, run_id),
        )
    else:
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


DEFAULT_TIMEOUT_MINUTES = 30.0
# 超时后仍活着的线程上限（每 job）。Python 线程杀不掉：超时只能"放弃等它"，让 APScheduler 的
# max_instances=1 槽位释放、下一次触发能进来；被放弃的线程留在后台（daemon，不拦进程退出）。
# 真卡死的 job 每次触发都会再泄漏一个线程，所以设上限：已有 MAX_ORPHANS 个没退出的就拒绝再启动
# （只打 error 日志，不写 job_runs 行——看门狗的 stale 判据会在 2 个周期后接着报，重启调度器清空）。
MAX_ORPHANS = 3
_ORPHANS: Dict[str, List[threading.Thread]] = {}


def _job_timeout_minutes(cfg: Dict[str, Any]) -> float:
    """jobs/*.yml 的 `timeout_minutes`；缺省 / 非正数 / 非数字 → 默认 30（打 warning）。"""
    raw = cfg.get("timeout_minutes", DEFAULT_TIMEOUT_MINUTES)
    try:
        v = float(raw)
        if v > 0:
            return v
    except (TypeError, ValueError):
        pass
    log.warning(f"[{cfg.get('name')}] timeout_minutes={raw!r} 非法，退回默认 {DEFAULT_TIMEOUT_MINUTES:g}")
    return DEFAULT_TIMEOUT_MINUTES


def _run_with_timeout(job_name: str, entry: str, timeout_minutes: float):
    """在 daemon 线程里跑 entry，最多等 timeout_minutes。返回 (status, error, output)。

    超时 → status='timeout'，output 附该线程此刻的调用栈（直接指出卡在哪，2026-07-30 的
    event_watch 线程池 join 卡死当时就缺这个）。
    """
    box: Dict[str, Any] = {}

    def target() -> None:
        try:
            box["result"] = _resolve_entry(entry)()
        except BaseException as e:  # noqa: BLE001  SystemExit 等也要落成 failed，别让行停在 running
            box["exc"] = e
            box["tb"] = traceback.format_exc()
        if box.get("abandoned"):
            log.warning(f"[{job_name}] 超时被放弃的线程后来自行结束了（慢而非死锁）")

    t = threading.Thread(target=target, name=f"job:{job_name}", daemon=True)
    t.start()
    t.join(timeout_minutes * 60)
    if t.is_alive():
        box["abandoned"] = True
        _ORPHANS.setdefault(job_name, []).append(t)
        frame = sys._current_frames().get(t.ident)
        stack = "".join(traceback.format_stack(frame)) if frame is not None else "(线程栈不可得)"
        log.error(f"[{job_name}] 超过 {timeout_minutes:g} 分钟未结束，标 timeout 并放弃该线程\n{stack}")
        return (
            "timeout",
            f"TimeoutError: 超过 {timeout_minutes:g} 分钟未结束（线程无法强杀，仍在后台）",
            f"超时时刻的线程调用栈:\n{stack}",
        )
    if "exc" in box:
        log.error(f"[{job_name}] 失败", exc_info=box["exc"])
        return "failed", f"{type(box['exc']).__name__}: {box['exc']}", box["tb"]
    log.info(f"[{job_name}] 成功")
    result = box.get("result")
    return "success", None, str(result) if result is not None else ""


def _wrap_job(job_name: str, entry: str,
              timeout_minutes: float = DEFAULT_TIMEOUT_MINUTES) -> Callable[[], str]:
    """把 entry 包装成一个会写 run_log + 异常隔离 + 墙钟超时的可执行函数。

    返回本次终态 success / failed / timeout，或 "refused"（遗留线程满额拒绝启动）；
    APScheduler 忽略返回值，`--once` 据此决定退出码。"""
    def wrapped() -> str:
        alive = [t for t in _ORPHANS.get(job_name, []) if t.is_alive()]
        _ORPHANS[job_name] = alive
        if len(alive) >= MAX_ORPHANS:
            log.error(f"[{job_name}] 已有 {len(alive)} 个超时后仍未退出的线程（上限 {MAX_ORPHANS}），"
                      f"拒绝再启动；重启调度器可清空")
            return "refused"
        started = datetime.now().astimezone().isoformat(timespec="seconds")
        log.info(f"[{job_name}] 启动 (started={started}, timeout={timeout_minutes:g}min)")
        run_id = _record_start(job_name, started)
        status, error, output = "running", None, None
        try:
            status, error, output = _run_with_timeout(job_name, entry, timeout_minutes)
        finally:
            finished = datetime.now().astimezone().isoformat(timespec="seconds")
            _record_run(job_name, started, finished, status, error, output, run_id)
        return status
    return wrapped


# ---------- 外部心跳（dead-man switch） ----------

HEARTBEAT_INTERVAL_MIN = 5


def _heartbeat() -> None:
    """GET 一次 `scheduler.heartbeat_url`（healthchecks.io 式 ping URL；空 = 不发）。

    job_watchdog 自己是调度器里的 job——调度进程死了/线程池全卡死，它也一起哑（job_watchdog.py
    docstring 的盲区）。本函数同样跑在调度器线程池里，所以 ping 断了 = 进程没了或池子满了，
    由外部服务（healthchecks.io 等，period 5min / grace 10min）在它那边告警。
    每次重读 config：改 URL 不用重启；失败只记 warning，绝不影响调度。
    """
    try:
        from openinvest.core.config import load_config
        url = (load_config(_force_reload=True).scheduler.heartbeat_url or "").strip()
        if not url:
            return
        if not url.startswith(("http://", "https://")):
            log.warning("[heartbeat] heartbeat_url 必须是 http(s)://，已忽略")
            return
        with urllib.request.urlopen(url, timeout=10) as resp:
            resp.read(1)
    except Exception as e:  # noqa: BLE001  监控外发永远不拦调度
        log.warning(f"[heartbeat] ping 失败（不影响调度）: {type(e).__name__}: {e}")


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
            crontab_trigger(sched_str)  # 白名单写入时已校验；这里兜底防手改 overrides json
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

        trigger = crontab_trigger(schedule, timezone=cfg.get("timezone", "Asia/Shanghai"))
        sched.add_job(
            _wrap_job(cfg["name"], cfg["entry"], _job_timeout_minutes(cfg)),
            trigger=trigger,
            id=cfg["name"],
            name=cfg["name"],
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=600,  # 重启后 10 分钟内的 misfire 也补跑
        )
        _LAST_SCHEDULES[cfg["name"]] = schedule
        SCHEDULED_SINCE[cfg["name"]] = datetime.now(timezone.utc)
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
    status = _wrap_job(cfg["name"], cfg["entry"], _job_timeout_minutes(cfg))()
    if status == "timeout":
        # 一次性进程：被放弃的线程随进程退出被直接终止（非原子的写 / 发了一半的外部副作用无从保证）
        log.warning(f"[{job_name}] 单次执行超时，进程即将退出，被放弃的线程随之终止")
    return 0 if status == "success" else 1


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
    # 外部心跳：启动即 ping 一次，之后每 5 分钟；URL 为空时是个空操作
    sched.add_job(
        _heartbeat, "interval", minutes=HEARTBEAT_INTERVAL_MIN,
        id="_heartbeat", name="_heartbeat", next_run_time=datetime.now(timezone.utc),
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
