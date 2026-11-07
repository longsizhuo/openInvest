"""Immutable proposal drafts and idempotent reasoning-review receipts."""
from __future__ import annotations

from datetime import datetime,timezone
import json
from pathlib import Path

from openinvest.core.memory_store import _atomic_write_text,_file_lock
from openinvest.core.evidence_eligibility import digest
from openinvest.core.committee.reasoning_quality import POLICY,SYSTEM_PROMPT,alternative_contract,call_review,parse_review


def _save(path,payload):
    path.parent.mkdir(parents=True,exist_ok=True)
    _atomic_write_text(path,json.dumps({"payload":payload,"sha256":digest(payload)},ensure_ascii=False))


def review_once(request,*,memory_root,agent_factory,ask,asset,external_raw=None,backend_allowed=True):
    """No failed logical review regenerates CIO or chooses a different action."""
    root=Path(memory_root) if memory_root is not None else None
    if root is not None:
        from openinvest.core.memory_store import MemoryStore
        from openinvest.paths import INVEST_ROOT
        if MemoryStore(root).data_root.resolve()!=INVEST_ROOT.resolve():backend_allowed=False
    directory=root/".evidence/reasoning" if root is not None else None
    key=request["request_sha256"]
    if digest({name:value for name,value in request.items() if name!="request_sha256"})!=key:
        raise ValueError("reasoning_request_integrity_failure")
    if directory is None:
        if external_raw is not None:raise ValueError("external_verification_requires_an_issued_request")
        return _review(request,agent_factory=agent_factory,ask=ask,asset=asset,external_raw=external_raw,backend_allowed=backend_allowed)
    path=directory/"receipts"/(key+".json")
    with _file_lock(path):
        attempts=0
        external_allowed=False
        draft=directory/"drafts"/(key+".json")
        if not draft.exists():_save(draft,{"request":request,"recorded_at":datetime.now(timezone.utc).isoformat(),"allowed_uses":["audit"]})
        else:
            original=json.loads(draft.read_text())
            if original["sha256"]!=digest(original["payload"]) or original["payload"]["request"]!=request:
                raise ValueError("reasoning_draft_integrity_failure")
        if path.exists():
            envelope=json.loads(path.read_text());payload=envelope["payload"]
            if envelope["sha256"]!=digest(payload) or payload["request_sha256"]!=key:raise ValueError("reasoning_receipt_integrity_failure")
            external_allowed=payload.get("external_allowed") is True and not backend_allowed and payload.get("state")=="awaiting_external"
            if external_raw is not None and not external_allowed:
                raise ValueError("external_verification_was_not_issued_for_this_request")
            if payload["state"]=="done":return {**payload["review"],"replayed":True}
            attempts=payload.get("attempts",0)
            if payload["state"]=="started":
                return {"status":"unavailable","policy":POLICY,"request_sha256":key,"reason":"previous_verification_outcome_unknown"}
            if external_raw is None and not backend_allowed:return {**payload["review"],"replayed":True}
            if external_raw is None and attempts>=2:return {**payload["review"],"replayed":True,"retry_limit_reached":True}
        elif external_raw is not None:
            raise ValueError("external_verification_requires_an_issued_request")
        _save(path,{"request_sha256":key,"state":"started","attempts":attempts+1})
        review=_review(request,agent_factory=agent_factory,ask=ask,asset=asset,external_raw=external_raw,backend_allowed=backend_allowed)
        _save(directory/"attempts"/key/(str(attempts+1)+".json"),{"request_sha256":key,"review":review,
              "recorded_at":datetime.now(timezone.utc).isoformat()})
        state="awaiting_external" if review["status"] in ("unavailable","invalid_response") else "done"
        _save(path,{"request_sha256":key,"state":state,"review":review,"attempts":attempts+1,
                    "external_allowed":not backend_allowed and review["status"] in ("unavailable","invalid_response")})
        return review


def _review(request,*,agent_factory,ask,asset,external_raw,backend_allowed):
    contract=alternative_contract(request["proposal"].get("fields") or {})
    if not contract["valid"]:
        return {"status":"failed","policy":POLICY,"request_sha256":request["request_sha256"],
                "contract_error":contract["reason"],"source_qualification_granted":False,"trade_authorized":False}
    if external_raw is not None:return {**parse_review(external_raw,request),"origin":"host_reported","raw_response":external_raw}
    if not backend_allowed:
        return {"status":"unavailable","policy":POLICY,"request_sha256":request["request_sha256"],"reason":"external_verifier_required"}
    try:return {**call_review(request,agent_factory=agent_factory,ask=ask,asset=asset),"origin":"backend_api"}
    except Exception as exc:
        return {"status":"unavailable","policy":POLICY,"request_sha256":request["request_sha256"],
                "origin":"backend_api","reason":type(exc).__name__}


def external_request(request):
    return {"status":"verification_required","current_advice_valid":False,
            "request_sha256":request["request_sha256"],"system_prompt":SYSTEM_PROMPT,
            "user_prompt":json.dumps(request,ensure_ascii=False,sort_keys=True),
            "next_step":"核验已保存的原始提案并提交 reasoning_review_json；不要重跑同一套委员会来追求通过。"}
