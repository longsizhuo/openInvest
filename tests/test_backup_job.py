"""jobs/backup.py + db/wal.py 回归测试（评审路线 Sprint 2 D2）。

守的行为：
- 调度器长驻持有 trades.db 连接（WAL 非空）时备份，解压后账本一行不少
  （对照：直接拷主库文件会丢——证明测试真的卡住了 checkpoint）
- 轮转：只留最近 14 份 auto_*，手动备份（openinvest-backup-* / manual_*）不动
- memory/ 打包排除 *.lock、*.bak.*、原子写残留 *.tmp.*；zip 内路径相对数据根（兼容 run.sh restore）
- 失败要抛：checkpoint 报错照样写出 zip 再抛（watchdog 告警）；无物可备 → skipped 不产空 zip
- backup.yml 被 runner 发现、entry 可解析
全部 hermetic：tmp 数据根，不联网不外发。
"""
from __future__ import annotations

import functools
import shutil
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from openinvest.db import wal
from openinvest.db.insights_db import InsightsDB
from openinvest.db.trades_db import TradesDB
from openinvest.jobs import backup
from openinvest.scheduler import runner

T0 = datetime(2026, 10, 18, 19, 30, tzinfo=timezone.utc)


def _seed_ledger(root: Path, n: int = 3) -> TradesDB:
    """造 trades.db 并写 n 笔，不关连接——模拟调度器长驻连接（WAL 里有未 checkpoint 的提交）。"""
    db = TradesDB(str(root / "db" / "trades.db"))
    for i in range(n):
        db.record_trade(symbol="GC=F", direction="buy", units=1 + i, price=100.0 + i)
    return db


def _rows(db_file: Path) -> int:
    """trades 行数；表都不存在（建表也还在 WAL 里）算 0。"""
    conn = sqlite3.connect(str(db_file))
    try:
        return conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


def test_trades_in_wal_survive_backup(tmp_path):
    root = tmp_path / "home"
    db = _seed_ledger(root)
    wal = root / "db" / "trades.db-wal"
    assert wal.stat().st_size > 0  # 前提：成交还躺在 WAL 里

    # 对照：只拷主库文件（旧备份的行为）看不到这 3 笔
    naive = tmp_path / "naive.db"
    shutil.copy(root / "db" / "trades.db", naive)
    assert _rows(naive) == 0

    res = backup.run(root=root, now=T0)

    assert res["status"] == "ok"
    assert res["path"].endswith("auto_20261018_1930.zip")
    assert res["size_bytes"] > 0 and res["files"] >= 1
    assert res["checkpoint"]["db/trades.db"]["busy"] == 0
    with zipfile.ZipFile(res["path"]) as zf:
        zf.extract("db/trades.db", tmp_path / "restored")
        assert "db/trades.db-wal" not in zf.namelist()  # TRUNCATE 之后 WAL 为空，不进包
    assert _rows(tmp_path / "restored" / "db" / "trades.db") == 3
    db.conn.close()


def test_busy_checkpoint_ships_wal(tmp_path, monkeypatch):
    """读事务挡住 checkpoint（busy）→ 残留 -wal 随包带上，仍可恢复出全部成交。"""
    monkeypatch.setattr(backup, "checkpoint_file", functools.partial(wal.checkpoint_file, timeout=0.1))
    root = tmp_path / "home"
    db = _seed_ledger(root)
    reader = sqlite3.connect(str(root / "db" / "trades.db"))
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM trades").fetchone()  # 持有读快照
    db.record_trade(symbol="GC=F", direction="buy", units=9, price=1.0)  # 快照之后再写一笔

    res = backup.run(root=root, now=T0)  # checkpoint 被读事务挡住（timeout 压到 0.1s 免得测试等 10s）

    assert res["checkpoint"]["db/trades.db"]["busy"] == 1
    with zipfile.ZipFile(res["path"]) as zf:
        assert "db/trades.db-wal" in zf.namelist()
        zf.extractall(tmp_path / "restored")
    # 恢复 = 解压回去后正常打开（SQLite 自动从 -wal 恢复），4 笔都在
    assert _rows(tmp_path / "restored" / "db" / "trades.db") == 4
    reader.close()
    db.conn.close()


def test_retention_keeps_latest_14_auto_only(tmp_path):
    root = tmp_path / "home"
    (root / "memory").mkdir(parents=True)
    (root / "memory" / "portfolio.md").write_text("x")
    out = root / ".backups"
    out.mkdir()
    for d in range(1, 21):  # 20 份旧 auto_（2026-09-01..20）
        (out / f"auto_202609{d:02d}_1930.zip").write_bytes(b"old")
    (out / "manual_20260101_0000.zip").write_bytes(b"keep")
    (out / "openinvest-backup-20260708-162054-UTC.zip").write_bytes(b"keep")

    res = backup.run(root=root, now=T0)

    autos = sorted(p.name for p in out.glob("auto_*.zip"))
    assert len(autos) == 14
    assert autos[-1] == "auto_20261018_1930.zip"  # 新的在
    assert autos[0] == "auto_20260908_1930.zip"   # 保留 13 份旧 + 1 份新
    assert len(res["pruned"]) == 7
    assert (out / "manual_20260101_0000.zip").exists()
    assert (out / "openinvest-backup-20260708-162054-UTC.zip").exists()


def test_excludes_lock_bak_tmp_and_keeps_hidden_dirs(tmp_path):
    root = tmp_path / "home"
    mem = root / "memory"
    (mem / ".committee" / "2026-10-17").mkdir(parents=True)
    (mem / ".state").mkdir()
    for rel in ("portfolio.md", ".committee/2026-10-17/GC=F.md", ".state/pnl_history.jsonl",
                "portfolio.md.lock", "portfolio.md.bak.20260519-050555", "user.md.tmp.4242"):
        (mem / rel).write_text("x")
    (root / "db").mkdir()
    (root / "db" / "market_data.db").write_bytes(b"cache")  # 可重建，不进自动备份
    InsightsDB(str(root / "db" / "insights.db")).close()

    res = backup.run(root=root, now=T0)

    with zipfile.ZipFile(res["path"]) as zf:
        names = set(zf.namelist())
    assert names == {
        "memory/portfolio.md",
        "memory/.committee/2026-10-17/GC=F.md",
        "memory/.state/pnl_history.jsonl",
        "db/insights.db",
    }
    assert res["files"] == 4


def test_checkpoint_error_still_writes_zip_then_raises(tmp_path):
    root = tmp_path / "home"
    (root / "db").mkdir(parents=True)
    (root / "db" / "trades.db").write_bytes(b"this is not a sqlite file" * 50)
    (root / "memory").mkdir()
    (root / "memory" / "portfolio.md").write_text("x")

    with pytest.raises(RuntimeError, match="db/trades.db"):
        backup.run(root=root, now=T0)

    assert (root / ".backups" / "auto_20261018_1930.zip").exists()  # 备份照写，不因一个库坏了全丢
    assert not list((root / ".backups").glob("*.tmp"))


def test_nothing_to_back_up_is_skipped(tmp_path):
    res = backup.run(root=tmp_path / "empty", now=T0)
    assert res["status"] == "skipped"
    assert not (tmp_path / "empty" / ".backups").exists()


def test_db_classes_expose_checkpoint(tmp_path):
    t = TradesDB(str(tmp_path / "trades.db"))
    i = InsightsDB(str(tmp_path / "insights.db"))
    t.record_trade(symbol="AAPL", direction="buy", units=1, price=1.0)
    i.upsert(slug="s", asset="AAPL", title="t", body="b")
    for db, wal in ((t, tmp_path / "trades.db-wal"), (i, tmp_path / "insights.db-wal")):
        assert wal.stat().st_size > 0
        assert db.checkpoint()["busy"] == 0
        assert wal.stat().st_size == 0
        db.conn.close()


def test_backup_job_registered_with_runner():
    cfg = next(c for c in runner._load_job_configs() if c["name"] == "backup")
    assert cfg["enabled"] is True
    assert cfg["schedule"] == "30 3 * * *" and cfg["timezone"] == "Asia/Shanghai"
    assert runner._resolve_entry(cfg["entry"]) is backup.run
