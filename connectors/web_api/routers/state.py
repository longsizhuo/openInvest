"""state 路由 — 从 system.py 按域拆分（行为不变）。

子系统状态端点：Dreaming 状态、PnL 历史数据点、跑赢基准事件列表。
所有 @router.get path 逐字搬运，行为零漂移。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import List

from fastapi import APIRouter, Query

from core.memory_store import MemoryStore

from connectors.web_api.models import (
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
async def get_dreams_state(
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
async def get_pnl_history(
    since: int = Query(60, ge=1, le=2000, description="返回最近 N 条快照"),
) -> PnLHistoryResponse:
    """原始 PnL 历史数据点（jobs/pnl_snapshot 工作日每 2h 写一条）"""
    store = MemoryStore()
    path = store.root / ".state" / "pnl_history.jsonl"
    points: List[PnLHistoryPoint] = []
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.readlines()
            for line in lines[-since:]:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    points.append(PnLHistoryPoint(**obj))
                except Exception:
                    continue
        except Exception as e:  # noqa: BLE001
            log.warning(f"读 pnl_history 失败: {e}")
    return PnLHistoryResponse(count=len(points), points=points)


@router.get("/api/outperform_events", response_model=OutperformEventsResponse, tags=["system"])
async def get_outperform_events(
    since: int = Query(20, ge=1, le=500),
) -> OutperformEventsResponse:
    """openInvest 跑赢基准的"可分享瞬间"列表

    PM-3 增长杠杆：每次 pnl_snapshot 检测到 user_pct > bench_pct 都会落一条到
    docs/outperform_events.jsonl。GUI 可以把最近一条做成 toast / 截图分享卡。
    """
    docs_path = Path(__file__).parent.parent / "docs" / "outperform_events.jsonl"
    events: List[OutperformEvent] = []
    if docs_path.exists():
        try:
            with open(docs_path, encoding="utf-8") as f:
                lines = f.readlines()
            for line in reversed(lines[-since:]):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    events.append(OutperformEvent(**obj))
                except Exception:
                    continue
        except Exception as e:  # noqa: BLE001
            log.warning(f"读 outperform_events 失败: {e}")
    return OutperformEventsResponse(count=len(events), events=events)
