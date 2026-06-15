"""events 路由 — 从 web_api.py 按 tag 拆分（行为不变）。"""
from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, HTTPException, Query

from connectors.web_api.models import EventCheckResponse, EventItem, EventsRecentResponse

log = logging.getLogger("web_api")
router = APIRouter()


@router.get("/api/events/recent", response_model=EventsRecentResponse, tags=["events"])
async def events_recent(
    hours: int = Query(24, ge=1, le=168, description="时间窗（小时），默认 24h"),
    min_severity: Literal["low", "mid", "high"] = Query("low"),
    limit: int = Query(50, ge=1, le=200),
) -> EventsRecentResponse:
    """列最近 N 小时入库的事件 + 各严重度计数。给 Events Tab 用。

    跟 committee 决策路径的 recall() 不一样：不按 symbol 过滤，纯时序扫描，
    让用户看到"系统现在感知到啥"。
    """
    from db.event_store import EventStore
    from services.embeddings import DEFAULT_DIM
    store = EventStore(embedding_dim=DEFAULT_DIM)
    raw_items = store.list_recent(hours=hours, min_severity=min_severity, limit=limit)
    counts = store.count_recent(hours=hours)
    items = [
        EventItem(
            event_id=r["event_id"],
            one_line_claim=r["one_line_claim"],
            event_type=r["event_type"],
            stance=r["stance"],
            severity=r["severity"],  # _row_to_event 已经转 str
            affected_symbols=r.get("affected_symbols") or [],
            entities=r.get("entities") or [],
            ts=r["ts"],
            committee_task_id=r.get("committee_task_id"),
        )
        for r in raw_items
    ]
    return EventsRecentResponse(hours=hours, counts=counts, items=items)


@router.post("/api/events/check", response_model=EventCheckResponse, tags=["events"])
async def events_check() -> EventCheckResponse:
    """同步触发一次 event_watch（拉新闻 + 归一化 + 入库 + 命中触发委员会）。

    给 Events Tab "立即扫描" 按钮用。**同步等待完成**（30-90s 不等），
    前端用 loading state 兜住。

    后端已有的 cron path 每 30 分钟也跑一次；这个端点是 on-demand 手动跑。
    """
    import time as _time
    from jobs.event_watch import run as event_watch_run
    t0 = _time.perf_counter()
    try:
        result = event_watch_run()
    except Exception as e:
        log.exception(f"events_check failed: {e}")
        raise HTTPException(status_code=500, detail=f"event_watch 跑失败: {e}") from e
    duration_ms = int((_time.perf_counter() - t0) * 1000)
    return EventCheckResponse(
        status=result.get("status", "ok"),
        fetched=int(result.get("fetched", 0) or 0),
        new_events=int(result.get("new_events", 0) or 0),
        triggered=int(result.get("triggered", 0) or 0),
        duration_ms=duration_ms,
    )
