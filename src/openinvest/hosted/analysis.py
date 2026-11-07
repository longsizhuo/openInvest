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
    from openinvest.core.decision_evidence import load_context
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
        context = load_context(root()/"memory", result["context_id"], canonical)
        facts = {"context_id": context["context_id"], "inputs": context["inputs"]}
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


def submit(call, symbol, transcript, provider, model, analysis_id, *, reasoning_review_json=None):
    from openinvest.core.runner.coordinator import _COMMITTEE_SECTION_RE
    from openinvest.core.decision_evidence import load_context
    from openinvest.services.symbol_map import resolve_symbol
    from openinvest.core.portfolio_manager import PortfolioManager
    canonical = resolve_symbol(symbol, PortfolioManager().strategy.get("target_assets", []))
    if not analysis_id or len(analysis_id) != 32:
        raise ValueError("analysis_id from prepare_committee is required")
    signature = hashlib.sha256(json.dumps([canonical, transcript, provider, model]).encode()).hexdigest()
    peer_signature=hashlib.sha256(reasoning_review_json.encode()).hexdigest() if reasoning_review_json is not None else None
    with _lock:
        with db() as connection:
            row = connection.execute("SELECT * FROM analyses WHERE id=?", (analysis_id,)).fetchone()
            if not row or row["symbol"] != canonical:
                raise ValueError("Unknown analysis for this symbol")
            if row["state"] == "done":
                receipt = json.loads(row["result"])
                if receipt["signature"] != signature:
                    raise ValueError("analysis_id already submitted with different arguments")
                if peer_signature is not None and receipt.get("peer_signature")!=peer_signature:
                    raise ValueError("analysis_id already finalized with a different reasoning review")
                response = receipt["response"]
                from openinvest.core.decision_evidence import current_decision
                valid = current_decision(root()/"memory", {"decision_id": response.get("evidence_id", "")}, canonical)
                return {**response, "replayed": True, "response_type": "historical_receipt", "current_advice_valid": valid}
            if row["state"] != "prepared":
                raise ValueError("Analysis unavailable or outcome unknown; inspect the ledger")
            draft=json.loads(row["result"]) if row["result"] else None
            if draft is not None and draft.get("signature")!=signature:
                raise ValueError("analysis_id draft already contains a different proposal")
            if reasoning_review_json is not None and not (draft and draft.get("state")=="awaiting_verification"):
                raise ValueError("Submit the original proposal first to obtain its bound verification request")
            if time.time()-row["created"] > 21600 or fingerprint() != row["fingerprint"]:
                connection.execute("UPDATE analyses SET state='expired' WHERE id=?", (analysis_id,))
                invalid = True
            else:
                invalid = False
        if invalid:
            raise ValueError("Prepared inputs expired or ledger changed; prepare again")
        facts = json.loads(row["facts"])
        try:
            current = load_context(root()/"memory", facts["context_id"], canonical)
            if current["inputs"] != facts["inputs"]:
                raise ValueError("Prepared evidence changed")
        except (ValueError, KeyError, OSError) as e:
            with db() as connection:
                connection.execute("UPDATE analyses SET state='expired' WHERE id=?", (analysis_id,))
            raise ValueError("Prepared evidence is unavailable; prepare again") from e
        from openinvest.core.committee import parse_cio_memo
        parts = _COMMITTEE_SECTION_RE.split(transcript)
        sections = {parts[i].strip(): parts[i+1].strip() for i in range(1, len(parts)-1, 2)}
        cio = sections.get("CIO", transcript if not sections else "")
        if not transcript.strip() or parse_cio_memo(cio, worker_brief=transcript).get("verdict") == "UNCLEAR":
            raise ValueError("CIO verdict is missing; correct the transcript and resubmit")
        if current.get("declared_model") not in (None, "unknown", model.strip()):
            raise ValueError("Model changed after prepare; correct the model or cancel and prepare again")
        if any(not sections.get(role) for role in current["inputs"].get("required_roles", [])):
            raise ValueError("Required committee roles are missing; complete the prepared protocol")
        with db() as connection:
            connection.execute("UPDATE analyses SET state='running' WHERE id=?", (analysis_id,))
        # After validation, ANY failure can follow a ledger side effect. Preserve
        # the uncertain receipt; never turn a post-write ValueError into replay.
        extra={"reasoning_review_json":reasoning_review_json} if reasoning_review_json is not None else {}
        result = call(canonical, transcript, provider, model, context_id=facts["context_id"],**extra)
        result = {**result, "analysis_id": analysis_id}
        if "saved" in result:
            saved = Path(result["saved"])
            result["saved"] = str(saved.relative_to(root()))
        with db() as connection:
            waiting=result.get("status")=="verification_required"
            connection.execute("UPDATE analyses SET state=?,result=? WHERE id=?",
                               ("prepared" if waiting else "done",json.dumps({"signature": signature,
                                "peer_signature":peer_signature,"state":"awaiting_verification" if waiting else "done",
                                "response": result}, default=str), analysis_id))
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
