"""Explicit synthetic qualifications and immutable input fixtures for contract tests."""
from datetime import datetime, timezone

from openinvest.core.decision_evidence import prepare_context
from openinvest.core.memory_store import MemoryStore
from tests.test_evidence_eligibility import approve
import pytest


@pytest.fixture
def reviewed_event_storage(monkeypatch):
    """Opt-in only: independently reviewed synthetic inputs for storage contracts."""
    reviewed_storage_contracts(monkeypatch)


def approve_historical_generator(root):
    from openinvest.services.historical_evidence import ARTIFACT,model_specification
    parents=["dataset:synthetic-history","evaluation:synthetic-temporal-test","protocol:synthetic-preregistered"]
    for parent in parents:approve(root,parent,{"synthetic":parent})
    approve(root,ARTIFACT,model_specification(),parents=parents)


def approve_event_reporting(store,event_id):
    """Explicit administrative synthetic evidence for non-authentication contracts."""
    import json
    from openinvest.services.source_reporting import source_fingerprint,source_payload,event_review_id,event_review_payload
    event=store.get_event(event_id,purpose="audit")
    sources=store.get_sources(event_id,purpose="audit")
    parents=[]
    for source in sources:
        key="source_report:"+source_fingerprint(source);parents.append(key)
        approve(store.data_root,key,source_payload(source))
    approve(store.data_root,event_review_id(event,sources),event_review_payload(event,sources),parents=parents)
    # These fixtures model independently verified historical input availability.
    path=store.data_root/"integrity/eligibility.json";state=json.loads(path.read_text())
    for key in [*parents,event_review_id(event,sources)]:state["artifacts"][key]["origin"]="verified_point_in_time"
    path.write_text(json.dumps(state))


def approve_public_event(store,event_id):
    from openinvest.services.event_replication import publication_id,publication_payload
    event=store.get_event(event_id,purpose="audit");sources=store.get_sources(event_id,purpose="audit")
    approve(store.data_root,publication_id(event,sources),publication_payload(event,sources))


def reviewed_storage_contracts(monkeypatch):
    """Keep storage/ranking fixtures qualified; unknown-source tests never use this."""
    from openinvest.db.event_store import EventStore
    from openinvest.services.source_reporting import from_item,source_fingerprint,source_payload
    upsert=EventStore.upsert_event;add=EventStore.add_source;write=EventStore.write_news_observation
    selection=EventStore.normalization_input
    def reviewed_selection(self,item):
        source=from_item(item)
        approve(self.data_root,'source_report:'+source_fingerprint(source),source_payload(source))
        return selection(self,item)
    def reviewed_upsert(self,*args,**kwargs):
        result=upsert(self,*args,**kwargs);approve_event_reporting(self,result[1]);return result
    def reviewed_add(self,event_id,*args,**kwargs):
        result=add(self,event_id,*args,**kwargs);approve_event_reporting(self,event_id);return result
    def reviewed_write(self,event,raw_item=None,**kwargs):
        if raw_item:
            source=from_item(raw_item)
            approve(self.data_root,"source_report:"+source_fingerprint(source),source_payload(source))
        return write(self,event,raw_item,**kwargs)
    monkeypatch.setattr(EventStore,"upsert_event",reviewed_upsert)
    monkeypatch.setattr(EventStore,"add_source",reviewed_add)
    monkeypatch.setattr(EventStore,"write_news_observation",reviewed_write)
    monkeypatch.setattr(EventStore,'normalization_input',reviewed_selection)


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
