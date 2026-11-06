"""Operator-controlled research journal; automatic publication defaults to off."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re

from openinvest.core.evidence_eligibility import digest
from openinvest.core.memory_store import _atomic_write_text, _file_lock
from openinvest.core.evaluation_protocol import release_gate, validate_protocol

DEFAULT_POLICY = {"schema": 1, "auto_publish": False, "parameter_allowlist": {},
                  "approved_runtime": {}, "approved_protocols": [], "minimum_release_interval_days": 7}


class ResearchJournal:
    def __init__(self, root: Path):
        self.root = Path(root) / "integrity"

    def register(self, candidate: dict, protocol: dict) -> str:
        validate_protocol(protocol)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", str(candidate.get("id", ""))):
            raise ValueError("invalid candidate id")
        payload = {"candidate": candidate, "protocol": protocol, "protocol_sha256": digest(protocol)}
        directory = self.root / "candidates"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{candidate['id']}.json"
        with _file_lock(path):
            if path.exists():
                old = json.loads(path.read_text())
                if old["payload"] != payload:
                    raise ValueError("candidate already registered with different inputs")
                return old["sha256"]
            record = {"registered_at": datetime.now(timezone.utc).isoformat(),
                      "sha256": digest(payload), "payload": payload}
            _atomic_write_text(path, json.dumps(record, ensure_ascii=False, indent=2))
        return record["sha256"]

    def reserve_final_set(self, dataset_hash: str, candidate_id: str, *, final_start: str, final_end: str) -> None:
        """A failed trial consumes the final set too; retries of the same id are idempotent."""
        if not re.fullmatch(r"[a-f0-9]{64}", dataset_hash):
            raise ValueError("invalid final dataset digest")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", candidate_id):
            raise ValueError("invalid candidate id")
        candidate = self.root / "candidates" / f"{candidate_id}.json"
        if not candidate.exists():
            raise ValueError("register every candidate before evaluation")
        registered = json.loads(candidate.read_text())
        if registered["sha256"] != digest(registered["payload"]):
            raise ValueError("candidate registration integrity failed")
        from openinvest.core.evaluation_protocol import timestamp
        start, end = timestamp(final_start), timestamp(final_end)
        if end <= start:
            raise ValueError("invalid final window")
        path = self.root / "final_set_uses.json"
        with _file_lock(path):
            uses = json.loads(path.read_text()) if path.exists() else {"datasets": {}, "windows": []}
            if dataset_hash in uses["datasets"] and uses["datasets"][dataset_hash] != candidate_id:
                raise ValueError("final set already inspected by another candidate; obtain a fresh holdout")
            for window in uses["windows"]:
                if window["candidate"] != candidate_id and start <= timestamp(window["end"]) and end >= timestamp(window["start"]):
                    raise ValueError("overlapping final window already inspected; re-slicing is not a fresh holdout")
            uses["datasets"][dataset_hash] = candidate_id
            entry = {"candidate": candidate_id, "start": final_start, "end": final_end}
            if entry not in uses["windows"]:
                uses["windows"].append(entry)
            _atomic_write_text(path, json.dumps(uses, sort_keys=True))

    def final_set_reserved(self, dataset_hash: str, candidate_id: str, protocol: dict) -> bool:
        path = self.root / "final_set_uses.json"
        if not path.exists():
            return False
        uses = json.loads(path.read_text())
        return (uses.get("datasets", {}).get(dataset_hash) == candidate_id and
                {"candidate": candidate_id, "start": protocol["final_start"], "end": protocol["final_end"]} in uses.get("windows", []))


def publication_blockers(root: Path, bundle: dict, evaluation: dict, reviews: list[dict]) -> list[str]:
    path = Path(root) / "integrity/release_policy.json"
    policy = json.loads(path.read_text()) if path.exists() else DEFAULT_POLICY
    blockers = release_gate(bundle, policy, evaluation, reviews)
    if not bundle.get("parameter_changes"):
        blockers.append("no_parameter_change")
    for key in ("code", "features", "model", "prompts", "rules"):
        if policy.get("approved_runtime", {}).get(key) != bundle.get(key):
            blockers.append(f"runtime_not_preapproved:{key}")
    if bundle.get("protocol") not in policy.get("approved_protocols", []):
        blockers.append("protocol_not_preapproved")
    if evaluation.get("data_sha256") != bundle.get("data"):
        blockers.append("evaluation_dataset_mismatch")
    if evaluation.get("parameters_sha256") != bundle.get("parameters"):
        blockers.append("evaluation_candidate_mismatch")
    if bundle.get("parameters") != digest(bundle.get("parameter_changes", {})):
        blockers.append("parameter_digest_mismatch")
    # Only market research parameters can be in this automatic path. Broker
    # permissions, money caps, source validity and scoring definitions cannot.
    for key in bundle.get("parameter_changes", {}):
        if key.split(".")[0] not in {"regime", "path", "valuation", "macro_buckets", "market_rules"}:
            blockers.append(f"protected_parameter:{key}")
        from dataclasses import asdict
        from openinvest.core.config.tunable import TunableConfig
        section, _, name = key.partition(".")
        default = asdict(TunableConfig()).get(section, {}).get(name)
        if isinstance(default, bool) or not isinstance(default, (int, float)):
            blockers.append(f"unknown_or_non_numeric_parameter:{key}")
        elif type(default) is int and type(bundle["parameter_changes"][key]) is not int:
            blockers.append(f"integer_parameter_required:{key}")
    return sorted(set(blockers))


def publish_parameters(store, bundle: dict, evaluation: dict, reviews: list[dict], *, now=None) -> dict:
    with _file_lock(store.data_root / "integrity/governance"):
        return _publish_parameters_locked(store, bundle, evaluation, reviews, now=now)


def _authority_snapshot(root: Path, bundle_id: str) -> dict:
    import hashlib
    paths = [root / name for name in ("release_policy.json", "eligibility.json", "exclusions.jsonl")]
    paths += [root / "approvals" / bundle_id / f"{role}.json" for role in ("engineering", "data", "statistics")]
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None for path in paths}


def _publish_parameters_locked(store, bundle: dict, evaluation: dict, reviews: list[dict], *, now=None) -> dict:
    """Version-bound parameter publication; never publishes code, models or trades."""
    from openinvest.core.decision_evidence import code_version, config_snapshot, _eligibility_version
    authority = store.data_root / "integrity"
    for value in (evaluation.get("candidate_id", ""), bundle.get("rollback_target", "")):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", str(value)):
            return {"status": "blocked", "reasons": ["invalid_artifact_identifier"]}
    bundle_id = digest(bundle)
    authority_snapshot = _authority_snapshot(authority, bundle_id)
    # A release request cannot certify its own reviews or evaluation. These are
    # separately stored operator-reviewed artifacts bound to the exact bundle.
    trusted_reviews = []
    for role in ("engineering", "data", "statistics"):
        path = authority / "approvals" / bundle_id / f"{role}.json"
        if path.exists():
            trusted_reviews.append(json.loads(path.read_text()))
    reviews = trusted_reviews
    blockers = publication_blockers(store.data_root, bundle, evaluation, reviews)
    actual_config = config_snapshot()
    if bundle.get("baseline_config_sha256") != digest(actual_config):
        blockers.append("baseline_configuration_changed")
    if bundle.get("consent") != digest(_eligibility_version(store.data_root)):
        blockers.append("evidence_or_exclusions_changed")
    evaluation_path = authority / "evaluations" / f"{digest(evaluation)}.json"
    if not evaluation_path.exists() or json.loads(evaluation_path.read_text()) != evaluation:
        blockers.append("evaluation_artifact_not_registered")
    candidate_path = authority / "candidates" / f"{evaluation.get('candidate_id', '')}.json"
    if not candidate_path.exists():
        blockers.append("candidate_not_registered")
    else:
        registration = json.loads(candidate_path.read_text())
        protocol = registration["payload"]["protocol"]
        registered_candidate = registration["payload"]["candidate"]
        if (digest(registered_candidate) != evaluation.get("candidate_sha256")
                or digest(registered_candidate.get("parameters", {})) != bundle.get("parameters")
                or registered_candidate.get("model_version") != protocol["model_version"]):
            blockers.append("registered_candidate_changed")
        if not ResearchJournal(store.data_root).final_set_reserved(evaluation.get("data_sha256"), evaluation["candidate_id"], protocol):
            blockers.append("final_set_was_not_reserved")
        if (digest(registration["payload"]) != registration["sha256"]
                or digest(protocol) != bundle.get("protocol")
                or datetime.fromisoformat(registration["registered_at"]) >= datetime.fromisoformat(protocol["final_start"])):
            blockers.append("final_protocol_not_preregistered")
    if not (authority / "rollback_targets" / f"{bundle.get('rollback_target', '')}.json").exists():
        blockers.append("rollback_target_not_registered")
    if bundle.get("code") != code_version():
        blockers.append("runtime_code_changed")
    if blockers:
        return {"status": "blocked", "reasons": blockers}
    now = now or datetime.now(timezone.utc)
    root = store.data_root / "integrity"
    marker = root / "last_parameter_release.json"
    with _file_lock(root / "parameter_publication.lock"):
        current_overrides = store.state_get("config_overrides") or {}
        previous = current_overrides.get("_learning_release") or (json.loads(marker.read_text()) if marker.exists() else None)
        release_id = digest(bundle)
        if previous and previous.get("release_id") == release_id:
            return {"status": "already_published", "release_id": release_id}
        policy = json.loads((root / "release_policy.json").read_text())
        interval = max(7, int(policy.get("minimum_release_interval_days", 7)))
        if previous and (now-datetime.fromisoformat(previous["published_at"])).total_seconds() < interval * 86400:
            return {"status": "blocked", "reasons": ["release_frequency_limit"]}

        def change(current):
            if _authority_snapshot(authority, bundle_id) != authority_snapshot:
                raise ValueError("publication authorization or exclusions changed during validation")
            current = current or {}
            if digest(current) != bundle.get("baseline_overrides_sha256"):
                raise ValueError("configuration changed since the reviewed baseline")
            release = {"release_id": release_id, "prepared_at": now.isoformat(),
                       "previous_overrides": {k: v for k, v in current.items() if k != "_learning_release"}, "bundle": bundle,
                       "rollback_rule": "restore decision parameters only; retain current exclusions, consent and real ledger"}
            target = root / "releases" / f"{release_id}.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                _atomic_write_text(target, json.dumps(release, ensure_ascii=False, indent=2))
            updated = json.loads(json.dumps(current))
            for key, value in bundle["parameter_changes"].items():
                section, name = key.split(".", 1)
                updated.setdefault(section, {})[name] = value
            from openinvest.core.config._loader import _build_tunable_from_dict
            effective = json.loads(json.dumps(actual_config["tunable"]))
            for key, value in bundle["parameter_changes"].items():
                section, name = key.split(".", 1)
                effective[section][name] = value
            _build_tunable_from_dict(effective)  # joint validation before the atomic write
            updated["_learning_release"] = {"release_id": release_id, "published_at": now.isoformat()}
            return updated, release

        release = store.state_update("config_overrides", change)
        release["published_at"] = now.isoformat()
        _atomic_write_text(root / "releases" / f"{release_id}.json", json.dumps(release, ensure_ascii=False, indent=2))
        _atomic_write_text(marker, json.dumps(release, ensure_ascii=False, indent=2))
        return {"status": "published", "release_id": release_id}
