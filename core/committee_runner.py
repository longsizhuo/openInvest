"""端到端单资产委员会跑（live 端点用）

把 daily_report 的数据准备逻辑抽出来，让 web_api 能用 progress_callback 实时上报进度。
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable, Dict, List, Optional

from core.committee import run_committee, run_macro_view
from core.portfolio_manager import PortfolioManager
from core.regime import format_regime_brief
from utils.exchange_fee import (
    analyze_multi_timeframe, get_history_data, get_macro_data,
)
from utils.market_metrics import compute_metrics

log = logging.getLogger(__name__)


def _resolve_event_brief(symbol: str, override: Optional[str]) -> str:
    """事件 RAG 召回（feature flag + 调用方 override 两条路）

    优先级：
    1. caller 显式传 override（含空串） → 用 override
    2. env INVEST_EVENT_RAG_ENABLED 非真 → 返回 ""（默认关，根因分析跑完再开）
    3. EventStore.recall(symbol) → format_event_brief

    任何异常都降级为 ""，不阻断 committee。
    """
    if override is not None:
        return override
    if os.getenv("INVEST_EVENT_RAG_ENABLED", "").lower() not in {"1", "true", "yes", "on"}:
        return ""
    try:
        from db.event_store import EventStore
        from services.embeddings import DEFAULT_DIM, embed_text
        store = EventStore(embedding_dim=DEFAULT_DIM)
        q_embed = embed_text(symbol) if store.vec_loaded else None
        events = store.recall(
            symbol,
            time_window_days=int(os.getenv("INVEST_EVENT_RAG_WINDOW_DAYS", "7")),
            min_severity=os.getenv("INVEST_EVENT_RAG_MIN_SEVERITY", "mid"),
            top_k=int(os.getenv("INVEST_EVENT_RAG_TOP_K", "8")),
            query_embedding=q_embed,
        )
        return format_event_brief(events)
    except Exception as e:  # noqa: BLE001
        log.warning(f"event RAG recall 失败 {symbol}: {type(e).__name__}: {e}")
        return ""


def format_event_brief(events: List[Dict[str, Any]]) -> str:
    """事件列表 → Macro prompt 注入用的结构化文本（人类可读 + 时效冲突显式）

    格式（最新在前）：
        [2026-05-13 14:32] [risk/high] [NDQ.AX, NVDA] (sources: reuters, bloomberg)
        Nvidia Q1 guidance miss, futures -3% AH.

        [2026-05-12 08:00] [opportunity/mid] [GC=F] (sources: ft)
        Powell signals dovish pivot; gold up 1.2%.
         ↳ supersedes 2026-05-10 hawkish-fed event
    """
    if not events:
        return ""
    lines: List[str] = []
    for e in events:
        ts = e.get("ts", "")
        stance = e.get("stance", "neutral")
        severity = e.get("severity", "low")
        affected = ", ".join(e.get("affected_symbols") or [])
        sources = ", ".join({(s.get("src_name") or "").split(":")[0] for s in (e.get("sources") or [])})
        src_part = f" (sources: {sources})" if sources else ""
        lines.append(
            f"[{ts}] [{stance}/{severity}] [{affected}]{src_part}\n"
            f"{e.get('one_line_claim', '')}"
        )
        if e.get("supersedes"):
            lines.append(f" ↳ supersedes event {e['supersedes']}")
        lines.append("")
    return "\n".join(lines).strip()


def run_committee_for_symbol(
    symbol: str,
    *,
    max_debate_rounds: int = 4,
    progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    shared_macro_view: Optional[str] = None,
    event_brief: Optional[str] = None,
) -> Dict[str, Any]:
    """端到端跑单资产 committee

    准备数据 → macro view（如果没传共享版） → multi-round Quant/Risk 辩论 → CIO → 持久化

    Args:
        shared_macro_view: 多资产场景下传共享 macro，避免每个资产都跑一次浪费 token
        event_brief: 事件层 RAG 召回的盘中事件上下文（结构化文本）。None / "" 都
            等价于"不召回 / 不注入"。caller 显式传则 override 内部默认召回。
            受 env INVEST_EVENT_RAG_ENABLED 总开关控制（默认 false）。
    """
    def emit(phase: str, **extra: Any) -> None:
        if progress_callback is None:
            return
        try:
            # asset 字段方便多资产 progress 区分
            progress_callback({"phase": phase, "asset": symbol, **extra})
        except Exception as e:  # noqa: BLE001
            log.warning(f"progress emit fail: {e}")

    emit("preparing", symbol=symbol)

    # 1. 拉 strategy 找 target
    pm = PortfolioManager()
    target = next(
        (a for a in pm.strategy.get("target_assets", []) if a.get("symbol") == symbol),
        None,
    )
    if target is None:
        emit("error", reason=f"asset {symbol} not in strategy.target_assets")
        return {"error": f"asset {symbol} not in strategy.target_assets"}

    # 2. 行情 + 指标 + regime
    try:
        df = get_history_data(symbol, "2y")
    except Exception as e:  # noqa: BLE001
        emit("error", reason=f"行情拉取失败: {e}")
        return {"error": f"行情拉取失败: {e}"}
    if df is None or df.empty:
        emit("error", reason=f"无 {symbol} 行情数据")
        return {"error": f"无 {symbol} 行情数据"}

    metrics = compute_metrics(df)
    market_data = analyze_multi_timeframe(
        df, f"{target.get('display_name', symbol)} ({symbol})",
    )
    # P1-2: 传 symbol 让 regime 用 per-asset 阈值（黄金/纳指/加密各异）
    regime_brief = format_regime_brief(metrics, symbol=symbol)
    emit("data_ready", regime_brief=regime_brief[:240])

    # 3. 事件 RAG 召回（事件层第二条腿；env feature flag 默认关）
    effective_event_brief = _resolve_event_brief(symbol, event_brief)
    if effective_event_brief:
        emit("event_brief_loaded", event_brief_preview=effective_event_brief[:240])

    # 4. Macro view —— 共享版优先（多资产 orchestrator 已经跑过一次）
    if shared_macro_view is not None:
        macro_view = shared_macro_view
        emit("macro_done", macro_preview=macro_view[:240], shared=True)
    else:
        emit("macro_start")
        try:
            macro_data = get_macro_data()
        except Exception as e:  # noqa: BLE001
            log.warning(f"get_macro_data 失败: {e}")
            macro_data = {}
        macro_view = run_macro_view(str(macro_data), event_brief=effective_event_brief)
        emit("macro_done", macro_preview=macro_view[:240])

    # 5. Portfolio summary（v2 通用 holdings 接口读）
    cash_cny = pm.cash_amount("CNY")
    aud_cash = pm.cash_amount("AUD")
    h = pm.holdings.find(symbol)
    units = float(h.get("units", 0) or 0) if h else 0.0
    avg_cost = float(h.get("avg_cost", 0) or 0) if h else 0.0
    cost_ccy = h.get("cost_currency", "") if h else ""
    buffer_cny = float(pm.user.get("exchange_buffer_cny", 0) or 0)
    risk_level = str(pm.user.get("risk_tolerance", "Balanced"))
    dry_powder = max(0.0, cash_cny - buffer_cny)

    portfolio_summary = (
        f"用户风险偏好: {risk_level}\n"
        f"CNY 现金: ¥{cash_cny:,.0f}（应急金 ¥{buffer_cny:,.0f}，可投 ¥{dry_powder:,.0f}）\n"
        f"AUD 现金: ${aud_cash:,.0f}\n"
        f"目标资产 {symbol}: {units} 单位"
        + (f"，均价 {avg_cost} {cost_ccy}" if avg_cost else "（暂无持仓）")
    )

    # 6. 跑多轮辩论 + CIO
    return run_committee(
        target,
        market_data=market_data,
        macro_view=macro_view,
        portfolio_summary=portfolio_summary,
        regime_brief=regime_brief,
        max_debate_rounds=max_debate_rounds,
        progress_callback=progress_callback,
    )
