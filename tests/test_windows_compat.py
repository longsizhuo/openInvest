"""Windows 原生兼容：oslock 垫片（POSIX 上就是 fcntl 原物）、pid_alive 不杀进程、文本写钉死 LF。"""
import os
import subprocess
import sys

from openinvest.utils import oslock


def test_posix_is_fcntl_passthrough():
    if os.name == "nt":
        return
    import fcntl
    assert oslock.flock is fcntl.flock and oslock.LOCK_EX == fcntl.LOCK_EX
    assert oslock.CAN_UNLINK_OPEN


def test_nonblocking_lock_conflicts_across_handles(tmp_path):
    p = tmp_path / "x.lock"
    p.touch()
    with open(p, "a") as a, open(p, "a") as b:
        oslock.flock(a.fileno(), oslock.LOCK_EX)
        try:
            oslock.flock(b.fileno(), oslock.LOCK_EX | oslock.LOCK_NB)
            raise AssertionError("second handle must not get the lock")
        except BlockingIOError:
            pass
        oslock.flock(a.fileno(), oslock.LOCK_UN)
        oslock.flock(b.fileno(), oslock.LOCK_EX | oslock.LOCK_NB)


def test_pid_alive_does_not_kill():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert oslock.pid_alive(child.pid)
        assert child.poll() is None  # 探活没把它杀掉（Windows 上 os.kill(pid, 0) 会）
    finally:
        child.kill()
        child.wait()
    assert not oslock.pid_alive(child.pid) or os.name == "nt"  # Windows PID 句柄回收时机不定，只断言 POSIX
    assert not oslock.pid_alive(0)


def test_src_text_writes_pin_newline():
    """src/ 里所有文本写（open w/a/+、Path.write_text、Path.open）必须显式 newline=：
    Windows 文本模式默认把 \\n 写成 \\r\\n，产物就和 Linux 不一致（jsonl / md / svg）。"""
    import ast
    from pathlib import Path

    bad = []
    for f in sorted((Path(__file__).resolve().parents[1] / "src").rglob("*.py")):
        for c in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if not isinstance(c, ast.Call):
                continue
            fn = c.func
            name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
            if name not in ("open", "write_text"):
                continue
            if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) and fn.value.id in (
                    "os", "gzip", "io", "tarfile", "zipfile", "webbrowser"):
                continue
            kw = {k.arg: k.value for k in c.keywords}
            if name == "write_text":
                mode = "w"
            else:
                i = 0 if isinstance(fn, ast.Attribute) else 1  # Path.open(mode) vs open(path, mode)
                pos = c.args[i] if len(c.args) > i else None
                m = kw.get("mode", pos)
                mode = "r" if m is None else (m.value if isinstance(m, ast.Constant) else "?")
            if "b" not in mode and any(x in mode for x in "wax+?") and "newline" not in kw:
                bad.append(f"{f.name}:{c.lineno}")
    assert bad == [], f"文本写缺 newline=\"\\n\"：{bad}"
