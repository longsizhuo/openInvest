"""跨平台的进程间文件锁与进程探活。

POSIX 上 `flock` / `LOCK_*` 就是 `fcntl` 原物（行为零变化）；Windows 没有 fcntl，
用 `msvcrt.locking` 锁文件末尾外 `_WIN_LOCK_OFFSET` 处的 1 字节模拟独占 flock（只用到 LOCK_EX / LOCK_NB / LOCK_UN 三种）。

Windows 两点差异，调用方要知道：
- 打开中的文件不能 unlink（PermissionError）——`CAN_UNLINK_OPEN` 为 False 时别在持锁时删 sidecar。
- `os.kill(pid, 0)` 在 Windows 上会 TerminateProcess 杀掉对方，探活必须走 `pid_alive`。
- 目标文件正被别的句柄打开时 `os.replace` 会 PermissionError（Python 的 open 不带 FILE_SHARE_DELETE）——原子替换走 `replace`。
"""
from __future__ import annotations

import errno
import os
import time

CAN_UNLINK_OPEN = os.name != "nt"

try:
    from fcntl import LOCK_EX, LOCK_SH, LOCK_NB, LOCK_UN, flock  # noqa: F401
except ImportError:  # Windows
    import msvcrt

    LOCK_SH, LOCK_EX, LOCK_NB, LOCK_UN = 1, 2, 4, 8
    # Windows 字节锁是强制锁：锁第 0 字节会让别的句柄读数据文件报 EACCES（flock 是建议锁，不挡读）。
    # 锁远在 EOF 之外的一个字节 → 只在加锁者之间互斥，不影响任何读写。
    _WIN_LOCK_OFFSET = 1 << 40

    # Shared probes conservatively use the same exclusive byte lock on Windows.
    def flock(fd, op: int) -> None:  # type: ignore[misc]
        """fd 可以是 int 或带 fileno() 的文件对象；读写位置调用前后不变。"""
        fd = fd if isinstance(fd, int) else fd.fileno()
        pos = os.lseek(fd, 0, os.SEEK_CUR)
        os.lseek(fd, _WIN_LOCK_OFFSET, os.SEEK_SET)
        try:
            if op & LOCK_UN:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                return
            while True:
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    return
                except OSError as e:
                    if e.errno not in (errno.EACCES, errno.EDEADLK):
                        raise  # 坏 fd / 参数错不是"锁被占"，别静默轮询到天荒地老
                    if op & LOCK_NB:
                        raise BlockingIOError(errno.EAGAIN, "lock held by another process") from None
                    time.sleep(0.05)  # ponytail: 轮询等锁；LK_LOCK 只重试 10 秒就抛，不等价于阻塞 flock
        finally:
            os.lseek(fd, pos, os.SEEK_SET)


def pid_alive(pid: int) -> bool:
    """PID 是否存活。POSIX：kill 0，EPERM（无权发信号）也算活着。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_ulong)
        k32.WaitForSingleObject.restype = ctypes.c_ulong
        k32.CloseHandle.argtypes = (ctypes.c_void_p,)
        h = k32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not h:
            return ctypes.get_last_error() == 5  # ACCESS_DENIED = 存在但无权查询
        try:
            # 进程句柄在退出时变 signaled；不用 GetExitCodeProcess==259，免得以 259 退出的进程被当活着
            return k32.WaitForSingleObject(h, 0) == 0x102  # WAIT_TIMEOUT
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except OSError as e:
        return e.errno == errno.EPERM
    return True


def replace(src, dst) -> None:
    """os.replace；Windows 上目标被读者短暂打开时重试最多 ~2 秒（POSIX 一次到位）。"""
    for _ in range(40 if os.name == "nt" else 0):
        try:
            return os.replace(src, dst)
        except PermissionError:
            time.sleep(0.05)
    os.replace(src, dst)
