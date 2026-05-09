"""Web API connector — FastAPI REST 层

设计原则（仿 connectors/napcat_bot.py 的多消费者模式）：
- core/PortfolioManager 是唯一数据源，本文件只做 HTTP 包装，不重新写业务逻辑
- 每个请求新建一个 PortfolioManager，确保读到最新 memory（和 napcat 一致）
- 写操作复用 PortfolioManager.with_portfolio_tx() 的 fcntl 锁（PR 2 才用到）
- 同源部署（Caddy /api/* → 本服务），生产环境**不需要 CORS 头**
- 仅当 INVEST_WEB_DEV_CORS=1 时为 Vite dev server (5173) 放行跨域
- 鉴权由 Cloudflare Access 在边缘完成，本服务只绑 127.0.0.1，公网扫不到

启动：
    uvicorn connectors.web_api:app --host 127.0.0.1 --port 8765

systemd 见 systemd/invest-web.service
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

from dotenv import load_dotenv
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from core.memory_store import MemoryStore
from core.portfolio_manager import PortfolioManager
from core.schemas import (
    StrategyData,
    TargetAsset as SchemaTargetAsset,
    validate_strategy,
)
from utils.exchange_fee import get_history_data
from utils.gold_price import get_gold_snapshot, infer_offset_pct
from utils.quotes import get_quote

load_dotenv()

# ============ 配置 ============

# 仅开发环境（Vite :5173 跨域调本机 :8765）需要打开 CORS；生产同源部署不开
DEV_CORS = os.getenv("INVEST_WEB_DEV_CORS", "0") == "1"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("web_api")


# ============ Pydantic 响应模型 ============

class HealthResponse(BaseModel):
    """/api/health 响应"""
    ok: bool = True
    service: str = "invest-web-api"
    timestamp: str


class CashSummary(BaseModel):
    """现金部分"""
    cny: float = Field(..., description="CNY 现金")
    aud: float = Field(..., description="AUD 现金（CommSec 子弹）")


class GoldHolding(BaseModel):
    """黄金持仓 + 实时估值"""
    grams: float = Field(..., description="持仓克数")
    avg_cost_cny_per_gram: float = Field(..., description="加权均价 CNY/g")
    spot_cny_per_gram: Optional[float] = Field(None, description="实时现货价 CNY/g（yfinance）")
    bank_cny_per_gram: Optional[float] = Field(None, description="浙商参考克价（含点差）")
    offset_pct: float = Field(0.0, description="浙商点差")
    market_value_cny: Optional[float] = Field(None, description="持仓现值 CNY")
    pnl_cny: Optional[float] = Field(None, description="浮盈 CNY")
    is_stale: bool = Field(False, description="价格来自 DB 兜底（yfinance 不可用）")


class NDQHolding(BaseModel):
    """NDQ.AX 持仓 + 实时行情"""
    shares: float = Field(..., description="持仓股数")
    last_price_aud: Optional[float] = Field(None, description="最新价 AUD")
    prev_close_aud: Optional[float] = Field(None, description="前收 AUD")
    day_change_pct: Optional[float] = Field(None, description="日变化 %")
    last_updated: Optional[str] = Field(None, description="行情日期 YYYY-MM-DD")


class PortfolioResponse(BaseModel):
    """/api/portfolio 响应：完整持仓快照"""
    cash: CashSummary
    gold: GoldHolding
    ndq: NDQHolding


class TargetAsset(BaseModel):
    """strategy.md 中的单个目标资产"""
    symbol: str
    display_name: Optional[str] = None
    channel: Optional[str] = None
    max_single_invest_cny: float = 0
    price_offset_pct: Optional[float] = None
    sell_fee_pct: Optional[float] = None


class StrategyResponse(BaseModel):
    """/api/strategy 响应"""
    target_allocation_stock: float
    target_allocation_cash: float
    target_assets: List[TargetAsset]


class HistoryRow(BaseModel):
    """单笔交易记录（兼容历史字段变化）"""
    # frontmatter 历史上字段会增减，开 extra=allow 兜底
    model_config = ConfigDict(extra="allow")

    ts: Optional[str] = None
    ts_origin: Optional[str] = None
    action: Optional[str] = None
    symbol: Optional[str] = None
    units: Optional[float] = None
    price_per_unit: Optional[float] = None
    total_amount: Optional[float] = None
    currency: Optional[str] = None
    channel: Optional[str] = None
    source: Optional[str] = None


class HistoryResponse(BaseModel):
    """/api/history 响应"""
    count: int
    rows: List[HistoryRow]


class DailyEntry(BaseModel):
    """单天 daily 日志（完整 markdown）"""
    date: str
    content: str


class DailyResponse(BaseModel):
    """/api/daily 响应"""
    count: int
    entries: List[DailyEntry]


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


def _new_pm() -> PortfolioManager:
    """每请求新建 PortfolioManager（仿 napcat_bot.route 内的做法），
    保证读到 scheduler 刚写完的最新 memory，避免缓存陈旧"""
    return PortfolioManager()


# ============ 端点：健康检查 ============

@app.get("/api/health", response_model=HealthResponse, tags=["meta"])
async def health() -> HealthResponse:
    """健康检查 — systemd / Caddy 探活用"""
    return HealthResponse(
        timestamp=datetime.now().astimezone().isoformat(timespec="seconds"),
    )


# ============ 端点：持仓 ============

def _build_gold(pm: PortfolioManager) -> GoldHolding:
    """组装黄金持仓 + 实时估值（v2: 从 pm.holdings.find('GC=F') 读）"""
    targets = pm.strategy.get("target_assets", [])
    gold_target = next((a for a in targets if a.get("symbol") == "GC=F"), None)
    # offset 优先从 holding 取，兜底从 strategy.target_assets
    gold_holding = pm.holdings.find("GC=F")
    offset = 0.0
    if gold_holding and gold_holding.get("price_offset_pct") is not None:
        offset = float(gold_holding["price_offset_pct"])
    elif gold_target:
        offset = float(gold_target.get("price_offset_pct", 0.0) or 0.0)

    grams = float(gold_holding.get("units", 0) or 0) if gold_holding else 0.0
    avg_cost = float(gold_holding.get("avg_cost", 0) or 0) if gold_holding else 0.0

    snap = get_gold_snapshot(offset_pct=offset)
    if snap is None:
        # yfinance + DB 兜底都失败：仅返回静态字段
        return GoldHolding(
            grams=grams,
            avg_cost_cny_per_gram=avg_cost,
            offset_pct=offset,
            is_stale=True,
        )
    market_value = snap.spot_cny_per_gram * grams if grams else None
    pnl = (snap.spot_cny_per_gram - avg_cost) * grams if avg_cost and grams else None
    return GoldHolding(
        grams=grams,
        avg_cost_cny_per_gram=avg_cost,
        spot_cny_per_gram=snap.spot_cny_per_gram,
        bank_cny_per_gram=snap.bank_cny_per_gram,
        offset_pct=snap.offset_pct,
        market_value_cny=market_value,
        pnl_cny=pnl,
        is_stale=snap.is_stale,
    )


def _build_ndq(shares: float) -> NDQHolding:
    """从 yfinance 拉 NDQ.AX 5d 历史；失败时仅返回股数，不阻塞整个 portfolio 端点"""
    try:
        df = get_history_data("NDQ.AX", "5d")
    except Exception as e:  # noqa: BLE001  yfinance 抛各种网络异常都接住
        log.warning(f"NDQ.AX 行情拉取失败: {e}")
        return NDQHolding(shares=shares)
    if df is None or df.empty:
        return NDQHolding(shares=shares)
    last = float(df["Close"].iloc[-1])
    prev = float(df["Close"].iloc[-2]) if len(df) > 1 else last
    pct = (last / prev - 1) * 100 if prev else 0.0
    return NDQHolding(
        shares=shares,
        last_price_aud=last,
        prev_close_aud=prev,
        day_change_pct=round(pct, 4),
        last_updated=df.index[-1].strftime("%Y-%m-%d"),
    )


def _build_portfolio_response(pm: PortfolioManager) -> PortfolioResponse:
    """组装完整持仓快照（v2: 从 pm.cash + pm.holdings 读，输出仍是 v1 schema 兼容前端）"""
    ndq_h = pm.holdings.find("NDQ.AX")
    ndq_shares = float(ndq_h.get("units", 0) or 0) if ndq_h else 0.0
    return PortfolioResponse(
        cash=CashSummary(
            cny=pm.cash_amount("CNY"),
            aud=pm.cash_amount("AUD"),
        ),
        gold=_build_gold(pm),
        ndq=_build_ndq(ndq_shares),
    )


@app.get("/api/portfolio", response_model=PortfolioResponse, tags=["read"])
async def get_portfolio() -> PortfolioResponse:
    """完整持仓快照（v1 兼容输出，前端无感）：现金 CNY/AUD + 黄金 + NDQ.AX"""
    return _build_portfolio_response(_new_pm())


# ============ v2 通用持仓 ============

class HoldingQuote(BaseModel):
    """单个 holding 的实时行情（通用化 v2 端点用）"""
    price: Optional[float] = None
    currency: Optional[str] = None
    unit: Optional[str] = None
    last_updated: Optional[str] = None
    is_stale: bool = False
    extra: Optional[Dict[str, Any]] = None


class HoldingV2(BaseModel):
    """单个 v2 holding（含静态字段 + 实时 quote + 计算的 P&L）"""
    symbol: str
    kind: str
    units: float
    unit_label: str
    avg_cost: float
    cost_currency: str
    channel: Optional[str] = None
    display_name: Optional[str] = None
    yfinance_proxy: Optional[str] = None
    proxy_kind: str = "direct"
    price_offset_pct: Optional[float] = None
    sell_fee_pct: Optional[float] = None
    is_tracking_only: bool = False
    quote: Optional[HoldingQuote] = None
    market_value: Optional[float] = None     # cost_currency 计价
    pnl: Optional[float] = None              # cost_currency 计价


class HoldingsListResponse(BaseModel):
    """GET /api/holdings 响应"""
    cash: Dict[str, float]
    holdings: List[HoldingV2]


def _build_holding_v2(h: Dict[str, Any]) -> HoldingV2:
    """组装单个 v2 holding（含实时 quote + P&L 计算）"""
    quote = get_quote(h)
    units = float(h.get("units", 0) or 0)
    avg_cost = float(h.get("avg_cost", 0) or 0)

    market_value = None
    pnl = None
    quote_resp: Optional[HoldingQuote] = None
    if quote is not None:
        quote_resp = HoldingQuote(
            price=quote.price,
            currency=quote.currency,
            unit=quote.unit,
            last_updated=quote.last_updated,
            is_stale=quote.is_stale,
            extra=quote.extra or None,
        )
        # 追踪仓不算 P&L
        if not h.get("is_tracking_only") and units > 0:
            market_value = quote.price * units
            if avg_cost:
                pnl = (quote.price - avg_cost) * units

    return HoldingV2(
        symbol=h["symbol"],
        kind=h.get("kind", "other"),
        units=units,
        unit_label=h.get("unit_label", "share"),
        avg_cost=avg_cost,
        cost_currency=h.get("cost_currency", ""),
        channel=h.get("channel"),
        display_name=h.get("display_name"),
        yfinance_proxy=h.get("yfinance_proxy"),
        proxy_kind=h.get("proxy_kind", "direct"),
        price_offset_pct=h.get("price_offset_pct"),
        sell_fee_pct=h.get("sell_fee_pct"),
        is_tracking_only=bool(h.get("is_tracking_only", False)),
        quote=quote_resp,
        market_value=market_value,
        pnl=pnl,
    )


@app.get("/api/holdings", response_model=HoldingsListResponse, tags=["read"])
async def get_holdings() -> HoldingsListResponse:
    """v2 通用持仓列表：cash dict + holdings 数组（含实时 quote + 计算 P&L）"""
    pm = _new_pm()
    holdings = [_build_holding_v2(h) for h in pm.holdings]
    return HoldingsListResponse(cash=pm.cash, holdings=holdings)


# ============ 多币种总市值折算（v3 补充）============

class TotalValueBreakdownItem(BaseModel):
    """单项资产 / 现金 在折算币种下的金额"""
    label: str
    kind: str   # "cash" | "holding"
    amount_local: float
    currency_local: str
    amount_in_base: Optional[float] = None
    fx_rate: Optional[float] = None
    note: Optional[str] = None


class TotalValueResponse(BaseModel):
    """完整资产折算到指定币种"""
    base_currency: str
    cash_total: float
    holdings_total: float
    grand_total: float
    breakdown: List[TotalValueBreakdownItem]
    fx_rates: Dict[str, Optional[float]]


@app.get("/api/portfolio/total_value", response_model=TotalValueResponse, tags=["read"])
async def get_portfolio_total_value(
    base: str = Query("CNY", min_length=3, max_length=5, description="折算目标币种"),
) -> TotalValueResponse:
    """所有现金 + 持仓 折算到指定币种的总市值

    现金：用 cash dict + yfinance 汇率
    持仓：用 quote.price * units，quote 是 cost_currency 计价，再用汇率折算
    追踪仓不计入（is_tracking_only）
    """
    from utils.fx import get_fx_rate
    base = base.upper().strip()
    pm = _new_pm()

    breakdown: List[TotalValueBreakdownItem] = []
    fx_rates: Dict[str, Optional[float]] = {}

    cash_total = 0.0
    for ccy, amt in pm.cash.items():
        rate = get_fx_rate(ccy, base)
        fx_rates[ccy] = rate
        in_base = amt * rate if rate is not None else None
        if in_base is not None:
            cash_total += in_base
        breakdown.append(TotalValueBreakdownItem(
            label=f"现金 {ccy}",
            kind="cash",
            amount_local=amt,
            currency_local=ccy,
            amount_in_base=in_base,
            fx_rate=rate,
            note=None if rate is not None else "汇率拉取失败，未计入总额",
        ))

    holdings_total = 0.0
    for h in pm.holdings:
        if h.get("is_tracking_only"):
            continue
        units = float(h.get("units", 0) or 0)
        if units <= 0:
            continue
        cost_ccy = str(h.get("cost_currency", "")).upper()
        sym = str(h.get("symbol", ""))
        # 用实时 quote 算市值
        quote = get_quote(h)
        if quote is None or quote.price <= 0:
            breakdown.append(TotalValueBreakdownItem(
                label=h.get("display_name") or sym,
                kind="holding",
                amount_local=0,
                currency_local=cost_ccy,
                amount_in_base=None,
                fx_rate=None,
                note="行情拉取失败，未计入",
            ))
            continue
        market_value_local = quote.price * units
        rate = fx_rates.get(cost_ccy)
        if rate is None:
            rate = get_fx_rate(cost_ccy, base)
            fx_rates[cost_ccy] = rate
        in_base = market_value_local * rate if rate is not None else None
        if in_base is not None:
            holdings_total += in_base
        breakdown.append(TotalValueBreakdownItem(
            label=f"{h.get('display_name') or sym} ({sym})",
            kind="holding",
            amount_local=round(market_value_local, 2),
            currency_local=cost_ccy,
            amount_in_base=round(in_base, 2) if in_base is not None else None,
            fx_rate=rate,
            note=None if rate is not None else "汇率拉取失败",
        ))

    return TotalValueResponse(
        base_currency=base,
        cash_total=round(cash_total, 2),
        holdings_total=round(holdings_total, 2),
        grand_total=round(cash_total + holdings_total, 2),
        breakdown=breakdown,
        fx_rates=fx_rates,
    )


# ============ v2 通用 holdings CRUD ============

class HoldingCreateRequest(BaseModel):
    """POST /api/holdings body — 新增持仓"""
    symbol: str = Field(..., min_length=1, max_length=32, pattern=r"^[A-Za-z0-9._=^:\-]+$")
    kind: Literal["equity", "etf", "metal", "crypto", "bond", "fund", "other"] = "equity"
    units: float = Field(default=0.0, ge=0)
    unit_label: str = Field(default="股", max_length=8)
    avg_cost: float = Field(default=0.0, ge=0)
    cost_currency: str = Field(..., pattern=r"^[A-Za-z]{3,5}$")
    channel: Optional[str] = Field(None, max_length=64)
    display_name: Optional[str] = Field(None, max_length=128)
    yfinance_proxy: Optional[str] = Field(None, max_length=32)
    proxy_kind: Literal["direct", "gold_cny_per_gram", "fx_pair"] = "direct"
    is_tracking_only: bool = False


class HoldingPatchRequest(BaseModel):
    """PUT /api/holdings/{symbol} body — 部分字段更新"""
    kind: Optional[Literal["equity", "etf", "metal", "crypto", "bond", "fund", "other"]] = None
    units: Optional[float] = Field(None, ge=0)
    unit_label: Optional[str] = Field(None, max_length=8)
    avg_cost: Optional[float] = Field(None, ge=0)
    cost_currency: Optional[str] = Field(None, pattern=r"^[A-Za-z]{3,5}$")
    channel: Optional[str] = None
    display_name: Optional[str] = None
    yfinance_proxy: Optional[str] = None
    proxy_kind: Optional[Literal["direct", "gold_cny_per_gram", "fx_pair"]] = None
    is_tracking_only: Optional[bool] = None


@app.post("/api/holdings", response_model=HoldingV2, tags=["holdings_write"])
async def add_holding(body: HoldingCreateRequest = Body(...)) -> HoldingV2:
    """新增持仓（任意 yfinance symbol）。symbol 已存在 → 409"""
    new_holding = body.model_dump(exclude_none=True)
    new_holding["cost_currency"] = str(new_holding["cost_currency"]).upper()

    pm = _new_pm()
    with pm.with_portfolio_tx() as p:
        holdings = list(p.get("holdings") or [])
        if any(h.get("symbol") == body.symbol for h in holdings):
            raise HTTPException(status_code=409, detail=f"symbol {body.symbol} 已存在；用 PUT 更新或先 DELETE")
        holdings.append(new_holding)
        p["holdings"] = holdings
    pm._reload()

    h = pm.holdings.find(body.symbol)
    if h is None:  # 防御
        raise HTTPException(status_code=500, detail="新增后读不到，数据可能撕裂")
    return _build_holding_v2(h)


@app.put("/api/holdings/{symbol}", response_model=HoldingV2, tags=["holdings_write"])
async def update_holding(symbol: str, body: HoldingPatchRequest = Body(...)) -> HoldingV2:
    """部分字段更新单个 holding（仅传非空字段会被改写）"""
    patch = body.model_dump(exclude_none=True)
    if not patch:
        raise HTTPException(status_code=400, detail="patch 必须含至少一个字段")
    if "cost_currency" in patch:
        patch["cost_currency"] = str(patch["cost_currency"]).upper()

    pm = _new_pm()
    with pm.with_portfolio_tx() as p:
        holdings = list(p.get("holdings") or [])
        target = next((h for h in holdings if h.get("symbol") == symbol), None)
        if target is None:
            raise HTTPException(status_code=404, detail=f"symbol {symbol} 不存在")
        target.update(patch)
        p["holdings"] = holdings
    pm._reload()
    h = pm.holdings.find(symbol)
    if h is None:
        raise HTTPException(status_code=500, detail="更新后读不到")
    return _build_holding_v2(h)


@app.delete("/api/holdings/{symbol}", tags=["holdings_write"])
async def delete_holding(symbol: str) -> Dict[str, Any]:
    """删除持仓。units > 0 时拒绝（避免数据丢失），用户必须先卖光或显式 set units=0"""
    pm = _new_pm()
    with pm.with_portfolio_tx() as p:
        holdings = list(p.get("holdings") or [])
        target = next((h for h in holdings if h.get("symbol") == symbol), None)
        if target is None:
            raise HTTPException(status_code=404, detail=f"symbol {symbol} 不存在")
        if not target.get("is_tracking_only") and float(target.get("units", 0) or 0) > 0:
            raise HTTPException(
                status_code=400,
                detail=f"{symbol} 持仓 {target.get('units')} > 0，请先卖光或显式 set units=0",
            )
        p["holdings"] = [h for h in holdings if h.get("symbol") != symbol]
    pm._reload()
    return {"ok": True, "deleted": symbol}


# 注：v2 通用 cash CRUD（/api/cash/{currency}/deposit|withdraw）放在文件末尾，
# 因为它依赖 WriteResponse 定义（在 PR 2 区域）

# ============ yfinance Search 端点 ============

class SymbolSearchResult(BaseModel):
    """单个搜索命中"""
    symbol: str
    shortname: Optional[str] = None
    longname: Optional[str] = None
    exchange: Optional[str] = None
    quote_type: Optional[str] = None


class SymbolSearchResponse(BaseModel):
    count: int
    results: List[SymbolSearchResult]


@app.get("/api/symbols/search", response_model=SymbolSearchResponse, tags=["read"])
async def search_symbols(
    q: str = Query(..., min_length=1, max_length=64, description="搜索关键词"),
    limit: int = Query(8, ge=1, le=20),
) -> SymbolSearchResponse:
    """通过 yfinance Search 搜索 symbol（零 token 配置）。
    用户输入 'apple' / '腾讯' / 'TSLA' 都能用"""
    try:
        from yfinance import Search
        s = Search(q, max_results=limit)
        quotes_raw = list(getattr(s, "quotes", []) or [])
    except Exception as e:  # noqa: BLE001
        log.warning(f"yfinance Search '{q}' 失败: {e}")
        return SymbolSearchResponse(count=0, results=[])

    results = []
    for r in quotes_raw[:limit]:
        if not isinstance(r, dict):
            continue
        results.append(SymbolSearchResult(
            symbol=str(r.get("symbol", "")),
            shortname=r.get("shortname"),
            longname=r.get("longname"),
            exchange=r.get("exchange"),
            quote_type=r.get("quoteType"),
        ))
    return SymbolSearchResponse(count=len(results), results=results)


# ============ 端点：策略 ============

@app.get("/api/strategy", response_model=StrategyResponse, tags=["read"])
async def get_strategy() -> StrategyResponse:
    """当前投资策略：目标比例 + 各资产 cap / 点差 / 费率"""
    pm = _new_pm()
    targets_raw = pm.strategy.get("target_assets", []) or []
    targets = [
        TargetAsset(
            symbol=str(a.get("symbol", "")),
            display_name=a.get("display_name"),
            channel=a.get("channel"),
            max_single_invest_cny=float(a.get("max_single_invest_cny", 0) or 0),
            price_offset_pct=a.get("price_offset_pct"),
            sell_fee_pct=a.get("sell_fee_pct"),
        )
        for a in targets_raw
    ]
    return StrategyResponse(
        target_allocation_stock=float(pm.strategy.get("target_allocation_stock", 0.7)),
        target_allocation_cash=float(pm.strategy.get("target_allocation_cash", 0.3)),
        target_assets=targets,
    )


# ============ 端点：黄金独立查询 ============

@app.get("/api/gold", response_model=GoldHolding, tags=["read"])
async def get_gold() -> GoldHolding:
    """黄金持仓 + 实时金价 + 浙商参考价（独立端点，前端可单独刷新而不重拉 NDQ）"""
    return _build_gold(_new_pm())


# ============ 端点：NDQ 独立查询 ============

@app.get("/api/ndq", response_model=NDQHolding, tags=["read"])
async def get_ndq() -> NDQHolding:
    """NDQ.AX 持仓 + 实时价 + 日变化（v2: 从 pm.holdings 读）"""
    pm = _new_pm()
    ndq_h = pm.holdings.find("NDQ.AX")
    shares = float(ndq_h.get("units", 0) or 0) if ndq_h else 0.0
    return _build_ndq(shares)


# ============ 端点：交易历史 ============

@app.get("/api/history", response_model=HistoryResponse, tags=["read"])
async def get_history(
    limit: int = Query(100, ge=1, le=1000, description="返回最近 N 笔（按时间倒序）"),
) -> HistoryResponse:
    """交易流水（portfolio_history.jsonl），按时间倒序返回最近 limit 条"""
    store = MemoryStore()
    rows = store.read_history()
    # jsonl 是 append-only 时间正序，前端要倒序展示，这里直接 reverse 切片
    rows_recent = list(reversed(rows[-limit:]))
    return HistoryResponse(
        count=len(rows_recent),
        rows=[HistoryRow(**r) for r in rows_recent],
    )


# ============ 端点：PnL 趋势图（SVG）============

PNL_CHART_PATH = Path(__file__).parent.parent / "docs" / "pnl_chart.svg"


@app.get("/api/pnl_chart.svg", tags=["read"])
async def get_pnl_chart() -> FileResponse:
    """jobs/pnl_snapshot 每 2h 工作日自动生成的 PnL 趋势图（vs 8 个基准）。
    SVG 只含百分比，不暴露绝对金额"""
    if not PNL_CHART_PATH.exists():
        raise HTTPException(status_code=404, detail="pnl_chart.svg 不存在；jobs/pnl_snapshot 还没跑过")
    return FileResponse(
        PNL_CHART_PATH,
        media_type="image/svg+xml",
        headers={"Cache-Control": "no-cache"},  # 让前端能拿到最新版本
    )


# ============ 端点：daily 决策快照 ============

@app.get("/api/daily", response_model=DailyResponse, tags=["read"])
async def get_daily(
    since: int = Query(7, ge=1, le=90, description="最近 N 天"),
) -> DailyResponse:
    """daily/<date>.md 完整 markdown，前端用 react-markdown 渲染"""
    store = MemoryStore()
    paths = store.list_daily(since_days=since)
    entries: List[DailyEntry] = []
    for p in paths:
        try:
            content = p.read_text(encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            log.warning(f"读 {p} 失败: {e}")
            continue
        # 文件名是 YYYY-MM-DD.md，stem 即日期
        entries.append(DailyEntry(date=p.stem, content=content))
    return DailyResponse(count=len(entries), entries=entries)


# ============================================================
# PR 2: 写操作 + 委员会异步
# ============================================================
# 设计：
# - 所有写端点都走 PortfolioManager.with_portfolio_tx()（fcntl + 原子写）
# - 业务逻辑直接复用 connectors/napcat_bot.py 里 _deposit/_gold_buy 等的写法，
#   不重新发明轮子（保证 NapCat 与 Web 写入语义一致，包括 history 字段）
# - 委员会触发是长任务（~6 min），用 asyncio.run_in_executor 跑同步入口，
#   状态落盘到 memory/.committee/<task_id>/status.json，前端 SWR 轮询查


# ===== 写操作请求模型 =====

class DepositRequest(BaseModel):
    """POST /api/deposit body"""
    currency: Literal["cny", "aud"] = Field("cny", description="cny=人民币 / aud=澳元（NDQ 子弹）")
    amount: float = Field(..., gt=0, description="正数金额")


class WithdrawRequest(BaseModel):
    """POST /api/withdraw body"""
    currency: Literal["cny", "aud"] = Field("cny", description="cny=人民币 / aud=澳元")
    amount: float = Field(..., gt=0, description="正数金额")


class GoldTradeRequest(BaseModel):
    """POST /api/gold/buy 和 /api/gold/sell 共用 body"""
    grams: float = Field(..., gt=0, description="买入/卖出克数")
    price_per_gram: float = Field(..., gt=0, description="单价 CNY/g")


class GoldSetRequest(BaseModel):
    """POST /api/gold/set body — 直接覆盖克数（不计流水，仅校正用）"""
    grams: float = Field(..., ge=0, description="目标克数（≥0）")


class GoldOffsetRequest(BaseModel):
    """POST /api/gold/offset body — 报当日浙商克价，反推点差"""
    bank_price: float = Field(..., gt=0, description="浙商当日克价 CNY/g")


class WriteResponse(BaseModel):
    """写操作统一响应：返回新写入字段 + history 是否记录"""
    ok: bool = True
    cash_cny: Optional[float] = None
    aud_cash: Optional[float] = None
    gold_grams: Optional[float] = None
    gold_avg_cost_cny_per_gram: Optional[float] = None
    history_appended: bool = False
    message: str


def _now_iso() -> str:
    """本地时区 ISO 时间戳（与 core.memory_store._now_iso 对齐）"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ===== /api/deposit =====

@app.post("/api/deposit", response_model=WriteResponse, tags=["write"])
async def deposit(body: DepositRequest = Body(...)) -> WriteResponse:
    """存入现金（v2: 任意币种 cash dict 写入）。RMW 单锁，并发安全"""
    pm = _new_pm()
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

@app.post("/api/withdraw", response_model=WriteResponse, tags=["write"])
async def withdraw(body: WithdrawRequest = Body(...)) -> WriteResponse:
    """取出现金（v2: 任意币种 + 负数校验）。
    余额不足默认 400 拒绝（PM 关切：避免 AUD -6894 类似事故）"""
    pm = _new_pm()
    ccy = body.currency.upper()
    cur = pm.cash_amount(ccy)
    if cur < body.amount:
        raise HTTPException(
            status_code=400,
            detail=f"{ccy} 余额不足：当前 {cur:.2f}，需要扣 {body.amount}",
        )

    with pm.with_portfolio_tx() as p:
        cash = dict(p.get("cash") or {})
        new_balance = float(cash.get(ccy, 0) or 0) - body.amount
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

@app.post("/api/gold/buy", response_model=WriteResponse, tags=["write"])
async def gold_buy(body: GoldTradeRequest = Body(...)) -> WriteResponse:
    """记录黄金买入（v2: holdings.upsert("GC=F")）"""
    pm = _new_pm()
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

@app.post("/api/gold/sell", response_model=WriteResponse, tags=["write"])
async def gold_sell(body: GoldTradeRequest = Body(...)) -> WriteResponse:
    """记录黄金卖出（v2: holdings.find + cash["CNY"] 联动）"""
    pm = _new_pm()
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

@app.post("/api/gold/set", response_model=WriteResponse, tags=["write"])
async def gold_set(body: GoldSetRequest = Body(...)) -> WriteResponse:
    """直接设置黄金克数（v2: holdings GC=F units 直接覆盖；均价不变）"""
    pm = _new_pm()
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


# ===== /api/gold/offset — 反推浙商点差，写回 strategy.md =====

@app.post("/api/gold/offset", response_model=WriteResponse, tags=["write"])
async def gold_offset(body: GoldOffsetRequest = Body(...)) -> WriteResponse:
    """报当日浙商克价 → 反推点差 offset → 写回 strategy.md。系统自学习浙商溢价"""
    offset = infer_offset_pct(body.bank_price)
    if offset is None:
        raise HTTPException(status_code=503, detail="无法获取实时金价，反推失败")

    pm = _new_pm()
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
        message=f"浙商点差已更新: {offset*100:+.2f}%（用户报 ¥{body.bank_price}/g）",
    )


# ============ 委员会异步任务 ============

# task store 落盘根目录（fcntl 锁由 MemoryStore 复用）
COMMITTEE_DIR = Path(__file__).parent.parent / "memory" / ".committee"


class CommitteeRunRequest(BaseModel):
    """POST /api/committee/run body

    v3 升级（2026-05-06）：
    - symbols 空 = 跑 strategy.target_assets 全部（多资产并行）
    - symbols 给了 = 跑指定的（单 / 多资产并行）
    - 与旧 daily_report.run() 区别：默认 max_debate_rounds=4 真讨论，不发邮件
    - cron 自动跑仍走 daily_report.run()（max=1 串行 + 邮件，节省成本）
    """
    note: Optional[str] = Field(None, description="可选备注")
    symbols: Optional[List[str]] = Field(
        default=None,
        description="资产列表；None = strategy.target_assets 全部",
    )
    max_debate_rounds: int = Field(
        default=4, ge=1, le=8,
        description="cross-challenge 上限。1=旧行为；4=真讨论（推荐）",
    )


class CommitteeRunResponse(BaseModel):
    """触发后立即返回"""
    task_id: str
    status: Literal["queued"]
    started_at: str
    poll_url: str  # 前端轮询路径


class CommitteeStatusResponse(BaseModel):
    """GET /api/committee/{task_id} 响应"""
    model_config = ConfigDict(extra="allow")

    task_id: str
    status: Literal["queued", "running", "done", "error"]
    started_at: str
    ended_at: Optional[str] = None
    note: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    # v3 live 模式新增字段（旧 /run 端点不写）
    phase: Optional[str] = None                          # 当前 stage 名（round_1_done / cio_done 等）
    last_event: Optional[Dict[str, Any]] = None
    events: Optional[List[Dict[str, Any]]] = None        # 最近 100 条 progress 事件
    symbol: Optional[str] = None                         # live 模式跑的是哪个资产
    max_debate_rounds: Optional[int] = None


def _committee_status_path(task_id: str) -> Path:
    return COMMITTEE_DIR / task_id / "status.json"


def _committee_meta_path(task_id: str) -> Path:
    """审计 trail：runtime metadata（commit hash / model / temperature / 等）"""
    return COMMITTEE_DIR / task_id / "meta.json"


# 模块级 cache：commit hash 跑一次 git 就够（lru_cache 在 multiprocessing 跨进程
# 不共享，但每个进程跑一次也只是 ~10ms 的 subprocess）
import functools as _functools
import subprocess as _subprocess
import sys as _sys


@_functools.lru_cache(maxsize=1)
def _audit_commit_hash() -> str:
    """git rev-parse --short HEAD —— 失败返回 'unknown' 而不是抛异常"""
    try:
        result = _subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True,
            cwd=str(Path(__file__).parent.parent),
            timeout=2,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:  # noqa: BLE001 git 不存在/不在仓库等都视为 unknown
        pass
    return "unknown"


def _build_audit_meta(
    task_id: str,
    symbols: Optional[List[str]],
    max_rounds: int,
) -> Dict[str, Any]:
    """采集本次 committee 跑的运行环境快照，落审计 trail

    关键字段（合规视角）：
    - commit_hash: 跑这次的代码版本
    - model + temperature: LLM 运行参数（决定 verdict 的可复现性）
    - max_debate_rounds: 辩论轮数上限
    - python_version: 运行时
    - symbols: 这次跑了哪些资产
    """
    return {
        "task_id": task_id,
        "started_at": _now_iso(),
        "commit_hash": _audit_commit_hash(),
        "python_version": _sys.version.split()[0],
        "model": os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
        "model_temperature": float(os.getenv("INVEST_LLM_TEMPERATURE", "0.2")),
        "max_debate_rounds": max_rounds,
        "symbols": symbols if symbols else "(strategy.target_assets all)",
        "executor": "core.committee_runner.run_committee_for_symbol",
        "provider": "deepseek (Web/Cron path)",
    }


# v3 真并行后多线程并发写 status.json（每个 asset 的 progress callback 在线程池里跑）
# 必须按 task_id 加锁，避免 tmp 文件冲突 + JSON 文件被半截写花
import threading as _threading
_status_locks: Dict[str, _threading.Lock] = {}
_status_locks_lock = _threading.Lock()


def _get_status_lock(task_id: str) -> _threading.Lock:
    with _status_locks_lock:
        lk = _status_locks.get(task_id)
        if lk is None:
            lk = _threading.Lock()
            _status_locks[task_id] = lk
        return lk


def _write_committee_status(task_id: str, payload: Dict[str, Any]) -> None:
    """原子写 status.json（按 task_id 加 threading.Lock 防多线程混写）"""
    path = _committee_status_path(task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 用 pid + thread id 区分 tmp 文件避免竞争（即便锁失效也不会冲突）
    import os as _os
    tmp = path.with_suffix(f".json.tmp.{_os.getpid()}.{_threading.get_ident()}")
    with _get_status_lock(task_id):
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)


def _read_committee_status(task_id: str) -> Optional[Dict[str, Any]]:
    path = _committee_status_path(task_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        log.error(f"committee status {task_id} 损坏: {e}")
        return None


async def _run_committee_task(
    task_id: str,
    symbols: Optional[List[str]],
    max_rounds: int,
) -> None:
    """v3 真并行：多资产同时跑，共享 macro_view，progress 实时推 status.json

    流程：
      1. macro_view 跑 1 次（跨资产共享）
      2. ThreadPoolExecutor 并行跑每个 symbol 的 run_committee_for_symbol
         （max_workers 限 4 防 LLM API 限流）
      3. 每个资产内部 Round 1/2 内 Quant + Risk 已并行（committee.py 实现）
    """
    cur = _read_committee_status(task_id) or {}
    cur.update({"status": "running", "running_at": _now_iso(), "events": []})
    _write_committee_status(task_id, cur)

    # 审计 trail：runtime metadata 落 meta.json，永不被 progress 写覆盖
    audit_meta = _build_audit_meta(task_id, symbols, max_rounds)
    try:
        meta_path = _committee_meta_path(task_id)
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(
            json.dumps(audit_meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:  # noqa: BLE001 审计落盘失败不能阻断业务
        log.warning(f"audit meta.json 落盘失败 task_id={task_id}: {e}")

    def on_progress(event: Dict[str, Any]) -> None:
        s = _read_committee_status(task_id) or {}
        events = s.get("events") or []
        events.append({"ts": _now_iso(), **event})
        s["events"] = events[-200:]    # 多资产时 events 量大，限 200
        s["phase"] = event.get("phase")
        s["last_event"] = event
        s["status"] = "running"
        _write_committee_status(task_id, s)

    try:
        from concurrent.futures import ThreadPoolExecutor
        from core.committee_runner import run_committee_for_symbol
        from core.committee import run_macro_view
        from core.portfolio_manager import PortfolioManager
        from utils.exchange_fee import get_macro_data

        # 解析 symbols：空 → strategy 全部
        pm = PortfolioManager()
        if not symbols:
            symbols = [
                str(a.get("symbol")) for a in pm.strategy.get("target_assets", [])
                if a.get("symbol")
            ]
        if not symbols:
            raise RuntimeError("没有可跑的 symbol（strategy.target_assets 空）")

        # 1) 共享 macro_view 跑一次（不进 thread pool 因为只有一个）
        on_progress({"phase": "macro_start", "symbols": symbols})
        try:
            macro_data = get_macro_data()
        except Exception as e:  # noqa: BLE001
            log.warning(f"macro_data 拉取失败: {e}")
            macro_data = {}
        shared_macro = run_macro_view(str(macro_data))
        on_progress({
            "phase": "macro_done",
            "macro_preview": shared_macro[:240],
            "shared": True,
        })

        # 2) 多资产并行跑 committee（每个内部 Round 1/2 已并行）
        loop = asyncio.get_running_loop()

        def _run_one(sym: str) -> Dict[str, Any]:
            return run_committee_for_symbol(
                sym,
                max_debate_rounds=max_rounds,
                progress_callback=on_progress,
                shared_macro_view=shared_macro,
            )

        # 限 4 worker 防 LLM API 限流
        max_workers = min(len(symbols), 4)
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {sym: loop.run_in_executor(pool, _run_one, sym) for sym in symbols}
            results = {sym: await fut for sym, fut in futures.items()}

        # 提取 verdict 摘要（避免序列化整个 CommitteeReport 对象）
        summary = {}
        for sym, res in results.items():
            if isinstance(res, dict):
                summary[sym] = {
                    "verdict": res.get("verdict"),
                    "debate_meta": (
                        {k: v for k, v in (res.get("debate") or {}).items()
                         if k not in {"quant_history", "risk_history"}}
                        if res.get("debate") else None
                    ),
                    "error": res.get("error"),
                }

        s = _read_committee_status(task_id) or {}
        s.update({
            "status": "done",
            "ended_at": _now_iso(),
            "phase": "done",
            "result": {
                "symbols": symbols,
                "max_debate_rounds": max_rounds,
                "by_asset": summary,
            },
        })
        _write_committee_status(task_id, s)
    except Exception as e:  # noqa: BLE001
        log.exception(f"committee task {task_id} 失败")
        err = _read_committee_status(task_id) or {}
        err.update({
            "status": "error",
            "ended_at": _now_iso(),
            "phase": "error",
            "error": f"{type(e).__name__}: {e}",
        })
        _write_committee_status(task_id, err)


@app.post("/api/committee/run", response_model=CommitteeRunResponse, tags=["committee"])
async def committee_run(body: CommitteeRunRequest = Body(default=CommitteeRunRequest())) -> CommitteeRunResponse:
    """v3 真并行委员会触发（统一端点）

    - 不传 symbols → 跑 strategy.target_assets 全部
    - 传单个 symbol → 单资产快速版（旧 run_single 等效）
    - 多资产并行：macro 共享 1 次，每个资产独立线程跑（内部 Round 1/2 也并行）
    - max_debate_rounds 默认 4 真讨论；旧 daily_report cron 走单独路径不受影响
    """
    task_id = uuid.uuid4().hex[:12]
    started_at = _now_iso()
    _write_committee_status(task_id, {
        "task_id": task_id,
        "status": "queued",
        "phase": "queued",
        "started_at": started_at,
        "note": body.note,
        "symbols": body.symbols,
        "max_debate_rounds": body.max_debate_rounds,
        "events": [],
    })

    asyncio.create_task(_run_committee_task(
        task_id,
        symbols=body.symbols,
        max_rounds=body.max_debate_rounds,
    ))

    return CommitteeRunResponse(
        task_id=task_id,
        status="queued",
        started_at=started_at,
        poll_url=f"/api/committee/{task_id}",
    )


@app.get("/api/committee/{task_id}", response_model=CommitteeStatusResponse, tags=["committee"])
async def committee_status(task_id: str) -> CommitteeStatusResponse:
    """查询委员会任务状态（pending → running → done/error）"""
    status = _read_committee_status(task_id)
    if status is None:
        raise HTTPException(status_code=404, detail=f"task_id {task_id} 不存在")
    # 兜底未写入字段，避免 Pydantic validation 失败
    status.setdefault("task_id", task_id)
    status.setdefault("started_at", _now_iso())
    return CommitteeStatusResponse(**status)


@app.get("/api/committee/{task_id}/audit", tags=["committee"])
async def committee_audit_meta(task_id: str) -> Dict[str, Any]:
    """读取审计 trail（commit_hash / model / temperature / max_debate_rounds 等）

    给合规 / 复盘用：监管来查"那天 verdict 是哪个 commit / 哪个 model 跑的"时一查就有。
    Frontmatter 进 memory/.committee/<task_id>/meta.json，永不被 progress 覆盖。
    """
    meta_path = _committee_meta_path(task_id)
    if not meta_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"audit meta for task_id {task_id} not found（旧 task 没写过 meta.json）",
        )
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"meta.json 损坏: {e}") from e


@app.get("/api/committee/live/{task_id}", tags=["committee"])
async def committee_live(task_id: str) -> StreamingResponse:
    """SSE 流式推送 task 状态变化（前端 EventSource 订阅）

    用法（前端）：
        const es = new EventSource(`/api/committee/live/${taskId}`);
        es.addEventListener('progress', (e) => { ... });
        es.addEventListener('done', () => es.close());

    每 2s 重读 status.json 如有变化推送 progress event；
    每 25s 推送 `: keepalive` 注释防 CF Access 5 min idle 超时
    """
    async def event_stream():
        last_status_str: Optional[str] = None
        max_iterations = 600   # 600 * 2s = 20 min 上限
        keepalive_every = 12   # 每 12 次循环（24s）发一次心跳
        loop_count = 0

        while loop_count < max_iterations:
            status = _read_committee_status(task_id)
            if status is None:
                yield f"event: not_found\ndata: {json.dumps({'task_id': task_id})}\n\n"
                return

            current = json.dumps(status, ensure_ascii=False, default=str)
            if current != last_status_str:
                last_status_str = current
                yield f"event: progress\ndata: {current}\n\n"

            phase = status.get("status")
            if phase == "done":
                yield f"event: done\ndata: {current}\n\n"
                return
            if phase == "error":
                yield f"event: error\ndata: {current}\n\n"
                return

            loop_count += 1
            if loop_count % keepalive_every == 0:
                yield ": keepalive\n\n"   # SSE 注释行，防 CF idle timeout
            await asyncio.sleep(2)

        yield f"event: timeout\ndata: {json.dumps({'task_id': task_id, 'reason': 'exceeded_20min'})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",   # 关 nginx/Caddy 缓冲
            "Connection": "keep-alive",
        },
    )


# ============================================================
# 策略写操作 — 颗粒度从大到小
# ============================================================
# 设计：
# - 所有写都走 store.transaction("strategy") 单锁 RMW（commit-on-success 语义保证 schema fail 不写半截）
# - 写入 metadata 后，提交前显式跑 StrategyData.model_validate；失败抛 HTTPException(400)，
#   transaction 异常退出自动 rollback，已改的 frontmatter 不会落盘
# - body（人类写的策略说明）不动——schema 只管结构化字段，不动 markdown 文本


def _validate_strategy_or_400(metadata: Dict[str, Any]) -> None:
    """提交前 schema 校验，失败转 HTTP 400 并暴露 validation 详情给前端"""
    try:
        validate_strategy(metadata)
    except Exception as e:  # ValidationError 或派生
        raise HTTPException(status_code=400, detail=f"strategy schema validation failed: {e}")


# ===== 请求模型 =====

class AllocationsRequest(BaseModel):
    """PUT /api/strategy/allocations body"""
    target_allocation_stock: float = Field(..., ge=0, le=1)
    target_allocation_cash: float = Field(..., ge=0, le=1)


class TargetAssetCreate(BaseModel):
    """POST /api/strategy/asset body"""
    symbol: str = Field(..., min_length=1)
    display_name: Optional[str] = None
    channel: Optional[str] = None
    max_single_invest_cny: float = Field(..., ge=0, le=1_000_000)
    price_offset_pct: Optional[float] = Field(None, ge=-0.1, le=0.1)
    sell_fee_pct: Optional[float] = Field(None, ge=0, le=0.05)
    # 允许扩展字段（旧 md 用 currency/market/note 等，前端可能想顺便改）
    extra: Optional[Dict[str, Any]] = None


class TargetAssetPatch(BaseModel):
    """PUT /api/strategy/asset/{symbol} body — 全字段可选，仅更新提供的字段"""
    display_name: Optional[str] = None
    channel: Optional[str] = None
    max_single_invest_cny: Optional[float] = Field(None, ge=0, le=1_000_000)
    price_offset_pct: Optional[float] = Field(None, ge=-0.1, le=0.1)
    sell_fee_pct: Optional[float] = Field(None, ge=0, le=0.05)


class StrategyWriteResponse(BaseModel):
    """策略写操作统一响应"""
    ok: bool = True
    target_allocation_stock: float
    target_allocation_cash: float
    target_assets: List[Dict[str, Any]]
    message: str


def _strategy_response(metadata: Dict[str, Any], message: str) -> StrategyWriteResponse:
    return StrategyWriteResponse(
        target_allocation_stock=float(metadata.get("target_allocation_stock", 0)),
        target_allocation_cash=float(metadata.get("target_allocation_cash", 0)),
        target_assets=list(metadata.get("target_assets", [])),
        message=message,
    )


# ===== PUT /api/strategy/allocations =====

@app.put("/api/strategy/allocations", response_model=StrategyWriteResponse, tags=["strategy_write"])
async def put_allocations(body: AllocationsRequest = Body(...)) -> StrategyWriteResponse:
    """改资产配置目标（stock/cash 比例）。两者之和必须 ≈ 1（schema 强约束）"""
    store = MemoryStore()
    with store.transaction("strategy") as tx:
        tx["target_allocation_stock"] = body.target_allocation_stock
        tx["target_allocation_cash"] = body.target_allocation_cash
        _validate_strategy_or_400(dict(tx.metadata))
        final_meta = dict(tx.metadata)
    return _strategy_response(
        final_meta,
        f"目标配置已更新: 股 {body.target_allocation_stock:.0%} / 现 {body.target_allocation_cash:.0%}",
    )


# ===== POST /api/strategy/asset =====

@app.post("/api/strategy/asset", response_model=StrategyWriteResponse, tags=["strategy_write"])
async def add_target_asset(body: TargetAssetCreate = Body(...)) -> StrategyWriteResponse:
    """新增 target_asset。symbol 不能与现有重复"""
    store = MemoryStore()
    new_asset: Dict[str, Any] = body.model_dump(exclude_none=True, exclude={"extra"})
    if body.extra:
        new_asset.update(body.extra)

    with store.transaction("strategy") as tx:
        existing: List[Dict[str, Any]] = list(tx.get("target_assets", []) or [])
        if any(a.get("symbol") == body.symbol for a in existing):
            raise HTTPException(
                status_code=409,
                detail=f"symbol {body.symbol} 已存在，请用 PUT 更新或先 DELETE",
            )
        existing.append(new_asset)
        tx["target_assets"] = existing
        _validate_strategy_or_400(dict(tx.metadata))
        final_meta = dict(tx.metadata)
    return _strategy_response(final_meta, f"已新增资产 {body.symbol}")


# ===== PUT /api/strategy/asset/{symbol} =====

@app.put(
    "/api/strategy/asset/{symbol}",
    response_model=StrategyWriteResponse,
    tags=["strategy_write"],
)
async def update_target_asset(
    symbol: str,
    body: TargetAssetPatch = Body(...),
) -> StrategyWriteResponse:
    """更新单个 target_asset 的部分字段（仅传非空字段会被改写）"""
    patch = body.model_dump(exclude_none=True)
    if not patch:
        raise HTTPException(status_code=400, detail="patch 必须至少含一个字段")

    store = MemoryStore()
    with store.transaction("strategy") as tx:
        existing: List[Dict[str, Any]] = list(tx.get("target_assets", []) or [])
        target = next((a for a in existing if a.get("symbol") == symbol), None)
        if target is None:
            raise HTTPException(status_code=404, detail=f"symbol {symbol} 不存在")
        target.update(patch)
        tx["target_assets"] = existing
        _validate_strategy_or_400(dict(tx.metadata))
        final_meta = dict(tx.metadata)
    return _strategy_response(final_meta, f"{symbol} 已更新: {list(patch.keys())}")


# ===== DELETE /api/strategy/asset/{symbol} =====

@app.delete(
    "/api/strategy/asset/{symbol}",
    response_model=StrategyWriteResponse,
    tags=["strategy_write"],
)
async def delete_target_asset(symbol: str) -> StrategyWriteResponse:
    """删除 target_asset。schema 要求至少剩 1 个，否则 400"""
    store = MemoryStore()
    with store.transaction("strategy") as tx:
        existing: List[Dict[str, Any]] = list(tx.get("target_assets", []) or [])
        if not any(a.get("symbol") == symbol for a in existing):
            raise HTTPException(status_code=404, detail=f"symbol {symbol} 不存在")
        new_list = [a for a in existing if a.get("symbol") != symbol]
        tx["target_assets"] = new_list
        _validate_strategy_or_400(dict(tx.metadata))   # schema 保证至少 1 个
        final_meta = dict(tx.metadata)
    return _strategy_response(final_meta, f"已删除 {symbol}")


# ============================================================
# v2 通用 cash CRUD（任意币种） — 放在文件末尾因为依赖 WriteResponse
# ============================================================

class CashWriteRequest(BaseModel):
    """POST /api/cash/{currency}/deposit | withdraw body"""
    amount: float = Field(..., gt=0)


@app.post("/api/cash/{currency}/deposit", response_model=WriteResponse, tags=["cash_write"])
async def cash_deposit(currency: str, body: CashWriteRequest = Body(...)) -> WriteResponse:
    """v2 通用任意币种存款"""
    ccy = currency.upper()
    if not (3 <= len(ccy) <= 5) or not ccy.isalpha():
        raise HTTPException(status_code=400, detail=f"非法币种 {currency}")

    pm = _new_pm()
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


# ============================================================
# 系统 / 原理可视化端点（让 GUI 能看到所有静默 cron 的内部状态）
# ============================================================

class JobStatus(BaseModel):
    name: str
    description: str
    schedule: str
    timezone: str
    enabled: bool
    next_run_time: Optional[str] = None


class JobsStatusResponse(BaseModel):
    jobs: List[JobStatus]


class InsightItem(BaseModel):
    slug: str
    metadata: Dict[str, Any]
    body: str


class InsightsResponse(BaseModel):
    count: int
    items: List[InsightItem]


class RegimeResponse(BaseModel):
    symbol: str
    regime: str
    reason: str
    inputs: Dict[str, Any]
    strategy_hint: str
    brief: str


class DreamEvent(BaseModel):
    model_config = ConfigDict(extra="allow")

    ts: str
    phase: str


class DreamsStateResponse(BaseModel):
    short_term: Optional[Dict[str, Any]] = None
    candidates: Optional[Dict[str, Any]] = None
    recent_events: List[DreamEvent]


class PnLHistoryPoint(BaseModel):
    model_config = ConfigDict(extra="allow")

    ts: str
    total_pnl_pct: Optional[float] = None


class PnLHistoryResponse(BaseModel):
    count: int
    points: List[PnLHistoryPoint]


class CommitteeSessionSummary(BaseModel):
    date: str
    symbol: str
    verdict: Optional[str] = None
    confidence: Optional[float] = None
    dominant_view: Optional[str] = None
    suggested_alloc_cny: Optional[float] = None
    file_path: str


class CommitteeSessionsResponse(BaseModel):
    count: int
    sessions: List[CommitteeSessionSummary]


class CommitteeSessionDetail(BaseModel):
    date: str
    symbol: str
    content: str


@app.get("/api/jobs/status", response_model=JobsStatusResponse, tags=["system"])
async def get_jobs_status() -> JobsStatusResponse:
    """所有 cron job 的配置 + APScheduler 下次触发时间。让 GUI 能看到"什么在静默跑"""
    import yaml
    jobs_dir = Path(__file__).parent.parent / "jobs"
    items: List[JobStatus] = []
    for yml in sorted(jobs_dir.glob("*.yml")):
        try:
            cfg = yaml.safe_load(yml.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            log.warning(f"读 {yml} 失败: {e}")
            continue
        if not cfg or not isinstance(cfg, dict):
            continue
        items.append(JobStatus(
            name=cfg.get("name", yml.stem),
            description=cfg.get("description", ""),
            schedule=cfg.get("schedule", ""),
            timezone=cfg.get("timezone", ""),
            enabled=bool(cfg.get("enabled", False)),
            next_run_time=(
                _next_run_from_cron(cfg.get("schedule", ""), cfg.get("timezone", "Asia/Shanghai"))
                if cfg.get("enabled") else None
            ),
        ))
    return JobsStatusResponse(jobs=items)


def _next_run_from_cron(schedule: str, tz: str = "Asia/Shanghai") -> Optional[str]:
    """根据 cron 表达式算下一次触发时间（不依赖 APScheduler db，因为 scheduler 用 MemoryJobStore）"""
    if not schedule:
        return None
    try:
        from croniter import croniter
        from datetime import datetime as _dt
        try:
            from zoneinfo import ZoneInfo
            now = _dt.now(ZoneInfo(tz))
        except Exception:
            now = _dt.now().astimezone()
        return croniter(schedule, now).get_next(_dt).isoformat(timespec="seconds")
    except Exception as e:  # noqa: BLE001
        log.debug(f"croniter 算下一次失败 {schedule}: {e}")
        return None


@app.get("/api/insights", response_model=InsightsResponse, tags=["system"])
async def get_insights() -> InsightsResponse:
    """Dreaming 整合出的长期模式（memory/insights/*.md）"""
    store = MemoryStore()
    insights_dir = store.root / "insights"
    items: List[InsightItem] = []
    if insights_dir.exists():
        for md_file in sorted(insights_dir.glob("*.md")):
            doc = store.read(f"insights/{md_file.stem}")
            if not doc:
                continue
            meta_clean = {
                k: v for k, v in doc.metadata.items()
                if k not in {"name", "type", "updated"}
            }
            items.append(InsightItem(
                slug=md_file.stem,
                metadata=meta_clean,
                body=doc.body,
            ))
    return InsightsResponse(count=len(items), items=items)


@app.get("/api/regime/{symbol:path}", response_model=RegimeResponse, tags=["system"])
async def get_regime(symbol: str) -> RegimeResponse:
    """实时算指定 symbol 的市场 regime（牛/熊/震荡）+ 给 LLM 看的 brief"""
    from core.regime import classify_regime, regime_strategy_hint, format_regime_brief
    from utils.market_metrics import compute_metrics

    try:
        df = get_history_data(symbol, "2y")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, f"行情拉取失败: {e}")
    if df is None or df.empty:
        raise HTTPException(404, f"无 {symbol} 行情数据")

    metrics = compute_metrics(df)
    info = classify_regime(metrics)
    regime_label = info.get("regime", "unknown")
    reason = info.get("reason", "")
    hint = regime_strategy_hint(regime_label, metrics.get("price_quantile_2y"))
    brief = format_regime_brief(metrics)

    # 把 numpy / pandas 类型转成 JSON-safe
    inputs_safe = {}
    for k, v in metrics.items():
        try:
            if v is None:
                inputs_safe[k] = None
            elif hasattr(v, "item"):
                inputs_safe[k] = v.item()
            else:
                inputs_safe[k] = v
        except Exception:
            inputs_safe[k] = str(v)

    return RegimeResponse(
        symbol=symbol,
        regime=regime_label,
        reason=reason,
        inputs=inputs_safe,
        strategy_hint=hint,
        brief=brief,
    )


@app.get("/api/dreams/state", response_model=DreamsStateResponse, tags=["system"])
async def get_dreams_state(
    event_limit: int = Query(20, ge=1, le=200),
) -> DreamsStateResponse:
    """Dreaming 子系统当前状态：短期记忆 + 候选池 + 最近 events"""
    store = MemoryStore()
    short_term = store.read_dream_state("short-term-recall")
    candidates = store.read_dream_state("candidates")

    events: List[DreamEvent] = []
    events_path = store.root / ".dreams" / "events.jsonl"
    if events_path.exists():
        try:
            with open(events_path, encoding="utf-8") as f:
                lines = f.readlines()
            # 倒序取最近 N 条
            for line in reversed(lines[-event_limit:]):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    events.append(DreamEvent(**obj))
                except Exception:  # noqa: BLE001
                    continue
        except Exception as e:  # noqa: BLE001
            log.warning(f"读 dreams events 失败: {e}")

    return DreamsStateResponse(
        short_term=short_term,
        candidates=candidates,
        recent_events=events,
    )


@app.get("/api/pnl_history", response_model=PnLHistoryResponse, tags=["system"])
async def get_pnl_history(
    since: int = Query(60, ge=1, le=2000, description="返回最近 N 条快照"),
) -> PnLHistoryResponse:
    """原始 PnL 历史数据点（jobs/pnl_snapshot 工作日每 2h 写一条）"""
    store = MemoryStore()
    path = store.root / ".state" / "pnl_history.jsonl"
    points: List[PnLHistoryPoint] = []
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.readlines()
            for line in lines[-since:]:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    points.append(PnLHistoryPoint(**obj))
                except Exception:
                    continue
        except Exception as e:  # noqa: BLE001
            log.warning(f"读 pnl_history 失败: {e}")
    return PnLHistoryResponse(count=len(points), points=points)


@app.get("/api/committee_sessions", response_model=CommitteeSessionsResponse, tags=["system"])
async def list_committee_sessions(
    limit: int = Query(50, ge=1, le=500),
) -> CommitteeSessionsResponse:
    """历史委员会决议列表（memory/.committee/<date>/<symbol>.md），按时间倒序"""
    store = MemoryStore()
    base = store.root / ".committee"
    sessions: List[CommitteeSessionSummary] = []
    if not base.exists():
        return CommitteeSessionsResponse(count=0, sessions=[])
    # 日期目录倒序
    for date_dir in sorted(base.iterdir(), reverse=True):
        if not date_dir.is_dir():
            continue
        for md in sorted(date_dir.glob("*.md")):
            try:
                content = md.read_text(encoding="utf-8")
            except Exception:
                continue
            verdict, confidence, dominant, alloc = _parse_committee_header(content)
            sessions.append(CommitteeSessionSummary(
                date=date_dir.name,
                symbol=md.stem,
                verdict=verdict,
                confidence=confidence,
                dominant_view=dominant,
                suggested_alloc_cny=alloc,
                file_path=str(md.relative_to(store.root.parent)),
            ))
            if len(sessions) >= limit:
                return CommitteeSessionsResponse(count=len(sessions), sessions=sessions)
    return CommitteeSessionsResponse(count=len(sessions), sessions=sessions)


def _parse_committee_header(content: str) -> tuple:
    """从 committee md 头部提取 verdict / confidence / dominant / alloc（regex 解析）"""
    import re as _re
    verdict = None
    confidence = None
    dominant = None
    alloc = None
    m = _re.search(r"\*\*Verdict\*\*:\s*(\w+)\s*\(confidence\s*([\d.]+)\)", content)
    if m:
        verdict = m.group(1)
        try:
            confidence = float(m.group(2))
        except ValueError:
            pass
    m2 = _re.search(r"\*\*Dominant view\*\*:\s*(\w+)", content)
    if m2:
        dominant = m2.group(1)
    m3 = _re.search(r"\*\*Suggested allocation CNY\*\*:\s*(-?[\d.]+)", content)
    if m3:
        try:
            alloc = float(m3.group(1))
        except ValueError:
            pass
    return verdict, confidence, dominant, alloc


# ============ LLM telemetry 端点（v3 透明化 D1）============

class LlmUsageRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    ts: str
    agent_role: str
    asset: Optional[str] = None
    round: Optional[str] = None
    provider: str = "deepseek"
    model: str = "deepseek-chat"
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0
    cost_cny: float = 0.0
    tool_calls: int = 0
    iteration: int = 0
    ok: bool = True
    error: Optional[str] = None


class LlmUsageResponse(BaseModel):
    count: int
    records: List[LlmUsageRecord]


class LlmRoleStats(BaseModel):
    calls: int
    input_tokens: int
    output_tokens: int
    cost_cny: float
    avg_latency_ms: int


class LlmSummaryResponse(BaseModel):
    total_calls: int
    total_input_tokens: int
    total_output_tokens: int
    total_cost_cny: float
    by_role: Dict[str, LlmRoleStats]


@app.get("/api/llm/usage", response_model=LlmUsageResponse, tags=["system"])
async def get_llm_usage(
    since: int = Query(200, ge=1, le=5000),
) -> LlmUsageResponse:
    """每次 LLM 调用的明细（input/output tokens、延迟、成本、tool 调用数）"""
    from core.llm_telemetry import read_telemetry
    records = read_telemetry(since=since)
    # 倒序：最新在前
    records_sorted = list(reversed(records))
    return LlmUsageResponse(
        count=len(records_sorted),
        records=[LlmUsageRecord(**r) for r in records_sorted],
    )


@app.get("/api/llm/summary", response_model=LlmSummaryResponse, tags=["system"])
async def get_llm_summary(
    since_records: int = Query(1000, ge=1, le=10_000),
) -> LlmSummaryResponse:
    """LLM 用量汇总：总调用 / 总 token / 总成本，按 agent_role 拆分"""
    from core.llm_telemetry import telemetry_summary
    s = telemetry_summary(since_records=since_records)
    return LlmSummaryResponse(
        total_calls=s["total_calls"],
        total_input_tokens=s["total_input_tokens"],
        total_output_tokens=s["total_output_tokens"],
        total_cost_cny=s["total_cost_cny"],
        by_role={k: LlmRoleStats(**v) for k, v in s["by_role"].items()},
    )


# ============ 数据源健康 + 基准对标（v3 透明化 C11/C8）============

class DataSourceHealth(BaseModel):
    name: str
    description: str
    last_success_at: Optional[str] = None
    is_stale: bool = False
    sample_value: Optional[Any] = None
    error: Optional[str] = None


class DataSourcesHealthResponse(BaseModel):
    sources: List[DataSourceHealth]


@app.get("/api/data_sources/health", response_model=DataSourcesHealthResponse, tags=["system"])
async def get_data_sources_health() -> DataSourcesHealthResponse:
    """所有数据源的当前可达性 + 最后成功拉取时间。GUI 透明化"我们用什么数据决策"

    B5 通用化（2026-05）：监控 symbol 不再硬编码作者持仓（NDQ.AX/GC=F），
    动态读用户实际 holdings；额外保留宏观指标（VIX/TNX/USDCNY 等）作背景。
    """
    sources: List[DataSourceHealth] = []

    # 用户实际持仓 + 通用宏观背景指标
    pm = _new_pm()
    user_symbols: List[Tuple[str, str]] = []
    for h in pm.holdings:
        sym = str(h.get("symbol") or "")
        if not sym:
            continue
        # GC=F 走 gold_cny_per_gram 反推链路，单独检查（下方）
        if h.get("proxy_kind") == "gold_cny_per_gram":
            continue
        display = str(h.get("display_name") or sym)
        ccy = str(h.get("cost_currency") or "")
        user_symbols.append((sym, f"{display} ({ccy})" if ccy else display))

    # 通用宏观背景：所有用户都关心（不因 fork 而异）
    macro_symbols = [
        ("USDCNY=X", "美元兑人民币汇率"),
        ("AUDCNY=X", "澳元兑人民币汇率"),
        ("^VIX", "波动率指数 VIX"),
        ("^TNX", "10 年美债收益率 TNX"),
    ]

    yf_symbols = user_symbols + macro_symbols
    for symbol, desc in yf_symbols:
        try:
            df = get_history_data(symbol, "5d")
            if df is None or df.empty:
                sources.append(DataSourceHealth(
                    name=f"yfinance:{symbol}",
                    description=desc,
                    is_stale=True,
                    error="empty dataframe",
                ))
                continue
            last_ts = df.index[-1].strftime("%Y-%m-%d")
            last_close = float(df["Close"].iloc[-1])
            # 简单判定：最近一条数据 > 5 天 → stale
            from datetime import datetime as _dt
            try:
                last_dt = _dt.strptime(last_ts, "%Y-%m-%d")
                age_days = (_dt.now() - last_dt).days
                is_stale = age_days > 5
            except Exception:
                is_stale = False
            sources.append(DataSourceHealth(
                name=f"yfinance:{symbol}",
                description=desc,
                last_success_at=last_ts,
                is_stale=is_stale,
                sample_value=round(last_close, 4),
            ))
        except Exception as e:  # noqa: BLE001
            sources.append(DataSourceHealth(
                name=f"yfinance:{symbol}",
                description=desc,
                is_stale=True,
                error=f"{type(e).__name__}: {e}",
            ))

    # 7: 黄金克价反推链路
    try:
        snap = get_gold_snapshot(offset_pct=0.0)
        if snap is None:
            sources.append(DataSourceHealth(
                name="gold_cny_per_gram",
                description="GC=F + USDCNY 反推 CNY/克 + DB 兜底",
                is_stale=True,
                error="snapshot returned None",
            ))
        else:
            sources.append(DataSourceHealth(
                name="gold_cny_per_gram",
                description="GC=F + USDCNY 反推 CNY/克 + DB 兜底",
                is_stale=snap.is_stale,
                sample_value=round(snap.spot_cny_per_gram, 2),
                last_success_at=_now_iso() if not snap.is_stale else None,
            ))
    except Exception as e:  # noqa: BLE001
        sources.append(DataSourceHealth(
            name="gold_cny_per_gram",
            description="GC=F + USDCNY 反推 CNY/克 + DB 兜底",
            is_stale=True,
            error=f"{type(e).__name__}: {e}",
        ))

    # 8: CommSec 邮件 (processed_emails state)
    store = MemoryStore()
    processed_emails = store.state_get("processed_emails", [])
    sources.append(DataSourceHealth(
        name="commsec_imap",
        description="CommSec 邮件 IMAP（每 2h 拉，180 天回溯去重）",
        last_success_at=None,   # state 文件 mtime 是更准的代理
        is_stale=False,
        sample_value=f"{len(processed_emails)} emails 已处理",
    ))

    # 9: PnL history jsonl
    pnl_path = store.root / ".state" / "pnl_history.jsonl"
    if pnl_path.exists():
        from datetime import datetime as _dt
        mtime = _dt.fromtimestamp(pnl_path.stat().st_mtime).astimezone()
        line_count = 0
        try:
            with open(pnl_path, encoding="utf-8") as f:
                line_count = sum(1 for line in f if line.strip())
        except Exception:
            pass
        sources.append(DataSourceHealth(
            name="pnl_history_jsonl",
            description="PnL 快照（jobs/pnl_snapshot 每 2h 工作日写）",
            last_success_at=mtime.isoformat(timespec="seconds"),
            is_stale=False,
            sample_value=f"{line_count} 条快照",
        ))
    else:
        sources.append(DataSourceHealth(
            name="pnl_history_jsonl",
            description="PnL 快照",
            is_stale=True,
            error="文件不存在",
        ))

    # 10: market DB
    db_path = Path(__file__).parent.parent / "db" / "market_data.db"
    if db_path.exists():
        from datetime import datetime as _dt
        mtime = _dt.fromtimestamp(db_path.stat().st_mtime).astimezone()
        sources.append(DataSourceHealth(
            name="market_db_sqlite",
            description="行情 DB 兜底（yfinance 挂时回落到这里）",
            last_success_at=mtime.isoformat(timespec="seconds"),
            is_stale=False,
            sample_value=f"{db_path.stat().st_size // 1024} KB",
        ))
    else:
        sources.append(DataSourceHealth(
            name="market_db_sqlite",
            description="行情 DB 兜底",
            is_stale=True,
            error="db 文件不存在",
        ))

    return DataSourcesHealthResponse(sources=sources)


# ============ Verdict Review 端点（v3 透明化 B4 — marketing 核心信任）============

class VerdictReviewItem(BaseModel):
    """单条 verdict review 记录（实际 schema 字段类型多样，宽松接受）"""
    model_config = ConfigDict(extra="allow")

    date: str
    asset: Optional[str] = None
    verdict: Optional[str] = None
    confidence: Optional[float] = None
    expected_direction: Optional[str] = None
    # 实际 macro_shock 是 dict {detected, drivers}，不是 bool
    macro_shock: Optional[Dict[str, Any]] = None
    source: Optional[str] = None
    actual_returns: Optional[Dict[str, float]] = None
    hits: Optional[Dict[str, bool]] = None


class VerdictReviewDataResponse(BaseModel):
    count: int
    items: List[VerdictReviewItem]


class VerdictReviewSummary(BaseModel):
    """命中率汇总"""
    total: int
    by_window: Dict[str, Dict[str, Any]]      # { "1d": {n, hit_rate}, "7d": ..., "30d": ... }
    by_verdict: Dict[str, Dict[str, Any]]     # { "BUY": {n, avg_conf, 1d_hit, ...}, ... }
    directional_only_hit_rate: Optional[float] = None  # 剔除 HOLD 后真实 alpha
    has_report_md: bool


class VerdictReviewReportResponse(BaseModel):
    """docs/verdict_accuracy.md 完整内容"""
    exists: bool
    generated_at: Optional[str] = None
    content: Optional[str] = None


@app.get("/api/verdict_review/data", response_model=VerdictReviewDataResponse, tags=["system"])
async def get_verdict_review_data(
    since: int = Query(200, ge=1, le=5000),
) -> VerdictReviewDataResponse:
    """读 memory/.dreams/verdict_review.jsonl 原始数据（每条决议事后是否命中）"""
    store = MemoryStore()
    path = store.root / ".dreams" / "verdict_review.jsonl"
    items: List[VerdictReviewItem] = []
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.readlines()
            for line in lines[-since:]:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    items.append(VerdictReviewItem(**obj))
                except Exception:  # noqa: BLE001
                    continue
        except Exception as e:  # noqa: BLE001
            log.warning(f"读 verdict_review jsonl 失败: {e}")
    items_sorted = list(reversed(items))   # 最新在前
    return VerdictReviewDataResponse(count=len(items_sorted), items=items_sorted)


@app.get("/api/verdict_review/summary", response_model=VerdictReviewSummary, tags=["system"])
async def get_verdict_review_summary() -> VerdictReviewSummary:
    """命中率汇总（按时间窗口 + 按 verdict 类型）。GUI marketing 主战场"""
    store = MemoryStore()
    path = store.root / ".dreams" / "verdict_review.jsonl"
    report_path = Path(__file__).parent.parent / "docs" / "verdict_accuracy.md"

    if not path.exists():
        return VerdictReviewSummary(
            total=0, by_window={}, by_verdict={},
            has_report_md=report_path.exists(),
        )

    items = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    items.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception as e:  # noqa: BLE001
        log.warning(f"summary 读 verdict_review 失败: {e}")
        return VerdictReviewSummary(
            total=0, by_window={}, by_verdict={},
            has_report_md=report_path.exists(),
        )

    total = len(items)

    # 按时间窗口聚合 hit_rate
    by_window: Dict[str, Dict[str, Any]] = {}
    for w in ("1d", "7d", "30d"):
        n = 0
        hits = 0
        for it in items:
            h = (it.get("hits") or {}).get(w)
            if h is None:
                continue
            n += 1
            if h:
                hits += 1
        if n > 0:
            by_window[w] = {
                "n": n,
                "hit_rate": round(hits / n, 4),
            }

    # 按 verdict 类型聚合
    by_verdict: Dict[str, Dict[str, Any]] = {}
    for it in items:
        v = (it.get("verdict") or "UNKNOWN").upper()
        d = by_verdict.setdefault(v, {
            "n": 0,
            "conf_sum": 0.0,
            "hits_1d": 0, "hits_7d": 0, "hits_30d": 0,
            "n_1d": 0, "n_7d": 0, "n_30d": 0,
        })
        d["n"] += 1
        d["conf_sum"] += float(it.get("confidence") or 0)
        for w in ("1d", "7d", "30d"):
            h = (it.get("hits") or {}).get(w)
            if h is not None:
                d[f"n_{w}"] += 1
                if h:
                    d[f"hits_{w}"] += 1
    # 整理输出
    by_verdict_clean: Dict[str, Dict[str, Any]] = {}
    for v, d in by_verdict.items():
        by_verdict_clean[v] = {
            "n": d["n"],
            "avg_confidence": round(d["conf_sum"] / d["n"], 3) if d["n"] else 0,
            "hit_rate_1d": round(d["hits_1d"] / d["n_1d"], 4) if d["n_1d"] else None,
            "hit_rate_7d": round(d["hits_7d"] / d["n_7d"], 4) if d["n_7d"] else None,
            "hit_rate_30d": round(d["hits_30d"] / d["n_30d"], 4) if d["n_30d"] else None,
        }

    # 剔除 HOLD 后的真实方向性 hit rate（report 里特别强调的指标）
    directional_total = sum(1 for it in items if (it.get("verdict") or "").upper() != "HOLD")
    directional_hits = sum(
        1 for it in items
        if (it.get("verdict") or "").upper() != "HOLD"
        and (it.get("hits") or {}).get("7d") is True
    )
    directional_only = (
        round(directional_hits / directional_total, 4) if directional_total else None
    )

    return VerdictReviewSummary(
        total=total,
        by_window=by_window,
        by_verdict=by_verdict_clean,
        directional_only_hit_rate=directional_only,
        has_report_md=report_path.exists(),
    )


@app.get("/api/verdict_review/report", response_model=VerdictReviewReportResponse, tags=["system"])
async def get_verdict_review_report() -> VerdictReviewReportResponse:
    """完整 docs/verdict_accuracy.md markdown 报告"""
    report_path = Path(__file__).parent.parent / "docs" / "verdict_accuracy.md"
    if not report_path.exists():
        return VerdictReviewReportResponse(exists=False)
    try:
        content = report_path.read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        return VerdictReviewReportResponse(exists=False, content=f"读取失败: {e}")

    # 从 markdown 头部提取生成时间
    import re as _re
    m = _re.search(r"\*Generated:\s*([^*]+)\*", content)
    return VerdictReviewReportResponse(
        exists=True,
        generated_at=m.group(1).strip() if m else None,
        content=content,
    )


# ============ Tool Calls Audit 端点（v3 透明化 A8/D2）============

class ToolCallRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    ts: str
    agent_role: str
    asset: Optional[str] = None
    round: Optional[str] = None
    tool_name: str
    arguments: Dict[str, Any] = Field(default_factory=dict)
    result_preview: str = ""
    latency_ms: int = 0
    iteration: int = 0


class ToolCallsResponse(BaseModel):
    count: int
    records: List[ToolCallRecord]


# ============ Regime 规则 + 4 角色 prompt 暴露（v3 透明化 B6/A9）============

class AgentPromptInfo(BaseModel):
    """单个 agent 角色的 prompt 配置"""
    role: str
    label: str                    # 中文显示名
    description: str              # 一句话职责
    prompt_opening: Optional[str] = None     # Round 1 prompt（quant / risk）
    prompt_rebuttal: Optional[str] = None    # Round 2 prompt（quant / risk）
    prompt_full: Optional[str] = None        # macro / cio 单 prompt
    temperature: float = 0.2
    enable_tools: bool = True
    notes: List[str] = Field(default_factory=list)


class RegimeRulesResponse(BaseModel):
    """Regime 判定硬规则 + 4 角色 prompt 全集（marketing 主战场）"""
    regime_thresholds: Dict[str, float]
    regime_types: List[str]
    regime_priority: List[str]
    verdict_options: List[str]
    sanity_checks: List[str]
    agents: List[AgentPromptInfo]
    tools: List[Dict[str, Any]]   # 5 个可调 tool


@app.get("/api/regime_rules", response_model=RegimeRulesResponse, tags=["system"])
async def get_regime_rules() -> RegimeRulesResponse:
    """暴露 invest 项目所有「硬规则」+「LLM 提示词」给 GUI marketing 页

    包含：
    - core/regime.py 阈值表
    - 4 个 agent 角色的 system prompt 全文
    - CIO sanity check 清单
    - 5 个可被 LLM 调用的 tool
    """
    from core.regime import THRESHOLDS
    from agents.macro_strategist import PROMPT_MACRO_STRATEGIST
    from agents.quant import build_quant_prompt
    from agents.risk_officer import build_risk_officer_prompt
    from agents.cio import build_cio_prompt
    from agents.tools import TOOL_DEFINITIONS

    sample_asset = {
        "symbol": "<SYMBOL>",
        "display_name": "<asset name>",
    }

    agents_info = [
        AgentPromptInfo(
            role="macro",
            label="宏观分析师 (Macro Strategist)",
            description="跨资产共享，每次 daily_report 跑一次。评估全球利率/通胀/地缘风险。",
            prompt_full=PROMPT_MACRO_STRATEGIST,
            temperature=0.2,
            enable_tools=True,
            notes=[
                "强制输出: SIGNAL (risk_on/risk_off/neutral) + STRENGTH 0-10 + SCORE -5~+5",
                "Hard rule: SCORE<-2 → risk_off / SCORE>2 → risk_on",
            ],
        ),
        AgentPromptInfo(
            role="quant",
            label="量化分析师 (Quant Analyst)",
            description="技术信号（RSI/MA/分位数），受 REGIME 硬约束保护",
            prompt_opening=build_quant_prompt(sample_asset, "opening"),
            prompt_rebuttal=build_quant_prompt(sample_asset, "rebuttal"),
            temperature=0.2,
            enable_tools=False,
            notes=[
                "REGIME=uptrend → 禁 bearish",
                "REGIME=downtrend → 禁 bullish",
                "REGIME=range_bound + price_quantile≤20% → 偏 bullish（底部逢低）",
                "REGIME=range_bound + price_quantile≥80% → 偏 bearish（顶部减仓）",
                "REGIME=crash → 强制 neutral（任何方向都不可执行）",
            ],
        ),
        AgentPromptInfo(
            role="risk",
            label="风险官 (Risk Officer)",
            description="集中度 / dry_powder / 压力测试",
            prompt_opening=build_risk_officer_prompt(sample_asset, "opening"),
            prompt_rebuttal=build_risk_officer_prompt(sample_asset, "rebuttal"),
            temperature=0.2,
            enable_tools=False,
            notes=[
                "Hard rule: 集中度 > 60% → 至少 concerned",
                "Hard rule: dry_powder < 1000 CNY → concerned（无加仓能力）",
                "Hard rule: 7 天内多次买同资产 → high_risk（情绪化追涨）",
            ],
        ),
        AgentPromptInfo(
            role="cio",
            label="CIO 决策者",
            description="综合 Macro + Quant + Risk 三方意见，给出最终 verdict",
            prompt_full=build_cio_prompt(sample_asset),
            temperature=0.1,    # 更保守
            enable_tools=False,
            notes=[
                "5 个 verdict: BUY / ACCUMULATE / HOLD / TRIM / SELL",
                "Sanity: confidence≥0.95+BUY → 自动降级 ACCUMULATE",
                "Sanity: |alloc|>100k → clamp 到 ±100k",
                "Sanity: 输入含 [WORKER_UNAVAILABLE] → 强制 HOLD + confidence≤0.4",
            ],
        ),
    ]

    return RegimeRulesResponse(
        regime_thresholds=dict(THRESHOLDS),
        regime_types=["crash", "uptrend", "downtrend", "range_bound", "unknown"],
        regime_priority=[
            "1. crash (ATR% ≥ 5% → 任何方向都强制 neutral)",
            "2. uptrend (MA20 vs MA120 偏离 ≥ +3%)",
            "3. downtrend (MA20 vs MA120 偏离 ≤ -3%)",
            "4. range_bound (其他)",
            "5. unknown (数据不足 → 维持原计划)",
        ],
        verdict_options=["BUY", "ACCUMULATE", "HOLD", "TRIM", "SELL"],
        sanity_checks=[
            "confidence≥0.95 + verdict=BUY → 自动降级到 ACCUMULATE(0.6)",
            "|SUGGESTED_ALLOC_CNY| > 100,000 → clamp 到 ±100,000",
            "输入含 [WORKER_UNAVAILABLE] → 强制 HOLD + confidence ≤ 0.4",
            "Risk=high_risk → 即便 Quant+Macro bullish 也最多 ACCUMULATE，禁 BUY",
            "CONCENTRATION > 60% → 任何加仓 ≤ dry_powder × 10%",
        ],
        agents=agents_info,
        tools=list(TOOL_DEFINITIONS),
    )


@app.get("/api/agents/tool_calls", response_model=ToolCallsResponse, tags=["system"])
async def get_tool_calls(
    since: int = Query(200, ge=1, le=5000),
    asset: Optional[str] = Query(None, description="过滤特定资产 symbol"),
    role: Optional[str] = Query(None, description="过滤特定 agent role"),
) -> ToolCallsResponse:
    """每次 LLM 主动调 tool 的明细（agent_role / asset / tool_name / args / result preview / 耗时）。
    GUI 用此端点告诉用户 'AI 在 18:05 主动查了 multi_timeframe(NDQ.AX) 拿技术指标'"""
    from core.llm_telemetry import read_tool_calls
    records = read_tool_calls(since=since)
    if asset:
        records = [r for r in records if r.get("asset") == asset]
    if role:
        records = [r for r in records if r.get("agent_role") == role]
    # 倒序：最新在前
    records_sorted = list(reversed(records))
    return ToolCallsResponse(
        count=len(records_sorted),
        records=[ToolCallRecord(**r) for r in records_sorted],
    )


@app.get(
    "/api/committee_sessions/{date}/{symbol}",
    response_model=CommitteeSessionDetail,
    tags=["system"],
)
async def get_committee_session(date: str, symbol: str) -> CommitteeSessionDetail:
    """单个委员会决议完整 markdown"""
    store = MemoryStore()
    md = store.root / ".committee" / date / f"{symbol}.md"
    if not md.exists():
        raise HTTPException(404, f"未找到 {date}/{symbol}")
    return CommitteeSessionDetail(
        date=date,
        symbol=symbol,
        content=md.read_text(encoding="utf-8"),
    )


@app.post("/api/cash/{currency}/withdraw", response_model=WriteResponse, tags=["cash_write"])
async def cash_withdraw(currency: str, body: CashWriteRequest = Body(...)) -> WriteResponse:
    """v2 通用任意币种取款（默认禁止扣到负数 — PM 强烈要求；后续可加 force=true 显式越过）"""
    ccy = currency.upper()
    if not (3 <= len(ccy) <= 5) or not ccy.isalpha():
        raise HTTPException(status_code=400, detail=f"非法币种 {currency}")

    pm = _new_pm()
    cur = pm.cash_amount(ccy)
    if cur < body.amount:
        raise HTTPException(
            status_code=400,
            detail=f"{ccy} 余额不足：当前 {cur:.2f}，需要扣 {body.amount}",
        )

    with pm.with_portfolio_tx() as p:
        cash = dict(p.get("cash") or {})
        new_balance = float(cash.get(ccy, 0) or 0) - body.amount
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


# ============================================================
# GUI 静态文件挂载（如果 static/ 已 sync）
# ============================================================
# ============ CommSec 手动导入端点（Task #38）============
# 替代旧 cron 自动模式：cron 模式 IMAP 临时失败会静默丢成交。改成
# 用户主动触发：先 /preview 看拉到了什么，确认后再 /apply 写入

class CommsecPreviewResponse(BaseModel):
    """GET /api/commsec/preview"""
    ok: bool = True
    lookback_days: int
    new_trades: List[Dict[str, Any]] = Field(default_factory=list)
    skipped_count: int = 0
    error: Optional[str] = None


class CommsecApplyRequest(BaseModel):
    lookback_days: int = Field(default=180, ge=1, le=365)


class CommsecApplyResponse(BaseModel):
    ok: bool = True
    written: int = 0
    skipped: int = 0
    errors: List[str] = Field(default_factory=list)


def _commsec_fetch(lookback_days: int) -> tuple[List[Dict[str, Any]], Optional[str]]:
    """共享拉取逻辑：返回 (new_trades, err_msg)"""
    email_user = os.getenv("EMAIL_SENDER")
    email_pass = os.getenv("EMAIL_PASSWORD")
    if not (email_user and email_pass):
        return [], "缺少 EMAIL_SENDER / EMAIL_PASSWORD 环境变量"

    from services.commsec_reader import CommSecReader

    pm = _new_pm()
    reader = CommSecReader(email_user, email_pass)
    if not reader.connect():
        return [], "IMAP 连接失败（凭证错误或 Gmail 限速）"

    try:
        processed = pm.get_processed_emails()
        trades = reader.fetch_trade_confirmations(
            lookback_days=lookback_days, processed_ids=processed,
        )
    finally:
        reader.close()
    return trades, None


@app.get(
    "/api/commsec/preview",
    response_model=CommsecPreviewResponse,
    tags=["commsec"],
)
async def commsec_preview(
    lookback_days: int = Query(180, ge=1, le=365),
) -> CommsecPreviewResponse:
    """预览 CommSec 邮件拉到的新成交（不写入）。GUI [Import] 按钮先调它"""
    trades, err = _commsec_fetch(lookback_days)
    if err:
        return CommsecPreviewResponse(
            ok=False, lookback_days=lookback_days, error=err,
        )
    return CommsecPreviewResponse(
        ok=True, lookback_days=lookback_days, new_trades=trades,
    )


@app.post(
    "/api/commsec/apply",
    response_model=CommsecApplyResponse,
    tags=["commsec"],
)
async def commsec_apply(
    body: CommsecApplyRequest = Body(...),
) -> CommsecApplyResponse:
    """实际写入 CommSec 拉到的成交到 portfolio + history.jsonl

    GUI 弹确认窗 → 用户点 Confirm → 调本接口
    """
    trades, err = _commsec_fetch(body.lookback_days)
    if err:
        raise HTTPException(status_code=503, detail=err)

    pm = _new_pm()
    written = 0
    errors: List[str] = []
    for t in trades:
        try:
            pm.record_external_trade(t)
            written += 1
        except Exception as e:  # noqa: BLE001
            errors.append(f"{t.get('symbol', '?')}: {type(e).__name__} {e}")

    return CommsecApplyResponse(
        ok=len(errors) == 0,
        written=written,
        skipped=len(trades) - written,
        errors=errors,
    )


# 一键部署模式：跑完 `python -m scripts.sync_gui_dist` 后，static/ 含 invest-gui 构建产物
# FastAPI 把它挂到 /，所有非 /api/* 请求自动 serve GUI（含 SPA 路由 fallback）
#
# 生产 Caddy 部署时这块不会被触发——Caddy 优先 file_server /srv/invest-gui，
# 只把 /api/* 反代到本服务，根本不会到这条 mount。共存无冲突。
#
# 必须放在所有路由声明之后；StaticFiles(html=True) 让 / 自动 serve index.html
_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
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
