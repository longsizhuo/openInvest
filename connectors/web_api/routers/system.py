"""system 路由 — 从 web_api.py 按 tag 拆分（行为不变）。"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from core.memory_store import MemoryStore
from core.portfolio_manager import PortfolioManager
from utils.exchange_fee import get_history_data
from utils.gold_price import get_gold_snapshot

from connectors.web_api.models import (
    AgentPromptInfo,
    CommitteeSessionDetail,
    CommitteeSessionSummary,
    CommitteeSessionsResponse,
    DataSourceHealth,
    DataSourcesHealthResponse,
    DreamEvent,
    DreamsStateResponse,
    FreshInsightItem,
    FreshInsightsResponse,
    InsightItem,
    InsightsResponse,
    JobStatus,
    JobsStatusResponse,
    LlmRoleStats,
    LlmSummaryResponse,
    LlmUsageRecord,
    LlmUsageResponse,
    OutperformEvent,
    OutperformEventsResponse,
    PnLHistoryPoint,
    PnLHistoryResponse,
    ReengagementAlert,
    ReengagementResponse,
    RegimeResponse,
    RegimeRulesResponse,
    ToolCallRecord,
    ToolCallsResponse,
    VerdictReviewDataResponse,
    VerdictReviewItem,
    VerdictReviewReportResponse,
    VerdictReviewSummary,
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


@router.get("/api/insights", response_model=InsightsResponse, tags=["system"])
async def get_insights() -> InsightsResponse:
    """Dreaming 整合出的长期模式

    优先从 SQLite（db/insights.db）读取，降级到 memory/insights/*.md glob 扫描。
    SQLite 方案减少 I/O 开销并支持 SQL 查询；.md 文件保留为人类可读副本。
    """
    # 优先走 SQLite
    try:
        from db.insights_db import InsightsDB
        db = InsightsDB()
        rows = db.list_all()
        if rows:
            items = [
                InsightItem(
                    slug=row["slug"],
                    metadata={
                        k: row[k] for k in ("asset", "hit_rate", "sample_count", "source_score", "created_at")
                        if row.get(k) is not None
                    },
                    body=row.get("body") or "",
                )
                for row in rows
            ]
            return InsightsResponse(count=len(items), items=items)
    except Exception as e:
        log.warning(f"InsightsDB 查询失败，降级到 .md glob: {e}")

    # 降级：glob memory/insights/*.md（保留原有行为，确保渐进迁移期间不断服）
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


@router.get("/api/insights/fresh", response_model=FreshInsightsResponse, tags=["system"])
async def get_fresh_insights(
    since_hours: int = Query(48, ge=1, le=720, description="只返回 N 小时内新写入的"),
    limit: int = Query(5, ge=1, le=50),
) -> FreshInsightsResponse:
    """最近 N 小时新写入的 Dreaming insight，给 GUI 主面板做 toast/nudge 用

    PM-3 留存漏洞 #1 修复：之前 Dreaming 三阶段（Light/REM/Deep Sleep）的产物
    insights 只在 System 页深处展示，用户感受不到 "AI 在变聪明"。这个端点专门
    挑"刚出炉"的 insight 让前端做 toast：
        "AI 学到一条 80% 命中率新模式：黄金 ATR>3% 时 ACCUMULATE 7 天后..."

    数据源优先级：
      1. SQLite（db/insights.db），O(1) 查询，按 created_at 过滤
      2. memory/insights/*.md glob + mtime（降级，渐进迁移期间保底）
    """
    import time

    # 优先走 SQLite
    try:
        from db.insights_db import InsightsDB
        db = InsightsDB()
        rows = db.list_fresh(since_hours=since_hours, limit=limit)
        if rows:
            items = [
                FreshInsightItem(
                    slug=row["slug"],
                    title=(row.get("title") or row["slug"])[:120],
                    hit_rate=row.get("hit_rate"),
                    sample_count=row.get("sample_count"),
                    asset=row.get("asset"),
                    written_at=row["created_at"],
                )
                for row in rows
            ]
            return FreshInsightsResponse(count=len(items), items=items)
    except Exception as e:
        log.warning(f"InsightsDB fresh 查询失败，降级到 .md glob: {e}")

    # 降级：glob memory/insights/*.md + mtime 过滤（保留原有行为）
    store = MemoryStore()
    insights_dir = store.root / "insights"
    if not insights_dir.exists():
        return FreshInsightsResponse(count=0, items=[])

    cutoff_ts = time.time() - since_hours * 3600
    candidates: List[FreshInsightItem] = []
    for md_file in insights_dir.glob("*.md"):
        try:
            mtime = md_file.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff_ts:
            continue
        doc = store.read(f"insights/{md_file.stem}")
        if not doc:
            continue
        meta = doc.metadata or {}
        # title 优先用 metadata.title，没就抓 body 第一行 h1/h2
        title = str(meta.get("title") or meta.get("summary") or "").strip()
        if not title:
            for line in (doc.body or "").splitlines():
                stripped = line.strip().lstrip("#").strip()
                if stripped:
                    title = stripped
                    break
        if not title:
            title = md_file.stem
        candidates.append(FreshInsightItem(
            slug=md_file.stem,
            title=title[:120],
            hit_rate=meta.get("hit_rate") if isinstance(meta.get("hit_rate"), (int, float)) else None,
            sample_count=meta.get("sample_count") if isinstance(meta.get("sample_count"), int) else None,
            asset=str(meta.get("asset")) if meta.get("asset") else None,
            written_at=datetime.fromtimestamp(mtime).isoformat(timespec="seconds"),
        ))
    candidates.sort(key=lambda x: x.written_at, reverse=True)
    return FreshInsightsResponse(count=len(candidates[:limit]), items=candidates[:limit])


@router.get("/api/reengagement", response_model=ReengagementResponse, tags=["system"])
async def get_reengagement_alerts(pm: PortfolioManager = Depends(get_pm)) -> ReengagementResponse:
    """主动 nudge 用户回 GUI 的事件流。前端轮询，detected 就弹 toast。

    PM-3 留存漏洞 #3 修复：当前没有任何 outbound 触发器把"事件"推到用户面前。
    这个端点把以下三类事件聚合：
      - volatile: 任一持仓今日涨跌幅 > 5%
      - high_confidence_buy: 最新 verdict confidence > 0.8 且方向是 BUY/ACCUMULATE
      - stale_decision: 上次跑委员会 > 7 天（用户该看一眼了）
    """
    store = MemoryStore()
    alerts: List[ReengagementAlert] = []
    now = datetime.now()

    # 1. volatile: 今日涨跌 > 5%
    for h in pm.holdings:
        if h.get("is_tracking_only"):
            continue
        sym = str(h.get("symbol") or "")
        if not sym:
            continue
        try:
            df = get_history_data(sym, "5d")
        except Exception:
            continue
        if df is None or df.empty or len(df) < 2:
            continue
        last = float(df["Close"].iloc[-1])
        prev = float(df["Close"].iloc[-2])
        if prev <= 0:
            continue
        pct = (last / prev - 1) * 100
        if abs(pct) >= 5.0:
            alerts.append(ReengagementAlert(
                kind="volatile",
                asset=sym,
                message=f"{h.get('display_name', sym)} 今日 {pct:+.2f}%，超过 5% 异动阈值，建议查看",
                severity="warn" if abs(pct) < 8 else "urgent",
                detected_at=now.isoformat(timespec="seconds"),
            ))

    # 2. high_confidence_buy: 最新 verdict
    # 注意：committee .md 不写 frontmatter，verdict / confidence 在正文
    # `**Verdict**: BUY (confidence 0.85)` 这一行，所以用 regex 解析（既兼容
    # save_committee skill 路径写的格式，也兼容 daily_report DeepSeek 路径写的）
    verdict_re = re.compile(
        r"\*\*Verdict\*\*:\s*([A-Z_]+)\s*\(confidence\s+([0-9.]+)\)",
        re.IGNORECASE,
    )
    committee_dir = store.root / ".committee"
    if committee_dir.exists():
        for date_dir in sorted(committee_dir.iterdir(), reverse=True)[:3]:
            if not date_dir.is_dir():
                continue
            for md in date_dir.glob("*.md"):
                try:
                    text = md.read_text(encoding="utf-8")
                except Exception:
                    continue
                m = verdict_re.search(text)
                if not m:
                    continue
                verdict = m.group(1).upper()
                try:
                    conf = float(m.group(2))
                except ValueError:
                    continue
                if conf >= 0.8 and verdict in ("BUY", "ACCUMULATE"):
                    alerts.append(ReengagementAlert(
                        kind="high_confidence_buy",
                        asset=md.stem,
                        message=f"{md.stem} 最新决议 {verdict}（置信 {conf:.2f}），高置信加仓信号值得复核",
                        severity="info",
                        detected_at=date_dir.name,
                    ))

    # 3. stale_decision: 最近一次委员会 > 7 天
    if committee_dir.exists():
        all_dates = sorted([d.name for d in committee_dir.iterdir() if d.is_dir()], reverse=True)
        if all_dates:
            try:
                last_date = datetime.strptime(all_dates[0], "%Y-%m-%d")
                age_days = (now - last_date).days
                if age_days >= 7:
                    alerts.append(ReengagementAlert(
                        kind="stale_decision",
                        asset=None,
                        message=f"上次跑委员会是 {age_days} 天前，建议今日复盘一次",
                        severity="info",
                        detected_at=now.isoformat(timespec="seconds"),
                    ))
            except Exception:
                pass

    return ReengagementResponse(count=len(alerts), alerts=alerts)


@router.get("/api/regime/{symbol:path}", response_model=RegimeResponse, tags=["system"])
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


@router.get("/api/dreams/state", response_model=DreamsStateResponse, tags=["system"])
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


@router.get("/api/pnl_history", response_model=PnLHistoryResponse, tags=["system"])
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


@router.get("/api/outperform_events", response_model=OutperformEventsResponse, tags=["system"])
async def get_outperform_events(
    since: int = Query(20, ge=1, le=500),
) -> OutperformEventsResponse:
    """openInvest 跑赢基准的"可分享瞬间"列表

    PM-3 增长杠杆：每次 pnl_snapshot 检测到 user_pct > bench_pct 都会落一条到
    docs/outperform_events.jsonl。GUI 可以把最近一条做成 toast / 截图分享卡。
    """
    docs_path = Path(__file__).parent.parent / "docs" / "outperform_events.jsonl"
    events: List[OutperformEvent] = []
    if docs_path.exists():
        try:
            with open(docs_path, encoding="utf-8") as f:
                lines = f.readlines()
            for line in reversed(lines[-since:]):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    events.append(OutperformEvent(**obj))
                except Exception:
                    continue
        except Exception as e:  # noqa: BLE001
            log.warning(f"读 outperform_events 失败: {e}")
    return OutperformEventsResponse(count=len(events), events=events)


@router.get("/api/committee_sessions", response_model=CommitteeSessionsResponse, tags=["system"])
async def list_committee_sessions(
    response: Response,
    limit: int = Query(50, ge=1, le=500),
) -> CommitteeSessionsResponse:
    """历史委员会决议列表（memory/.committee/<date>/<symbol>.md），按时间倒序

    no-store：决策回放页"看不到内容"的常见误诊——SWR 拿到的是中间层缓存的
    空列表。每跑一次委员会都会新增 .md，必须保证下一次 GET 拿到的是 disk
    实际状态。
    """
    response.headers["Cache-Control"] = "no-store"
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


@router.get("/api/verdict_review/data", response_model=VerdictReviewDataResponse, tags=["system"])
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


@router.get("/api/verdict_review/summary", response_model=VerdictReviewSummary, tags=["system"])
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


@router.get("/api/verdict_review/report", response_model=VerdictReviewReportResponse, tags=["system"])
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


@router.get("/api/regime_rules", response_model=RegimeRulesResponse, tags=["system"])
async def get_regime_rules() -> RegimeRulesResponse:
    """暴露 invest 项目所有「硬规则」+「LLM 提示词」给 GUI marketing 页

    包含：
    - core/regime.py 阈值表
    - 4 个 agent 角色的 system prompt 全文
    - CIO sanity check 清单
    - 5 个可被 LLM 调用的 tool
    """
    from core.regime import get_thresholds
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
        regime_thresholds=get_thresholds(),
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


@router.get(
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
