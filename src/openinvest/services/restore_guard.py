"""Restore data without reviving revoked evidence or authorizing stale ledger writes."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import stat
import zipfile
import sqlite3
import tempfile
import os
from contextlib import closing

from openinvest.core.memory_store import _atomic_write_text, _file_lock


def restore_archive(root: Path, archive: Path) -> dict:
    root, archive = Path(root).resolve(), Path(archive).resolve()
    integrity = root / "integrity"
    integrity.mkdir(parents=True, exist_ok=True)
    if (integrity / "eligibility.json").exists() and not (integrity / "exclusions.jsonl").exists():
        raise ValueError("existing eligibility registry has lost its exclusions ledger; recover it before restoring")
    with zipfile.ZipFile(archive) as zf:
        members = []
        for member in zf.infolist():
            relative = PurePosixPath(member.filename)
            if relative.is_absolute() or ".." in relative.parts or stat.S_ISLNK(member.external_attr >> 16):
                raise ValueError("unsafe backup path or symlink")
            target = (root / member.filename).resolve()
            if not target.is_relative_to(root):
                raise ValueError("backup escapes the selected data root")
            if relative.parts and relative.parts[0] in {"memory", "db", ".env", "user_profile.json", "user_profile.json.bak", ".hosted-initialization.json"}:
                members.append(member)
        # Refuse active/stale WAL state before changing anything. Online restore
        # cannot safely reuse old SQLite sidecars; the operator must stop writers.
        for member in members:
            if member.is_dir():
                continue
            target = root / member.filename
            if target.suffix in {".db", ".sqlite", ".sqlite3"}:
                if any(Path(str(target)+suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
                    raise ValueError("SQLite sidecars present; stop writers and reconcile/checkpoint before offline restore")
                if target.exists():
                    with closing(sqlite3.connect(target, timeout=0)) as connection:
                        connection.execute("BEGIN EXCLUSIVE")
                        connection.rollback()
                with tempfile.NamedTemporaryFile(dir=integrity, suffix=".sqlite3") as staged:
                    staged.write(zf.read(member)); staged.flush()
                    with closing(sqlite3.connect(staged.name)) as connection:
                        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                            raise ValueError("backup database failed integrity verification")
        marker = {"restored_at": datetime.now(timezone.utc).isoformat(),
                  "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                  "reason": "financial state requires reconciliation; historical learning approvals are not restored"}
        with _file_lock(root / "memory/portfolio.md"):
            _atomic_write_text(integrity / "restore_pending.json", json.dumps(marker, ensure_ascii=False))
            # Existing integrity/ (especially exclusions and spent holdouts) is
            # retained. A fresh installation gets no historical positive grants.
            if not (integrity / "eligibility.json").exists():
                _atomic_write_text(integrity / "eligibility.json", '{"schema":1,"artifacts":{}}')
            if not (integrity / "exclusions.jsonl").exists():
                _atomic_write_text(integrity / "exclusions.jsonl", "")
            for member in members:
                target = root / member.filename
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as staged:
                        staged.write(zf.read(member)); staged.flush(); os.fsync(staged.fileno())
                        temporary = Path(staged.name)
                    try:
                        os.replace(temporary, target)
                    finally:
                        temporary.unlink(missing_ok=True)
    return {"status": "restored_pending_reconciliation", "files": len(members),
            "automatic_writes_enabled": False}
