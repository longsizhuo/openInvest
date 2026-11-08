"""Client declarations anchor independent balance fields; they never reconstruct fills."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json

import pytest

from openinvest.core.evidence_eligibility import Eligibility, digest
from openinvest.core.memory_store import MemoryStore
from openinvest.core.portfolio_manager import PortfolioManager
from openinvest.services.balance_observations import AUTHORITY, current_confirmations, record_observation
from openinvest.services.ledger_evidence import record_tracking_estimate


SYMBOL = "SYNTHETIC"


@pytest.fixture
def pm(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    store.write("user", "user", {"display_name": "Synthetic"}, "")
    store.write("strategy", "strategy", {"target_assets": []}, "")
    store.write("portfolio", "state", {"schema_version": 2, "updated": "2020-01-01T00:00:00+00:00",
        "cash": {"CNY": 1000}, "holdings": [{"symbol": SYMBOL, "kind": "etf", "units": 10,
        "avg_cost": 5, "cost_currency": "CNY", "unit_label": "股", "proxy_kind": "direct"}]}, "")
    return PortfolioManager(store)


def _as_of(days=0):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _record(pm, **kwargs):
    return record_observation(pm, SYMBOL, **{
        "as_of": _as_of(), "source_text": "用户声明：当前账户余额，合成测试资料。",
        "source_locator": "synthetic:user-statement", "idempotency_key": "observation-1", **kwargs})


def _estimates(pm):
    return [json.loads(path.read_text()) for path in (pm.store.root / ".evidence/tracking_estimates").glob("*.json")]


def _confirmations(pm):
    return current_confirmations(pm, pm.store.read_history(), _estimates(pm))


def _portfolio_bytes(pm):
    return pm.store.path_of("portfolio").read_bytes()


def _reseal_heads(pm, change):
    path = pm.store.root / ".state/account_observation_heads.json"
    state = json.loads(path.read_text())["payload"]
    change(state)
    path.write_text(json.dumps({"schema": 1, "payload": state, "sha256": digest(state)}))


def test_value_only_receipt_preserves_raw_source_and_does_not_guess_quantity_or_cost(pm):
    before = _portfolio_bytes(pm)
    source = " 原始声明\n持仓市值 1234.56 元；没有份额或成本资料。 "
    receipt = _record(pm, reported_value=1234.56, value_currency="cny", source_text=source)
    assert receipt["source_authority"] == AUTHORITY
    assert receipt["request"]["source_text"] == source
    assert receipt["source_text_sha256"] == hashlib.sha256(source.encode()).hexdigest()
    assert receipt["portfolio_mutated"] is False and receipt["execution_observed"] is False
    assert receipt["cash_flow_recorded"] is False and receipt["historical_execution_eligible"] is False
    assert _portfolio_bytes(pm) == before and pm.cash_amount("CNY") == 1000
    row, = pm.store.read_history()
    assert row["action"] == "balance_observation" and row["units_effect"] == row["cash_effect"] == 0
    assert row["source_reference"]["observation_id"] == receipt["observation_id"]
    fields = _confirmations(pm)["holdings"][SYMBOL]
    assert set(fields) == {"current_value"}
    assert fields["current_value"]["reported"] == {"value": 1234.56, "currency": "CNY"}
    assert fields["current_value"]["independent_broker_verification"] is False


def test_matching_native_quantity_and_average_cost_are_absolute_without_cash_effect(pm):
    receipt = _record(pm, reported_quantity=7.25, quantity_unit="股", native_instrument=SYMBOL,
                      reported_cost=4.25, cost_currency="CNY")
    assert receipt["status"] == "current" and receipt["portfolio_mutated"] is True
    fresh = PortfolioManager(pm.store)
    assert fresh.find_holding(SYMBOL)["units"] == 7.25
    assert fresh.find_holding(SYMBOL)["avg_cost"] == 4.25 and fresh.cash_amount("CNY") == 1000
    history = pm.store.read_history()
    assert [row["action"] for row in history] == ["manual_override", "balance_observation"]
    assert history[0]["units"] == 7.25 and history[0]["cash_effect"] == 0 and history[0]["external"] is False
    assert all(row["evidence_basis"] == "client_reported_balance_observation" for row in history)
    assert receipt["request"]["cost_basis"] == "average_cost_per_native_unit"
    confirmed = _confirmations(pm)
    assert all(row["status"] == "client_reported_balance_confirmed" for row in confirmed["holdings"][SYMBOL].values())
    assert confirmed["historical_execution_eligible"] is False
    assert "account_observation:" + receipt["observation_id"] in confirmed["parent_ids"]


def test_zero_quantity_clears_only_declared_quantity_and_preserves_cost_and_cash(pm):
    _record(pm, reported_quantity=0, quantity_unit="股", native_instrument=SYMBOL)
    fresh = PortfolioManager(pm.store)
    assert fresh.find_holding(SYMBOL)["units"] == 0
    assert fresh.find_holding(SYMBOL)["avg_cost"] == 5 and fresh.cash_amount("CNY") == 1000
    assert set(_confirmations(pm)["holdings"][SYMBOL]) == {"current_quantity"}


def test_cost_only_updates_average_cost_without_changing_quantity_or_cash(pm):
    _record(pm, reported_cost=0, cost_currency="CNY", quantity_unit="股", native_instrument=SYMBOL)
    fresh = PortfolioManager(pm.store)
    assert fresh.find_holding(SYMBOL)["avg_cost"] == 0
    assert fresh.find_holding(SYMBOL)["units"] == 10 and fresh.cash_amount("CNY") == 1000
    assert [row["action"] for row in pm.store.read_history()] == ["balance_observation"]
    assert set(_confirmations(pm)["holdings"][SYMBOL]) == {"cost_basis"}


@pytest.mark.parametrize("change", [{"native_instrument": "ACTUAL:UNRELATED_FUND"}, {"quantity_unit": "份"},
                                    {"native_instrument": None}])
def test_native_instrument_and_unit_mismatch_cannot_write_real_fund_quantity_into_proxy_etf(pm, change):
    before = _portfolio_bytes(pm)
    receipt = _record(pm, **{"reported_quantity": 200, "quantity_unit": "股", "native_instrument": SYMBOL,
                            "reported_value": 500, "value_currency": "CNY", **change})
    assert _portfolio_bytes(pm) == before
    assert receipt["field_disposition"]["current_quantity"] == "native_identity_unconfirmed"
    assert receipt["fields"]["current_quantity"]["value"] == 200
    assert set(_confirmations(pm)["holdings"][SYMBOL]) == {"current_value"}


@pytest.mark.parametrize("reference,native", [("510300.SS", "FUND:110020"), ("FUND:110020", "510300.SS")])
def test_native_metadata_and_identical_share_labels_cannot_prove_a_fund_etf_quantity_mapping(pm, reference, native):
    with pm.with_portfolio_tx() as portfolio:
        portfolio["holdings"][0].update(symbol=reference, native_instrument=native, unit_label="份",
            yfinance_proxy="510300.SS", extra={"native_unit": "份", "proxy_unit": "份", "mapping": "same_unit"})
    before = _portfolio_bytes(pm)
    receipt = record_observation(pm, reference, as_of=_as_of(), source_text="原生基金/ETF份额声明。",
        source_locator="synthetic:foreign-native", idempotency_key="foreign-native",
        reported_quantity=200, quantity_unit="份", native_instrument=native,
        reported_cost=1.2, cost_currency="CNY", reported_value=240, value_currency="CNY")
    assert receipt["portfolio_mutated"] is False and _portfolio_bytes(pm) == before
    assert receipt["field_disposition"] == {"current_quantity": "recorded_only", "cost_basis": "recorded_only",
                                             "current_value": "current"}
    assert receipt["field_reasons"] == {field: "native_proxy_unit_mapping_unproven"
                                         for field in ("current_quantity", "cost_basis")}
    assert receipt["fields"]["current_quantity"]["value"] == 200
    assert receipt["fields"]["current_quantity"]["native_instrument"] == native
    assert set(_confirmations(pm)["holdings"][reference]) == {"current_value"}


def test_unproven_foreign_native_quantity_and_cost_alone_remain_recorded_only(pm):
    with pm.with_portfolio_tx() as portfolio:
        portfolio["holdings"][0].update(native_instrument="FUND:110020", unit_label="份")
    before = _portfolio_bytes(pm)
    receipt = _record(pm, reported_quantity=200, quantity_unit="份", native_instrument="FUND:110020",
                      reported_cost=1.2, cost_currency="CNY")
    assert receipt["status"] == "recorded_only" and _portfolio_bytes(pm) == before
    assert _confirmations(pm)["holdings"] == {}
    assert [row["action"] for row in pm.store.read_history()] == ["balance_observation"]


def test_actual_native_fund_balance_preserves_the_same_instrument_quantity_contract(pm):
    symbol = "FUND:110020"
    with pm.with_portfolio_tx() as portfolio:
        portfolio["holdings"][0].update(symbol=symbol, kind="fund", native_instrument=symbol,
                                         unit_label="份", proxy_kind="eastmoney_fund")
    receipt = record_observation(pm, symbol, as_of=_as_of(), source_text="原生基金份额声明。",
        source_locator="synthetic:native-fund", idempotency_key="native-fund",
        reported_quantity=200, quantity_unit="份", native_instrument=symbol, reported_cost=1.2, cost_currency="CNY")
    holding = PortfolioManager(pm.store).find_holding(symbol)
    assert receipt["status"] == "current" and receipt["portfolio_mutated"] is True
    assert holding["units"] == 200 and holding["avg_cost"] == 1.2
    assert all(row["status"] == "client_reported_balance_confirmed"
               for row in _confirmations(pm)["holdings"][symbol].values())


@pytest.mark.parametrize("change,allowed", [({}, True), ({"proxy_kind": "direct"}, False),
    ({"kind": "etf"}, False), ({"unit_label": "份"}, False), ({"cost_currency": "USD"}, False),
    ({"yfinance_proxy": "510300.SS"}, False)])
def test_only_the_existing_backend_gold_gram_mapping_can_cross_native_instrument_identity(pm, change, allowed):
    with pm.with_portfolio_tx() as portfolio:
        portfolio["holdings"][0].update({"symbol": "GC=F", "native_instrument": "BANK:GOLD", "kind": "metal",
            "unit_label": "克", "cost_currency": "CNY", "proxy_kind": "gold_cny_per_gram", "yfinance_proxy": "GC=F", **change})
    before = _portfolio_bytes(pm)
    unit = change.get("unit_label", "克")
    currency = change.get("cost_currency", "CNY")
    receipt = record_observation(pm, "GC=F", as_of=_as_of(), source_text="银行积存金克数与每克成本。",
        source_locator="synthetic:bank-gold", idempotency_key="native-gold",
        reported_quantity=7.25, quantity_unit=unit, native_instrument="BANK:GOLD",
        reported_cost=880, cost_currency=currency)
    holding = PortfolioManager(pm.store).find_holding("GC=F")
    if allowed:
        assert receipt["status"] == "current" and receipt["portfolio_mutated"] is True
        assert holding["units"] == 7.25 and holding["avg_cost"] == 880
        assert all(row["status"] == "client_reported_balance_confirmed"
                   for row in _confirmations(pm)["holdings"]["GC=F"].values())
    else:
        assert receipt["status"] == "recorded_only" and _portfolio_bytes(pm) == before
        assert set(receipt["field_reasons"].values()) == {"native_proxy_unit_mapping_unproven"}
        assert _confirmations(pm)["holdings"] == {}


def test_cost_currency_mismatch_is_independent_from_a_matching_quantity(pm):
    receipt = _record(pm, reported_quantity=8, quantity_unit="股", native_instrument=SYMBOL,
                      reported_cost=99, cost_currency="USD")
    assert receipt["field_disposition"]["cost_basis"] == "native_identity_unconfirmed"
    fresh = PortfolioManager(pm.store)
    assert fresh.find_holding(SYMBOL)["units"] == 8 and fresh.find_holding(SYMBOL)["avg_cost"] == 5
    assert set(_confirmations(pm)["holdings"][SYMBOL]) == {"current_quantity"}


def test_older_as_of_than_activity_is_historical_even_when_received_today(pm):
    pm.store.append_history({"action": "buy", "symbol": SYMBOL, "units": 1, "price": 5,
                             "ts": _as_of(days=1), "source": "dca_daily"})
    before = _portfolio_bytes(pm)
    receipt = _record(pm, as_of=_as_of(days=2), reported_quantity=20, quantity_unit="股", native_instrument=SYMBOL)
    assert receipt["status"] == "historical" and receipt["portfolio_mutated"] is False
    assert _portfolio_bytes(pm) == before and _confirmations(pm)["holdings"] == {}
    assert datetime.fromisoformat(receipt["observed_at"]) > datetime.fromisoformat(receipt["as_of"])
    assert pm.store.read_history()[0]["source"] == "dca_daily"  # Original projection is not relabeled.


def test_older_as_of_than_projection_is_historical_without_overwriting_snapshot(pm):
    record_tracking_estimate(pm.store, symbol=SYMBOL, day=datetime.now(timezone.utc).date().isoformat(),
                             plan={"amount": 100}, quote={"projected_quantity": 20})
    before = _portfolio_bytes(pm)
    receipt = _record(pm, as_of=_as_of(days=1), reported_value=50, value_currency="CNY")
    assert receipt["status"] == "historical" and _portfolio_bytes(pm) == before
    assert _confirmations(pm)["holdings"] == {}


def test_undated_legacy_activity_allows_archival_observation_but_no_current_overwrite(pm):
    (pm.store.root / "portfolio_history.jsonl").write_text(json.dumps(
        {"action": "buy", "symbol": SYMBOL, "units": 1, "price": 5}) + "\n")
    before = _portfolio_bytes(pm)
    receipt = _record(pm, reported_quantity=99, quantity_unit="股", native_instrument=SYMBOL)
    assert receipt["status"] == "historical" and _portfolio_bytes(pm) == before
    assert _confirmations(pm)["holdings"] == {}


def test_same_current_values_can_be_observed_without_changing_the_price_or_snapshot(pm):
    before = _portfolio_bytes(pm)
    receipt = _record(pm, reported_quantity=10, quantity_unit="股", native_instrument=SYMBOL,
                      reported_cost=5, cost_currency="CNY")
    assert receipt["status"] == "current" and receipt["portfolio_mutated"] is False
    assert _portfolio_bytes(pm) == before
    assert _confirmations(pm)["holdings"][SYMBOL]["current_quantity"]["status"] == "client_reported_balance_confirmed"


def test_metadata_update_does_not_turn_matching_old_balance_into_a_new_as_of_or_force_a_mutation(pm):
    with pm.store.transaction("portfolio") as portfolio:
        portfolio["updated"] = _as_of()
    pm._reload()
    before = _portfolio_bytes(pm)
    old = _as_of(days=1)
    receipt = _record(pm, as_of=old, reported_quantity=10, quantity_unit="股", native_instrument=SYMBOL)
    assert receipt["status"] == "last_observed" and receipt["as_of"] == old
    assert receipt["portfolio_mutated"] is False and _portfolio_bytes(pm) == before
    field = _confirmations(pm)["holdings"][SYMBOL]["current_quantity"]
    assert field["temporal_status"] == "last_observed" and field["as_of"] == old


def test_unknown_snapshot_time_does_not_allow_different_old_quantity_to_override_current_snapshot(pm):
    with pm.store.transaction("portfolio") as portfolio:
        portfolio["updated"] = _as_of()
    pm._reload()
    before = _portfolio_bytes(pm)
    receipt = _record(pm, as_of=_as_of(days=1), reported_quantity=99, quantity_unit="股", native_instrument=SYMBOL)
    assert receipt["status"] == "historical" and _portfolio_bytes(pm) == before
    assert _confirmations(pm)["holdings"] == {}


def test_last_observed_quantity_does_not_authorize_overwriting_other_fields_of_a_newer_snapshot(pm):
    with pm.store.transaction("portfolio") as portfolio:
        portfolio["updated"] = _as_of()
    pm._reload()
    stamp = _as_of(days=1)
    quantity = _record(pm, as_of=stamp, reported_quantity=10, quantity_unit="股", native_instrument=SYMBOL)
    assert quantity["status"] == "last_observed"
    before = _portfolio_bytes(pm)
    cost = _record(pm, as_of=stamp, idempotency_key="cost-of-old-statement", reported_cost=6,
                   cost_currency="CNY", quantity_unit="股", native_instrument=SYMBOL)
    assert cost["status"] == "historical" and cost["portfolio_mutated"] is False
    assert cost["as_of"] == stamp and _portfolio_bytes(pm) == before
    assert set(_confirmations(pm)["holdings"][SYMBOL]) == {"current_quantity"}


def test_resealed_temporal_status_cannot_promote_last_observed_into_snapshot_overwrite_authority(pm):
    with pm.with_portfolio_tx() as portfolio:
        portfolio["updated"] = _as_of()
    old = _as_of(days=2)
    first = _record(pm, as_of=old, reported_quantity=10, quantity_unit="股", native_instrument=SYMBOL)
    assert first["field_disposition"]["current_quantity"] == "last_observed"
    receipt_path = pm.store.root / ".evidence/account_observations" / (first["observation_id"] + ".json")
    receipt_bytes = receipt_path.read_bytes()
    _reseal_heads(pm, lambda state: state["positions"][SYMBOL]["current_quantity"].update(temporal_status="current"))
    assert _confirmations(pm)["status"] == "unavailable"
    before, history = _portfolio_bytes(pm), pm.store.read_history()
    with pytest.raises(ValueError, match="field binding failed"):
        _record(pm, as_of=_as_of(days=1), idempotency_key="forged-current-followup", reported_quantity=99,
                quantity_unit="股", native_instrument=SYMBOL)
    assert _portfolio_bytes(pm) == before and pm.store.read_history() == history
    assert receipt_path.read_bytes() == receipt_bytes
    assert len(list(receipt_path.parent.glob("*.json"))) == 1


@pytest.mark.parametrize("head_change", [
    {"temporal_status": "last_observed"}, {"field": "cost_basis"}, {"sha256": "0" * 64},
    {"as_of": "2020-01-01T00:00:00+00:00"}, {"reported": {"value": 99, "unit": "股", "native_instrument": SYMBOL}},
    {"position_sha256": "0" * 64}, {"estimates_sha256": "0" * 64}, {"history_sha256": "0" * 64},
    {"unbound_qualification": "current"},
])
def test_every_resealed_field_head_qualification_is_bound_before_read_replay_or_write(pm, head_change):
    stamp = _as_of()
    _record(pm, as_of=stamp, reported_quantity=10, quantity_unit="股", native_instrument=SYMBOL)
    _reseal_heads(pm, lambda state: state["positions"][SYMBOL]["current_quantity"].update(head_change))
    before, history = _portfolio_bytes(pm), pm.store.read_history()
    assert _confirmations(pm)["status"] == "unavailable"
    with pytest.raises(ValueError, match="binding failed"):
        _record(pm, as_of=stamp, reported_quantity=10, quantity_unit="股", native_instrument=SYMBOL)
    with pytest.raises(ValueError, match="binding failed"):
        _record(pm, idempotency_key="new-after-bad-head", reported_quantity=99,
                quantity_unit="股", native_instrument=SYMBOL)
    assert _portfolio_bytes(pm) == before and pm.store.read_history() == history


def test_head_cannot_be_reassigned_to_a_different_position_with_a_resealed_envelope(pm):
    _record(pm, reported_quantity=10, quantity_unit="股", native_instrument=SYMBOL)
    _reseal_heads(pm, lambda state: state["positions"].update(OTHER=state["positions"].pop(SYMBOL)))
    assert _confirmations(pm)["status"] == "unavailable"
    before = _portfolio_bytes(pm)
    with pytest.raises(ValueError, match="field binding failed"):
        _record(pm, idempotency_key="new-after-foreign-head", reported_quantity=99,
                quantity_unit="股", native_instrument=SYMBOL)
    assert _portfolio_bytes(pm) == before


@pytest.mark.parametrize("change", ["delete_field", "delete_position", "rollback_head"])
def test_resealed_heads_cannot_erase_a_completed_current_field_or_its_newer_statement_clock(pm, change):
    _record(pm, as_of=_as_of(days=2), reported_quantity=10, quantity_unit="股", native_instrument=SYMBOL)
    path = pm.store.root / ".state/account_observation_heads.json"
    original_head = json.loads(path.read_text())["payload"]["positions"][SYMBOL]["current_quantity"]
    _record(pm, as_of=_as_of(days=1), idempotency_key="newer-current-confirmation", reported_quantity=10,
            quantity_unit="股", native_instrument=SYMBOL)

    def tamper(state):
        if change == "delete_field":
            state["positions"][SYMBOL].pop("current_quantity")
        elif change == "delete_position":
            state["positions"].pop(SYMBOL)
        else:
            state["positions"][SYMBOL]["current_quantity"] = original_head

    _reseal_heads(pm, tamper)
    before, history = _portfolio_bytes(pm), pm.store.read_history()
    assert _confirmations(pm)["status"] == "unavailable"
    with pytest.raises(ValueError, match="coverage binding failed"):
        _record(pm, as_of=_as_of(days=1.5), idempotency_key="between-completed-clocks", reported_quantity=99,
                quantity_unit="股", native_instrument=SYMBOL)
    assert _portfolio_bytes(pm) == before and pm.store.read_history() == history


@pytest.mark.parametrize("receive_clock", ["forward", "equal", "backward"])
def test_same_asof_value_correction_has_immutable_succession_even_when_receive_clocks_tie_or_go_backwards(
        pm, monkeypatch, receive_clock):
    from openinvest.services import balance_observations as service
    stamp = _as_of(days=1)
    clock_value = datetime.now(timezone.utc) - timedelta(minutes=1)

    class ReceiptClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock_value.astimezone(tz) if tz else clock_value.replace(tzinfo=None)

    monkeypatch.setattr(service, "datetime", ReceiptClock)
    first = _record(pm, as_of=stamp, reported_value=50, value_currency="CNY")
    path = pm.store.root / ".state/account_observation_heads.json"
    old_head = json.loads(path.read_text())["payload"]["positions"][SYMBOL]["current_value"]
    first_path = pm.store.root / ".evidence/account_observations" / (first["observation_id"] + ".json")
    first_bytes = first_path.read_bytes()
    clock_value += timedelta(seconds={"forward": 1, "equal": 0, "backward": -1}[receive_clock])
    second = _record(pm, as_of=stamp, idempotency_key="same-asof-corrected-value", reported_value=60, value_currency="CNY")
    assert first["field_predecessors"] == {"current_value": None}
    assert second["field_predecessors"] == {"current_value": {"observation_id": first["observation_id"], "sha256": first["sha256"]}}
    current = _confirmations(pm)
    assert current["status"] == "ok" and current["holdings"][SYMBOL]["current_value"]["reported"]["value"] == 60
    assert first_path.read_bytes() == first_bytes
    _reseal_heads(pm, lambda state: state["positions"][SYMBOL].update(current_value=old_head))
    before, history = _portfolio_bytes(pm), pm.store.read_history()
    assert _confirmations(pm)["status"] == "unavailable"
    with pytest.raises(ValueError, match="succession binding failed"):
        _record(pm, as_of=stamp, idempotency_key="after-rollback", reported_quantity=99,
                quantity_unit="股", native_instrument=SYMBOL)
    assert _portfolio_bytes(pm) == before and pm.store.read_history() == history
    assert len(list(first_path.parent.glob("*.json"))) == 2


def test_historical_audit_receipt_does_not_replace_a_current_field_or_its_predecessor(pm):
    stamp = _as_of(days=1)
    first = _record(pm, as_of=stamp, reported_value=50, value_currency="CNY")
    old = _record(pm, as_of=_as_of(days=2), idempotency_key="historical-value", reported_value=999, value_currency="CNY")
    assert old["status"] == "historical" and old["field_predecessors"] == {}
    second = _record(pm, as_of=stamp, idempotency_key="corrected-value", reported_value=60, value_currency="CNY")
    assert second["field_predecessors"]["current_value"] == {"observation_id": first["observation_id"], "sha256": first["sha256"]}
    before = (pm.store.root / ".state/account_observation_heads.json").read_bytes()
    replay = _record(pm, as_of=stamp, reported_value=50, value_currency="CNY")
    assert replay["replayed"] is True and replay["observation_id"] == first["observation_id"]
    assert (pm.store.root / ".state/account_observation_heads.json").read_bytes() == before
    assert _confirmations(pm)["holdings"][SYMBOL]["current_value"]["reported"]["value"] == 60


@pytest.mark.parametrize("remove", ["newest_value", "only_receipt_and_heads", "receipt_directory_and_heads", "correction_receipt"])
def test_journal_receipt_references_cannot_be_erased_by_deleting_files_and_mutable_completed_entries(pm, remove):
    first = _record(pm, as_of=_as_of(days=2), reported_value=50, value_currency="CNY")
    heads_path = pm.store.root / ".state/account_observation_heads.json"
    old_state = json.loads(heads_path.read_text())["payload"]
    if remove in ("only_receipt_and_heads", "receipt_directory_and_heads"):
        deleted = first
        heads_path.unlink()
    else:
        report = ({"reported_quantity": 8, "quantity_unit": "股", "native_instrument": SYMBOL}
                  if remove == "correction_receipt" else {"reported_value": 60, "value_currency": "CNY"})
        deleted = _record(pm, as_of=_as_of(days=1), idempotency_key="newest-receipt", **report)
        heads_path.write_text(json.dumps({"schema": 1, "payload": old_state, "sha256": digest(old_state)}))
    (pm.store.root / ".evidence/account_observations" / (deleted["observation_id"] + ".json")).unlink()
    if remove == "receipt_directory_and_heads":
        (pm.store.root / ".evidence/account_observations").rmdir()
    assert any((row.get("source_reference") or {}).get("observation_id") == deleted["observation_id"]
               for row in pm.store.read_history())
    before, history = _portfolio_bytes(pm), pm.store.read_history()
    assert _confirmations(pm)["status"] == "unavailable"
    with pytest.raises(ValueError, match="receipt.*(binding failed|missing)"):
        _record(pm, idempotency_key="after-missing-receipt", reported_quantity=99,
                quantity_unit="股", native_instrument=SYMBOL)
    assert _portfolio_bytes(pm) == before and pm.store.read_history() == history


@pytest.mark.parametrize("corrupt", ["missing_note", "duplicate_note", "wrong_receipt_hash", "wrong_disposition", "wrong_order", "missing_correction"])
def test_journal_must_cover_each_receipt_once_with_the_exact_hash_and_field_succession(pm, corrupt):
    stamp = _as_of(days=1)
    _record(pm, as_of=stamp, reported_value=50, value_currency="CNY")
    _record(pm, as_of=stamp, idempotency_key="corrected-current-value", reported_value=60, value_currency="CNY")
    if corrupt == "missing_correction":
        _record(pm, as_of=stamp, idempotency_key="quantity-correction", reported_quantity=8,
                quantity_unit="股", native_instrument=SYMBOL)
    rows = pm.store.read_history()
    if corrupt == "missing_note":
        rows.pop()
    elif corrupt == "duplicate_note":
        rows.append(rows[-1])
    elif corrupt == "wrong_receipt_hash":
        rows[-1]["source_reference"]["observation_sha256"] = "0" * 64
    elif corrupt == "wrong_disposition":
        rows[-1]["field_disposition"]["current_value"] = "historical"
    elif corrupt == "wrong_order":
        rows.reverse()
    else:
        rows = [row for row in rows if row["action"] != "manual_override"]
    (pm.store.root / "portfolio_history.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    before = _portfolio_bytes(pm)
    assert _confirmations(pm)["status"] == "unavailable"
    with pytest.raises(ValueError, match="binding failed"):
        _record(pm, idempotency_key="after-corrupt-journal", reported_value=70, value_currency="CNY")
    assert _portfolio_bytes(pm) == before


@pytest.mark.parametrize("first_quantity", [10, 8])
def test_valid_old_current_head_cannot_waive_new_snapshot_time_after_its_position_binding_breaks(pm, first_quantity):
    first = _record(pm, as_of=_as_of(days=2), reported_quantity=first_quantity,
                    quantity_unit="股", native_instrument=SYMBOL)
    assert first["status"] == "current"
    with pm.with_portfolio_tx() as portfolio:
        portfolio["holdings"][0]["units"] = 12
    assert _confirmations(pm)["holdings"][SYMBOL]["current_quantity"]["status"] == "stale"
    before = _portfolio_bytes(pm)
    later = _record(pm, as_of=_as_of(days=1), idempotency_key="older-than-new-snapshot", reported_quantity=99,
                    quantity_unit="股", native_instrument=SYMBOL)
    assert later["status"] == "historical" and later["portfolio_mutated"] is False
    assert _portfolio_bytes(pm) == before


@pytest.mark.parametrize("first_field,changed_field,next_report", [
    ("cost_basis", "units", {"reported_quantity": 99, "quantity_unit": "股", "native_instrument": SYMBOL}),
    ("current_quantity", "avg_cost", {"reported_cost": 99, "cost_currency": "CNY", "quantity_unit": "股", "native_instrument": SYMBOL}),
    ("current_value", "avg_cost", {"reported_cost": 99, "cost_currency": "CNY", "quantity_unit": "股", "native_instrument": SYMBOL}),
])
def test_independently_confirmed_field_cannot_waive_snapshot_time_for_an_unexplained_other_field_change(
        pm, first_field, changed_field, next_report):
    reports = {"cost_basis": {"reported_cost": 5, "cost_currency": "CNY", "quantity_unit": "股", "native_instrument": SYMBOL},
               "current_quantity": {"reported_quantity": 10, "quantity_unit": "股", "native_instrument": SYMBOL},
               "current_value": {"reported_value": 50, "value_currency": "CNY"}}
    _record(pm, as_of=_as_of(days=2), **reports[first_field])
    with pm.with_portfolio_tx() as portfolio:
        portfolio["holdings"][0][changed_field] = 12
    assert _confirmations(pm)["holdings"][SYMBOL][first_field]["status"] == "client_reported_balance_confirmed"
    before = _portfolio_bytes(pm)
    next_receipt = _record(pm, as_of=_as_of(days=1), idempotency_key="other-field-old-statement", **next_report)
    assert next_receipt["status"] == "historical" and next_receipt["portfolio_mutated"] is False
    assert _portfolio_bytes(pm) == before


def test_idempotent_replay_is_read_only_and_changed_parameters_are_rejected(pm):
    stamp = _as_of()
    first = _record(pm, as_of=stamp, reported_quantity=8, quantity_unit="股", native_instrument=SYMBOL)
    portfolio, history = _portfolio_bytes(pm), pm.store.read_history()
    second = _record(pm, as_of=stamp, reported_quantity=8, quantity_unit="股", native_instrument=SYMBOL)
    assert second["replayed"] is True and second["observation_id"] == first["observation_id"]
    assert second["observed_at"] == first["observed_at"]
    assert _portfolio_bytes(pm) == portfolio and pm.store.read_history() == history
    with pytest.raises(ValueError, match="idempotency key reused"):
        _record(pm, as_of=stamp, reported_quantity=9, quantity_unit="股", native_instrument=SYMBOL)
    assert _portfolio_bytes(pm) == portfolio and pm.store.read_history() == history


def test_concurrent_same_key_records_one_receipt_and_one_absolute_correction(pm):
    root, stamp = pm.store.root, _as_of()

    def record(_):
        return _record(PortfolioManager(MemoryStore(root)), as_of=stamp, reported_quantity=8,
                       quantity_unit="股", native_instrument=SYMBOL)

    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = list(pool.map(record, range(8)))
    assert sum(not row["replayed"] for row in receipts) == 1
    assert len({row["observation_id"] for row in receipts}) == 1
    assert len(list((root / ".evidence/account_observations").glob("*.json"))) == 1
    assert [row["action"] for row in pm.store.read_history()] == ["manual_override", "balance_observation"]
    assert PortfolioManager(pm.store).find_holding(SYMBOL)["units"] == 8


def test_concurrent_same_field_corrections_form_one_application_chain_with_identical_receive_clocks(pm, monkeypatch):
    from openinvest.services import balance_observations as service
    fixed_clock = datetime.now(timezone.utc)
    stamp = (fixed_clock - timedelta(minutes=1)).isoformat()
    root = pm.store.root

    class ReceiptClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_clock.astimezone(tz) if tz else fixed_clock.replace(tzinfo=None)

    monkeypatch.setattr(service, "datetime", ReceiptClock)

    def record(index):
        return _record(PortfolioManager(MemoryStore(root)), as_of=stamp, idempotency_key=f"concurrent-field-{index}",
                       reported_value=50 + index, value_currency="CNY")

    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = list(pool.map(record, range(8)))
    assert len({row["observed_at"] for row in receipts}) == 1
    predecessors = [row["field_predecessors"]["current_value"] for row in receipts]
    assert predecessors.count(None) == 1
    replaced = {row["observation_id"] for row in predecessors if row is not None}
    terminal, = [row for row in receipts if row["observation_id"] not in replaced]
    confirmed = _confirmations(pm)
    assert confirmed["status"] == "ok"
    assert confirmed["holdings"][SYMBOL]["current_value"]["observation_id"] == terminal["observation_id"]
    assert confirmed["holdings"][SYMBOL]["current_value"]["reported"] == terminal["fields"]["current_value"]
    assert len(pm.store.read_history()) == 8


def test_later_normal_trade_stales_the_anchor_even_with_a_stale_pm_instance(pm):
    _record(pm, reported_quantity=10, quantity_unit="股", native_instrument=SYMBOL)
    old_history = pm.store.read_history()
    PortfolioManager(pm.store).buy(SYMBOL, 1, 5)
    result = current_confirmations(pm, old_history, [])
    assert result["holdings"][SYMBOL]["current_quantity"]["status"] == "stale"
    assert result["historical_execution_eligible"] is False


def test_later_estimate_stales_the_anchor_and_changes_its_fingerprint(pm):
    _record(pm, reported_value=50, value_currency="CNY")
    first = _confirmations(pm)
    record_tracking_estimate(pm.store, symbol=SYMBOL, day=datetime.now(timezone.utc).date().isoformat(),
                             plan={"amount": 100}, quote={"projected_quantity": 20})
    later = _confirmations(pm)
    assert later["holdings"][SYMBOL]["current_value"]["status"] == "stale"
    assert later["fingerprint"] != first["fingerprint"]


def test_resealed_heads_cannot_move_an_old_receipt_to_a_different_current_quantity(pm):
    from openinvest.services import balance_observations as service
    _record(pm, reported_quantity=10, quantity_unit="股", native_instrument=SYMBOL)
    with pm.with_portfolio_tx() as portfolio:
        portfolio["holdings"][0]["units"] = 11
    path = pm.store.root / ".state/account_observation_heads.json"
    envelope = json.loads(path.read_text())
    state = envelope["payload"]
    state["positions"][SYMBOL]["current_quantity"]["position_sha256"] = digest(
        service._field_position(PortfolioManager(pm.store).find_holding(SYMBOL), "current_quantity"))
    path.write_text(json.dumps({"schema": 1, "payload": state, "sha256": digest(state)}))
    assert _confirmations(pm)["status"] == "unavailable"


def test_resealed_heads_cannot_erase_a_later_activity_from_the_receipts_tail_binding(pm):
    _record(pm, reported_quantity=10, quantity_unit="股", native_instrument=SYMBOL)
    pm.store.append_history({"action": "buy", "symbol": SYMBOL, "units": 0, "price": 5})
    path = pm.store.root / ".state/account_observation_heads.json"
    envelope = json.loads(path.read_text())
    state = envelope["payload"]
    rows = [row for row in pm.store.read_history() if row["action"] != "balance_observation"]
    state["positions"][SYMBOL]["current_quantity"]["history_sha256"] = digest(rows)
    path.write_text(json.dumps({"schema": 1, "payload": state, "sha256": digest(state)}))
    assert _confirmations(pm)["status"] == "unavailable"


def test_cost_only_observation_does_not_invalidate_an_independent_unchanged_quantity_field(pm):
    _record(pm, reported_quantity=10, quantity_unit="股", native_instrument=SYMBOL)
    _record(pm, idempotency_key="cost-2", reported_cost=6, cost_currency="CNY", quantity_unit="股", native_instrument=SYMBOL)
    fields = _confirmations(pm)["holdings"][SYMBOL]
    assert fields["current_quantity"]["status"] == fields["cost_basis"]["status"] == "client_reported_balance_confirmed"


@pytest.mark.parametrize("first_field,second_field", [
    ("current_quantity", "cost_basis"), ("current_quantity", "current_value"),
    ("cost_basis", "current_quantity"), ("current_value", "current_quantity"),
])
def test_independent_fields_can_be_reported_in_either_order_at_the_same_statement_time(pm, first_field, second_field):
    stamp = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    reports = {
        "current_quantity": {"reported_quantity": 8, "quantity_unit": "股", "native_instrument": SYMBOL},
        "cost_basis": {"reported_cost": 6, "cost_currency": "CNY", "quantity_unit": "股", "native_instrument": SYMBOL},
        "current_value": {"reported_value": 80, "value_currency": "CNY"},
    }
    first = _record(pm, as_of=stamp, idempotency_key="first-field", **reports[first_field])
    first_path = pm.store.root / ".evidence/account_observations" / (first["observation_id"] + ".json")
    first_bytes = first_path.read_bytes()
    second = _record(pm, as_of=stamp, idempotency_key="second-field", **reports[second_field])
    assert first["status"] == second["status"] == "current"
    assert first["as_of"] == second["as_of"] == stamp
    assert datetime.fromisoformat(second["observed_at"]) > datetime.fromisoformat(stamp)
    fields = _confirmations(pm)["holdings"][SYMBOL]
    assert fields[second_field]["status"] == "client_reported_balance_confirmed"
    assert fields[first_field]["status"] == "client_reported_balance_confirmed"
    assert fields[first_field]["as_of"] == fields[second_field]["as_of"] == stamp
    assert first_path.read_bytes() == first_bytes
    fresh = PortfolioManager(pm.store)
    assert fresh.find_holding(SYMBOL)["units"] == 8
    assert fresh.find_holding(SYMBOL)["avg_cost"] == (6 if "cost_basis" in (first_field, second_field) else 5)
    assert fresh.cash_amount("CNY") == 1000


@pytest.mark.parametrize("later_activity", ["buy", "estimate", "manual_override"])
def test_same_statement_time_cannot_cross_a_later_real_activity_or_estimate(pm, later_activity):
    stamp = _as_of()
    _record(pm, as_of=stamp, reported_quantity=8, quantity_unit="股", native_instrument=SYMBOL)
    if later_activity == "buy":
        PortfolioManager(pm.store).buy(SYMBOL, 1, 5, source="mcp")
    elif later_activity == "estimate":
        record_tracking_estimate(pm.store, symbol=SYMBOL, day=datetime.now(timezone.utc).date().isoformat(),
                                 plan={"amount": 100}, quote={"projected_quantity": 20})
    else:
        pm.store.append_history({"action": "manual_override", "symbol": SYMBOL, "units": 9,
                                 "source": "mcp", "observation_as_of": stamp})
    before = _portfolio_bytes(pm)
    receipt = _record(pm, as_of=stamp, idempotency_key="late-cost", reported_cost=6,
                      cost_currency="CNY", quantity_unit="股", native_instrument=SYMBOL)
    assert receipt["status"] == "historical" and receipt["portfolio_mutated"] is False
    assert receipt["as_of"] == stamp and _portfolio_bytes(pm) == before
    assert _confirmations(pm)["holdings"][SYMBOL]["current_quantity"]["status"] == "stale"


@pytest.mark.parametrize("first_field", ["cost_basis", "current_value"])
def test_a_newer_quantity_statement_is_not_ignored_as_a_same_time_correction(pm, first_field):
    old = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    new = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    report = ({"reported_cost": 6, "cost_currency": "CNY", "quantity_unit": "股", "native_instrument": SYMBOL}
              if first_field == "cost_basis" else {"reported_value": 80, "value_currency": "CNY"})
    _record(pm, as_of=old, idempotency_key="older-field", **report)
    quantity = _record(pm, as_of=new, idempotency_key="newer-quantity", reported_quantity=8,
                       quantity_unit="股", native_instrument=SYMBOL)
    assert quantity["status"] == "current"
    assert _confirmations(pm)["holdings"][SYMBOL][first_field]["status"] == "stale"
    before = _portfolio_bytes(pm)
    older = _record(pm, as_of=old, idempotency_key="late-old-cost", reported_cost=7,
                    cost_currency="CNY", quantity_unit="股", native_instrument=SYMBOL)
    assert older["status"] == "historical" and older["as_of"] == old
    assert older["portfolio_mutated"] is False and _portfolio_bytes(pm) == before


def test_a_real_trade_after_same_statement_field_corrections_stales_all_fields(pm):
    stamp = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    _record(pm, as_of=stamp, reported_value=80, value_currency="CNY")
    _record(pm, as_of=stamp, idempotency_key="same-statement-quantity", reported_quantity=8,
            quantity_unit="股", native_instrument=SYMBOL)
    assert all(field["status"] == "client_reported_balance_confirmed"
               for field in _confirmations(pm)["holdings"][SYMBOL].values())
    PortfolioManager(pm.store).buy(SYMBOL, 1, 5, source="mcp")
    assert all(field["status"] == "stale" for field in _confirmations(pm)["holdings"][SYMBOL].values())


def test_correction_clock_must_match_its_sealed_receipt(pm):
    stamp = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    _record(pm, as_of=stamp, reported_quantity=8, quantity_unit="股", native_instrument=SYMBOL)
    rows = pm.store.read_history()
    rows[0]["observation_as_of"] = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    path = pm.store.root / "portfolio_history.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    assert _confirmations(pm)["status"] == "unavailable"
    before = _portfolio_bytes(pm)
    with pytest.raises(ValueError, match="receipt binding failed"):
        _record(pm, as_of=stamp, idempotency_key="cost-after-tampering", reported_cost=6,
                cost_currency="CNY", quantity_unit="股", native_instrument=SYMBOL)
    assert _portfolio_bytes(pm) == before


@pytest.mark.parametrize("target", ["observation", "parent"])
def test_revoked_observation_or_parent_source_cannot_confirm_current_fields(pm, target):
    receipt = _record(pm, reported_value=50, value_currency="CNY", source_locator={"parent_ids": ["source:statement-1"]})
    first = _confirmations(pm)
    artifact = "account_observation:" + receipt["observation_id"] if target == "observation" else "source:statement-1"
    Eligibility(pm.store.data_root).revoke(artifact, "synthetic withdrawal")
    later = _confirmations(pm)
    assert later["holdings"][SYMBOL]["current_value"]["status"] == "revoked"
    assert later["fingerprint"] != first["fingerprint"]


@pytest.mark.parametrize("ancestor", ["client_source", "source_locator"])
def test_audit_lineage_revocation_closure_reaches_old_plans_that_only_reference_the_observation(pm, ancestor):
    receipt = _record(pm, reported_value=50, value_currency="CNY",
                      source_locator={"artifact_id": "source:original-statement"})
    policy = Eligibility(pm.store.data_root)
    ident = "account_observation:" + receipt["observation_id"]
    policy.record_audit_lineage("plan:previously-prepared", {"plan": "synthetic"}, parents={ident})
    parent = "client_source:" + receipt["source_text_sha256"] if ancestor == "client_source" else "source:original-statement"
    assert ident not in policy.excluded_ids()
    policy.revoke(parent, "synthetic source withdrawal")
    assert {parent, ident, "plan:previously-prepared"} <= policy.excluded_ids()


def test_observation_registry_only_records_audit_purpose_and_preserves_the_original_receipt_on_replay(pm):
    stamp = _as_of()
    receipt = _record(pm, as_of=stamp, reported_value=50, value_currency="CNY",
                      source_locator={"parent_ids": ["source:external-record"]})
    ident = "account_observation:" + receipt["observation_id"]
    path = pm.store.root / ".evidence/account_observations" / (receipt["observation_id"] + ".json")
    original = path.read_bytes()
    registry = json.loads((pm.store.data_root / "integrity/eligibility.json").read_text())
    record = registry["artifacts"][ident]
    payload = json.loads(original)["payload"]
    assert record["allowed_uses"] == ["audit"] and record["origin"] == "unverified"
    assert record["source_validity"] == "pending_review" and record["sha256"] == digest(payload)
    assert set(record["parents"]) == set(receipt["parent_ids"])
    policy = Eligibility(pm.store.data_root)
    assert all(not policy.allows(ident, payload, purpose) for purpose in ("advice", "learn", "evaluate", "publish"))
    replay = _record(pm, as_of=stamp, reported_value=50, value_currency="CNY",
                     source_locator={"parent_ids": ["source:external-record"]})
    assert replay["replayed"] is True and replay["observed_at"] == receipt["observed_at"]
    assert path.read_bytes() == original
    assert json.loads((pm.store.data_root / "integrity/eligibility.json").read_text()) == registry


def test_field_parents_remain_separate_when_an_older_field_was_revoked(pm):
    old = _record(pm, reported_quantity=10, quantity_unit="股", native_instrument=SYMBOL,
                  source_text="原始份额声明", source_locator={"parent_ids": ["source:quantity-old"]})
    new = _record(pm, idempotency_key="value-new", reported_value=50, value_currency="CNY",
                  source_text="独立市值声明", source_locator={"parent_ids": ["source:value-new"]})
    Eligibility(pm.store.data_root).revoke("source:quantity-old", "synthetic withdrawal")
    status = _confirmations(pm)
    quantity, value = (status["holdings"][SYMBOL][name] for name in ("current_quantity", "current_value"))
    assert quantity["status"] == "revoked" and value["status"] == "client_reported_balance_confirmed"
    assert set(quantity["parent_ids"]) == {"account_observation:" + old["observation_id"], *old["parent_ids"]}
    assert set(value["parent_ids"]) == {"account_observation:" + new["observation_id"], *new["parent_ids"]}
    assert "source:quantity-old" not in value["parent_ids"]
    assert {"source:quantity-old", "source:value-new"} <= set(status["parent_ids"])


def test_failed_lineage_registration_never_publishes_a_head_or_changes_the_snapshot(pm, monkeypatch):
    monkeypatch.setattr(Eligibility, "record_audit_lineage",
                        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("synthetic registry failure")))
    before = _portfolio_bytes(pm)
    with pytest.raises(OSError, match="synthetic registry failure"):
        _record(pm, reported_quantity=99, quantity_unit="股", native_instrument=SYMBOL)
    assert _portfolio_bytes(pm) == before and pm.store.read_history() == []
    assert not (pm.store.root / ".state/account_observation_heads.json").exists()
    assert _confirmations(pm)["status"] == "unavailable"


def test_a_previously_revoked_source_is_archived_without_overwriting_quantity(pm):
    Eligibility(pm.store.data_root).revoke("source:withdrawn", "synthetic withdrawal")
    before = _portfolio_bytes(pm)
    receipt = _record(pm, reported_quantity=99, quantity_unit="股", native_instrument=SYMBOL,
                      source_locator={"parent_ids": ["source:withdrawn"]})
    assert receipt["status"] == "revoked" and _portfolio_bytes(pm) == before
    assert _confirmations(pm)["holdings"] == {}


@pytest.mark.parametrize("corrupt", ["receipt", "heads", "delete_heads"])
def test_corrupt_or_missing_evidence_fails_closed_and_cannot_be_treated_as_empty(pm, corrupt):
    receipt = _record(pm, reported_value=50, value_currency="CNY")
    path = pm.store.root / ".evidence/account_observations" / (receipt["observation_id"] + ".json")
    heads = pm.store.root / ".state/account_observation_heads.json"
    if corrupt == "delete_heads":
        heads.unlink()
    else:
        (path if corrupt == "receipt" else heads).write_text('{"broken":true}')
    assert _confirmations(pm)["status"] == "unavailable"
    before = _portfolio_bytes(pm)
    with pytest.raises(ValueError):
        _record(pm, idempotency_key="new-key", reported_quantity=99, quantity_unit="股", native_instrument=SYMBOL)
    assert _portfolio_bytes(pm) == before


def test_interrupted_application_requires_reconciliation_instead_of_reapplying(pm, monkeypatch):
    from openinvest.services import balance_observations as service
    stamp = _as_of()
    monkeypatch.setattr(service, "_write_heads", lambda *args: (_ for _ in ()).throw(OSError("synthetic interruption")))
    with pytest.raises(OSError):
        _record(pm, as_of=stamp, reported_quantity=8, quantity_unit="股", native_instrument=SYMBOL)
    assert PortfolioManager(pm.store).find_holding(SYMBOL)["units"] == 8
    assert _confirmations(pm)["status"] == "unavailable"
    before = pm.store.read_history()
    with pytest.raises(ValueError, match="heads missing"):
        _record(pm, as_of=stamp, reported_quantity=8, quantity_unit="股", native_instrument=SYMBOL)
    assert pm.store.read_history() == before


def test_paper_ledger_and_arbitrary_source_kind_never_become_broker_verified(pm):
    with pm.store.transaction("portfolio") as portfolio:
        portfolio["ledger_domain"] = "paper_simulation"
    pm._reload()
    receipt = _record(pm, reported_quantity=10, quantity_unit="股", native_instrument=SYMBOL,
                      source_locator={"kind": "broker_verified", "parent_ids": []})
    field = _confirmations(pm)["holdings"][SYMBOL]["current_quantity"]
    assert receipt["source_authority"] == field["source_authority"] == AUTHORITY
    assert receipt["execution_domain"] == field["execution_domain"] == "paper_simulation"
    assert field["independent_broker_verification"] is False


def test_later_change_to_a_paper_book_stales_an_earlier_real_domain_head(pm):
    _record(pm, reported_quantity=10, quantity_unit="股", native_instrument=SYMBOL)
    with pm.store.transaction("portfolio") as portfolio:
        portfolio["ledger_domain"] = "paper_simulation"
    assert _confirmations(pm)["holdings"][SYMBOL]["current_quantity"]["status"] == "stale"


@pytest.mark.parametrize("kwargs", [{"as_of": "2026-01-01"}, {"source_text": " "}, {"source_locator": " "},
    {"reported_value": float("nan")}, {"reported_value": float("inf")}, {"reported_value": -1},
    {"reported_value": True}, {"reported_quantity": -1}, {"reported_quantity": 0, "quantity_unit": None},
    {"reported_cost": -1, "cost_currency": "CNY"}, {"value_currency": None},
    {"as_of": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}])
def test_invalid_inputs_cannot_change_any_formal_state_or_create_a_receipt(pm, kwargs):
    before = _portfolio_bytes(pm)
    with pytest.raises((ValueError, TypeError)):
        _record(pm, **{"reported_value": 50, "value_currency": "CNY", **kwargs})
    assert _portfolio_bytes(pm) == before and pm.store.read_history() == []
    assert list((pm.store.root / ".evidence/account_observations").glob("*.json")) == []


def test_read_without_observations_is_side_effect_free(pm):
    before = {str(path): path.read_bytes() for path in pm.store.root.rglob("*") if path.is_file()}
    result = _confirmations(pm)
    assert result["status"] == "ok" and result["holdings"] == {} and result["parent_ids"] == []
    assert {str(path): path.read_bytes() for path in pm.store.root.rglob("*") if path.is_file()} == before


def test_qualifier_accepts_the_existing_ledger_estimate_envelope_shape(pm):
    record_tracking_estimate(pm.store, symbol=SYMBOL, day=datetime.now(timezone.utc).date().isoformat(),
                             plan={"amount": 100}, quote={"projected_quantity": 20})
    _record(pm, reported_quantity=10, quantity_unit="股", native_instrument=SYMBOL)
    envelopes = [{"sha256": row["sha256"], "payload": row["payload"]} for row in _estimates(pm)]
    result = current_confirmations(pm, pm.store.read_history(), envelopes)
    assert result["holdings"][SYMBOL]["current_quantity"]["status"] == "client_reported_balance_confirmed"
