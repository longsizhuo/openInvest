"""Identity/control database; no ledger or research payloads are stored here."""
from contextlib import contextmanager
from pathlib import Path
import hashlib
import json
import secrets
import sqlite3
import time
import uuid
import os
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class Conflict(ValueError):
    pass


class Store:
    def __init__(self, root: Path, encryption_key: bytes):
        self.root = root
        self.cipher = AESGCM(encryption_key)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = root / "control.sqlite3"
        with self.db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS accounts (
              id TEXT PRIMARY KEY, subject TEXT UNIQUE, participant TEXT UNIQUE NOT NULL,
              initialized INTEGER NOT NULL DEFAULT 0, accepted TEXT,
              research INTEGER NOT NULL DEFAULT 0, epoch INTEGER NOT NULL DEFAULT 0,
              revoked_before REAL NOT NULL DEFAULT 0, deleted INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS credentials (
              id TEXT PRIMARY KEY, owner TEXT NOT NULL, digest TEXT UNIQUE NOT NULL,
              kind TEXT NOT NULL, label TEXT NOT NULL, created REAL NOT NULL, expires REAL,
              csrf TEXT, revoked INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS oauth_connections (
              owner TEXT NOT NULL, client TEXT NOT NULL, revoked INTEGER DEFAULT 0,
              PRIMARY KEY(owner,client));
            CREATE TABLE IF NOT EXISTS rates (owner TEXT, window INTEGER, n INTEGER,
              PRIMARY KEY(owner,window));
            CREATE TABLE IF NOT EXISTS metrics (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS clients(owner TEXT,credential TEXT,info BLOB,PRIMARY KEY(owner,credential));
            CREATE TABLE IF NOT EXISTS tickets (digest TEXT PRIMARY KEY, created REAL NOT NULL);
            """)
        self.path.chmod(0o600)
        business = root / "business"
        business.mkdir(mode=0o700, exist_ok=True)
        self.receipts_path = business / "receipts.sqlite3"
        with self.receipts_db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS operations(owner TEXT,key TEXT,fingerprint TEXT NOT NULL,state TEXT NOT NULL,result BLOB,created REAL NOT NULL,PRIMARY KEY(owner,key))")
        self.receipts_path.chmod(0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def receipts_db(self):
        db = sqlite3.connect(self.receipts_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def account(self, owner):
        with self.db() as db:
            row = db.execute("SELECT * FROM accounts WHERE id=? AND deleted=0", (owner,)).fetchone()
        return dict(row) if row else None

    def ticket_once(self, ticket):
        with self.db() as db:
            db.execute("DELETE FROM tickets WHERE created<?", (time.time()-900,))
            try:
                db.execute("INSERT INTO tickets VALUES(?,?)", (self.digest(ticket), time.time()))
            except sqlite3.IntegrityError:
                raise Conflict("Authorization ticket was already used") from None

    def identity(self, subject):
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO accounts(id,subject,participant) VALUES(?,?,?)",
                       (uuid.uuid4().hex, subject, uuid.uuid4().hex))
            row = db.execute("SELECT * FROM accounts WHERE subject=?", (subject,)).fetchone()
        if row["deleted"]:
            raise Conflict("Account was deleted")
        return dict(row)

    def preferences(self, owner, enabled, policy=None):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE accounts SET research=?, epoch=epoch+1, accepted=COALESCE(?,accepted) WHERE id=? AND deleted=0",
                       (int(enabled), policy, owner))
        return self.account(owner)

    def initialized(self, owner):
        with self.db() as db:
            db.execute("UPDATE accounts SET initialized=1 WHERE id=?", (owner,))

    @staticmethod
    def digest(value):
        return hashlib.sha256(value.encode()).hexdigest()

    def credential(self, owner, kind="pat", label="Terminal", ttl=None):
        token = ("oi_" if kind == "pat" else "ws_") + secrets.token_urlsafe(32)
        item = dict(id=uuid.uuid4().hex, owner=owner, digest=self.digest(token), kind=kind,
                    label=label, created=time.time(), expires=time.time()+ttl if ttl else None,
                    csrf=secrets.token_urlsafe(24) if kind == "session" else None)
        with self.db() as db:
            db.execute("INSERT INTO credentials(id,owner,digest,kind,label,created,expires,csrf) VALUES(:id,:owner,:digest,:kind,:label,:created,:expires,:csrf)", item)
        return token, item

    def authenticate(self, token, kind):
        with self.db() as db:
            row = db.execute("SELECT * FROM credentials WHERE digest=? AND kind=? AND revoked=0 AND (expires IS NULL OR expires>?)",
                             (self.digest(token), kind, time.time())).fetchone()
        if row and self.account(row["owner"]):
            return dict(row)
        return None

    def credentials(self, owner):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT id,label,created,revoked FROM credentials WHERE owner=? AND kind='pat'", (owner,))]

    def revoke(self, owner, credential_id):
        with self.db() as db:
            db.execute("UPDATE credentials SET revoked=1 WHERE owner=? AND id=?", (owner, credential_id))

    def revoke_all(self, owner):
        with self.db() as db:
            db.execute("UPDATE credentials SET revoked=1 WHERE owner=? AND kind='pat'", (owner,))
            db.execute("UPDATE oauth_connections SET revoked=1 WHERE owner=?", (owner,))

    def connect_oauth(self, owner, client, *, authorize=False):
        with self.db() as db:
            if authorize:
                db.execute("INSERT INTO oauth_connections(owner,client,revoked) VALUES(?,?,0) ON CONFLICT(owner,client) DO UPDATE SET revoked=0", (owner, client))
            row = db.execute("SELECT revoked FROM oauth_connections WHERE owner=? AND client=?", (owner, client)).fetchone()
        return bool(row and not row[0])

    def reserve(self, owner, key, method, arguments):
        fingerprint = self.digest(json.dumps([method, arguments], sort_keys=True, separators=(",", ":"), ensure_ascii=False))
        with self.receipts_db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM operations WHERE owner=? AND key=?", (owner, key)).fetchone()
            if row:
                if row["fingerprint"] != fingerprint:
                    raise Conflict("Idempotency key was used with different arguments")
                if row["state"] == "done":
                    blob = bytes(row["result"])
                    return json.loads(self.cipher.decrypt(blob[:12], blob[12:], owner.encode()))
                raise Conflict("Previous operation outcome is unknown; inspect the ledger before continuing")
            db.execute("INSERT INTO operations VALUES(?,?,?,'pending',NULL,?)", (owner, key, fingerprint, time.time()))
        return None

    def complete(self, owner, key, result):
        nonce = os.urandom(12)
        receipt = nonce + self.cipher.encrypt(nonce, json.dumps(result, ensure_ascii=False).encode(), owner.encode())
        with self.receipts_db() as db:
            db.execute("UPDATE operations SET state='done',result=? WHERE owner=? AND key=?",
                       (receipt, owner, key))

    def rate(self, owner):
        window = int(time.time() // 60)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM rates WHERE window<?", (window-1,))
            db.execute("INSERT INTO rates VALUES(?,?,1) ON CONFLICT(owner,window) DO UPDATE SET n=n+1", (owner, window))
            return db.execute("SELECT n FROM rates WHERE owner=? AND window=?", (owner, window)).fetchone()[0] <= 30

    def metric(self, name):
        try:
            with self.db() as db:
                db.execute("INSERT INTO metrics VALUES(?,1) ON CONFLICT(name) DO UPDATE SET value=value+1", (name,))
        except sqlite3.Error:
            pass  # Accounting/telemetry failures must not change completed operations.

    def delete(self, owner):
        with self.db() as db:
            db.execute("UPDATE accounts SET deleted=1,research=0,epoch=epoch+1,subject=NULL WHERE id=?", (owner,))
            db.execute("UPDATE credentials SET revoked=1 WHERE owner=?", (owner,))
            db.execute("DELETE FROM clients WHERE owner=?", (owner,))
        with self.receipts_db() as db:
            db.execute("DELETE FROM operations WHERE owner=?", (owner,))


    def client_info(self, owner, credential, data=None):
        digest = self.digest(credential)
        with self.db() as db:
            if data is not None:
                nonce = os.urandom(12)
                payload = nonce + self.cipher.encrypt(nonce, json.dumps(data).encode(), owner.encode())
                db.execute("INSERT OR REPLACE INTO clients VALUES(?,?,?)", (owner, digest, payload))
            row = db.execute("SELECT info FROM clients WHERE owner=? AND credential=?", (owner,digest)).fetchone()
        if not row:
            return {}
        blob = row[0]
        return json.loads(self.cipher.decrypt(blob[:12], blob[12:], owner.encode()))

    def gauge(self, name, value):
        with self.db() as db:
            db.execute("INSERT INTO metrics VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value", (name,value))


    def connections(self, owner):
        with self.db() as db:
            return [dict(row) for row in db.execute("SELECT client,revoked FROM oauth_connections WHERE owner=?", (owner,))]

    def revoke_connection(self, owner, client):
        with self.db() as db:
            db.execute("UPDATE oauth_connections SET revoked=1 WHERE owner=? AND client=?", (owner,client))

    def reset_ledger(self, owner):
        with self.db() as db:
            db.execute("UPDATE accounts SET initialized=0 WHERE id=?", (owner,))
        with self.receipts_db() as db:
            db.execute("DELETE FROM operations WHERE owner=?", (owner,))
