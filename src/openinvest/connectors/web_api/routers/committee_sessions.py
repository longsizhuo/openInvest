"""committee_sessions 路由 — 从 system.py 按域拆分（行为不变）。

历史委员会决议端点：列表（倒序、no-store）+ 单条完整 markdown。
_parse_committee_header 私有 helper 随 list_committee_sessions 同模块搬运。
所有 @router.get path 逐字搬运，行为零漂移。
"""
from __future__ import annotations

import logging
from typing import List

from fastapi import APIRouter, HTTPException, Query, Response

from openinvest.core.memory_store import MemoryStore

from openinvest.connectors.web_api.models import (
    CommitteeSessionDetail,
    CommitteeSessionSummary,
    CommitteeSessionsResponse,
)

log = logging.getLogger("web_api")

router = APIRouter()


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
