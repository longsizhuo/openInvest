"""meta 路由 — 从 web_api.py 按 tag 拆分（行为不变）。"""
from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter

from openinvest.connectors.web_api.models import HealthResponse

log = logging.getLogger("web_api")
router = APIRouter()


# ============ 端点：健康检查 ============

@router.get("/api/health", response_model=HealthResponse, tags=["meta"])
async def health() -> HealthResponse:
    """健康检查 — systemd / Caddy 探活用"""
    return HealthResponse(
        timestamp=datetime.now().astimezone().isoformat(timespec="seconds"),
    )
