"""Immutable proposal drafts and idempotent reasoning-review receipts."""
from __future__ import annotations

from datetime import datetime,timezone
import json
import re
from pathlib import Path
from os.path import basename

from openinvest.core.memory_store import _atomic_write_text,_file_lock
from openinvest.core.evidence_eligibility import digest
from openinvest.core.committee.reasoning_quality import POLICY,SYSTEM_PROMPT,alternative_contract,call_review,parse_review


def _save(path,payload):
    path.parent.mkdir(parents=True,exist_ok=True)
    _atomic_write_text(path,json.dumps({"payload":payload,"sha256":digest(payload)},ensure_ascii=False))


def preserve_submission(memory_root,context_id,symbol,transcript,provider,model):
    """Preserve the exact caller bytes before issuing an external review."""
    if not isinstance(context_id,str) or not re.fullmatch(r"[a-f0-9]{32}",context_id):raise ValueError("invalid_context_id")
    path=Path(memory_root)/".evidence/submissions"/(basename(context_id)+".json")
    content={"context_id":context_id,"symbol":symbol,"transcript":transcript,"provider":provider,"model":model}
    with _file_lock(path):
        if path.exists():
            record=json.loads(path.read_text())
            if record.get("sha256")!=digest(record.get("payload")) or record["payload"]!=content:
                raise ValueError("original_submission_integrity_failure")
        else:_save(path,content)


def submitted_review(memory_root,context_id):
    """Read one ledger's bound draft without acknowledgement or inference."""
    if not re.fullmatch(r"[a-f0-9]{32}",context_id or ""):raise ValueError("invalid_context_id")
    root=Path(memory_root)
    path=root/".evidence/submissions"/(context_id+".json")
    if not path.exists():
        return {"status":"unrecoverable_original_missing","context_id":context_id,"current_advice_valid":False,
                "resume_allowed":False,"next_step":"原始全文缺失，仅保留审计；不得从摘要或记忆重建原文。新材料到达时再准备新上下文。"}
    record=json.loads(path.read_text());original=record["payload"]
    if record.get("sha256")!=digest(original) or original.get("context_id")!=context_id:
        raise ValueError("original_submission_integrity_failure")
    receipt=json.loads((root/".evidence/receipts"/(context_id+".json")).read_text())
    expected=digest({name:original[name] for name in ("symbol","transcript","provider","model")})
    if receipt.get("signature")!=expected:raise ValueError("submission_receipt_binding_failure")
    response=receipt.get("response") or {}
    key=response.get("request_sha256") or ((response.get("verdict") or {}).get("reasoning_review") or {}).get("request_sha256")
    if not re.fullmatch(r"[a-f0-9]{64}",key or ""):raise ValueError("bound_review_request_unavailable")
    draft=json.loads((root/".evidence/reasoning/drafts"/(key+".json")).read_text())
    request=draft["payload"]["request"]
    if draft.get("sha256")!=digest(draft["payload"]) or request.get("request_sha256")!=key:
        raise ValueError("reasoning_draft_integrity_failure")
    if digest({k:v for k,v in request.items() if k!="request_sha256"})!=key:
        raise ValueError("reasoning_request_integrity_failure")
    issued=draft["payload"].get("issued_prompts") or {name:response.get(name) for name in ("system_prompt","user_prompt")}
    import hashlib
    system=issued.get("system_prompt");user=issued.get("user_prompt")
    if not isinstance(system,str) or not isinstance(user,str):raise ValueError("original_issued_prompts_unavailable")
    if (hashlib.sha256(system.encode()).hexdigest()!=(request.get("verifier_contract") or {}).get("system_prompt_sha256")
            or json.loads(user)!=request):raise ValueError("issued_review_prompt_integrity_failure")
    context_record=json.loads((root/".evidence/contexts"/(context_id+".json")).read_text())
    context_payload=context_record["payload"]
    if context_record.get("sha256")!=digest(context_payload) or context_payload.get("symbol")!=original["symbol"]:
        raise ValueError("context_integrity_failure")
    from datetime import timedelta
    expires_at=(datetime.fromisoformat(context_payload["prepared_at"])+timedelta(hours=6)).isoformat()
    from openinvest.core.decision_evidence import load_context
    try:
        context=load_context(root,context_id,original["symbol"])
        context_current=True
    except (ValueError,OSError,KeyError,TypeError):
        context_current=False;context={}
    resume=context_current and receipt.get("state")=="awaiting_verification"
    return {**issued,"request_sha256":key,**original,"status":"verification_required" if resume else "audit_only",
            "receipt_state":receipt.get("state"),"resume_allowed":resume,"context_current":context_current,
            "prepared_at":context_payload["prepared_at"],"expires_at":expires_at,"current_advice_valid":False,
            "next_step":"使用逐字原文及已签发核验请求继续提交；不重跑六角色。" if resume else
                        "原提案仅供审计；上下文已变化或核验已定稿，不能把旧记录当作当前建议。"}


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
        if not draft.exists():_save(draft,{"request":request,"issued_prompts":{"system_prompt":SYSTEM_PROMPT,
            "user_prompt":json.dumps(request,ensure_ascii=False,sort_keys=True)},
            "recorded_at":datetime.now(timezone.utc).isoformat(),"allowed_uses":["audit"]})
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
