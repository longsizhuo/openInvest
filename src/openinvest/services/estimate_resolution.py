"""Read-only resolution of fund NAV projections by explicit order confirmations.

The original estimate is always retained. Public NAV, a balance observation,
an automatic confirmation or a terminal status alone cannot prove a fill.
Paper confirmations resolve only within their separately reported paper domain.
"""
from __future__ import annotations

from datetime import date
import math

from openinvest.calc.ledger_replay import execution_basis, execution_domain
from openinvest.core.evidence_eligibility import digest

_CONFIRMATION_BASIS = "client_reported_nav_and_fee"
_POSITIVE_BASES = {"client_reported_not_independently_verified", "broker_observed_fill"}
_REAL_DOMAINS = {"client_reported", "broker_live"}
_PAPER_DOMAINS = {"paper_simulation", "broker_paper"}


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _day(value):
    if not isinstance(value, str):
        return None
    try:
        return value if date.fromisoformat(value).isoformat() == value else None
    except ValueError:
        return None


def _estimate(envelope):
    if not isinstance(envelope, dict) or not isinstance(envelope.get("payload"), dict):
        raise ValueError("invalid_estimate_receipt")
    payload = envelope["payload"]
    identity = payload.get("identity")
    if (envelope.get("sha256") != digest(payload) or not isinstance(identity, dict)
            or payload.get("estimate_id") != digest(identity) or identity.get("kind") != "tracking_estimate"
            or not isinstance(identity.get("symbol"), str) or not identity["symbol"]
            or _day(identity.get("date")) is None or not isinstance(identity.get("plan"), dict)):
        raise ValueError("invalid_estimate_receipt")
    return payload, identity, identity["plan"]


def _confirmation(row, *, symbol, amount, planned):
    if row.get("symbol") != symbol:
        return "confirmation_symbol_mismatch", None
    if row.get("confirmation_basis") != _CONFIRMATION_BASIS:
        return "explicit_actual_nav_and_fee_confirmation_missing", None
    basis, domain = execution_basis(row), execution_domain(row)
    raw_domain = row.get("execution_domain")
    if raw_domain not in _REAL_DOMAINS | _PAPER_DOMAINS:
        return "confirmation_domain_unknown_or_unqualified", None
    if domain != raw_domain or basis not in _POSITIVE_BASES:
        return "confirmation_source_or_domain_unqualified", None
    if row.get("status") not in (None, "executed", "confirmed"):
        return "confirmation_status_not_executed", None
    actual_amount, nav, units = (_number(row.get(key)) for key in ("amount", "price", "units"))
    fee = _number(row.get("fee")) if "fee" in row else None
    if actual_amount is None or actual_amount != amount:
        return "confirmation_amount_mismatch", None
    if fee is None or fee < 0 or fee >= amount:
        return "actual_fee_unknown_or_invalid", None
    if nav is None or nav <= 0 or units is None or units <= 0:
        return "actual_nav_or_quantity_invalid", None
    if row.get("currency") != "CNY":
        # This producer's place_fund_order contract records native CNY amounts.
        return "confirmation_currency_mismatch_or_unknown", None
    nav_day, confirm_day = _day(row.get("nav_date")), _day(row.get("trade_date"))
    actual_plan = _day(row.get("nav_date_planned")) if "nav_date_planned" in row else nav_day
    if (actual_plan != planned or nav_day is None or confirm_day is None
            or nav_day < planned or confirm_day < nav_day):
        return "confirmation_order_dates_mismatch", None
    if row.get("cash_effect") != 0 or row.get("external") is not False:
        return "confirmation_cash_effect_invalid", None
    effect = _number(row.get("units_effect"))
    if effect is None or effect != units:
        return "confirmation_quantity_effect_mismatch", None
    calculated = (amount - fee) / nav
    if not math.isfinite(calculated):
        return "confirmation_quantity_arithmetic_invalid", None
    # PM records units to six decimal places. This is its storage precision,
    # not a return tolerance or a threshold learned from observed outcomes.
    tolerance = 0.5e-6 + max(math.ulp(units), math.ulp(calculated))
    if abs(units - calculated) > tolerance:
        return "confirmation_quantity_arithmetic_mismatch", None
    try:
        history_hash = digest(row)
    except (TypeError, ValueError):
        return "confirmation_content_not_hashable", None
    return None, {"resolution_history_sha256": history_hash, "execution_domain": domain,
                  "evidence_basis": basis, "real_execution_eligible": domain in _REAL_DOMAINS,
                  "paper_execution": domain in _PAPER_DOMAINS, "actual_nav": nav,
                  "actual_fee": fee, "actual_quantity": units, "amount": amount,
                  "currency": "CNY", "nav_date_planned": planned, "nav_date": nav_day,
                  "confirm_date": confirm_day, "independent_broker_verification": False}


def classify_estimates(estimates_envelopes, history):
    """Return original pending envelopes and content-bound resolution receipts.

    Only fund_nav_projection can be resolved here. A matching confirmation is
    authoritative even when its actual NAV/fee differ from the public projection.
    No files, portfolio fields, eligibility registry entries or inputs are changed.
    """
    if not isinstance(estimates_envelopes, list) or not isinstance(history, list):
        raise ValueError("estimates_and_history_must_be_lists")
    if any(not isinstance(row, dict) for row in history):
        return {"status": "unavailable", "pending": list(estimates_envelopes), "resolved": [],
                "unresolved": [{"reason": "confirmation_history_invalid"}], "fingerprint": None}
    pending, resolved, unresolved = [], [], []
    integrity_failure = False
    for envelope in estimates_envelopes:
        try:
            payload, identity, plan = _estimate(envelope)
        except (TypeError, ValueError, KeyError):
            pending.append(envelope)
            unresolved.append({"reason": "invalid_estimate_receipt"})
            integrity_failure = True
            continue
        details = {"estimate_id": payload["estimate_id"], "receipt_sha256": envelope["sha256"],
                   "order_id": plan.get("order_id"), "symbol": identity["symbol"]}
        reason, resolution = None, None
        order_id, amount, planned = plan.get("order_id"), _number(plan.get("amount")), _day(plan.get("nav_date_planned"))
        if plan.get("purpose") != "fund_nav_projection":
            reason = "estimate_kind_not_resolvable_by_fund_confirmation"
        elif not isinstance(order_id, str) or not order_id or amount is None or amount <= 0 or planned is None:
            reason = "fund_projection_order_identity_invalid"
        elif plan.get("currency", "CNY") != "CNY":
            reason = "fund_projection_native_currency_unsupported"
        else:
            rows = [(seq, row) for seq, row in enumerate(history) if row.get("order_id") == order_id]
            confirmations = [(seq, row) for seq, row in rows if row.get("action") == "fund_order_confirmed"]
            placed = [row for _, row in rows if row.get("action") == "fund_order_placed"]
            if any(row.get("action") == "fund_order_cancelled" for _, row in rows):
                reason = "order_cancelled_not_resolved_as_a_fill"
            elif not confirmations:
                reason = "actual_order_confirmation_missing"
            elif len(confirmations) != 1:
                reason = "duplicate_or_conflicting_order_confirmations"
            elif len(placed) > 1:
                reason = "duplicate_or_conflicting_order_placements"
            elif any(row.get("symbol") != identity["symbol"] or _number(row.get("amount")) != amount
                     or _day(row.get("nav_date")) != planned or row.get("currency") != "CNY" for row in placed):
                reason = "placed_order_identity_conflict"
            else:
                seq, confirmed = confirmations[0]
                reason, resolution = _confirmation(confirmed, symbol=identity["symbol"], amount=amount, planned=planned)
                if resolution is not None:
                    resolution["resolution_history_seq"] = seq
                    # Known placed and confirmed domains must describe the same book.
                    if any(row.get("execution_domain") is not None
                           and (row["execution_domain"] not in _REAL_DOMAINS | _PAPER_DOMAINS
                                or execution_domain(row) != row["execution_domain"]
                                or row["execution_domain"] != resolution["execution_domain"]) for row in placed):
                        reason, resolution = "placed_and_confirmed_execution_domains_conflict", None
        if reason is not None:
            pending.append(envelope)
            unresolved.append({**details, "reason": reason})
        else:
            resolved.append({**details, **resolution, "resolution_kind": "explicit_fund_order_confirmation",
                             "original_projection_retained": True})
    if integrity_failure:
        # A broken input set must not be presented as completely classified.
        return {"status": "unavailable", "pending": list(estimates_envelopes), "resolved": [],
                "unresolved": unresolved, "fingerprint": None}
    return {"status": "ok", "pending": pending, "resolved": resolved, "unresolved": unresolved,
            "fingerprint": digest({"input_receipt_hashes": [row["sha256"] for row in estimates_envelopes],
                                   "resolved": resolved, "unresolved": unresolved})}
