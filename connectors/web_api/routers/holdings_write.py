"""holdings_write 路由 — 从 web_api.py 按 tag 拆分（行为不变）。"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

from dotenv import load_dotenv
from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from core.memory_store import MemoryStore
from core.portfolio_manager import PortfolioManager, _guess_kind_from_symbol
from core.schemas import StrategyData, validate_strategy
from utils.exchange_fee import get_history_data
from utils.gold_price import get_gold_snapshot, infer_offset_pct
from utils.quotes import get_quote

from connectors.web_api.models import *  # noqa: F401,F403
from connectors.web_api.deps import get_pm

log = logging.getLogger("web_api")
from connectors.web_api.routers.read import _build_holding_v2

router = APIRouter()


@router.post("/api/holdings", response_model=HoldingV2, tags=["holdings_write"])
async def add_holding(body: HoldingCreateRequest = Body(...), pm: PortfolioManager = Depends(get_pm)) -> HoldingV2:
    """新增持仓（任意 yfinance symbol）。symbol 已存在 → 409"""
    new_holding = body.model_dump(exclude_none=True)
    new_holding["cost_currency"] = str(new_holding["cost_currency"]).upper()

    with pm.with_portfolio_tx() as p:
        holdings = list(p.get("holdings") or [])
        if any(h.get("symbol") == body.symbol for h in holdings):
            raise HTTPException(status_code=409, detail=f"symbol {body.symbol} 已存在；用 PUT 更新或先 DELETE")
        holdings.append(new_holding)
        p["holdings"] = holdings
    pm._reload()

    h = pm.holdings.find(body.symbol)
    if h is None:  # 防御
        raise HTTPException(status_code=500, detail="新增后读不到，数据可能撕裂")
    return _build_holding_v2(h)


@router.put("/api/holdings/{symbol}", response_model=HoldingV2, tags=["holdings_write"])
async def update_holding(symbol: str, body: HoldingPatchRequest = Body(...), pm: PortfolioManager = Depends(get_pm)) -> HoldingV2:
    """部分字段更新单个 holding（仅传非空字段会被改写）"""
    patch = body.model_dump(exclude_none=True)
    if not patch:
        raise HTTPException(status_code=400, detail="patch 必须含至少一个字段")
    if "cost_currency" in patch:
        patch["cost_currency"] = str(patch["cost_currency"]).upper()

    with pm.with_portfolio_tx() as p:
        holdings = list(p.get("holdings") or [])
        target = next((h for h in holdings if h.get("symbol") == symbol), None)
        if target is None:
            raise HTTPException(status_code=404, detail=f"symbol {symbol} 不存在")
        target.update(patch)
        p["holdings"] = holdings
    pm._reload()
    h = pm.holdings.find(symbol)
    if h is None:
        raise HTTPException(status_code=500, detail="更新后读不到")
    return _build_holding_v2(h)


@router.delete("/api/holdings/{symbol}", tags=["holdings_write"])
async def delete_holding(symbol: str, pm: PortfolioManager = Depends(get_pm)) -> Dict[str, Any]:
    """删除持仓。units > 0 时拒绝（避免数据丢失），用户必须先卖光或显式 set units=0"""
    with pm.with_portfolio_tx() as p:
        holdings = list(p.get("holdings") or [])
        target = next((h for h in holdings if h.get("symbol") == symbol), None)
        if target is None:
            raise HTTPException(status_code=404, detail=f"symbol {symbol} 不存在")
        if not target.get("is_tracking_only") and float(target.get("units", 0) or 0) > 0:
            raise HTTPException(
                status_code=400,
                detail=f"{symbol} 持仓 {target.get('units')} > 0，请先卖光或显式 set units=0",
            )
        p["holdings"] = [h for h in holdings if h.get("symbol") != symbol]
    pm._reload()
    return {"ok": True, "deleted": symbol}
