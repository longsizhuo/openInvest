"""write 路由 — 从 web_api.py 按 tag 拆分（行为不变）。"""
from __future__ import annotations

import logging
import os
from datetime import datetime

from fastapi import APIRouter, Body, Depends, HTTPException

from core.portfolio_manager import PortfolioManager
from utils.gold_price import infer_offset_pct

from connectors.web_api.models import (
    DepositRequest,
    GoldOffsetRequest,
    GoldSetRequest,
    GoldTradeRequest,
    WithdrawRequest,
    WriteResponse,
)
from connectors.web_api.deps import get_pm

log = logging.getLogger("web_api")
router = APIRouter()


def _now_iso() -> str:
    """本地时区 ISO 时间戳（与 core.memory_store._now_iso 对齐）"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ===== /api/deposit =====

@router.post("/api/deposit", response_model=WriteResponse, tags=["write"])
async def deposit(body: DepositRequest = Body(...), pm: PortfolioManager = Depends(get_pm)) -> WriteResponse:
    """存入现金（v2: 任意币种 cash dict 写入）。RMW 单锁，并发安全"""
    ccy = body.currency.upper()

    with pm.with_portfolio_tx() as p:
        cash = dict(p.get("cash") or {})
        new_balance = float(cash.get(ccy, 0) or 0) + body.amount
        cash[ccy] = round(new_balance, 2)
        p["cash"] = cash
    pm._reload()

    pm.store.append_history({
        "ts_origin": _now_iso(),
        "action": "deposit",
        "symbol": ccy,
        "units": body.amount,
        "currency": ccy,
        "source": "web_api",
    })

    return WriteResponse(
        cash_cny=pm.cash_amount("CNY"),
        aud_cash=pm.cash_amount("AUD"),
        history_appended=True,
        message=f"已存入 {ccy} {body.amount}，新余额 {new_balance:.2f}",
    )


# ===== /api/withdraw =====

@router.post("/api/withdraw", response_model=WriteResponse, tags=["write"])
async def withdraw(body: WithdrawRequest = Body(...), pm: PortfolioManager = Depends(get_pm)) -> WriteResponse:
    """取出现金（v2: 任意币种 + 负数校验）。
    余额不足默认 400 拒绝（PM 关切：避免 AUD -6894 类似事故）

    余额检查在 fcntl 锁内执行，避免并发取款 TOCTOU 竞态。
    """
    ccy = body.currency.upper()

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
        "ts_origin": _now_iso(),
        "action": "withdraw",
        "symbol": ccy,
        "units": body.amount,
        "currency": ccy,
        "source": "web_api",
    })

    return WriteResponse(
        cash_cny=pm.cash_amount("CNY"),
        aud_cash=pm.cash_amount("AUD"),
        history_appended=True,
        message=f"已扣减 {ccy} {body.amount}，新余额 {new_balance:.2f}",
    )


# ===== Gold helpers =====

def _gold_channel_defaults(pm: PortfolioManager) -> tuple[str, str]:
    """B4: 计算"创建黄金 holding"时用的默认 (channel, display_name)

    fork 用户可能用工行积存金 / 招行积存金 / 华安黄金 ETF / 实物黄金等渠道，
    硬编码 "浙商积存金" 会让账目从第一笔就语义错误。优先级：
      1. strategy.target_assets[GC=F].channel/display_name
      2. INVEST_GOLD_CHANNEL / INVEST_GOLD_DISPLAY env
      3. 通用兜底
    """
    targets = list(pm.strategy.get("target_assets", []) or [])
    gold_cfg = next((a for a in targets if a.get("symbol") == "GC=F"), None)
    if gold_cfg:
        ch = str(gold_cfg.get("channel") or "").strip()
        dn = str(gold_cfg.get("display_name") or "").strip()
        if ch and dn:
            return ch, dn
    env_ch = os.getenv("INVEST_GOLD_CHANNEL", "").strip()
    env_dn = os.getenv("INVEST_GOLD_DISPLAY", "").strip()
    if env_ch and env_dn:
        return env_ch, env_dn
    return "黄金（自营）", "实物黄金"


# ===== /api/gold/buy =====（保留旧 path 给前端兼容；内部用 holdings 写）

@router.post("/api/gold/buy", response_model=WriteResponse, tags=["write"])
async def gold_buy(body: GoldTradeRequest = Body(...), pm: PortfolioManager = Depends(get_pm)) -> WriteResponse:
    """记录黄金买入（v2: holdings.upsert("GC=F")）"""
    grams, price = body.grams, body.price_per_gram
    total = grams * price
    channel, display_name = _gold_channel_defaults(pm)

    with pm.with_portfolio_tx() as p:
        holdings = list(p.get("holdings") or [])
        gold = next((h for h in holdings if h.get("symbol") == "GC=F"), None)
        cur_grams = float(gold.get("units", 0) or 0) if gold else 0.0
        cur_avg = float(gold.get("avg_cost", 0) or 0) if gold else 0.0
        new_grams = cur_grams + grams
        new_avg = (
            (cur_avg * cur_grams + price * grams) / new_grams if new_grams else price
        )
        if gold:
            gold["units"] = round(new_grams, 4)
            gold["avg_cost"] = round(new_avg, 2)
        else:
            holdings.append({
                "symbol": "GC=F", "kind": "metal",
                "units": round(new_grams, 4), "unit_label": "克",
                "avg_cost": round(new_avg, 2), "cost_currency": "CNY",
                "channel": channel,
                "display_name": display_name,
                "yfinance_proxy": "GC=F", "proxy_kind": "gold_cny_per_gram",
                "sell_fee_pct": 0.0038,
            })
        p["holdings"] = holdings
    pm._reload()

    pm.store.append_history({
        "ts_origin": _now_iso(), "action": "bought",
        "symbol": "GOLD-CNY", "channel": channel,
        "units": grams, "price_per_unit": price,
        "total_amount": total, "currency": "CNY", "source": "web_api",
    })

    gold_h = pm.holdings.find("GC=F")
    return WriteResponse(
        gold_grams=float(gold_h.get("units", 0) or 0) if gold_h else 0.0,
        gold_avg_cost_cny_per_gram=float(gold_h.get("avg_cost", 0) or 0) if gold_h else 0.0,
        history_appended=True,
        message=f"买入 {grams}g @ ¥{price}/g (¥{total:,.2f})",
    )


# ===== /api/gold/sell =====

@router.post("/api/gold/sell", response_model=WriteResponse, tags=["write"])
async def gold_sell(body: GoldTradeRequest = Body(...), pm: PortfolioManager = Depends(get_pm)) -> WriteResponse:
    """记录黄金卖出（v2: holdings.find + cash["CNY"] 联动）"""
    grams, price = body.grams, body.price_per_gram

    targets = pm.strategy.get("target_assets", [])
    gold_target = next((a for a in targets if a.get("symbol") == "GC=F"), None)
    fee_pct = float(gold_target.get("sell_fee_pct", 0.0038)) if gold_target else 0.0038
    channel, _ = _gold_channel_defaults(pm)

    gross = grams * price
    fee = gross * fee_pct
    net = gross - fee

    with pm.with_portfolio_tx() as p:
        holdings = list(p.get("holdings") or [])
        gold = next((h for h in holdings if h.get("symbol") == "GC=F"), None)
        cur_grams = float(gold.get("units", 0) or 0) if gold else 0.0
        if cur_grams < grams:
            raise HTTPException(
                status_code=400,
                detail=f"卖出克数 {grams} 超过持仓 {cur_grams}",
            )
        gold["units"] = round(cur_grams - grams, 4)
        cash = dict(p.get("cash") or {})
        cash["CNY"] = round(float(cash.get("CNY", 0) or 0) + net, 2)
        p["holdings"] = holdings
        p["cash"] = cash
    pm._reload()

    pm.store.append_history({
        "ts_origin": _now_iso(), "action": "sold",
        "symbol": "GOLD-CNY", "channel": channel,
        "units": grams, "price_per_unit": price,
        "total_amount": gross, "fee": round(fee, 2), "net_amount": round(net, 2),
        "currency": "CNY", "source": "web_api",
    })

    gold_h = pm.holdings.find("GC=F")
    return WriteResponse(
        gold_grams=float(gold_h.get("units", 0) or 0) if gold_h else 0.0,
        cash_cny=pm.cash_amount("CNY"),
        history_appended=True,
        message=f"卖出 {grams}g @ ¥{price}/g，净入 ¥{net:,.2f}（扣费 ¥{fee:,.2f}）",
    )


# ===== /api/gold/set — 直接覆盖克数（校正用，不计流水）=====

@router.post("/api/gold/set", response_model=WriteResponse, tags=["write"])
async def gold_set(body: GoldSetRequest = Body(...), pm: PortfolioManager = Depends(get_pm)) -> WriteResponse:
    """直接设置黄金克数（v2: holdings GC=F units 直接覆盖；均价不变）"""
    channel, display_name = _gold_channel_defaults(pm)
    with pm.with_portfolio_tx() as p:
        holdings = list(p.get("holdings") or [])
        gold = next((h for h in holdings if h.get("symbol") == "GC=F"), None)
        if gold:
            gold["units"] = round(body.grams, 4)
        else:
            # 创建 GC=F holding（均价 0，等下次买入填）
            holdings.append({
                "symbol": "GC=F", "kind": "metal",
                "units": round(body.grams, 4), "unit_label": "克",
                "avg_cost": 0.0, "cost_currency": "CNY",
                "channel": channel,
                "display_name": display_name,
                "yfinance_proxy": "GC=F", "proxy_kind": "gold_cny_per_gram",
                "sell_fee_pct": 0.0038,
            })
        p["holdings"] = holdings
    pm._reload()
    gold_h = pm.holdings.find("GC=F")
    return WriteResponse(
        gold_grams=float(gold_h.get("units", 0) or 0) if gold_h else 0.0,
        history_appended=False,
        message=f"黄金克数已直接设为 {body.grams}g（成本均价不变）",
    )


# ===== /api/gold/offset — 反推渠道点差，写回 strategy.md =====

@router.post("/api/gold/offset", response_model=WriteResponse, tags=["write"])
async def gold_offset(body: GoldOffsetRequest = Body(...), pm: PortfolioManager = Depends(get_pm)) -> WriteResponse:
    """报当日实际买入克价 → 反推点差 offset → 写回 strategy.md。系统自学习渠道溢价"""
    offset = infer_offset_pct(body.bank_price)
    if offset is None:
        raise HTTPException(status_code=503, detail="无法获取实时金价，反推失败")

    targets = list(pm.strategy.get("target_assets", []))
    for a in targets:
        if a.get("symbol") == "GC=F":
            a["price_offset_pct"] = round(offset, 4)

    new_data = {
        "target_assets": targets,
        "target_allocation_stock": pm.strategy.get("target_allocation_stock", 0.7),
        "target_allocation_cash": pm.strategy.get("target_allocation_cash", 0.3),
    }
    pm.store.write("strategy", "strategy", new_data, pm.strategy.body)
    pm._reload()

    return WriteResponse(
        history_appended=False,
        message=f"渠道点差已更新: {offset*100:+.2f}%（用户报当日买入价 ¥{body.bank_price}/g）",
    )
