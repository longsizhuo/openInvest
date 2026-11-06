"""Operator audit/quarantine entry. Never changes cash, holdings, trades or execution state."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, help="Previously captured forensic manifest; required for quarantine")
    parser.add_argument("command", choices=["status", "rules", "quarantine"])
    args = parser.parse_args()
    root = args.home.resolve()
    if not root.is_dir():
        parser.error("existing data home required")
    os.environ["INVEST_HOME"] = str(root)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from openinvest.core.evidence_eligibility import Eligibility, digest
    from openinvest.core.learning_control import DEFAULT_POLICY
    from openinvest.core.memory_store import MemoryStore, _atomic_write_text, append_jsonl
    if args.command == "status":
        from openinvest.services.integrity_status import status
        print(json.dumps(status(), ensure_ascii=False, indent=2)); return
    if args.command == "rules":
        from openinvest.core.rule_audit import inventory
        print(json.dumps(inventory(), ensure_ascii=False, indent=2)); return

    if not args.baseline or not args.baseline.is_file():
        parser.error("quarantine requires an existing --baseline forensic manifest")
    baseline_hash = hashlib.sha256(args.baseline.read_bytes()).hexdigest()

    directory = root / "integrity"
    directory.mkdir(mode=0o700, exist_ok=True)
    registry = directory / "eligibility.json"
    exclusions = directory / "exclusions.jsonl"
    if registry.exists() and not exclusions.exists():
        raise ValueError("Existing registry has no exclusion ledger; recover exclusions before proceeding")
    if not registry.exists():
        _atomic_write_text(registry, '{"schema":1,"artifacts":{}}')
    if not exclusions.exists():
        _atomic_write_text(exclusions, "")
    if not (directory / "release_policy.json").exists():
        _atomic_write_text(directory / "release_policy.json", json.dumps(DEFAULT_POLICY, indent=2))
    policy = Eligibility(root)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    archive = directory / "quarantine" / stamp
    archive.mkdir(parents=True, mode=0o700)
    records = []
    db_path = root / "db/insights.db"
    if db_path.exists():
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                for row in connection.execute("SELECT * FROM insights").fetchall():
                    row = dict(row)
                    if policy.allows_insight(row["slug"], row, row.get("body") or ""):
                        continue
                    target = archive / f"insight-{digest(row)}.json"
                    _atomic_write_text(target, json.dumps(row, ensure_ascii=False, indent=2))
                    records.append({"kind": "insight_row", "id": row["slug"], "snapshot": target.name,
                                    "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                                    "source_validity": "unverified", "judgment_outcome": "not_assessed",
                                    "allowed_uses": ["audit"]})
                    connection.execute("DELETE FROM insights WHERE slug=?", (row["slug"],))
        finally:
            connection.close()
    store = MemoryStore(root / "memory")
    for path in sorted((store.root / "insights").glob("*.md")):
        if store.read(f"insights/{path.stem}") is not None:
            continue
        target = archive / "insights" / path.name
        target.parent.mkdir(exist_ok=True)
        shutil.copy2(path, target)
        records.append({"kind": "insight_md", "snapshot": str(target.relative_to(archive)),
                        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                        "source_validity": "unverified", "judgment_outcome": "not_assessed", "allowed_uses": ["audit"]})
        path.unlink()
    for relative in ("memory/DREAMS.md", "memory/.dreams/confidence_lookup.json", "memory/.dreams/candidates.json", "memory/.dreams/short-term-recall.json",
                     "db/chroma.sqlite3", "db/chroma.sqlite3-wal", "db/chroma.sqlite3-shm"):
        path = root / relative
        if path.is_file():
            target = archive / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            records.append({"kind": "derived_archive", "snapshot": relative,
                            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "allowed_uses": ["audit"]})
            path.unlink()
    index = store.root / "MEMORY.md"
    if index.exists():
        previous = index.read_text()
        shutil.copy2(index, archive / "MEMORY.before.md")
        content = re.sub(r"\n## 长期洞察 \(Deep Sleep.*?(?=\n## |\Z)", "", previous, flags=re.S)
        note = "\n\n## 洞察资格\n长期洞察以通过资格核验的工具/API 返回为准。历史派生内容已保留在 integrity/quarantine 供审计，不作为当前建议依据。\n"
        if "## 洞察资格" not in content:
            content += note
        _atomic_write_text(index, content)
    report = {"performed_at": datetime.now(timezone.utc).isoformat(), "records": records,
              "baseline": str(args.baseline.resolve()), "baseline_sha256": baseline_hash,
              "financial_data_changed": False, "raw_decisions_deleted": False,
              "rule": "Quarantine is based on missing use qualification, never on a prediction losing money."}
    _atomic_write_text(archive / "manifest.json", json.dumps(report, ensure_ascii=False, indent=2))
    append_jsonl(directory / "operations.jsonl", {"operation": "quarantine", "at": report["performed_at"], "manifest": str(archive / "manifest.json")})
    print(json.dumps({"status": "quarantined", "records": len(records), "manifest": str(archive / "manifest.json"), "financial_data_changed": False}))


if __name__ == "__main__":
    main()
