"""coordinator — Coordinator 路径 prepare/save（Claude Code spawn subagent）（从 committee_runner.py 拆分，逻辑不变）。"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict

# prepare_committee_brief / save_committee_transcript 在模块作用域用到这些；
# exchange_fee / PortfolioManager 等改走函数内 call-time import（保持可被契约测试 patch）。
from openinvest.core.committee import (
    atr_defense_from_text,  # save_committee_transcript 提 ATR 防御腿
    load_backup_cny,
    load_wealth_context_view,
    parse_cio_memo,
    regime_label_from_text,
)
from openinvest.core.regime import classify_regime, format_regime_brief
from openinvest.utils.market_metrics import compute_metrics

log = logging.getLogger(__name__)

from openinvest.core.runner.loaders import load_prior_insights, load_sentiment_brief, load_valuation_brief

# ============================================================================
# Coordinator 路径 service functions — prepare / save（CLI 与 Web API 共享）
# ============================================================================
#
# 2026-06 远端模式（hub-and-spoke）重构：cmd_prepare_committee / cmd_save_committee
# 的计算体从 scripts/skill.py 提取到这里。scripts/skill.py（本地模式）与
# connectors/web_api.py（POST /api/committee/prepare|save，远端模式）都调同一份，
# 防止 entry 各自手搓 prep 的漂移（CLAUDE.md 漂移历史的根因模式）。
#
# ⚠️ 函数内的 PortfolioManager / get_history_data / portfolio_summary_text 等
# 必须保持**延迟 import（call-time 解析）**——tests/test_prepare_committee.py 的
# _mock_world 靠 monkeypatch 这些模块属性注入 fake；load_* 系列以模块全局名调用
# （同样可被 monkeypatch.setattr(cr, ...) 替换）。改成模块顶 import 会让契约测试失效。
# ============================================================================

_COMMITTEE_SECTION_RE = re.compile(
    r"^===\s*(MACRO|QUANT_R1|RISK_R1|QUANT_R2|RISK_R2|CIO|QUANT|RISK)\s*===\s*$",
    re.MULTILINE,
)


def _safe_close_latest(symbol: str) -> float:
    """最新收盘价（1d 失败退 5d）。延迟 import 保持 get_history_data 可被测试 patch"""
    from openinvest.utils.exchange_fee import get_history_data
    df = get_history_data(symbol, "1d")
    if df.empty:
        df = get_history_data(symbol, "5d")
    return float(df["Close"].iloc[-1]) if not df.empty else 0.0


def prepare_committee_brief(symbol: str) -> Dict[str, Any]:
    """输出 Investment Committee brief — 含项目原生 prompt + 用户上下文，给 Claude 扮演 4 角色

    返回自包含 dict（brief + 6 段 prompt 全内联），coordinator 的 4 个 subagent
    不需要读任何磁盘文件。symbol 不在 target_assets 时返回 status=error dict。
    """
    from openinvest.capabilities.committee.cio import build_cio_prompt
    from openinvest.capabilities.committee.macro_strategist import PROMPT_MACRO_STRATEGIST
    from openinvest.capabilities.committee.quant import build_quant_prompt
    from openinvest.capabilities.committee.risk_officer import build_risk_officer_prompt
    from openinvest.core.portfolio_manager import PortfolioManager
    from openinvest.utils.exchange_fee import (
        analyze_multi_timeframe, get_history_data, get_macro_data,
    )
    from openinvest.utils.gold_price import format_gold_report, get_gold_snapshot

    pm = PortfolioManager()
    target = next(
        (a for a in pm.strategy.get("target_assets", []) if a["symbol"] == symbol),
        None,
    )
    if target is None:
        return {
            "status": "error",
            "error": f"asset {symbol} not in strategy.target_assets",
            "hint": (
                f"先把 {symbol} 加进 strategy.md target_assets 再重试。"
                "GUI 策略页可以加，或参考 references/adding-assets.md 手动编辑。"
            ),
        }

    # 算 metrics + regime 一次，给 analyze_multi_timeframe 和 format_regime_brief 共用
    df_target = get_history_data(target["symbol"], "2y")
    metrics = compute_metrics(df_target)
    market = analyze_multi_timeframe(
        df_target,
        f"{target.get('display_name', target['symbol'])} ({target['symbol']})",
    )
    # P1-2: 传 symbol 让 regime 用 per-asset 阈值
    # 2026-05-31: STRATEGY_HINT 改中性引用 OHLC 概率口径（同 run_committee_for_symbol），
    # 不再给人写方向预设。算好概率口径传进去；读失败退化无数字中性版。
    _regime_for_hint = classify_regime(metrics, symbol=target["symbol"])["regime"]
    _prob_hint = None
    try:
        from openinvest.core.regime_probability import get_regime_forward_summary
        _prob_hint = get_regime_forward_summary(
            target["symbol"], _regime_for_hint, metrics.get("current_price"),
        )
    except Exception:  # noqa: BLE001  概率口径读失败不阻断
        pass
    regime_brief = format_regime_brief(metrics, symbol=target["symbol"], prob_hint=_prob_hint)
    macro_data = get_macro_data()
    snap = get_gold_snapshot(offset_pct=0.0)
    gold_ctx = format_gold_report(snap) if (snap and target.get("type") == "metal") else ""

    # 2026-05-19 (A7 修复): 之前手搓 portfolio_summary 写死 NDQ + 黄金，fork 用户的
    # AAPL/0700.HK/BTC-USD 完全不进 Risk Officer 视野；total_cny 漏算非 NDQ/gold
    # 持仓 → 集中度严重低估。改用 daily_report_builder.portfolio_summary_text +
    # utils.fx.total_portfolio_value_cny，跟 cron 路径 (daily_report) 用同一套
    # 通用化逻辑（动态遍历所有 holdings，多币种 to_base 折算）。
    # import 路径保持 jobs.daily_report_builder（re-export）——契约测试 patch 的是它
    from openinvest.jobs.daily_report_builder import portfolio_summary_text
    from openinvest.utils.fx import total_portfolio_value_cny

    gold_now = snap.spot_cny_per_gram if snap else 0.0
    # 通用化 current_prices：所有 holdings 拉实时价，黄金特殊处理用 spot_cny_per_gram
    current_prices: Dict[str, float] = {}
    for h in pm.holdings:
        sym = str(h.get("symbol") or "")
        if not sym or h.get("is_tracking_only"):
            continue
        if sym == "GC=F":
            current_prices[sym] = gold_now
        else:
            current_prices[sym] = _safe_close_latest(sym)

    total_cny, _value_status = total_portfolio_value_cny(pm, current_prices, base="CNY")
    # backup_cny（off-portfolio 兜底）/ insights / wealth view / 确定性事实块全部
    # 以模块全局名调用 shared loaders（单一可信源 + 可被契约测试 monkeypatch）
    _backup_cny = load_backup_cny(pm)
    portfolio_summary = portfolio_summary_text(pm, total_cny, current_prices, backup_cny=_backup_cny)
    insights = load_prior_insights(target, pm)

    # 2026-05-18 漂移修复: skill 路径之前没接 wealth_context_view，Risk Officer
    # 永远看不到 family_backup / account_purpose，按 PWM 老逻辑误判超配。
    wealth_view = load_wealth_context_view()

    # 2026-06-11 漂移修复（coordinator 防御链对齐）: direct 路径在 service layer
    # 加载的确定性事实块，coordinator 路径此前没产出 —— CIO 看不到 VALUATION/
    # MARKET SENTIMENT（INDEP_DEFENSE_FLAG 不进 transcript → save_committee 的
    # 确定性防御降级永不触发）、EXPECTED_PATH 没有路径参考可引用。
    # 与 run_committee_for_symbol step 5.8 同源 loader（graceful 退化空串）。
    sentiment_brief = load_sentiment_brief()
    valuation_brief = load_valuation_brief(
        target["symbol"], metrics.get("price_quantile_2y"),
    )
    reentry_reference = ""
    path_profile = None   # 结构化路径预测快照——与 session 路径对齐（issue: 两 entry 别漂移）
    try:
        from openinvest.core.regime_probability import build_reentry_reference, convert_ccy_for
        # 币种自适应（ADR-021）：与 service layer 同源——持仓非报价币种时按持仓币种附下行口径。
        try:
            _holding = pm.holdings.find(target["symbol"])
            _convert_ccy = convert_ccy_for(target["symbol"], (_holding or {}).get("cost_currency"))
        except Exception:  # noqa: BLE001  持仓币种取不到 → 退回本币口径，不影响 reentry 主体
            _convert_ccy = None
        # 用 *_reference（非 *_text）取回结构化 profile，与 session.py 同口径：daily report
        # 的一句话人话摘要(daily_report_builder 读 path_profile)在两条 entry 上才一致。
        reentry_reference, path_profile = build_reentry_reference(
            target["symbol"], _regime_for_hint, metrics.get("current_price"),
            convert_ccy=_convert_ccy,
        )
    except Exception:  # noqa: BLE001  路径参考读失败不阻断 prepare
        pass

    return {
        "asset": target,
        "portfolio_summary": portfolio_summary,
        "wealth_context_view": wealth_view,  # Claude worker 必须把这个塞进 Risk Officer R1/R2 prompt
        "macro_data": macro_data,
        "market_data": market,
        "regime_brief": regime_brief,  # Claude worker 必须把这个塞进 Quant Round 1/2 prompt
        # 确定性事实块（2026-06）: sentiment 进 Quant+CIO（INDEP_DEFENSE_FLAG 必须
        # 原样进 transcript，save_committee 靠它做防御降级）；valuation 仅权益类非空
        "sentiment_brief": sentiment_brief,
        "valuation_brief": valuation_brief,
        # CIO 的"卖出后路径/买回点参考"（30/60/90 多窗+路径形状），EXPECTED_PATH 必须引用
        "reentry_reference": reentry_reference,
        # 结构化 path_profile：与 session 路径一致，供 daily report 一句话摘要等确定性渲染
        "path_profile": path_profile,
        "gold_snapshot": gold_ctx,
        "prior_insights": insights,
        "prompts": {
            "macro_strategist": PROMPT_MACRO_STRATEGIST,
            "quant_round1": build_quant_prompt(target, "opening"),
            "risk_round1": build_risk_officer_prompt(target, "opening"),
            "quant_round2_after_risk": build_quant_prompt(target, "rebuttal"),
            "risk_round2_after_quant": build_risk_officer_prompt(target, "rebuttal"),
            "cio": build_cio_prompt(target),
        },
        "save_command": (
            f"~/.claude/skills/invest/run.sh save_committee {symbol}"
        ),
        "instructions": (
            "Claude: 这是 Investment Committee 的 3 轮流程：\n"
            "  Round 1 - 独立陈述: Macro (跨资产共享) + Quant + Risk Officer 各自看自己的数据\n"
            "  Round 2 - 横向交流: Quant 看到 Risk 报告后调整 + Risk 看到 Quant 报告后调整\n"
            "  Round 3 - CIO 综合 4 份输出 + portfolio_summary，输出完整 memo\n"
            "**Quant 必须塞 regime_brief + 确定性事实块**: 召唤 Quant Round 1/2 worker 时，prompt 模板:\n"
            '  "<paste prompts.quant_round1>\\n\\n# 市场 Regime:\\n<paste regime_brief>'
            '\\n\\n# 估值 (确定性事实):\\n<paste valuation_brief>'
            '\\n\\n# 市场情绪表盘 (确定性事实):\\n<paste sentiment_brief>'
            '\\n\\n# 市场数据:\\n<paste market_data>"\n'
            "  Quant 基于 regime 概率口径 + 当前指标自行判断 SIGNAL（无方向硬锁；集中度归 Risk 管）。\n"
            "  valuation_brief 为空串=非权益类（黄金等走 Macro 货币因素），该段跳过。\n"
            "**Risk Officer 必须塞 wealth_context_view**（2026-05-18 漂移修复）:\n"
            '  "<paste prompts.risk_round1>\\n\\n# 用户持仓:\\n<paste portfolio_summary>'
            '\\n\\n# Wealth Context (off-portfolio 真实流动性):\\n<paste wealth_context_view>'
            '\\n\\n# 长期模式:\\n<paste prior_insights>"\n'
            "  确保 Risk Officer 能拿到 SOLVENCY_BUFFER_LEVEL（family_backup_available\n"
            "  + account_purpose 折算后的真实流动性等级），不按 PWM 老逻辑误判超配。\n"
            "  Round 2 Risk 同样塞，让升级判断仍基于正确的 buffer level。\n"
            "**CIO 必须塞确定性事实块 + 路径参考**（2026-06 防御链/路径化）: CIO prompt 末尾追加:\n"
            '  "\\n\\n=== VALUATION (确定性事实，必须纳入) ===\\n<paste valuation_brief>'
            '\\n\\n=== MARKET SENTIMENT 表盘 (确定性事实，必须纳入) ===\\n<paste sentiment_brief>'
            '\\n\\n=== 卖出后路径 / 买回点参考 ===\\n<paste reentry_reference>"\n'
            "  ⚠️ sentiment_brief 的 INDEP_DEFENSE_FLAG 行和 regime_brief 的 INPUTS 行\n"
            "  （含 atr_spike_ratio）必须**原样**进最终 transcript —— save_committee 靠\n"
            "  这些确定性行做防御降级/风险档后处理，缺了整条防御链失效。\n"
            "请依次扮演 6 段输出，用以下分隔符：\n"
            "=== MACRO ===\n=== QUANT_R1 ===\n=== RISK_R1 ===\n"
            "=== QUANT_R2 ===\n=== RISK_R2 ===\n=== CIO ===\n"
            f"全部完成后通过 stdin 喂给 save_committee {symbol}"
        ),
    }


def save_committee_transcript(symbol: str, raw: str) -> Dict[str, Any]:
    """把 coordinator 模式产出的 6 段 transcript 落到 memory/.committee/<date>/<asset>.md

    解析 CIO 段 → parse_cio_memo（含确定性防御降级后处理）→ 落盘 + dream_event。
    返回 {"saved": <path>, "verdict": {...}}。
    """
    from datetime import datetime

    from openinvest.core.memory_store import MemoryStore

    parts = _COMMITTEE_SECTION_RE.split(raw)
    sections: Dict[str, str] = {}
    if len(parts) > 1:
        for i in range(1, len(parts), 2):
            role = parts[i].strip()
            content = parts[i + 1].strip() if i + 1 < len(parts) else ""
            sections[role] = content

    cio_text = sections.get("CIO", raw if not sections else "")

    # risk_profile / 快崩防御 后处理输入（与 direct 路径同款）：transcript 里有
    # 粘贴进去的确定性 regime_brief（REGIME:/INPUTS:/THRESHOLDS: 行）和
    # sentiment_brief（INDEP_DEFENSE_FLAG 行）
    verdict = parse_cio_memo(
        cio_text,
        regime=regime_label_from_text(raw),
        defense_flag_on=(
            "INDEP_DEFENSE_FLAG: on" in raw or atr_defense_from_text(raw)
        ),
    )

    store = MemoryStore()
    today = datetime.now().strftime("%Y-%m-%d")
    out_dir = store.root / ".committee" / today
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_sym = re.sub(r"[^a-zA-Z0-9_-]", "_", symbol)
    path = out_dir / f"{safe_sym}.md"

    lines = [
        f"# Committee: {symbol}",
        f"\n**Date**: {today}",
        f"**Symbol**: {symbol}",
        "**Provider**: claude (skill mode)",
        f"**Verdict**: {verdict['verdict']} (confidence {verdict['confidence']:.2f})",
        f"**Dominant view**: {verdict['dominant_view']}",
        f"**Suggested allocation CNY**: {verdict['alloc_cny']}",
        "\n\n---\n\n## Reports\n",
    ]
    for role in ["MACRO", "QUANT_R1", "RISK_R1",
                 "QUANT_R2", "RISK_R2", "CIO", "QUANT", "RISK"]:
        if role in sections:
            lines.append(f"\n### {role}\n\n{sections[role]}\n")
    if not sections:
        lines.append(f"\n### RAW (未分段)\n\n{raw}\n")

    path.write_text("\n".join(lines), encoding="utf-8")
    store.dream_event({
        "phase": "committee_finished_skill",
        "asset": symbol,
        "verdict": verdict["verdict"],
        "confidence": verdict["confidence"],
        "provider": "claude",
    })
    return {"saved": str(path), "verdict": verdict}

__all__ = [
    "_COMMITTEE_SECTION_RE",
    "_safe_close_latest",
    "prepare_committee_brief",
    "save_committee_transcript",
]
