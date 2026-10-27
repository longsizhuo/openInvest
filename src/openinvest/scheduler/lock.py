"""调度器单实例锁：同一个 INVEST_HOME 只允许一个调度器在跑。

三种起法共用这把锁——系统服务（`openinvest service install`）、手动 / systemd 的 daemon
（`python -m openinvest.scheduler.runner`）、MCP 进程拉起的内嵌子进程（embedded）。
没有它，两个进程会把 price_sentinel / event_watch 等每个 job 跑两遍（只有部分 job 有 state_claim 幂等闸）。

fcntl.flock 持有到进程退出，进程死了内核自动释放——不存在"pid 文件残留要判僵尸"的问题
（对比 core/consolidation_lock.py 的 pid + stale 判据）。锁文件正文是持有者信息（JSON），
给 doctor / `service status` 读，也让系统服务启动时认出并请走内嵌实例。
"""
from __future__ import annotations

import fcntl
import json
import os
from datetime import datetime
from pathlib import Path
from typing import IO, Any, Dict, Optional

from openinvest.paths import INVEST_ROOT


def lock_path(root: Optional[Path] = None) -> Path:
    return (root or INVEST_ROOT) / "db" / ".scheduler.lock"


def _version() -> str:
    from openinvest import __version__
    return __version__


def try_acquire(mode: str) -> Optional[IO[str]]:
    """抢锁。抢到返回打开的锁文件（调用方持有到进程结束，别 close）；已被别的进程持有返回 None。

    mode: "daemon"（系统服务 / 手动起的常驻进程）或 "embedded"（MCP 拉起的子进程）。
    """
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+", encoding="utf-8")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        return None
    f.seek(0)
    f.truncate()
    json.dump({
        "pid": os.getpid(),
        "mode": mode,
        "version": _version(),
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }, f)
    f.flush()
    return f


def holder(root: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """当前持锁的调度器信息；没人持有返回 None。只探测不抢：拿共享锁试一下，拿得到 = 没人持排他锁。"""
    path = lock_path(root)
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                return json.loads(f.read() or "{}")
            except ValueError:
                return {}  # 持有者刚抢到、还没写完正文
        fcntl.flock(f, fcntl.LOCK_UN)
        return None
