"""Synthetic client confirmations resolve exactly their own fund NAV projection."""
from __future__ import annotations

from copy import deepcopy
import json

import pytest

from openinvest.core.evidence_eligibility import digest
from openinvest.core.memory_store import MemoryStore
from openinvest.core.portfolio_manager import PortfolioManager
from openinvest.services.estimate_resolution import classify_estimates


FUND = "FUND:123456"
MON, TUE = "2026-01-05", "2026-01-06"


def _estimate(order_id="synthetic-order-1", *, amount=1000, day=MON, planned=MON, symbol=FUND, purpose="fund_nav_projection", nav=2):
    identity = {"symbol": symbol, "date": day, "kind": "tracking_estimate",
                "plan": {"order_id": order_id, "amount": amount, "nav_date_planned": planned, "purpose": purpose}}
    payload = {"estimate_id": digest(identity), "identity": identity,
               "quote": {"nav": nav, "nav_date": day, "projected_quantity_if_fee_zero": 500,
                         "reported_fee": None, "fee_observed": False, "execution_observed": False},
               "recorded_at": "2026-01-06T14:00:00+00:00", "execution_observed": False,
               "actual_fee": None, "actual_quantity": None, "allowed_uses": ["audit"],
               "purpose": "tracking_projection_not_confirmed_trade", "portfolio_mutated": False,
               "cash_flow_recorded": False}
    return {"schema": 1, "payload": payload, "sha256": digest(payload)}


def _confirmed(**changes):
    # This fixture is an explicit synthetic client declaration, not a relabeling
    # of an auto-confirmed/public-NAV historical record.
    return {"action": "fund_order_confirmed", "source": "skill_cli", "symbol": FUND,
            "order_id": "synthetic-order-1", "amount": 1000, "fee": 5, "price": 2.5, "units": 398,
            "units_effect": 398, "cash_effect": 0, "external": False, "currency": "CNY",
            "nav_date": MON, "trade_date": TUE, "confirmation_basis": "client_reported_nav_and_fee",
            "evidence_basis": "client_reported_not_independently_verified", "execution_domain": "client_reported",
            **changes}


def _placed(**changes):
    return {"action": "fund_order_placed", "source": "skill_cli", "symbol": FUND, "amount": 1000,
            "order_id": "synthetic-order-1", "nav_date": MON, "trade_date": MON, "currency": "CNY",
            "execution_domain": "client_reported", **changes}


def test_actual_confirmation_can_differ_from_public_nav_and_fee_without_rewriting_the_projection():
    estimate, confirmation = _estimate(nav=2), _confirmed()
    original_estimate, original_history = deepcopy(estimate), deepcopy(confirmation)
    result = classify_estimates([estimate], [_placed(), confirmation])
    assert result["status"] == "ok" and result["pending"] == [] and result["unresolved"] == []
    receipt, = result["resolved"]
    assert (receipt["actual_nav"], receipt["actual_fee"], receipt["actual_quantity"]) == (2.5, 5, 398)
    assert receipt["receipt_sha256"] == estimate["sha256"]
    assert receipt["resolution_history_sha256"] == digest(confirmation)
    assert receipt["resolution_history_seq"] == 1 and receipt["order_id"] == "synthetic-order-1"
    assert receipt["real_execution_eligible"] is True and receipt["independent_broker_verification"] is False
    assert receipt["original_projection_retained"] is True
    assert estimate == original_estimate and confirmation == original_history
    assert estimate["payload"]["actual_quantity"] is None and estimate["payload"]["quote"]["nav"] == 2


def test_explicit_actual_zero_fee_is_valid_but_an_unknown_fee_is_not():
    estimate = _estimate()
    row = _confirmed(fee=0, price=2, units=500, units_effect=500)
    result = classify_estimates([estimate], [row])
    assert result["pending"] == [] and result["resolved"][0]["actual_fee"] == 0
    for unknown in (None, "unknown", True):
        bad = classify_estimates([estimate], [{**row, "fee": unknown}])
        assert bad["pending"] == [estimate] and bad["resolved"] == []
        assert bad["unresolved"][0]["reason"] == "actual_fee_unknown_or_invalid"
    del row["fee"]
    assert classify_estimates([estimate], [row])["resolved"] == []


def test_only_the_exact_order_is_resolved_while_dca_balance_and_other_fund_estimates_stay_pending():
    matching, other, dca = _estimate(), _estimate("synthetic-order-2"), _estimate(purpose="tracking_projection")
    balance = {"action": "balance_observation", "order_id": "synthetic-order-2", "symbol": FUND,
               "reported_quantity": 500, "evidence_basis": "client_reported_balance_observation"}
    result = classify_estimates([other, matching, dca], [_confirmed(), balance])
    assert result["pending"][0] is other and result["pending"][1] is dca
    assert len(result["resolved"]) == 1 and result["resolved"][0]["estimate_id"] == matching["payload"]["estimate_id"]
    assert {row["reason"] for row in result["unresolved"]} == {
        "actual_order_confirmation_missing", "estimate_kind_not_resolvable_by_fund_confirmation"}


def test_multiple_public_nav_versions_of_the_same_order_resolve_to_the_same_actual_confirmation():
    first, second = _estimate(nav=2), _estimate(nav=2.2, day=TUE)
    result = classify_estimates([first, second], [_confirmed()])
    assert result["pending"] == [] and len(result["resolved"]) == 2
    assert {row["resolution_history_sha256"] for row in result["resolved"]} == {digest(_confirmed())}
    assert len({row["receipt_sha256"] for row in result["resolved"]}) == 2


def test_actual_shifted_nav_day_must_still_bind_the_original_planned_order_date():
    estimate = _estimate(day=TUE)
    row = _confirmed(nav_date=TUE, nav_date_planned=MON, trade_date=TUE)
    result = classify_estimates([estimate], [row])
    assert result["pending"] == [] and result["resolved"][0]["nav_date"] == TUE
    del row["nav_date_planned"]
    assert classify_estimates([estimate], [row])["unresolved"][0]["reason"] == "confirmation_order_dates_mismatch"


@pytest.mark.parametrize("changes, reason", [
    ({"order_id": "another-order"}, "actual_order_confirmation_missing"),
    ({"symbol": "FUND:654321"}, "confirmation_symbol_mismatch"),
    ({"amount": 999}, "confirmation_amount_mismatch"),
    ({"fee": 1000}, "actual_fee_unknown_or_invalid"),
    ({"fee": -1}, "actual_fee_unknown_or_invalid"),
    ({"fee": float("nan")}, "actual_fee_unknown_or_invalid"),
    ({"price": 0}, "actual_nav_or_quantity_invalid"),
    ({"price": float("inf")}, "actual_nav_or_quantity_invalid"),
    ({"units": 0, "units_effect": 0}, "actual_nav_or_quantity_invalid"),
    ({"units": 400, "units_effect": 400}, "confirmation_quantity_arithmetic_mismatch"),
    ({"units_effect": 399}, "confirmation_quantity_effect_mismatch"),
    ({"nav_date": "2026-01-04"}, "confirmation_order_dates_mismatch"),
    ({"trade_date": "2026-01-04"}, "confirmation_order_dates_mismatch"),
    ({"trade_date": "bad"}, "confirmation_order_dates_mismatch"),
    ({"currency": "USD"}, "confirmation_currency_mismatch_or_unknown"),
    ({"cash_effect": -5}, "confirmation_cash_effect_invalid"),
    ({"external": True}, "confirmation_cash_effect_invalid"),
    ({"status": "planned"}, "confirmation_status_not_executed"),
    ({"confirmation_basis": None}, "explicit_actual_nav_and_fee_confirmation_missing"),
    ({"execution_domain": "unknown"}, "confirmation_domain_unknown_or_unqualified"),
    ({"execution_domain": None}, "confirmation_domain_unknown_or_unqualified"),
    ({"execution_domain": "broker_unknown"}, "confirmation_domain_unknown_or_unqualified"),
    ({"execution_domain": "invented-live-domain"}, "confirmation_domain_unknown_or_unqualified"),
    ({"source": "fund_auto_confirm"}, "confirmation_source_or_domain_unqualified"),
    ({"evidence_basis": "nav_calculated_without_execution_confirmation"}, "confirmation_source_or_domain_unqualified"),
    ({"source": "paper_autopilot", "execution_domain": "paper_simulation"}, "confirmation_source_or_domain_unqualified"),
])
def test_invalid_or_unqualified_confirmation_never_resolves_the_projection(changes, reason):
    estimate = _estimate()
    row = _confirmed(**changes)
    original = repr(row)
    result = classify_estimates([estimate], [row])
    assert result["pending"] == [estimate] and result["resolved"] == []
    assert result["unresolved"][0]["reason"] == reason
    assert repr(row) == original


@pytest.mark.parametrize("same", [True, False])
def test_duplicate_and_conflicting_confirmations_remain_pending(same):
    first = _confirmed()
    second = deepcopy(first) if same else _confirmed(price=2, units=497.5, units_effect=497.5)
    result = classify_estimates([_estimate()], [first, second])
    assert result["resolved"] == [] and len(result["pending"]) == 1
    assert result["unresolved"][0]["reason"] == "duplicate_or_conflicting_order_confirmations"


def test_cancelled_order_cannot_be_resolved_as_a_fill_even_with_a_contradictory_confirmation():
    cancel = {"action": "fund_order_cancelled", "order_id": "synthetic-order-1", "symbol": FUND}
    for rows in ([cancel], [cancel, _confirmed()]):
        result = classify_estimates([_estimate()], rows)
        assert result["resolved"] == [] and len(result["pending"]) == 1
        assert result["unresolved"][0]["reason"] == "order_cancelled_not_resolved_as_a_fill"


@pytest.mark.parametrize("changes", [{"amount": 999}, {"symbol": "FUND:654321"}, {"nav_date": TUE}, {"currency": "USD"}])
def test_conflicting_placement_cannot_be_overridden_by_a_confirmation_label(changes):
    result = classify_estimates([_estimate()], [_placed(**changes), _confirmed()])
    assert result["resolved"] == [] and result["unresolved"][0]["reason"] == "placed_order_identity_conflict"


@pytest.mark.parametrize("domain", ["paper_simulation", "broker_paper"])
def test_explicit_client_confirmation_in_a_paper_book_is_resolved_only_as_paper(domain):
    row = _confirmed(execution_domain=domain)
    result = classify_estimates([_estimate()], [_placed(execution_domain=domain), row])
    resolved, = result["resolved"]
    assert result["pending"] == [] and resolved["paper_execution"] is True
    assert resolved["execution_domain"] == domain and resolved["real_execution_eligible"] is False
    assert row["execution_domain"] == domain  # Paper evidence is retained, never recast as real.


def test_a_paper_confirmation_cannot_resolve_a_differently_scoped_real_order():
    result = classify_estimates([_estimate()], [_placed(), _confirmed(execution_domain="paper_simulation")])
    assert result["resolved"] == []
    assert result["unresolved"][0]["reason"] == "placed_and_confirmed_execution_domains_conflict"


def test_an_invented_placement_domain_cannot_be_normalized_into_a_qualified_real_order():
    result = classify_estimates([_estimate()], [_placed(execution_domain="invented-domain"), _confirmed()])
    assert result["resolved"] == []
    assert result["unresolved"][0]["reason"] == "placed_and_confirmed_execution_domains_conflict"


def test_duplicate_placement_cash_events_do_not_get_cleared_by_a_single_confirmation():
    result = classify_estimates([_estimate()], [_placed(), _placed(), _confirmed()])
    assert result["resolved"] == []
    assert result["unresolved"][0]["reason"] == "duplicate_or_conflicting_order_placements"


def test_an_explicitly_different_projection_amount_currency_cannot_match_a_cny_confirmation():
    estimate = _estimate()
    estimate["payload"]["identity"]["plan"]["currency"] = "USD"
    estimate["payload"]["estimate_id"] = digest(estimate["payload"]["identity"])
    estimate["sha256"] = digest(estimate["payload"])
    result = classify_estimates([estimate], [_confirmed()])
    assert result["resolved"] == []
    assert result["unresolved"][0]["reason"] == "fund_projection_native_currency_unsupported"


@pytest.mark.parametrize("domain", ["broker_live", "broker_paper"])
def test_broker_observed_confirmation_keeps_its_original_broker_domain(domain):
    row = _confirmed(source="ibkr_autopilot", evidence_basis="broker_observed_fill", execution_domain=domain,
                     source_reference={"exec_id": "synthetic-execution", "account": "synthetic-account"})
    result = classify_estimates([_estimate()], [row])
    resolved, = result["resolved"]
    assert resolved["execution_domain"] == domain
    assert resolved["real_execution_eligible"] is (domain == "broker_live")


def test_six_decimal_quantity_storage_precision_accepts_valid_rounding_without_accepting_a_different_quantity():
    # Net 1 / NAV 3 = one third; PM stores six decimals.
    estimate = _estimate(amount=1)
    row = _confirmed(amount=1, fee=0, price=3, units=0.333333, units_effect=0.333333)
    assert len(classify_estimates([estimate], [row])["resolved"]) == 1
    row.update(units=0.333332, units_effect=0.333332)
    assert classify_estimates([estimate], [row])["resolved"] == []


def test_legacy_automatic_confirmed_status_without_new_basis_is_not_actual_execution_evidence():
    row = _confirmed(source="fund_auto_confirm", status="confirmed")
    del row["confirmation_basis"]
    original = deepcopy(row)
    result = classify_estimates([_estimate()], [row])
    assert result["resolved"] == [] and row == original
    assert result["unresolved"][0]["reason"] == "explicit_actual_nav_and_fee_confirmation_missing"


def test_invalid_estimate_receipt_cannot_be_treated_as_an_empty_or_resolved_projection():
    good, corrupt = _estimate(), _estimate("synthetic-order-2")
    corrupt["sha256"] = "0" * 64
    result = classify_estimates([good, corrupt], [_confirmed()])
    assert result["status"] == "unavailable" and result["resolved"] == []
    assert result["pending"][0] is good and result["pending"][1] is corrupt
    assert any(row["reason"] == "invalid_estimate_receipt" for row in result["unresolved"])


def test_invalid_history_is_unavailable_and_empty_inputs_are_supported():
    estimate = _estimate()
    result = classify_estimates([estimate], [None])
    assert result["status"] == "unavailable" and result["pending"] == [estimate] and result["resolved"] == []
    assert classify_estimates([], [])["pending"] == classify_estimates([], [])["resolved"] == []


def test_real_pm_confirmation_and_terminal_order_pruning_leave_a_content_bound_history_resolution(tmp_path, monkeypatch):
    import openinvest.utils.eastmoney_fund as funds
    from openinvest.services.ledger_evidence import account_source_status
    monkeypatch.setattr(funds, "fetch_fund_profile", lambda *a, **k: {"基金简称": "合成测试基金"})
    monkeypatch.setattr(funds, "fetch_fund_nav_history", lambda *a, **k: [(MON, 2.0)])
    store = MemoryStore(tmp_path / "memory")
    store.write("user", "user", {"display_name": "Synthetic"}, "")
    store.write("strategy", "strategy", {"target_assets": []}, "")
    store.write("portfolio", "state", {"schema_version": 2, "cash": {"CNY": 10000}, "holdings": []}, "")
    pm = PortfolioManager(store)
    order_id = pm.place_fund_order(FUND, 1000, order_ts=MON + "T10:00:00")["order_id"]
    assert pm.confirm_fund_order(order_id)["status"] == "pending_execution_confirmation"
    path, = (store.root / ".evidence/tracking_estimates").glob("*.json")
    original = path.read_bytes()
    envelope = json.loads(original)
    assert classify_estimates([envelope], store.read_history())["resolved"] == []
    before_grade = account_source_status(pm)
    assert before_grade["pending_estimates"] == 1
    assert not before_grade["historical_execution_eligible"]
    assert before_grade["holdings"][FUND]["current_quantity"] == "estimated_or_proxy_unreconciled"
    actual = pm.confirm_fund_order(order_id, nav=2.5, fee=5, confirm_date=TUE)
    assert actual["status"] == "ok" and actual["units"] == 398
    with pm.with_portfolio_tx() as portfolio:
        portfolio["holdings"][0]["extra"]["pending_orders"] = []
    before_portfolio, before_history = store.path_of("portfolio").read_bytes(), store.read_history()
    result = classify_estimates([envelope], before_history)
    resolved, = result["resolved"]
    assert result["pending"] == [] and resolved["order_id"] == order_id
    assert resolved["actual_nav"] == 2.5 and resolved["actual_fee"] == 5 and resolved["actual_quantity"] == 398
    assert resolved["receipt_sha256"] == envelope["sha256"]
    assert resolved["resolution_history_sha256"] == digest(before_history[-1])
    assert path.read_bytes() == original and store.path_of("portfolio").read_bytes() == before_portfolio
    assert store.read_history() == before_history and PortfolioManager(store).cash_amount("CNY") == 9000
    after_grade = account_source_status(PortfolioManager(store))
    assert after_grade["pending_estimates"] == 0 and after_grade["retained_estimates"] == 1
    assert after_grade["resolved_estimates"] == result["resolved"]
    assert after_grade["historical_execution_eligible"]
    assert after_grade["holdings"][FUND]["current_quantity"] == "client_recorded"
    assert after_grade["content_sha256"] != before_grade["content_sha256"]
    # Removing the explicit fee evidence must revoke the resolution on a fresh
    # read, even when the retained projection and current holding are unchanged.
    original_history_bytes = (store.root / "portfolio_history.jsonl").read_bytes()
    invalid_history = deepcopy(before_history)
    invalid_history[-1].pop("fee")
    (store.root / "portfolio_history.jsonl").write_text("".join(json.dumps(row) + "\n" for row in invalid_history))
    invalid_grade = account_source_status(PortfolioManager(store))
    assert invalid_grade["pending_estimates"] == 1 and not invalid_grade["historical_execution_eligible"]
    assert invalid_grade["content_sha256"] != after_grade["content_sha256"]
    assert path.read_bytes() == original
    (store.root / "portfolio_history.jsonl").write_bytes(original_history_bytes)
