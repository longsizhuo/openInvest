"""cash_write 路由 — 从 web_api.py 按 tag 拆分（行为不变）。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Body, Depends, HTTPException

from core.portfolio_manager import PortfolioManager

from connectors.web_api.models import CashWriteRequest, WriteResponse
from connectors.web_api.deps import get_pm

log = logging.getLogger("web_api")
from connectors.web_api.routers.write import _now_iso

router = APIRouter()


@router.post("/api/cash/{currency}/deposit", response_model=WriteResponse, tags=["cash_write"])
async def cash_deposit(currency: str, body: CashWriteRequest = Body(...), pm: PortfolioManager = Depends(get_pm)) -> WriteResponse:
    """v2 通用任意币种存款"""
    ccy = currency.upper()
    if not (3 <= len(ccy) <= 5) or not ccy.isalpha():
        raise HTTPException(status_code=400, detail=f"非法币种 {currency}")

    with pm.with_portfolio_tx() as p:
        cash = dict(p.get("cash") or {})
        new_balance = float(cash.get(ccy, 0) or 0) + body.amount
        cash[ccy] = round(new_balance, 2)
        p["cash"] = cash
    pm._reload()
    pm.store.append_history({
        "ts_origin": _now_iso(), "action": "deposit",
        "symbol": ccy, "units": body.amount, "currency": ccy, "source": "web_api",
    })
    return WriteResponse(
        cash_cny=pm.cash_amount("CNY"),
        aud_cash=pm.cash_amount("AUD"),
        history_appended=True,
        message=f"已存入 {ccy} {body.amount}，新余额 {new_balance:.2f}",
    )


@router.post("/api/cash/{currency}/withdraw", response_model=WriteResponse, tags=["cash_write"])
async def cash_withdraw(currency: str, body: CashWriteRequest = Body(...), pm: PortfolioManager = Depends(get_pm)) -> WriteResponse:
    """v2 通用任意币种取款（默认禁止扣到负数 — PM 强烈要求；后续可加 force=true 显式越过）

    余额检查在 fcntl 锁内执行，避免并发取款 TOCTOU 竞态。
    """
    ccy = currency.upper()
    if not (3 <= len(ccy) <= 5) or not ccy.isalpha():
        raise HTTPException(status_code=400, detail=f"非法币种 {currency}")


    with pm.with_portfolio_tx() as p:
        cash = dict(p.get("cash") or {})
        cur = float(cash.get(ccy, 0) or 0)
        if cur < body.amount:
            raise HTTPException(
                status_code=400,
                detail=f"{ccy} 余额不足：当前 {cur:.2f}，需要扣 {body.amount}",
            )
        new_balance = cur - body.amount
        cash[ccy] = round(new_balance, 2)
        p["cash"] = cash
    pm._reload()
    pm.store.append_history({
        "ts_origin": _now_iso(), "action": "withdraw",
        "symbol": ccy, "units": body.amount, "currency": ccy, "source": "web_api",
    })
    return WriteResponse(
        cash_cny=pm.cash_amount("CNY"),
        aud_cash=pm.cash_amount("AUD"),
        history_appended=True,
        message=f"已扣减 {ccy} {body.amount}，新余额 {new_balance:.2f}",
    )
