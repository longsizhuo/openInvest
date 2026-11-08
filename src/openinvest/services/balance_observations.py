"""Sealed client balance observations, separate from fills and return evidence.

``reported_cost`` is average cost per native unit, never total cost. A value
observation cannot manufacture quantity, cost, cash flows or broker authority.
The heads are a cache of field references; immutable receipts own the facts.
"""
from __future__ import annotations

from datetime import datetime, time, timezone
import hashlib
import json
import math
from pathlib import Path
import uuid
from zoneinfo import ZoneInfo

from openinvest.calc.ledger_replay import BALANCE_CORRECTION_ACTION, balance_observation_correction
from openinvest.core.evidence_eligibility import Eligibility, digest
from openinvest.core.memory_store import _atomic_write_text, _file_lock

AUTHORITY = "client_reported_not_independently_broker_verified"
BASIS = "client_reported_balance_observation"
_NAMESPACE = uuid.UUID("2242bccd-43d1-4b82-8f10-cc82f31739dc")
_FIELDS = ("current_value", "current_quantity", "cost_basis")


def _instant(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("as_of requires an explicit timezone")
    return parsed


def _number(value, name):
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be finite and nonnegative")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{name} must be finite and nonnegative")
    return result


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value


def _currency(value, required):
    if value is None and not required:
        return None
    value = _text(value, "currency").strip().upper()
    if not 3 <= len(value) <= 5 or not value.isalpha() or not value.isascii():
        raise ValueError("invalid currency")
    return value


def _paths(pm):
    return _store_paths(pm.store)


def _store_paths(store):
    root = Path(store.root).resolve()
    directory = root / ".evidence/account_observations"
    heads = root / ".state/account_observation_heads.json"
    if not directory.resolve().is_relative_to(root) or not heads.resolve().is_relative_to(root):
        raise ValueError("observation paths must remain inside the owning memory directory")
    return directory, heads


def _sealed_read(path):
    envelope = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(envelope, dict) or envelope.get("schema") != 1
            or not isinstance(envelope.get("payload"), dict)
            or envelope.get("sha256") != digest(envelope["payload"])):
        raise ValueError("observation integrity failure; reconciliation required")
    return envelope


def _heads(directory, path, history):
    if not path.exists():
        if any(directory.glob("*.json")):
            raise ValueError("observation heads missing; reconciliation required")
        _validate_journal(history, {}, directory, {})
        return {"schema": 1, "positions": {}, "completed": {}}
    state = _sealed_read(path)["payload"]
    if (state.get("schema") != 1 or not isinstance(state.get("positions"), dict)
            or not isinstance(state.get("completed"), dict)):
        raise ValueError("invalid observation heads")
    if {p.stem for p in directory.glob("*.json")} != set(state["completed"]):
        raise ValueError("incomplete observation application; reconciliation required")
    receipts = {}
    for ident, fingerprint in state["completed"].items():
        if not isinstance(fingerprint, str) or len(fingerprint) != 64:
            raise ValueError("invalid completed observation hash")
        receipts[ident] = _receipt(directory, ident, fingerprint)
    _validate_journal(history, receipts, directory, state["completed"])
    for symbol, fields in state["positions"].items():
        if not isinstance(symbol, str) or not symbol or not isinstance(fields, dict):
            raise ValueError("invalid observation position head")
        for field, head in fields.items():
            _validate_head(symbol, field, head, receipts, history, directory, state["completed"])
    for envelope in receipts.values():
        receipt = envelope["payload"]
        for field, disposition in receipt["field_disposition"].items():
            if disposition not in ("current", "last_observed"):
                continue
            head = state["positions"].get(receipt["reference_symbol"], {}).get(field)
            if head is None or _instant(head["as_of"]) < _instant(receipt["as_of"]):
                raise ValueError("observation head coverage binding failed")
    _validate_succession(state["positions"], receipts)
    return state


def _write_heads(path, state):
    _atomic_write_text(path, json.dumps({"schema": 1, "sha256": digest(state), "payload": state},
                                      ensure_ascii=False, sort_keys=True, allow_nan=False))


def _receipt(directory, observation_id, expected_hash=None):
    if str(uuid.UUID(observation_id)) != observation_id:
        raise ValueError("invalid observation id")
    path = directory / (observation_id + ".json")
    if not path.resolve().is_relative_to(directory.resolve()):
        raise ValueError("observation receipt escaped its owner")
    envelope = _sealed_read(path)
    value = envelope["payload"]
    request = value.get("request")
    if (value.get("observation_id") != observation_id or value.get("source_authority") != AUTHORITY
            or not isinstance(request, dict) or value.get("request_sha256") != digest(request)
            or value.get("source_text_sha256") != hashlib.sha256(request["source_text"].encode()).hexdigest()
            or (expected_hash is not None and envelope["sha256"] != expected_hash)):
        raise ValueError("observation receipt binding failed")
    _instant(request["as_of"])
    _instant(value["observed_at"])
    return envelope


def _validate_journal(history, receipts, directory, completed):
    """The surviving journal independently witnesses every completed receipt."""
    if not isinstance(history, list) or any(not isinstance(row, dict) for row in history):
        raise ValueError("invalid history")
    notes, corrections, previous = {}, {}, {}
    for row in history:
        is_note = row.get("action") == "balance_observation"
        is_correction = row.get("source") == "client_balance_observation" and balance_observation_correction(row)
        if not is_note and not is_correction:
            if row.get("source") == "client_balance_observation" or row.get("evidence_basis") == BASIS:
                raise ValueError("observation journal receipt binding failed")
            continue
        if is_correction:
            _correction_receipt(row, directory, completed)
        reference = row.get("source_reference")
        envelope = receipts.get(reference.get("observation_id")) if isinstance(reference, dict) else None
        if envelope is None:
            raise ValueError("observation journal receipt binding failed")
        receipt = envelope["payload"]
        ident = receipt["observation_id"]
        expected_reference = {"observation_id": ident, "source_locator": receipt["request"]["source_locator"],
            "source_text_sha256": receipt["source_text_sha256"], "observation_sha256": envelope["sha256"],
            "parent_ids": receipt["parent_ids"]}
        if (digest(reference) != digest(expected_reference) or row.get("symbol") != receipt["reference_symbol"]
                or row.get("source") != "client_balance_observation" or row.get("evidence_basis") != BASIS
                or row.get("execution_domain") != receipt["execution_domain"]
                or row.get("observation_as_of") != receipt["as_of"]
                or row.get("trade_date") != _instant(receipt["as_of"]).date().isoformat()
                or row.get("cash_effect") != 0 or row.get("external") is not False):
            raise ValueError("observation journal receipt binding failed")
        if is_correction:
            corrections[ident] = corrections.get(ident, 0) + 1
            continue
        if (row.get("units_effect") != 0
                or digest(row.get("field_disposition")) != digest(receipt["field_disposition"])):
            raise ValueError("observation journal receipt binding failed")
        notes[ident] = notes.get(ident, 0) + 1
        for field, disposition in receipt["field_disposition"].items():
            if disposition not in ("current", "last_observed"):
                continue
            key = (receipt["reference_symbol"], field)
            predecessors = receipt.get("field_predecessors", {})
            if not isinstance(predecessors, dict) or field not in predecessors or predecessors[field] != previous.get(key):
                raise ValueError("observation journal succession binding failed; reconciliation required")
            previous[key] = {"observation_id": ident, "sha256": envelope["sha256"]}
    if set(notes) != set(receipts):
        raise ValueError("observation journal receipt coverage binding failed")
    for ident, envelope in receipts.items():
        changed_quantity = envelope["payload"].get("quantity_corrected", False)
        if (not isinstance(changed_quantity, bool) or notes[ident] != 1
                or corrections.get(ident, 0) != int(changed_quantity)):
            raise ValueError("observation journal receipt coverage binding failed")


def _validate_succession(positions, receipts):
    """Immutable predecessor links determine each field's sole current head.

    The lock establishes application order; receive clocks and equal statement
    timestamps cannot establish which correction superseded another one.
    """
    chains = {}
    for ident, envelope in receipts.items():
        receipt = envelope["payload"]
        active = {field for field, disposition in receipt["field_disposition"].items()
                  if disposition in ("current", "last_observed")}
        predecessors = receipt.get("field_predecessors", {})
        if (not isinstance(predecessors, dict) or set(predecessors) != active
                or not active <= set(_FIELDS)):
            # Legacy active receipts have no immutable application order. Keep
            # their audit records and require explicit reconciliation.
            raise ValueError("observation field succession binding failed; reconciliation required")
        for field, predecessor in predecessors.items():
            prior_id = None
            if predecessor is not None:
                if (not isinstance(predecessor, dict)
                        or set(predecessor) != {"observation_id", "sha256"}
                        or not isinstance(predecessor["observation_id"], str)):
                    raise ValueError("observation field succession binding failed")
                prior_id = predecessor["observation_id"]
                prior = receipts.get(prior_id)
                if (prior is None or predecessor["sha256"] != prior["sha256"]
                        or prior["payload"]["reference_symbol"] != receipt["reference_symbol"]
                        or prior["payload"]["field_disposition"].get(field) not in ("current", "last_observed")
                        or _instant(prior["payload"]["as_of"]) > _instant(receipt["as_of"])):
                    raise ValueError("observation field succession binding failed")
            chains.setdefault((receipt["reference_symbol"], field), {})[ident] = prior_id
    for (symbol, field), nodes in chains.items():
        successors = {}
        for ident, prior_id in nodes.items():
            if prior_id in successors or (prior_id is not None and prior_id not in nodes):
                raise ValueError("observation field succession binding failed")
            successors[prior_id] = ident
        current, seen, last = successors.get(None), set(), None
        while current is not None and current not in seen:
            seen.add(current)
            last, current = current, successors.get(current)
        if (current is not None or len(seen) != len(nodes)
                or positions.get(symbol, {}).get(field, {}).get("observation_id") != last):
            raise ValueError("observation field succession binding failed")


def _validate_head(symbol, field, head, receipts, history, directory, completed):
    """A resealed cache cannot change any immutable field qualification."""
    if field not in _FIELDS or not isinstance(head, dict):
        raise ValueError("invalid observation field head")
    envelope = receipts.get(head.get("observation_id"))
    if envelope is None:
        raise ValueError("observation head receipt missing")
    receipt = envelope["payload"]
    temporal_status = receipt["field_disposition"].get(field)
    expected = {"observation_id": receipt["observation_id"], "sha256": envelope["sha256"],
        "as_of": receipt["as_of"], "field": field, "reported": receipt["fields"].get(field),
        "temporal_status": temporal_status,
        "position_sha256": digest(receipt["position_bindings"].get(field)),
        "history_sha256": head.get("history_sha256"), "estimates_sha256": receipt["estimates_sha256"]}
    if (receipt["reference_symbol"] != symbol or receipt["request"]["reference_symbol"] != symbol
            or receipt["request"]["as_of"] != receipt["as_of"]
            or temporal_status not in ("current", "last_observed")
            or field not in receipt["fields"] or field not in receipt["position_bindings"]
            or digest(head) != digest(expected)):
        raise ValueError("observation field binding failed")
    # The post-write journal contains a reference to the receipt's own hash,
    # so its digest cannot be embedded in that receipt. Bind it to the original
    # journal prefix and the immutable pre-observation history instead.
    rows = _history_rows(history, symbol)
    own_corrections = [row for row in rows if row.get("source") == "client_balance_observation"
        and balance_observation_correction(row)
        and (row.get("source_reference") or {}).get("observation_id") == receipt["observation_id"]]
    for row in own_corrections:
        _correction_receipt(row, directory, completed)
    prefix = list(rows)
    while digest(prefix) != head["history_sha256"]:
        if not prefix:
            raise ValueError("observation history head binding failed")
        prefix.pop()
    if (digest(_without_own_correction(prefix, symbol, receipt["observation_id"]))
            != receipt["prior_history_sha256"]
            or any(row not in prefix for row in own_corrections)
            or len(own_corrections) > 1):
        raise ValueError("observation history head binding failed")


def _position(holding):
    state = {key: holding.get(key) for key in ("symbol", "kind", "native_instrument", "units", "avg_cost",
            "unit_label", "cost_currency", "proxy_kind", "yfinance_proxy", "is_tracking_only", "extra")}
    for field in ("units", "avg_cost"):
        state[field] = _number(state[field] or 0, field)
    return state


def _field_position(holding, field):
    identity = {key: holding.get(key) for key in ("symbol", "kind", "native_instrument", "unit_label",
                "cost_currency", "proxy_kind", "yfinance_proxy")}
    if field in ("current_value", "current_quantity"):
        identity["units"] = _number(holding.get("units") or 0, "units")
    if field == "cost_basis":
        identity["avg_cost"] = _number(holding.get("avg_cost") or 0, "avg_cost")
    return identity


def _history_rows(history, symbol):
    if not isinstance(history, list) or any(not isinstance(row, dict) for row in history):
        raise ValueError("invalid history")
    # Pure observation notes do not invalidate an independent field observation.
    return [row for row in history if row.get("symbol") == symbol and row.get("action") != "balance_observation"]


def _without_own_correction(history, symbol, observation_id):
    return [row for row in _history_rows(history, symbol)
            if not (row.get("source") == "client_balance_observation"
                    and balance_observation_correction(row)
                    and (row.get("source_reference") or {}).get("observation_id") == observation_id)]


def _estimate_rows(estimates, symbol):
    rows = []
    for envelope in estimates:
        if (not isinstance(envelope, dict) or not isinstance(envelope.get("payload"), dict)
                or envelope.get("sha256") != digest(envelope["payload"])):
            raise ValueError("tracking estimate integrity failure")
        if envelope["payload"].get("identity", {}).get("symbol") == symbol:
            rows.append({"sha256": envelope["sha256"], "payload": envelope["payload"]})
    return sorted(rows, key=lambda row: row["sha256"])


def _estimates_on_disk(pm):
    return [_sealed_read(path) for path in sorted((pm.store.root / ".evidence/tracking_estimates").glob("*.json"))]


def _source_time(row, *, estimate=False):
    payload = row["payload"] if estimate else row
    day = payload.get("identity", {}).get("date") if estimate else payload.get("trade_date")
    for key in (("recorded_at",) if estimate else ("execution_time", "ts", "ts_origin")):
        value = payload.get(key)
        if not value:
            continue
        try:
            instant = _instant(value)
        except ValueError:
            day = day or str(value)[:10]
            continue
        if not day or str(day) <= instant.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat():
            return instant
    if day:
        # A day-only source does not claim an execution clock; conservatively
        # require an observation after that entire day for a current anchor.
        try:
            return datetime.combine(datetime.fromisoformat(str(day)).date(), time.max, ZoneInfo("Asia/Shanghai"))
        except ValueError:
            pass
    return None  # Retain an unordered legacy source; it cannot authorize a current overwrite.


def _correction_receipt(row, directory, completed):
    """Only a sealed, completed balance correction owns an observation clock."""
    if row.get("source") != "client_balance_observation" or not balance_observation_correction(row):
        return None
    reference = row.get("source_reference")
    if not isinstance(reference, dict) or reference.get("observation_id") not in completed:
        raise ValueError("balance correction receipt missing; reconciliation required")
    ident = reference["observation_id"]
    envelope = _receipt(directory, ident, completed[ident])
    receipt = envelope["payload"]
    quantity = receipt["fields"].get("current_quantity", {})
    if (reference.get("observation_sha256") != envelope["sha256"]
            or receipt["reference_symbol"] != row.get("symbol")
            or receipt["field_disposition"].get("current_quantity") != "current"
            or not receipt["portfolio_mutated"] or row.get("units") != quantity.get("value")
            or row.get("cash_effect") != 0 or row.get("external") is not False
            or _instant(row.get("observation_as_of")) != _instant(receipt["as_of"])
            or _instant(receipt["request"]["as_of"]) != _instant(receipt["as_of"])):
        raise ValueError("balance correction receipt binding failed")
    return receipt


def _current_as_of(pm, symbol, as_of, history, estimates, positions, completed, *, include_snapshot=True):
    directory, _ = _paths(pm)
    times = []
    for row in _history_rows(history, symbol):
        receipt = _correction_receipt(row, directory, completed)
        # Receiving another field from the same statement is not a later fill.
        times.append(_instant(receipt["as_of"]) if receipt is not None else _source_time(row))
    times += [_source_time(row, estimate=True) for row in _estimate_rows(estimates, symbol)]
    has_activity = bool(times)
    times += [_instant(row["as_of"]) for row in positions.get(symbol, {}).values()]
    if any(value is None for value in times):
        return False
    for head in positions.get(symbol, {}).values():
        if _instant(head["as_of"]) != as_of:
            continue
        ident = head["observation_id"]
        receipt = _receipt(directory, ident, completed[ident])["payload"]
        if (_history_binding_tail(history, symbol, ident, head, receipt, directory, completed) is None
                or digest(_estimate_rows(estimates, symbol)) != head["estimates_sha256"]):
            # Append order also protects against second-resolution journal
            # clocks rounding a genuinely later fill below the statement time.
            return False
    holding = pm.find_holding(symbol)
    owner = pm.store.root.parent if pm.store.root.name == "memory" else pm.store.root
    excluded = Eligibility(owner).excluded_ids()
    has_current_head = False
    for field, head in positions.get(symbol, {}).items():
        if head["temporal_status"] != "current":
            continue
        ident = head["observation_id"]
        receipt = _receipt(directory, ident, completed[ident])["payload"]
        tail = _history_binding_tail(history, symbol, ident, head, receipt, directory, completed)
        if (holding is not None and tail is not None
                and _domain_of(pm.portfolio) == receipt["execution_domain"]
                and _position_binding_matches(holding, field, head, receipt, tail)
                # A field can remain independently confirmed after another
                # field changes, but it cannot explain a newer full snapshot.
                and digest(_position(holding)) == receipt["after_position_sha256"]
                and (field == "current_value" or _native_mapping(holding, head["reported"].get("native_instrument"),
                                                                head["reported"].get("unit"))[0])
                and digest(_estimate_rows(estimates, symbol)) == head["estimates_sha256"]
                and not ({"account_observation:" + ident, *receipt["parent_ids"]} & excluded)):
            has_current_head = True
            break
    if (not has_current_head and (not has_activity or positions.get(symbol))
            and include_snapshot and pm.portfolio.get("updated")):
        times.append(_instant(pm.portfolio.get("updated")))
    return not times or as_of >= max(times)


def _history_binding_tail(history, symbol, ident, head, receipt, directory, completed):
    rows = _history_rows(history, symbol)
    tail = []
    while True:
        if (digest(rows) == head["history_sha256"]
                and digest(_without_own_correction(rows, symbol, ident)) == receipt["prior_history_sha256"]):
            return tail
        if not rows:
            return None
        later = _correction_receipt(rows[-1], directory, completed)
        if (later is None or later["observation_id"] == ident
                or _instant(later["as_of"]) != _instant(receipt["as_of"])):
            return None
        # An unchanged field may retain its original history prefix across a
        # verified correction of another field at the identical statement time.
        # Position bindings still invalidate value/quantity fields that changed.
        tail.append(later)
        rows.pop()


def _position_binding_matches(holding, field, head, receipt, tail):
    position = _field_position(holding, field)
    if digest(position) == head["position_sha256"]:
        return True
    if field != "current_value" or not tail:
        return False
    original = receipt["position_bindings"][field]
    # A separately reported value remains the statement's original value when
    # a later-arriving quantity from that exact statement corrects the book.
    # Only the sealed correction in the bound tail can explain a units change.
    return (digest({key: value for key, value in position.items() if key != "units"})
            == digest({key: value for key, value in original.items() if key != "units"})
            and position["units"] == tail[0]["fields"]["current_quantity"]["value"])


def _domain_of(portfolio):
    domain = portfolio.get("ledger_domain")
    if domain in ("paper_simulation", "broker_paper", "broker_unknown", "unknown", "projection"):
        return domain
    return "client_reported"


def _parents(locator, source_hash):
    ids = {"client_source:" + source_hash}
    if isinstance(locator, dict):
        parents = locator.get("parent_ids", [])
        if not isinstance(parents, list) or any(not isinstance(key, str) or not key for key in parents):
            raise ValueError("source parent_ids must be nonempty strings")
        ids.update(parents)
        if "artifact_id" in locator:
            ids.add(_text(locator["artifact_id"], "source artifact_id"))
    return sorted(ids)


def _native_mapping(holding, native, unit):
    """Only backend-defined valuation units can cross an instrument identity."""
    symbol = holding["symbol"]
    if native is None or native != (holding.get("native_instrument") or symbol):
        return False, "native_identity_unconfirmed"
    if unit is None or unit.casefold() != str(holding.get("unit_label") or "share").casefold():
        return False, "native_unit_unconfirmed"
    from openinvest.services.ledger_evidence import valuation_unit_mapping
    return valuation_unit_mapping(holding)


def record_observation(pm, reference_symbol, *, as_of, source_text, source_locator, idempotency_key,
                       reported_value=None, value_currency=None, reported_quantity=None, quantity_unit=None,
                       native_instrument=None, reported_cost=None, cost_currency=None):
    """Record independent fields; only an explicit matching native balance can set absolute fields.

    No source locator is dereferenced. A receipt interrupted before its head
    commit requires reconciliation; retrying never silently reapplies it.
    """
    symbol = _text(reference_symbol, "reference_symbol").strip()
    instant = _instant(as_of)
    observed_at = datetime.now(timezone.utc)
    if instant > observed_at:
        raise ValueError("as_of cannot be in the future")
    source_text = _text(source_text, "source_text")
    key = _text(idempotency_key, "idempotency_key")
    if not isinstance(source_locator, (str, dict)) or not source_locator:
        raise ValueError("source_locator is required")
    if isinstance(source_locator, str):
        _text(source_locator, "source_locator")
    locator = json.loads(json.dumps(source_locator, ensure_ascii=False, allow_nan=False))
    value = _number(reported_value, "reported_value")
    quantity = _number(reported_quantity, "reported_quantity")
    cost = _number(reported_cost, "reported_cost")
    if value is quantity is cost is None:
        raise ValueError("at least one reported field is required")
    value_ccy, cost_ccy = _currency(value_currency, value is not None), _currency(cost_currency, cost is not None)
    unit = _text(quantity_unit, "quantity_unit").strip() if quantity_unit is not None else None
    if quantity is not None and unit is None:
        raise ValueError("reported_quantity requires quantity_unit")
    native = _text(native_instrument, "native_instrument").strip() if native_instrument is not None else None
    source_hash = hashlib.sha256(source_text.encode()).hexdigest()
    parents = _parents(locator, source_hash)
    request = {"reference_symbol": symbol, "as_of": instant.isoformat(), "source_text": source_text,
               "source_locator": locator, "idempotency_key_sha256": digest(key), "reported_value": value,
               "value_currency": value_ccy, "reported_quantity": quantity, "quantity_unit": unit,
               "native_instrument": native, "reported_cost": cost, "cost_currency": cost_ccy,
               "cost_basis": "average_cost_per_native_unit"}
    observation_id = str(uuid.uuid5(_NAMESPACE, digest(key)))
    directory, heads_path = _paths(pm)
    path = directory / (observation_id + ".json")
    with _file_lock(heads_path):
        state = _heads(directory, heads_path, pm.store.read_history())
        if path.exists():
            existing = _receipt(directory, observation_id, state["completed"].get(observation_id))
            if existing["payload"]["request"] != request:
                raise ValueError("idempotency key reused with different observation parameters")
            owner = pm.store.root.parent if pm.store.root.name == "memory" else pm.store.root
            Eligibility(owner).record_audit_lineage("account_observation:" + observation_id,
                existing["payload"], parents=set(existing["payload"]["parent_ids"]))
            return {**existing["payload"], "sha256": existing["sha256"], "replayed": True}
        pm._reload()
        holding = pm.find_holding(symbol)
        if holding is None:
            raise ValueError("reference_symbol must identify an existing tracked position")
        history, estimates = pm.store.read_history(), _estimates_on_disk(pm)
        current = _current_as_of(pm, symbol, instant, history, estimates, state["positions"], state["completed"])
        source_times_allow = _current_as_of(pm, symbol, instant, history, estimates,
                                           state["positions"], state["completed"], include_snapshot=False)
        owner = pm.store.root.parent if pm.store.root.name == "memory" else pm.store.root
        revoked = bool(({"account_observation:" + observation_id, *parents}) & Eligibility(owner).excluded_ids())
        current = current and not revoked
        native_matches, native_reason = _native_mapping(holding, native, unit)
        fields = {}
        if value is not None:
            fields["current_value"] = {"value": value, "currency": value_ccy}
        if quantity is not None:
            fields["current_quantity"] = {"value": quantity, "unit": unit, "native_instrument": native}
        if cost is not None:
            fields["cost_basis"] = {"value": cost, "currency": cost_ccy, "unit": unit,
                                   "native_instrument": native, "basis": "average_cost_per_native_unit"}
        disposition = {field: "revoked" if revoked else "historical" if not current else "current" for field in fields}
        reasons = {}
        for field in ("current_quantity", "cost_basis"):
            if field not in fields:
                continue
            matches = native_matches and (field != "cost_basis" or cost_ccy == holding.get("cost_currency"))
            if current and not matches:
                reason = native_reason or "native_cost_currency_unconfirmed"
                disposition[field] = "recorded_only" if reason == "native_proxy_unit_mapping_unproven" else "native_identity_unconfirmed"
                reasons[field] = reason
            elif not current and not revoked and source_times_allow and matches:
                present = float(holding.get("units" if field == "current_quantity" else "avg_cost") or 0)
                if fields[field]["value"] == present:
                    disposition[field] = "last_observed"
        before = _position(holding)
        after = json.loads(json.dumps(before))
        if disposition.get("current_quantity") == "current":
            after["units"] = quantity
        if disposition.get("cost_basis") == "current":
            after["avg_cost"] = cost
        changed = before != after
        predecessors = {}
        for field, field_disposition in disposition.items():
            if field_disposition in ("current", "last_observed"):
                prior = state["positions"].get(symbol, {}).get(field)
                predecessors[field] = ({"observation_id": prior["observation_id"], "sha256": prior["sha256"]}
                                       if prior is not None else None)
        receipt = {"schema": 1, "observation_id": observation_id, "request": request,
                   "request_sha256": digest(request), "source_authority": AUTHORITY,
                   "source_text_sha256": source_hash, "observed_at": observed_at.isoformat(),
                   "as_of": instant.isoformat(), "reference_symbol": symbol, "fields": fields,
                   "field_disposition": disposition, "field_reasons": reasons, "parent_ids": parents,
                   "field_predecessors": predecessors,
                   "execution_domain": _domain_of(pm.portfolio), "historical_execution_eligible": False,
                   "execution_observed": False, "cash_flow_recorded": False, "portfolio_mutated": changed,
                   "quantity_corrected": before["units"] != after["units"],
                   "before_position_sha256": digest(before), "after_position_sha256": digest(after),
                   "position_bindings": {field: _field_position(after, field) for field in fields},
                   "prior_history_sha256": digest(_history_rows(history, symbol)),
                   "estimates_sha256": digest(_estimate_rows(estimates, symbol)),
                   "status": "revoked" if revoked else "current" if "current" in disposition.values()
                             else "last_observed" if "last_observed" in disposition.values()
                             else "historical" if not current else "recorded_only"}
        from openinvest.core.decision_evidence import _write_once
        envelope = _write_once(path, receipt)
        # Negative lineage must exist before any current head can be published.
        # This only records audit provenance; it grants no decision/learning use.
        Eligibility(owner).record_audit_lineage("account_observation:" + observation_id,
                                               receipt, parents=set(receipt["parent_ids"]))
        if changed:
            with pm.with_portfolio_tx() as portfolio:
                target = next((row for row in portfolio.get("holdings", []) if row.get("symbol") == symbol), None)
                if target is None or digest(_position(target)) != digest(before):
                    raise ValueError("position changed during observation; reconciliation required")
                if disposition.get("current_quantity") == "current":
                    target["units"] = quantity
                if disposition.get("cost_basis") == "current":
                    target["avg_cost"] = cost
            pm._reload()
        reference = {"observation_id": observation_id, "source_locator": locator, "source_text_sha256": source_hash,
                     "observation_sha256": envelope["sha256"], "parent_ids": parents}
        with pm.recording_scope(evidence_basis=BASIS, execution_domain=receipt["execution_domain"],
                                source_reference=reference, trade_date=instant.date().isoformat()):
            if before["units"] != after["units"]:
                pm.store.append_history({"action": BALANCE_CORRECTION_ACTION, "symbol": symbol, "units": quantity,
                    "schema_version": 2, "cash_effect": 0.0, "external": False, "source": "client_balance_observation",
                    "currency": holding.get("cost_currency"), "observation_as_of": instant.isoformat(),
                    "correction_basis": "absolute_balance_not_an_observed_fill"})
            pm.store.append_history({"action": "balance_observation", "symbol": symbol, "schema_version": 2,
                "source": "client_balance_observation", "cash_effect": 0.0, "units_effect": 0.0, "external": False,
                "observation_as_of": instant.isoformat(), "field_disposition": disposition})
        post_history = _history_rows(pm.store.read_history(), symbol)
        post_estimates = _estimate_rows(_estimates_on_disk(pm), symbol)
        positions = state["positions"].setdefault(symbol, {})
        for field, reported in fields.items():
            if disposition[field] in ("current", "last_observed"):
                positions[field] = {"observation_id": observation_id, "sha256": envelope["sha256"],
                    "as_of": instant.isoformat(), "field": field, "reported": reported,
                    "temporal_status": disposition[field],
                    "position_sha256": digest(_field_position(after, field)), "history_sha256": digest(post_history),
                    "estimates_sha256": digest(post_estimates)}
        state["completed"][observation_id] = envelope["sha256"]
        _write_heads(heads_path, state)
        return {**receipt, "sha256": envelope["sha256"], "replayed": False}


def _confirmation_fact(envelope, field, status):
    receipt = envelope["payload"]
    return {"status": status, "observation_id": receipt["observation_id"], "observation_sha256": envelope["sha256"],
        "as_of": receipt["as_of"], "reported": receipt["fields"][field], "source_authority": AUTHORITY,
        "parent_ids": sorted({"account_observation:" + receipt["observation_id"], *receipt["parent_ids"]}),
        "temporal_status": receipt["field_disposition"][field], "independent_broker_verification": False,
        "execution_domain": receipt["execution_domain"]}


def validate_parent_facts(store, ledger_evidence):
    """Verify the exact client balance fields consumed by a sealed context.

    This checks original facts and negative source lineage, not whether their
    heads still describe today's account. A later trade or independent balance
    does not invalidate a historical review condition. No learning or execution
    eligibility is granted by this check.
    """
    try:
        if not isinstance(ledger_evidence, dict):
            raise ValueError("invalid captured ledger evidence")
        parent_ids = ledger_evidence.get("parent_ids", [])
        holdings = ledger_evidence.get("holdings", {})
        if (not isinstance(parent_ids, list) or any(not isinstance(key, str) or not key for key in parent_ids)
                or not isinstance(holdings, dict)):
            raise ValueError("invalid captured observation parents")
        captured_parents = set(parent_ids)
        directory, _ = _store_paths(store)
        owner = store.root.parent if store.root.name == "memory" else store.root
        excluded = Eligibility(owner).excluded_ids()
        if captured_parents & excluded:
            raise ValueError("captured observation parent revoked")
        consumed, receipt_cache, registry = set(), {}, None
        for symbol, grades in holdings.items():
            if not isinstance(symbol, str) or not isinstance(grades, dict):
                raise ValueError("invalid captured holding grades")
            facts = grades.get("balance_observations", {})
            if not isinstance(facts, dict):
                raise ValueError("invalid captured balance fields")
            for field in _FIELDS:
                fact = facts.get(field)
                if grades.get(field) != "client_observed_balance":
                    if isinstance(fact, dict) and fact.get("status") == "client_reported_balance_confirmed":
                        raise ValueError("captured balance field grade mismatch")
                    continue
                if not isinstance(fact, dict):
                    raise ValueError("captured balance field binding missing")
                ident, expected_hash = fact.get("observation_id"), fact.get("observation_sha256")
                if (not isinstance(ident, str) or not isinstance(expected_hash, str)
                        or len(expected_hash) != 64):
                    raise ValueError("captured balance receipt hash missing")
                if ident not in receipt_cache:
                    receipt_cache[ident] = _receipt(directory, ident, expected_hash)
                envelope = receipt_cache[ident]
                receipt = envelope["payload"]
                if (expected_hash != envelope["sha256"] or receipt["reference_symbol"] != symbol
                        or receipt["request"]["reference_symbol"] != symbol
                        or receipt["request"]["as_of"] != receipt["as_of"]
                        or receipt["field_disposition"].get(field) not in ("current", "last_observed")
                        or digest(fact) != digest(_confirmation_fact(envelope, field, "client_reported_balance_confirmed"))):
                    raise ValueError("captured balance field differs from its original receipt")
                ancestors = {"account_observation:" + ident, *receipt["parent_ids"]}
                if not ancestors <= captured_parents or ancestors & excluded:
                    raise ValueError("captured balance source lineage changed or revoked")
                if registry is None:
                    registry = json.loads((owner / "integrity/eligibility.json").read_text())
                    if registry.get("schema") != 1 or not isinstance(registry.get("artifacts"), dict):
                        raise ValueError("captured balance lineage registry invalid")
                artifact = registry["artifacts"].get("account_observation:" + ident)
                if (not isinstance(artifact, dict) or artifact.get("sha256") != envelope["sha256"]
                        or set(artifact.get("parents", [])) != set(receipt["parent_ids"])):
                    raise ValueError("captured balance audit lineage changed")
                consumed.add("account_observation:" + ident)
        if {key for key in captured_parents if key.startswith("account_observation:")} != consumed:
            raise ValueError("captured observation parent has no consumed field binding")
        return {"allowed": True, "reason": "sealed_observation_fields_match", "historical_facts_only": True,
                "historical_execution_eligible": False}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {"allowed": False, "reason": "balance_observation_parent_fact_unverified",
                "historical_facts_only": True, "historical_execution_eligible": False}


def current_confirmations(pm, history, estimates):
    """Read verified field heads; ``estimates`` contains sealed ``{sha256,payload}`` envelopes.

    Never grant eligibility to old fills or returns. Heads cannot rewrite the
    receipt's field binding or pre-observation history and estimate signatures.
    """
    try:
        directory, heads_path = _paths(pm)
        live_history, live_estimates = pm.store.read_history(), _estimates_on_disk(pm)
        state = _heads(directory, heads_path, live_history)
        owner = pm.store.root.parent if pm.store.root.name == "memory" else pm.store.root
        excluded = Eligibility(owner).excluded_ids()
        portfolio = pm.store.read("portfolio")
        if portfolio is None or not isinstance(portfolio.get("holdings"), list):
            raise ValueError("current portfolio unavailable")
        holdings, parents = {}, set()
        receipt_cache = {}
        for symbol, fields in state["positions"].items():
            holding = next((row for row in portfolio.get("holdings") if row.get("symbol") == symbol), None)
            for field, head in fields.items():
                if field not in _FIELDS or not isinstance(head, dict):
                    raise ValueError("invalid observation field head")
                ident = head["observation_id"]
                if ident not in receipt_cache:
                    receipt_cache[ident] = _receipt(directory, ident, state["completed"].get(ident))
                envelope = receipt_cache[ident]
                receipt = envelope["payload"]
                ancestors = {"account_observation:" + ident, *receipt["parent_ids"]}
                parents.update(ancestors)
                history_tail = _history_binding_tail(history, symbol, ident, head, receipt, directory, state["completed"])
                live_tail = _history_binding_tail(live_history, symbol, ident, head, receipt, directory, state["completed"])
                stale = (holding is None or _domain_of(portfolio) != receipt["execution_domain"]
                    or history_tail is None or live_tail is None
                    or not _position_binding_matches(holding, field, head, receipt, live_tail)
                    or (field != "current_value" and not _native_mapping(holding,
                        head["reported"].get("native_instrument"), head["reported"].get("unit"))[0])
                    or digest(_estimate_rows(estimates, symbol)) != head["estimates_sha256"]
                    or digest(_estimate_rows(live_estimates, symbol)) != head["estimates_sha256"])
                status = "revoked" if ancestors & excluded else "stale" if stale else "client_reported_balance_confirmed"
                holdings.setdefault(symbol, {})[field] = _confirmation_fact(envelope, field, status)
        fingerprint = digest({"heads": state, "holdings": holdings, "excluded_ids": sorted(excluded)})
        return {"status": "ok", "holdings": holdings, "parent_ids": sorted(parents), "fingerprint": fingerprint,
                "historical_execution_eligible": False}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {"status": "unavailable", "reason": "balance_observation_integrity_failure", "holdings": {},
                "parent_ids": [], "fingerprint": None, "historical_execution_eligible": False}
