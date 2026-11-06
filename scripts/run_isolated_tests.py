"""Linux test runner: isolated filesystem, credentials and network, including loopback.

Usage: python3 scripts/run_isolated_tests.py --python .venv/bin/python --self-test
       python3 scripts/run_isolated_tests.py --python .venv/bin/python -- tests/ -q
Dependencies must already be installed; this runner never downloads them.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not shutil.which("bwrap"):
        parser.error("bubblewrap is required; refusing an unisolated fallback")
    uv = shutil.which("uv")
    if not uv:
        parser.error("uv is required for the backup/restore subprocess regression tests")
    awk = shutil.which("awk")
    if not awk:
        parser.error("awk is required for the backup listing regression tests")
    python = args.python.absolute()
    packages = list(python.parent.parent.glob("lib/python*/site-packages"))
    if len(packages) != 1:
        parser.error("--python must point to an existing project virtual environment")
    runtime = python.resolve().parent.parent
    repo = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory(prefix="openinvest-isolated-") as directory:
        tmp = Path(directory)
        source = tmp / "source"
        source.mkdir()
        # Mount a source-only copy: a read-only checkout would still expose its
        # .env, ledger and historical evaluation data to tests.
        names = subprocess.check_output(
            ["git", "-C", str(repo), "ls-files", "--cached", "--others", "--exclude-standard", "-z"]
        ).decode().split("\0")
        private = {"memory", "db", "logs", ".git", ".venv", ".claude", ".hermes", "integrity", ".backups"}
        for name in names:
            if not name:
                continue
            rel = Path(name)
            if rel.parts[0] in private or any(part.startswith(".env") for part in rel.parts):
                continue
            original = repo / rel
            if not original.exists():
                continue  # locally deleted tracked file
            if original.is_symlink():
                # Relative links within the staged code stay valid; links into
                # the host home cannot resolve in the isolated filesystem.
                target = source / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(os.readlink(original))
            elif original.is_file():
                target = source / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(original, target)
        scratch = tmp / "scratch"
        outside = tmp / "outside"
        scratch.mkdir()
        outside.mkdir()
        (outside / "canary").write_text("original")
        etc = tmp / "etc"
        etc.mkdir()
        (etc / "hosts").write_text("127.0.0.1 localhost\n::1 localhost\n")
        (etc / "nsswitch.conf").write_text("hosts: files\n")
        (etc / "timezone").write_text("Etc/UTC\n")
        # A writable, disposable venv for uv --no-sync subprocesses, reusing the
        # read-only dependency mount. No production venv or cache is writable.
        venv = scratch / "venv"
        (venv / "bin").mkdir(parents=True)
        (venv / "pyvenv.cfg").write_text("home = /runtime/bin\ninclude-system-site-packages = false\n")
        for alias in {"python", "python3", python.resolve().name}:
            (venv / "bin" / alias).symlink_to(f"/runtime/bin/{python.resolve().name}")
        libs = venv / "lib" / packages[0].parent.name
        libs.mkdir(parents=True)
        (libs / "site-packages").symlink_to("/deps")
        cmd = ["bwrap", "--unshare-all", "--new-session", "--die-with-parent",
               "--cap-drop", "ALL", "--clearenv"]
        for path in ("/usr", "/lib", "/lib64"):
            if Path(path).exists():
                cmd.extend(["--ro-bind", path, path])
        cmd.extend(["--symlink", "usr/bin", "/bin", "--dev", "/dev", "--proc", "/proc",
                    "--tmpfs", "/tmp", "--ro-bind", str(runtime), "/runtime",
                    "--ro-bind", str(packages[0]), "/deps", "--ro-bind", str(source), "/work",
                    "--ro-bind", str(etc), "/etc", "--dir", "/tools",
                    "--ro-bind", str(Path(uv).resolve()), "/tools/uv", "--symlink", "uv", "/tools/uvx",
                    "--ro-bind", str(Path(awk).resolve()), "/tools/awk",
                    "--ro-bind", str(outside), "/outside", "--bind", str(scratch), "/scratch"])
        env = {"HOME": "/scratch", "PATH": "/tools:/runtime/bin:/usr/bin:/bin",
               "PYTHONPATH": "/work/src:/work:/deps", "PYTHONDONTWRITEBYTECODE": "1",
               "INVEST_HOME": "/scratch/invest", "INVEST_TEST_ISOLATED": "1",
               "PYTHON_DOTENV_DISABLED": "1", "LC_ALL": "C.UTF-8",
               "UV_OFFLINE": "1", "UV_PYTHON_DOWNLOADS": "never", "UV_CACHE_DIR": "/scratch/uv-cache",
               "UV_PROJECT_ENVIRONMENT": "/scratch/venv", "UV_PYTHON": "/scratch/venv/bin/python"}
        for key, value in env.items():
            cmd.extend(["--setenv", key, value])
        cmd.extend(["--chdir", "/work", f"/runtime/bin/{python.resolve().name}"])
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen()
            if args.self_test:
                probe = '''
import os, pathlib, socket, subprocess, sys
def denied(fn):
    try: fn()
    except OSError: return
    raise AssertionError("out-of-scope write succeeded")
denied(lambda: pathlib.Path('/outside/canary').write_text('changed'))
def temporary_write_delete():
    p=pathlib.Path('/outside/temporary'); p.write_text('x'); p.unlink()
denied(temporary_write_delete)
pathlib.Path('/scratch/link').symlink_to('/outside/canary')
denied(lambda: pathlib.Path('/scratch/link').write_text('changed'))
r=subprocess.run([sys.executable,'-c',"open('/outside/canary','w').write('changed')"],capture_output=True)
assert r.returncode != 0
assert not pathlib.Path('/home/ubuntu').exists()
assert not pathlib.Path('/work/.env').exists()
assert not pathlib.Path('/work/memory').exists()
assert not pathlib.Path('/work/db').exists()
assert not pathlib.Path('/work/integrity').exists()
assert not pathlib.Path('/work/.backups').exists()
assert not any('KEY' in k or 'TOKEN' in k or 'PASSWORD' in k for k in os.environ)
with socket.socket() as s:
    s.settimeout(1)
    assert s.connect_ex(('127.0.0.1', PORT)) != 0
with socket.socket() as s:
    s.settimeout(1)
    assert s.connect_ex(('192.0.2.1',443)) != 0
print('PASS: direct, temporary, symlink and subprocess writes denied; host loopback and external network unreachable; production home and credentials absent')
'''.replace("PORT", str(server.getsockname()[1]))
                cmd.extend(["-c", probe])
            else:
                tests = args.args[1:] if args.args[:1] == ["--"] else args.args
                cmd.extend(["-m", "pytest", "-o", "cache_dir=/scratch/pytest-cache", *tests])
            result = subprocess.run(cmd, close_fds=True)
        assert (outside / "canary").read_text() == "original"
        return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
