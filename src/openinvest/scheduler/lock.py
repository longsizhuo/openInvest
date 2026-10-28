"""调度器单实例锁：同一个 INVEST_HOME 只允许一个调度器在跑。

三种起法共用这把锁——系统服务（`openinvest service install`）、手动 / systemd 的 daemon
（`python -m openinvest.scheduler.runner`）、MCP 进程拉起的内嵌子进程（embedded）。
没有它，两个进程会把 price_sentinel / event_watch 等每个 job 跑两遍（只有部分 job 有 state_claim 幂等闸）。

oslock.flock 持有到进程退出，进程死了内核自动释放——不存在"pid 文件残留要判僵尸"的问题
（对比 core/consolidation_lock.py 的 pid + stale 判据）。锁文件正文是持有者信息（JSON），
给 doctor / `service status` 读，也让系统服务启动时认出并请走内嵌实例。
"""
from __future__ import annotations

from openinvest.utils import oslock
import json
import os
import socket
from datetime import datetime
from pathlib import Path
from typing import IO, Any, Dict, List, Optional

from openinvest.paths import INVEST_ROOT


def lock_path(root: Optional[Path] = None) -> Path:
    return (root or INVEST_ROOT) / "db" / ".scheduler.lock"


def _version() -> str:
    from openinvest import __version__
    return __version__


def _pidns() -> Optional[str]:
    try:
        return os.readlink("/proc/self/ns/pid")
    except OSError:  # macOS 没有 /proc
        return None


def same_pid_namespace(h: Dict[str, Any]) -> bool:
    """持锁进程的 pid 在我这儿有没有意义。compose 把 db/ bind mount 进容器时，宿主机的内嵌实例
    和容器里的 daemon 共用这把锁，但 pid 属于不同命名空间——对着它发信号要么 ESRCH，要么误杀同号进程。"""
    return h.get("host") == socket.gethostname() and h.get("pidns") == _pidns()


# 调度器 job 会读的变量。内嵌实例从宿主 MCP 配置的 env: 继承；系统服务只有 INVEST_HOME + .env，
# 只配在宿主那边的 key 装成服务后就没了（发信 / LLM 归一化悄悄降级）
_JOB_ENV_KEYS = (
    "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL",
    "EMAIL_SENDER", "EMAIL_PASSWORD", "DIGEST_EMAIL_TO", "SEARXNG_URL", "EQUIBLES_API_KEY",
    "CHATBOT_ALERT_URL", "CHATBOT_INTERNAL_KEY", "GITHUB_TOKEN",
)
# INVEST_* 里只管本进程怎么起的那些，不用带进服务
_PROCESS_LOCAL_KEYS = {"INVEST_HOME", "INVEST_EMBEDDED_SCHEDULER", "INVEST_MCP_SCOPE", "INVEST_ADVISORY_MODE",
                       "INVEST_API_BASE", "INVEST_MCP_HOST", "INVEST_MCP_PORT"}


def env_only_keys() -> List[str]:
    """当前进程环境里有、INVEST_HOME/.env 里没有的 job 变量名（只有名字，不带值）。"""
    try:
        from dotenv import dotenv_values
        in_file = set(dotenv_values(INVEST_ROOT / ".env"))
    except ImportError:
        in_file = set()
    return sorted(
        k for k, v in os.environ.items()
        if v and k not in in_file and k not in _PROCESS_LOCAL_KEYS
        and (k in _JOB_ENV_KEYS or k.startswith("INVEST_"))
    )


def try_acquire(mode: str) -> Optional[IO[str]]:
    """抢锁。抢到返回打开的锁文件（调用方持有到进程结束，别 close）；已被别的进程持有返回 None。

    mode: "daemon"（系统服务 / 手动起的常驻进程）或 "embedded"（MCP 拉起的子进程）。
    """
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+", encoding="utf-8", newline="\n")
    try:
        oslock.flock(f, oslock.LOCK_EX | oslock.LOCK_NB)
    except BlockingIOError:
        f.close()
        return None
    body: Dict[str, Any] = {
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "pidns": _pidns(),
        "mode": mode,
        "version": _version(),
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    if mode == "embedded":
        body["env_only"] = env_only_keys()  # service install 读它提示用户把这些写进 .env
    f.seek(0)
    f.truncate()
    json.dump(body, f)
    f.flush()
    return f


def holder(root: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """当前持锁的调度器信息；没人持有返回 None。只探测不抢：拿共享锁试一下，拿得到 = 没人持排他锁。"""
    path = lock_path(root)
    if not path.exists():
        return None
    with open(path, "r+" if os.name == "nt" else "r", encoding="utf-8", newline="\n") as f:
        try:
            oslock.flock(f, oslock.LOCK_SH | oslock.LOCK_NB)
        except BlockingIOError:
            try:
                return json.loads(f.read() or "{}")
            except ValueError:
                return {}  # 持有者刚抢到、还没写完正文
        oslock.flock(f, oslock.LOCK_UN)
        return None
