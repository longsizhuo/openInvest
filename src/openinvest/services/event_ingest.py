"""Agent 投喂事件入口（#153 方案①）—— MCP `ingest_event` / CLI `ingest_event` 共用。

设计动机（events.db 10,553 条审计，2026-07-06）：自托管爬虫 92% 产出与持仓无关，
而持仓盲区（510300.SS 三十天 0 条、NDQ.AX 1 条）恰是爬虫最难覆盖的（中文源反爬、
区域源长尾）。宿主 agent 自带比任何爬虫都强的搜索——本模块让 agent 把看到的新闻
推进**既有管道**（normalize → severity/symbol 判级 → events.db → RAG 召回），
runtime 只守护城河：归一化、判级、去重、持仓关联。

幂等（ADR-016）：复用管道两级去重——url 已见跳过（sources 表）+ claim 哈希
upsert（event_id 唯一键）。agent 重发同一条新闻不会重复入账。

2026-10：新事件入库后走 services/event_trigger（与 event_watch 同一道闸 + 冷却/日上限）
——此前只入库不触发，Hermes 哨兵喂进来的持仓风险事件从没触发过委员会。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


def ingest_events(items: List[Dict[str, Any]], *, ingested_by: str = "host-agent") -> Dict[str, Any]:
    """把 agent 提供的原始新闻批量走归一化管道入库。

    Args:
        items: [{title(必填), url(必填), snippet?, source?, published_at?}, ...]
        ingested_by: 喂料 agent 身份（如 "hermes" / "claude-code"），落 events.ingested_by
            （溯源：坏 verdict 反查坏输入是谁喂的）。source 是**新闻来源**（落
            sources.src_name=agent:<source>），两者语义不同。

    Returns:
        {status, ingested, duplicates, events: [{event_id, one_line_claim,
         event_type, stance, severity, affected_symbols}], committee_task_id}
        committee_task_id: 新事件过闸且未被冷却/日上限拦下时触发的委员会 task（否则 None）
        缺 LLM key → {status: "error", error, hint}（归一化必须 LLM，无降级路径）
    """
    from openinvest.db.event_store import EventStore
    from openinvest.services.embeddings import DEFAULT_DIM
    from openinvest.services.event_normalizer import normalize
    from openinvest.services.news_sources import RawNewsItem

    raw: List[RawNewsItem] = []
    for it in items:
        title = (it.get("title") or "").strip()
        url = (it.get("url") or "").strip()
        if not title or not url:
            return {"status": "error",
                    "error": f"title/url 必填，收到 {it!r}"}
        src = (it.get("source") or "host-agent").strip()
        raw.append(RawNewsItem(
            src_name=f"agent:{src}",
            title=title,
            url=url,
            snippet=(it.get("snippet") or "").strip(),
            published_at=it.get("published_at"),
        ))

    store = EventStore(embedding_dim=DEFAULT_DIM)
    unseen = [it for it in raw if not store.is_seen_url(it.url)]
    duplicates = len(raw) - len(unseen)
    if not unseen:
        return {"status": "ok", "ingested": 0, "duplicates": duplicates, "events": [],
                "committee_task_id": None}

    normalized = normalize(unseen)
    if not normalized:
        # normalize 空返回 = 缺 LLM key 或全批失败——对 agent 必须显式，不能装成功
        from openinvest.utils.llm import get_llm_config_safe
        ak, *_ = get_llm_config_safe()
        if not ak:
            return {"status": "error",
                    "error": "缺 LLM key，事件归一化不可用",
                    "hint": "在 $INVEST_HOME/.env 配 DEEPSEEK_API_KEY（或 LLM_API_KEY），"
                            "归一化/判级由后端 LLM 完成"}
        return {"status": "error", "error": "归一化失败（LLM 调用异常），事件未入库"}

    out = []
    new_events: List[Dict[str, Any]] = []
    for ne in normalized:
        ne.event["ingested_by"] = ingested_by  # normalizer 输出全来自 LLM——溯源在入库前注入
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
            duplicates += 1
            continue
        ev = ne.event
        new_events.append({**ev, "event_id": eid})
        out.append({
            "event_id": eid,
            "one_line_claim": ev.get("one_line_claim"),
            "event_type": ev.get("event_type"),
            "stance": ev.get("stance"),
            "severity": ev.get("severity"),
            "affected_symbols": ev.get("affected_symbols") or [],
        })
    log.info(f"[event_ingest] agent 投喂 {len(raw)} 条 → 新事件 {len(out)}，去重 {duplicates}")
    task_id = None
    try:
        from openinvest.services.event_trigger import trigger_for_new_events
        task_id = trigger_for_new_events(new_events, store=store)["committee_task_id"]
    except Exception as e:  # 事件已入库，触发失败不能让 agent 以为没喂进去
        log.warning(f"[event_ingest] 触发闸失败: {type(e).__name__}: {e}")
    return {"status": "ok", "ingested": len(out), "duplicates": duplicates, "events": out,
            "committee_task_id": task_id}
