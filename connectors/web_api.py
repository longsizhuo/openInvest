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

import logging
import os
from datetime import datetime
from typing import List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field

from core.memory_store import MemoryStore
from core.portfolio_manager import PortfolioManager
from utils.exchange_fee import get_history_data
from utils.gold_price import get_gold_snapshot

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
    """组装黄金持仓 + 实时估值（offset 来自 strategy.md）"""
    targets = pm.strategy.get("target_assets", [])
    gold_target = next((a for a in targets if a.get("symbol") == "GC=F"), None)
    offset = float(gold_target.get("price_offset_pct", 0.0)) if gold_target else 0.0

    grams = float(pm.portfolio.get("gold_grams", 0) or 0)
    avg_cost = float(pm.portfolio.get("gold_avg_cost_cny_per_gram", 0) or 0)

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


@app.get("/api/portfolio", response_model=PortfolioResponse, tags=["read"])
async def get_portfolio() -> PortfolioResponse:
    """完整持仓快照：现金（CNY/AUD）+ 黄金（含浮盈）+ NDQ.AX（含日变化）"""
    pm = _new_pm()
    return PortfolioResponse(
        cash=CashSummary(
            cny=float(pm.portfolio.get("cash_cny", 0)),
            aud=float(pm.portfolio.get("aud_cash", 0)),
        ),
        gold=_build_gold(pm),
        ndq=_build_ndq(float(pm.portfolio.get("ndq_shares", 0))),
    )


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
    """NDQ.AX 持仓 + 实时价 + 日变化（独立端点，便于前端按需刷新）"""
    pm = _new_pm()
    return _build_ndq(float(pm.portfolio.get("ndq_shares", 0)))


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
