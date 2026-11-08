"""state 路由 — 从 system.py 按域拆分（行为不变）。

子系统状态端点：Dreaming 状态、PnL 历史数据点、跑赢基准事件列表。
所有 @router.get path 逐字搬运，行为零漂移。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import List

from fastapi import APIRouter, HTTPException, Query

from openinvest.core.memory_store import MemoryStore
from openinvest.services.financial_publication import (
    derived_publication_block,
    historical_performance_publication_block,
    qualified_history,
    qualified_outperform_events,
)

from openinvest.paths import INVEST_ROOT
from openinvest.connectors.web_api.models import (
    DreamEvent,
    DreamsStateResponse,
    OutperformEvent,
    OutperformEventsResponse,
    PnLHistoryPoint,
    PnLHistoryResponse,
)

log = logging.getLogger("web_api")

router = APIRouter()


@router.get("/api/dreams/state", response_model=DreamsStateResponse, tags=["system"])
def get_dreams_state(
    event_limit: int = Query(20, ge=1, le=200),
) -> DreamsStateResponse:
    """Dreaming 子系统当前状态：短期记忆 + 候选池 + 最近 events"""
    store = MemoryStore()
    short_term = store.read_dream_state("short-term-recall")
    candidates = store.read_dream_state("candidates")

    events: List[DreamEvent] = []
    events_path = store.root / ".dreams" / "events.jsonl"
    if events_path.exists():
        try:
            with open(events_path, encoding="utf-8") as f:
                lines = f.readlines()
            # 倒序取最近 N 条
            for line in reversed(lines[-event_limit:]):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    events.append(DreamEvent(**obj))
                except Exception:  # noqa: BLE001
                    continue
        except Exception as e:  # noqa: BLE001
            log.warning(f"读 dreams events 失败: {e}")

    return DreamsStateResponse(
        short_term=short_term,
        candidates=candidates,
        recent_events=events,
    )


@router.get("/api/pnl_history", response_model=PnLHistoryResponse, tags=["system"])
def get_pnl_history(
    since: int = Query(60, ge=1, le=2000, description="返回最近 N 条快照"),
) -> PnLHistoryResponse:
    """原始 PnL 历史数据点（jobs/pnl_snapshot 工作日每 2h 写一条）"""
    store = MemoryStore()
    if blocked := historical_performance_publication_block(store=store):
        raise HTTPException(status_code=409, detail=blocked)
    path = store.root / ".state" / "pnl_history.jsonl"
    try:
        qualified = qualified_history(store, path)
    except (OSError, ValueError, KeyError, TypeError):
        raise HTTPException(status_code=409, detail=derived_publication_block("unverified_point_receipt"))
    if len(qualified) < 2:
        raise HTTPException(status_code=409, detail=derived_publication_block("insufficient_qualified_history", points=len(qualified)))
    points = [PnLHistoryPoint(**row) for row in qualified[-since:]]
    return PnLHistoryResponse(count=len(points), points=points)


@router.get("/api/outperform_events", response_model=OutperformEventsResponse, tags=["system"])
def get_outperform_events(
    since: int = Query(20, ge=1, le=500),
) -> OutperformEventsResponse:
    """openInvest 跑赢基准的"可分享瞬间"列表

    PM-3 增长杠杆：每次 pnl_snapshot 检测到 user_pct > bench_pct 都会落一条到
    docs/outperform_events.jsonl。GUI 可以把最近一条做成 toast / 截图分享卡。
    """
    store = MemoryStore()
    if blocked := historical_performance_publication_block(store=store):
        raise HTTPException(status_code=409, detail=blocked)
    docs_path = INVEST_ROOT / "docs" / "outperform_events.jsonl"
    history_path = store.root / ".state/pnl_history.jsonl"
    try:
        qualified = qualified_history(store, history_path)
        if len(qualified) < 2:
            raise HTTPException(status_code=409, detail=derived_publication_block("insufficient_qualified_history", points=len(qualified)))
        approved_events = qualified_outperform_events(store, docs_path, history_path)
    except (OSError, ValueError, KeyError, TypeError):
        raise HTTPException(status_code=409, detail=derived_publication_block("unverified_outperform_manifest"))
    if docs_path.exists() and docs_path.read_text().strip() and not approved_events:
        raise HTTPException(status_code=409, detail=derived_publication_block("unverified_outperform_manifest"))
    events = [OutperformEvent(**row) for row in reversed(approved_events[-since:])]
    return OutperformEventsResponse(count=len(events), events=events)
