"""Content-bound eligibility for derived evidence, shared by every reader.

Missing provenance is not a wrong prediction. Unknown, synthetic and revoked
artifacts remain inspectable for audit, but cannot silently enter advice or
learning. The registry is administrative state, never an LLM output field.
"""
from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Mapping

USES = frozenset({"advice", "learn", "evaluate", "publish"})
_MODEL_SCOPE = ContextVar("evidence_model_scope", default=None)


@contextmanager
def model_scope(model_era: str):
    token = _MODEL_SCOPE.set(model_era.strip().lower())
    try:
        yield
    finally:
        _MODEL_SCOPE.reset(token)


def runtime_scope() -> dict:
    from openinvest.core.decision_evidence import code_version, config_snapshot
    from openinvest.utils.llm import get_llm_config_safe, reasoning_by_agent
    model = _MODEL_SCOPE.get()
    if model is None:
        model = "agent:unknown" if reasoning_by_agent() else f"direct:{get_llm_config_safe()[2] or 'unknown'}"
    return {"code_version": code_version(), "config_sha256": digest(config_snapshot()), "model_era": model.lower()}


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def insight_payload(slug: str, metadata: Mapping[str, Any], body: str) -> dict:
    """Canonical content shared by MD and SQLite; score/timestamps are not provenance."""
    return {
        "kind": "insight", "slug": slug, "asset": metadata.get("asset"),
        "title": metadata.get("title", body.strip().splitlines()[0].lstrip("# ") if body.strip() else slug),
        "body": body.strip(), "hit_rate": metadata.get("hit_rate"),
        "sample_count": metadata.get("sample_count", metadata.get("count")),
        "source_score": metadata.get("source_score", metadata.get("score")),
    }


def candidate_payload(candidate: Mapping[str, Any]) -> dict:
    return {k: v for k, v in candidate.items() if k not in {"score", "legacy_score_gate_passed"}}


class Eligibility:
    def __init__(self, root: Path, *, consent_checker=None):
        self.root = Path(root)
        self.directory = self.root / "integrity"
        self.consent_checker = consent_checker

    def excluded_ids(self) -> set[str]:
        """Exclusions also apply to raw SQL/vector recall and survive DB restore."""
        path = self.directory / "exclusions.jsonl"
        if not path.exists() and not (self.directory / "eligibility.json").exists():
            return set()
        try:
            rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
            excluded = {row["artifact_id"] for row in rows}
            if any(not isinstance(key, str) or not key for key in excluded):
                raise ValueError("invalid exclusion id")
            registry_path = self.directory / "eligibility.json"
            artifacts = json.loads(registry_path.read_text()).get("artifacts", {}) if registry_path.exists() else {}
            changed = True
            while changed:
                additions = {key for key, value in artifacts.items()
                             if key not in excluded and any(p in excluded for p in value.get("parents", []))}
                changed = bool(additions)
                excluded |= additions
            return excluded
        except (OSError, ValueError, TypeError, KeyError) as e:
            raise ValueError("DATA_UNTRUSTED: exclusion ledger unavailable") from e

    def _state(self) -> tuple[dict, set[str]]:
        """Reload revocations on every read, including after an old DB restore."""
        registry = json.loads((self.directory / "eligibility.json").read_text())
        if registry.get("schema") != 1 or not isinstance(registry.get("artifacts"), dict):
            raise ValueError("invalid eligibility registry")
        exclusions = self.directory / "exclusions.jsonl"
        revoked = set()
        # Registry explicitly requires its exclusion ledger; missing is not empty.
        if not exclusions.exists():
            raise ValueError("missing exclusion ledger")
        for line in exclusions.read_text().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row.get("artifact_id"), str) or not row["artifact_id"]:
                raise ValueError("invalid exclusion record")
            revoked.add(row["artifact_id"])
        return registry["artifacts"], revoked

    def allows(self, artifact_id: str, payload: Any, use: str) -> bool:
        if use == "audit":
            return True
        if use not in USES:
            return False
        try:
            artifacts, revoked = self._state()

            def check(key: str, expected_hash: str | None, visiting: set[str]) -> bool:
                if key in revoked or key in visiting:
                    return False
                record = artifacts.get(key)
                if not isinstance(record, dict):
                    return False
                if (record.get("source_validity") != "valid"
                        or record.get("origin") not in ("prospective", "verified_point_in_time", "derived")
                        or use not in record.get("allowed_uses", [])
                        or not record.get("review_id") or not record.get("reviewed_at")
                        or record.get("deleted") or record.get("research_participation") == "revoked"
                        or not isinstance(record.get("sha256"), str)
                        or len(record["sha256"]) != 64):
                    return False
                if record.get("hosted") and record.get("research_participation") != "active":
                    return False
                if record.get("hosted") and (self.consent_checker is None or not self.consent_checker(record)):
                    return False
                if expected_hash is not None and record["sha256"] != expected_hash:
                    return False
                if use == "advice" and (key.startswith("insight:") or key == "derived:confidence_lookup" or record.get("scope")):
                    if record.get("scope") != runtime_scope():
                        return False
                # Outcomes deliberately do not select eligibility: valid wrong
                # predictions are essential negative samples.
                parents = record.get("parents")
                if not isinstance(parents, list):
                    return False
                hashes = record.get("parent_hashes", {})
                if parents and (not isinstance(hashes, dict) or set(hashes) != set(parents)):
                    return False
                if record.get("origin") == "derived" and not parents:
                    return False
                return all(isinstance(parent, str) and check(parent, hashes.get(parent), visiting | {key})
                           for parent in parents)

            return check(artifact_id, digest(payload), set())
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            return False

    def allows_insight(self, slug: str, metadata: Mapping[str, Any], body: str,
                       use: str = "advice") -> bool:
        return self.allows(f"insight:{slug}", insight_payload(slug, metadata, body), use)

    def revoke(self, artifact_id: str, reason: str) -> None:
        """Append-only exclusions live outside the replaceable DB/memory trees."""
        from datetime import datetime, timezone
        from openinvest.core.memory_store import append_jsonl, _file_lock
        self.directory.mkdir(parents=True, exist_ok=True)
        with _file_lock(self.directory / "governance"):
            append_jsonl(self.directory / "exclusions.jsonl", {
                "artifact_id": artifact_id, "reason": reason,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            })

    def derive(self, artifact_id: str, payload: Any, parents: dict[str, Any]) -> None:
        """Publish a deterministic projection only from explicitly reviewed parents."""
        from datetime import datetime, timezone
        from openinvest.core.memory_store import _file_lock, _atomic_write_text
        if not parents or not all(self.allows(key, value, "publish") for key, value in parents.items()):
            raise ValueError("unqualified publication parents")
        path = self.directory / "eligibility.json"
        with _file_lock(self.directory / "governance"), _file_lock(path):
            artifacts, revoked = self._state()
            if artifact_id in revoked or any(key in revoked for key in parents):
                raise ValueError("publication was revoked")
            if any(artifacts[key]["sha256"] != digest(value) for key, value in parents.items()):
                raise ValueError("publication parent changed during validation")
            uses = set.intersection(*(set(artifacts[key]["allowed_uses"]) for key in parents))
            artifacts[artifact_id] = {
                "sha256": digest(payload), "source_validity": "valid", "origin": "derived",
                "judgment_outcome": "not_inferred_from_profit", "allowed_uses": sorted(uses),
                "parents": list(parents), "review_id": "derived:" + digest([artifacts[key]["review_id"] for key in parents]),
                "parent_hashes": {key: digest(value) for key, value in parents.items()},
                "reviewed_at": datetime.now(timezone.utc).isoformat(),
            }
            scopes = [artifacts[key].get("scope") for key in parents]
            if scopes and scopes[0] and all(scope == scopes[0] for scope in scopes):
                artifacts[artifact_id]["scope"] = scopes[0]
            _atomic_write_text(path, json.dumps({"schema": 1, "artifacts": artifacts}, ensure_ascii=False, indent=2))
