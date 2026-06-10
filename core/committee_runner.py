"""端到端单资产委员会跑（live 端点用）

把 daily_report 的数据准备逻辑抽出来，让 web_api 能用 progress_callback 实时上报进度。
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable, Dict, List, Optional

from core.committee import (
    atr_defense_from_text,  # re-export: cmd_save_committee 提 ATR 防御腿用
    load_backup_cny,  # re-export: entry (daily_report / skill) 走 service layer
    load_wealth_context_view,
    parse_cio_memo,  # re-export: 给 entry (scripts.skill cmd_save_committee) 用，
                     # 避免 entry 直接 import core.committee 违反 lint-imports 契约
    regime_label_from_text,  # re-export: 同上，cmd_save_committee 提 regime 标签用
    run_committee,
    run_macro_view,
)
from core.portfolio_manager import PortfolioManager
from core.regime import classify_regime, format_regime_brief
from core.regime_probability import (
    RegimeProbability,
    build_probability_table_from_ohlc,
    get_regime_probability,
)
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


def load_sentiment_brief(event_brief: str = "") -> str:
    """市场情绪表盘 shared loader（确定性：VIX 分位保底 + CNN 锦上添花）。

    地位同 load_wealth_context_view：session 跑一次（VIX/CNN 是市场级、跨资产相同），
    结果注入每个 run_committee(..., sentiment_brief=...)。

    任何失败 graceful 退化空字符串，不阻断 committee。CNN 不可达时 VIX 分位照常输出
    （绝不单点故障，见 utils/sentiment.py 设计红线）。
    """
    try:
        from utils.sentiment import build_sentiment_brief
        return build_sentiment_brief(event_brief)
    except Exception as e:  # noqa: BLE001
        log.warning(f"load_sentiment_brief graceful 退化 '': {type(e).__name__}: {e}")
        return ""


def load_valuation_brief(
    symbol: str, price_quantile_2y: Optional[float] = None,
) -> str:
    """估值 shared loader（确定性：trailing PE + 价格分位，仅权益类，per-asset）。

    黄金/商品类返回 ""（它们的"基本面"=货币因素走 Macro）。任何失败 graceful 退化 ""。
    """
    try:
        from utils.valuation import build_valuation_brief
        return build_valuation_brief(symbol, price_quantile_2y)
    except Exception as e:  # noqa: BLE001
        log.warning(f"load_valuation_brief graceful 退化 '': {type(e).__name__}: {e}")
        return ""


def _build_default_portfolio_summary(pm: PortfolioManager) -> str:
    """service layer 默认 portfolio_summary 拼装（含集中度 + 总资产 + 浮盈）

    2026-05-19 修复：Direct 路径（scripts/skill.py:cmd_run_committee）调
    run_committee_session 时**没传** portfolio_summary_override，service layer
    之前自拼的简化版只 4 行，缺总资产 / 缺所有持仓 / 缺集中度数字。Risk Officer
    自己算集中度连续 6 天错（真实 33.4% → 算成 81.6%），推荐错误"减仓"。

    现在默认行为：
    1. 遍历 pm.holdings 一次性拉所有实仓 current_prices（5d yfinance close）。
       N 个资产 = N 次 get_history_data，多数用户 ≤5 个 holding，总耗时 < 5s。
    2. 用 utils.fx.total_portfolio_value_cny 多币种折算总资产。
    3. 调 utils.portfolio_summary.portfolio_summary_text 拼完整版（与 cron 路径
       daily_report.py 用同一个 helper）。

    cron 路径仍走 portfolio_summary_override（daily_report.py 自己拼的版本
    包含 data_warnings 陈旧告警，service layer 拿不到那些）。

    Args:
        pm: PortfolioManager 实例

    Returns:
        portfolio_summary 完整文本（含集中度数字）。任何子步骤失败都 graceful
        退化到旧版精简版，不阻断 committee 主流程。
    """
    try:
        from utils.exchange_fee import get_history_data
        from utils.fx import total_portfolio_value_cny
        from utils.portfolio_summary import portfolio_summary_text

        # 一次性拉所有实仓 current_prices。
        # 黄金 GC=F 必须走 get_gold_snapshot 反推 spot_cny_per_gram（与持仓的
        # unit=克、cost_currency=CNY 同口径）；直接用 get_history_data("GC=F")
        # 返回的是 USD/oz，会让市值算错百倍。其他 yfinance symbol 走 5d close。
        current_prices: Dict[str, float] = {}
        for h in pm.holdings:
            if h.get("is_tracking_only"):
                continue
            sym = str(h.get("symbol") or "")
            if not sym:
                continue
            units = float(h.get("units", 0) or 0)
            if units <= 0:
                continue
            try:
                if sym == "GC=F":
                    # 黄金：克价 in CNY（与持仓 cost_currency=CNY 同口径，
                    # 避免 USD/oz × FX 算错百倍）
                    from utils.gold_price import get_gold_snapshot
                    snap = get_gold_snapshot(offset_pct=0.0)
                    if snap is not None:
                        current_prices[sym] = float(snap.spot_cny_per_gram)
                else:
                    df = get_history_data(sym, "5d")
                    if df is not None and not df.empty:
                        current_prices[sym] = float(df["Close"].iloc[-1])
            except Exception as e:  # noqa: BLE001
                log.warning(
                    f"_build_default_portfolio_summary: {sym} 价拉取失败已跳过: "
                    f"{type(e).__name__}: {e}"
                )

        total_cny, _status = total_portfolio_value_cny(pm, current_prices, base="CNY")
        # backup_cny（off-portfolio 兜底）走单一可信源 loader，三路径一致
        backup_cny = load_backup_cny(pm)
        return portfolio_summary_text(pm, total_cny, current_prices, backup_cny=backup_cny)
    except Exception as e:  # noqa: BLE001
        # 兜底：拉价/折算彻底失败 → 用历史简化版，至少 Risk Officer 还有点上下文
        log.warning(
            f"_build_default_portfolio_summary 失败，退化到精简版: "
            f"{type(e).__name__}: {e}"
        )
        cash_cny = pm.cash_amount("CNY")
        aud_cash = pm.cash_amount("AUD")
        buffer_cny = float(pm.user.get("exchange_buffer_cny", 0) or 0)
        risk_level = str(pm.user.get("risk_tolerance", "Balanced"))
        dry_powder = max(0.0, cash_cny - buffer_cny)
        return (
            f"用户风险偏好: {risk_level}\n"
            f"CNY 现金: ¥{cash_cny:,.0f}（应急金 ¥{buffer_cny:,.0f}，"
            f"可投 ¥{dry_powder:,.0f}）\n"
            f"AUD 现金: ${aud_cash:,.0f}\n"
            f"⚠️ portfolio_summary 完整版构建失败，请勿据此做集中度判断"
        )


def _extract_regime_label(regime_brief: str) -> str:
    """从 format_regime_brief 输出提取 regime label（第一行 'REGIME: xxx'）"""
    for line in regime_brief.splitlines():
        if line.startswith("REGIME:"):
            return line.split(":", 1)[1].strip()
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
    sentiment_brief: Optional[str] = None,
    valuation_brief_override: Optional[str] = None,
    probability_table: Optional[Dict[tuple, RegimeProbability]] = None,
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
        sentiment_brief: 市场情绪表盘（VIX 分位 + CNN）。市场级跨资产相同，session
            一次性算好传进来；None → 内部调 load_sentiment_brief() fallback。
        valuation_brief_override: 估值（仅权益类）。per-asset，None → 内部调
            load_valuation_brief(symbol, price_quantile_2y)。
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
    # 2026-05-31: STRATEGY_HINT 不再给人写方向预设，改成中性引用该 regime 的 OHLC
    # 概率口径（中位 forward return / 跌破现价概率 / n）。先算好概率口径传进去，
    # 让 Quant 基于数据判断方向，不是凭空判断。读失败 graceful 退化无数字中性版。
    _classification = classify_regime(metrics, symbol=symbol)
    _regime_for_hint = _classification["regime"]
    # 独立快崩防御 ATR 腿（资产级，通用口径）：波动突变比 = 当日 ATR% / 自身
    # 近 1 年滚动中位，≥ sentiment.atr_defense_spike_ratio（默认 2.0=翻倍）触发。
    # 尺度无关（任何资产自校准，无 per-asset magic number），与 crash 分类完全
    # 解耦，且不等 30d 回撤确认——crash 锁永不触发的根因就是慢腿拖死快腿。
    # VIX 腿（市场级）在 sentiment_brief（INDEP_DEFENSE_FLAG），run_committee 里 OR。
    from core.config import load_config as _load_config
    _spike = metrics.get("atr_spike_ratio")
    _spike_min = _load_config().sentiment.atr_defense_spike_ratio
    atr_defense_on = bool(_spike is not None and _spike >= _spike_min)
    if atr_defense_on:
        emit("atr_defense_on", atr_spike_ratio=_spike, threshold=_spike_min)
    _prob_hint = None
    try:
        from core.regime_probability import get_regime_forward_summary
        _prob_hint = get_regime_forward_summary(
            symbol, _regime_for_hint, metrics.get("current_price"),
        )
    except Exception as e:  # noqa: BLE001  概率口径读失败不阻断委员会
        log.warning(f"regime forward summary 取失败，STRATEGY_HINT 退化无数字版: {e}")
    regime_brief = format_regime_brief(metrics, symbol=symbol, prob_hint=_prob_hint)
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

    # 5. WealthContextOfficer view（修复 2026-05-15 漂移: 之前没读 user.md 的
    # wealth_context, Risk Officer 永远按 portfolio cash 判风险）
    # caller 已经算过就 override 进来避免重复调用 LLM；否则 fallback 自己调
    # 先于 portfolio summary 加载，因为 portfolio_summary_text 需要 backup_cny
    # 来附注真实财富占比
    if wealth_context_view is not None:
        wealth_view = wealth_context_view
    else:
        wealth_view = load_wealth_context_view()
    if wealth_view:
        emit("wealth_context_loaded", preview=wealth_view[:240])

    # 5.1. Portfolio summary
    # 2026-05-19 修复 Direct 路径集中度漂移：之前 service layer 自拼简化版
    # （只 4 行: 风险/CNY/AUD/目标资产单位），没有总资产、没有所有持仓、没有集中度
    # 数字，Risk Officer 自己算集中度连续 6 天错算（NDQ 真实 33.4% → LLM 算成
    # 81.6%）。现在默认走 utils.portfolio_summary.portfolio_summary_text 拼完整版
    # （含每个 asset 的 concentration_pct + 总资产 + 浮盈），与 cron / Coordinator
    # 路径对齐。caller override 仍优先（cron daily_report 已经自己拼好了 +
    # data_warnings）。
    if portfolio_summary_override is not None:
        portfolio_summary = portfolio_summary_override
    else:
        portfolio_summary = _build_default_portfolio_summary(pm)

    # 5.6. Prior insights / Dreaming long-term 行为模式（修复 2026-05-16 漂移:
    # service layer 之前从不读 insights, Web/GUI 路径的 LLM 永远看不到长期模式）
    if prior_insights_override is not None:
        prior_insights = prior_insights_override
    else:
        prior_insights = load_prior_insights(target, pm)
    if prior_insights:
        emit("prior_insights_loaded", preview=prior_insights[:240])

    # 5.7. 卖出后路径 / 买回点参考（给 CIO 出 TRIM 时填 REENTRY_PRICE 的数据依据）
    # current_price 同时给 parse_cio_memo 的 Sanity check 5 做"买回点必须低于现价"校验
    current_price = metrics.get("current_price")
    regime_label = _extract_regime_label(regime_brief)
    reentry_reference = ""
    try:
        from core.regime_probability import build_reentry_reference_text
        reentry_reference = build_reentry_reference_text(
            symbol, regime_label, current_price,
        )
        if reentry_reference:
            emit("reentry_reference_loaded", asset=symbol, regime=regime_label)
    except Exception as e:  # noqa: BLE001  概率表读失败不能阻断委员会
        log.warning(f"reentry reference 构建失败 graceful 退化空: {e}")

    # 5.8. 确定性事实块（对齐 TradingAgents 维度，非投票 agent）
    # 情绪表盘：市场级，session 已算好就 override；否则 per-asset fallback
    if sentiment_brief is not None:
        effective_sentiment_brief = sentiment_brief
    else:
        effective_sentiment_brief = load_sentiment_brief(effective_event_brief)
    # per-asset 净情绪行：从该资产视角过滤 event_brief（头行 [syms] 含本 symbol），
    # 只改本地副本——session 共享的市场级 sentiment_brief 不变（与 valuation 的
    # 市场级/资产级分工同构）。仅基座非空才附加（守"VIX 拿不到整块降级"红线）。
    # 标的来自本函数 symbol 参数（← strategy.target_assets 动态解析），无硬编码。
    try:
        from utils.sentiment import event_stance_line_for_symbol
        _per_asset_stance = event_stance_line_for_symbol(effective_event_brief, symbol)
    except Exception as e:  # noqa: BLE001  聚合行失败不阻断委员会
        log.warning(f"per-asset EVENT_STANCE 行构建失败 graceful 跳过: {e}")
        _per_asset_stance = None
    if _per_asset_stance and effective_sentiment_brief:
        effective_sentiment_brief = effective_sentiment_brief + "\n" + _per_asset_stance
    if effective_sentiment_brief:
        emit("sentiment_brief_loaded", preview=effective_sentiment_brief[:240])
    # 估值：per-asset，仅权益类出（黄金/商品走 Macro 货币因素返 ""）
    if valuation_brief_override is not None:
        effective_valuation_brief = valuation_brief_override
    else:
        effective_valuation_brief = load_valuation_brief(
            symbol, metrics.get("price_quantile_2y"),
        )
    if effective_valuation_brief:
        emit("valuation_brief_loaded", preview=effective_valuation_brief[:240])

    # 6. 跑多轮辩论 + CIO
    result = run_committee(
        target,
        market_data=market_data,
        macro_view=macro_view,
        portfolio_summary=portfolio_summary,
        prior_insights=prior_insights,
        regime_brief=regime_brief,
        wealth_context_view=wealth_view,
        reentry_reference=reentry_reference,
        current_price=current_price,
        sentiment_brief=effective_sentiment_brief,
        valuation_brief=effective_valuation_brief,
        atr_defense_on=atr_defense_on,
        max_debate_rounds=max_debate_rounds,
        progress_callback=progress_callback,
    )

    # 7. 查概率分布（按 asset×regime，regime 是信号 verdict 是噪声）
    if probability_table is not None:
        regime_label = _extract_regime_label(regime_brief)
        prob = get_regime_probability(
            symbol, regime_label, table=probability_table,
        )
        if prob is not None:
            result["regime_probability"] = prob
            emit("regime_probability",
                 asset=symbol, regime=regime_label,
                 summary=prob.summary_line())

    return result


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
    sentiment_brief_override: Optional[str] = None,
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

    # ---- Step 4.4: shared sentiment_brief（市场级情绪表盘，跨资产相同，跑一次）----
    # VIX 分位 + CNN F&G 是市场级指标，N 资产共享一份，避免每个资产各拉一次 VIX/CNN。
    # event_brief 已在上面 resolve，传进去给净情绪聚合行（纯计数，无新 IO）。
    if sentiment_brief_override is not None:
        sentiment_brief = sentiment_brief_override
    else:
        sentiment_brief = load_sentiment_brief(event_brief)
    if sentiment_brief:
        emit("sentiment_brief_loaded", preview=sentiment_brief[:240])

    # ---- Step 4.5: 构建概率表（共享，只读一次）----
    # 数据源 = 几十年 OHLC 直算（纯算术，0 LLM token），不再用 verdict_review 276 条。
    # 原因：regime→forward return 全是算术，样本量 (asset,regime) 16~80 → 900~4150。
    # verdict_review 源保留给命中率/confidence 校准（那些需要 LLM 真实输出）。
    prob_table: Dict[tuple, RegimeProbability] = {}
    try:
        prob_table = build_probability_table_from_ohlc(symbols)
        if prob_table:
            emit("probability_table_loaded", groups=len(prob_table))
    except Exception as e:  # noqa: BLE001
        log.warning(f"概率表构建失败 graceful 退化空 dict: {e}")

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
            sentiment_brief=sentiment_brief,  # 市场级共享，避免每资产各拉 VIX/CNN
            probability_table=prob_table,
            # prior_insights_override 不传 → service 内 load_prior_insights(sym)
            # 每个资产 insights 不同，session 不预加载
            # valuation_brief_override 不传 → service 内 load_valuation_brief(sym)
            # 估值 per-asset（不同资产 PE 不同），session 不预加载
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
        "sentiment_brief": sentiment_brief,
        "asset_committees": asset_committees,
        "errors": errors,
        "audit": {
            "shared_macro": True,
            "event_brief_source": event_brief_source,
            "event_brief_attached": bool(event_brief),
            "wealth_view_attached": bool(wealth_view),
            "sentiment_brief_attached": bool(sentiment_brief),
            "max_debate_rounds": max_debate_rounds,
            "max_workers": effective_workers,
            "started_at": started_at,
            "ended_at": ended_at,
        },
    }
