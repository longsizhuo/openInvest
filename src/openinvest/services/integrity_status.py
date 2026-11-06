"""Read-only operational evidence status for CLI/MCP callers."""
from __future__ import annotations

import json
import sqlite3

from openinvest.core.memory_store import MemoryStore
from openinvest.core.evidence_eligibility import Eligibility
from openinvest.core.learning_control import DEFAULT_POLICY
from openinvest.services.decision_quality import findings


def status() -> dict:
    store = MemoryStore()
    root = store.data_root
    counts = {"total": 0, "advice_qualified": 0}
    path = root / "db/insights.db"
    if path.exists():
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            for row in connection.execute("SELECT * FROM insights"):
                counts["total"] += 1
                counts["advice_qualified"] += int(Eligibility(root).allows_insight(row["slug"], dict(row), row["body"] or ""))
        finally:
            connection.close()
    policy_path = root / "integrity/release_policy.json"
    policy = json.loads(policy_path.read_text()) if policy_path.exists() else DEFAULT_POLICY
    from openinvest.core.config import load_config
    warnings = findings(store.root, hold_window=load_config().scheduler.decision_hold_review_days)
    initialized = all((store.root / f"{name}.md").exists() for name in ("user", "strategy", "portfolio"))
    return {"status": "DATA_UNTRUSTED" if (root / "integrity/restore_pending.json").exists() else "ready_with_eligibility_gates" if initialized else "needs_setup",
            "insights": counts, "automatic_learning_publication": bool(policy.get("auto_publish")),
            "learnable_parameters": sorted(policy.get("parameter_allowlist", {})),
            "quality_alerts": [f["text"] for f in warnings],
            "note": "来源资格、预测结果、允许用途分别记录；样本不足不会自动发布，也不会自动改成买卖建议。"}
