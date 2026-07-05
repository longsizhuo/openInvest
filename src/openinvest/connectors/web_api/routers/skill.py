"""skill 路由 — 从 web_api.py 按 tag 拆分（行为不变）。"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from openinvest.core.portfolio_manager import PortfolioManager

from openinvest.connectors.web_api.models import (
    SkillBuyRequest,
    SkillCashRequest,
    SkillDeleteHoldingRequest,
    SkillSellRequest,
    SkillWhatIfRequest,
)
from openinvest.connectors.web_api.deps import get_pm
from openinvest.paths import INVEST_ROOT

log = logging.getLogger("web_api")
router = APIRouter()


# ============================================================
# Skill-parity 端点（远端模式 hub-and-spoke）
# ============================================================
# scripts/skill.py 在 INVEST_API_BASE 模式下把子命令转发到这里。输出形状与
# CLI 完全一致——共享 services/skill_views.py / PortfolioManager.buy|sell，
# 客户端拿到的 JSON 与本地跑零差异（防 local/remote 漂移，见 CLAUDE.md 分层契约）。
#
# 错误语义对齐 CLI：域内错误（what_if symbol 不在持仓等）保持 CLI 行为 ——
# HTTP 200 + {"status": "error", ...} dict；仅基础设施/参数错误用 HTTP 状态码
# （503 memory 未初始化 / 400 units 非法），remote dispatch 端把它们映射回
# CLI 同款 error JSON + exit code。

_REPO_ROOT = INVEST_ROOT


@router.get("/api/doctor", tags=["skill"])
async def skill_doctor() -> Dict[str, Any]:
    """cmd_doctor 同款健康自检（hub 视角：检查 hub 的 memory/.env/LLM 可达性）

    远端模式下客户端 doctor = 本接口结果 + 客户端本地段（连通性/token）。
    """
    from openinvest.services.skill_views import build_doctor_view
    return build_doctor_view(_REPO_ROOT)


@router.get("/api/skill/status", tags=["skill"])
async def skill_status() -> Dict[str, Any]:
    """cmd_status 同款 JSON（cash/ndq/gold/all_holdings/total_assets_cny/fx/live_prices）"""
    from openinvest.services.skill_views import build_status_view
    try:
        return build_status_view()
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                f"openInvest 还没初始化（{exc!s}）。先在 hub 上跑 "
                "`~/.claude/skills/invest/scripts/run.sh init` 完成 onboarding。"
            ),
        ) from exc


@router.get("/api/skill/strategy", tags=["skill"])
async def skill_strategy() -> Dict[str, Any]:
    """cmd_strategy 同款 JSON（strategy frontmatter + Dreaming insights）"""
    from openinvest.services.skill_views import build_strategy_view
    return build_strategy_view()


@router.get("/api/skill/history", tags=["skill"])
async def skill_history(n: int = Query(10, ge=1)) -> Dict[str, Any]:
    """cmd_history 同款 JSON（recent_trades + recent_debates，/api/history 只有 trades）"""
    from openinvest.services.skill_views import build_history_view
    return build_history_view(n)


@router.post("/api/skill/what_if", tags=["skill"])
async def skill_what_if(body: SkillWhatIfRequest = Body(default=SkillWhatIfRequest())) -> Dict[str, Any]:
    """cmd_what_if 同款情景模拟（任意持仓涨跌 / 兼容 gold/ndq/audcny 旧参数）"""
    from openinvest.services.skill_views import build_what_if_view
    return build_what_if_view(
        symbol=body.symbol, pct=body.pct, price=body.price,
        gold_price=body.gold_price, gold_pct=body.gold_pct,
        ndq_price=body.ndq_price, ndq_pct=body.ndq_pct,
        audcny=body.audcny,
    )


@router.post("/api/skill/buy", tags=["skill"])
async def skill_buy(body: SkillBuyRequest = Body(...), pm: PortfolioManager = Depends(get_pm)) -> Dict[str, Any]:
    """cmd_buy 同款加仓/建仓（加权平均成本 + 同步扣现金 + history 记 skill_remote）"""
    try:
        return pm.buy(
            body.symbol, body.units, body.price,
            currency=body.currency, kind=body.kind, unit_label=body.unit_label,
            source="skill_remote",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/skill/sell", tags=["skill"])
async def skill_sell(body: SkillSellRequest = Body(...), pm: PortfolioManager = Depends(get_pm)) -> Dict[str, Any]:
    """cmd_sell 同款减仓（units 减、cost_avg 不变、按 cost_currency 还现金）"""
    try:
        return pm.sell(body.symbol, body.units, body.price, source="skill_remote")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/skill/deposit", tags=["skill"])
async def skill_deposit(body: SkillCashRequest = Body(...), pm: PortfolioManager = Depends(get_pm)) -> Dict[str, Any]:
    """cmd_deposit 同款存现金（/api/cash/* 是 WriteResponse 形状，对不上 CLI，故另设）"""
    try:
        return pm.deposit_cash(body.currency, body.amount, source="skill_remote")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/skill/withdraw", tags=["skill"])
async def skill_withdraw(body: SkillCashRequest = Body(...), pm: PortfolioManager = Depends(get_pm)) -> Dict[str, Any]:
    """cmd_withdraw 同款取现金（余额检查在 fcntl 锁内）"""
    try:
        return pm.withdraw_cash(body.currency, body.amount, source="skill_remote")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/skill/delete_holding", tags=["skill"])
async def skill_delete_holding(body: SkillDeleteHoldingRequest = Body(...), pm: PortfolioManager = Depends(get_pm)) -> Dict[str, Any]:
    """cmd_delete_holding 同款删持仓行（支持 force；DELETE /api/holdings 无 force 语义）"""
    try:
        return pm.delete_holding(body.symbol, force=body.force, source="skill_remote")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
