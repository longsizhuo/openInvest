"""Explicit synthetic qualifications and immutable input fixtures for contract tests."""
from datetime import datetime, timezone

from openinvest.core.decision_evidence import prepare_context
from openinvest.core.memory_store import MemoryStore
from tests.test_evidence_eligibility import approve


def approve_historical_generator(root):
    from openinvest.services.historical_evidence import ARTIFACT,model_specification
    parents=["dataset:synthetic-history","evaluation:synthetic-temporal-test","protocol:synthetic-preregistered"]
    for parent in parents:approve(root,parent,{"synthetic":parent})
    approve(root,ARTIFACT,model_specification(),parents=parents)


def context_id(symbol, **facts):
    data = {"current_price": 100.0, "price_last_bar": datetime.now(timezone.utc).isoformat(),
            "regime_brief": "REGIME: range_bound", "sentiment_brief": "INDEP_DEFENSE_FLAG: off",
            "target": {"symbol": symbol}, "strategy": {}, "alloc_ceiling": 100000,
            "single_invest_cap": None, "defense_dca": None, **facts}
    data.setdefault("macro_data", "SYNTHETIC MACRO INPUT")
    return prepare_context(MemoryStore().root, symbol, data)["context_id"]


def bound_raw(symbol, raw, **facts):
    return f"CONTEXT_ID: {context_id(symbol, **facts)}\n" + raw


def bind_cached_transcript(memory_root, symbol, text):
    import json,re
    from openinvest.core.decision_evidence import prepare_context,record_decision
    from openinvest.core.decision_ledger import parse_committee_text
    from openinvest.utils.llm import get_llm_config_safe
    parsed=parse_committee_text(text)
    parsed["reasoning_review"]={"status":"passed","origin":"synthetic_unit_fixture"}
    context=prepare_context(memory_root,symbol,{"synthetic_fixture":True},model=get_llm_config_safe()[2])
    health=parsed.get("input_health",{})
    evidence=record_decision(memory_root,symbol,{"transcript":text},{k:v for k,v in parsed.items() if k != "input_health"},health,context_id=context['context_id'])
    health.update(evidence)
    section="## Input Health (deterministic)\n\n```json\n"+json.dumps(health)+"\n```\n\n"
    pattern=r"## Input Health[^\n]*\n\s*```json\n.*?\n```\n*"
    bound = re.sub(pattern,lambda _:section,text,flags=re.S) if "## Input Health" in text else text+"\n"+section
    from openinvest.core.decision_evidence import _write_once
    directory=memory_root/".evidence/transcripts";directory.mkdir(parents=True,exist_ok=True)
    (directory/f"{evidence['decision_id']}.md").write_text(bound)
    _write_once(directory/f"{evidence['decision_id']}.json",{"text":bound})
    return bound
