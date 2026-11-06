"""Weekly research gate status. It cannot invent qualifications or approve itself."""
from __future__ import annotations

import json

from openinvest.core.memory_store import MemoryStore
from openinvest.core.learning_control import publish_parameters, DEFAULT_POLICY


def run() -> dict:
    store = MemoryStore()
    root = store.data_root / "integrity"
    policy_file = root / "release_policy.json"
    policy = json.loads(policy_file.read_text()) if policy_file.exists() else DEFAULT_POLICY
    if not policy.get("auto_publish"):
        return {"status": "gated", "reason": "automatic_publication_disabled", "parameter_allowlist": policy.get("parameter_allowlist", {})}
    results = []
    for path in sorted((root / "release_requests").glob("*.json")):
        request = json.loads(path.read_text())
        results.append(publish_parameters(store, request["bundle"], request["evaluation"], request["reviews"]))
    return {"status": "ok", "reviewed": len(results), "results": results}
