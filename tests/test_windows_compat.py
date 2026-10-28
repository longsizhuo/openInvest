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


def test_replace_waits_out_a_reader_holding_target(tmp_path):
    """Windows：目标被读者打开时 os.replace 抛 PermissionError；oslock.replace 等读者关掉再替换。"""
    import threading

    dst, src = tmp_path / "a.json", tmp_path / "a.json.tmp"
    dst.write_text("old", newline="\n")
    src.write_text("new", newline="\n")
    reader = open(dst)
    threading.Timer(0.3, reader.close).start()
    oslock.replace(src, dst)
    reader.close()
    assert dst.read_text() == "new" and not src.exists()


# 函数名 → (位置参数里 mode 的下标, 默认 mode)
_MODE_ARG = {"open": (1, "r"), "fdopen": (1, "r"), "NamedTemporaryFile": (0, "w+b"), "TemporaryFile": (0, "w+b")}


def _unpinned_text_writes(source: str):
    """源码里没钉 newline= 的文本写调用的行号。"""
    import ast

    out = []
    for c in ast.walk(ast.parse(source)):
        if not isinstance(c, ast.Call):
            continue
        fn = c.func
        name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
        owner = fn.value.id if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) else None
        if name not in _MODE_ARG and name != "write_text":
            continue
        if owner in ("tarfile", "zipfile", "webbrowser") or (owner == "os" and name == "open"):
            continue  # 不是文本文件 IO（os.open 收 flags 不收 mode）
        kw = {k.arg: k.value for k in c.keywords}
        if name == "write_text":
            mode = "w"
        else:
            i, default = _MODE_ARG[name]
            if name == "open" and isinstance(fn, ast.Attribute) and owner not in ("io", "gzip"):
                i = 0  # Path.open(mode)
            pos = c.args[i] if len(c.args) > i else None
            m = kw.get("mode", pos)
            mode = default if m is None else (m.value if isinstance(m, ast.Constant) else "?")
        if "b" not in mode and any(x in mode for x in "wax+?") and "newline" not in kw:
            out.append(c.lineno)
    return out


def test_src_text_writes_pin_newline():
    """src/ 里所有文本写（open / io.open / gzip.open / os.fdopen / Path.open 的 w/a/+、Path.write_text、
    NamedTemporaryFile/TemporaryFile 的文本 mode）必须显式 newline=：
    Windows 文本模式默认把 \n 写成 \r\n，产物就和 Linux 不一致（jsonl / md / svg）。"""
    from pathlib import Path

    bad = [f"{f.name}:{ln}" for f in sorted((Path(__file__).resolve().parents[1] / "src").rglob("*.py"))
           for ln in _unpinned_text_writes(f.read_text(encoding="utf-8"))]
    assert bad == [], f"文本写缺 newline=\"\\n\"：{bad}"


def test_newline_guard_catches_sneaky_forms():
    caught = """
open(p, "w")
io.open(p, "a")
gzip.open(p, "wt")
os.fdopen(fd, "w")
tempfile.NamedTemporaryFile(mode="w")
tempfile.TemporaryFile("w+")
p.open("a")
p.write_text(s)
"""
    ok = """
open(p)
open(p, "wb")
gzip.open(p, "wb")
gzip.open(p)
os.open(p, os.O_RDWR)
os.fdopen(fd, "rb")
tempfile.NamedTemporaryFile()
p.open()
open(p, "w", newline="\\n")
zipfile.open(p, "w")
"""
    assert _unpinned_text_writes(caught) == list(range(2, 10))
    assert _unpinned_text_writes(ok) == []


def test_json_output_on_legacy_windows_codepage(monkeypatch):
    import io
    from types import SimpleNamespace
    from openinvest.skill_cmds import _helpers

    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding="cp1252")
    monkeypatch.setattr(_helpers, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(_helpers.sys, "__stdout__", stream)
    _helpers._print_json({"error": "六位代码"})
    assert buffer.getvalue().decode("utf-8").endswith("\n")
    assert b"\r\n" not in buffer.getvalue()
    assert "六位代码" in buffer.getvalue().decode("utf-8")
    stream.detach()
