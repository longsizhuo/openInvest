"""session — 单资产端到端 + 三路径单一 orchestrator（从 committee_runner.py 拆分，逻辑不变）。"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from core.committee import (
    load_wealth_context_view,
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

from core.runner.event_brief import _get_event_store, _resolve_event_brief, format_event_brief, resolve_event_brief_multi
from core.runner.loaders import _build_default_portfolio_summary, load_prior_insights, load_sentiment_brief, load_valuation_brief
from core.runner.intervention import _extract_regime_label, _gold_defense_dca_gate, _intervention_record, _log_intervention, _save_path_snapshot

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
    path_profile = None   # 结构化路径预测快照（path_review 事后校验用）
    try:
        from core.regime_probability import build_reentry_reference, convert_ccy_for
        # 币种自适应（ADR-021）：持仓以非报价币种计价（如 GC=F 报 USD、浙商积存金记 CNY）
        # → path-profile 用汇率卷积合成持仓币种下行口径，避免 USD 口径低估 CNY 持有者风险。
        try:
            _holding = pm.holdings.find(symbol)
            _convert_ccy = convert_ccy_for(symbol, (_holding or {}).get("cost_currency"))
        except Exception:  # noqa: BLE001  持仓币种取不到 → 退回本币口径，不影响 reentry 主体
            _convert_ccy = None
        reentry_reference, path_profile = build_reentry_reference(
            symbol, regime_label, current_price, convert_ccy=_convert_ccy,
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
        _per_asset_stance = event_stance_line_for_symbol(
            effective_event_brief, symbol,
            tracks=target.get("tracks"),  # issue #26: 用户可选声明追踪的指数
        )
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

    # 5.9. 黄金防御分批 DCA 闸（2026-06-13 裁决）：仅黄金(type==metal)且配置开启时算。
    # 防御触发时把"全拦黄金买入"改成"放行一批 or 按 spacing/quota 暂拦"——尊重黄金
    # 中位右偏(典型涨不该禁)，用时间分散吃厚左尾(挤兑坑)。两条腿(VIX/ATR)已在
    # run_committee 里 OR 成单 defense_flag_on，故只算一份合成计划（非各腿独立分批，
    # 否则会叠成 1/9）。非黄金/未启用 → defense_dca=None → 旧全拦行为。
    # _force_reload：长驻 scheduler 经 /api/config 动态改 verdict 类开关后,这里必须重读
    # 否则吃陈旧缓存静默失效（对齐 jobs/dca_daily.py 的写法）。
    _vcfg = _load_config(_force_reload=True).verdict
    defense_dca = None
    if target.get("type") == "metal" and _vcfg.gold_defense_dca_enabled:
        try:
            defense_dca = _gold_defense_dca_gate(
                symbol, df.index,
                n_tranches=_vcfg.gold_defense_dca_n_tranches,
                fraction=_vcfg.gold_defense_dca_fraction,
                min_spacing_days=_vcfg.gold_defense_dca_min_spacing_days,
                window_days=_vcfg.gold_defense_dca_window_days,
            )
            emit("gold_defense_dca_gate", asset=symbol, **defense_dca)
        except Exception as e:  # noqa: BLE001  闸算失败 → 退回 None=旧全拦（安全侧）
            log.warning(f"黄金 DCA 闸计算失败 graceful，退回全拦：{e}")
            defense_dca = None

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
        defense_dca=defense_dca,
        max_debate_rounds=max_debate_rounds,
        progress_callback=progress_callback,
    )

    # 路径分布参考随 result 返回——entry（邮件/GUI/CLI）渲染给用户，
    # 与 CIO prompt 里看到的是同一份文本（所见即所得，防口径分叉）。
    # path_profile 是同源结构化数据，给"一句话人话摘要"等确定性渲染用。
    result["path_reference"] = reentry_reference
    result["path_profile"] = path_profile

    # 6.5. 路径预测快照落盘（path_review 闭环：90 天后回看实际路径 vs 预测分布）。
    # graceful：失败不阻断。
    if path_profile is not None:
        try:
            _save_path_snapshot(
                symbol, regime_label, current_price,
                metrics.get("atr_pct"), path_profile,
            )
            emit("path_snapshot_saved", asset=symbol)
        except Exception as e:  # noqa: BLE001
            log.warning(f"path 快照落盘失败 graceful 跳过: {e}")

    # 6.6. 反事实记账：确定性规则改写了 CIO 裁决 → 落 interventions.jsonl
    # （"如果没拦会怎样"由 jobs/intervention_review.py 事后回填）。graceful。
    try:
        _rec = _intervention_record(
            symbol, regime_label, current_price,
            result.get("verdict"), atr_defense_on,
        )
        if _rec is not None:
            _log_intervention(_rec)
            emit("intervention_logged", asset=symbol, rule=_rec["rule"])
    except Exception as e:  # noqa: BLE001
        log.warning(f"干预记账失败 graceful 跳过: {e}")

    # 7. 查概率分布（按 asset×regime，regime 是信号 verdict 是噪声）
    if probability_table is not None:
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

__all__ = [
    "run_committee_for_symbol",
    "run_committee_session",
]
