"""端到端单资产委员会跑（live 端点用）

把 daily_report 的数据准备逻辑抽出来，让 web_api 能用 progress_callback 实时上报进度。
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable, Dict, List, Optional

from core.committee import (
    load_wealth_context_view,
    parse_cio_memo,  # re-export: 给 entry (scripts.skill cmd_save_committee) 用，
                     # 避免 entry 直接 import core.committee 违反 lint-imports 契约
    run_committee,
    run_macro_view,
)
from core.portfolio_manager import PortfolioManager
from core.regime import format_regime_brief
from utils.exchange_fee import (
    analyze_multi_timeframe, get_history_data, get_macro_data,
)
from utils.market_metrics import compute_metrics

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


def load_prior_insights(asset: Dict[str, Any], pm: Optional[PortfolioManager] = None) -> str:
    """读 memory/insights/*.md → Dreaming 长期行为模式

    历史漂移背景（2026-05-16）: 这段代码原本在 jobs/daily_report.py:_gather_relevant_insights
    和 scripts/skill.py:_gather_relevant_insights 各有一份完全相同的副本，且
    run_committee_for_symbol（service layer）从来没调，导致 Web/GUI 路径的 LLM
    永远看不到 Dreaming long-term insights。统一提到这里作为 shared loader 后，
    三路径自动同步。

    Args:
        asset: strategy.target_assets 单项（dict 含 symbol/display_name 等）
        pm: PortfolioManager 实例（不传则现 new 一个，复用 store.root 路径）

    Returns:
        所有匹配 insights 拼接的 markdown，空字符串表示无相关。
    """
    try:
        if pm is None:
            pm = PortfolioManager()
        store = pm.store
        insights_dir = store.root / "insights"
        if not insights_dir.exists():
            return ""
        sym = asset.get("symbol", "").lower().replace("=", "_")
        matches = []
        for f in sorted(insights_dir.glob("*.md")):
            if sym in f.stem.lower() or any(
                tok in f.stem.lower() for tok in ["gold", "ndq"] if tok in sym
            ):
                matches.append(f"## {f.stem}\n{f.read_text(encoding='utf-8')[:600]}")
        return "\n\n".join(matches)
    except Exception as e:  # noqa: BLE001
        log.warning(f"load_prior_insights graceful 退化 '': {type(e).__name__}: {e}")
        return ""


def run_committee_for_symbol(
    symbol: str,
    *,
    max_debate_rounds: int = 4,
    progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    shared_macro_view: Optional[str] = None,
    event_brief: Optional[str] = None,
    wealth_context_view: Optional[str] = None,
    portfolio_summary_override: Optional[str] = None,
    prior_insights_override: Optional[str] = None,
) -> Dict[str, Any]:
    """端到端跑单资产 committee

    准备数据 → macro view（如果没传共享版） → multi-round Quant/Risk 辩论 → CIO → 持久化

    Args:
        shared_macro_view: 多资产场景下传共享 macro，避免每个资产都跑一次浪费 token
        event_brief: 事件层 RAG 召回的盘中事件上下文（结构化文本）。None / "" 都
            等价于"不召回 / 不注入"。caller 显式传则 override 内部默认召回。
            受 env INVEST_EVENT_RAG_ENABLED 总开关控制（默认 false）。
        wealth_context_view: shared loader 结果 override。None → 内部调
            load_wealth_context_view()（保持向后兼容）。Session orchestrator
            一次性算好后传进来避免重复调用。
        portfolio_summary_override: 给 cron daily_report 用——它拼了含 total_assets_cny
            + data_warnings 的完整版 portfolio_summary，需要让 Risk Officer 看见。
            None → 用内部精简版（默认）。
        prior_insights_override: shared loader 结果 override。None → 内部调
            load_prior_insights(asset, pm)（修复 2026-05-16 漂移：service layer
            过去从不读 insights，导致 Web/GUI 路径 LLM 看不到 Dreaming）。
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

    # portfolio_summary: 优先 caller override（daily_report 拼了含 total_assets_cny
    # + data_warnings 的完整版），否则用 service 内精简版
    if portfolio_summary_override is not None:
        portfolio_summary = portfolio_summary_override
    else:
        portfolio_summary = (
            f"用户风险偏好: {risk_level}\n"
            f"CNY 现金: ¥{cash_cny:,.0f}（应急金 ¥{buffer_cny:,.0f}，可投 ¥{dry_powder:,.0f}）\n"
            f"AUD 现金: ${aud_cash:,.0f}\n"
            f"目标资产 {symbol}: {units} 单位"
            + (f"，均价 {avg_cost} {cost_ccy}" if avg_cost else "（暂无持仓）")
        )

    # 5.5. WealthContextOfficer view（修复 2026-05-15 漂移: 之前没读 user.md 的
    # wealth_context, Risk Officer 永远按 portfolio cash 判风险）
    # caller 已经算过就 override 进来避免重复调用 LLM；否则 fallback 自己调
    if wealth_context_view is not None:
        wealth_view = wealth_context_view
    else:
        wealth_view = load_wealth_context_view()
    if wealth_view:
        emit("wealth_context_loaded", preview=wealth_view[:240])

    # 5.6. Prior insights / Dreaming long-term 行为模式（修复 2026-05-16 漂移:
    # service layer 之前从不读 insights, Web/GUI 路径的 LLM 永远看不到长期模式）
    if prior_insights_override is not None:
        prior_insights = prior_insights_override
    else:
        prior_insights = load_prior_insights(target, pm)
    if prior_insights:
        emit("prior_insights_loaded", preview=prior_insights[:240])

    # 6. 跑多轮辩论 + CIO
    return run_committee(
        target,
        market_data=market_data,
        macro_view=macro_view,
        portfolio_summary=portfolio_summary,
        prior_insights=prior_insights,
        regime_brief=regime_brief,
        wealth_context_view=wealth_view,
        max_debate_rounds=max_debate_rounds,
        progress_callback=progress_callback,
    )


# ============================================================================
# 主入口：run_committee_session — 三路径统一架构（防漂移单一可信源）
# ============================================================================
#
# 历史背景（2026-05-16）: openInvest 有三个调用入口（Skill/Web/Cron），都跑同一套
# "投资委员会"，但实现散在三处导致连续 4 次跨 entry 参数漂移事故（wealth_view /
# 邮件 render / event_brief / Gemini prompt）。
#
# 本函数是三路径**共享的单一可信源**：所有"跨资产 macro 共享 / event 多 symbol
# 召回 / wealth view 注入 / prior insights 加载 / 并行 dispatch"逻辑只在这一处实现。
#
# 三个 entry 只负责自己路径独有的事：
#   - Skill: Stage 0 同日 cache 检查、--force flag、最终 JSON 输出、NapCat hint
#   - Web/GUI: task_id 状态机、meta.json 审计、SSE 进度推送
#   - Cron: staleness 熔断、邮件渲染、Gemini 第二意见、Dreaming append_daily
#
# 加新跨 entry 参数时**只改本函数**，三路径自动同步。CLAUDE.md 分层契约段 + 漂移
# 历史表强制此契约。
#
# 测试: tests/test_committee_contract.py:test_run_committee_session_* 守护
# ============================================================================

def run_committee_session(
    symbols: Optional[List[str]] = None,
    *,
    max_debate_rounds: int = 4,
    progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    event_brief_override: Optional[str] = None,
    event_ids: Optional[List[str]] = None,
    macro_view_override: Optional[str] = None,
    wealth_view_override: Optional[str] = None,
    portfolio_summary_override: Optional[str] = None,
    max_workers: int = 4,
    on_asset_error: str = "continue",
) -> Dict[str, Any]:
    """跑一次"committee session"——跨资产 macro 共享 + event RAG + 并行 dispatch.

    Args:
        symbols: 要跑的 symbol 列表。None → 读 strategy.target_assets 全部
        max_debate_rounds: Quant/Risk cross-challenge 轮数上限，默认 4（三路径对齐）
        progress_callback: phase 事件推送（Web SSE 用；Skill/Cron 传 None）
        event_brief_override: 优先级最高的 event_brief 注入。含空串等价"我不要事件"
        event_ids: Web event-trigger 路径用。session 内部翻译成 brief。与 override
            互斥（override 优先）
        macro_view_override: 测试桩用，跳过 run_macro_view
        wealth_view_override: 测试桩 / backtest 用，跳过 load_wealth_context_view
        portfolio_summary_override: cron daily_report 拼了含 total_assets_cny 的
            完整版，传进来让 Risk Officer 看见。其他路径不传走 service 默认精简版
        max_workers: ThreadPoolExecutor 并发数，默认 4 防 LLM API 限流
        on_asset_error: "continue"（单资产失败不阻断其他）或 "raise"（任一失败抛）

    Returns:
        {
            "symbols": List[str],
            "macro_view": str,
            "wealth_view": str,
            "event_brief": str,
            "asset_committees": Dict[str, Dict],  # sym → result，或 {"error": str}
            "errors": Dict[str, str],
            "audit": {...},
        }

    Raises:
        RuntimeError: symbols 解析为空 / 全部资产失败 / on_asset_error="raise" 时单资产失败
    """
    from datetime import datetime, timezone

    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def emit(phase: str, **extra: Any) -> None:
        if progress_callback is None:
            return
        try:
            progress_callback({"phase": phase, **extra})
        except Exception as e:  # noqa: BLE001
            log.warning(f"session progress emit fail: {e}")

    # ---- Step 1: 解析 symbols（None / 空 → strategy.target_assets）----
    pm = PortfolioManager()
    if not symbols:
        symbols = [
            str(a.get("symbol")) for a in pm.strategy.get("target_assets", [])
            if a.get("symbol")
        ]
    if not symbols:
        raise RuntimeError("run_committee_session: 没有可跑的 symbol "
                           "（symbols 参数空 + strategy.target_assets 也空）")
    emit("session_start", symbols=symbols, max_debate_rounds=max_debate_rounds)

    # ---- Step 2: shared wealth_view ----
    if wealth_view_override is not None:
        wealth_view = wealth_view_override
    else:
        wealth_view = load_wealth_context_view()
    if wealth_view:
        emit("wealth_context_loaded", preview=wealth_view[:240])

    # ---- Step 3: shared event_brief（三选一，严格优先级）----
    event_brief_source: str
    if event_brief_override is not None:
        event_brief = event_brief_override
        event_brief_source = "override"
    elif event_ids:
        # Web event-trigger 路径：caller 已知具体 event_ids，反查 + format
        event_brief = ""
        try:
            store = _get_event_store()
            if store is not None:
                fetched: List[Dict[str, Any]] = []
                for eid in event_ids:
                    ev = store.get_event(eid)
                    if not ev:
                        continue
                    ev = dict(ev)
                    ev["sources"] = store.get_sources(ev["event_id"])
                    fetched.append(ev)
                event_brief = format_event_brief(fetched)
        except Exception as e:  # noqa: BLE001
            log.warning(f"event_ids 翻译失败 graceful 退化 '': {type(e).__name__}: {e}")
        event_brief_source = "event_ids"
    else:
        # 默认：跨资产 multi 召回 + 去重
        event_brief = resolve_event_brief_multi(symbols)
        event_brief_source = "multi_recall" if event_brief else "disabled"
    if event_brief:
        emit("event_brief_loaded", source=event_brief_source,
             preview=event_brief[:240])

    # ---- Step 4: shared macro_view ----
    if macro_view_override is not None:
        macro_view = macro_view_override
        emit("macro_done", macro_preview=macro_view[:240], shared=True,
             from_override=True)
    else:
        emit("macro_start", symbols=symbols,
             event_brief_attached=bool(event_brief))
        try:
            macro_data = get_macro_data()
        except Exception as e:  # noqa: BLE001
            log.warning(f"get_macro_data 失败 graceful 退化空 dict: {e}")
            macro_data = {}
        macro_view = run_macro_view(str(macro_data), event_brief=event_brief)
        emit("macro_done", macro_preview=macro_view[:240], shared=True)

    # ---- Step 5: dispatch（串行 or 并行）----
    # 单资产 / max_workers<=1 → 走串行（避免 ThreadPoolExecutor 嵌套，外层 caller
    # 如 web_api 的 background task 已经在 worker thread 里跑时，再嵌内层 pool
    # 在 anyio TestClient 下会出现 future 不 resolve 的诡异问题）
    asset_committees: Dict[str, Dict[str, Any]] = {}
    errors: Dict[str, str] = {}

    def _run_one(sym: str) -> Dict[str, Any]:
        return run_committee_for_symbol(
            sym,
            max_debate_rounds=max_debate_rounds,
            progress_callback=progress_callback,
            shared_macro_view=macro_view,
            event_brief=event_brief,
            wealth_context_view=wealth_view,
            portfolio_summary_override=portfolio_summary_override,
            # prior_insights_override 不传 → service 内 load_prior_insights(sym)
            # 每个资产 insights 不同，session 不预加载
        )

    def _record_result(sym: str, result: Any, err: Optional[str] = None) -> None:
        if err is not None:
            errors[sym] = err
            asset_committees[sym] = {"error": err}
            return
        if isinstance(result, dict) and "error" in result:
            errors[sym] = result["error"]
        asset_committees[sym] = result

    effective_workers = min(len(symbols), max_workers)
    if effective_workers <= 1 or len(symbols) <= 1:
        # 串行：跟 cron daily_report 原行为一致；TestClient / 嵌套 thread 安全
        for sym in symbols:
            try:
                _record_result(sym, _run_one(sym))
            except Exception as e:  # noqa: BLE001
                msg = f"{type(e).__name__}: {e}"
                log.warning(f"session: {sym} 失败 ({on_asset_error}): {msg}")
                _record_result(sym, None, err=msg)
                if on_asset_error == "raise":
                    raise
    else:
        # 真并行：多资产用 ThreadPoolExecutor 提速
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=effective_workers) as pool:
            futures = {pool.submit(_run_one, sym): sym for sym in symbols}
            for fut in futures:
                sym = futures[fut]
                try:
                    _record_result(sym, fut.result())
                except Exception as e:  # noqa: BLE001
                    msg = f"{type(e).__name__}: {e}"
                    log.warning(f"session: {sym} 失败 ({on_asset_error}): {msg}")
                    _record_result(sym, None, err=msg)
                    if on_asset_error == "raise":
                        raise

    # 全部失败 → 抛（caller 没法拼报告）
    if errors and len(errors) == len(symbols):
        raise RuntimeError(
            f"session: 全部 {len(symbols)} 个资产都失败: "
            + ", ".join(f"{s}={e[:60]}" for s, e in errors.items())
        )

    ended_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    emit("session_done", ok=len(symbols) - len(errors), errors=len(errors))

    return {
        "symbols": symbols,
        "macro_view": macro_view,
        "wealth_view": wealth_view,
        "event_brief": event_brief,
        "asset_committees": asset_committees,
        "errors": errors,
        "audit": {
            "shared_macro": True,
            "event_brief_source": event_brief_source,
            "event_brief_attached": bool(event_brief),
            "wealth_view_attached": bool(wealth_view),
            "max_debate_rounds": max_debate_rounds,
            "max_workers": effective_workers,
            "started_at": started_at,
            "ended_at": ended_at,
        },
    }
