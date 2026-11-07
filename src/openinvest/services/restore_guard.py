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


def _exclusion_rows(text: str) -> list[dict]:
    rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    if any(not isinstance(r, dict) or not isinstance(r.get("artifact_id"), str) or not r["artifact_id"] for r in rows):
        raise ValueError("invalid restored exclusion ledger")
    return rows


def restore_archive(root: Path, archive: Path) -> dict:
    root, archive = Path(root).resolve(), Path(archive).resolve()
    integrity = root / "integrity"
    if integrity.is_symlink():
        raise ValueError("protected integrity directory must not be a symlink")
    integrity.mkdir(parents=True, exist_ok=True)
    if (integrity / "eligibility.json").exists() and not (integrity / "exclusions.jsonl").exists():
        raise ValueError("existing eligibility registry has lost its exclusions ledger; recover it before restoring")
    with zipfile.ZipFile(archive) as zf:
        archive_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
        names = set(zf.namelist())
        incoming = _exclusion_rows(zf.read("integrity/exclusions.jsonl").decode()) if "integrity/exclusions.jsonl" in names else []
        old_registry = json.loads(zf.read("integrity/eligibility.json")) if "integrity/eligibility.json" in names else {}
        artifacts = old_registry.get("artifacts", {})
        if not isinstance(artifacts, dict):
            raise ValueError("invalid restored eligibility registry")
        denied = {r["artifact_id"] for r in incoming}
        denied.update(k for k,v in artifacts.items() if v.get("deleted") or v.get("research_participation") == "revoked")
        while True:
            inherited = {k for k,v in artifacts.items() if any(parent in denied for parent in v.get("parents", []))}
            if inherited <= denied:
                break
            denied |= inherited
        incoming.extend({"artifact_id": key, "reason": "inherited_restored_revocation", "archive_sha256": archive_hash}
                        for key in sorted(denied - {r["artifact_id"] for r in incoming}))
        members = []
        for member in zf.infolist():
            relative = PurePosixPath(member.filename)
            if relative.is_absolute() or ".." in relative.parts or stat.S_ISLNK(member.external_attr >> 16):
                raise ValueError("unsafe backup path or symlink")
            target = (root / member.filename).resolve()
            if not target.is_relative_to(root):
                raise ValueError("backup escapes the selected data root")
            if relative.parts and relative.parts[0]!="integrity" and target.is_relative_to(integrity):
                raise ValueError("backup target aliases protected integrity state")
            candidate=root/member.filename
            if any(part.is_symlink() for part in (candidate,*candidate.parents) if part!=root and part.is_relative_to(root)):
                raise ValueError("restore target contains an existing symlink")
            if relative.parts and relative.parts[0] in {"memory", "db", ".env", "user_profile.json", "user_profile.json.bak", ".hosted-initialization.json"}:
                if relative.parts[0] == "db" and member.filename.endswith(("-wal", "-shm", "-journal")):
                    if member.file_size:
                        raise ValueError("SQLite sidecar archive requires verified offline consolidation before restore")
                    continue
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
                        if "integrity/exclusions.jsonl" not in names and connection.execute(
                                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='events'").fetchone():
                            # Legacy archives cannot certify which raw events were revoked.
                            # Keep their rows for audit; they cannot silently regain advice use.
                            incoming.extend({"artifact_id": "event:"+str(row[0]),
                                             "reason": "restored_without_revocation_history_audit_only",
                                             "archive_sha256": archive_hash}
                                            for row in connection.execute("SELECT event_id FROM events"))
        marker = {"restored_at": datetime.now(timezone.utc).isoformat(),
                  "archive_sha256": archive_hash,
                  "reason": "financial state requires reconciliation; historical learning approvals are not restored"}
        with _file_lock(integrity / "governance"), _file_lock(root / "memory/portfolio.md"):
            existing = _exclusion_rows((integrity / "exclusions.jsonl").read_text()) if (integrity / "exclusions.jsonl").exists() else []
            from openinvest.core.evidence_eligibility import Eligibility
            # An exclusion made AFTER this backup must propagate through the
            # backup's parent graph too. Never restore its positive grants.
            denied.update(Eligibility(root).excluded_ids())
            denied.update(row["artifact_id"] for row in existing)
            while True:
                inherited={key for key,value in artifacts.items() if any(parent in denied for parent in value.get("parents",[]))}
                if inherited<=denied:break
                denied.update(inherited)
            incoming.extend({"artifact_id":key,"reason":"inherited_current_or_restored_revocation","archive_sha256":archive_hash}
                            for key in sorted(denied-{row["artifact_id"] for row in incoming}))
            known = {r["artifact_id"] for r in existing}
            for row in incoming:
                if row["artifact_id"] not in known:
                    existing.append(row); known.add(row["artifact_id"])
            _atomic_write_text(integrity / "restore_pending.json", json.dumps(marker, ensure_ascii=False))
            # Existing integrity/ (especially exclusions and spent holdouts) is
            # retained. A fresh installation gets no historical positive grants.
            if not (integrity / "eligibility.json").exists():
                _atomic_write_text(integrity / "eligibility.json", '{"schema":1,"artifacts":{}}')
            _atomic_write_text(integrity / "exclusions.jsonl", "".join(json.dumps(r, ensure_ascii=False)+"\n" for r in existing))
            if "integrity/final_set_uses.json" in names:
                spent_path = integrity / "final_set_uses.json"
                spent = json.loads(spent_path.read_text()) if spent_path.exists() else {"datasets": {}, "windows": []}
                restored = json.loads(zf.read("integrity/final_set_uses.json"))
                for key, value in restored.get("datasets", {}).items():
                    spent["datasets"].setdefault(key, "restored-use:"+archive_hash+":"+value)
                for window in restored.get("windows", []):
                    blocked_window = {**window, "candidate": "restored-use:"+archive_hash+":"+window["candidate"]}
                    if blocked_window not in spent["windows"]:
                        spent["windows"].append(blocked_window)
                _atomic_write_text(spent_path, json.dumps(spent, sort_keys=True))
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
