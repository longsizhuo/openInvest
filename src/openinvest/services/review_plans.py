"""Persistent review conditions. A trigger requests review, never authorizes a trade."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import operator
import uuid
from typing import Any

from openinvest.core.evidence_eligibility import digest
from openinvest.core.memory_store import MemoryStore, append_jsonl
from openinvest.utils.symbols import safe_symbol

OPS = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le}
METRICS = {"quote_price", "price_cny_per_gram", "vix", "rsi", "price_vs_ma20_pct", "price_vs_ma120_pct"}


def _now(now=None):
    return now or datetime.now(timezone.utc)


def validate_plan(plan: Any, now: datetime) -> dict:
    if not isinstance(plan, dict):
        raise ValueError("review_plan must be an object")
    expires = datetime.fromisoformat(str(plan.get("expires_at", "")))
    if expires.tzinfo is None or expires <= now:
        raise ValueError("review_plan requires a future expiry with timezone")
    conditions = plan.get("conditions")
    if not isinstance(conditions, list) or not 1 <= len(conditions) <= 8:
        raise ValueError("review_plan requires 1 to 8 explicit conditions")
    for c in conditions:
        if (not isinstance(c, dict) or c.get("metric") not in METRICS or c.get("op") not in OPS
                or not isinstance(c.get("unit"), str) or not c["unit"]
                or isinstance(c.get("value"), bool) or not isinstance(c.get("value"), (int, float))
                or not math.isfinite(c["value"])):
            raise ValueError("invalid review condition or missing price unit")
    if plan.get("logic", "all") not in ("all", "any"):
        raise ValueError("invalid condition logic")
    return {"conditions": conditions, "logic": plan.get("logic", "all"),
            "expires_at": expires.isoformat(), "intent": "review_only"}


def check_conditions(spec: dict, observations: dict) -> bool:
    results = []
    for c in spec["conditions"]:
        value = observations.get(c["metric"])
        if (not isinstance(value, dict) or value.get("unit") != c["unit"]
                or value.get("stale") or not isinstance(value.get("value"), (int, float))
                or not math.isfinite(value["value"])):
            results.append(False)
        else:
            results.append(OPS[c["op"]](value["value"], c["value"]))
    return all(results) if spec.get("logic") == "all" else any(results)


def update_plan(store: MemoryStore, symbol: str, plan: Any = None, *,
                observations: dict | None = None, evidence_id: str = "", now=None) -> dict:
    now = _now(now)
    key = f"review_plan_{safe_symbol(symbol)}"
    events = []

    def change(old):
        record = dict(old or {})
        alerts = list(record.get("alerts", []))

        def emit(kind, value):
            snapshot = json.loads(json.dumps(value))
            snapshot.pop("alerts", None)
            events.append((kind, snapshot))
            if kind != "created":
                alerts.append({"id": uuid.uuid4().hex, "kind": kind, "plan_id": value.get("plan_id"),
                               "symbol": symbol, "at": now.isoformat(), "spec": value.get("spec")})

        def finish(value):
            if value or alerts:
                value["alerts"] = alerts
            return value, value
        if record and record.get("state") == "active":
            if datetime.fromisoformat(record["spec"]["expires_at"]) <= now:
                record.update(state="expired", changed_at=now.isoformat())
                emit("expired", record)
            elif check_conditions(record["spec"], observations or {}):
                record.update(state="triggered", changed_at=now.isoformat())
                emit("conditions_met", record)
        if plan is not None:
            try:
                spec = validate_plan(plan, now)
            except (ValueError, TypeError, KeyError) as e:
                record["last_error"] = str(e)
                record.setdefault("first_error_at", now.isoformat())
                record.setdefault("changed_at", now.isoformat())
                emit("invalid_plan", record)
                return finish(record)
            same = record.get("spec") and digest(spec) == digest(record["spec"])
            if record.get("plan_id") and not same:
                if (plan.get("replaces_plan_id") != record["plan_id"]
                        or not str(plan.get("change_reason") or "").strip()):
                    record.update(last_error="replacement_requires_old_plan_id_and_reason", changed_at=now.isoformat())
                    record.setdefault("first_error_at", now.isoformat())
                    emit("replacement_rejected", record)
                    return finish(record)
            if not same:
                record = {"symbol": symbol, "plan_id": uuid.uuid4().hex, "spec": spec,
                          "state": "active", "created_at": now.isoformat(), "changed_at": now.isoformat(),
                          "evidence_id": evidence_id, "replaces_plan_id": record.get("plan_id"),
                          "change_reason": plan.get("change_reason"), "execution_authorized": False}
                emit("created", record)
        return finish(record)

    result = store.state_update(key, change)
    for kind, snapshot in events:
        append_jsonl(store.root / ".evidence/review_plan_events.jsonl", {
            "event": kind, "at": now.isoformat(), "symbol": symbol, "record": snapshot,
            "observations": observations or {}, "execution_authorized": False,
        })
    return result


def plan_brief(store: MemoryStore, symbol: str) -> str:
    if not store.state_get(f"review_plan_{safe_symbol(symbol)}"):
        return ""
    record = update_plan(store, symbol)
    if not record:
        return ""
    visible = {k: v for k, v in record.items() if k != "alerts"}
    visible["source_type"] = "prior_generated_plan_not_a_user_instruction"
    return ("\n=== EXISTING REVIEW PLAN ===\n" + json.dumps(visible, ensure_ascii=False)
            + "\n先复核旧条件。更换等待条件必须提供 replaces_plan_id 和 change_reason；触发只要求复核，不授权交易。")


def observe_prepared_market(store: MemoryStore, symbol: str, frame) -> None:
    """Evaluate the existing plan before the new prompt can move its conditions."""
    if not store.state_get(f"review_plan_{safe_symbol(symbol)}"):
        return
    from openinvest.core.config import load_config
    if (not frame.empty and not frame.attrs.get("yf_fetch_failed")
            and 0 <= (_now().date() - frame.index[-1].date()).days <= load_config().staleness.price_stale_days):
        observe_quote(store, symbol, float(frame["Close"].iloc[-1]), queue=False)


def observe_quote(store: MemoryStore, symbol: str, price: float, *, now=None, queue=True) -> dict:
    from openinvest.calc.regime_probability import quote_currency_iso
    current = store.state_get(f"review_plan_{safe_symbol(symbol)}")
    if not current:
        return {}
    ccy = quote_currency_iso(symbol) or "UNKNOWN"
    unit = f"{ccy}/oz" if symbol == "GC=F" else f"{ccy}/unit"
    observations = {"quote_price": {"value": price, "unit": unit}}
    needed = {c["metric"] for c in current.get("spec", {}).get("conditions", [])}
    def fresh(frame):
        from openinvest.core.config import load_config
        return (not frame.empty and not frame.attrs.get("yf_fetch_failed")
                and 0 <= (_now(now).date() - frame.index[-1].date()).days <= load_config().staleness.price_stale_days)
    try:
        from openinvest.utils.exchange_fee import get_history_data
        from openinvest.utils.market_metrics import compute_metrics, METRICS_PERIOD
        if needed & {"rsi", "price_vs_ma20_pct", "price_vs_ma120_pct"}:
            frame = get_history_data(symbol, METRICS_PERIOD)
            if fresh(frame):
                m = compute_metrics(frame)
                if m.get("rsi14") is not None:
                    observations["rsi"] = {"value": m["rsi14"], "unit": "points"}
                for ma in ("ma20", "ma120"):
                    if m.get(ma) and m[ma] > 0:
                        observations[f"price_vs_{ma}_pct"] = {"value": (price / m[ma] - 1) * 100, "unit": "percent"}
        if "vix" in needed:
            frame = get_history_data("^VIX", "5d")
            if fresh(frame):
                observations["vix"] = {"value": float(frame["Close"].iloc[-1]), "unit": "points"}
        if "price_cny_per_gram" in needed and symbol == "GC=F":
            from openinvest.services.price_alerts import fetch_quote
            quote = fetch_quote(symbol, {"symbol": symbol, "proxy_kind": "gold_cny_per_gram", "cost_currency": "CNY"})
            if quote is not None and not quote.is_stale:
                observations["price_cny_per_gram"] = {"value": quote.price, "unit": "CNY/g"}
    except Exception:
        # Missing observations cannot satisfy a condition; do not invent them.
        pass
    record = update_plan(store, symbol, observations=observations, now=now)
    if queue and record.get("state") in ("triggered", "expired"):
        claim = f"{record['plan_id']}:{record['state']}"
        if store.state_claim("review_plan_queue", claim):
            try:
                from openinvest.utils.llm import reasoning_by_agent
                if reasoning_by_agent():
                    from openinvest.services.agent_queue import enqueue_committee
                    enqueue_committee([symbol], [], reason=f"review_plan:{claim}; reassess existing conditions, not an order")
                else:
                    from openinvest.services.event_trigger import _trigger_committee
                    if not _trigger_committee([symbol], []):
                        raise RuntimeError("review-plan committee could not be scheduled")
            except Exception:
                store.state_unclaim("review_plan_queue", claim)
                raise
    return record
