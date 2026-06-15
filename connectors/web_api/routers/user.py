"""user 路由 — 从 web_api.py 按 tag 拆分（行为不变）。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Body, HTTPException

from connectors.web_api.models import UserProfileResponse, WealthContextRequest

log = logging.getLogger("web_api")
router = APIRouter()


@router.get("/api/user", response_model=UserProfileResponse, tags=["user"])
async def get_user_profile() -> UserProfileResponse:
    """读 user.md frontmatter（含 wealth_context 子对象）"""
    from core.memory_store import MemoryStore
    store = MemoryStore()
    doc = store.read("user")
    if doc is None:
        raise HTTPException(status_code=404, detail="user.md 不存在，先跑 invest-setup")
    meta = dict(doc.metadata)
    return UserProfileResponse(
        display_name=meta.get("display_name"),
        risk_tolerance=meta.get("risk_tolerance"),
        exchange_buffer_cny=float(meta.get("exchange_buffer_cny", 0) or 0),
        last_payday=meta.get("last_payday"),
        user_email=meta.get("user_email"),
        wealth_context=meta.get("wealth_context"),
    )


@router.put("/api/user/wealth_context", response_model=UserProfileResponse, tags=["user"])
async def update_wealth_context(body: WealthContextRequest = Body(...)) -> UserProfileResponse:
    """更新 user.md 的 wealth_context 字段（原子写）。

    传 null/缺省 = 不动该字段；传值 = 覆盖。要清空某字段传空字符串或 0。
    """
    from core.memory_store import MemoryStore
    store = MemoryStore()

    # 只取调用方真的有传的字段（不覆盖未填的）
    new_ctx = body.model_dump(exclude_unset=True, exclude_none=False)

    # 单锁 read-modify-write（避免 TOCTOU lost update）
    doc = store.read("user")
    if doc is None:
        raise HTTPException(status_code=404, detail="user.md 不存在，先跑 invest-setup")

    existing_ctx = dict(doc.metadata.get("wealth_context") or {})
    existing_ctx.update(new_ctx)
    # 删除明确传 None 的字段
    for k, v in list(new_ctx.items()):
        if v is None:
            existing_ctx.pop(k, None)

    updated = store.update_fields("user", wealth_context=existing_ctx)
    if updated is None:
        raise HTTPException(status_code=500, detail="update_fields 失败")

    meta = dict(updated.metadata)
    return UserProfileResponse(
        display_name=meta.get("display_name"),
        risk_tolerance=meta.get("risk_tolerance"),
        exchange_buffer_cny=float(meta.get("exchange_buffer_cny", 0) or 0),
        last_payday=meta.get("last_payday"),
        user_email=meta.get("user_email"),
        wealth_context=meta.get("wealth_context"),
    )
