"""event_brief — 事件 brief 解析 + 向量召回（从 committee_runner.py 拆分，逻辑不变）。"""
from __future__ import annotations

# 格式化纯核已迁 event_format（ADR-026）——导回保持历史导出面
from openinvest.core.runner.event_format import format_event_brief  # noqa: F401

import logging
import os
from datetime import datetime
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
            from openinvest.db.event_store import EventStore
            from openinvest.services.embeddings import DEFAULT_DIM
            _EVENT_STORE_SINGLETON = EventStore(embedding_dim=DEFAULT_DIM)
        except Exception as e:  # noqa: BLE001
            log.warning(f"_get_event_store init 失败: {type(e).__name__}: {e}")
            return None
    return _EVENT_STORE_SINGLETON


def _check_as_of(as_of: Optional[datetime]) -> None:
    """as_of 用法错误必须大声失败，不能被下面的 graceful except 吞成空 brief——
    否则回测传 naive/str 日期会静默跑成"无事件层"（2026-10-07 review）。"""
    if as_of is not None and (not isinstance(as_of, datetime) or as_of.utcoffset() is None):
        raise ValueError(f"as_of 必须是带时区的 datetime，收到 {as_of!r}")


def _recall_events(symbol: str, *, as_of: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """事件 RAG 召回（原始事件列表）。flag 关 / store 缺 / 召回失败 → []，不阻断 committee。

    召回参数全走 config.event.*；as_of 透传 EventStore.recall(as_of=)（issue #196）。
    """
    from openinvest.core.config import load_config
    cfg = load_config()
    if not cfg.event.enabled:
        return []
    try:
        from openinvest.services.embeddings import embed_query
        store = _get_event_store()
        if store is None:
            return []
        # embed_query：hash provider 返回 None → 不精排、按时间倒序（hash 精排 = 随机挑，2026-10-08）
        q_embed = embed_query(symbol) if store.vec_loaded else None
        # issue #26: 召回时把用户标的扩展为代理集合（NDQ.AX 也命中 ^NDX 指数事件）
        from openinvest.services.symbol_map import proxy_symbols_for
        return store.recall(
            symbol,
            time_window_days=cfg.event.rag_window_days,
            min_severity=cfg.event.rag_min_severity,
            top_k=cfg.event.rag_top_k,
            query_embedding=q_embed,
            aliases=sorted(proxy_symbols_for(symbol)),
            as_of=as_of,
        )
    except Exception as e:  # noqa: BLE001
        log.warning(f"event RAG recall 失败 {symbol}: {type(e).__name__}: {e}")
        return []


def _resolve_event_brief(
    symbol: str, override: Optional[str], *, as_of: Optional[datetime] = None,
) -> str:
    """事件 RAG 召回（feature flag + 调用方 override 两条路）

    优先级：
    1. caller 显式传 override（含空串） → 用 override
    2. env INVEST_EVENT_RAG_ENABLED 显式设 false → 返回 ""
    3. EventStore.recall(symbol) → format_event_brief

    任何异常都降级为 ""，不阻断 committee。

    as_of（issue #196）：透传 EventStore.recall(as_of=)，回测 as-of-D 只召回 D 时刻
    已入库的事件（created_at 口径）。None = 生产现行为。

    **默认行为 (2026-05-15 改 default-on)**：env 不设 / 设空 → 当作 true。
    用户记不住 4 步开 RAG 流程，所以默认就让 Macro 看新闻；明确设
    INVEST_EVENT_RAG_ENABLED=false 才关掉。安全保障：recall 任何失败
    （DB 缺、key 缺、网络挂）都 graceful 退化空字符串。
    """
    _check_as_of(as_of)
    if override is not None:
        return override
    try:
        return format_event_brief(_recall_events(symbol, as_of=as_of))
    except Exception as e:  # noqa: BLE001
        log.warning(f"event brief 格式化失败 {symbol}: {type(e).__name__}: {e}")
        return ""


def resolve_event_brief_multi(
    symbols: List[str], *, as_of: Optional[datetime] = None,
) -> str:
    """跨资产 event RAG 召回 + 去重，作为 daily_report cron 路径的共享 loader。

    跨资产共享 loader：跑一次，结果同时注入
    run_macro_view(event_brief=...) 和每个 run_committee(..., event_brief=...)。

    去重策略：先按 event_id 去重（同一事件被多个 symbol 各自召回只留一次，保留首次出现顺序），
    再把合并后的事件列表**一次性**交给 format_event_brief——同论点转载归并
    （event.max_corroboration）因此跨 symbol 也成立，而不是只在单 symbol 召回内成立。
    EVENT_STANCE 由这份 brief 的头行解析，计数随之按"不同论点"算。

    任何单 symbol 召回失败都 graceful 跳过（_recall_events 内部也已 graceful）。
    全部失败时返回 ""，不阻断 daily_report 主流程。

    Args:
        symbols: 所有 target_assets 的 symbol 列表（如 ["NDQ.AX", "GC=F"]）
        as_of: issue #196 回测 as-of-D 截断（逐 symbol 透传 _recall_events）；
            None = 生产现行为

    Returns:
        合并去重后的 event_brief 文本，空字符串表示无可用事件。
    """
    _check_as_of(as_of)
    if not symbols:
        return ""

    events: List[Dict[str, Any]] = []
    seen_ids: set = set()
    for sym in symbols:
        try:
            for e in _recall_events(sym, as_of=as_of):
                eid = e.get("event_id")
                if eid is not None:
                    if eid in seen_ids:
                        continue
                    seen_ids.add(eid)
                events.append(e)
        except Exception as e:  # noqa: BLE001
            log.warning(f"resolve_event_brief_multi: {sym} 召回失败（已跳过）: {type(e).__name__}: {e}")
    try:
        return format_event_brief(events)
    except Exception as e:  # noqa: BLE001
        log.warning(f"resolve_event_brief_multi: 格式化失败（退化为空）: {type(e).__name__}: {e}")
        return ""

# 注：_EVENT_STORE_SINGLETON 是模块级可变单例，刻意不放进 __all__——
# 否则 façade 的 `import *` 会带出一个 import 时的 None 快照（不跟踪真单例），
# 平白扩大 public surface。测试要重置直接 `core.runner.event_brief._EVENT_STORE_SINGLETON = None`。
__all__ = [
    "_get_event_store",
    "_resolve_event_brief",
    "format_event_brief",
    "resolve_event_brief_multi",
]
