"""Bounded reasoning checks, separate from sources, outcomes and trade authority."""
from __future__ import annotations

import json
from typing import Any

from openinvest.core.evidence_eligibility import digest

POLICY="cio_reasoning_quality_v5"
CHECKS={"source_attribution","time_and_units","arithmetic","internal_consistency",
        "constraint_authority","exit_purpose","probability_event_definition"}
CODES={"source_misquotation","unsupported_case_fact","arithmetic_or_unit_error","internal_contradiction",
       "invented_user_constraint","risk_exit_mixed_with_tactical","terminal_as_first_touch","analysis_contract_missing"}
REQUIRED_ALTERNATIVES={"HOLD_EXISTING","ADD","REDUCE_RISK","TACTICAL_REENTRY"}

SYSTEM_PROMPT="""You check the supplied proposal for bounded reasoning defects. You do not select an investment action,
rewrite the proposal, predict returns, infer the user's loss tolerance from cash, or treat model agreement as independent evidence.
Correct HOLD may pass. Unknown loss tolerance only forbids claiming it is known; it requires no particular action.
Unknown first-touch probability permits an explicitly conditional tactical plan. Risk reduction need not promise a lower repurchase.
Model-proposed levels are not existing user stop rules. Missing a stop does not establish that holding is safe.
Separate market/account observations from analyst interpretations and generated review plans. Quote existence does not prove truth.
Separate market direction/evidence from account suitability, execution readiness and the chosen action. A valid positive market assessment may coexist with zero executable allocation.
No qualified historical forecast or first-touch probability is not itself evidence against ADD or in favor of HOLD. Unknown user loss tolerance forbids inventing tolerance; it does not determine market risk or direction.
Applicability is different from preference. Assess the supplied support and counterevidence, not whether the selected action meets a trading quota.
Market risk needs a stated baseline and horizon. Normal asset volatility and a distant historical drawdown alone do not prove a current risk increase; cash alone does not prove ordinary market risk.
A TIP ETF price change is not an observed real-yield change, including when an inverse relationship is asserted. Only supplied actual yield data can establish that fact.
For financial scenarios, current-mark-to-scenario value change, cost-based unrealized PnL and realized-PnL increments have different baselines.
A no-trade realized increment of zero does not mean a zero change in current market value. Historical realized totals require execution history.
A single target price cannot establish maximum drawdown, a worst-loss bound or a forecast probability.
Use supplied typed position qualification and backend scenario calculations; unknown quantity, mark or cost is not zero.
Legacy freeform stress-loss amounts are not verified financial facts. The backend scenario contract is independently enforced even if your review passes.
Check only material source, arithmetic, unit, temporal and logical errors. Subjective risk/return weighting is not a defect by itself.
Do not reject an unprofitable judgment just because later prices are known to you; use only the supplied sources and as-of context.
Terminal-return and first-touch events differ. Valid event inclusion bounds are allowed, but cannot imply an execution guarantee.
Separate defects from notes. An issue changes an asserted fact, measurement type/value/unit/time, evidence independence, user authority, or a necessary logical premise. A concrete financial arithmetic or authority defect remains an issue even when the selected action happens to be reasonable.
Editorial attribution precision or judgment uncertainty that introduces no new asserted fact, independence or authority belongs in notes, not in a defect code. Missing forecast probabilities are unknown, not evidence of unfavorable odds; a categorical contrary assertion is still a defect.
Qualitative market-regime labels such as risk_on and risk_off are interpretations, not measured indices. When the label is correctly present in the supplied analyst opinion, the underlying observations are unchanged, and no independent confirmation is claimed, loose attribution that groups that label with observations is an editorial note. Apply this equally to positive and negative labels. This does not excuse misquoting the analyst's actual label, inventing a measured index or yield, changing measured values/units/times, converting TIP returns into observed yields, or counting the opinion as another independent observation.
Validated market citations only establish that a quotation occurs in the supplied market/macro/reported-event text. Preserve its source kind: a normalized report is not verified truth. Such citations cannot establish an account balance, native position quotation, PnL, user permission or independent evidence. Compare assertions to the source and backend citation binding; do not treat a copied monetary market quotation as personal account arithmetic.
notes is an array (possibly empty) of {kind, claim_quote, basis_refs, explanation, changes_asserted_fact, changes_evidence_independence, changes_user_authority}. kind is editorial_attribution or judgment_uncertainty; all three changes flags must be false. Quotes/references must be exact as for issues. Do not use notes to downgrade an actual error.
Return one JSON object with exactly request_sha256, checks, issues, notes. Echo the supplied request_sha256.
checks is an object containing every supplied check name once, with passed, warning, not_applicable or failed as value.
issues is an array. Each issue has code, blocking (boolean), claim_quote (exact nonempty quote from the proposal),
basis_refs (array of {source_id, quote}, with exact source quotes), and explanation. Use only the supplied issue codes.
An absent source can instead reference a supplied structured not_provided source; never invent an absence quote.
No issue may recommend BUY/SELL or criticize insufficient trading activity. A blocking issue needs an actual material defect.
No blocking issues means the limited checks found no blocking issue; this does not approve sources, trading or learning eligibility.
"""


def make_request(*,sources:dict,proposal:dict,model:str="unknown") -> dict:
    from openinvest.core.decision_evidence import _plain
    from pathlib import Path
    import hashlib
    from openinvest.utils.llm import get_llm_config_safe,committee_role_thinking,verifier_generation_limits
    proposal=_plain(proposal)
    sources=dict(sources)
    position=(sources.get('position') or {}).get('value')
    if 'position' in sources:
        from openinvest.core.committee.scenarios import validate_scenarios
        fields=proposal.get('fields') or {}
        scenarios=fields.get('scenario_analysis')
        if fields.get('_scenario_analysis_error'):scenarios={'parse_error':fields['_scenario_analysis_error']}
        calculation=validate_scenarios(position,scenarios,memo=proposal.get('memo',''),
            context_id=(sources.get('analysis_context') or {}).get('value'),fields=fields,
            market_reference=(sources.get('market_reference') or {}).get('value'),market_sources=sources)
        sources['scenario_math']={'kind':'conditional_backend_arithmetic_not_forecast_or_permission',
            'value':calculation,'text':json.dumps(calculation,ensure_ascii=False,sort_keys=True)}
    _,endpoint,runtime_model,provider=get_llm_config_safe()
    packet={"policy":POLICY,"sources":sources,"proposal":proposal,"verifier_model":model,
            "verifier_contract":{"system_prompt_sha256":hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
                "implementation_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "schema_version":2,**verifier_generation_limits(),"provider":provider,"runtime_model":runtime_model,
                "endpoint_sha256":hashlib.sha256(str(endpoint).encode()).hexdigest(),
                "temperature":0.0,"thinking":committee_role_thinking("reasoning_verifier"),"response_format":{"type":"json_object"}},
            "required_checks":sorted(CHECKS),"issue_codes":sorted(CODES)}
    return {**packet,"request_sha256":digest(packet)}


def proposal_sources(facts:dict,sections:dict) -> dict:
    """Keep generated plans and analyst judgments out of user/market authority."""
    from openinvest.core.committee.evidence import execution_constraints_context
    from openinvest.core.committee.scenarios import market_citation_catalog
    sources={name:{'kind':kind,'text':'not_provided'} for name,kind in (
        ('market','market_input'),('macro','macro_input'),('events','normalized_reports_not_verified_truth'))}
    sources.update(market_citation_catalog(facts))
    for name,key,kind in [("regime","regime_brief","rule_classification_and_historical_reference"),
                          ("terminal","path_reference","terminal_statistics_not_first_touch"),
                          ("valuation","valuation_brief","valuation_input"),("sentiment","sentiment_brief","sentiment_input")]:
        value=str(facts.get(key) or "")
        if name in ("regime","terminal"):
            from openinvest.core.committee.evidence import historical_reference_context
            value=historical_reference_context(value,qualification=facts.get("historical_qualification"),
                kind="regime" if name=="regime" else "path")
        sources[name]={"kind":kind,"text":value or "not_provided"}
    reference=facts.get('market_reference')
    sources['market_reference']={'kind':'market_snapshot_not_native_position_quote','value':reference,
        'text':json.dumps(reference,ensure_ascii=False,sort_keys=True) if reference else 'not_provided'}
    position=facts.get('position_facts')
    sources['position']={'kind':'typed_field_qualification_not_account_authority',
        'value':position,'text':json.dumps(position,ensure_ascii=False,sort_keys=True) if position else 'not_provided'}
    sources['analysis_context']={'kind':'context_identity','value':facts.get('context_id'),
        'text':str(facts.get('context_id') or 'not_provided')}
    portfolio=str(facts.get("portfolio_summary") or "")
    user,marker,plans=portfolio.partition("=== EXISTING REVIEW PLAN ===")
    sources["account"]={"kind":"supplied_account_context_not_assumed_loss_tolerance","text":user or "not_provided"}
    sources["model_plans"]={"kind":"generated_review_proposals_not_user_rules","text":plans if marker else "not_provided"}
    sources["constraints"]={"kind":"runtime_effective_constraints","text":execution_constraints_context(facts.get("single_invest_cap"),facts.get("execution_constraint_facts"))}
    sources["as_of"]={"kind":"analysis_clock","text":str(facts.get("analysis_as_of") or facts.get("prepared_at") or "not_provided")}
    sources["first_touch"]={"kind":"quantity_availability","text":"validated_first_touch_probability: not_provided; terminal_return is not first_touch"}
    for name,text in sections.items():
        if name!="CIO":sources["analyst_"+name]={"kind":"model_assessment_not_raw_fact_or_user_instruction","text":str(text)}
    return sources


def parse_review(raw:str,request:dict) -> dict:
    try:
        def unique_pairs(pairs):
            result={}
            for key,value in pairs:
                if key in result:raise ValueError("duplicate_json_key")
                result[key]=value
            return result
        value=json.loads(raw,object_pairs_hook=unique_pairs,parse_constant=lambda value:(_ for _ in ()).throw(ValueError("nonfinite_json")))
        if not isinstance(value,dict) or not {"request_sha256","checks","issues"}<=set(value) or set(value)-{"request_sha256","checks","issues","notes"}:raise ValueError("review_schema")
        if value["request_sha256"]!=request["request_sha256"]:raise ValueError("review_binding")
        checks=value["checks"]
        if not isinstance(checks,dict) or set(checks)!=CHECKS or any(status not in ("passed","warning","not_applicable","failed") for status in checks.values()):
            raise ValueError("review_check_coverage")
        issues=value["issues"]
        if not isinstance(issues,list):raise ValueError("review_issues")
        proposal=json.dumps(request["proposal"],ensure_ascii=False)
        # The original prose is also matched directly; JSON escaping must not
        # make a true exact quotation impossible to bind.
        texts=[str(request["proposal"].get(key) or "") for key in ("memo","wire")]
        for issue in issues:
            if (not isinstance(issue,dict) or issue.get("code") not in CODES or type(issue.get("blocking")) is not bool
                    or not isinstance(issue.get("claim_quote"),str) or not issue["claim_quote"]
                    or not any(issue["claim_quote"] in text for text in [proposal,*texts])):
                raise ValueError("review_claim_reference")
            refs=issue.get("basis_refs")
            if not isinstance(refs,list) or not refs:raise ValueError("review_basis_reference")
            for ref in refs:
                source=request["sources"].get(ref.get("source_id"))
                quote=ref.get("quote")
                if not source or not isinstance(quote,str) or not quote or quote not in source["text"]:
                    raise ValueError("review_basis_reference")
        notes=value.get('notes',[])
        if not isinstance(notes,list):raise ValueError('review_notes')
        for note in notes:
            if (not isinstance(note,dict) or note.get('kind') not in {'editorial_attribution','judgment_uncertainty'}
                    or any(note.get(key) is not False for key in ('changes_asserted_fact','changes_evidence_independence','changes_user_authority'))
                    or not isinstance(note.get('claim_quote'),str) or not note['claim_quote']
                    or not any(note['claim_quote'] in text for text in [proposal,*texts])
                    or not isinstance(note.get('explanation'),str) or not note['explanation']):
                raise ValueError('review_note_scope')
            if not isinstance(note.get('basis_refs'),list) or not note['basis_refs']:raise ValueError('review_note_basis')
            for ref in note['basis_refs']:
                source=request['sources'].get(ref.get('source_id'));quote=ref.get('quote')
                if not source or not isinstance(quote,str) or not quote or quote not in source['text']:raise ValueError('review_note_basis')
        blocked=any(issue["blocking"] for issue in issues)
        if ("failed" in checks.values())!=blocked:raise ValueError("review_check_issue_mismatch")
        return {"status":"failed" if blocked else "needs_review" if issues else "passed","policy":POLICY,"request_sha256":request["request_sha256"],
                "checks":checks,"issues":issues,"notes":notes,"source_qualification_granted":False,"trade_authorized":False}
    except (ValueError,TypeError,KeyError,AttributeError) as exc:
        return {"status":"invalid_response","policy":POLICY,"request_sha256":request["request_sha256"],
                "error":str(exc),"source_qualification_granted":False,"trade_authorized":False}


def alternative_contract(proposal:dict) -> dict:
    alternatives=proposal.get("alternatives")
    if not isinstance(alternatives,list):return {"valid":False,"reason":"typed_alternatives_missing"}
    allowed=REQUIRED_ALTERNATIVES|{"THESIS_EXIT","LIQUIDITY_EXIT"}
    if any(not isinstance(entry,dict) or not isinstance(entry.get("kind"),str)
           or entry["kind"] not in allowed for entry in alternatives):
        return {"valid":False,"reason":"alternative_kind"}
    kinds=[entry["kind"] for entry in alternatives]
    if not REQUIRED_ALTERNATIVES<=set(kinds) or len(kinds)!=len(set(kinds)):
        return {"valid":False,"reason":"alternative_coverage_or_duplicates"}
    for entry in alternatives:
        if not isinstance(entry,dict) or entry.get("applicability") not in ("applicable","not_applicable","unknown"):
            return {"valid":False,"reason":"alternative_applicability"}
        expected={"REDUCE_RISK":"risk_reduction","TACTICAL_REENTRY":"tactical_reentry", "THESIS_EXIT":"thesis_invalidated","LIQUIDITY_EXIT":"liquidity_need"}.get(entry["kind"])
        if expected and entry.get("exit_intent")!=expected:return {"valid":False,"reason":"alternative_exit_intent"}
    return {"valid":True}


def apply_review(verdict:dict,review:dict) -> None:
    """Qualification never overwrites the market proposal or source validity."""
    verdict["reasoning_review"]=review
    if review.get('status')=='passed' and review.get('issues'):
        # A reported factual/logic defect cannot qualify merely because the
        # checker assigns its own warning/non-blocking severity.
        review={**review,'review_status_before_qualification':'passed','status':'needs_review',
                'qualification_reason':'reported_factual_or_logic_defect'}
        verdict['reasoning_review']=review
    if review.get("status")=="passed":return
    verdict.setdefault("_original_alloc",verdict.get("alloc_cny"))
    verdict["alloc_cny"]=0
    if verdict.get("decision_status")!="DATA_UNTRUSTED":
        verdict.update(decision_status="REVIEW_REQUIRED",qualification_reason="reasoning_"+review.get("status","unavailable"))


def call_review(request,*,agent_factory,ask,asset):
    from openinvest.utils.llm import committee_role_thinking
    contract=request.get('verifier_contract') or {}
    if contract:
        import hashlib
        from pathlib import Path
        from openinvest.utils.llm import get_llm_config_safe
        _,endpoint,runtime_model,provider=get_llm_config_safe()
        expected={'system_prompt_sha256':hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
            'implementation_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'runtime_model':runtime_model,'provider':provider,
            'endpoint_sha256':hashlib.sha256(str(endpoint).encode()).hexdigest()}
        if any(contract.get(key)!=value for key,value in expected.items()):
            return {'status':'unavailable','policy':POLICY,'request_sha256':request['request_sha256'],
                    'reason':'verifier_runtime_contract_changed'}
    agent=agent_factory(SYSTEM_PROMPT,search_enabled=False,temperature=0.0,role="reasoning_verifier",asset=asset,
                        round_label="cio_quality",enable_thinking=contract.get("thinking",committee_role_thinking("reasoning_verifier")),response_format={"type":"json_object"},
                        max_output_tokens=contract.get("max_output_tokens"),reasoning_effort=contract.get("reasoning_effort"))
    if agent is None:
        return {"status":"unavailable","policy":POLICY,"request_sha256":request["request_sha256"],"reason":"verifier_capability_unavailable"}
    raw=ask(agent,json.dumps(request,ensure_ascii=False,sort_keys=True))
    if not raw or raw.startswith("[WORKER_UNAVAILABLE] reason="):
        return {"status":"unavailable","policy":POLICY,"request_sha256":request["request_sha256"],"reason":"verifier_call_unavailable","retryable":False}
    result={**parse_review(raw,request),"raw_response":raw}
    if result.get('status')=='invalid_response':result['retryable']=False
    return result
