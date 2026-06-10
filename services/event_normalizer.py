"""services/event_normalizer —— 把 RawNewsItem 列表归一化成结构化 Event

核心：一次 flash 调用，批量喂多条新闻，让 LLM 输出 JSON 列表。
为什么批量：DeepSeek v4-flash 单 prompt 处理 25 条 ≈ 5k token，比 25 次单调用省 80% 成本，
延迟也从 25*2s 降到 1*5s。

输出每条 Event 字段：
  idx: 输入 raw item 的索引（让 caller 把 sources 挂回去）
  event_type: earnings / macro / policy / m&a / regulatory / other
  stance: risk / opportunity / neutral
  severity: low / mid / high
  source_reliability: low / mid / high
  one_line_claim: 一句话事实声明（中性写法，剥情绪 / 标题党）
  entities: 涉及的实体 / macro tag（小写，如 ["nvidia","semis","us_rates"]）
  affected_symbols: yfinance ticker 列表（["NVDA","NDQ.AX"]）
  ts: ISO 8601（取 published_at；没有则用 fetched_at）

调用方再合并 sources（同 event_id 多源）。
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from core.llm_telemetry import TelemetryMeta, record_llm_call
from services.embeddings import embed_text
from services.news_sources import RawNewsItem

log = logging.getLogger(__name__)

DEFAULT_MODEL = "deepseek-v4-flash"
MAX_BATCH = 25  # 单次 prompt 最多 25 条


@dataclass
class NormalizedEvent:
    """归一化后的事件 + 配套的 source 信息"""
    raw_idx: int
    event: Dict[str, Any]          # 给 event_store.upsert_event 用
    embedding: Optional[List[float]] = None
    raw_item: Optional[RawNewsItem] = None  # 留给调用方挂 sources


_SYSTEM_PROMPT = """\
You are a financial-event normalizer. Read each news headline + snippet and output ONE structured event per item.

For each input item, return a JSON object with these EXACT fields:
- idx: integer, the input "idx" field copied through
- event_type: one of "earnings", "macro", "policy", "ma", "regulatory", "geopolitical", "other"
- stance: one of "risk", "opportunity", "neutral"
- severity: one of "low", "mid", "high"
- source_reliability: one of "low", "mid", "high"
- one_line_claim: a short neutral factual statement (≤ 140 chars), strip clickbait/fear language, mention the entity + action + observable effect
- entities: lowercase list of entities and macro tags (e.g. ["nvidia", "semis"]). 1-5 items.
- affected_symbols: yfinance tickers actually moved by this event (e.g. ["NVDA", "NDQ.AX"]). May be empty.
- ts: ISO 8601 timestamp from the input's "published" field; if missing, copy "fetched_at"

Rules:
- A "risk" event creates probable downside for the listed symbols; "opportunity" creates upside.
- "neutral" means routine reporting (e.g. analyst restate, generic market summary). Use it liberally — most news is neutral.
- Severity reflects MAGNITUDE of expected price impact, not news drama.
- If a headline is pure clickbait (no concrete entity / event), set severity="low", stance="neutral".
- Gold/commodity mapping: central bank gold purchases, gold ETF in/outflows, and real-rate / USD moves
  that materially affect gold -> entities must include "gold" (plus the specific tag, e.g.
  "central_bank_gold" or "gold_etf_flows"), and affected_symbols must include "GC=F".

Return STRICT JSON: an object with one key "events" whose value is the list. No prose, no markdown.
"""


def normalize(
    items: List[RawNewsItem],
    *,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    skip_embedding: bool = False,
) -> List[NormalizedEvent]:
    """批量归一化。空 items 返回 []，缺 LLM key 返回 []。"""
    if not items:
        return []
    # 统一从 utils.llm 读 LLM 配置，向后兼容 DEEPSEEK_*
    from utils.llm import get_llm_config_safe
    _ak, _bu, _m, _p = get_llm_config_safe(default_model=DEFAULT_MODEL)
    api_key = api_key or _ak
    if not api_key:
        log.warning("LLM_API_KEY / DEEPSEEK_API_KEY 缺失，event_normalizer 跳过")
        return []

    results: List[NormalizedEvent] = []
    # 分批，每批 MAX_BATCH 条
    for i in range(0, len(items), MAX_BATCH):
        batch = items[i:i + MAX_BATCH]
        try:
            batch_events = _call_flash_batch(
                batch,
                start_idx=i,
                model=model or _m,
                api_key=api_key,
                base_url=base_url or _bu,
            )
        except Exception as e:
            log.warning(f"normalize batch starting idx={i} 失败: {type(e).__name__}: {e}")
            continue

        for ne in batch_events:
            raw = items[ne.raw_idx] if 0 <= ne.raw_idx < len(items) else None
            ne.raw_item = raw
            if not skip_embedding:
                ne.embedding = embed_text(ne.event["one_line_claim"])
            results.append(ne)
    return results


def _call_flash_batch(
    batch: List[RawNewsItem],
    *,
    start_idx: int,
    model: str,
    api_key: str,
    base_url: str,
) -> List[NormalizedEvent]:
    """单批调用 flash，返回 NormalizedEvent 列表（embedding 暂为空，由 normalize 加）"""
    from openai import OpenAI

    payload_items = []
    for j, it in enumerate(batch):
        payload_items.append({
            "idx": start_idx + j,
            "title": it.title[:240],
            "snippet": it.snippet[:480],
            "source": it.src_name,
            "published": it.published_at or "",
            "fetched_at": it.fetched_at,
        })
    user_msg = (
        "Normalize these news items into structured events. "
        "Return a JSON object {\"events\": [...]}.\n\n"
        f"Items:\n{json.dumps(payload_items, ensure_ascii=False)}"
    )

    client = OpenAI(api_key=api_key, base_url=base_url)
    meta = TelemetryMeta(
        agent_role="event_normalizer",
        provider="deepseek",
        model=model,
    )
    t0 = time.time()
    extra: Dict[str, Any] = {}
    if "v4" in model:
        # v4-flash 默认 thinking 模式且不兼容 response_format / 大 batch；关闭
        extra["extra_body"] = {"thinking": {"type": "disabled"}}
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        response_format={"type": "json_object"},
        temperature=0.0,
        **extra,
    )
    latency_ms = int((time.time() - t0) * 1000)
    usage = getattr(resp, "usage", None)
    record_llm_call(
        meta,
        input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
        output_tokens=getattr(usage, "completion_tokens", 0) or 0,
        latency_ms=latency_ms,
        ok=True,
    )

    raw_text = resp.choices[0].message.content or ""
    return _parse_events_json(raw_text, expected_size=len(batch), offset=start_idx)


def _parse_events_json(raw_text: str, *, expected_size: int, offset: int) -> List[NormalizedEvent]:
    """解析 LLM 返回。容忍 markdown code fence 包裹 / 部分字段缺失"""
    text = raw_text.strip()
    # 剥 ```json ... ``` 围栏
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\s*", "", text)
        text = re.sub(r"```\s*$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        log.warning(f"normalize JSON 解析失败: {e}; raw[:200]={raw_text[:200]}")
        return []

    raw_events = data.get("events") if isinstance(data, dict) else data
    if not isinstance(raw_events, list):
        log.warning(f"normalize 返回非 list: {type(raw_events)}")
        return []

    out: List[NormalizedEvent] = []
    for raw in raw_events:
        if not isinstance(raw, dict):
            continue
        try:
            ne = _sanitize_event(raw, offset=offset)
        except Exception as e:
            log.warning(f"event 解析跳过: {e}, raw={raw}")
            continue
        if ne:
            out.append(ne)
    return out


def _apply_entity_symbol_fallback(
    entities: List[str], affected: List[str],
) -> List[str]:
    """entities 命中确定性映射且 symbol 不在 affected → append（保序去重）。

    映射表在 services/symbol_map.py（issue #26：黄金 + 指数统一的两层映射模块；
    词边界正则防 goldman sachs 误映射）。纯代码规则，零 LLM。
    """
    from services.symbol_map import canonical_symbols_for_entities
    for symbol in canonical_symbols_for_entities(entities):
        if symbol not in affected:
            affected = [*affected, symbol]
    return affected


def _sanitize_event(raw: Dict[str, Any], *, offset: int) -> Optional[NormalizedEvent]:
    """字段校验 + 归一化。缺关键字段返回 None"""
    idx = raw.get("idx")
    if not isinstance(idx, int):
        return None
    claim = (raw.get("one_line_claim") or "").strip()
    if not claim:
        return None

    stance = (raw.get("stance") or "neutral").lower()
    if stance not in {"risk", "opportunity", "neutral"}:
        stance = "neutral"
    severity = (raw.get("severity") or "low").lower()
    if severity not in {"low", "mid", "high"}:
        severity = "low"

    entities_raw = raw.get("entities") or []
    entities = [str(e).lower().strip() for e in entities_raw if str(e).strip()][:10]
    affected_raw = raw.get("affected_symbols") or []
    affected = [str(s).strip() for s in affected_raw if str(s).strip()][:10]
    affected = _apply_entity_symbol_fallback(entities, affected)

    event = {
        "one_line_claim": claim[:240],
        "event_type": (raw.get("event_type") or "other").lower(),
        "stance": stance,
        "severity": severity,
        "source_reliability": (raw.get("source_reliability") or "mid").lower(),
        "ts": raw.get("ts") or "",
        "entities": entities,
        "affected_symbols": affected,
    }
    return NormalizedEvent(raw_idx=idx, event=event)
