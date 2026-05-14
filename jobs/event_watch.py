"""jobs/event_watch —— 第一层：盘中事件感知 + 邮件通知 + 触发委员会

每 30m cron 跑一次：
1. 拉用户上下文（holdings ∪ target_assets ∪ wealth_context.macro_tags）
2. 构 query → fetch_all 多源新闻
3. event_normalizer.normalize → 结构化事件
4. dedup（同 url 跳过）+ event_store.upsert_event
5. 对 newly_inserted 中 severity ≥ mid 且影响到关注 symbol 的：
   → POST /api/committee/run（触发委员会重跑）
   → send_event_alert（digest 邮件）

环境变量：
  INVEST_EVENT_MIN_SEVERITY    默认 mid（trigger 阈值）
  INVEST_EVENT_API_URL         触发 committee 的 endpoint，默认 http://127.0.0.1:8765
  INVEST_EVENT_DRY_RUN         真值则不发邮件不触委员会，只入库

CLI:
    python -m jobs.event_watch                # 单次跑
    python -m jobs.event_watch --dry-run      # 只入库
    python -m jobs.event_watch --recall NDQ.AX  # 测 RAG 召回
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from db.event_store import EventStore
from services.embeddings import DEFAULT_DIM
from services.event_normalizer import NormalizedEvent, normalize
from services.event_notifier import send_event_alert
from services.news_sources import fetch_all
from services.news_sources.rss_feed import load_default_feeds

log = logging.getLogger(__name__)

_SEVERITY_RANK = {"low": 1, "mid": 2, "high": 3}


def _load_user_context() -> Dict[str, Any]:
    """从 PortfolioManager 抓 holdings / target_assets / wealth_context tags

    返回 dict 即可，不要把 PM 实例直接传出去（避免 caller 误改持仓）
    """
    try:
        from core.portfolio_manager import PortfolioManager
        pm = PortfolioManager()
    except Exception as e:
        log.warning(f"PortfolioManager 不可用，event_watch 用空上下文: {e}")
        return {"holdings": [], "watching": [], "macro_tags": [], "queries": []}

    holdings = [h.get("symbol") for h in pm.holdings.all() if h.get("symbol")]
    watching = [a.get("symbol") for a in (pm.strategy.get("target_assets") or [])
                if a.get("symbol")]
    wc = (pm.user.get("wealth_context") or {})
    macro_tags = wc.get("macro_tags") or []

    # 构造给 ddgs 用的 query 列表：
    # 每个 watched/held symbol 一条 + 用户 macro_tag 一条 + 默认 macro keyword
    queries: List[str] = []
    for sym in (set(holdings) | set(watching)):
        queries.append(f"{sym} news")
    queries.extend([f"{tag} markets" for tag in macro_tags])
    if not any("fed" in q.lower() for q in queries):
        queries.append("Fed rate decision")  # 默认抓宏观

    return {
        "holdings": holdings,
        "watching": watching,
        "macro_tags": [t.lower() for t in macro_tags],
        "queries": list(dict.fromkeys(queries)),  # 去重保序
    }


def _holdings_snapshot(symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    """给邮件正文用：每个受影响 symbol 当前 units / 现价 / pnl"""
    try:
        from core.portfolio_manager import PortfolioManager
        from utils.exchange_fee import get_history_data
        pm = PortfolioManager()
    except Exception:
        return {}

    snap: Dict[str, Dict[str, Any]] = {}
    for sym in symbols:
        h = pm.holdings.find(sym)
        if not h:
            continue
        units = float(h.get("units", 0) or 0)
        avg_cost = float(h.get("avg_cost", 0) or 0)
        price = None
        try:
            df = get_history_data(sym, "5d")
            if df is not None and not df.empty:
                price = float(df["Close"].iloc[-1])
        except Exception:
            price = None
        entry: Dict[str, Any] = {"units": units}
        if price is not None:
            entry["price"] = price
            entry["mv"] = price * units
            if avg_cost > 0:
                entry["pnl_pct"] = (price / avg_cost) - 1.0
        snap[sym] = entry
    return snap


def _trigger_committee(symbols: List[str], event_ids: List[str]) -> Optional[str]:
    """调本机 web_api /api/committee/run 触发委员会重跑。返回 task_id"""
    if not symbols:
        return None
    import requests
    base = os.getenv("INVEST_EVENT_API_URL", "http://127.0.0.1:8765")
    try:
        r = requests.post(
            f"{base.rstrip('/')}/api/committee/run",
            json={
                "symbols": symbols,
                "max_debate_rounds": int(os.getenv("INVEST_EVENT_MAX_ROUNDS", "2")),
                "note": f"triggered by event_watch event_ids={','.join(event_ids[:4])}",
                "event_ids": event_ids,
            },
            timeout=10,
        )
        r.raise_for_status()
        return r.json().get("task_id")
    except Exception as e:
        log.warning(f"trigger committee 失败: {type(e).__name__}: {e}")
        return None


def run(
    *,
    dry_run: bool = False,
    custom_rss_feeds: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    """job entry。返回汇总 dict。"""
    if os.getenv("INVEST_EVENT_DRY_RUN"):
        dry_run = True

    ctx = _load_user_context()
    watched = list(set(ctx["holdings"]) | set(ctx["watching"]))
    log.info(f"[event_watch] watched={watched}, queries={len(ctx['queries'])}")

    rss_feeds = custom_rss_feeds if custom_rss_feeds is not None else load_default_feeds()

    raw_items = fetch_all(
        queries=ctx["queries"],
        symbols=watched,
        rss_feeds=rss_feeds,
        max_per_source=int(os.getenv("INVEST_EVENT_MAX_PER_SOURCE", "15")),
        extract_fulltext=False,  # 实时层不抓正文，省时间
    )
    log.info(f"[event_watch] fetched {len(raw_items)} raw items")
    if not raw_items:
        return {"status": "ok", "fetched": 0, "new_events": 0, "triggered": 0}

    # 入库前先按 url 跳过 sources 已见的（省 LLM token）
    store = EventStore(embedding_dim=DEFAULT_DIM)
    unseen_items = [it for it in raw_items if not store.is_seen_url(it.url)]
    log.info(f"[event_watch] unseen={len(unseen_items)} / total={len(raw_items)}")
    if not unseen_items:
        return {"status": "ok", "fetched": len(raw_items), "new_events": 0, "triggered": 0}

    normalized: List[NormalizedEvent] = normalize(unseen_items)
    log.info(f"[event_watch] normalized {len(normalized)} events")

    # 入库 + 收集新事件
    min_sev = os.getenv("INVEST_EVENT_MIN_SEVERITY", "mid")
    min_sev_rank = _SEVERITY_RANK.get(min_sev, 2)
    watched_set = {s.lower() for s in watched}
    macro_tag_set = set(ctx["macro_tags"])

    triggerable_events: List[Dict[str, Any]] = []
    for ne in normalized:
        was_new, eid = store.upsert_event(ne.event, embedding=ne.embedding)
        if ne.raw_item:
            store.add_source(
                eid,
                src_name=ne.raw_item.src_name,
                url=ne.raw_item.url,
                title=ne.raw_item.title,
                snippet=ne.raw_item.snippet,
                fetched_at=ne.raw_item.fetched_at,
            )
        if not was_new:
            continue

        ev = ne.event
        sev_rank = _SEVERITY_RANK.get(ev["severity"], 1)
        if sev_rank < min_sev_rank:
            continue
        if ev["stance"] == "neutral":
            continue

        affected_lower = {s.lower() for s in (ev["affected_symbols"] or [])}
        entity_lower = {e.lower() for e in (ev["entities"] or [])}
        if not (affected_lower & watched_set) and not (entity_lower & macro_tag_set):
            continue

        # 这条事件值得触发
        ev_for_email = dict(ev)
        ev_for_email["event_id"] = eid
        ev_for_email["sources"] = store.get_sources(eid)
        triggerable_events.append(ev_for_email)

    log.info(f"[event_watch] triggerable={len(triggerable_events)}")

    if not triggerable_events:
        return {
            "status": "ok",
            "fetched": len(raw_items),
            "new_events": sum(1 for _ in normalized),
            "triggered": 0,
        }

    # 受影响 symbols 合并去重，只对真持仓 / 关注列表跑委员会
    affected_syms = sorted({
        s for ev in triggerable_events for s in (ev["affected_symbols"] or [])
        if s in (ctx["holdings"] + ctx["watching"])
    })

    task_id = None
    if not dry_run:
        task_id = _trigger_committee(
            symbols=affected_syms,
            event_ids=[ev["event_id"] for ev in triggerable_events],
        )
        if task_id:
            for ev in triggerable_events:
                store.mark_committee_task(ev["event_id"], task_id)

        try:
            send_event_alert(
                triggerable_events,
                committee_task_id=task_id,
                holdings_snapshot=_holdings_snapshot(affected_syms),
            )
        except Exception as e:
            log.warning(f"send_event_alert 失败: {type(e).__name__}: {e}")

    return {
        "status": "ok",
        "fetched": len(raw_items),
        "new_events": len(normalized),
        "triggered": len(triggerable_events),
        "committee_task_id": task_id,
        "affected_symbols": affected_syms,
        "dry_run": dry_run,
    }


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只入库，不发邮件 / 不触委员会")
    parser.add_argument("--recall", metavar="SYMBOL", help="测试 event_store.recall 召回（不抓新源）")
    args = parser.parse_args()

    if args.recall:
        store = EventStore(embedding_dim=DEFAULT_DIM)
        events = store.recall(args.recall)
        import json
        print(json.dumps(events, ensure_ascii=False, indent=2, default=str))
        return 0

    out = run(dry_run=args.dry_run)
    import json
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
