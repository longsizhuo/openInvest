"""observability 路由 — 从 system.py 按域拆分（行为不变）。

可观测性面板端点：cron job 状态、LLM 用量明细/汇总、数据源健康、tool 调用明细。
所有 @router.get path 逐字搬运，行为零漂移；_next_run_from_cron 私有 helper
随 get_jobs_status 同模块搬运。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional, Tuple

from fastapi import APIRouter, Depends, Query

from core.memory_store import MemoryStore
from core.portfolio_manager import PortfolioManager
from utils.exchange_fee import get_history_data
from utils.gold_price import get_gold_snapshot

from connectors.web_api.models import (
    DataSourceHealth,
    DataSourcesHealthResponse,
    JobStatus,
    JobsStatusResponse,
    LlmRoleStats,
    LlmSummaryResponse,
    LlmUsageRecord,
    LlmUsageResponse,
    ToolCallRecord,
    ToolCallsResponse,
)
from connectors.web_api.deps import get_pm

log = logging.getLogger("web_api")
from connectors.web_api.routers.write import _now_iso

router = APIRouter()


@router.get("/api/jobs/status", response_model=JobsStatusResponse, tags=["system"])
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


@router.get("/api/llm/usage", response_model=LlmUsageResponse, tags=["system"])
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


@router.get("/api/llm/summary", response_model=LlmSummaryResponse, tags=["system"])
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


@router.get("/api/data_sources/health", response_model=DataSourcesHealthResponse, tags=["system"])
async def get_data_sources_health(pm: PortfolioManager = Depends(get_pm)) -> DataSourcesHealthResponse:
    """所有数据源的当前可达性 + 最后成功拉取时间。GUI 透明化"我们用什么数据决策"

    B5 通用化（2026-05）：监控 symbol 不再硬编码作者持仓（NDQ.AX/GC=F），
    动态读用户实际 holdings；额外保留宏观指标（VIX/TNX/USDCNY 等）作背景。
    """
    sources: List[DataSourceHealth] = []

    # 用户实际持仓 + 通用宏观背景指标
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


@router.get("/api/agents/tool_calls", response_model=ToolCallsResponse, tags=["system"])
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


@router.get("/api/discipline", tags=["system"])
async def get_discipline() -> dict:
    """委员会纪律台账(只读):默认不作为率(HOLD 占比)+ 拦截冲动操作次数 + 反事实省/费钱。
    对齐 ADR-023——委员会可证价值是纪律/透明,不是 alpha。GUI/agent 据此展示"它拦了什么"。
    返回 {summary: {...}, markdown: "..."}(结构化 + 已渲染人话,任选其一用)。"""
    from services.discipline import discipline_summary, render_discipline_md
    s = discipline_summary()
    return {"summary": s, "markdown": render_discipline_md(s)}
