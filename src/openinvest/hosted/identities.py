"""Application-owned login bindings. Auth0 authenticates; it does not own ledger IDs."""
import secrets
import time
import uuid


def provider_for(subject):
    return {"google-oauth2": "google", "github": "github"}.get(subject.split("|", 1)[0], "other")


class IdentityStore:
    def migrate_identities(self):
        # Additive, transactional migration: no ledger files or account IDs change.
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if "merged_into" not in {r[1] for r in db.execute("PRAGMA table_info(accounts)")}:
                db.execute("ALTER TABLE accounts ADD COLUMN merged_into TEXT")
            if "session_subject" not in {r[1] for r in db.execute("PRAGMA table_info(credentials)")}:
                db.execute("ALTER TABLE credentials ADD COLUMN session_subject TEXT")
            if "authenticated_at" not in {r[1] for r in db.execute("PRAGMA table_info(credentials)")}:
                db.execute("ALTER TABLE credentials ADD COLUMN authenticated_at REAL NOT NULL DEFAULT 0")
            db.execute("CREATE TABLE IF NOT EXISTS identity_schema(version INTEGER PRIMARY KEY)")
            db.execute("""CREATE TABLE IF NOT EXISTS identities(
                id TEXT PRIMARY KEY, subject TEXT UNIQUE NOT NULL, owner TEXT NOT NULL,
                provider TEXT NOT NULL, created REAL NOT NULL, tokens_after REAL NOT NULL DEFAULT 0,
                FOREIGN KEY(owner) REFERENCES accounts(id))""")
            db.execute("CREATE INDEX IF NOT EXISTS identities_owner ON identities(owner)")
            db.execute("CREATE TABLE IF NOT EXISTS identity_revocations(subject TEXT PRIMARY KEY,tokens_after REAL NOT NULL)")
            db.execute("""CREATE TABLE IF NOT EXISTS identity_flows(
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, credential TEXT NOT NULL,
                primary_subject TEXT NOT NULL, provider TEXT NOT NULL, phase TEXT NOT NULL,
                started REAL NOT NULL, expires REAL NOT NULL, secondary_subject TEXT,
                source_owner TEXT)""")
            if not db.execute("SELECT 1 FROM identity_schema WHERE version=1").fetchone():
                for row in db.execute("SELECT id,subject FROM accounts WHERE deleted=0 AND subject IS NOT NULL").fetchall():
                    db.execute("INSERT INTO identities VALUES(?,?,?,?,?,0)",
                               (uuid.uuid4().hex, row["subject"], row["id"], provider_for(row["subject"]), time.time()))
                db.execute("UPDATE credentials SET session_subject=(SELECT subject FROM accounts WHERE id=owner) WHERE kind='session'")
                db.execute("INSERT INTO identity_schema VALUES(1)")

    def identity_binding(self, subject):
        with self.db() as db:
            row = db.execute("""SELECT i.* FROM identities i JOIN accounts a ON a.id=i.owner
                WHERE i.subject=? AND a.deleted=0 AND a.merged_into IS NULL""", (subject,)).fetchone()
        return dict(row) if row else None

    def subject_tokens_after(self, subject):
        with self.db() as db:
            cutoff = db.execute("SELECT tokens_after FROM identity_revocations WHERE subject=?", (subject,)).fetchone()
        binding = self.identity_binding(subject)
        return max(cutoff[0] if cutoff else 0, binding["tokens_after"] if binding else 0)

    def login_methods(self, owner):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT id,provider,created FROM identities WHERE owner=? ORDER BY created,id", (owner,))]

    def archived_accounts(self, owner):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT id,initialized FROM accounts WHERE merged_into=? AND deleted=0 ORDER BY id", (owner,))]

    def participants(self, owner):
        with self.db() as db:
            return [r[0] for r in db.execute("SELECT participant FROM accounts WHERE id=? OR merged_into=?", (owner, owner))]

    def start_identity_flow(self, owner, credential, subject, provider):
        from .store import Conflict
        if provider not in {"google", "github"}:
            raise Conflict("unsupported_login_provider")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            binding = db.execute("SELECT owner FROM identities WHERE subject=?", (subject,)).fetchone()
            if not binding or binding[0] != owner:
                raise Conflict("login_identity_changed")
            if db.execute("SELECT 1 FROM identities WHERE owner=? AND provider=?", (owner, provider)).fetchone():
                raise Conflict("login_provider_already_linked")
            db.execute("DELETE FROM identity_flows WHERE credential=? OR expires<?", (credential, time.time()))
            flow = secrets.token_urlsafe(32)
            db.execute("INSERT INTO identity_flows VALUES(?,?,?,?,?,'primary',?,?,NULL,NULL)",
                       (flow, owner, credential, subject, provider, time.time(), time.time()+600))
        return flow

    def identity_flow(self, flow, credential):
        with self.db() as db:
            row = db.execute("SELECT * FROM identity_flows WHERE id=? AND credential=? AND expires>?", (flow, credential, time.time())).fetchone()
        return dict(row) if row else None

    def verify_identity_flow(self, flow, credential, subject, auth_time):
        from .store import Conflict
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM identity_flows WHERE id=? AND credential=? AND expires>?", (flow, credential, time.time())).fetchone()
            if not row or type(auth_time) not in {int, float} or not row["started"]-5 <= auth_time <= time.time()+60:
                raise Conflict("login_verification_expired")
            if row["phase"] == "primary":
                binding = db.execute("SELECT owner FROM identities WHERE subject=?", (subject,)).fetchone()
                if subject != row["primary_subject"] or not binding or binding[0] != row["owner"]:
                    raise Conflict("login_identity_changed")
                db.execute("UPDATE identity_flows SET phase='secondary' WHERE id=?", (flow,))
            elif row["phase"] == "secondary":
                if provider_for(subject) != row["provider"]:
                    raise Conflict("login_provider_mismatch")
                binding = db.execute("SELECT owner FROM identities WHERE subject=?", (subject,)).fetchone()
                if binding and binding[0] == row["owner"]:
                    raise Conflict("login_provider_already_linked")
                db.execute("UPDATE identity_flows SET phase='confirm',secondary_subject=?,source_owner=? WHERE id=?",
                           (subject, binding[0] if binding else None, flow))
            else:
                raise Conflict("login_verification_expired")

    def finish_identity_flow(self, flow, credential):
        from .store import Conflict
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM identity_flows WHERE id=? AND credential=? AND expires>? AND phase='confirm'", (flow, credential, time.time())).fetchone()
            if not row or not db.execute("SELECT 1 FROM credentials WHERE id=? AND revoked=0 AND expires>?", (credential, time.time())).fetchone():
                raise Conflict("login_verification_expired")
            owner, source = row["owner"], row["source_owner"]
            current = db.execute("SELECT owner FROM identities WHERE subject=?", (row["primary_subject"],)).fetchone()
            secondary = db.execute("SELECT owner FROM identities WHERE subject=?", (row["secondary_subject"],)).fetchone()
            if not current or current[0] != owner or (secondary[0] if secondary else None) != source:
                raise Conflict("login_identity_changed")
            if source:
                # Preserve the complete source ledger, receipts, preferences and participant.
                # No monetary rows move between accounts; old credentials cannot follow.
                db.execute("UPDATE accounts SET subject=NULL,merged_into=?,research=0,epoch=epoch+1 WHERE id=?", (owner, source))
                db.execute("UPDATE accounts SET merged_into=? WHERE merged_into=?", (owner, source))
                db.execute("UPDATE identities SET owner=?,tokens_after=? WHERE owner=?", (owner, time.time(), source))
                db.execute("UPDATE credentials SET revoked=1 WHERE owner=?", (source,))
                db.execute("UPDATE oauth_connections SET revoked=1 WHERE owner=?", (source,))
                db.execute("DELETE FROM clients WHERE owner=?", (source,))
            else:
                db.execute("INSERT INTO identities VALUES(?,?,?,?,?,?)", (uuid.uuid4().hex, row["secondary_subject"], owner, row["provider"], time.time(), time.time()))
            # Invalidate concurrent browser flows/sessions after a security-sensitive change.
            db.execute("UPDATE credentials SET revoked=1 WHERE owner=? AND kind='session'", (owner,))
            db.execute("DELETE FROM identity_flows WHERE owner=? OR owner=?", (owner, source))
            return owner, row["primary_subject"]

    def unlink_identity(self, owner, identity_id, current_subject):
        from .store import Conflict
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT id,subject FROM identities WHERE owner=? ORDER BY created,id", (owner,)).fetchall()
            target = next((r for r in rows if r["id"] == identity_id), None)
            if not target:
                raise Conflict("login_identity_changed")
            if len(rows) < 2:
                raise Conflict("last_login_method")
            if target["subject"] == current_subject:
                raise Conflict("current_login_method")
            db.execute("INSERT OR REPLACE INTO identity_revocations VALUES(?,?)", (target["subject"], time.time()))
            db.execute("DELETE FROM identities WHERE id=? AND owner=?", (identity_id, owner))
            db.execute("UPDATE credentials SET revoked=1 WHERE owner=? AND session_subject=?", (owner, target["subject"]))
            replacement = next(r["subject"] for r in rows if r["id"] != identity_id)
            db.execute("UPDATE accounts SET subject=? WHERE id=? AND subject=?", (replacement, owner, target["subject"]))
            db.execute("DELETE FROM identity_flows WHERE owner=?", (owner,))
