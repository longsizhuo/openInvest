"""event_brief — 事件 brief 解析 + 向量召回（从 committee_runner.py 拆分，逻辑不变）。"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

_EVENT_STORE_SINGLETON: Optional[Any] = None  # module-level cache


def _get_event_store():
    """Lazy module-level singleton.

    PR #5 Copilot CR 修复: 旧版每次 _resolve_event_brief 调用都新建 EventStore
    (开 sqlite + WAL pragmas + 试 load sqlite-vec + CREATE TABLE IF NOT EXISTS).
    多资产 committee 时 N 资产跑 N 次, 浪费 fd + 启动开销.

    fork-safety: sqlite WAL 单一 conn 跨 fork 不安全, 但 invest 不走多进程
    fork, 所有 entry 都是单进程 / ThreadPoolExecutor; 安全.

    任何初始化失败 → 返回 None, _resolve_event_brief 走异常降级返 "".
    """
    global _EVENT_STORE_SINGLETON
    if _EVENT_STORE_SINGLETON is None:
        try:
            from db.event_store import EventStore
            from services.embeddings import DEFAULT_DIM
            _EVENT_STORE_SINGLETON = EventStore(embedding_dim=DEFAULT_DIM)
        except Exception as e:  # noqa: BLE001
            log.warning(f"_get_event_store init 失败: {type(e).__name__}: {e}")
            return None
    return _EVENT_STORE_SINGLETON


def _resolve_event_brief(symbol: str, override: Optional[str]) -> str:
    """事件 RAG 召回（feature flag + 调用方 override 两条路）

    优先级：
    1. caller 显式传 override（含空串） → 用 override
    2. env INVEST_EVENT_RAG_ENABLED 显式设 false → 返回 ""
    3. EventStore.recall(symbol) → format_event_brief

    任何异常都降级为 ""，不阻断 committee。

    **默认行为 (2026-05-15 改 default-on)**：env 不设 / 设空 → 当作 true。
    用户记不住 4 步开 RAG 流程，所以默认就让 Macro 看新闻；明确设
    INVEST_EVENT_RAG_ENABLED=false 才关掉。安全保障：recall 任何失败
    （DB 缺、key 缺、网络挂）都 graceful 退化空字符串。
    """
    if override is not None:
        return override
    flag = os.getenv("INVEST_EVENT_RAG_ENABLED", "true").lower()
    if flag in {"0", "false", "no", "off"}:
        return ""
    try:
        from services.embeddings import embed_text
        store = _get_event_store()
        if store is None:
            return ""
        q_embed = embed_text(symbol) if store.vec_loaded else None
        # issue #26: 召回时把用户标的扩展为代理集合（NDQ.AX 也命中 ^NDX 指数事件）
        from services.symbol_map import proxy_symbols_for
        events = store.recall(
            symbol,
            time_window_days=int(os.getenv("INVEST_EVENT_RAG_WINDOW_DAYS", "7")),
            min_severity=os.getenv("INVEST_EVENT_RAG_MIN_SEVERITY", "mid"),
            top_k=int(os.getenv("INVEST_EVENT_RAG_TOP_K", "8")),
            query_embedding=q_embed,
            aliases=sorted(proxy_symbols_for(symbol)),
        )
        return format_event_brief(events)
    except Exception as e:  # noqa: BLE001
        log.warning(f"event RAG recall 失败 {symbol}: {type(e).__name__}: {e}")
        return ""


def format_event_brief(events: List[Dict[str, Any]]) -> str:
    """事件列表 → Macro prompt 注入用的结构化文本（人类可读 + 时效冲突显式）

    格式（最新在前；ts 为 DB 原样 ISO 8601，可能含 Z/+00:00 或为空）：
        [2026-05-13T14:32:00+00:00] [risk/high] [NDQ.AX, NVDA] (sources: reuters, bloomberg)
        Nvidia Q1 guidance miss, futures -3% AH.

        [2026-05-12T08:00:00Z] [opportunity/mid] [GC=F] (sources: ft)
        Powell signals dovish pivot; gold up 1.2%.
         ↳ supersedes 2026-05-10 hawkish-fed event

    **头行格式是与 utils/sentiment._parse_event_brief_entries 的互解析契约**
    （EVENT_STANCE 聚合行靠它解析 stance/severity/syms/ts）——改头行结构必须
    同步解析正则 + tests/test_event_rag_resolve.py 的互解析回归测试。
    """
    if not events:
        return ""
    lines: List[str] = []
    for e in events:
        ts = e.get("ts", "")
        stance = e.get("stance", "neutral")
        severity = e.get("severity", "low")
        affected = ", ".join(e.get("affected_symbols") or [])
        # PR #5 Copilot CR: set comprehension 的迭代顺序非确定 → LLM 看到的
        # source 顺序每次跑都不同，影响 token-level cache / replay 稳定性。
        # 改用 dict.fromkeys 保留首次出现顺序 + sorted 兜底，全确定。
        _source_names = [(s.get("src_name") or "").split(":")[0] for s in (e.get("sources") or [])]
        sources = ", ".join(sorted(dict.fromkeys(_source_names)))
        src_part = f" (sources: {sources})" if sources else ""
        lines.append(
            f"[{ts}] [{stance}/{severity}] [{affected}]{src_part}\n"
            f"{e.get('one_line_claim', '')}"
        )
        if e.get("supersedes"):
            lines.append(f" ↳ supersedes event {e['supersedes']}")
        lines.append("")
    return "\n".join(lines).strip()


def resolve_event_brief_multi(symbols: List[str]) -> str:
    """跨资产 event RAG 召回 + 去重，作为 daily_report cron 路径的共享 loader。

    等价地位同 load_wealth_context_view：跑一次，结果同时注入
    run_macro_view(event_brief=...) 和每个 run_committee(..., event_brief=...)。

    去重策略：按 "ts|one_line_claim" 拆行后 dict 去重（保留首次出现顺序），
    避免同一事件被多个 symbol 各自召回后重复出现在 Macro prompt 里。

    任何单 symbol 召回失败都 graceful 跳过（_resolve_event_brief 内部也已 graceful）。
    全部失败时返回 ""，不阻断 daily_report 主流程。

    Args:
        symbols: 所有 target_assets 的 symbol 列表（如 ["NDQ.AX", "GC=F"]）

    Returns:
        合并去重后的 event_brief 文本，空字符串表示无可用事件。
    """
    if not symbols:
        return ""

    # 按 symbol 逐个召回，汇总所有事件段落
    # _resolve_event_brief(symbol, override=None) → 走内部 feature flag 召回逻辑
    all_briefs: List[str] = []
    for sym in symbols:
        try:
            brief = _resolve_event_brief(sym, override=None)
            if brief:
                all_briefs.append(brief)
        except Exception as e:  # noqa: BLE001
            log.warning(f"resolve_event_brief_multi: {sym} 召回失败（已跳过）: {type(e).__name__}: {e}")

    if not all_briefs:
        return ""

    # 按"段落"去重：每个事件以两行为一块（ts/stance 行 + claim 行）
    # 用首行（含 ts + claim）作为 key，保留首次出现顺序
    # 实现：把所有 brief 拼起来再按段分割，用 dict.fromkeys 去重
    combined = "\n\n".join(all_briefs)
    # 段落以空行分隔；split("\n\n") 可能产生空串，filter 掉
    paragraphs = [p.strip() for p in combined.split("\n\n") if p.strip()]
    # 去重 key = 段落全文（完全相同才去重，避免同 ts 不同 claim 被错误合并）
    deduped = list(dict.fromkeys(paragraphs))
    return "\n\n".join(deduped)

# 注：_EVENT_STORE_SINGLETON 是模块级可变单例，刻意不放进 __all__——
# 否则 façade 的 `import *` 会带出一个 import 时的 None 快照（不跟踪真单例），
# 平白扩大 public surface。测试要重置直接 `core.runner.event_brief._EVENT_STORE_SINGLETON = None`。
__all__ = [
    "_get_event_store",
    "_resolve_event_brief",
    "format_event_brief",
    "resolve_event_brief_multi",
]
