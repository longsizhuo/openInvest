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
            "fetch_failed": bool(frame.attrs.get("yf_fetch_failed", False)),
            "source_observation": _plain(frame.attrs.get("source_observation"))}


def enforce_input_health(verdict: dict, health: dict) -> None:
    reasons = []
    if health.get("price_fetch_failed") is True or health.get("has_df") is False:
        reasons.append("market_input_unavailable")
    if (health.get('market_source_observation') or {}).get('history_basis_qualified') is False:
        reasons.append('market_history_basis_unqualified')
    if health.get("macro_data_empty") is True:
        reasons.append("macro_input_unavailable")
    if "portfolio_summary" in health.get("empty_inputs", []):
        reasons.append("portfolio_input_unavailable")
    if health.get('worker_unavailable') and 'worker_input_unavailable' not in reasons:
        reasons.append('worker_input_unavailable')
    if verdict.get('decision_status') == 'DATA_UNTRUSTED' and not reasons:
        reasons.append(verdict.get('hold_reason') or 'unqualified_input_or_output')
    if reasons:
        verdict.setdefault("_original_verdict", verdict.get("verdict"))
        verdict.setdefault("_original_alloc", verdict.get("alloc_cny"))
        no_proposal=verdict.get("proposal_available") is False
        if not no_proposal:
            verdict.setdefault('raw_market_verdict', (verdict.get('_raw_proposal') or {}).get('verdict', verdict.get('_original_verdict')))
        verdict.update(verdict="UNCLEAR", alloc_cny=0, confidence=0.0,
                       decision_status="DATA_UNTRUSTED", hold_reason=verdict.get("hold_reason") if no_proposal else "incomplete_required_inputs")
        health.update(decision_status="DATA_UNTRUSTED", input_errors=reasons)
    if not reasons and health.get('model_output_errors'):
        verdict.setdefault('raw_market_verdict',(verdict.get('_raw_proposal') or {}).get('verdict',verdict.get('verdict')))
        verdict.setdefault('_original_alloc',verdict.get('alloc_cny'))
        verdict.update(alloc_cny=0,decision_status='REVIEW_REQUIRED',qualification_reason='model_output_incomplete_or_invalid')
        health['decision_status']='REVIEW_REQUIRED'
    account=health.get('ledger_evidence') or {}
    target=(account.get('holdings') or {}).get(health.get('target_symbol'),{})
    quantity=target.get('current_quantity','client_recorded')
    if (not reasons and verdict.get('verdict') in {'TRIM','SELL'}
            and (account.get('status')=='unavailable' or quantity in {'estimated_or_proxy_unreconciled','broker_observed_domain_unresolved'}
                 or str(quantity).startswith('balance_observation_'))):
        verdict.setdefault('_original_alloc',verdict.get('alloc_cny'))
        verdict.update(alloc_cny=0,decision_status='REVIEW_REQUIRED',account_execution_unverified=True)
        health.update(decision_status='REVIEW_REQUIRED',account_reconciliation_required=True)
    if (not reasons and verdict.get('verdict') in {'ACCUMULATE','BUY'}
            and (account.get('status')=='unavailable' or account.get('allocation_cash_eligible') is False)):
        verdict.setdefault('_original_alloc',verdict.get('alloc_cny'))
        verdict.update(alloc_cny=0,decision_status='REVIEW_REQUIRED',account_execution_unverified=True)
        health.update(decision_status='REVIEW_REQUIRED',account_reconciliation_required=True)


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
    for relative in ("memory/user.md", "memory/strategy.md", "memory/portfolio.md","memory/portfolio_history.jsonl",
                     "memory/.state/account_observation_heads.json",
                     "memory/.state/config_overrides.json", "integrity/eligibility.json", "integrity/exclusions.jsonl"):
        path = memory_root / relative.removeprefix("memory/") if relative.startswith("memory/") else owner / relative
        if path.exists():
            st = path.stat()
            if relative in {'integrity/eligibility.json', 'integrity/exclusions.jsonl'}:
                from openinvest.core.evidence_eligibility import context_authority_snapshot
                files[relative] = {'context_authority_sha256': digest(context_authority_snapshot(owner))}
            else:
                files[relative] = (st.st_ino, st.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
        else:
            if relative in {'integrity/eligibility.json', 'integrity/exclusions.jsonl'}:
                from openinvest.core.evidence_eligibility import context_authority_snapshot
                files[relative] = {'context_authority_sha256': digest(context_authority_snapshot(owner))}
            else:
                files[relative] = None
    estimates=memory_root/'.evidence/tracking_estimates'
    files['tracking_estimate_receipts']={path.name:hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(estimates.glob('*.json'))}
    for kind in ('broker_fee_reports','broker_fee_applications'):
        files[kind]={path.name:hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted((memory_root/'.evidence'/kind).glob('*.json'))}
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


def _validate_context_references(memory_root: Path, payload: dict) -> None:
    """Current decisions recheck the exact referenced sources and sealed plans."""
    owner = memory_root.parent if memory_root.name == 'memory' else memory_root
    if not payload.get('source_event_lineage_captured', True):
        raise ValueError('source_event_lineage_missing')
    if not payload.get('source_plan_lineage_captured', True):
        raise ValueError('source_plan_lineage_missing')
    versions = payload.get('source_event_versions', {})
    if not isinstance(versions, dict):raise ValueError('invalid_source_event_versions')
    if versions:
        from openinvest.db.event_store import EventStore
        from openinvest.services.source_reporting import event_version_binding
        path = owner/'db/events.db'
        if not path.exists():raise ValueError('referenced_source_database_missing')
        index = EventStore(str(path), readonly=True)
        try:
            for eid, version in versions.items():
                if not event_version_binding(index, eid, version)['allowed']:
                    raise ValueError('referenced_source_changed_or_unqualified')
        finally:index.conn.close()
    expected_states = payload.get('source_plan_states')
    if expected_states is not None and expected_states != _capture_plan_states(memory_root, payload):
        raise ValueError('referenced_plan_changed_after_prepare')
    refs = payload.get('inputs', {}).get('source_plan_refs', [])
    if not isinstance(refs, list) or len(refs)>16:raise ValueError('invalid_source_plan_refs')
    if refs:
        from openinvest.core.memory_store import MemoryStore
        from openinvest.services.review_plans import plan_qualification
        store=MemoryStore(memory_root)
        for ref in refs:
            ident=ref.get('plan_id')
            if not re.fullmatch(r'[a-f0-9]{32}', ident or ''):raise ValueError('invalid_source_plan_id')
            record=json.loads((memory_root/'.evidence/review_plans'/f'{ident}.json').read_text())
            if record.get('sha256')!=ref.get('artifact_sha256') or digest(record['payload'])!=record['sha256']:
                raise ValueError('referenced_plan_seal_changed')
            plan=record['payload']
            if not plan_qualification(store,payload['symbol'],plan,purpose=plan['purpose'])['allowed']:
                raise ValueError('referenced_plan_parent_unqualified')


def _capture_plan_states(memory_root: Path, payload: dict) -> dict:
    refs = payload.get('inputs', {}).get('source_plan_refs', [])
    if not isinstance(refs,list) or len(refs)>16:raise ValueError('invalid_source_plan_refs')
    if not refs:return {}
    from openinvest.core.memory_store import MemoryStore
    from openinvest.services.review_plans import _key, _plan_payload, canonical_spec
    store=MemoryStore(memory_root);result={}
    for ref in refs:
        ident=ref.get('plan_id')
        if not re.fullmatch(r'[a-f0-9]{32}', ident or ''):raise ValueError('invalid_source_plan_id')
        envelope=json.loads((memory_root/'.evidence/review_plans'/f'{ident}.json').read_text())
        if envelope.get('sha256')!=ref.get('artifact_sha256') or digest(envelope['payload'])!=envelope['sha256']:
            raise ValueError('referenced_plan_seal_changed')
        original=envelope['payload'];current=store.state_get(_key(payload['symbol'],original['purpose']))
        if (not current or current.get('plan_id')!=ident or current.get('state')=='retired_audit_only'
                or digest(canonical_spec(current['spec']))!=digest(canonical_spec(original['spec']))):
            raise ValueError('referenced_plan_changed_during_preparation')
        material={**_plan_payload(current),'spec':canonical_spec(current['spec']),'state':current.get('state')}
        result[ident]=digest(material)
    return result


def _referenced_audit_records(owner: Path, payload: dict) -> dict:
    """Preserve existence/negative lineage of used automatic audit records."""
    from openinvest.core.evidence_eligibility import Eligibility, source_artifact_ids, _inert_auto_audit
    gate=Eligibility(owner);gate.excluded_ids()
    path=owner/'integrity/eligibility.json'
    registry=json.loads(path.read_text()).get('artifacts',{}) if path.exists() else {}
    versions=payload.get('source_event_versions', {}); ids=set()
    if versions:
        import sqlite3
        database=owner/'db/events.db'
        connection=sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True)
        try:
            for eid, version in versions.items():
                ids.add('event:'+eid)
                row=connection.execute('SELECT payload_json FROM event_versions WHERE event_id=? AND id=?',(eid,version)).fetchone()
                if not row:raise ValueError('referenced_event_version_missing')
                event=json.loads(row[0])
                from openinvest.services.source_reporting import metadata
                for source in event.get('sources',[]):
                    ids.update(source_artifact_ids(metadata(source) if 'provenance_json' in source else source.get('raw_meta') or {}))
                support=event.get('reporting_support') or {}
                if support.get('observation_id'):ids.add('backend_observation:'+support['observation_id'])
        finally:connection.close()
    # Snapshot dependencies of the actually retained source, not all unrelated
    # registry rows. Additional negative ancestry is checked by the full closure.
    pending=list(ids);seen=set()
    while pending:
        ident=pending.pop()
        if ident in seen:continue
        seen.add(ident);value=registry.get(ident)
        if value and not ident.startswith('event:'):pending.extend(value.get('parents',[]))
    return {ident:registry[ident] for ident in seen if ident in registry and _inert_auto_audit(ident,registry[ident])}


def _check_referenced_audits(owner: Path, expected: dict) -> None:
    path=owner/'integrity/eligibility.json'
    state=json.loads(path.read_text()).get('artifacts',{}) if path.exists() else {}
    for ident, before in expected.items():
        now=state.get(ident)
        if not isinstance(now,dict) or not set(before.get('parents',[]))<=set(now.get('parents',[])):
            raise ValueError('referenced_audit_lineage_removed')
        if not ident.startswith('event:') and now.get('sha256')!=before.get('sha256'):
            raise ValueError('referenced_source_snapshot_changed')


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
    from openinvest.services.ledger_evidence import store_source_status
    from openinvest.core.memory_store import MemoryStore
    ledger_evidence=store_source_status(MemoryStore(Path(memory_root)))
    from openinvest.core.evidence_eligibility import context_authority_snapshot
    payload = {
        "context_id": context_id, "symbol": symbol, "prepared_at": utc_now().isoformat(),
        "code_version": code_version(), "config": config, "config_sha256": digest(config),
        "declared_model": model, "inputs": _plain(inputs),
        "source_event_versions":dict(getattr(inputs.get("event_brief"),"event_versions",{})),
        "source_event_lineage_captured":hasattr(inputs.get("event_brief"),"event_versions") or not inputs.get("event_brief"),
        "source_plan_lineage_captured":'=== EXISTING REVIEW PLAN ===' not in str(inputs.get('portfolio_summary') or '') or bool(inputs.get('source_plan_refs')),
        "account_evidence_fingerprint":ledger_evidence.get('content_sha256'),
        "origin": "synthetic" if os.getenv("INVEST_TEST_ISOLATED") == "1" else "prospective", "availability_evidence": "captured_before_inference",
        "historical_vintage_reconstructed": False,
        "eligibility_version": _eligibility_version(owner),  # raw forensic/legacy version; not the context authority test
        "context_authority": context_authority_snapshot(owner),
        "documents": {name: (Path(memory_root) / f"{name}.md").read_text()
                      for name in ("user", "strategy", "portfolio")
                      if (Path(memory_root) / f"{name}.md").exists()},
    }
    # Capture may include explicitly unbound reports for analysis/audit. It
    # never grants current advice: load_context enforces all references before
    # a decision can be persisted/reused as qualified.
    payload['source_plan_states'] = _capture_plan_states(Path(memory_root), payload)
    payload['referenced_audit_records'] = _referenced_audit_records(owner, payload)
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
    if 'context_authority' in payload:
        from openinvest.core.evidence_eligibility import context_authority_snapshot
        if payload['context_authority'] != context_authority_snapshot(owner):
            raise ValueError('evidence authority changed after prepare; prepare again')
        _check_referenced_audits(owner, payload.get('referenced_audit_records', {}))
    elif payload.get("eligibility_version") != _eligibility_version(owner):
        raise ValueError("evidence eligibility changed after prepare; prepare again")
    _validate_context_references(Path(memory_root), payload)
    if (owner / "integrity/restore_pending.json").exists():
        raise ValueError("restored financial state requires reconciliation")
    for name, content in payload.get("documents", {}).items():
        if (Path(memory_root) / f"{name}.md").read_text() != content:
            raise ValueError("portfolio or user constraints changed after prepare; prepare again")
    from openinvest.services.ledger_evidence import store_source_status
    from openinvest.core.memory_store import MemoryStore
    ledger_evidence=store_source_status(MemoryStore(Path(memory_root)))
    if (payload.get('account_evidence_fingerprint') is None or ledger_evidence.get('content_sha256') is None
            or payload.get('account_evidence_fingerprint')!=ledger_evidence.get('content_sha256')):
        raise ValueError('account evidence changed after prepare; prepare again')
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
