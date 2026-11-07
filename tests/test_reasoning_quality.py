"""Quality qualification is action-neutral and never turns model failure into HOLD."""
import json

from openinvest.core.committee.reasoning_quality import CHECKS,make_request,parse_review,apply_review,alternative_contract


def request():
    return make_request(sources={"market":{"kind":"raw_market","text":"RVOL 0.71"},
                                 "probability":{"kind":"terminal_return","text":"terminal_return; first_touch not provided"}},
                        proposal={"memo":"HOLD, volume increased. End probability proves a lower reentry is unlikely.","wire":"original JSON"},
                        model="synthetic-model")


def response(req,issues=None):
    issues=issues or []
    checks={name:"passed" for name in CHECKS}
    if any(issue["blocking"] for issue in issues):checks["internal_consistency"]="failed"
    return {"request_sha256":req["request_sha256"],"checks":checks,"issues":issues}


def test_correct_hold_with_unknown_budget_can_pass_without_permission_to_trade_or_learn():
    req=request();result=parse_review(json.dumps(response(req)),req)
    assert result["status"]=="passed" and not result["trade_authorized"] and not result["source_qualification_granted"]
    proposal={"verdict":"HOLD","alloc_cny":0,"decision_status":"READY"}
    apply_review(proposal,result)
    assert proposal["verdict"]=="HOLD" and proposal["decision_status"]=="READY"


def test_bound_material_error_marks_review_required_and_preserves_direction():
    req=request();issue={"code":"terminal_as_first_touch","blocking":True,
        "claim_quote":"End probability proves a lower reentry is unlikely.",
        "basis_refs":[{"source_id":"probability","quote":"terminal_return; first_touch not provided"}],
        "explanation":"The events differ; no probability for first-touch was supplied."}
    result=parse_review(json.dumps(response(req,[issue])),req)
    assert result["status"]=="failed"
    proposal={"verdict":"BUY","alloc_cny":2000,"decision_status":"READY","_raw_proposal":{"verdict":"BUY"}}
    apply_review(proposal,result)
    assert proposal["verdict"]=="BUY" and proposal["alloc_cny"]==0
    assert proposal["decision_status"]=="REVIEW_REQUIRED" and proposal["_raw_proposal"]["verdict"]=="BUY"


def test_fake_quote_and_other_request_hash_are_invalid_review_not_cio_error():
    req=request();value=response(req);value["request_sha256"]="wrong"
    assert parse_review(json.dumps(value),req)["status"]=="invalid_response"
    issue={"code":"source_misquotation","blocking":True,"claim_quote":"invented output",
           "basis_refs":[{"source_id":"market","quote":"RVOL 0.71"}],"explanation":"fake"}
    assert parse_review(json.dumps(response(req,[issue])),req)["status"]=="invalid_response"


def test_unavailable_review_does_not_override_input_failure_or_claim_judgment_error():
    verdict={"verdict":"HOLD","alloc_cny":0,"decision_status":"DATA_UNTRUSTED"}
    apply_review(verdict,{"status":"unavailable"})
    assert verdict["decision_status"]=="DATA_UNTRUSTED"
    assert verdict["reasoning_review"]["status"]=="unavailable"


def test_risk_reduction_and_conditional_tactical_plan_can_both_be_applicable():
    proposal={"alternatives":[{"kind":kind,"applicability":"unknown" if kind=="TACTICAL_REENTRY" else "applicable",
        "exit_intent":{"REDUCE_RISK":"risk_reduction","TACTICAL_REENTRY":"tactical_reentry"}.get(kind)}
        for kind in ["HOLD_EXISTING","ADD","REDUCE_RISK","TACTICAL_REENTRY"]]}
    assert alternative_contract(proposal)["valid"]
    proposal["alternatives"][2]["exit_intent"]="tactical_reentry"
    assert not alternative_contract(proposal)["valid"]


def test_missing_checks_cannot_default_to_pass():
    req=request();value=response(req);value["checks"].pop(next(iter(CHECKS)))
    assert parse_review(json.dumps(value),req)["status"]=="invalid_response"


def test_review_cache_key_changes_with_prompt_or_effective_model_config(monkeypatch):
    import openinvest.core.committee.reasoning_quality as quality
    first=request()["request_sha256"]
    monkeypatch.setattr(quality,"SYSTEM_PROMPT",quality.SYSTEM_PROMPT+"\nChanged verification instruction.")
    second=request()["request_sha256"]
    assert second!=first
    monkeypatch.setenv("LLM_MODEL","synthetic-other-model")
    assert request()["request_sha256"]!=second


def test_malformed_extra_alternative_is_a_contract_failure():
    import pytest
    valid=[{"kind":kind,"applicability":"unknown","exit_intent":{
        "REDUCE_RISK":"risk_reduction","TACTICAL_REENTRY":"tactical_reentry"}.get(kind)}
        for kind in ["HOLD_EXISTING","ADD","REDUCE_RISK","TACTICAL_REENTRY"]]
    for extra in [{"applicability":"unknown"},{"kind":[]},{"kind":"UNKNOWN"},None]:
        assert alternative_contract({"alternatives":[*valid,extra]})=={"valid":False,"reason":"alternative_kind"}
