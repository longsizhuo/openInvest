"""Immutable, request-bound inputs and decisions; historical gaps stay explicit."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import math
import tempfile
import uuid
from typing import Any

from openinvest.core.evidence_eligibility import digest


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def market_snapshot(frame) -> dict:
    columns = [name for name in ("Open", "High", "Low", "Close", "Volume") if name in frame.columns]
    return {"columns": columns, "index": [str(value) for value in frame.index],
            "values": _plain(frame[columns].values.tolist()),
            "observed_by_analysis_at": utc_now().isoformat(),
            "fetch_failed": bool(frame.attrs.get("yf_fetch_failed", False))}


def enforce_input_health(verdict: dict, health: dict) -> None:
    reasons = []
    if health.get("price_fetch_failed") is True or health.get("has_df") is False:
        reasons.append("market_input_unavailable")
    if health.get("macro_data_empty") is True:
        reasons.append("macro_input_unavailable")
    if "portfolio_summary" in health.get("empty_inputs", []):
        reasons.append("portfolio_input_unavailable")
    if reasons:
        verdict.setdefault("_original_verdict", verdict.get("verdict"))
        verdict.setdefault("_original_alloc", verdict.get("alloc_cny"))
        verdict.update(verdict="HOLD", alloc_cny=0, confidence=0.0,
                       decision_status="DATA_UNTRUSTED", hold_reason="incomplete_required_inputs")
        health.update(decision_status="DATA_UNTRUSTED", input_errors=reasons)


def advice_qualified(verdict: dict) -> bool:
    return (verdict.get("decision_status","READY") in ("READY","CONSTRAINED")
            and verdict.get("verdict") not in (None,"UNCLEAR")
            and (verdict.get("reasoning_review") or {}).get("status")=="passed")


def code_version() -> str:
    """Hash the actual installed code/prompts, including uncommitted local changes."""
    root = Path(__file__).resolve().parents[1]
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix in (".py", ".md", ".yaml", ".yml", ".json"):
            h.update(str(p.relative_to(root)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()


def config_snapshot() -> dict:
    from openinvest.core.config import load_config, get_locked
    from openinvest.utils.llm import get_llm_config_safe
    _, base_url, model, provider = get_llm_config_safe()
    return {"tunable": asdict(load_config(_force_reload=True)),
            "locked": [asdict(c) for c in get_locked()],
            "model_runtime": {"provider": provider, "model": model,
                              "endpoint_sha256": hashlib.sha256(str(base_url).encode()).hexdigest()},
            "legacy_env": {key: os.getenv(key) for key in (
                "INVEST_CIO_CONFIDENCE_CAP", "INVEST_ALLOC_AGGRESSIVENESS", "INVEST_CIO_THINKING",
                "INVEST_LLM_MAX_ATTEMPTS", "INVEST_LLM_BASE_DELAY", "INVEST_LLM_MAX_DELAY")}}


def preparation_state(memory_root: Path) -> dict:
    owner = memory_root.parent if memory_root.name == "memory" else memory_root
    from openinvest.core.evidence_eligibility import Eligibility
    Eligibility(owner).excluded_ids()
    files = {}
    for relative in ("memory/user.md", "memory/strategy.md", "memory/portfolio.md",
                     "memory/.state/config_overrides.json", "integrity/eligibility.json", "integrity/exclusions.jsonl"):
        path = memory_root / relative.removeprefix("memory/") if relative.startswith("memory/") else owner / relative
        if path.exists():
            st = path.stat()
            files[relative] = (st.st_ino, st.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
        else:
            files[relative] = None
    return {"files": files, "config": config_snapshot(), "code": code_version()}


def _write_once(path: Path, payload: dict) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _plain(payload)
    record = {"schema": 1, "sha256": digest(payload), "payload": payload}
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=path.parent, delete=False) as f:
            temporary = Path(f.name)
            json.dump(record, f, ensure_ascii=False, sort_keys=True, allow_nan=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.link(temporary, path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
    return record


def _plain(value):
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(v) for v in value]
    if hasattr(value, "item"):
        return _plain(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None  # explicit unavailable numeric input, never a fabricated value
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    raise TypeError(f"unsupported evidence value: {type(value).__name__}")


def prepare_context(memory_root: Path, symbol: str, inputs: dict, *, model: str = "unknown", expected_state: dict | None = None) -> dict:
    owner = memory_root.parent if memory_root.name == "memory" else memory_root
    if (owner / "integrity/restore_pending.json").exists():
        raise ValueError("DATA_UNTRUSTED: restored financial state requires reconciliation")
    if expected_state is not None and preparation_state(memory_root) != expected_state:
        raise ValueError("DATA_UNTRUSTED: inputs or eligibility changed during preparation; prepare again")
    from openinvest.services.agent_queue import pending_committee_items
    covered={r["request_id"] for r in inputs.get("trigger_requests",[])}
    if any(set(group["request_ids"])-covered for group in pending_committee_items(memory_root=memory_root) if group["symbol"]==symbol):
        raise ValueError("DATA_UNTRUSTED: pending risk causes are not covered by these inputs; prepare again")
    context_id = uuid.uuid4().hex
    config = config_snapshot()
    payload = {
        "context_id": context_id, "symbol": symbol, "prepared_at": utc_now().isoformat(),
        "code_version": code_version(), "config": config, "config_sha256": digest(config),
        "declared_model": model, "inputs": _plain(inputs),
        "origin": "synthetic" if os.getenv("INVEST_TEST_ISOLATED") == "1" else "prospective", "availability_evidence": "captured_before_inference",
        "historical_vintage_reconstructed": False,
        "eligibility_version": _eligibility_version(owner),
        "documents": {name: (Path(memory_root) / f"{name}.md").read_text()
                      for name in ("user", "strategy", "portfolio")
                      if (Path(memory_root) / f"{name}.md").exists()},
    }
    _write_once(Path(memory_root) / ".evidence/contexts" / f"{context_id}.json", payload)
    return payload


def _eligibility_version(owner: Path) -> dict:
    return {name: hashlib.sha256((owner / "integrity" / name).read_bytes()).hexdigest()
            if (owner / "integrity" / name).exists() else None
            for name in ("eligibility.json", "exclusions.jsonl")}


def load_context(memory_root: Path, context_id: str, symbol: str, *,
                 now: datetime | None = None, max_age: timedelta = timedelta(hours=6)) -> dict:
    if not re.fullmatch(r"[a-f0-9]{32}", context_id or ""):
        raise ValueError("missing or invalid context_id; prepare the committee first")
    path = Path(memory_root) / ".evidence/contexts" / f"{context_id}.json"
    record = json.loads(path.read_text())
    payload = record["payload"]
    if record.get("sha256") != digest(payload):
        raise ValueError("context integrity check failed")
    age = (now or utc_now()) - datetime.fromisoformat(payload["prepared_at"])
    if (payload["context_id"] != context_id or payload["symbol"] != symbol
            or age < timedelta(0) or age > max_age):
        raise ValueError("context expired or belongs to another request")
    if digest(payload["config"]) != digest(config_snapshot()):
        raise ValueError("configuration changed after prepare; prepare again")
    if payload["code_version"] != code_version():
        raise ValueError("decision code changed after prepare; prepare again")
    owner = memory_root.parent if memory_root.name == "memory" else memory_root
    if payload.get("eligibility_version") != _eligibility_version(owner):
        raise ValueError("evidence eligibility changed after prepare; prepare again")
    if (owner / "integrity/restore_pending.json").exists():
        raise ValueError("restored financial state requires reconciliation")
    for name, content in payload.get("documents", {}).items():
        if (Path(memory_root) / f"{name}.md").read_text() != content:
            raise ValueError("portfolio or user constraints changed after prepare; prepare again")
    from openinvest.services.agent_queue import pending_committee_items
    prepared=datetime.fromisoformat(payload["prepared_at"])
    for group in pending_committee_items(memory_root=memory_root):
        if group["symbol"]==symbol and any(datetime.fromisoformat(r["queued_at"])>prepared for r in group["requests"]):
            raise ValueError("new risk request arrived after prepare; prepare fresh inputs")
    return payload


def record_decision(memory_root: Path, symbol: str, raw: dict, final: dict,
                    health: dict, *, context_id: str | None = None,
                    historical: bool = False) -> dict:
    decision_id = uuid.uuid4().hex
    payload = {
        "decision_id": decision_id, "symbol": symbol, "recorded_at": utc_now().isoformat(),
        "origin": "synthetic" if os.getenv("INVEST_TEST_ISOLATED") == "1" else ("retrospective" if historical else "prospective"),
        "context_id": context_id, "raw_proposal": raw, "validated_proposal": final,
        "input_health": health, "code_version": code_version(),
        "config": config_snapshot(), "execution": "unknown",
        "judgment_outcome": "unmatured", "source_validity": "pending_review",
        "allowed_uses": ["audit"],
    }
    record = _write_once(Path(memory_root) / ".evidence/decisions" / f"{decision_id}.json", payload)
    return {"decision_id": decision_id, "evidence_sha256": record["sha256"],
            "context_id": context_id, "code_version": payload["code_version"]}


def current_decision(memory_root: Path, health: dict, symbol: str, *, verdict: dict | None = None,
                     expected_model: str | None = None, transcript: str | None = None,
                     allow_pending_requests: bool = False) -> bool:
    """A historical record is readable, but only bound current inputs qualify as a cache."""
    try:
        decision_id = health.get("decision_id", "")
        if not re.fullmatch(r"[a-f0-9]{32}", decision_id):
            return False
        record = json.loads((memory_root / ".evidence/decisions" / f"{decision_id}.json").read_text())
        payload = record["payload"]
        if record["sha256"] != digest(payload) or payload["symbol"] != symbol:
            return False
        final = payload["validated_proposal"]
        if not advice_qualified(final):return False
        input_health = payload.get("input_health") or {}
        if (final.get("decision_status") in ("DATA_UNTRUSTED","REVIEW_REQUIRED") or final.get("verdict") == "UNCLEAR"
                or input_health.get("decision_status") == "DATA_UNTRUSTED" or input_health.get("worker_unavailable")
                or input_health.get("input_errors") or input_health.get("cio_unclear")):
            return False
        sealed_text = json.loads((memory_root / ".evidence/transcripts" / f"{decision_id}.json").read_text())
        if digest(sealed_text["payload"]) != sealed_text["sha256"]:
            return False
        canonical_text = sealed_text["payload"]["text"]
        if (memory_root / ".evidence/transcripts" / f"{decision_id}.md").read_text() != canonical_text:
            return False
        if transcript is not None and transcript != canonical_text:
            return False
        if verdict is not None and "cio_memo" in verdict:
            from openinvest.core.decision_ledger import parse_committee_text
            if verdict["cio_memo"] != parse_committee_text(canonical_text).get("cio_memo"):
                return False
        context = load_context(memory_root, payload["context_id"], symbol)
        if not allow_pending_requests:
            from openinvest.services.agent_queue import pending_committee_items
            covered={r["request_id"] for r in context["inputs"].get("trigger_requests",[])}
            if any(set(group["request_ids"])-covered for group in pending_committee_items(memory_root=memory_root) if group["symbol"]==symbol):
                return False
        if expected_model and context.get("declared_model") != expected_model:
            return False
        if verdict is not None:
            for key in set(payload["validated_proposal"]) & set(verdict):
                actual, expected = verdict.get(key), payload["validated_proposal"].get(key)
                if key == "confidence" and isinstance(expected, (int,float)) and actual == round(expected, 2):
                    continue
                if actual != expected:
                    return False
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False
