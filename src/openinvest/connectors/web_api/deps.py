"""get_pm 依赖 — 每请求新建 PortfolioManager（从 web_api.py 拆分，逻辑不变）。

每请求新建是刻意的：保证读到 scheduler/cron 刚写完的最新 memory，避免陈旧。
用 def（非 async）：同步文件读由 FastAPI 自动丢线程池，不堵事件循环。
"""
from __future__ import annotations

from fastapi import HTTPException

from openinvest.core.portfolio_manager import PortfolioManager


def get_pm() -> PortfolioManager:
    """每请求新建 PortfolioManager，
    保证读到 scheduler 刚写完的最新 memory，避免缓存陈旧

    fork 用户初次部署时 memory/*.md 还没生成 → PortfolioManager 抛 FileNotFoundError
    → 这里转成 503 友好提示，不让前端拿到 generic 500 + traceback
    """
    try:
        return PortfolioManager()
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                f"openInvest 还没初始化（{exc!s}）。先在服务器上跑 "
                "`~/.claude/skills/invest/scripts/run.sh init` 完成 onboarding，"
                "然后刷新本页面。"
            ),
        ) from exc
