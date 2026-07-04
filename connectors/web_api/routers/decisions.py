"""decisions 路由 — Decision Accounting 统一决策视图（issue #133 Decision 9）。

GET  /api/decisions            决议 ↔ 干预 ↔ 执行 ↔ 结果 读时 join + 采纳率汇总
POST /api/decisions/execution  宿主 Agent 回写执行/拒绝（executions.jsonl，幂等 ADR-016）
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query

from connectors.web_api.models import (
    DecisionExecution,
    DecisionsResponse,
    DecisionsSummary,
    RecordExecutionRequest,
)

log = logging.getLogger("web_api")

router = APIRouter()


@router.get("/api/decisions", response_model=DecisionsResponse, tags=["decisions"])
async def get_decisions(days: int = Query(90, ge=1, le=3650)) -> DecisionsResponse:
    """统一决策视图：每条委员会决议 join 规则干预 / 用户执行 / 事后结果。最新在前。"""
    from core.decision_ledger import list_decisions, summarize_decisions
    ds = list_decisions(days=days)
    return DecisionsResponse(
        count=len(ds),
        summary=DecisionsSummary(**summarize_decisions(ds)),
        decisions=ds,
    )


@router.post("/api/decisions/execution", response_model=DecisionExecution,
             tags=["decisions"])
async def post_execution(body: RecordExecutionRequest) -> DecisionExecution:
    """记录用户对某决议的执行/拒绝 + 原因（Reason Loop 的存储端，采集在宿主 Agent）。"""
    from core.decision_ledger import record_execution
    try:
        rec = record_execution(
            decision_id=body.decision_id,
            executed=body.executed,
            reason=body.reason,
            trade_ids=body.trade_ids,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return DecisionExecution(**{k: rec.get(k) for k in DecisionExecution.model_fields})
