"""Draft/review/finalize reuses one proposal and cannot activate unverified plans."""
from datetime import datetime,timezone
import json

from openinvest.core.committee.reasoning_quality import CHECKS
from openinvest.core.decision_evidence import prepare_context
from openinvest.core.memory_store import MemoryStore
from openinvest.core.runner.coordinator import save_committee_transcript


def transcript():
    alternatives=[{"kind":kind,"applicability":"unknown","exit_intent":{
        "REDUCE_RISK":"risk_reduction","TACTICAL_REENTRY":"tactical_reentry"}.get(kind)}
        for kind in ["HOLD_EXISTING","ADD","REDUCE_RISK","TACTICAL_REENTRY"]]
    roles={"MACRO":"SIGNAL: neutral\nSTRENGTH: 4","QUANT_R1":"SIGNAL: neutral\nSTRENGTH: 4",
           "QUANT_R2":"ADJUSTED_SIGNAL: neutral\nSTRENGTH: 4","RISK_R1":"SIGNAL: concerned\nSTRENGTH: 4\nMARKET_RISK: elevated\nLIQUIDITY: available",
           "RISK_R2":"ADJUSTED_SIGNAL: concerned\nSTRENGTH: 4\nMARKET_RISK: elevated\nLIQUIDITY: available","CIO":"VERDICT: HOLD\nCONFIDENCE: 0.6\nDOMINANT_VIEW: risk\nSUGGESTED_ALLOC_CNY: 0\nALTERNATIVES_JSON: "+json.dumps(alternatives)}
    return "\n\n".join("=== "+name+" ===\n"+body for name,body in roles.items())


def test_keyless_draft_then_external_review_does_not_regenerate_or_consume_context(tmp_path,monkeypatch):
    monkeypatch.setattr("openinvest.core.memory_store.MEMORY_ROOT",tmp_path/"memory")
    monkeypatch.setattr("openinvest.paths.INVEST_ROOT",tmp_path)
    monkeypatch.setenv("INVEST_REASONING_BY","agent")
    store=MemoryStore()
    from openinvest.services import agent_queue as queue
    from openinvest.core.runner.event_format import EventBrief
    queue.enqueue_committee(["SYNTHETIC"], [], "synthetic risk observation", memory_root=store.root)
    bindings=queue.bind_context_requests(store.root,"SYNTHETIC",EventBrief("",{}),datetime.now(timezone.utc).isoformat())
    facts={"market_data":"reference price 100 CNY/unit","current_price":100,
           "price_last_bar":datetime.now(timezone.utc).isoformat(),"macro_data":"provided macro observation",
           "portfolio_summary":"declared position: 1 unit; cash 1000 CNY",
           "required_roles":["MACRO","QUANT_R1","RISK_R1","QUANT_R2","RISK_R2","CIO"],
           "trigger_requests":bindings}
    context=prepare_context(store.root,"SYNTHETIC",facts,model="synthetic-model")
    raw=transcript()+"\n\n "
    first=save_committee_transcript("SYNTHETIC",raw,provider="unit",model="synthetic-model",context_id=context["context_id"])
    assert first["status"]=="verification_required" and not first["current_advice_valid"]
    assert not (store.root/".committee").exists()
    waiting=queue.pending_committee_items(memory_root=store.root)[0]
    assert waiting["status"]=="awaiting_reasoning_review"
    assert waiting["review_requests"][0]["completion_granted"] is False
    from openinvest.services.reasoning_review import submitted_review
    before={str(path.relative_to(store.root)):path.read_bytes() for path in store.root.rglob("*.json")}
    recovered=submitted_review(store.root,context["context_id"])
    assert recovered["transcript"]==raw and recovered["resume_allowed"]
    assert recovered["provider"]=="unit" and recovered["model"]=="synthetic-model"
    assert recovered["request_sha256"]==first["request_sha256"]
    assert datetime.fromisoformat(recovered["expires_at"]).tzinfo is not None
    assert before=={str(path.relative_to(store.root)):path.read_bytes() for path in store.root.rglob("*.json")}
    assert queue.pending_committee_items(memory_root=store.root)[0]["status"]=="awaiting_reasoning_review"
    replay=save_committee_transcript("SYNTHETIC",raw,provider="unit",model="synthetic-model",context_id=context["context_id"])
    assert replay["request_sha256"]==first["request_sha256"] and replay["replayed"]
    check=json.dumps({"request_sha256":first["request_sha256"],"checks":{key:"passed" for key in CHECKS},"issues":[]})
    final=save_committee_transcript("SYNTHETIC",raw,provider="unit",model="synthetic-model",context_id=context["context_id"],reasoning_review_json=check)
    assert final["verdict"]["reasoning_review"]["status"]=="passed"
    assert final["verdict"]["verdict"]=="HOLD"
    assert queue.pending_committee_items(memory_root=store.root)==[]
    assert submitted_review(store.root,context["context_id"])["status"]=="audit_only"
    import openinvest.services.reasoning_review as review_service
    monkeypatch.setattr(review_service,"SYSTEM_PROMPT","Changed current prompt must not replace the issued historical prompt")
    historical=submitted_review(store.root,context["context_id"])
    assert historical["system_prompt"]==first["system_prompt"]
    import hashlib
    original_request=json.loads(historical["user_prompt"])
    assert hashlib.sha256(historical["system_prompt"].encode()).hexdigest()==original_request["verifier_contract"]["system_prompt_sha256"]
    assert len(list((store.root/".evidence/decisions").glob("*.json")))==1
    assert len(list((store.root/".evidence/reasoning/drafts").glob("*.json")))==1
    changed=json.loads(check);changed["checks"]["arithmetic"]="warning"
    import pytest
    with pytest.raises(ValueError,match="different review"):
        save_committee_transcript("SYNTHETIC",raw,provider="unit",model="synthetic-model",context_id=context["context_id"],reasoning_review_json=json.dumps(changed))


def test_review_retrieval_never_guesses_a_missing_original_or_reads_another_ledger(tmp_path):
    from openinvest.services.reasoning_review import submitted_review
    missing=submitted_review(tmp_path/"other-ledger/memory","a"*32)
    assert missing["status"]=="unrecoverable_original_missing" and not missing["resume_allowed"]
    assert "transcript" not in missing and not (tmp_path/"other-ledger").exists()


def test_unsolicited_external_review_cannot_replace_backend_verification(tmp_path,monkeypatch):
    import pytest
    from openinvest.core.committee.reasoning_quality import make_request
    from openinvest.services.reasoning_review import review_once
    monkeypatch.setattr("openinvest.paths.INVEST_ROOT",tmp_path)
    request=make_request(sources={},proposal={"memo":"synthetic"},model="synthetic-model")
    external=json.dumps({"request_sha256":request["request_sha256"],"checks":{key:"passed" for key in CHECKS},"issues":[]})
    def denied(*args,**kwargs):raise AssertionError("No model call is needed to reject an unsolicited external review")
    with pytest.raises(ValueError,match="requires_an_issued_request"):
        review_once(request,memory_root=tmp_path/"memory",agent_factory=denied,ask=denied,
                    asset="SYNTHETIC",external_raw=external,backend_allowed=True)
    assert not list((tmp_path/"memory/.evidence/reasoning/receipts").glob("*.json"))


def test_direct_failed_review_marks_bound_risk_cause_waiting(tmp_path,monkeypatch):
    from openinvest.services import agent_queue as queue
    from openinvest.core.runner.event_format import EventBrief
    from openinvest.core.committee.debate import CommitteeReport
    from openinvest.core.committee.persist import _persist
    monkeypatch.setattr("openinvest.paths.INVEST_ROOT",tmp_path)
    monkeypatch.setattr("openinvest.core.memory_store.MEMORY_ROOT",tmp_path/"memory")
    monkeypatch.setattr("openinvest.core.committee.persist._capture_macro_context",lambda **kwargs:{})
    store=MemoryStore()
    queue.enqueue_committee(["SYNTHETIC"],[],"synthetic risk observation",memory_root=store.root)
    bindings=queue.bind_context_requests(store.root,"SYNTHETIC",EventBrief("",{}),datetime.now(timezone.utc).isoformat())
    context=prepare_context(store.root,"SYNTHETIC",{"trigger_requests":bindings})
    review={"status":"failed","request_sha256":"synthetic-review"}
    verdict={"verdict":"HOLD","confidence":0.6,"alloc_cny":0,"dominant_view":"risk",
             "decision_status":"REVIEW_REQUIRED","reasoning_review":review}
    report=CommitteeReport(asset={"symbol":"SYNTHETIC"},reasoning_review=review)
    _persist(report,verdict,input_health={"context_id":context["context_id"]})
    pending=queue.pending_committee_items(memory_root=store.root)
    assert pending[0]["status"]=="reasoning_rejected"
    assert pending[0]["review_requests"][0]["request_sha256"]=="synthetic-review"
    from openinvest.services.decision_quality import findings
    assert any(row.get('resolution_state')=='rejected_proposal' and '被否决' in row['text'] for row in findings(store.root))


def test_billed_empty_verifier_does_not_automatically_charge_same_request_again(tmp_path,monkeypatch):
    from openinvest.core.committee.reasoning_quality import make_request
    from openinvest.services.reasoning_review import review_once
    monkeypatch.setattr('openinvest.paths.INVEST_ROOT',tmp_path)
    alternatives=[{'kind':kind,'applicability':'unknown','exit_intent':{'REDUCE_RISK':'risk_reduction','TACTICAL_REENTRY':'tactical_reentry'}.get(kind)}
        for kind in ('HOLD_EXISTING','ADD','REDUCE_RISK','TACTICAL_REENTRY')]
    req=make_request(sources={},proposal={'fields':{'alternatives':alternatives},'memo':'synthetic qualitative judgment'})
    called=[]
    def ask(*args):called.append(1);return ''
    first=review_once(req,memory_root=tmp_path/'memory',agent_factory=lambda *a,**kw:object(),ask=ask,asset='TEST')
    second=review_once(req,memory_root=tmp_path/'memory',agent_factory=lambda *a,**kw:object(),ask=ask,asset='TEST')
    assert first['status']=='unavailable' and first['retryable'] is False
    assert second['replayed'] and called==[1]
