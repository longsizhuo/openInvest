"""read 路由 — 从 web_api.py 按 tag 拆分（行为不变）。"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import FileResponse

from core.memory_store import MemoryStore
from core.portfolio_manager import PortfolioManager
from utils.exchange_fee import get_history_data
from utils.gold_price import get_gold_snapshot
from utils.quotes import get_quote

from connectors.web_api.models import (
    CashSummary,
    DailyEntry,
    DailyResponse,
    GoldHolding,
    HistoryResponse,
    HistoryRow,
    HoldingQuote,
    HoldingV2,
    HoldingsListResponse,
    NDQHolding,
    PortfolioResponse,
    StrategyResponse,
    SymbolSearchResponse,
    SymbolSearchResult,
    TargetAsset,
    TotalValueBreakdownItem,
    TotalValueResponse,
)
from connectors.web_api.deps import get_pm

log = logging.getLogger("web_api")
router = APIRouter()


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


@router.get("/api/portfolio", response_model=PortfolioResponse, tags=["read"])
async def get_portfolio(response: Response, pm: PortfolioManager = Depends(get_pm)) -> PortfolioResponse:
    """完整持仓快照（v1 兼容输出，前端无感）：现金 CNY/AUD + 黄金 + NDQ.AX

    no-store：fork 用户报告"GUI 不同步"——常见原因是反向代理 / 浏览器把这条
    GET 缓存住，NapCat 写完 portfolio.md 后 SWR 拿到的还是旧响应。后端每次
    都直接读 disk（PortfolioManager 不缓存），所以靠 no-store 让中间层别截。
    """
    response.headers["Cache-Control"] = "no-store"
    return _build_portfolio_response(pm)


@router.get("/api/portfolio/state", tags=["read"])
async def get_portfolio_state(response: Response) -> Dict[str, Any]:
    """轻量同步信号：返回 portfolio.md 的 mtime + size + 一句概要。

    GUI / agent 可以用这条做 polling 探针——比 /api/portfolio 便宜，且只要
    NapCat / CLI 写过盘 mtime 就会跳。比单纯靠 60s SWR 刷新更精准。

    用法（curl 端）：
        curl -s http://127.0.0.1:8765/api/portfolio/state
        → {"mtime": 1715823491.2, "size": 1005, "exists": true, ...}
    """
    response.headers["Cache-Control"] = "no-store"
    store = MemoryStore()
    path = store.path_of("portfolio")
    if not path.exists():
        return {"exists": False, "mtime": None, "size": 0,
                "hint": "memory/portfolio.md 不存在，先跑 `python -m scripts.skill init`"}
    st = path.stat()
    # 顺便给一个 holdings 数 + cash 币种数，前端可不拉全量就知道有没有数据
    try:
        pm = get_pm()
        holdings_count = sum(1 for _ in pm.holdings)
        cash_currencies = list(pm.cash.keys())
    except HTTPException:
        holdings_count = 0
        cash_currencies = []
    return {
        "exists": True,
        "mtime": st.st_mtime,
        "size": st.st_size,
        "holdings_count": holdings_count,
        "cash_currencies": cash_currencies,
    }


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


@router.get("/api/holdings", response_model=HoldingsListResponse, tags=["read"])
async def get_holdings(response: Response, pm: PortfolioManager = Depends(get_pm)) -> HoldingsListResponse:
    """v2 通用持仓列表：cash dict + holdings 数组（含实时 quote + 计算 P&L）

    no-store：参见 /api/portfolio 同步链路注释——防中间层缓存住，NapCat 写完
    portfolio.md 后下次 SWR refresh 必须拿到新数据。
    """
    response.headers["Cache-Control"] = "no-store"
    holdings = [_build_holding_v2(h) for h in pm.holdings]
    return HoldingsListResponse(cash=pm.cash, holdings=holdings)


@router.get("/api/portfolio/total_value", response_model=TotalValueResponse, tags=["read"])
async def get_portfolio_total_value(
    base: str = Query("CNY", min_length=3, max_length=5, description="折算目标币种"),
    pm: PortfolioManager = Depends(get_pm),
) -> TotalValueResponse:
    """所有现金 + 持仓 折算到指定币种的总市值

    现金：用 cash dict + yfinance 汇率
    持仓：用 quote.price * units，quote 是 cost_currency 计价，再用汇率折算
    追踪仓不计入（is_tracking_only）
    """
    from utils.fx import get_fx_rate
    base = base.upper().strip()

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


@router.get("/api/symbols/search", response_model=SymbolSearchResponse, tags=["read"])
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

@router.get("/api/strategy", response_model=StrategyResponse, tags=["read"])
async def get_strategy(pm: PortfolioManager = Depends(get_pm)) -> StrategyResponse:
    """当前投资策略：目标比例 + 各资产 cap / 点差 / 费率"""
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

@router.get("/api/gold", response_model=GoldHolding, tags=["read"])
async def get_gold(pm: PortfolioManager = Depends(get_pm)) -> GoldHolding:
    """黄金持仓 + 实时金价 + 渠道参考价（独立端点，前端可单独刷新而不重拉其他资产）"""
    return _build_gold(pm)


# ============ 端点：NDQ 独立查询 ============

@router.get("/api/ndq", response_model=NDQHolding, tags=["read"])
async def get_ndq(pm: PortfolioManager = Depends(get_pm)) -> NDQHolding:
    """NDQ.AX 持仓 + 实时价 + 日变化（v2: 从 pm.holdings 读）"""
    ndq_h = pm.holdings.find("NDQ.AX")
    shares = float(ndq_h.get("units", 0) or 0) if ndq_h else 0.0
    return _build_ndq(shares)


# ============ 端点：交易历史 ============

@router.get("/api/history", response_model=HistoryResponse, tags=["read"])
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


@router.get("/api/pnl_chart.svg", tags=["read"])
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

@router.get("/api/daily", response_model=DailyResponse, tags=["read"])
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
