"""config 路由 — GET/PUT/DELETE /api/config（ADR-017 config-via-API）。

把白名单 tunable（API_SETTABLE）暴露给 GUI + agent 运行时读写：
- 落盘 memory/.state/config_overrides.json（原子写 + fcntl），load_config 在 env 之上合入；
- env 退为 bootstrap 默认；locked.py / 内部 sweep 阈值永不在此暴露（守 ADR-010）。
GUI（invest-gui Settings 页「委员会配置」区）与 skill `config get/set` 都走这个端点，三层对等。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Body, HTTPException

from openinvest.connectors.web_api.models import ConfigItem, ConfigResponse, ConfigUpdateRequest

log = logging.getLogger("web_api")
router = APIRouter()


def _view() -> ConfigResponse:
    from openinvest.core.config import effective_api_config
    return ConfigResponse(items=[ConfigItem(**it) for it in effective_api_config()])


@router.get("/api/config", response_model=ConfigResponse, tags=["config"])
async def get_config() -> ConfigResponse:
    """读白名单全部配置项的当前生效值（含是否被持久 override + 元信息）。"""
    return _view()


@router.put("/api/config", response_model=ConfigResponse, tags=["config"])
async def put_config(body: ConfigUpdateRequest = Body(...)) -> ConfigResponse:
    """设一条白名单 override（落盘持久化，跨 web/cron/skill 共读）。非白名单/非法值 → 400。"""
    from openinvest.core.config import set_persisted_override
    try:
        set_persisted_override(body.key, body.value)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    log.info("config override set: %s=%r", body.key, body.value)
    return _view()


@router.delete("/api/config/{key}", response_model=ConfigResponse, tags=["config"])
async def delete_config(key: str) -> ConfigResponse:
    """删一条持久 override，回退到 env/yaml/默认。非白名单 key → 404。"""
    from openinvest.core.config import clear_persisted_override
    try:
        clear_persisted_override(key)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    log.info("config override cleared: %s", key)
    return _view()
