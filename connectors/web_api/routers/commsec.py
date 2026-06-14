"""commsec 路由 — 从 web_api.py 按 tag 拆分（行为不变）。"""
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
router = APIRouter()


def _commsec_fetch(lookback_days: int) -> tuple[List[Dict[str, Any]], Optional[str]]:
    """共享拉取逻辑：返回 (new_trades, err_msg)"""
    email_user = os.getenv("EMAIL_SENDER")
    email_pass = os.getenv("EMAIL_PASSWORD")
    if not (email_user and email_pass):
        return [], "缺少 EMAIL_SENDER / EMAIL_PASSWORD 环境变量"

    from services.commsec_reader import CommSecReader

    pm = get_pm()
    reader = CommSecReader(email_user, email_pass)
    if not reader.connect():
        return [], "IMAP 连接失败（凭证错误或 Gmail 限速）"

    try:
        processed = pm.get_processed_emails()
        trades = reader.fetch_trade_confirmations(
            lookback_days=lookback_days, processed_ids=processed,
        )
    finally:
        reader.close()
    return trades, None


@router.get(
    "/api/commsec/preview",
    response_model=CommsecPreviewResponse,
    tags=["commsec"],
)
async def commsec_preview(
    lookback_days: int = Query(180, ge=1, le=365),
) -> CommsecPreviewResponse:
    """预览 CommSec 邮件拉到的新成交（不写入）。GUI [Import] 按钮先调它"""
    trades, err = _commsec_fetch(lookback_days)
    if err:
        return CommsecPreviewResponse(
            ok=False, lookback_days=lookback_days, error=err,
        )
    return CommsecPreviewResponse(
        ok=True, lookback_days=lookback_days, new_trades=trades,
    )


@router.post(
    "/api/commsec/apply",
    response_model=CommsecApplyResponse,
    tags=["commsec"],
)
async def commsec_apply(
    body: CommsecApplyRequest = Body(...),
    pm: PortfolioManager = Depends(get_pm),
) -> CommsecApplyResponse:
    """实际写入 CommSec 拉到的成交到 portfolio + history.jsonl

    GUI 弹确认窗 → 用户点 Confirm → 调本接口
    """
    trades, err = _commsec_fetch(body.lookback_days)
    if err:
        raise HTTPException(status_code=503, detail=err)

    written = 0
    errors: List[str] = []
    for t in trades:
        try:
            pm.record_external_trade(t)
            written += 1
        except Exception as e:  # noqa: BLE001
            errors.append(f"{t.get('symbol', '?')}: {type(e).__name__} {e}")

    return CommsecApplyResponse(
        ok=len(errors) == 0,
        written=written,
        skipped=len(trades) - written,
        errors=errors,
    )
