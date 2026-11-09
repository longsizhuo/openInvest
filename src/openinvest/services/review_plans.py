"""Persistent review conditions. A trigger requests review, never authorizes a trade."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import operator
import os
import re
import sqlite3
import uuid
from typing import Any

from openinvest.core.evidence_eligibility import digest
from openinvest.core.memory_store import MemoryStore, append_jsonl
from openinvest.utils.symbols import safe_symbol

OPS = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le}
METRICS = {"quote_price", "price_cny_per_gram", "vix", "rsi", "price_vs_ma20_pct", "price_vs_ma120_pct"}


class PlanBrief(str):
    def __new__(cls,text,parents=()):
        value=super().__new__(cls,text);value.plan_refs=list(parents);return value


def _plan_payload(record):
    return {key:record.get(key) for key in ('symbol','purpose','plan_id','spec','created_at','evidence_id',
                                           'replaces_plan_id','change_reason','execution_authorized')}


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
    if plan is None and not store.state_get(key):
        return {}  # Absence is not a plan; do not manufacture empty state files.
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
                if isinstance(plan,dict):
                    if plan.get('purpose',purpose)!=purpose:raise ValueError('plan_purpose_mismatch')
                    if plan.get('plan_id') is not None and plan['plan_id']!=record.get('plan_id'):
                        raise ValueError('plan_id_not_owned_by_symbol_and_purpose')
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
            rejustified=bool(same and evidence_id and evidence_id!=record.get('evidence_id')
                            and plan.get('replaces_plan_id')==record.get('plan_id')
                            and str(plan.get('change_reason') or '').strip())
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
            if not same or rejustified:
                record = {"symbol": symbol, "purpose": purpose, "plan_id": uuid.uuid4().hex, "spec": spec,
                          "state": "active", "created_at": now.isoformat(), "changed_at": now.isoformat(),
                          "evidence_id": evidence_id, "replaces_plan_id": record.get("plan_id"),
                          "change_reason": plan.get("change_reason"), "execution_authorized": False}
                from openinvest.core.decision_evidence import _write_once
                _write_once(store.root/'.evidence/review_plans'/f"{int(record['plan_id'],16):032x}.json",_plan_payload(record))
                emit("created", record)
        return finish(record)

    result = store.state_update(key, change)
    for kind, snapshot in events:
        append_jsonl(store.root / ".evidence/review_plan_events.jsonl", {
            "event": kind, "at": now.isoformat(), "symbol": symbol, "record": snapshot,
            "observations": observations or {}, "execution_authorized": False,
        })
    return result


def plan_qualification(store: MemoryStore, symbol: str, record: dict, *, purpose='review',_seen=frozenset(),_audit=None) -> dict:
    """A conditional plan outlives the six-hour advice cache, not its evidence.

    Bind to its sealed, qualified parent proposal and exact purpose/spec. Price
    changes alone do not erase an existing trigger. Missing lineage stays audit
    only and cannot put an unverified old target back into a model's account facts.
    """
    from openinvest.core.decision_evidence import advice_qualified,code_version
    from openinvest.core.evidence_eligibility import Eligibility
    if _audit is None:_audit={'memo':{},'nodes':0,'code_version':None}
    memo_key=None
    try:
        plan_id=record.get('plan_id')
        if not re.fullmatch(r'[0-9a-f]{32}',plan_id or '') or plan_id in _seen or len(_seen)>=128:
            raise ValueError('plan_lineage_missing_cyclic_or_too_deep')
        memo_key=(symbol,purpose,plan_id,digest(_plan_payload(record)))
        if memo_key in _audit['memo']:return _audit['memo'][memo_key]
        if _audit['nodes']>=128:raise ValueError('plan_lineage_node_limit')
        _audit['nodes']+=1
        sealed_plan=json.loads((store.root/'.evidence/review_plans'/f'{int(plan_id,16):032x}.json').read_text())
        if sealed_plan['sha256']!=digest(sealed_plan['payload']) or sealed_plan['payload']!=_plan_payload(record):
            raise ValueError('plan_identity_or_content_changed')
        ident=record.get('evidence_id')
        if not re.fullmatch(r'[0-9a-f]{32}',ident or ''):raise ValueError('parent_decision_missing')
        envelope=json.loads((store.root/'.evidence/decisions'/f'{int(ident,16):032x}.json').read_text())
        parent=envelope['payload']
        if envelope['sha256']!=digest(parent) or parent['symbol']!=symbol or parent.get('decision_id')!=ident:raise ValueError('parent_decision_changed')
        if parent.get('origin')!='prospective' and not (parent.get('origin')=='synthetic' and os.getenv('INVEST_TEST_ISOLATED')=='1'):
            raise ValueError('parent_is_not_a_prospective_decision')
        proposal=parent['validated_proposal']
        if not advice_qualified(proposal):raise ValueError('parent_proposal_unqualified')
        if record.get('purpose','review')!=purpose:raise ValueError('plan_purpose_mismatch')
        field='risk_review_plan' if purpose=='risk' else 'review_plan'
        expected=validate_plan(proposal[field],datetime.fromisoformat(record['created_at']),symbol)
        if digest(canonical_spec(record['spec']))!=digest(expected):raise ValueError('plan_spec_differs_from_parent_proposal')
        if _audit['code_version'] is None:_audit['code_version']=code_version()
        if parent.get('code_version')!=_audit['code_version']:raise ValueError('parent_decision_version_requires_review')
        context_id=parent.get('context_id')
        if not re.fullmatch(r'[0-9a-f]{32}',context_id or ''):raise ValueError('parent_context_missing')
        context_envelope=json.loads((store.root/'.evidence/contexts'/f'{int(context_id,16):032x}.json').read_text())
        context=context_envelope['payload']
        if (context_envelope['sha256']!=digest(context) or context['symbol']!=symbol
                or context.get('context_id')!=context_id):raise ValueError('parent_context_changed')
        from openinvest.core.decision_evidence import _check_referenced_audits
        if 'referenced_audit_records' in context:
            _check_referenced_audits(store.data_root,context['referenced_audit_records'])
        elif context.get('source_event_versions') or context['inputs'].get('source_plan_refs'):
            raise ValueError('legacy_parent_audit_lineage_requires_review')
        excluded=Eligibility(store.data_root).excluded_ids()
        if {'decision:'+ident,'context:'+context_id,'review_plan:'+str(record.get('plan_id'))}&excluded:
            raise ValueError('parent_evidence_revoked')
        if set((context['inputs'].get('ledger_evidence') or {}).get('parent_ids',[]))&excluded:
            raise ValueError('parent_account_observation_revoked')
        from openinvest.services.balance_observations import validate_parent_facts
        if not validate_parent_facts(store,context['inputs'].get('ledger_evidence') or {})['allowed']:
            raise ValueError('parent_account_observation_fact_unverified')
        if not context.get('source_event_lineage_captured') or context['inputs'].get('prior_insights'):
            raise ValueError('parent_source_lineage_requires_review')
        if not context.get('source_plan_lineage_captured'):raise ValueError('parent_plan_lineage_requires_review')
        forecast=context['inputs'].get('historical_qualification') or {}
        if forecast.get('forecast_use_allowed') is True:
            from openinvest.services.historical_evidence import qualification
            if qualification(store.data_root)!=forecast:raise ValueError('parent_forecast_qualification_changed')
        refs=context['inputs'].get('source_plan_refs',[])
        if not isinstance(refs,list) or len(refs)>16:raise ValueError('invalid_parent_plan_refs')
        for ref in refs:
            ancestor_id=ref['plan_id']
            if not re.fullmatch(r'[0-9a-f]{32}',ancestor_id or ''):raise ValueError('invalid_parent_plan_id')
            ancestor=json.loads((store.root/'.evidence/review_plans'/f'{int(ancestor_id,16):032x}.json').read_text())
            if ancestor['sha256']!=ref['artifact_sha256']:raise ValueError('parent_plan_ref_changed')
            value=ancestor['payload']
            if not plan_qualification(store,symbol,value,purpose=value['purpose'],_seen=_seen|{plan_id},_audit=_audit)['allowed']:
                raise ValueError('ancestor_plan_or_source_unqualified')
        versions=context.get('source_event_versions',{})
        if versions:
            from openinvest.db.event_store import EventStore
            database=EventStore(str(store.data_root/'db/events.db'),readonly=True)
            try:
                for eid,version in versions.items():
                    from openinvest.services.source_reporting import event_version_binding
                    if not event_version_binding(database,eid,version)['allowed']:
                        raise ValueError('parent_source_changed_or_revoked')
            finally:database.conn.close()
        result={'allowed':True,'reason':'sealed_parent_plan_matches','historical_condition_only':True,
                'artifact_sha256':sealed_plan['sha256']}
        _audit['memo'][memo_key]=result
        return result
    except (OSError,ValueError,KeyError,TypeError,AttributeError,sqlite3.Error) as exc:
        detail=str(exc) if isinstance(exc,ValueError) and re.fullmatch(r'[a-z][a-z0-9_]+',str(exc)) else type(exc).__name__
        result={'allowed':False,'reason':'plan_parent_or_source_qualification_unverified','detail':detail}
        return result


def plan_brief(store: MemoryStore, symbol: str, *, purpose=None) -> str:
    if purpose is None:
        briefs=[plan_brief(store, symbol, purpose=p) for p in ("review", "risk")]
        return PlanBrief("\n".join(briefs),[ref for brief in briefs for ref in getattr(brief,'plan_refs',[])])
    if not store.state_get(_key(symbol, purpose)):
        return ""
    record = update_plan(store, symbol, purpose=purpose)
    if not record:
        return ""
    qualification=plan_qualification(store,symbol,record,purpose=purpose)
    store.state_update(_key(symbol,purpose),lambda old:({**old,'qualification':qualification},None))
    record['qualification']=qualification
    if not qualification['allowed']:
        # Preserve the immutable target and its alerts for operator audit, but
        # expose only an identity/replacement reference, never its old price.
        return '\n[QUALITY_ALERT] '+json.dumps({'purpose':purpose,'plan_id':record.get('plan_id'),
            'state':record.get('state'),'qualification':qualification,
            'instruction':'旧生成计划只供审计，不是用户止损线、等待理由或当前风险判断。新计划须重新论证；替换时引用该ID和理由。'},ensure_ascii=False)
    visible = {k: v for k, v in record.items() if k != "alerts"}
    visible["source_type"] = "prior_generated_plan_not_a_user_instruction"
    return PlanBrief("\n=== EXISTING REVIEW PLAN ===\n" + json.dumps(visible, ensure_ascii=False)
            + "\n先复核旧条件。更换等待条件必须提供 replaces_plan_id 和 change_reason；触发只要求复核，不授权交易。",
            [{'plan_id':record['plan_id'],'artifact_sha256':qualification['artifact_sha256']}])


def observe_prepared_market(store: MemoryStore, symbol: str, frame) -> None:
    """Evaluate the existing plan before the new prompt can move its conditions."""
    if not any(store.state_get(_key(symbol,p)) for p in ("review", "risk")):
        return
    from openinvest.core.config import load_config
    observation = frame.attrs.get('source_observation') or {}
    if (not frame.empty and not frame.attrs.get("yf_fetch_failed")
            and observation.get('quote_fresh') is True):
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
                and (frame.attrs.get('source_observation') or {}).get('quote_fresh') is True)
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
            if quote is not None and not quote.is_stale and not getattr(quote, 'is_reference', False):
                observations["price_cny_per_gram"] = {"value": quote.price, "unit": "CNY/g"}
    except Exception:
        # Missing observations cannot satisfy a condition; do not invent them.
        pass
    record = update_plan(store, symbol, observations=observations, now=now, purpose=purpose)
    ident=queue_plan_reviews(store,symbol,[record],now=now) if queue else None
    return {**record,"notification_id":ident} if ident else record


def queue_plan_reviews(store,symbol,records,*,now=None):
    """Generated plans stay out of news RAG; notify and review, never trade."""
    records=[r for r in records if r and r.get("state") in ("triggered","expired")
             and plan_qualification(store,symbol,r,purpose=r.get('purpose','review'))['allowed']]
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
