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
import logging
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from openinvest.paths import INVEST_ROOT

log = logging.getLogger(__name__)

# 10 分钟 = 2 根 5m bar；数据最后一根 bar 距今超过该分钟数视为闭市/停滞，跳过
_WINDOW_BARS = 2
_MAX_STALE_MIN = 25
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
    """拉 5m 收盘序列 + 日 ATR%。返回 {closes, last_bar_utc, price, atr_pct} 或 None。"""
    try:
        import yfinance as yf
        from openinvest.utils.market_metrics import compute_metrics

        t = yf.Ticker(symbol)
        intraday = t.history(period="1d", interval="5m", auto_adjust=True)
        # 末根 Close=NaN（半成型 bar）会让涨跌幅变 NaN、比较全为 False → 这一轮静默不报
        intraday = intraday.dropna(subset=["Close"]) if intraday is not None else None
        if intraday is None or len(intraday) < 1:
            return None
        closes = [float(v) for v in intraday["Close"].tolist()]
        last_bar_utc = intraday.index[-1].tz_convert("UTC").to_pydatetime()

        daily = t.history(period="2y", interval="1d", auto_adjust=True)
        daily = daily.dropna(subset=["Close"]) if daily is not None and len(daily) else daily  # 同上，否则 ATR% 变 None、退回绝对阈值
        session_date = intraday.index[-1].date()
        prior = daily[daily.index.date < session_date] if daily is not None and len(daily) else daily
        atr_pct = compute_metrics(prior).get("atr_pct") if prior is not None and len(prior) else None
        from openinvest.calc.risk_observations import return_risk_observations
        from openinvest.core.config import load_config
        policy=load_config().event
        risk = return_risk_observations(prior["Close"].tolist() if prior is not None else [], closes[-1],
                                       tail_probability=policy.risk_tail_probability,
                                       minimum_samples=policy.risk_min_reference_samples,
                                       lookback=policy.risk_reference_sessions)
        # Do not call a discontinuous overnight or lunch-break interval "10 minutes".
        if len(intraday) >= 3 and (intraday.index[-1]-intraday.index[-3]).total_seconds() > 600:
            closes = closes[-1:]
        return {
            "closes": closes,
            "last_bar_utc": last_bar_utc,
            "price": closes[-1],
            "atr_pct": atr_pct,
            "session_date": str(session_date), "risk_observations": risk,
            "reference_basis": "completed prior sessions; yfinance adjusted quote scale; contract-roll effects unverified",
        }
    except Exception as e:
        log.warning(f"[{symbol}] 行情抓取失败: {e}")
        return None


# ---------- 用户自设报警线（C8） ----------

def _alert_targets() -> List[Dict[str, Any]]:
    """strategy.target_assets 里设了 alerts 的标的 → [{symbol, alerts, holding}]。PM 不可用 → []。"""
    try:
        from openinvest.core.portfolio_manager import PortfolioManager

        pm = PortfolioManager()
        return [
            {"symbol": a["symbol"], "alerts": a["alerts"], "holding": pm.holdings.find(a["symbol"])}
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
    event = {**event, "event_id": hashlib.sha256(key.encode()).hexdigest()[:24]}
    from openinvest.services.source_reporting import record_backend_observation
    witness=record_backend_observation(ms.data_root,event,producer="openinvest.jobs.price_sentinel",inputs={"notification_key":key,"observation":event})
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
    detected = []
    for target in _alert_targets():
        sym = target["symbol"]
        try:
            quote = fetch_quote(sym, target["holding"])
            if quote is None or quote.is_stale or not quote.price > 0:
                continue
            hits = [r for r in evaluate_symbol(sym, target["alerts"], target["holding"], price=quote.price,
                    extra=quote.extra, last_updated=quote.last_updated) if r["breached"]]
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
                event = {"one_line_claim": f"{sym} 触发用户报警线：{hit['text']}；现价 {quote.price:.2f} {quote.currency or ''}",
                         "event_type":"price_alert", "stance":"opportunity" if rule in _ALERT_UP_RULES else "risk",
                         "severity":"low", "affected_symbols":[sym], "entities":["user_price_alert",sym.lower()], "ts":now.isoformat()}
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
    except Exception as exc:
        user_lines = []
        coverage["user_alert_lines"] = {"status":"evaluation_failed", "error":type(exc).__name__}
    owners=_reference_owners(symbols,ms)
    for sym in owners:
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
        age = (now-frames["last_bar_utc"]).total_seconds()/60
        if age < 0 or age > _MAX_STALE_MIN:
            coverage[sym] = {"status":"future_timestamp" if age<0 else "stale_session_unverified", "age_minutes":age}
            continue
        coverage[sym] = {"status":"observed", "last_bar":frames["last_bar_utc"].isoformat(),
                         "return_reference":frames.get("risk_observations",{}).get("status","not_available")}
        if not dry_run and sym in symbols:
            try:
                observed=observe_quote(ms,sym,frames["price"],now=now)
                if observed and observed.get("notification_id"):queued.append(observed["notification_id"])
            except Exception as exc:
                coverage[sym]["plan_error"]=type(exc).__name__
        hits = []
        vertical = _detect_move(frames["closes"],frames["atr_pct"],cfg.event.sentinel_atr_mult)
        if vertical:
            hits.append({**vertical,"basis":"10_minute","severity":"high" if vertical.get("ratio") is not None and vertical["ratio"]>=1.5 else "mid"})
        tails = [x for x in frames.get("risk_observations",{}).get("observations",[]) if x.get("breached")]
        if tails:
            hits.append({"direction":"down","basis":"prior_session_returns", "severity":"high" if any(x["historical_tail_rank"]<=.01 for x in tails) else "mid", "observations":tails})
        for hit in hits:
            direction=hit["direction"]; family=f"{direction}:{hit['basis']}:{hit['severity']}"
            from openinvest.services.notification_outbox import NotificationOutbox
            domain=f"market-observation:{sym}:{family}"
            last=None if dry_run else NotificationOutbox(ms).last_accepted(domain)
            if last is not None and now.timestamp()-last < cfg.event.sentinel_cooldown_min*60:
                continue
            state=ms.state_get(_STATE_NAME,{}) or {}
            if not _cooldown_ok(state,sym,family,now,cfg.event.sentinel_cooldown_min):
                continue
            detected.append(sym)
            if dry_run:
                continue
            session=frames.get("session_date") or frames["last_bar_utc"].date().isoformat()
            period=session if hit["basis"]=="prior_session_returns" else str(int(now.timestamp()//max(1,cfg.event.sentinel_cooldown_min*60)))
            key=f"market-observation:{sym}:{family}:{period}"
            if hit["basis"]=="10_minute":
                detail=f"10分钟变化 {hit['move_pct']:+.2f}%"
            else:
                detail="；".join(f"{o['horizon_sessions']}交易日累计 {o['return_pct']:+.2f}%（此前历史下侧参考 {o['threshold_pct']:.2f}%，n={o['sample_count']}）" for o in tails)
            event={"one_line_claim":f"{sym} {detail}；现价 {frames['price']:.4f} {quote_unit(sym) or 'unit_unverified'}。这是已观察变化，不是后续下跌预测或交易指令。",
                   "event_type":"price_action", "stance":"risk" if direction=="down" else "opportunity", "severity":hit["severity"],
                   "affected_symbols":[sym],"entities":["price_observation",sym.lower()],"ts":now.isoformat()}
            affected=sorted(owners[sym])
            if any(target!=sym for target in affected):
                event["one_line_claim"]+=f" 关联资产：{'、'.join(affected)}；这是跟踪参考的变化，不是这些资产的报价，也不保证同幅变动。"
            event["affected_symbols"]=list(dict.fromkeys([sym,*affected]))
            try:
                receipt=_queue_event(ms,event,key=key,followup_symbols=[target for target in affected if target in ctx["watching"]],risk_alert=direction=="down",
                                     now=now,
                                     cooldown={"domain":domain,"seconds":cfg.event.sentinel_cooldown_min*60})
                queued.append(receipt["notification_id"])
                if receipt["accepted"]:
                    accepted.append(sym);stamp=_accepted_time(receipt,now).isoformat()
                    ms.state_update(_STATE_NAME,lambda current,k=f"{sym}:{family}",v=stamp: ({**(current or {}),k:v},None))
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
