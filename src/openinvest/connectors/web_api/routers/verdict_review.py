"""verdict_review 路由 — 从 system.py 按域拆分（行为不变）。

后验命中率端点：原始 verdict_review.jsonl、命中率汇总、完整 markdown 报告。
所有 @router.get path 逐字搬运，行为零漂移。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, Query

from openinvest.core.memory_store import MemoryStore

from openinvest.paths import INVEST_ROOT
from openinvest.connectors.web_api.models import (
    VerdictReviewDataResponse,
    VerdictReviewItem,
    VerdictReviewReportResponse,
    VerdictReviewSummary,
)

log = logging.getLogger("web_api")

router = APIRouter()


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
    report_path = INVEST_ROOT / "docs" / "verdict_accuracy.md"

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
    report_path = INVEST_ROOT / "docs" / "verdict_accuracy.md"
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
