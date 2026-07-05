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
# INVEST_API_TOKEN 不设 → 行为完全不变。设了 → **所有来源**（含 loopback）访问
# /api/*（/api/health 豁免，留给探活）都必须带 `Authorization: Bearer <token>`。
#
# 2026-07-05（#106）：loopback 豁免已删。原豁免假设"反代边缘另有 CF Access
# 兜底"，但典型 Caddy/Nginx 反代下 request.client.host 恒为 127.0.0.1——外网
# 请求被静默免密，token 形同虚设。现语义：设了 token 就当真；本机 curl 自己
# 带上 `-H "Authorization: Bearer $INVEST_API_TOKEN"`，内部触发（event_watch）
# 从同一 .env 读 token 自动附带。
#
# 红线：token 永不进日志、永不进任何响应体。


@app.middleware("http")
async def _bearer_token_auth(request, call_next):
    # 每请求读 env：进程内改 env（测试 monkeypatch / systemd reload）即时生效
    token = os.getenv("INVEST_API_TOKEN", "").strip()
    if token:
        path = request.url.path
        if path.startswith("/api/") and path != "/api/health":
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
from openinvest.connectors.web_api.routers import (  # noqa: E402
    meta, read, holdings_write, user, write, cash_write, strategy_write,
    committee, events, commsec, trades, skill,
    insights, observability, verdict_review, committee_sessions, regime, state,
    config, decisions,
)

for _m in (meta, read, holdings_write, user, write, cash_write, strategy_write,
           committee, events, commsec, trades, skill,
           insights, observability, verdict_review, committee_sessions, regime, state,
           config, decisions):
    app.include_router(_m.router)

# 顶层 re-export：保持 `from connectors.web_api import app / get_pm /
# _sync_trade_to_portfolio` 可用（测试 + CI smoke import 硬依赖）
from openinvest.connectors.web_api.deps import get_pm  # noqa: E402,F401
from openinvest.connectors.web_api.routers.trades import _sync_trade_to_portfolio  # noqa: E402,F401
from openinvest.paths import INVEST_ROOT


# GUI 壳层已退役（2026-07-05）：static/ SPA 挂载、gui_dist 同步、run.sh gui 全部删除。
# 本 REST API 标记 **deprecated**——不再新增端点；存量端点服务 remote hub 模式
# （INVEST_API_BASE 转发）与 event_watch 内部触发，待 MCP 覆盖 remote 场景后整体退役。
# GUI 若重做，走独立前端直连 MCP/新通道，不再由本进程 serve 静态文件。
