"""Durable per-ledger analysis receipts and immutable preparation evidence."""
from contextlib import contextmanager
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import sqlite3
import threading
import time
import uuid

_lock = threading.RLock()


def root():
    from openinvest.paths import INVEST_ROOT
    return INVEST_ROOT


def rule_version():
    from openinvest.core.runner import coordinator
    from openinvest.core.committee import cio_parse
    from openinvest.core.runner import intervention
    digest = hashlib.sha256()
    for module in (coordinator, cio_parse, intervention):
        digest.update(Path(module.__file__).read_bytes())
    return digest.hexdigest()


def fingerprint():
    digest = hashlib.sha256(rule_version().encode())
    for path in [root()/"config.yaml", *[root()/"memory"/f"{name}.md" for name in ("user", "strategy", "portfolio")]]:
        digest.update(path.name.encode())
        digest.update(path.read_bytes() if path.exists() else b"")
    return digest.hexdigest()


@contextmanager
def db():
    path = root()/"db"/"hosted_analyses.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE IF NOT EXISTS analyses(id TEXT PRIMARY KEY,symbol TEXT,created REAL,state TEXT,fingerprint TEXT,facts TEXT,brief TEXT,result TEXT)")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def cancel():
    with _lock, db() as connection:
        if connection.execute("SELECT 1 FROM analyses WHERE state='running'").fetchone():
            raise ValueError("An active submission cannot be cancelled")
        connection.execute("UPDATE analyses SET state='cancelled' WHERE state='prepared'")


def prepare(call, symbol):
    from openinvest.core.runner.coordinator import _brief_facts_path
    with _lock:
        with db() as connection:
            connection.execute("UPDATE analyses SET state='expired' WHERE state='prepared' AND created<?", (time.time()-21600,))
            if connection.execute("SELECT 1 FROM analyses WHERE state IN ('prepared','running')").fetchone():
                raise ValueError("An analysis is already pending; submit it or cancel it on the account page")
        before = fingerprint()
        result = call(symbol)
        if result.get("status") == "error":
            return result
        if fingerprint() != before:
            raise ValueError("Ledger changed during preparation; prepare again")
        canonical = result["asset"]["symbol"]
        facts = json.loads(_brief_facts_path(canonical).read_text())
        analysis_id = uuid.uuid4().hex
        from openinvest.core.portfolio_manager import PortfolioManager
        pm = PortfolioManager()
        stored = {**result, "_hosted_snapshot": {"input_snapshot": {"holdings": pm.portfolio.get("holdings", []), "cash": pm.cash,
                  "strategy": pm.strategy}, "facts": facts, "rule_version": rule_version(), "snapshot_hash": before,
                  "backend_version": version("openinvest"), "prompt_version": hashlib.sha256(json.dumps(result.get("prompts", {}), sort_keys=True).encode()).hexdigest()}}
        with db() as connection:
            connection.execute("INSERT INTO analyses VALUES(?,?,?,'prepared',?,?,?,NULL)",
                               (analysis_id, canonical, time.time(), before, json.dumps(facts), json.dumps(stored, default=str)))
        return {**result, "analysis_id": analysis_id, "analysis_expires_in_seconds": 21600}


def submit(call, symbol, transcript, provider, model, analysis_id):
    from openinvest.core.runner.coordinator import _load_brief_facts, _COMMITTEE_SECTION_RE
    from openinvest.services.symbol_map import resolve_symbol
    from openinvest.core.portfolio_manager import PortfolioManager
    canonical = resolve_symbol(symbol, PortfolioManager().strategy.get("target_assets", []))
    if not analysis_id or len(analysis_id) != 32:
        raise ValueError("analysis_id from prepare_committee is required")
    signature = hashlib.sha256(json.dumps([canonical, transcript, provider, model]).encode()).hexdigest()
    with _lock:
        with db() as connection:
            row = connection.execute("SELECT * FROM analyses WHERE id=?", (analysis_id,)).fetchone()
            if not row or row["symbol"] != canonical:
                raise ValueError("Unknown analysis for this symbol")
            if row["state"] == "done":
                receipt = json.loads(row["result"])
                if receipt["signature"] != signature:
                    raise ValueError("analysis_id already submitted with different arguments")
                return receipt["response"]
            if row["state"] != "prepared":
                raise ValueError("Analysis unavailable or outcome unknown; inspect the ledger")
            if time.time()-row["created"] > 21600 or fingerprint() != row["fingerprint"]:
                connection.execute("UPDATE analyses SET state='expired' WHERE id=?", (analysis_id,))
                invalid = True
            else:
                invalid = False
        if invalid:
            raise ValueError("Prepared inputs expired or ledger changed; prepare again")
        facts = json.loads(row["facts"])
        current = _load_brief_facts(canonical)
        if current != facts:
            raise ValueError("Prepared evidence is unavailable; cancel and prepare again")
        from openinvest.core.committee import parse_cio_memo
        parts = _COMMITTEE_SECTION_RE.split(transcript)
        sections = {parts[i].strip(): parts[i+1].strip() for i in range(1, len(parts)-1, 2)}
        cio = sections.get("CIO", transcript if not sections else "")
        if not transcript.strip() or parse_cio_memo(cio, worker_brief=transcript).get("verdict") == "UNCLEAR":
            raise ValueError("CIO verdict is missing; correct the transcript and resubmit")
        with db() as connection:
            connection.execute("UPDATE analyses SET state='running' WHERE id=?", (analysis_id,))
        # After validation, ANY failure can follow a ledger side effect. Preserve
        # the uncertain receipt; never turn a post-write ValueError into replay.
        result = call(canonical, transcript, provider, model)
        result = {**result, "analysis_id": analysis_id}
        if "saved" in result:
            saved = Path(result["saved"])
            result["saved"] = str(saved.relative_to(root()))
        with db() as connection:
            connection.execute("UPDATE analyses SET state='done',result=? WHERE id=?",
                               (json.dumps({"signature": signature, "response": result}, default=str), analysis_id))
        return result


def evidence_for(analysis_id):
    import re
    if not re.fullmatch(r"[0-9a-f]{32}", analysis_id):
        raise ValueError("Invalid analysis")
    with db() as connection:
        row = connection.execute("SELECT brief FROM analyses WHERE id=?", (analysis_id,)).fetchone()
    if not row:
        raise ValueError("Unknown analysis")
    data = json.loads(row[0])
    snapshot = data.pop("_hosted_snapshot", {})
    snapshot["prepared_text"] = {key: value for key, value in data.items() if isinstance(value, str)}
    return snapshot
