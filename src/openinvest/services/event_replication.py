"""One-way public-news replication, preserving local data and source exclusions."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

from openinvest.core.evidence_eligibility import Eligibility
from openinvest.db.event_store import EventStore
from openinvest.services.source_reporting import event_payload,source_payload


def publication_payload(event,sources):
    """A public hostname never authorizes publication of an owner's summary."""
    return {"event":event_payload(event),"sources":sorted((source_payload(item) for item in sources),key=lambda item:json.dumps(item,sort_keys=True))}


def publication_id(event,sources):
    from openinvest.core.evidence_eligibility import digest
    return "public_event:"+digest(publication_payload(event,sources))


def sync_public_events(source_root: Path, target_root: Path, allowed_hosts: set[str]) -> dict:
    source_root, target_root = Path(source_root).resolve(), Path(target_root).resolve()
    if source_root == target_root:
        raise ValueError("replication source and destination must differ")
    reader=EventStore(str(source_root / "db/events.db"),readonly=True)
    source = reader.conn
    target = EventStore(str(target_root / "db/events.db"))
    source_policy, target_policy = Eligibility(source_root), Eligibility(target_root)
    namespace = hashlib.sha256(str(source_root).encode()).hexdigest()[:16]
    imported = excluded = pending = 0
    try:
        source_exclusions = source_policy.excluded_ids()
        target_exclusions = target_policy.excluded_ids()
        known = {r[0] for r in target.conn.execute("SELECT event_id FROM events")}
        for item in target.conn.execute("SELECT provenance_json FROM sources").fetchall():
            meta=json.loads(item[0] or "{}")
            publication=meta.get("upstream_publication")
            if meta.get("upstream_instance")==namespace and publication in source_exclusions:
                key="replica_publication:"+namespace+":"+publication
                if key not in target_exclusions:
                    target_policy.revoke(key,"upstream publication withdrawn; independently acquired target evidence is unaffected")
                    target_exclusions.add(key);excluded+=1
        def replicated(eid):
            local = target.conn.execute("SELECT * FROM events WHERE event_id=?", (eid,)).fetchone()
            if local is None:
                return False
            from openinvest.services.source_reporting import reporting_use
            if any(reporting_use(target_root,item).get("kind") in ("captured_api_response","controlled_adapter_report")
                   and reporting_use(target_root,item)["allowed"] for item in target.get_sources(eid,purpose="audit")):
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
            current=reader.get_event_with_sources(eid)
            if current and current.get("event_type") in ("price_alert","price_action"):continue
            sources = []
            for item in current["sources"] if current else []:
                url = urlsplit(item["url"])
                host = (url.hostname or "").lower()
                if (url.scheme in ("http", "https") and not url.username and not url.password
                        and any(host == allowed or host.endswith("." + allowed) for allowed in allowed_hosts)):
                    sources.append(dict(item))
            if not sources:
                continue
            if not source_policy.allows(publication_id(current,sources),publication_payload(current,sources),"publish"):
                pending+=1
                continue
            approved=publication_payload(current,sources)
            event={**approved["event"],"ingested_by":f"public_replica:{namespace}"}
            # No foreign vectors: an unversioned embedding is not portable.
            # Target as-of history starts at actual replication time, not the
            # source instance's original created_at.
            local = target.get_event(eid, purpose="audit") if eid in known else None
            differs = local is None or any(local.get(key) != event[key] for key in ("one_line_claim", "event_type", "ts", "stance", "severity", "affected_symbols", "entities"))
            if differs:
                target.upsert_event(event,revision=True)
            for item in approved["sources"]:
                target.add_source(eid, src_name=item["src_name"], url=item["url"],
                                  title=item["title"], snippet=item["snippet"], fetched_at=item["fetched_at"],
                                  published_at=item.get("published_at"),raw_text=item.get("raw_text", ""),
                                  provenance={"source_origin":"published_replica_pending_target_verification",
                                              "upstream_publication":publication_id(current,sources),"upstream_instance":namespace})
            imported += int(differs)
        return {"imported_public_events": imported, "propagated_exclusions": excluded,
                "publication_review_required":pending,"replica_use":"audit_until_target_acquisition_or_content_bound_review",
                "source_mutated": False, "foreign_vectors_copied": False}
    finally:
        target.conn.close()
        source.close()
