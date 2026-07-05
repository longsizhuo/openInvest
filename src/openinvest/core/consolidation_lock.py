"""仿 Claude Code v2.1.88 leaked 的 consolidationLock.ts
(src/services/autoDream/consolidationLock.ts)

防止 Dreaming 被三处同时触发（cron / NapCat / Skill）撕裂数据。
设计要点：
- mtime = lastConsolidatedAt（一次 stat 即可读上次完成时间）
- body = holder PID（PID 复用守护：进程死了释放）
- HOLDER_STALE_MS = 60min（PID 还在但卡住超过这个时长视为僵尸）

用法：
    prior = try_acquire_consolidation_lock()
    if prior is None:
        return {"status": "skipped", "reason": "lock_held"}
    try:
        ... do dreaming ...
    except Exception:
        rollback_consolidation_lock(prior)
        raise
"""
from __future__ import annotations

import errno
import fcntl
import os
import time
from pathlib import Path
from typing import Optional

LOCK_FILE = ".consolidate-lock"
LOCK_FCNTL_FILE = ".consolidate-lock.flock"  # 配对的 fcntl mutex（audit algo C3）
HOLDER_STALE_MS = 60 * 60 * 1000  # 60 min — 与 leaked 源码一致


def _is_process_running(pid: int) -> bool:
    """跨平台检测 PID 是否存活"""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _lock_path(memory_root: Path) -> Path:
    return memory_root / ".dreams" / LOCK_FILE


def read_last_consolidated_at(memory_root: Path) -> float:
    """返回上次完成的 mtime（毫秒）；不存在返回 0"""
    p = _lock_path(memory_root)
    try:
        return p.stat().st_mtime * 1000
    except FileNotFoundError:
        return 0


def try_acquire_consolidation_lock(memory_root: Path) -> Optional[float]:
    """
    成功 → 返回 prior mtime（用于 rollback）
    失败 → 返回 None（被其他活进程占着）

    锁占用判定：
    - 文件存在且 mtime 在 60min 内 + PID 还活着 → 占用
    - 文件存在但 PID 死了 OR mtime 超过 60min → 视为僵尸，重新认领

    并发正确性（audit algo C3 修复）：
    原版 path.write_text(PID) 后 read-back-verify 的 race window 让两个进程
    都可能读到自己的 PID（如果 OS 调度让它们交替执行）。现在外加一道 fcntl
    flock 跨进程互斥：写 PID 的整个段被串行化，物理上不会撞车。
    """
    path = _lock_path(memory_root)
    path.parent.mkdir(parents=True, exist_ok=True)

    flock_path = path.parent / LOCK_FCNTL_FILE
    flock_fd = None
    try:
        flock_fd = os.open(str(flock_path), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            # 非阻塞 LOCK_EX：拿不到立刻失败，让 caller 知道"另一进程正在认领"
            fcntl.flock(flock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                print("[autoDream] 另一进程正在认领锁，跳过")
                return None
            raise

        # 进入临界区：检查现有锁状态
        mtime_ms: Optional[float] = None
        holder_pid: Optional[int] = None
        if path.exists():
            try:
                mtime_ms = path.stat().st_mtime * 1000
                holder_pid = int(path.read_text().strip())
            except (ValueError, OSError):
                mtime_ms = None
                holder_pid = None

        now_ms = time.time() * 1000
        if mtime_ms is not None and now_ms - mtime_ms < HOLDER_STALE_MS:
            if holder_pid and holder_pid != os.getpid() and _is_process_running(holder_pid):
                print(f"[autoDream] 锁被活 PID {holder_pid} 持有，"
                      f"距上次刷新 {(now_ms - mtime_ms) / 1000:.0f}s，跳过")
                return None

        # 写入自己的 PID + 更新 mtime（仍在 fcntl 互斥下）
        path.write_text(str(os.getpid()))
        return mtime_ms or 0.0
    finally:
        # 写完即释放 fcntl mutex；PID file 本身仍在，作为长生命周期的"持有者标识"
        if flock_fd is not None:
            try:
                fcntl.flock(flock_fd, fcntl.LOCK_UN)
                os.close(flock_fd)
            except OSError:
                pass


def rollback_consolidation_lock(memory_root: Path, prior_mtime: float) -> None:
    """失败回滚：清掉 PID 字段 + 把 mtime 倒回去"""
    path = _lock_path(memory_root)
    try:
        if prior_mtime == 0:
            path.unlink(missing_ok=True)
            return
        path.write_text("")
        secs = prior_mtime / 1000
        os.utime(path, (secs, secs))
    except OSError as e:
        print(f"[autoDream] rollback 失败: {e}")


def record_manual_consolidation(memory_root: Path) -> None:
    """手动 /dream 触发时打个时间戳（best-effort，不抢锁）"""
    path = _lock_path(memory_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(str(os.getpid()))
    except OSError as e:
        print(f"[autoDream] manual stamp 失败: {e}")


__all__ = [
    "read_last_consolidated_at",
    "try_acquire_consolidation_lock",
    "rollback_consolidation_lock",
    "record_manual_consolidation",
    "HOLDER_STALE_MS",
]
