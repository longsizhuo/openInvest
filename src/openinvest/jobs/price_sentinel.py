"""Observe price risk independently of investment verdicts.

Scan every five minutes across timezones and record per-asset coverage. Compare
continuous ten-minute moves with prior-session ATR; also compare current one-
and five-session returns with the asset's completed historical distribution.
The current session never enters its own reference distribution.

Detection persists an event and notification before any analysis is requested.
The delivery worker attempts channels separately, then dispatches follow-up
analysis. A failed, disabled or unknown delivery never counts as accepted and
never consumes a successful-delivery cooldown. Historical cooldown files are
not interpreted as proof of delivery. These observations are not forecasts or
trading permissions. Explicit user price lines remain separate from news RAG.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from openinvest.paths import INVEST_ROOT
from openinvest.utils.market_observation import DEFAULT_QUOTE_MAX_AGE

log = logging.getLogger(__name__)

# Old bars cannot establish an immediate observation; closure is a separate provider fact.
_WINDOW_BARS = 2
_MAX_STALE_MIN = DEFAULT_QUOTE_MAX_AGE.total_seconds()/60
# 冷却状态在 memory/.state 里的名字
_STATE_NAME = "price_sentinel_delivery_cooldowns"
# 用户自设报警线：每标的每条规则的冷却（键 "SYM:rule"；价格线值 = 盖章时刻，日涨跌线值 = {"bar", "ts"}）
_ALERT_STATE_NAME = "price_alert_delivery_cooldowns"
_ALERT_COOLDOWN_MIN = 24 * 60
_ALERT_UP_RULES = ("price_above", "day_rise_pct")  # 上行线 = 机会措辞，其余 = 风险


# ---------- 纯函数（可单测） ----------

def _detect_move(
    closes: List[float],
    atr_pct: Optional[float],
    mult: float,
) -> Optional[Dict[str, Any]]:
    """Compare a continuous ten-minute move against verified prior ATR.

    Missing or invalid ATR leaves this detector unavailable; a universal price
    percentage is not substituted. Longer-horizon observations and user lines
    are evaluated separately.
    """
    if len(closes) < _WINDOW_BARS + 1:
        return None
    prev, last = closes[-1 - _WINDOW_BARS], closes[-1]
    if not all(math.isfinite(v) and v > 0 for v in (prev, last)):
        return None
    move_pct = (last / prev - 1.0) * 100.0
    if atr_pct and math.isfinite(atr_pct) and atr_pct > 0:
        ratio: Optional[float] = abs(move_pct) / atr_pct
        if ratio < mult:
            return None
    else:
        # Unknown reference is a coverage gap, not a universal 1% market rule.
        return None
    return {
        "move_pct": move_pct,
        "ratio": ratio,
        "direction": "up" if move_pct > 0 else "down",
    }


def _cooldown_ok(
    state: Dict[str, Any],
    symbol: str,
    direction: str,
    now: datetime,
    cooldown_min: int,
) -> bool:
    """同 symbol 同方向在冷却期内 → False。急涨后急跌属不同方向，各自可报。"""
    rec = (state or {}).get(f"{symbol}:{direction}")
    if not rec:
        return True
    try:
        last = datetime.fromisoformat(rec)
    except (TypeError, ValueError):
        return True
    return (now - last).total_seconds() >= cooldown_min * 60


def _latest_verdict(symbol: str, committee_root: Optional[Path] = None) -> str:
    """读最近一天 transcript 的 verdict 行做报警锚点。拿不到给中性文案，绝不抛。"""
    try:
        root = committee_root or INVEST_ROOT / "memory" / ".committee"
        fname = re.sub(r"[^A-Za-z0-9]", "_", symbol) + ".md"
        candidates = sorted(root.glob(f"*/{fname}"), reverse=True)  # 目录名是 ISO 日期,倒序=最新
        if not candidates:
            return "近期无委员会 verdict"
        text = candidates[0].read_text(encoding="utf-8", errors="ignore")[:2000]
        m = re.search(r"\*\*Verdict\*\*:\s*(\w+)[^\n]*", text)
        date = candidates[0].parent.name
        return f"最近委员会 verdict {m.group(1)}（{date}）" if m else f"最近委员会记录 {date}"
    except Exception as e:  # 锚点只是锦上添花，任何异常不拦报警
        log.warning(f"[{symbol}] 读最近 verdict 失败: {e}")
        return "近期无委员会 verdict"


# ---------- 数据抓取（monkeypatch 点） ----------

def _fetch_frames(symbol: str) -> Optional[Dict[str, Any]]:
    """Independent intraday and reported completed-session observations."""
    import pandas as pd
    import yfinance as yf
    from openinvest.utils.market_observation import history_observation, provider_metadata, frame_observation, utc_time
    from openinvest.utils.exchange_fee import get_history_data
    from openinvest.utils.market_metrics import compute_metrics
    from openinvest.calc.risk_observations import return_risk_observations
    from openinvest.core.config import load_config
    policy = load_config().event
    empty_risk = {'status':'not_available','observations':[]}
    intra = {'status':'unavailable','closes':[],'price':None,'last_bar_utc':None,'risk_observations':empty_risk}
    try:
        ticker = yf.Ticker(symbol)
        bars = ticker.history(period='1d', interval='5m', auto_adjust=True)
        bars = bars.dropna(subset=['Close']) if bars is not None else pd.DataFrame()
        meta = provider_metadata(ticker)
        captured = datetime.now(timezone.utc)
        if not bars.empty:
            stamps = [utc_time(value) for value in bars.index]
            valid = [i for i,stamp in enumerate(stamps) if stamp is not None
                     and stamp+timedelta(minutes=5) <= captured and math.isfinite(float(bars['Close'].iloc[i]))]
            bars = bars.iloc[valid]
        if not bars.empty:
            source = history_observation(bars, metadata=meta, interval='5m', now=captured)
            stamp = utc_time(bars.index[-1])+timedelta(minutes=5)
            age = (captured-stamp).total_seconds()/60
            closes = [float(value) for value in bars['Close']]
            if len(bars) >= 3 and any((utc_time(bars.index[i])-utc_time(bars.index[i-1])).total_seconds()!=300
                                     for i in range(len(bars)-2,len(bars))):
                closes = closes[-1:]
            intra = {'status':'observed' if 0 <= age <= _MAX_STALE_MIN else 'stale',
                'closes':closes,'price':closes[-1],'last_bar_utc':stamp,'session_date':source['session_date'],
                'source_observation':source,'risk_observations':empty_risk}
            source['symbol'] = symbol
            source.update(interval_completed=True,bar_start=utc_time(bars.index[-1]).isoformat(),bar_asof=stamp.isoformat())
    except Exception as exc:
        intra.update(status='failed',error=type(exc).__name__)
    daily = pd.DataFrame()
    daily_source = {}
    day = {'status':'unavailable','risk_observations':empty_risk}
    try:
        daily = get_history_data(symbol,'2y')
        daily = daily.dropna(subset=['Close']) if daily is not None else pd.DataFrame()
        daily_source = frame_observation(daily)
        complete = set(daily_source.get('completed_session_dates',[])) if daily_source.get('history_basis_qualified',True) else set()
        known = daily[[index.date().isoformat() in complete for index in daily.index]]
        if not daily.empty:
            day = {'status':'reference_only','source_observation':daily_source,'risk_observations':empty_risk}
        if not known.empty and daily_source.get('price_basis') in ('adjusted_close','raw_close'):
            current_date = known.index[-1].date().isoformat()
            risk = return_risk_observations(known['Close'].iloc[:-1].tolist(),float(known['Close'].iloc[-1]),
                tail_probability=policy.risk_tail_probability,minimum_samples=policy.risk_min_reference_samples,
                lookback=policy.risk_reference_sessions)
            eligible = bool(daily_source.get('daily_alert_eligible')) and current_date == daily_source.get('session_date')
            day.update(status='observed' if eligible else 'reference_only',price=float(known['Close'].iloc[-1]),
                session_date=current_date,completed_at=(daily_source.get('completion_ends') or {}).get(current_date),
                risk_observations=risk,return_reference={'source_observation':daily_source,
                    'current_session_excluded':True,'prior_max_session_date':known.index[-2].date().isoformat() if len(known)>1 else None,
                    'observations':risk.get('observations',[])})
        if intra['status'] == 'observed':
            session = intra['session_date']
            prior = known[known.index.date < datetime.fromisoformat(session).date()] if not known.empty else known
            intra['atr_pct'] = compute_metrics(prior).get('atr_pct') if not prior.empty else None
            if not prior.empty and intra.get('source_observation',{}).get('session_label_basis') == 'provider_period':
                intra['risk_observations'] = return_risk_observations(prior['Close'].tolist(),intra['price'],
                    tail_probability=policy.risk_tail_probability,minimum_samples=policy.risk_min_reference_samples,
                    lookback=policy.risk_reference_sessions)
            intra['return_reference'] = {'source_observation':daily_source,'current_session_excluded':True,
                'prior_max_session_date':prior.index[-1].date().isoformat() if not prior.empty else None,
                'atr_pct':intra['atr_pct'],'observations':intra['risk_observations'].get('observations',[])}
    except Exception as exc:
        day.update(status='failed',error=type(exc).__name__)
    return {'intraday':intra,'daily':day, 'closes':intra['closes'],'last_bar_utc':intra['last_bar_utc'],
        'price':intra['price'],'atr_pct':intra.get('atr_pct'), 'session_date':intra.get('session_date'),
        'risk_observations':intra['risk_observations'],
        'reference_basis':'provider-reported completed sessions; adjusted scale; contract-roll effects unverified'}


# ---------- 用户自设报警线（C8） ----------

def _alert_targets() -> List[Dict[str, Any]]:
    """strategy.target_assets 里设了 alerts 的标的 → [{symbol, alerts, holding}]。PM 不可用 → []。"""
    try:
        from openinvest.core.portfolio_manager import PortfolioManager

        pm = PortfolioManager()
        from openinvest.services.ledger_evidence import account_source_status
        source=account_source_status(pm)
        return [
            {"symbol": a["symbol"], "alerts": a["alerts"], "holding": pm.holdings.find(a["symbol"]),
             "cost_qualification":(source.get('holdings') or {}).get(a['symbol'],{})}
            for a in (pm.strategy.get("target_assets") or []) if a.get("symbol") and a.get("alerts")
        ]
    except Exception as e:  # 没初始化 / 读失败：这条路径静默让位，不影响 ATR 哨兵
        log.warning(f"[price_alert] 读 target_assets 失败，本轮跳过用户报警线: {e}")
        return []


def _claim_alert(ms: Any, sym: str, rule: str, now: datetime, bar: Optional[str] = None) -> Any:
    """原子冷却：同一把文件锁内「读 → 判冷却 → 盖章」。抢到返回盖的章（给 _release_alert 认领），冷却内返回 None。
    bar=None（价格线）：24h 滚动。bar=行情交易日（日涨跌线）：该交易日已报过就不再报，换了交易日才再报。"""
    stamp = now.isoformat(timespec="seconds")
    key = f"{sym}:{rule}"

    def _fn(cur: Any):
        cur = cur if isinstance(cur, dict) else {}
        if bar is None:
            if not _cooldown_ok(cur, sym, rule, now, _ALERT_COOLDOWN_MIN):
                return cur, None
            mark: Any = stamp
        else:
            prev = cur.get(key)
            if isinstance(prev, dict) and prev.get("bar") == bar:
                return cur, None
            mark = {"bar": bar, "ts": stamp}
        return {**cur, key: mark}, mark

    return ms.state_update(_ALERT_STATE_NAME, _fn)


def _release_alert(ms: Any, sym: str, rule: str, mark: Any) -> None:
    """入库失败时撤回刚盖的章（只撤自己的，别人期间写的不动），下个 tick 重试。"""
    key = f"{sym}:{rule}"
    ms.state_update(_ALERT_STATE_NAME, lambda cur: (
        {k: v for k, v in (cur if isinstance(cur, dict) else {}).items() if not (k == key and v == mark)}, None))


def _accepted_time(receipt, fallback):
    stamps = [c.get("accepted_at") for c in receipt.get("channels", {}).values() if c.get("status") == "accepted"]
    return datetime.fromtimestamp(min(stamps), timezone.utc) if stamps else fallback


def _queue_event(ms, event, *, key, followup_symbols=(), risk_alert=False, cooldown=None, now=None):
    from openinvest.db.event_store import EventStore
    from openinvest.services.event_notifier import send_event_alert_receipt
    from openinvest.services.notification_outbox import NotificationOutbox
    event = {**event, "event_id": hashlib.sha256(key.encode()).hexdigest()[:24]}
    from openinvest.services.source_reporting import record_backend_observation
    witness=record_backend_observation(ms.data_root,event,producer="openinvest.jobs.price_sentinel",
        inputs={"notification_key":key,"observation":event,"market_witness":event.get('_market_witness')})
    event={**event,"_backend_observation_id":witness}
    receipt = send_event_alert_receipt(
        [event], notification_key=key, risk_alert=risk_alert, dispatch=False, store=ms,
        cooldown=cooldown,now=now,
        followup={"symbols": list(followup_symbols), "event_ids": [event["event_id"]],
                  "severity_by_symbol":{symbol:{"low":1,"mid":2,"high":3}.get(event.get("severity"),0) for symbol in followup_symbols}} if followup_symbols else None,
    )
    database=None
    try:
        # A repeated detection key always indexes the original durable payload,
        # including its acquisition witness, rather than a changed local copy.
        with NotificationOutbox(ms).connection() as connection:
            saved=connection.execute("SELECT payload FROM notifications WHERE id=?",(receipt["notification_id"],)).fetchone()
        if saved:
            event=json.loads(saved[0])["events"][0]
        database=EventStore(str(ms.data_root / "db/events.db"))
        from openinvest.services.source_reporting import event_payload
        existing = database.get_event(event['event_id'],purpose='audit')
        support = database._reporting_support(event['event_id'])
        same = (existing is not None and support and support.get('kind') == 'backend_observation'
                and support.get('observation_id') == event.get('_backend_observation_id')
                and event_payload(existing) == event_payload(event))
        if not same:
            database.upsert_event(event,embedding=None)
        receipt["event_index_status"]="stored"
    except Exception as exc:
        receipt["event_index_status"]="pending"
        log.warning("event indexing pending; detection retained in notification outbox: %s",type(exc).__name__)
    finally:
        if database is not None:database.conn.close()
    return receipt


def _check_user_alerts(ms: Any, now: datetime, dry_run: bool) -> List[str]:
    """Persist explicit user-line detections; only accepted receipts consume cooldown."""
    import json
    from openinvest.services.price_alerts import evaluate_symbol, fetch_quote
    class Observations(list):
        pass
    detected = Observations()
    detected.coverage = {}
    for target in _alert_targets():
        sym = target["symbol"]
        try:
            quote = fetch_quote(sym, target["holding"])
            now = datetime.now(timezone.utc)  # Evaluate against the real post-retrieval clock.
            if quote is None or quote.is_stale or not quote.price > 0:
                detected.coverage[sym] = {'status':'unavailable' if quote is None else 'reference_only',
                    'source_observation':getattr(quote,'source_observation',{}) or {}}
                continue
            source = getattr(quote,'source_observation',{}) or {}
            from openinvest.utils.market_observation import utc_time
            stamp = utc_time(getattr(quote,'observed_at',None) or source.get('quote_asof'))
            if (source.get('quote_fresh') is not True or getattr(quote,'is_reference',True)
                    or stamp is None or not 0 <= (now-stamp).total_seconds() <= DEFAULT_QUOTE_MAX_AGE.total_seconds()):
                detected.coverage[sym] = {'status':'reference_only','source_observation':source}
                continue
            detected.coverage[sym] = {'status':'observed','source_observation':source}
            evaluated = evaluate_symbol(sym, target["alerts"], target["holding"], price=quote.price,
                    extra=quote.extra, last_updated=quote.last_updated,
                    cost_qualification=target.get('cost_qualification'),native_quote_qualified=True,quote_currency=quote.currency)
            unavailable=[r['rule'] for r in evaluated if r.get('current') is None]
            if unavailable:
                detected.coverage[sym].update(status='partial_observation',unavailable_rules=unavailable)
            hits = [r for r in evaluated if r["breached"]]
            version = hashlib.sha256(json.dumps(target["alerts"], sort_keys=True).encode()).hexdigest()[:16]
            for hit in hits:
                rule = hit["rule"]; slot = rule+":"+version; bar = hit.get("bar_date")
                from openinvest.services.notification_outbox import NotificationOutbox
                domain=f"user-line:{sym}:{slot}"
                last=None if dry_run else NotificationOutbox(ms).last_accepted(domain,bar=bar)
                if last is not None and (bar or now.timestamp()-last < _ALERT_COOLDOWN_MIN*60):
                    continue
                state = ms.state_get(_ALERT_STATE_NAME, {}) or {}
                old = state.get(f"{sym}:{slot}")
                if (bar and isinstance(old,dict) and old.get("bar") == bar) or (not bar and not _cooldown_ok(state,sym,slot,now,_ALERT_COOLDOWN_MIN)):
                    continue
                detected.append(f"{sym}:{rule}")
                if dry_run:
                    continue
                key = f"user-line:{sym}:{slot}:{bar or now.date().isoformat()}"
                basis = '参考估值（非银行报价）' if getattr(quote,'is_reference',False) else '观察价'
                event = {"one_line_claim": f"{sym} 触发用户报警线：{hit['text']}；现价 {quote.price:.2f} {quote.currency or ''}",
                         "event_type":"price_alert", "stance":"opportunity" if rule in _ALERT_UP_RULES else "risk",
                         "severity":"low", "affected_symbols":[sym], "entities":["user_price_alert",sym.lower()], "ts":now.isoformat()}
                event['one_line_claim'] = event['one_line_claim'].replace('；现价 ', '；'+basis+' ')
                event['one_line_claim'] += '; asof '+str(getattr(quote,'observed_at',None) or quote.last_updated or 'unknown')
                event['_market_witness'] = {'schema':'openinvest.market_witness.v1','kind':'native_quote_alert',
                    'price_basis':getattr(quote,'price_basis','unverified'),'source_symbol':sym,
                    'quote_fresh':True,'is_reference':False,'observation_asof':stamp.isoformat(),'quote_asof':stamp.isoformat(),
                    'source_observation':source,'return_reference':None}
                receipt = _queue_event(ms,event,key=key,risk_alert=True,
                                       now=now,
                                       cooldown={"domain":domain,"bar":bar,"seconds":_ALERT_COOLDOWN_MIN*60})
                if receipt["accepted"]:
                    _claim_alert(ms,sym,slot,_accepted_time(receipt,now),bar)
        except Exception as exc:
            log.warning("[%s] user-line detection/queue failed: %s",sym,type(exc).__name__)
    return detected


# ---------- 主流程 ----------

def _reference_owners(symbols,store):
    """Tracking references are risk information, never substitute target quotes."""
    from openinvest.services.symbol_map import TRACKING_WHITELIST,normalize_symbol
    strategy=store.read("strategy")
    declared={str(r.get("symbol")):r.get("tracks",[]) for r in (strategy.metadata.get("target_assets",[]) if strategy else [])}
    owners={}
    for symbol in symbols:
        extra=declared.get(symbol,[])
        if isinstance(extra,str):extra=[extra]
        references={symbol}|set(TRACKING_WHITELIST.get(normalize_symbol(symbol),()))|set(extra)
        for reference in references:
            owners.setdefault(reference,set()).add(symbol)
    return owners


def _daily_baseline(store, symbol, now, *, day=None, dry_run=False):
    """Only closes occurring after monitoring began can create a new alert."""
    from openinvest.utils.market_observation import utc_time
    def update(old):
        old = old or {}
        value = dict(old.get(symbol) or {'initialized_at':now.isoformat(),'last_completed_session':None})
        allowed = False
        if day and day.get('session_date'):
            label = day['session_date']
            previous = value.get('last_completed_session')
            end, initial = utc_time(day.get('completed_at')), utc_time(value['initialized_at'])
            allowed = (day.get('status') == 'observed' and end is not None and initial is not None
                       and initial < end <= now and (previous is None or label >= previous))
            if previous is None or label > previous:
                value['last_completed_session'] = label
        old[symbol] = value
        return old, allowed
    if dry_run:
        return update(store.state_get('price_sentinel_daily_baseline',{}))[1]
    return store.state_update('price_sentinel_daily_baseline',update)


def _price_condition(hit):
    return {'basis':hit['basis'],'direction':hit['direction'],'severity':hit['severity'],
            'horizons':sorted({int(value['horizon_sessions']) for value in hit.get('observations',[])})}


def _stored_price_condition(payload, hit):
    value = (payload.get('cooldown') or {}).get('condition')
    if isinstance(value,dict):
        return value
    if hit['basis'] == '10_minute':
        return {**_price_condition(hit),'horizons':[]}
    text = ' '.join(event.get('one_line_claim','') for event in payload.get('events',[]))
    horizons = sorted({int(value) for value in re.findall(r'(\d+)交易日累计',text)})
    return {**_price_condition(hit),'horizons':horizons} if horizons else None


def _price_notice_identity(outbox, symbol, hit, period, now):
    """Retain legacy notice IDs, while a new horizon is a different condition."""
    import json
    from openinvest.core.evidence_eligibility import digest
    family = f"{hit['direction']}:{hit['basis']}:{hit['severity']}"
    legacy = f'market-observation:{symbol}:{family}'
    base_key = legacy+':'+str(period)
    condition = _price_condition(hit)
    fingerprint = digest(condition)
    scope = legacy+':condition:'+fingerprint
    with outbox.connection() as db:
        recent = db.execute("""SELECT payload FROM notifications WHERE created>=?
            AND (json_extract(payload,'$.cooldown.domain')=? OR json_extract(payload,'$.cooldown.family_domain')=?)
            ORDER BY created DESC""", (now.timestamp()-86400,legacy,legacy)).fetchall()
        for row in recent:
            payload = json.loads(row['payload'])
            if _stored_price_condition(payload,hit) == condition:
                scope = (payload.get('cooldown') or {}).get('domain') or scope
                break
        ident = hashlib.sha256((outbox.scope+':'+base_key).encode()).hexdigest()
        old = db.execute('SELECT payload FROM notifications WHERE id=?',(ident,)).fetchone()
        if old:
            previous = _stored_price_condition(json.loads(old['payload']),hit)
            if previous is not None and previous != condition:
                base_key += ':condition:'+fingerprint
    return base_key, {'domain':scope,'family_domain':legacy,'condition':condition,'condition_id':fingerprint}


def _record_price_observation(store, symbol, hit, event):
    """Keep the observed fact even when delivery is cooling or unavailable."""
    from openinvest.core.evidence_eligibility import digest
    from openinvest.services.source_reporting import record_backend_observation
    point = {'symbol':symbol,'basis':hit['basis'],'price':hit['price'],'asof':hit['source_asof'],
             'condition':_price_condition(hit),'observations':hit.get('observations',[])}
    audit_event = {**event,'event_id':digest(point)[:24],'ts':hit['source_asof']}
    return record_backend_observation(store.data_root,audit_event,producer='openinvest.jobs.price_sentinel',
        inputs={**point,'market_witness':event.get('_market_witness')})


def run(dry_run: Optional[bool] = None) -> Dict[str, Any]:
    from openinvest.core.config import load_config
    from openinvest.core.memory_store import MemoryStore
    from openinvest.jobs.event_watch import _load_user_context
    from openinvest.services.review_plans import update_plan, observe_quote, quote_unit, queue_plan_reviews
    if dry_run is None:
        dry_run = os.getenv("INVEST_SENTINEL_DRY_RUN", "").lower() in ("1","true")
    cfg = load_config(_force_reload=True)
    if not cfg.event.sentinel_enabled:
        return {"status":"disabled", "checked":0, "alerted":0}
    ctx = _load_user_context(); symbols = list(dict.fromkeys([*ctx["holdings"],*ctx["watching"]]))
    ms = MemoryStore(); now = datetime.now(timezone.utc)
    coverage = {}; detected = []; accepted = []; queued = []
    try:
        user_lines = _check_user_alerts(ms,now,dry_run)
        if getattr(user_lines,'coverage',{}):
            coverage['user_alert_lines'] = {'status':'observed' if all(value['status']=='observed' for value in user_lines.coverage.values()) else 'coverage_unknown',
                                            'assets':user_lines.coverage}
    except Exception as exc:
        user_lines = []
        coverage["user_alert_lines"] = {"status":"evaluation_failed", "error":type(exc).__name__}
    owners=_reference_owners(symbols,ms)
    for sym in owners:
        _daily_baseline(ms,sym,now,dry_run=dry_run)
        if not dry_run and sym in symbols:
            try:
                plans=[update_plan(ms,sym,now=now,purpose=purpose) for purpose in ("review","risk")]
                ident=queue_plan_reviews(ms,sym,plans,now=now)
                if ident:queued.append(ident)
            except Exception as exc:
                coverage["plan:"+sym]={"status":"plan_evaluation_failed","error":type(exc).__name__}
        frames = _fetch_frames(sym)
        if frames is None:
            coverage[sym] = {"status":"fetch_failed_or_unsupported"}; continue
        now = datetime.now(timezone.utc)
        intra = frames.get('intraday')
        day = frames.get('daily') or {'status':'unavailable','risk_observations':{'observations':[]}}
        if intra is None:
            age = (now-frames["last_bar_utc"]).total_seconds()/60
            intra = {**frames,'status':'observed' if 0 <= age <= _MAX_STALE_MIN else 'stale'}
        elif intra.get('last_bar_utc') is not None and (now-intra['last_bar_utc']).total_seconds() > DEFAULT_QUOTE_MAX_AGE.total_seconds():
            intra = {**intra,'status':'stale','source_observation':{**intra.get('source_observation',{}),'quote_fresh':False}}
        observed = intra.get('status') == 'observed'
        coverage[sym] = {'status':'observed' if observed else 'observed_reference' if day.get('status')=='observed'
                         else 'stale_session_unverified' if intra.get('status')=='stale' else 'coverage_unknown',
            'intraday':{'status':intra.get('status'),'last_bar':intra['last_bar_utc'].isoformat() if intra.get('last_bar_utc') else None,
                        'source_observation':intra.get('source_observation',{})},
            'daily':{'status':day.get('status'),'session_date':day.get('session_date'),
                     'completed_at':day.get('completed_at'),'source_observation':day.get('source_observation',{})},
            'return_reference':intra.get('risk_observations',{}).get('status','not_available')}
        quote_eligible = intra.get('source_observation',{}).get('quote_fresh', 'intraday' not in frames)
        if observed and quote_eligible and not dry_run and sym in symbols:
            try:
                plan_observation=observe_quote(ms,sym,intra["price"],now=now)
                if plan_observation and plan_observation.get("notification_id"):queued.append(plan_observation["notification_id"])
            except Exception as exc:
                coverage[sym]["plan_error"]=type(exc).__name__
        hits = []
        vertical = _detect_move(intra.get("closes",[]),intra.get("atr_pct"),cfg.event.sentinel_atr_mult) if observed else None
        if vertical:
            hits.append({**vertical,"basis":"10_minute","severity":"high" if vertical.get("ratio") is not None and vertical["ratio"]>=1.5 else "mid",
                'price':intra['price'],'source_asof':intra['last_bar_utc'].isoformat(),'session_date':intra.get('session_date')})
        tails = [x for x in intra.get("risk_observations",{}).get("observations",[]) if x.get("breached")] if observed else []
        if tails:
            hits.append({"direction":"down","basis":"prior_session_returns", "severity":"high" if any(x["historical_tail_rank"]<=.01 for x in tails) else "mid",
                "observations":tails,'price':intra['price'],'source_asof':intra['last_bar_utc'].isoformat(),'session_date':intra.get('session_date')})
        daily_new = _daily_baseline(ms,sym,now,day=day,dry_run=dry_run)
        daily_tails = [value for value in day.get('risk_observations',{}).get('observations',[]) if value.get('breached')]
        if daily_new and daily_tails:
            hits.append({'direction':'down','basis':'completed_session_returns','severity':'high' if any(value['historical_tail_rank']<=.01 for value in daily_tails) else 'mid',
                'observations':daily_tails,'price':day['price'],'source_asof':day['completed_at'],'session_date':day['session_date']})
        for hit in hits:
            direction=hit["direction"]; family=f"{direction}:{hit['basis']}:{hit['severity']}"
            detected.append(sym)
            if dry_run:
                continue
            session=hit.get("session_date") or now.date().isoformat()
            period=session if hit["basis"] in ("prior_session_returns","completed_session_returns") else str(int(now.timestamp()//max(1,cfg.event.sentinel_cooldown_min*60)))
            if hit["basis"]=="10_minute":
                detail=f"10分钟变化 {hit['move_pct']:+.2f}%"
            else:
                detail="；".join(f"{o['horizon_sessions']}交易日累计 {o['return_pct']:+.2f}%（此前历史下侧参考 {o['threshold_pct']:.2f}%，n={o['sample_count']}）" for o in hit['observations'])
            event={"one_line_claim":f"{sym} {detail}；观察价 {hit['price']:.4f} {quote_unit(sym) or 'unit_unverified'}；asof {hit['source_asof']}。这是已观察变化，不是后续下跌预测或交易指令。",
                   "event_type":"price_action", "stance":"risk" if direction=="down" else "opportunity", "severity":hit["severity"],
                   "affected_symbols":[sym],"entities":["price_observation",sym.lower()],"ts":now.isoformat()}
            affected=sorted(owners[sym])
            if any(target!=sym for target in affected):
                event["one_line_claim"]+=f" 关联资产：{'、'.join(affected)}；这是跟踪参考的变化，不是这些资产的报价，也不保证同幅变动。"
            event["affected_symbols"]=list(dict.fromkeys([sym,*affected]))
            if sym == 'GC=F':
                event['one_line_claim'] += ' 连续期货是风险参考，不是银行积存金或伦敦现货报价；换月与渠道基差未核验。'
            component = day if hit['basis'] == 'completed_session_returns' else intra
            source = component.get('source_observation',{}) or {}
            event['_market_witness'] = {'schema':'openinvest.market_witness.v1',
                'kind':'completed_session_return' if hit['basis']=='completed_session_returns' else 'intraday_price_observation',
                'source_symbol':sym,'price_basis':source.get('price_basis','unverified'),
                'observation_asof':hit['source_asof'],'quote_asof':source.get('quote_asof'),
                'quote_fresh':source.get('quote_fresh') is True,
                'is_reference':source.get('quote_fresh') is not True,
                'source_observation':source,'return_reference':component.get('return_reference')}
            try:
                _record_price_observation(ms,sym,hit,event)
                from openinvest.services.notification_outbox import NotificationOutbox
                box = NotificationOutbox(ms)
                key, cooldown = _price_notice_identity(box,sym,hit,period,now)
                domain = cooldown['domain']
                last = box.last_accepted(domain)
                if last is not None and now.timestamp()-last < cfg.event.sentinel_cooldown_min*60:
                    continue
                state = ms.state_get(_STATE_NAME,{}) or {}
                if not _cooldown_ok(state,sym,domain,now,cfg.event.sentinel_cooldown_min):
                    continue
                receipt=_queue_event(ms,event,key=key,followup_symbols=[target for target in affected if target in ctx["watching"]],risk_alert=direction=="down",
                                     now=now,
                                     cooldown={**cooldown,"seconds":cfg.event.sentinel_cooldown_min*60})
                queued.append(receipt["notification_id"])
                if receipt["accepted"]:
                    accepted.append(sym);stamp=_accepted_time(receipt,now).isoformat()
                    ms.state_update(_STATE_NAME,lambda current,k=f"{sym}:{domain}",v=stamp: ({**(current or {}),k:v},None))
            except Exception as exc:
                coverage[sym]["queue_error"]=type(exc).__name__
    if not dry_run:
        ms.state_set("price_sentinel_coverage",{"at":now.isoformat(),"assets":coverage})
    limited=any(v["status"]!="observed" or v.get("queue_error") or v.get("plan_error") for v in coverage.values())
    return {"status":"coverage_limited" if limited else "ok", "checked":len(symbols),"detected":len(detected),
            "alerted":len(accepted),"accepted_by_channel":len(accepted),"notifications_queued":len(set(queued)),
            "symbols":list(dict.fromkeys(accepted)),"detected_symbols":list(dict.fromkeys(detected)),
            "price_alerts":user_lines,"coverage":coverage,"dry_run":dry_run}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(run())
