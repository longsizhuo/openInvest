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
from openinvest.jobs.review_calc import CONTAMINATION_CUTOFF

from openinvest.paths import INVEST_ROOT
from openinvest.connectors.web_api.models import (
    VerdictReviewBucket,
    VerdictReviewDataResponse,
    VerdictReviewItem,
    VerdictReviewReportResponse,
    VerdictReviewSummary,
)

log = logging.getLogger("web_api")

router = APIRouter()


@router.get("/api/verdict_review/data", response_model=VerdictReviewDataResponse, tags=["system"])
def get_verdict_review_data(
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


# 红线 #2：命中率 n<30 不出具体数字（与 review_calc / export_accuracy 同阈值）
_MIN_N = 30
_DIRECTIONAL = ("BUY", "ACCUMULATE", "SELL", "TRIM")
_BUCKET_META = {
    "live": (True, "live 实盘决议（唯一业绩口径）"),
    "backtest": (False, "回测干净段（中性模拟持仓，非业绩，不可外推 live）"),
    "contaminated": (False, "污染桶（决议日落在 LLM 训练窗口，记忆穿越，非业绩）"),
}


def _rate(hits: int, n: int):
    return round(hits / n, 4) if n >= _MIN_N else None


def _bucket_of(it: Dict[str, Any]) -> str:
    """contaminated 优先（不分 source），其余按 source。日期兜底：cutoff 只会往后挪。"""
    if it.get("contaminated") or str(it.get("date") or "") <= CONTAMINATION_CUTOFF:
        return "contaminated"
    return "backtest" if it.get("source") == "backtest" else "live"


def _summarize_bucket(name: str, items: List[Dict[str, Any]]) -> VerdictReviewBucket:
    is_perf, label = _BUCKET_META[name]
    n = len(items)
    suppressed = n < _MIN_N
    hit = lambda it, w: (it.get("hits") or {}).get(w)  # noqa: E731

    by_window: Dict[str, Dict[str, Any]] = {}
    for w in ("1d", "7d", "30d"):
        hs = [h for it in items if (h := hit(it, w)) is not None]
        if hs:
            by_window[w] = {"n": len(hs), "hit_rate": None if suppressed else _rate(sum(hs), len(hs))}

    by_verdict: Dict[str, Dict[str, Any]] = {}
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for it in items:
        groups.setdefault((it.get("verdict") or "UNKNOWN").upper(), []).append(it)
    for v, rows in groups.items():
        d: Dict[str, Any] = {
            "n": len(rows),
            "avg_confidence": round(sum(float(r.get("confidence") or 0) for r in rows) / len(rows), 3),
        }
        for w in ("1d", "7d", "30d"):
            hs = [h for r in rows if (h := hit(r, w)) is not None]
            d[f"hit_rate_{w}"] = None if suppressed or not hs else _rate(sum(hs), len(hs))
        by_verdict[v] = d

    # 方向性 7d 命中：分母只算 7d 已成熟的 BUY/ACCUMULATE/SELL/TRIM（未成熟不当 miss，UNCLEAR 不算方向）
    dir_hits = [h for it in items
                if (it.get("verdict") or "").upper() in _DIRECTIONAL and (h := hit(it, "7d")) is not None]
    return VerdictReviewBucket(
        n=n, is_performance=is_perf, label=label, rates_suppressed_sub30=suppressed,
        by_window=by_window, by_verdict=by_verdict,
        directional_only_hit_rate=None if suppressed else _rate(sum(dir_hits), len(dir_hits)),
        directional_n=len(dir_hits),
    )


@router.get("/api/verdict_review/summary", response_model=VerdictReviewSummary, tags=["system"])
def get_verdict_review_summary() -> VerdictReviewSummary:
    """命中率汇总，按来源分 live / backtest / contaminated 三桶（ADR-022：绝不合并成一个数）。

    只有 live 桶是业绩；另两桶标注非业绩。任何格子 n<30 命中率置 null（红线 #2）。
    """
    store = MemoryStore()
    path = store.root / ".dreams" / "verdict_review.jsonl"
    report_path = INVEST_ROOT / "docs" / "verdict_accuracy.md"

    items: List[Dict[str, Any]] = []
    if path.exists():
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
            items = []

    buckets: Dict[str, List[Dict[str, Any]]] = {k: [] for k in _BUCKET_META}
    for it in items:
        buckets[_bucket_of(it)].append(it)
    return VerdictReviewSummary(
        total=len(items),
        **{k: _summarize_bucket(k, rows) for k, rows in buckets.items()},
        has_report_md=report_path.exists(),
    )


@router.get("/api/verdict_review/report", response_model=VerdictReviewReportResponse, tags=["system"])
def get_verdict_review_report() -> VerdictReviewReportResponse:
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
