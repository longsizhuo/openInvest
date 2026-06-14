"""meta 路由 — 从 web_api.py 按 tag 拆分（行为不变）。"""
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


# ============ 端点：健康检查 ============

@router.get("/api/health", response_model=HealthResponse, tags=["meta"])
async def health() -> HealthResponse:
    """健康检查 — systemd / Caddy 探活用"""
    return HealthResponse(
        timestamp=datetime.now().astimezone().isoformat(timespec="seconds"),
    )
