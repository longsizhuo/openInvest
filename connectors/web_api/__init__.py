"""Web API connector — FastAPI REST 层（package 形态）。

路由按 tag 拆到 routers/ 下；本文件只做：建 app + 中间件 + CORS + 鉴权 +
挂载各 router + SPA 静态兜底。响应模型在 models.py，get_pm 依赖在 deps.py。

启动：uvicorn connectors.web_api:app --host 127.0.0.1 --port 8765
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()

DEV_CORS = os.getenv("INVEST_WEB_DEV_CORS", "0") == "1"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("web_api")


# ============ FastAPI 应用 ============

app = FastAPI(
    title="invest Web API",
    description="多资产投资 agent 系统的 REST API（被 invest-gui 前端消费）",
    version="0.1.0",
)


if DEV_CORS:
    # 开发环境放行 Vite dev server；生产同源部署不走这条
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    log.info("DEV_CORS 已开启，放行 http://localhost:5173")


# ============ 可选鉴权（远端模式 hub-and-spoke） ============
# INVEST_API_TOKEN 不设 → 行为完全不变（既有 127.0.0.1 + Caddy + CF Access
# 边缘鉴权部署、demo 实例零影响）。设了 → 非 loopback 来源访问 /api/*
# （/api/health 豁免，留给探活）必须带 `Authorization: Bearer <token>`。
#
# loopback 豁免的原因：生产链路是 Caddy → 127.0.0.1:8765（已被 CF Access 保护），
# 本机 GUI / curl 不应被自己的 token 卡住；token 只防"绑 0.0.0.0 裸跑局域网 /
# 内网穿透"场景下的陌生访问。
#
# 红线：token 永不进日志、永不进任何响应体。

_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


@app.middleware("http")
async def _bearer_token_auth(request, call_next):
    # 每请求读 env：进程内改 env（测试 monkeypatch / systemd reload）即时生效
    token = os.getenv("INVEST_API_TOKEN", "").strip()
    if token:
        path = request.url.path
        client_host = request.client.host if request.client else ""
        if (
            path.startswith("/api/")
            and path != "/api/health"
            and client_host not in _LOOPBACK_HOSTS
        ):
            auth_header = request.headers.get("authorization", "")
            provided = auth_header[7:].strip() if auth_header.startswith("Bearer ") else ""
            import secrets as _secrets
            if not (provided and _secrets.compare_digest(provided, token)):
                from fastapi.responses import JSONResponse
                return JSONResponse(
                    status_code=401,
                    content={"detail": (
                        "unauthorized：本 hub 开启了 INVEST_API_TOKEN 鉴权，"
                        "请求需带 `Authorization: Bearer <token>`"
                    )},
                )
    return await call_next(request)


# ============ 挂载各 tag router ============
from connectors.web_api.routers import (  # noqa: E402
    meta, read, holdings_write, user, write, cash_write, strategy_write,
    committee, events, commsec, trades, skill,
    insights, observability, verdict_review, committee_sessions, regime, state,
    config,
)

for _m in (meta, read, holdings_write, user, write, cash_write, strategy_write,
           committee, events, commsec, trades, skill,
           insights, observability, verdict_review, committee_sessions, regime, state,
           config):
    app.include_router(_m.router)

# 顶层 re-export：保持 `from connectors.web_api import app / get_pm /
# _sync_trade_to_portfolio` 可用（测试 + CI smoke import 硬依赖）
from connectors.web_api.deps import get_pm  # noqa: E402,F401
from connectors.web_api.routers.trades import _sync_trade_to_portfolio  # noqa: E402,F401


# 一键部署模式：跑完 `python -m scripts.sync_gui_dist` 后，static/ 含 invest-gui 构建产物
# FastAPI 把它挂到 /，所有非 /api/* 请求自动 serve GUI（含 SPA 路由 fallback）
#
# 生产 Caddy 部署时这块不会被触发——Caddy 优先 file_server /srv/invest-gui，
# 只把 /api/* 反代到本服务，根本不会到这条 mount。共存无冲突。
#
# 必须放在所有路由声明之后；StaticFiles(html=True) 让 / 自动 serve index.html
# NOTE: 本文件在 connectors/web_api/ 包内，repo root 要往上三级（拆包前是
# connectors/web_api.py 只需两级；refactor 后漏改导致指向不存在的 connectors/static，
# GUI 一直没挂——生产走 Caddy 没暴露，docker compose 一键部署才会踩到）。
# 必须与 scripts/sync_gui_dist.py 的写入目标（repo_root/static）一致。
_STATIC_DIR = Path(__file__).resolve().parent.parent.parent / "static"
if _STATIC_DIR.exists() and (_STATIC_DIR / "index.html").exists():
    from fastapi.staticfiles import StaticFiles
    from starlette.exceptions import HTTPException as _StarletteHTTPException
    from starlette.responses import FileResponse

    class _SPAStaticFiles(StaticFiles):
        """SPA 路由 fallback：未找到的路径回退到 index.html，让 React Router 接管"""
        async def get_response(self, path: str, scope):  # type: ignore[override]
            try:
                return await super().get_response(path, scope)
            except _StarletteHTTPException as e:
                if e.status_code == 404:
                    return FileResponse(str(Path(self.directory or "") / "index.html"))
                raise

    app.mount("/", _SPAStaticFiles(directory=str(_STATIC_DIR), html=True), name="gui")
    log.info(f"✓ GUI 已挂载（SPA fallback 模式）: / → {_STATIC_DIR}")
else:
    log.info(
        "⚠️  GUI 未挂载（static/ 不存在或缺 index.html）。"
        "跑 `python -m scripts.sync_gui_dist` 拉 GUI 构建产物。"
    )
