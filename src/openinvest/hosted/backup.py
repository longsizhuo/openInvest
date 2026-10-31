"""Encrypted consistent backups; database copies include SQLite WAL state."""
import asyncio
from contextlib import closing
import base64
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def seal(settings, kind, owner, data):
    nonce = os.urandom(12)
    payload = json.dumps({"version": 1, "kind": kind, "owner": owner, "created": time.time(),
                          "data": base64.b64encode(data).decode()}).encode()
    return nonce + AESGCM(settings.encryption_key).encrypt(nonce, payload, b"backup-v1")


def unseal(settings, blob, *, kind, owner):
    data = json.loads(AESGCM(settings.encryption_key).decrypt(blob[:12], blob[12:], b"backup-v1"))
    if data["version"] != 1 or data["kind"] != kind or data["owner"] != owner:
        raise ValueError("Backup type/owner mismatch")
    return base64.b64decode(data["data"], validate=True)


def database_copy(path):
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp)/"copy.sqlite3"
        with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(target)) as destination:
            source.backup(destination)
            destination.execute("PRAGMA journal_mode=DELETE")
        return target.read_bytes()


async def backup_all(settings, store, research, pool, archive):
    if archive is None:
        raise ValueError("Configure COS before backups")
    stamp = str(int(time.time()))
    with store.db() as db:
        owners = [r[0] for r in db.execute("SELECT id FROM accounts WHERE initialized=1 AND deleted=0")]
    for owner in owners:
        blob = await pool.internal(owner, "export")
        await asyncio.to_thread(archive.put, f"backups/{owner}/{stamp}.aesgcm", seal(settings, "ledger", owner, blob))
    for name, path in (("control", store.path), ("research", research.path), ("receipts", store.receipts_path)):
        content = await asyncio.to_thread(database_copy, path)
        await asyncio.to_thread(archive.put, f"backups/{name}/{stamp}.aesgcm", seal(settings, name, name, content))
    await asyncio.to_thread(archive.put, f"backups/manifests/{stamp}.aesgcm", seal(settings, "manifest", "manifest", json.dumps({"owners": owners, "stamp": stamp}).encode()))
    if hasattr(archive, "prune_backups"):
        await asyncio.to_thread(archive.prune_backups, 30)
    return {"backup": stamp, "accounts": len(owners)}


def restore_database(settings, archive, name, stamp):
    if name not in {"control", "research", "receipts"} or not stamp.isdigit():
        raise ValueError("Invalid recovery source")
    path = settings.root/("business/receipts.sqlite3" if name == "receipts" else name+".sqlite3")
    if any(Path(str(path)+suffix).exists() for suffix in ("", "-wal", "-shm")):
        raise ValueError("Restore databases into a new empty HOSTED_DATA_DIR with the gateway stopped")
    data = unseal(settings, archive.get(f"backups/{name}/{stamp}.aesgcm"), kind=name, owner=name)
    settings.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".restoring")
    temporary.write_bytes(data)
    temporary.chmod(0o600)
    try:
        with closing(sqlite3.connect(temporary)) as db:
            db.execute("PRAGMA journal_mode=DELETE")
            with db:
                if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Invalid database backup")
                if name == "control":
                    db.execute("UPDATE credentials SET revoked=1")
                    db.execute("UPDATE oauth_connections SET revoked=1")
                    # A historical backup cannot prove the latest opt-out state.
                    db.execute("UPDATE accounts SET research=0,epoch=epoch+1,accepted=NULL")
                elif name == "research":
                    # Untrained candidates may have been deleted after the backup;
                    # never resurrect them from a stale control-plane snapshot.
                    db.execute("DELETE FROM samples WHERE used=0")
                    db.execute("DELETE FROM datasets WHERE used=0")
                    db.execute("DELETE FROM reviews WHERE id NOT IN (SELECT id FROM samples)")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
