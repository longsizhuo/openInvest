"""strategy_write 路由 — 从 web_api.py 按 tag 拆分（行为不变）。"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Body, HTTPException

from core.memory_store import MemoryStore
from core.schemas import validate_strategy

from connectors.web_api.models import (
    AllocationsRequest,
    StrategyWriteResponse,
    TargetAssetCreate,
    TargetAssetPatch,
)

log = logging.getLogger("web_api")
router = APIRouter()


# ============================================================
# 策略写操作 — 颗粒度从大到小
# ============================================================
# 设计：
# - 所有写都走 store.transaction("strategy") 单锁 RMW（commit-on-success 语义保证 schema fail 不写半截）
# - 写入 metadata 后，提交前显式跑 StrategyData.model_validate；失败抛 HTTPException(400)，
#   transaction 异常退出自动 rollback，已改的 frontmatter 不会落盘
# - body（人类写的策略说明）不动——schema 只管结构化字段，不动 markdown 文本


def _validate_strategy_or_400(metadata: Dict[str, Any]) -> None:
    """提交前 schema 校验，失败转 HTTP 400 并暴露 validation 详情给前端"""
    try:
        validate_strategy(metadata)
    except Exception as e:  # ValidationError 或派生
        raise HTTPException(status_code=400, detail=f"strategy schema validation failed: {e}")


def _strategy_response(metadata: Dict[str, Any], message: str) -> StrategyWriteResponse:
    return StrategyWriteResponse(
        target_allocation_stock=float(metadata.get("target_allocation_stock", 0)),
        target_allocation_cash=float(metadata.get("target_allocation_cash", 0)),
        target_assets=list(metadata.get("target_assets", [])),
        message=message,
    )


# ===== PUT /api/strategy/allocations =====

@router.put("/api/strategy/allocations", response_model=StrategyWriteResponse, tags=["strategy_write"])
async def put_allocations(body: AllocationsRequest = Body(...)) -> StrategyWriteResponse:
    """改资产配置目标（stock/cash 比例）。两者之和必须 ≈ 1（schema 强约束）"""
    store = MemoryStore()
    with store.transaction("strategy") as tx:
        tx["target_allocation_stock"] = body.target_allocation_stock
        tx["target_allocation_cash"] = body.target_allocation_cash
        _validate_strategy_or_400(dict(tx.metadata))
        final_meta = dict(tx.metadata)
    return _strategy_response(
        final_meta,
        f"目标配置已更新: 股 {body.target_allocation_stock:.0%} / 现 {body.target_allocation_cash:.0%}",
    )


# ===== POST /api/strategy/asset =====

@router.post("/api/strategy/asset", response_model=StrategyWriteResponse, tags=["strategy_write"])
async def add_target_asset(body: TargetAssetCreate = Body(...)) -> StrategyWriteResponse:
    """新增 target_asset。symbol 不能与现有重复"""
    store = MemoryStore()
    new_asset: Dict[str, Any] = body.model_dump(exclude_none=True, exclude={"extra"})
    if body.extra:
        new_asset.update(body.extra)

    with store.transaction("strategy") as tx:
        existing: List[Dict[str, Any]] = list(tx.get("target_assets", []) or [])
        if any(a.get("symbol") == body.symbol for a in existing):
            raise HTTPException(
                status_code=409,
                detail=f"symbol {body.symbol} 已存在，请用 PUT 更新或先 DELETE",
            )
        existing.append(new_asset)
        tx["target_assets"] = existing
        _validate_strategy_or_400(dict(tx.metadata))
        final_meta = dict(tx.metadata)
    return _strategy_response(final_meta, f"已新增资产 {body.symbol}")


# ===== PUT /api/strategy/asset/{symbol} =====

@router.put(
    "/api/strategy/asset/{symbol}",
    response_model=StrategyWriteResponse,
    tags=["strategy_write"],
)
async def update_target_asset(
    symbol: str,
    body: TargetAssetPatch = Body(...),
) -> StrategyWriteResponse:
    """更新单个 target_asset 的部分字段（仅传非空字段会被改写）"""
    patch = body.model_dump(exclude_none=True)
    if not patch:
        raise HTTPException(status_code=400, detail="patch 必须至少含一个字段")

    store = MemoryStore()
    with store.transaction("strategy") as tx:
        existing: List[Dict[str, Any]] = list(tx.get("target_assets", []) or [])
        target = next((a for a in existing if a.get("symbol") == symbol), None)
        if target is None:
            raise HTTPException(status_code=404, detail=f"symbol {symbol} 不存在")
        target.update(patch)
        tx["target_assets"] = existing
        _validate_strategy_or_400(dict(tx.metadata))
        final_meta = dict(tx.metadata)
    return _strategy_response(final_meta, f"{symbol} 已更新: {list(patch.keys())}")


# ===== DELETE /api/strategy/asset/{symbol} =====

@router.delete(
    "/api/strategy/asset/{symbol}",
    response_model=StrategyWriteResponse,
    tags=["strategy_write"],
)
async def delete_target_asset(symbol: str) -> StrategyWriteResponse:
    """删除 target_asset。schema 要求至少剩 1 个，否则 400"""
    store = MemoryStore()
    with store.transaction("strategy") as tx:
        existing: List[Dict[str, Any]] = list(tx.get("target_assets", []) or [])
        if not any(a.get("symbol") == symbol for a in existing):
            raise HTTPException(status_code=404, detail=f"symbol {symbol} 不存在")
        new_list = [a for a in existing if a.get("symbol") != symbol]
        tx["target_assets"] = new_list
        _validate_strategy_or_400(dict(tx.metadata))   # schema 保证至少 1 个
        final_meta = dict(tx.metadata)
    return _strategy_response(final_meta, f"已删除 {symbol}")
