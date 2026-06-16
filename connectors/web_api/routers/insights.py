"""insights 路由 — 从 system.py 按域拆分（行为不变）。

Dreaming 长期洞察 + 新鲜洞察 toast + 主动 reengagement nudge 三个端点。
所有 @router.get path 逐字搬运，行为零漂移。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, Query

from core.memory_store import MemoryStore
from core.portfolio_manager import PortfolioManager
from utils.exchange_fee import get_history_data

from connectors.web_api.models import (
    FreshInsightItem,
    FreshInsightsResponse,
    InsightItem,
    InsightsResponse,
    ReengagementAlert,
    ReengagementResponse,
)
from connectors.web_api.deps import get_pm

log = logging.getLogger("web_api")

router = APIRouter()


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
