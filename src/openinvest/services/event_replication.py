"""One-way public-news replication, preserving local data and source exclusions."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit

from openinvest.core.evidence_eligibility import Eligibility
from openinvest.db.event_store import EventStore


def sync_public_events(source_root: Path, target_root: Path, allowed_hosts: set[str]) -> dict:
    source_root, target_root = Path(source_root).resolve(), Path(target_root).resolve()
    if source_root == target_root:
        raise ValueError("replication source and destination must differ")
    source = sqlite3.connect((source_root / "db/events.db").as_uri() + "?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    target = EventStore(str(target_root / "db/events.db"))
    source_policy, target_policy = Eligibility(source_root), Eligibility(target_root)
    namespace = hashlib.sha256(str(source_root).encode()).hexdigest()[:16]
    imported = excluded = 0
    try:
        source_exclusions = source_policy.excluded_ids()
        target_exclusions = target_policy.excluded_ids()
        known = {r[0] for r in target.conn.execute("SELECT event_id FROM events")}
        def replicated(eid):
            local = target.conn.execute("SELECT * FROM events WHERE event_id=?", (eid,)).fetchone()
            if local is None:
                return False
            if local["ingested_by"] == f"public_replica:{namespace}":
                return True
            upstream = source.execute("SELECT * FROM events WHERE event_id=?", (eid,)).fetchone()
            return bool(upstream and all(local[k] == upstream[k] for k in
                        ("created_at", "one_line_claim", "ingested_by", "event_type")))
        for key in source_exclusions:
            if key.startswith("event:") and key[6:] in known and key not in target_exclusions and replicated(key[6:]):
                target_policy.revoke(key, "upstream event excluded; retain forensic data, do not recall")
                excluded += 1
        for row in source.execute("SELECT * FROM events ORDER BY id"):
            eid = row["event_id"]
            # These records may contain an owner's private alarm line or last
            # committee verdict. They are not public news for another instance.
            if row["event_type"] in ("price_alert", "price_action"):
                if eid in known and f"event:{eid}" not in target_exclusions:
                    if replicated(eid):
                        target_policy.revoke(f"event:{eid}", "legacy replicated owner-specific event")
                        excluded += 1
                continue
            if (eid in known and not replicated(eid)) or f"event:{eid}" in source_exclusions or f"event:{eid}" in target_exclusions:
                continue
            sources = []
            for item in source.execute("SELECT * FROM sources WHERE event_id=?", (eid,)):
                url = urlsplit(item["url"])
                host = (url.hostname or "").lower()
                if (url.scheme in ("http", "https") and not url.username and not url.password
                        and any(host == allowed or host.endswith("." + allowed) for allowed in allowed_hosts)):
                    sources.append(dict(item))
            if not sources:
                continue
            event = {"event_id": eid, "one_line_claim": row["one_line_claim"], "event_type": row["event_type"],
                     "stance": row["stance"], "severity": {1:"low",2:"mid",3:"high"}.get(row["severity"], row["severity"]), "source_reliability": row["source_reliability"],
                     "ts": row["ts"], "entities": json.loads(row["entities_json"] or "[]"),
                     "affected_symbols": json.loads(row["affected_symbols_json"] or "[]"),
                     "ingested_by": f"public_replica:{namespace}"}
            # No foreign vectors: an unversioned embedding is not portable.
            # Target as-of history starts at actual replication time, not the
            # source instance's original created_at.
            local = target.get_event(eid, purpose="audit") if eid in known else None
            differs = local is None or any(local.get(key) != event[key] for key in ("stance", "severity", "affected_symbols", "entities"))
            if differs:
                target.upsert_event(event)
            for item in sources:
                target.add_source(eid, src_name=item["src_name"], url=item["url"],
                                  title=item["title"], snippet=item["snippet"], fetched_at=item["fetched_at"])
            imported += int(differs)
        return {"imported_public_events": imported, "propagated_exclusions": excluded,
                "source_mutated": False, "foreign_vectors_copied": False}
    finally:
        target.conn.close()
        source.close()
