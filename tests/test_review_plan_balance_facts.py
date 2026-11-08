"""Saved review conditions bind their consumed balance facts, not today's head."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

from openinvest.core.decision_evidence import prepare_context, record_decision
from openinvest.core.evidence_eligibility import Eligibility, digest
from openinvest.core.memory_store import MemoryStore
from openinvest.core.portfolio_manager import PortfolioManager
from openinvest.services.balance_observations import record_observation, validate_parent_facts
from openinvest.services.ledger_evidence import account_source_status
from openinvest.services.review_plans import plan_brief, plan_qualification, update_plan


SYMBOL = "SYNTHETIC"
TARGET = 876.54321


@pytest.fixture
def bound_plan(tmp_path, monkeypatch, request):
    monkeypatch.setattr("openinvest.core.decision_evidence.code_version", lambda: "synthetic-version")
    store = MemoryStore(tmp_path / "memory")
    store.write("user", "user", {"display_name": "Synthetic"}, "")
    store.write("strategy", "strategy", {"target_assets": []}, "")
    store.write("portfolio", "state", {"schema_version": 2, "updated": "2020-01-01T00:00:00+00:00",
        "cash": {"CNY": 1000}, "holdings": [{"symbol": SYMBOL, "kind": "etf", "units": 10,
        "avg_cost": 5, "cost_currency": "CNY", "unit_label": "股", "proxy_kind": "direct"}]}, "")
    pm = PortfolioManager(store)
    now = datetime.now(timezone.utc)
    reported = getattr(request, "param", {"reported_value": 50, "value_currency": "CNY"})
    receipt = record_observation(pm, SYMBOL, as_of=(now - timedelta(minutes=1)).isoformat(),
        source_text="原始合成账户声明，仅按明确报告字段记录。", source_locator={"parent_ids": ["source:original-statement"]},
        idempotency_key="original-plan-balance", **reported)
    ledger = account_source_status(pm)
    for field in receipt["fields"]:
        fact = ledger["holdings"][SYMBOL]["balance_observations"][field]
        assert ledger["holdings"][SYMBOL][field] == "client_observed_balance"
        assert fact["observation_sha256"] == receipt["sha256"]
    context = prepare_context(store.root, SYMBOL, {"ledger_evidence": ledger, "event_brief": ""}, model="synthetic")
    spec = {"conditions": [{"metric": "quote_price", "op": ">=", "value": TARGET, "unit": "USD/unit"}],
            "expires_at": (now + timedelta(days=7)).isoformat(), "logic": "all"}
    verdict = {"verdict": "HOLD", "decision_status": "READY", "reasoning_review": {"status": "passed"},
               "review_plan": spec}
    decision = record_decision(store.root, SYMBOL, {}, verdict, {}, context_id=context["context_id"])
    saved = update_plan(store, SYMBOL, spec, evidence_id=decision["decision_id"], now=now)
    assert plan_qualification(store, SYMBOL, saved)["allowed"] is True
    return {"pm": pm, "store": store, "receipt": receipt, "ledger": ledger, "context": context, "plan": saved}


def _receipt_path(bound):
    return bound["store"].root / ".evidence/account_observations" / (bound["receipt"]["observation_id"] + ".json")


def _reseal(path, change):
    envelope = json.loads(path.read_text())
    change(envelope["payload"])
    envelope["sha256"] = digest(envelope["payload"])
    path.write_text(json.dumps(envelope))


@pytest.mark.parametrize("bound_plan", [
    {"reported_quantity": 10, "quantity_unit": "股", "native_instrument": SYMBOL},
    {"reported_cost": 5, "cost_currency": "CNY", "quantity_unit": "股", "native_instrument": SYMBOL},
    {"reported_quantity": 10, "quantity_unit": "股", "native_instrument": SYMBOL,
     "reported_cost": 5, "cost_currency": "CNY", "reported_value": 50, "value_currency": "CNY"},
], indirect=True)
def test_plan_binds_quantity_cost_and_value_as_independent_original_fields_without_inference(bound_plan):
    bound = bound_plan
    assert validate_parent_facts(bound["store"], bound["ledger"])["allowed"]
    assert plan_qualification(bound["store"], SYMBOL, bound["plan"])["historical_condition_only"]
    before = {str(path): path.read_bytes() for path in bound["store"].data_root.rglob("*") if path.is_file()}
    assert validate_parent_facts(bound["store"], bound["ledger"])["historical_execution_eligible"] is False
    after = {str(path): path.read_bytes() for path in bound["store"].data_root.rglob("*") if path.is_file()}
    assert after == before


@pytest.mark.parametrize("damage", ["corrupt", "delete", "resealed_field", "resealed_source"])
def test_plan_rejects_a_damaged_deleted_or_changed_consumed_balance_receipt_without_moving_its_condition(bound_plan, damage):
    bound = bound_plan
    store, saved = bound["store"], bound["plan"]
    plan_path = store.root / ".evidence/review_plans" / (saved["plan_id"] + ".json")
    original_plan = plan_path.read_bytes()
    path = _receipt_path(bound)
    if damage == "corrupt":
        path.write_text('{"broken":true}')
    elif damage == "delete":
        path.unlink()
    elif damage == "resealed_field":
        _reseal(path, lambda receipt: receipt["fields"]["current_value"].update(value=999))
    else:
        _reseal(path, lambda receipt: receipt.update(observed_at=datetime.now(timezone.utc).isoformat()))
    assert account_source_status(bound["pm"])["status"] == "unavailable"
    assert validate_parent_facts(store, bound["ledger"])["allowed"] is False
    assert plan_qualification(store, SYMBOL, saved)["allowed"] is False
    assert str(TARGET) not in plan_brief(store, SYMBOL)
    assert store.state_get("review_plan_" + SYMBOL)["spec"]["conditions"][0]["value"] == TARGET
    assert plan_path.read_bytes() == original_plan


@pytest.mark.parametrize("ancestor", ["observation", "source_locator", "source_text"])
def test_plan_rechecks_the_consumed_balance_fact_and_all_captured_source_revocations(bound_plan, ancestor):
    bound = bound_plan
    receipt, store = bound["receipt"], bound["store"]
    artifact = {"observation": "account_observation:" + receipt["observation_id"],
                "source_locator": "source:original-statement",
                "source_text": "client_source:" + receipt["source_text_sha256"]}[ancestor]
    original = _receipt_path(bound).read_bytes()
    Eligibility(store.data_root).revoke(artifact, "synthetic source withdrawal")
    assert not validate_parent_facts(store, bound["ledger"])["allowed"]
    assert not plan_qualification(store, SYMBOL, bound["plan"])["allowed"]
    assert _receipt_path(bound).read_bytes() == original


@pytest.mark.parametrize("change", ["trade", "new_balance", "unrelated_new_receipt_corrupt", "unrelated_new_source_revoked"])
def test_later_account_changes_do_not_rewrite_or_disqualify_the_intact_historical_parent_fact(bound_plan, change):
    bound = bound_plan
    pm, store = bound["pm"], bound["store"]
    original = _receipt_path(bound).read_bytes()
    if change == "trade":
        PortfolioManager(store).buy(SYMBOL, 1, 5, source="mcp")
    else:
        receipt = record_observation(pm, SYMBOL, as_of=datetime.now(timezone.utc).isoformat(),
            source_text="后续独立合成声明：市值60元。", source_locator={"parent_ids": ["source:independent-later-statement"]},
            idempotency_key="independent-later-balance", reported_value=60, value_currency="CNY")
        if change == "unrelated_new_receipt_corrupt":
            (store.root / ".evidence/account_observations" / (receipt["observation_id"] + ".json")).write_text('{"broken":true}')
        elif change == "unrelated_new_source_revoked":
            Eligibility(store.data_root).revoke("source:independent-later-statement", "synthetic later source withdrawal")
    assert validate_parent_facts(store, bound["ledger"])["allowed"] is True
    qualified = plan_qualification(store, SYMBOL, bound["plan"])
    assert qualified["allowed"] is True and qualified["historical_condition_only"] is True
    assert str(TARGET) in plan_brief(store, SYMBOL)
    assert _receipt_path(bound).read_bytes() == original
    assert bound["plan"]["execution_authorized"] is False
    policy = Eligibility(store.data_root)
    payload = json.loads(original)["payload"]
    assert all(not policy.allows("account_observation:" + bound["receipt"]["observation_id"], payload, use)
               for use in ("advice", "learn", "evaluate", "publish"))


@pytest.mark.parametrize("damage", ["wrong_field_value", "missing_receipt_hash", "parent_without_field", "wrong_field"])
def test_plan_consumed_field_binding_must_match_its_original_receipt_even_when_context_is_resealed(bound_plan, damage):
    bound = bound_plan
    path = bound["store"].root / ".evidence/contexts" / (bound["context"]["context_id"] + ".json")

    def change(context):
        ledger = context["inputs"]["ledger_evidence"]
        grade = ledger["holdings"][SYMBOL]
        fact = grade["balance_observations"]["current_value"]
        if damage == "wrong_field_value":
            fact["reported"]["value"] = 999
        elif damage == "missing_receipt_hash":
            fact.pop("observation_sha256")
        elif damage == "parent_without_field":
            ledger["holdings"] = {}
        else:
            grade["cost_basis"] = "client_observed_balance"
            grade["balance_observations"]["cost_basis"] = fact

    _reseal(path, change)
    assert not plan_qualification(bound["store"], SYMBOL, bound["plan"])["allowed"]


@pytest.mark.parametrize("damage", ["missing_artifact", "wrong_artifact_hash", "changed_source_parents"])
def test_plan_requires_the_original_balance_audit_lineage_without_granting_learning_eligibility(bound_plan, damage):
    bound = bound_plan
    path = bound["store"].data_root / "integrity/eligibility.json"
    registry = json.loads(path.read_text())
    artifact_id = "account_observation:" + bound["receipt"]["observation_id"]
    if damage == "missing_artifact":
        registry["artifacts"].pop(artifact_id)
    elif damage == "wrong_artifact_hash":
        registry["artifacts"][artifact_id]["sha256"] = "0" * 64
    else:
        registry["artifacts"][artifact_id]["parents"] = ["source:changed-parent"]
    path.write_text(json.dumps(registry))
    assert not validate_parent_facts(bound["store"], bound["ledger"])["allowed"]
    assert not plan_qualification(bound["store"], SYMBOL, bound["plan"])["allowed"]
