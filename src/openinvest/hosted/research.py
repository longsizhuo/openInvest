"""Encrypted, bounded candidate queue. Identity fields never enter training exports."""
from contextlib import contextmanager
import base64
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

log = logging.getLogger(__name__)
# Unknown fields and raw text are not silently copied into the structured dataset.
NUMERIC = {"amount", "units", "quantity", "price", "cost_price", "cash_cny", "cash_aud", "alloc_cny",
           "suggested_allocation", "current_price", "total_value", "portfolio_value", "total_assets_cny",
           "target_pct", "target_allocation_stock", "target_allocation_cash", "confidence", "fee", "fee_cny",
           "total_amount", "max_single_invest_cny", "latency_ms", "input_tokens", "output_tokens", "atr_pct",
           "stock_pct", "cash_pct", "concentration_pct", "cost", "value", "market_value", "return_pct",
           "avg_cost", "market_value_cny", "pnl", "pnl_pct", "cny", "aud", "aud_in_cny", "_original_alloc", "_original_confidence", "_original_confidence_cap", "_original_confidence_unavailable"}
CONTAINERS = {"cash", "holdings", "asset", "assets", "verdict", "result", "data", "input_health",
              "intervention", "facts", "metrics", "strategy", "current_assets", "regime", "usage", "research_evidence", "original", "final", "sources", "versions", "input_snapshot", "all_holdings", "all_currencies", "target_assets", "performance", "income_30d", "pending_fund_orders", "fx"}
STRINGS = {"symbol", "currency", "cost_currency", "alloc_currency", "base_currency", "kind", "action",
           "verdict", "original_verdict", "final_verdict", "_original_verdict", "_original_trim_reason", "status", "rule", "rules", "provider", "model",
           "regime_label", "algorithm_version", "as_of", "data_date", "round", "source", "rule_version", "backend_version", "tool_version", "prompt_version", "client_version", "client_name", "snapshot_hash", "facts_source"}
# Bound the start of an email candidate and consume labels without backtracking.
# Otherwise a long run of '+' without '@' is retried from every character.
PII = re.compile(r"(?<![\w.+-])[\w.+-]++@(?:[\w-]++\.(?=[\w-]))++[A-Za-z]{2,}+|(?:Bearer\s+|sk-|oi_|ws_)[A-Za-z0-9_-]{8,}|https?://|\b(?:\d{1,3}\.){3}\d{1,3}\b|\b\d{15,19}\b", re.I)


def structured(value, parent=""):
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key in NUMERIC and isinstance(item, (int, float)) and not isinstance(item, bool):
                if item == item and abs(item) < 1e100:
                    out[key] = item
            elif ((parent in {"cash", "all_currencies"} and re.fullmatch(r"[A-Z]{3}", key)) or (parent == "fx" and re.fullmatch(r"[a-z]{3}cny", key))) and isinstance(item, (int, float)):
                out[key] = item
            elif key in CONTAINERS:
                out[key] = structured(item, key)
            elif key in STRINGS and isinstance(item, str) and len(item) <= 160 and not PII.search(item):
                out[key] = item
        return out
    if isinstance(value, list):
        return [structured(item, parent) for item in value[:1000] if isinstance(item, (dict, list))]
    return {}


def features(arguments, result):
    derived = {"currency_conversion": "none"}
    cash = result.get("cash", {}) if isinstance(result, dict) else {}
    cash = cash.get("all_currencies", cash)
    for currency, value in cash.items():
        if isinstance(value, (int, float)):
            magnitude = abs(value)
            derived.setdefault("cash_scale", {})[currency] = next((label for limit, label in
                ((1000, "under_1k"), (10000, "1k_10k"), (100000, "10k_100k"),
                 (1000000, "100k_1m"), (float("inf"), "over_1m")) if magnitude < limit), "over_1m")
    for key in ("stock_pct", "cash_pct", "concentration_pct"):
        if isinstance(result, dict) and key in result:
            derived[key] = result[key]
    if isinstance(result, dict) and isinstance(result.get("total_assets_cny"), (int,float)) and result["total_assets_cny"] > 0:
        derived["position_ratios_cny"] = [{"symbol": h["symbol"], "ratio": h["market_value_cny"]/result["total_assets_cny"]}
            for h in result.get("all_holdings", []) if isinstance(h,dict) and isinstance(h.get("market_value_cny"), (int,float)) and h.get("symbol")]
    total = result.get("portfolio_value") if isinstance(result, dict) else None
    if isinstance(total, (int, float)) and total > 0 and arguments.get("currency") == result.get("currency"):
        amount = arguments.get("amount")
        if isinstance(amount, (int, float)):
            derived["amount_to_portfolio_ratio"] = amount / total
    return derived


class Research:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.path = settings.root / "research.sqlite3"
        self.cipher = AESGCM(settings.encryption_key)
        self.tool_version = hashlib.sha256((Path(__file__).parents[1]/"connectors"/"mcp_server.py").read_bytes()).hexdigest()
        with self.db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS samples(id TEXT PRIMARY KEY, participant TEXT, epoch INTEGER,
              payload BLOB, bytes INTEGER, status TEXT, object_key TEXT, used INTEGER DEFAULT 0,
              label TEXT DEFAULT 'unverified', created REAL);
            CREATE TABLE IF NOT EXISTS reviews(id TEXT PRIMARY KEY, payload BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS datasets(id TEXT PRIMARY KEY, samples TEXT, used INTEGER DEFAULT 0, path TEXT NOT NULL);
            """)
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def seal(self, data):
        nonce = os.urandom(12)
        return nonce + self.cipher.encrypt(nonce, json.dumps(data, ensure_ascii=False, allow_nan=False).encode(), b"research-v1")

    def unseal(self, data):
        return json.loads(self.cipher.decrypt(data[:12], data[12:], b"research-v1"))

    def capture(self, account, tool, arguments, result, latency_ms, *, failed=False, retry=False, client_info=None):
        try:
            # Read preferences at completion too: turning off collection cancels in-flight capture.
            current = self.store.account(account["id"])
            if not current or not current["accepted"] or not current["research"] or current["epoch"] != account["epoch"]:
                return
            sample_id = uuid.uuid4().hex
            from importlib.metadata import version
            extracted = structured(result)
            analysis_id = arguments.get("analysis_id") or (result.get("analysis_id") if isinstance(result, dict) else None)
            if not isinstance(analysis_id, str) or not re.fullmatch(r"[a-f0-9]{32}", analysis_id):
                analysis_id = None
            text = arguments.get("transcript", "")
            parts = re.split(r"^===\s*(MACRO|QUANT_R1|RISK_R1|QUANT_R2|RISK_R2|CIO)\s*===\s*$", text, flags=re.M)
            roles = {parts[i] for i in range(1, len(parts)-1, 2) if parts[i+1].strip()}
            complete = roles == {"MACRO", "QUANT_R1", "RISK_R1", "QUANT_R2", "RISK_R2", "CIO"}
            payload = {"schema_version": 1, "sample_id": sample_id, "tool": tool,
                       "sample_type": "decision" if tool == "submit_committee" and complete and not failed else "tool_call",
                       "analysis_completeness": "complete" if complete else "partial_or_unavailable",
                       "participant": current["participant"], "retry": retry, "client": structured(client_info or {}),
                       "arguments": structured(arguments), "result": extracted,
                       "analysis_id": analysis_id,
                       "latency_ms": latency_ms, "outcome": "error" if failed else "tool_returned",
                       "task_goal": "unknown", "adoption": "unknown", "correctness": "unverified",
                       "runtime_version": version("openinvest"), "tool_version": self.tool_version, "client_model_source": "self_reported",
                       "policy_version": current["accepted"], "created": time.time()}
            # Raw natural language is encrypted and quarantined, never auto-exported.
            texts = {k: v for k, v in arguments.items() if k in {"transcript", "reason", "description"} and isinstance(v, str)}
            if isinstance(result, dict):
                texts.update((result.get("research_evidence") or {}).get("prepared_text", {}))
            payload["quarantine"] = {k: PII.sub("[REDACTED]", v) for k, v in texts.items()}
            payload["features"] = features(payload["arguments"], extracted)
            encrypted = self.seal(payload)
            with self.db() as db:
                db.execute("BEGIN IMMEDIATE")
                queued = db.execute("SELECT COALESCE(SUM(bytes),0) FROM samples WHERE payload IS NOT NULL").fetchone()[0]
                if queued + len(encrypted) > self.settings.queue_bytes:
                    self.store.metric("research_queue_full")
                    return
                # Recheck after queue-lock acquisition; settings changes also remove pending entries.
                latest = self.store.account(account["id"])
                if not latest or not latest["research"] or latest["epoch"] != account["epoch"]:
                    return
                db.execute("INSERT INTO samples(id,participant,epoch,payload,bytes,status,created) VALUES(?,?,?,?,?,'pending',?)",
                           (sample_id, current["participant"], current["epoch"], encrypted, len(encrypted), time.time()))
        except Exception:
            self.store.metric("research_capture_failed")
            log.warning("Research capture failed; request data omitted")

    def cancel_pending(self, participant):
        with self.db() as db:
            db.execute("UPDATE samples SET status='delete_pending',payload=NULL,bytes=0 WHERE participant=? AND status='pending' AND used=0", (participant,))

    def delete_unused(self, participant, archive=None):
        with self.db() as db:
            rows = list(db.execute("SELECT id,object_key FROM samples WHERE participant=? AND used=0 AND status IN ('pending','archived')", (participant,)))
            for row in rows:
                # Durable tombstone: an upload already in flight must not lose its
                # compensating delete, even if the operator process restarts.
                db.execute("UPDATE samples SET status='delete_pending',payload=NULL,bytes=0 WHERE id=?", (row["id"],))
                db.execute("DELETE FROM reviews WHERE id=?", (row["id"],))
            # Invalidate exports referencing deleted samples; manifests are private control records.
            for dataset in list(db.execute("SELECT id,samples,path FROM datasets WHERE used=0")):
                if set(json.loads(dataset["samples"])) & {row["id"] for row in rows}:
                    path = Path(dataset["path"])
                    path.unlink(missing_ok=True)
                    path.with_suffix(path.suffix+".manifest.json").unlink(missing_ok=True)
                    db.execute("DELETE FROM datasets WHERE id=?", (dataset["id"],))
        return len(rows)

    def flush(self, archive):
        with self.db() as db:
            rows = list(db.execute("SELECT * FROM samples WHERE status='pending' LIMIT 100"))
            tombstones = list(db.execute("SELECT * FROM samples WHERE status='delete_pending' LIMIT 100"))
        for row in tombstones:
            try:
                archive.delete(row["object_key"] or f"research/{row['participant']}/{row['id']}.aesgcm")
                with self.db() as db:
                    db.execute("DELETE FROM samples WHERE id=? AND status='delete_pending'", (row["id"],))
            except Exception:
                self.store.metric("research_delete_failed")
        for row in rows:
            try:
                with self.store.db() as control:
                    account = control.execute("SELECT * FROM accounts WHERE participant=?", (row["participant"],)).fetchone()
                if not account or not account["research"] or account["epoch"] != row["epoch"]:
                    self.cancel_pending(row["participant"])
                    continue
                key = f"research/{row['participant']}/{row['id']}.aesgcm"
                # Record the object name before network IO so a failed compensating
                # delete remains recoverable after preference changes or process crashes.
                with self.db() as db:
                    if not db.execute("UPDATE samples SET object_key=? WHERE id=? AND status='pending'", (key, row["id"])).rowcount:
                        continue
                archive.put(key, row["payload"])
                # If preference changed during upload, remove that object instead of retaining it.
                current = self.store.account(account["id"])
                if not current or not current["research"] or current["epoch"] != row["epoch"]:
                    archive.delete(key)
                    self.cancel_pending(row["participant"])
                    continue
                with self.db() as db:
                    updated = db.execute("UPDATE samples SET payload=NULL,bytes=0,status='archived',object_key=? WHERE id=? AND status='pending'", (key, row["id"])).rowcount
                if not updated:
                    archive.delete(key)
            except Exception:
                self.store.metric("research_archive_failed")
                log.warning("Research archive unavailable; encrypted queue retained")

    def review(self, sample_id, label, cleaned=None):
        if label not in {"accepted", "rejected", "failure_example"}:
            raise ValueError("Invalid review label")
        cleaned = cleaned or {}
        if set(cleaned) - {"task_summary", "rationale", "evidence_summary"}:
            raise ValueError("Unsupported reviewed fields")
        if any(not isinstance(v, str) or len(v) > 32000 or PII.search(v) for v in cleaned.values()):
            raise ValueError("Reviewed text contains a disallowed value")
        with self.db() as db:
            if not db.execute("SELECT 1 FROM samples WHERE id=? AND status IN ('pending','archived')", (sample_id,)).fetchone():
                raise ValueError("Unknown sample")
            db.execute("UPDATE samples SET label=? WHERE id=?", (label, sample_id))
            db.execute("INSERT OR REPLACE INTO reviews VALUES(?,?)", (sample_id, self.seal(cleaned)))

    def export(self, destination: Path, archive=None):
        import tempfile
        dataset_id, ids = uuid.uuid4().hex, []
        destination.parent.mkdir(parents=True, exist_ok=True)
        manifest_path = destination.with_suffix(destination.suffix + ".manifest.json")
        created, temporary = [], None
        try:
            with self.db() as db:
                # Serialize deletion/review/training-state changes with publishing.
                db.execute("BEGIN IMMEDIATE")
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=destination.parent, delete=False) as out:
                    temporary = Path(out.name)
                    for row in db.execute("SELECT * FROM samples WHERE status IN ('pending','archived') AND label IN ('accepted','failure_example') ORDER BY created,id"):
                        blob = row["payload"] or (archive.get(row["object_key"]) if archive else None)
                        if blob is None:
                            raise ValueError("Archive access required")
                        data = self.unseal(blob)
                        data.pop("quarantine", None)
                        review = db.execute("SELECT payload FROM reviews WHERE id=?", (row["id"],)).fetchone()
                        if review:
                            data["reviewed_text"] = self.unseal(review[0])
                        data.update(dataset_version=dataset_id, review_label=row["label"])
                        out.write(json.dumps(data, ensure_ascii=False, allow_nan=False) + "\n")
                        ids.append(row["id"])
                manifest = {"dataset_version": dataset_id, "samples": ids,
                            "sha256": hashlib.sha256(temporary.read_bytes()).hexdigest(),
                            "note": "Not marked as trained; use mark-used when training consumes this version."}
                os.link(temporary, destination)  # atomic publication, never overwrite
                created.append(destination)
                with manifest_path.open("x", encoding="utf-8", newline="\n") as output:
                    created.append(manifest_path)
                    manifest_path.chmod(0o600)
                    output.write(json.dumps(manifest, indent=2))
                db.execute("INSERT INTO datasets VALUES(?,?,0,?)", (dataset_id, json.dumps(ids), str(destination.resolve())))
            return manifest
        except BaseException:
            for path in created:
                path.unlink(missing_ok=True)
            raise
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)

    def mark_used(self, dataset_id):
        with self.db() as db:
            row = db.execute("SELECT samples FROM datasets WHERE id=?", (dataset_id,)).fetchone()
            if not row:
                raise ValueError("Unknown or invalidated dataset")
            db.execute("UPDATE datasets SET used=1 WHERE id=?", (dataset_id,))
            db.executemany("UPDATE samples SET used=1 WHERE id=?", [(x,) for x in json.loads(row[0])])


class COSArchive:
    def __init__(self, settings):
        from qcloud_cos import CosConfig, CosS3Client
        if settings.cos_region != "ap-singapore" or not settings.cos_bucket:
            raise ValueError("Configure a Singapore COS bucket")
        self.bucket = settings.cos_bucket
        self.client = CosS3Client(CosConfig(Region=settings.cos_region,
            SecretId=os.environ["HOSTED_COS_SECRET_ID"], SecretKey=os.environ["HOSTED_COS_SECRET_KEY"],
            Scheme="https", Timeout=15))

    def put(self, key, body):
        self.client.put_object(Bucket=self.bucket, Key=key, Body=body)

    def get(self, key):
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].get_raw_stream().read()

    def delete(self, key):
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def prune_backups(self, days):
        marker = ""
        cutoff = time.time()-days*86400
        while True:
            listing = self.client.list_objects(Bucket=self.bucket, Prefix="backups/", Marker=marker)
            for item in listing.get("Contents", []):
                stamp = item["Key"].rsplit("/", 1)[-1].split(".", 1)[0]
                if stamp.isdigit() and int(stamp) < cutoff:
                    self.delete(item["Key"])
            if str(listing.get("IsTruncated", "false")).lower() != "true":
                break
            marker = listing["NextMarker"]
