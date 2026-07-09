"""user 路由 — 从 web_api.py 按 tag 拆分（行为不变）。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from openinvest.connectors.web_api.models import UserProfileResponse

log = logging.getLogger("web_api")
router = APIRouter()


@router.get("/api/user", response_model=UserProfileResponse, tags=["user"])
async def get_user_profile() -> UserProfileResponse:
    """读 user.md frontmatter"""
    from openinvest.core.memory_store import MemoryStore
    store = MemoryStore()
    doc = store.read("user")
    if doc is None:
        raise HTTPException(status_code=404, detail="user.md 不存在，先跑 invest-setup")
    meta = dict(doc.metadata)
    return UserProfileResponse(
        display_name=meta.get("display_name"),
        risk_tolerance=meta.get("risk_tolerance"),
        user_email=meta.get("user_email"),
    )
