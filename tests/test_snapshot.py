"""tests/test_snapshot.py — snapshot → restore 往返完整性

finding #2：scripts/snapshot.py 搬的是"真金白银和九个月交易史"，但此前只有
smoke import（ci.yml `import scripts.snapshot`），没有任何往返回归网。这里补上：

- round-trip：snapshot → restore 到新 target，tier1 文本文件字节一致
- trades.db（WAL）online-backup 后数据可读、行内容一致
- manifest 字段齐全（schema_version / tier1_files+sha256 / tier1_sqlite+sha256 / env_keys_needed）
- 损坏拷贝 → restore 报 ok_with_warnings（sha256 不符），不静默放过
- --force 守卫：target 已有账本且无 --force → 拒绝（防覆盖真账本）
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.snapshot as snap
from db.trades_db import TradesDB


# ============ 测试辅助 ============

def _seed_source(mem: Path, db: Path) -> None:
    """造一份假 hub 权威状态：tier1 文件 + .state 目录 + trades.db(WAL) + tier2 目录。"""
    mem.mkdir(parents=True, exist_ok=True)
    (mem / "portfolio.md").write_text("# portfolio\ncash:\n  AUD: 5000\n", encoding="utf-8")
    (mem / "user.md").write_text("# user\nrisk: Balanced\n", encoding="utf-8")
    (mem / "strategy.md").write_text("# strategy\n", encoding="utf-8")
    (mem / "MEMORY.md").write_text("# memory index\n", encoding="utf-8")
    # .state（幂等去重键也住这儿——paydays_applied / processed_emails）
    state = mem / ".state"
    state.mkdir()
    (state / "paydays_applied.json").write_text('["2026-06"]', encoding="utf-8")
    # tier2 历史目录
    comm = mem / ".committee"
    comm.mkdir()
    (comm / "NDQ.AX.json").write_text('{"verdict": "BUY"}', encoding="utf-8")
    # trades.db（WAL）—— 写一笔，模拟九个月交易史里的一条
    db.mkdir(parents=True, exist_ok=True)
    tdb = TradesDB(db_path=str(db / "trades.db"))
    tdb.record_trade(symbol="NDQ.AX", direction="BUY", units=10.0, price=130.0,
                     cost_currency="AUD")


@pytest.fixture
def hub(tmp_path, monkeypatch):
    """把 snapshot 的 MEMORY_ROOT / DB_ROOT 指到临时 hub，清掉远端客户端守卫。"""
    mem = tmp_path / "src" / "memory"
    db = tmp_path / "src" / "db"
    _seed_source(mem, db)
    monkeypatch.setattr(snap, "MEMORY_ROOT", mem)
    monkeypatch.setattr(snap, "DB_ROOT", db)
    monkeypatch.delenv("INVEST_API_BASE", raising=False)
    return mem, db


def _do_snapshot(out: Path) -> None:
    snap.cmd_snapshot(argparse.Namespace(out=str(out)))


def _do_restore(in_file: Path, target: Path, force: bool = False) -> None:
    snap.cmd_restore(
        argparse.Namespace(in_file=str(in_file), target=str(target), force=force)
    )


def _read_manifest(tar_path: Path) -> dict:
    with tarfile.open(tar_path, "r:gz") as t:
        name = next(n for n in t.getnames() if n.endswith("manifest.json"))
        return json.loads(t.extractfile(name).read())


def _corrupt_member(tar_path: Path, rel: str, new_bytes: bytes) -> None:
    """解包 → 篡改 tar 里某个文件（manifest 的 sha 仍是原值）→ 重新打包，模拟传输损坏。"""
    with tempfile.TemporaryDirectory() as td:
        ex = Path(td) / "ex"
        ex.mkdir()
        with tarfile.open(tar_path, "r:gz") as t:
            t.extractall(ex, filter="data")
        (ex / rel).write_bytes(new_bytes)
        tar_path.unlink()
        with tarfile.open(tar_path, "w:gz") as t:
            t.add(ex, arcname=".")


# ============ 测试 ============

class TestSnapshotRoundTrip:
    def test_round_trip_tier1_bytes_and_trades(self, hub, tmp_path, capsys):
        mem, _ = hub
        out = tmp_path / "snap.tar.gz"
        _do_snapshot(out)
        snap_out = json.loads(capsys.readouterr().out)
        assert snap_out["status"] == "ok"
        assert out.is_file()

        target = tmp_path / "target"
        _do_restore(out, target)
        restore_out = json.loads(capsys.readouterr().out)
        assert restore_out["status"] == "ok"  # 无 sha 告警
        assert not restore_out["warnings"]

        # tier1 文本文件字节一致
        for name in ("portfolio.md", "user.md", "strategy.md", "MEMORY.md"):
            assert (target / "memory" / name).read_bytes() == (mem / name).read_bytes()
        # .state 子目录还原
        assert (target / "memory" / ".state" / "paydays_applied.json").read_text(
            encoding="utf-8"
        ) == '["2026-06"]'
        # tier2 历史目录还原
        assert (target / "memory" / ".committee" / "NDQ.AX.json").is_file()
        # trades.db：online-backup 后字节不必相同，但数据要在且一致
        rows = TradesDB(db_path=str(target / "db" / "trades.db")).list_trades(limit=10)
        assert len(rows) == 1
        assert rows[0]["symbol"] == "NDQ.AX"
        assert rows[0]["units"] == pytest.approx(10.0)
        assert rows[0]["cost_currency"] == "AUD"

    def test_manifest_fields_complete(self, hub, tmp_path, capsys):
        out = tmp_path / "snap.tar.gz"
        _do_snapshot(out)
        capsys.readouterr()

        m = _read_manifest(out)
        assert m["schema_version"] == snap.SCHEMA_VERSION
        # tier1 文件带 sha256
        assert "portfolio.md" in m["tier1_files"]
        assert "sha256" in m["tier1_files"]["portfolio.md"]
        # tier1 sqlite 带 sha256
        assert "trades.db" in m["tier1_sqlite"]
        assert "sha256" in m["tier1_sqlite"]["trades.db"]
        # tier2 目录记数
        assert ".committee" in m["tier2_dirs"]
        # env keys 列表（名字不含值）
        assert isinstance(m["env_keys_needed"], list)

    def test_corrupted_copy_reports_warning(self, hub, tmp_path, capsys):
        out = tmp_path / "snap.tar.gz"
        _do_snapshot(out)
        capsys.readouterr()  # 丢掉 snapshot 输出

        # 篡改 tar 里的 portfolio.md（manifest sha 仍是原值）→ restore 应当告警
        _corrupt_member(out, "memory/portfolio.md", b"# tampered in transit\n")

        target = tmp_path / "target"
        _do_restore(out, target)
        restore_out = json.loads(capsys.readouterr().out)
        assert restore_out["status"] == "ok_with_warnings"
        assert any("portfolio.md" in w for w in restore_out["warnings"])

    def test_force_guard_refuses_existing_ledger(self, hub, tmp_path, capsys):
        out = tmp_path / "snap.tar.gz"
        _do_snapshot(out)
        capsys.readouterr()

        target = tmp_path / "target"
        # 目标已有真账本——无 --force 必须拒绝覆盖
        (target / "memory").mkdir(parents=True)
        (target / "memory" / "portfolio.md").write_text("# existing real ledger\n",
                                                         encoding="utf-8")
        with pytest.raises(SystemExit):
            _do_restore(out, target, force=False)
        capsys.readouterr()  # 丢掉 _die 输出

        # 加 --force 才放行
        _do_restore(out, target, force=True)
        restore_out = json.loads(capsys.readouterr().out)
        assert restore_out["status"] in ("ok", "ok_with_warnings")
