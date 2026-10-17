"""plugin/skills/invest-backup/scripts/run.sh 端到端（subprocess，真跑 bash + zip + uv）。

守的行为（此前只有手工验证）：
- 调度器式长驻连接持有 trades.db（成交还在 WAL 里）时 `run.sh backup`：checkpoint 真的被调用，
  解包后 3 笔成交一笔不少（旧 run.sh 排除 *.db-wal，恢复出的库连表都没有）
- `run.sh list` 同时列手动 openinvest-backup-* 与后端自动 auto_*.zip
- 旧后端（没有 openinvest.jobs.backup，例如 uvx 钉了旧 SPEC）降级：checkpoint 跳过只打 warning，
  备份照做，*.db-wal 随包带上，解包后成交仍在

旧后端用 sitecustomize 垫片模拟（sys.modules[...] = None 让 import 失败）。临时 INVEST_HOME，
不联网不外发。缺 bash / zip / uv 时跳过。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import zipfile
from pathlib import Path

import pytest

from openinvest.db.trades_db import TradesDB

REPO = Path(__file__).resolve().parent.parent
RUN_SH = REPO / "plugin" / "skills" / "invest-backup" / "scripts" / "run.sh"

pytestmark = pytest.mark.skipif(
    not all(shutil.which(t) for t in ("bash", "zip", "uv")), reason="需要 bash / zip / uv"
)


def _run(home: Path, *args: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    # conftest 会清掉本进程所有 INVEST_*；子进程的数据根显式指定
    env = {**os.environ, "INVEST_HOME": str(home), **(env_extra or {})}
    return subprocess.run(["bash", str(RUN_SH), *args], env=env, cwd=REPO,
                          capture_output=True, text=True, timeout=180)


def _rows(db_file: Path) -> int:
    conn = sqlite3.connect(str(db_file))
    try:
        return conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    finally:
        conn.close()


@pytest.fixture
def live_home(tmp_path):
    """数据根里有 portfolio.md 与一个仍被持有的 trades.db 连接（3 笔成交在 WAL 里）。"""
    home = tmp_path / "home"
    (home / "memory").mkdir(parents=True)
    (home / "memory" / "portfolio.md").write_text("# portfolio\n")
    db = TradesDB(str(home / "db" / "trades.db"))
    for i in range(3):
        db.record_trade(symbol="GC=F", direction="buy", units=1 + i, price=1.0)
    assert (home / "db" / "trades.db-wal").stat().st_size > 0  # 前提：成交只在 WAL 里
    yield home
    db.conn.close()


def _backup_zip(res: subprocess.CompletedProcess) -> Path:
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout.strip().splitlines()[-1])
    assert out["status"] == "ok"
    return Path(out["path"])


def test_backup_checkpoints_and_keeps_live_wal_trades(live_home, tmp_path):
    res = _run(live_home, "backup")

    zp = _backup_zip(res)
    assert '"db/trades.db"' in res.stderr and '"busy": 0' in res.stderr  # checkpoint 真被调用（输出走 stderr）
    with zipfile.ZipFile(zp) as zf:
        names = zf.namelist()
        zf.extractall(tmp_path / "restored")
    assert not any(n.endswith("-shm") for n in names)  # -shm 仍不进包
    assert _rows(tmp_path / "restored" / "db" / "trades.db") == 3


def test_list_shows_manual_and_auto_backups(live_home):
    backups = live_home / ".backups"
    backups.mkdir()
    (backups / "auto_20261018_1930.zip").write_bytes(b"x")
    manual = _backup_zip(_run(live_home, "backup"))

    res = _run(live_home, "list")

    assert res.returncode == 0, res.stderr
    assert "auto_20261018_1930.zip" in res.stdout and manual.name in res.stdout


def test_old_backend_without_checkpoint_function_still_keeps_wal(live_home, tmp_path):
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "sitecustomize.py").write_text(
        "import sys\nsys.modules['openinvest.jobs.backup'] = None  # 模拟旧后端：没有这个模块\n")
    pypath = os.pathsep.join(p for p in (str(shim), os.environ.get("PYTHONPATH", "")) if p)

    res = _run(live_home, "backup", env_extra={"PYTHONPATH": pypath})

    zp = _backup_zip(res)  # 降级不拦备份
    assert "warning: checkpoint" in res.stderr
    with zipfile.ZipFile(zp) as zf:
        assert zf.getinfo("db/trades.db-wal").file_size > 0  # 没 checkpoint → WAL 必须随包
        zf.extractall(tmp_path / "restored")
    assert _rows(tmp_path / "restored" / "db" / "trades.db") == 3  # 主库 + WAL 一起恢复
