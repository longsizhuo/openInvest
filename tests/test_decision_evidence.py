import json
from datetime import timedelta

import pytest

from openinvest.core.decision_evidence import prepare_context, load_context, utc_now
from openinvest.core.config import set_config_override


def test_concurrent_prepare_is_request_bound(tmp_path):
    a = prepare_context(tmp_path, "SYNTHETIC", {"price": 100})
    b = prepare_context(tmp_path, "SYNTHETIC", {"price": 200})
    assert a["context_id"] != b["context_id"]
    assert load_context(tmp_path, a["context_id"], "SYNTHETIC")["inputs"]["price"] == 100
    assert load_context(tmp_path, b["context_id"], "SYNTHETIC")["inputs"]["price"] == 200
    with pytest.raises(ValueError):
        load_context(tmp_path, a["context_id"], "OTHER")
    with pytest.raises(ValueError):
        load_context(tmp_path, "", "SYNTHETIC")


def test_context_tamper_expiry_and_constraint_change(tmp_path):
    (tmp_path / "portfolio.md").write_text("original portfolio")
    a = prepare_context(tmp_path, "SYNTHETIC", {"price": 100})
    with pytest.raises(ValueError, match="expired"):
        load_context(tmp_path, a["context_id"], "SYNTHETIC", now=utc_now() + timedelta(hours=7))
    (tmp_path / "portfolio.md").write_text("changed portfolio")
    with pytest.raises(ValueError, match="constraints changed"):
        load_context(tmp_path, a["context_id"], "SYNTHETIC")
    (tmp_path / "portfolio.md").write_text("original portfolio")
    set_config_override({"verdict": {"risk_profile": "aggressive"}})
    with pytest.raises(ValueError, match="configuration changed"):
        load_context(tmp_path, a["context_id"], "SYNTHETIC")
    path = tmp_path / ".evidence/contexts" / (a["context_id"] + ".json")
    record = json.loads(path.read_text()); record["payload"]["inputs"]["price"] = 999
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="integrity"):
        load_context(tmp_path, a["context_id"], "SYNTHETIC")


def test_submission_replay_does_not_duplicate_decision_or_intervention():
    from tests.integrity_helpers import context_id
    from openinvest.core.memory_store import MemoryStore
    from openinvest.core.runner.coordinator import save_committee_transcript
    cid=context_id("SYNTHETIC")
    raw="=== CIO ===\nVERDICT: TRIM\nCONFIDENCE: 0.7\nTRIM_REASON: bearish\nREENTRY_PRICE: 120\nSUGGESTED_ALLOC_CNY: -1000"
    first=save_committee_transcript("SYNTHETIC",raw,context_id=cid)
    store=MemoryStore(); ledger=store.root/".dreams/interventions.jsonl"
    before=ledger.read_bytes() if ledger.exists() else b""
    assert before==b""  # An unqualified exit is not a successful money-saving intervention.
    second=save_committee_transcript("SYNTHETIC",raw,context_id=cid)
    assert second["replayed"] and second["decision_id"] == first["decision_id"]
    assert second["saved"] == first["saved"]
    assert len(list((store.root/".evidence/decisions").glob("*.json"))) == 1
    assert (ledger.read_bytes() if ledger.exists() else b"") == before
    with pytest.raises(ValueError,match="different content"):
        save_committee_transcript("SYNTHETIC",raw.replace("0.7","0.8"),context_id=cid)


def test_submit_uses_its_own_prepare_not_the_latest_symbol_snapshot():
    from tests.integrity_helpers import context_id
    from openinvest.core.runner.coordinator import save_committee_transcript
    first=context_id("SYNTHETIC",current_price=100)
    second=context_id("SYNTHETIC",current_price=200)
    raw="=== CIO ===\nVERDICT: TRIM\nCONFIDENCE: 0.7\nTRIM_REASON: bearish\nEXIT_INTENT: tactical_reentry\nREENTRY_PRICE: 150\nSUGGESTED_ALLOC_CNY: -1000"
    assert save_committee_transcript("SYNTHETIC",raw,context_id=first)["verdict"]["verdict"] == "HOLD"
    assert save_committee_transcript("SYNTHETIC",raw,context_id=second)["verdict"]["verdict"] == "TRIM"


@pytest.fixture(autouse=True)
def _synthetic_review_for_non_review_contract(monkeypatch):
    # These tests isolate storage/cache/format contracts; dedicated review-flow
    # tests exercise actual verification, unavailable capability and failures.
    monkeypatch.setattr("openinvest.services.reasoning_review.review_once",
        lambda *args,**kwargs:{"status":"passed","origin":"synthetic_unit_fixture"})
