"""Persistent review conditions. A trigger requests review, never authorizes a trade."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import operator
import re
import uuid
from typing import Any

from openinvest.core.evidence_eligibility import digest
from openinvest.core.memory_store import MemoryStore, append_jsonl
from openinvest.utils.symbols import safe_symbol

OPS = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le}
METRICS = {"quote_price", "price_cny_per_gram", "vix", "rsi", "price_vs_ma20_pct", "price_vs_ma120_pct"}


def _key(symbol: str, purpose: str) -> str:
    if purpose not in ("review", "risk"):
        raise ValueError("unsupported review plan purpose")
    return f"review_plan_{'risk_' if purpose == 'risk' else ''}{safe_symbol(symbol)}"


def _now(now=None):
    return now or datetime.now(timezone.utc)


def quote_unit(symbol: str) -> str | None:
    from openinvest.calc.regime_probability import quote_currency_iso
    symbol = symbol.upper()
    # Some exchanges quote in subunits (e.g. pence) or multiple currencies.
    # A currency suffix alone is insufficient to certify the native price scale.
    if symbol.endswith((".L", ".JO", ".TA")):
        return None
    if symbol == "GC=F":
        return "USD/oz"
    if symbol == "^TNX":
        return "percent"
    if symbol.startswith("^"):
        return "points"
    if symbol.startswith("FUND:"):
        return "CNY/unit"
    if symbol.endswith("=X") and len(symbol[:-2]) in (3, 6):
        return symbol[:-2][-3:] + "/unit"
    return f"{quote_currency_iso(symbol) or 'UNKNOWN'}/unit"


def canonical_unit(metric: str, unit):
    if not isinstance(unit, str):
        return unit
    unit = unit.strip()
    if metric == "quote_price" and unit in {"HKD", "AUD"}:
        return unit + "/unit"
    if metric == "quote_price" and (match := re.fullmatch(r"([A-Z]{3})/(?:share|shares|股|份|unit)",unit)):
        return match[1]+"/unit"
    return {"%": "percent", "点": "points", "point": "points", "CNY/克": "CNY/g"}.get(unit, unit)


def canonical_spec(spec: dict) -> dict:
    # Interpret equivalent unit spelling without moving a saved target or expiry.
    return {**spec, "conditions": [{**c, "unit": canonical_unit(c.get("metric"), c.get("unit"))}
                                    for c in spec["conditions"]]}


def validate_plan(plan: Any, now: datetime, symbol: str) -> dict:
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
    expected_units = {"quote_price": quote_unit(symbol), "price_cny_per_gram": "CNY/g",
                      "vix": "points", "rsi": "points", "price_vs_ma20_pct": "percent", "price_vs_ma120_pct": "percent"}
    conditions = [{**c, "unit": canonical_unit(c["metric"], c["unit"])} for c in conditions]
    for c in conditions:
        if expected_units[c["metric"]] is None:
            raise ValueError(f"native_quote_unit_unverified: {symbol}")
        if c["unit"] != expected_units[c["metric"]] or (c["metric"] == "price_cny_per_gram" and symbol.upper() != "GC=F"):
            raise ValueError(f"review condition unit/asset mismatch: {c['metric']} requires {expected_units[c['metric']]}")
    if plan.get("logic", "all") not in ("all", "any"):
        raise ValueError("invalid condition logic")
    return {"conditions": conditions, "logic": plan.get("logic", "all"),
            "expires_at": expires.isoformat(), "intent": "review_only"}


def check_conditions(spec: dict, observations: dict) -> bool:
    results = []
    for c in spec["conditions"]:
        value = observations.get(c["metric"])
        if (not isinstance(value, dict) or canonical_unit(c["metric"], value.get("unit")) != canonical_unit(c["metric"], c["unit"])
                or value.get("stale") or not isinstance(value.get("value"), (int, float))
                or not math.isfinite(value["value"])):
            results.append(False)
        else:
            results.append(OPS[c["op"]](value["value"], c["value"]))
    return all(results) if spec.get("logic") == "all" else any(results)


def update_plan(store: MemoryStore, symbol: str, plan: Any = None, *,
                observations: dict | None = None, evidence_id: str = "", now=None, purpose="review") -> dict:
    now = _now(now)
    key = _key(symbol, purpose)
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
                spec = validate_plan(plan, now, symbol)
            except (ValueError, TypeError, KeyError) as e:
                changed_error = record.get("last_error") != str(e)
                record["last_error"] = str(e)
                record.setdefault("first_error_at", now.isoformat())
                record.setdefault("changed_at", now.isoformat())
                if changed_error:
                    emit("invalid_plan", record)
                return finish(record)
            same = record.get("spec") and digest(spec) == digest(canonical_spec(record["spec"]))
            if record.get("plan_id") and not same:
                if (plan.get("replaces_plan_id") != record["plan_id"]
                        or not str(plan.get("change_reason") or "").strip()):
                    record.update(last_error="replacement_requires_old_plan_id_and_reason", changed_at=now.isoformat())
                    record.setdefault("first_error_at", now.isoformat())
                    emit("replacement_rejected", record)
                    return finish(record)
            if same:
                record.pop("last_error", None)
                record.pop("first_error_at", None)
            if not same:
                record = {"symbol": symbol, "purpose": purpose, "plan_id": uuid.uuid4().hex, "spec": spec,
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


def plan_brief(store: MemoryStore, symbol: str, *, purpose=None) -> str:
    if purpose is None:
        return "\n".join(plan_brief(store, symbol, purpose=p) for p in ("review", "risk"))
    if not store.state_get(_key(symbol, purpose)):
        return ""
    record = update_plan(store, symbol, purpose=purpose)
    if not record:
        return ""
    visible = {k: v for k, v in record.items() if k != "alerts"}
    visible["source_type"] = "prior_generated_plan_not_a_user_instruction"
    return ("\n=== EXISTING REVIEW PLAN ===\n" + json.dumps(visible, ensure_ascii=False)
            + "\n先复核旧条件。更换等待条件必须提供 replaces_plan_id 和 change_reason；触发只要求复核，不授权交易。")


def observe_prepared_market(store: MemoryStore, symbol: str, frame) -> None:
    """Evaluate the existing plan before the new prompt can move its conditions."""
    if not any(store.state_get(_key(symbol,p)) for p in ("review", "risk")):
        return
    from openinvest.core.config import load_config
    if (not frame.empty and not frame.attrs.get("yf_fetch_failed")
            and 0 <= (_now().date() - frame.index[-1].date()).days <= load_config().staleness.price_stale_days):
        observe_quote(store, symbol, float(frame["Close"].iloc[-1]), queue=False)


def observe_quote(store: MemoryStore, symbol: str, price: float, *, now=None, queue=True, purpose=None) -> dict:
    if purpose is None:
        primary = observe_quote(store, symbol, price, now=now, queue=False, purpose="review")
        downside = observe_quote(store, symbol, price, now=now, queue=False, purpose="risk")
        ident=queue_plan_reviews(store,symbol,[primary,downside],now=now) if queue else None
        record=primary or downside
        return {**record,"notification_id":ident} if ident else record
    current = store.state_get(_key(symbol, purpose))
    if not current:
        return {}
    unit = quote_unit(symbol)
    observations = {"quote_price": {"value": price, "unit": unit}} if unit else {}
    needed = {c["metric"] for c in current.get("spec", {}).get("conditions", [])}
    if unit is None and "quote_price" in needed:
        if current.get("last_error") == f"native_quote_unit_unverified: {symbol}":
            return update_plan(store, symbol, observations={}, now=now, purpose=purpose)
        return update_plan(store, symbol, current["spec"], observations={}, now=now, purpose=purpose)
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
    record = update_plan(store, symbol, observations=observations, now=now, purpose=purpose)
    ident=queue_plan_reviews(store,symbol,[record],now=now) if queue else None
    return {**record,"notification_id":ident} if ident else record


def queue_plan_reviews(store,symbol,records,*,now=None):
    """Generated plans stay out of news RAG; notify and review, never trade."""
    records=[r for r in records if r and r.get("state") in ("triggered","expired")]
    if not records:return None
    from openinvest.services.notification_outbox import NotificationOutbox
    from openinvest.services.notifier import render_markdown_email
    causes=sorted((r["plan_id"],r["state"]) for r in records)
    message=f"{symbol} 已有复核计划达到条件或到期，需要用当前信息重新检查。旧计划及条件已保留，不自动移动目标，也不授权下单。\n"
    message+="\n".join(f"计划 {ident}：{state}" for ident,state in causes)
    payload={"subject":f"openInvest 计划需要复核 — {symbol}","plain":message,"discord":message,
             "html":render_markdown_email(message,footer_label="Review plan"),"plan_causes":causes,
             "followup":{"symbols":[symbol],"event_ids":[],"reason":"existing_generated_plans_require_fresh_review"}}
    return NotificationOutbox(store).enqueue("plan-review:"+symbol+":"+digest(causes),payload,
                                             category="plan_review",policy="decision",now=_now(now))
