"""strategy_write 路由 — 薄壳转发 services.strategy_write（issue #179）。

写逻辑原 inline 在本文件（REST 独占，CLI/MCP 只读）。抽到
services/strategy_write.py 后三入口共用同一实现；本文件只做
HTTP 语义映射：StrategyConflict→409 / StrategyNotFound→404 / ValueError→400。
REST 契约（路径 / body / 响应结构 / 状态码）不变；409/404 的 detail 文案
随三入口统一微调（"请用 update 或先 remove"），无测试/消费方依赖旧文案。
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Body, HTTPException

from openinvest.services import strategy_write as svc

from openinvest.connectors.web_api.models import (
    AllocationsRequest,
    StrategyWriteResponse,
    TargetAssetCreate,
    TargetAssetPatch,
)

log = logging.getLogger("web_api")
router = APIRouter()


def _to_http(fn, *args, **kwargs) -> StrategyWriteResponse:
    """service 异常 → HTTP 状态码（业务语义映射的唯一位置）"""
    try:
        out: Dict[str, Any] = fn(*args, **kwargs)
    except svc.StrategyConflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    except svc.StrategyNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return StrategyWriteResponse(
        target_allocation_stock=out["target_allocation_stock"],
        target_allocation_cash=out["target_allocation_cash"],
        target_assets=out["target_assets"],
        message=out["message"],
    )


@router.put("/api/strategy/allocations", response_model=StrategyWriteResponse, tags=["strategy_write"])
async def put_allocations(body: AllocationsRequest = Body(...)) -> StrategyWriteResponse:
    """改资产配置目标（stock/cash 比例）。两者之和必须 ≈ 1（schema 强约束）"""
    return _to_http(
        svc.set_allocations, body.target_allocation_stock, body.target_allocation_cash
    )


@router.post("/api/strategy/asset", response_model=StrategyWriteResponse, tags=["strategy_write"])
async def add_target_asset(body: TargetAssetCreate = Body(...)) -> StrategyWriteResponse:
    """新增 target_asset。symbol 不能与现有重复"""
    new_asset: Dict[str, Any] = body.model_dump(exclude_none=True, exclude={"extra"})
    if body.extra:
        new_asset.update(body.extra)
    return _to_http(svc.add_target_asset, new_asset)


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
    return _to_http(svc.patch_target_asset, symbol, patch)


@router.delete(
    "/api/strategy/asset/{symbol}",
    response_model=StrategyWriteResponse,
    tags=["strategy_write"],
)
async def delete_target_asset(symbol: str) -> StrategyWriteResponse:
    """删除 target_asset。schema 要求至少剩 1 个，否则 400"""
    return _to_http(svc.remove_target_asset, symbol)
