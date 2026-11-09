"""Quality qualification is action-neutral and never turns model failure into HOLD."""
import json

from openinvest.core.committee.reasoning_quality import CHECKS,make_request,parse_review,apply_review,alternative_contract


def request():
    return make_request(sources={"market":{"kind":"raw_market","text":"RVOL 0.71"},
                                 "probability":{"kind":"terminal_return","text":"terminal_return; first_touch not provided"}},
                        proposal={"memo":"HOLD, volume increased. End probability proves a lower reentry is unlikely.","wire":"original JSON"},
                        model="synthetic-model")


def response(req,issues=None):
    import copy
    issues=copy.deepcopy(issues or [])
    for issue in issues:
        issue.setdefault('assertion_scope','formal_event_relation' if issue['code']=='terminal_as_first_touch' else 'observed_case_fact')
        ref=issue.get('basis_refs',[{}])[0]
        issue.setdefault('factual_delta',{'asserted_fact_quote':issue.get('claim_quote',''),
            'source_id':ref.get('source_id'),'source_fact_quote':ref.get('quote',''),
            'contradiction':issue.get('explanation','Synthetic factual discrepancy')})
    checks={name:"passed" for name in CHECKS}
    if any(issue["blocking"] for issue in issues):checks["internal_consistency"]="failed"
    return {"request_sha256":req["request_sha256"],"checks":checks,"issues":issues}


def test_current_hard_issue_requires_bound_factual_witness_not_an_investment_opinion():
    req=request();issue={'code':'source_misquotation','blocking':True,'claim_quote':'volume increased.',
        'basis_refs':[{'source_id':'market','quote':'RVOL 0.71'}],'explanation':'Synthetic observed-input mismatch'}
    value=response(req,[issue])
    assert parse_review(json.dumps(value),req)['status']=='failed'
    for change in ('missing_scope','judgment_scope','missing_delta','wrong_claim','wrong_source','wrong_basis'):
        import copy
        bad=copy.deepcopy(value);row=bad['issues'][0]
        if change=='missing_scope':row.pop('assertion_scope')
        elif change=='judgment_scope':row['assertion_scope']='market_judgment'
        elif change=='missing_delta':row.pop('factual_delta')
        elif change=='wrong_claim':row['factual_delta']['asserted_fact_quote']='invented measurement'
        elif change=='wrong_source':row['factual_delta']['source_id']='absent'
        else:row['factual_delta']['source_fact_quote']='RVOL 1.9'
        assert parse_review(json.dumps(bad),req)['status']=='invalid_response',change


def test_statement_scopes_do_not_turn_opinion_or_legacy_records_into_factual_authority():
    req=request()
    assert 'model_market_judgment' in req['proposal_statement_scopes']['market_assessment']
    assert 'market_judgment' not in req['factual_assertion_scopes']
    old={**req,'policy':'cio_reasoning_quality_v5'}
    value=response(old);parsed=parse_review(json.dumps(value),old)
    assert parsed['status']=='passed' and parsed['policy']=='cio_reasoning_quality_v5'
    assert parsed['trade_authorized'] is False and parsed['source_qualification_granted'] is False


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


def test_verifier_receives_the_same_config_policy_without_claiming_user_intent():
    from openinvest.core.committee.reasoning_quality import proposal_sources
    from openinvest.core.config import set_config_override
    set_config_override({"verdict":{"concentration_lens_enabled":False}})
    sources=proposal_sources({"single_invest_cap":1000},{})
    text=sources["constraints"]["text"]
    assert '"concentration_lens_enabled": false' in text
    assert '"authority": "effective_configuration_not_verified_user_intent"' in text
    assert '"legacy_tactical_pnl_filter": "unvalidated_policy_excluded_from_market_judgment"' in text


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


def test_reported_quantitative_error_cannot_be_laundered_by_nonblocking_severity():
    verdict={'verdict':'ACCUMULATE','alloc_cny':1000,'decision_status':'READY'}
    original={'status':'passed','issues':[{'code':'arithmetic_or_unit_error','blocking':False,'claim_quote':'synthetic mathematical defect'}]}
    apply_review(verdict,original)
    assert original['status']=='passed'
    assert verdict['verdict']=='ACCUMULATE' and verdict['alloc_cny']==0 and verdict['decision_status']=='REVIEW_REQUIRED'
    assert verdict['reasoning_review']['status']=='needs_review' and verdict['reasoning_review']['review_status_before_qualification']=='passed'


def test_source_bound_editorial_note_does_not_become_a_new_directional_veto():
    req=request();raw=response(req)
    raw['notes']=[{'kind':'editorial_attribution','claim_quote':'HOLD, volume increased.',
        'basis_refs':[{'source_id':'market','quote':'RVOL 0.71'}],'explanation':'Synthetic note fixture, not a factual-defect assertion.',
        'changes_asserted_fact':False,'changes_evidence_independence':False,'changes_user_authority':False}]
    review=parse_review(json.dumps(raw),req)
    assert review['status']=='passed' and review['notes'] and not review['issues']
    verdict={'verdict':'HOLD','alloc_cny':0};apply_review(verdict,review)
    assert verdict['reasoning_review']['status']=='passed' and verdict['verdict']=='HOLD'


def test_note_channel_cannot_declare_a_changed_fact_or_authority_as_harmless():
    req=request();raw=response(req)
    raw['notes']=[{'kind':'judgment_uncertainty','claim_quote':'HOLD, volume increased.',
        'basis_refs':[{'source_id':'market','quote':'RVOL 0.71'}],'explanation':'Synthetic invalid note.',
        'changes_asserted_fact':True,'changes_evidence_independence':False,'changes_user_authority':False}]
    assert parse_review(json.dumps(raw),req)['status']=='invalid_response'
