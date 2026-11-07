"""Source acquisition is distinct from truth, model judgment and learning permission."""
import json
from datetime import datetime,timezone

from openinvest.core.evidence_eligibility import Eligibility
from openinvest.services import agent_queue as q
from openinvest.services.source_reporting import reporting_use,from_input,source_payload,source_fingerprint
from tests.test_news_source_snapshots import source_context,captured_sina,news
from tests.test_evidence_eligibility import approve


def row(record):return {**from_input(record),"provenance_json":json.dumps({**record.get("raw_meta",{}),"source_snapshot_id":record["source_snapshot_id"]})}


def test_sealing_arbitrary_adapter_input_or_a_backend_label_does_not_authenticate_it(source_context):
    q.enqueue_news([news()]);record=q.pending_news_items()[0]
    source=row(record)
    source["provenance_json"]=json.dumps({**record["raw_meta"],"source_snapshot_id":record["source_snapshot_id"],"source_origin":"backend_collected"})
    assert not reporting_use(source_context,source)["allowed"]
    assert q.pending_news_items()  # Audit/relevance work is preserved.


def test_captured_original_api_item_can_be_a_report_but_grants_no_truth_or_learning(source_context,monkeypatch):
    record=captured_sina(monkeypatch);source=row(record)
    state=reporting_use(source_context,source)
    assert state["allowed"] and state["kind"]=="captured_api_response"
    assert not state["claims_verified"] and not state["learning_granted"]
    # Omitting a parent from the mutable source metadata cannot defeat revocation.
    source["provenance_json"]=json.dumps({"source_snapshot_id":record["source_snapshot_id"]})
    Eligibility(source_context).revoke("source_response:"+record["raw_meta"]["response_sha256"],"synthetic invalid acquisition")
    assert not reporting_use(source_context,source)["allowed"]


def test_reviewed_valid_source_remains_usable_even_when_its_judgment_was_wrong(source_context):
    source={"src_name":"legacy","url":"https://example.invalid/story","title":"Original report","snippet":"Original report", 
            "fetched_at":datetime.now(timezone.utc).isoformat()}
    assert not reporting_use(source_context,source)["allowed"]
    approve(source_context,"source_report:"+source_fingerprint(source),source_payload(source),outcome="wrong")
    assert reporting_use(source_context,source)["allowed"]
    source["snippet"]="Different invented text"
    assert not reporting_use(source_context,source)["allowed"]
