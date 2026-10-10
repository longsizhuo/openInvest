"""debate — 委员会主流程：CommitteeReport + 多轮 cross-challenge 编排（从 core/committee.py 拆分，逻辑逐字不变）。

职责：`CommitteeReport` dataclass（含 to_cio_brief）+ 收敛判定（`_extract_signal_strength`
/ `_check_convergence`）+ 辩论历史拼装 `_format_debate_history` + 主入口 `run_committee`
（Round 1 独立陈述 → Round 2..N cross-challenge → CIO 综合 → parse + persist）。

关键：run_committee 在本模块命名空间内解析 `_create_agent` / `_ask` / `_parallel_ask`
（agent_io）、`parse_cio_memo` / `regime_label_from_text` / `_extract_concentration_from_summary`
/ `_override_concentration_in_risk_output`（cio_parse）、`_persist`（persist），因此
这些名字必须在本模块顶部用 `from core.committee.<sub> import <name>` 引入（落进 debate
命名空间）——测试/脚本的 monkeypatch 钉的就是 core.committee.debate.* 这些名字。
绝不写 `from core.committee import ...`（会触发半初始化包反向求值 → ImportError）。
"""
from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from openinvest.capabilities.committee.cio import build_cio_prompt
from openinvest.capabilities.committee.quant import build_quant_prompt
from openinvest.capabilities.committee.risk_officer import build_risk_officer_prompt
from openinvest.capabilities.committee.scribe import build_scribe_prompt
from openinvest.core.committee.agent_io import (
    AGENT_UNAVAILABLE_MARKER,
    _ask,
    _create_agent,
    _parallel_ask,
)
from openinvest.core.committee.cio_parse import (
    _extract_concentration_from_summary,
    _override_concentration_in_risk_output,
    parse_cio_memo,
    regime_label_from_text,
)
from openinvest.core.committee.persist import _persist
from openinvest.utils.advisory import is_no_verdict_mode

log = logging.getLogger(__name__)

# 辩论纯核已迁 debate_calc（ADR-026）——导回保持历史导出面（含 façade __all__ 链）
from openinvest.core.committee.debate_calc import (  # noqa: F401
    _SIGNAL_RE,
    _STRENGTH_RE,
    _check_convergence,
    _extract_signal_strength,
    _format_debate_history,
)


@dataclass
class CommitteeReport:
    """4 角色 + cross-challenge round 的完整输出"""
    asset: Dict[str, Any]
    macro_view: str = ""              # 跨资产共享
    quant_view: str = ""              # Round 1: Quant 独立陈述
    risk_view: str = ""               # Round 1: Risk Officer 独立陈述
    quant_adjusted: str = ""          # Round 2: Quant 看到 Risk 后调整
    risk_adjusted: str = ""           # Round 2: Risk 看到 Quant 后调整
    cio_memo: str = ""                # Round 3: CIO 综合
    market_data: str = ""
    portfolio_summary: str = ""
    prior_insights: str = ""
    sentiment_brief: str = ""         # 市场情绪表盘（确定性：VIX 分位 + CNN F&G，跨资产共享）
    valuation_brief: str = ""         # 估值（确定性：trailing PE + 价格分位，仅权益类，per-asset）

    def to_cio_brief(self) -> str:
        """组装给 CIO 看的输入 - 含 cross-challenge round 后的调整"""
        lines = [
            f"=== ASSET: {self.asset.get('display_name', self.asset.get('symbol'))} ===",
            f"\n=== MACRO STRATEGIST (跨资产共享) ===\n{self.macro_view}",
        ]
        # 确定性事实块（必须纳入推理，非投票）：估值 + 情绪表盘
        if self.valuation_brief:
            lines.append(
                f"\n=== VALUATION (确定性事实，必须纳入'贵不贵'判断) ===\n{self.valuation_brief}"
            )
        if self.sentiment_brief:
            lines.append(
                f"\n=== MARKET SENTIMENT 表盘 (确定性事实，必须纳入) ===\n{self.sentiment_brief}"
            )
        lines.extend([
            "\n=== ROUND 1 (独立陈述) ===",
            f"\n--- QUANT ---\n{self.quant_view}",
            f"\n--- RISK OFFICER ---\n{self.risk_view}",
            "\n=== ROUND 2 (cross-challenge 后的调整) ===",
            f"\n--- QUANT 调整 ---\n{self.quant_adjusted}",
            f"\n--- RISK 调整 ---\n{self.risk_adjusted}",
            f"\n=== USER PORTFOLIO CONTEXT ===\n{self.portfolio_summary}",
        ])
        if self.prior_insights:
            lines.append(f"\n=== LONG-TERM INSIGHTS (Dreaming) ===\n{self.prior_insights}")
        return "\n".join(lines)










# 无裁决模式的服务端逐字闸（未经许可不得向公众提供金融领域的确定性结论）。
# 书记员 prompt 是第一道，这里是第二道：命中任意一条 = 纪要里有结论性表述。
# ponytail: 关键词黑名单，挡得住 LLM 常见的结论句式，挡不住刻意改写；要更稳就加 LLM 二审
_VERDICT_LANGUAGE = re.compile(
    "|".join([
        # 裁决枚举；中文紧挨英文时 \b 不成立（汉字也是 \w），所以只看两侧是不是 ASCII 字母
        r"(?<![A-Za-z])(?:STRONG[_ ]?BUY|BUY|SELL|HOLD|ACCUMULATE|TRIM)(?![A-Za-z])",
        r"(?:建议|应该|应当|可以考虑|可考虑|不妨|适合|值得)[^。；，,\n]{0,6}?"
        r"(?:买|卖|加仓|减仓|建仓|清仓|持有|止损|止盈|抄底|观望|入场|离场|配置|定投|赎回|申购|上车)",
        r"仓位|首笔|目标价|止损[价位线点]|止盈[价位线点]|买回[价点]|[买卖]点|入场点|胜率",
        r"(?:支撑|阻力)\s*(?:位|价|线)?\s*[¥￥]?\d|\d\s*(?:附近|一线|的)?\s*(?:支撑|阻力)",
        r"[¥￥]\s*\d|\d[\d,]*(?:\.\d+)?\s*(?:万?元|块钱)",
        r"\d+(?:\.\d+)?\s*%\s*的?(?:概率|可能性|机会)|概率\s*(?:约|为|是|有|高达|只有|仅)?\s*\d",
        r"(?<![A-Za-z])(?:should|recommend(?:ed|s)?|consider)\s+"
        r"(?:buy|sell|add|trim|hold|buying|selling|adding|trimming|holding)(?![A-Za-z])",
    ]),
    re.IGNORECASE,
)
_NO_VERDICT_DISCLAIMER = (
    "\n\n---\n以上是多个 AI 角色辩论的整理，不构成投资建议。要不要做、做多少，由你自己决定。"
)


# 闸查的串必须就是用户看到的串（parser differential）：纪要按 Markdown 渲染，
# `建*议*买入` 渲染出来是"建议买入"，零宽字符 / 全角字母 / HTML 实体 / 繁体同理。
# 新闻源在顾问模式下群友可加，经 Macro 进书记员上下文，是现实的注入入口。
_TRAD_TO_SIMP = str.maketrans(
    "議應該當慮適買賣倉減損場離觀贖購車筆標價點勝撐萬塊錢機會約為達僅線",
    "议应该当虑适买卖仓减损场离观赎购车笔标价点胜撑万块钱机会约为达仅线",
)
# 纪要里没有任何正当理由出现 HTML 标签/注释、HTML 实体、格式控制字符（零宽/双向覆盖/软连字符）
_SUSPICIOUS_MARKUP = re.compile(r"<\s*/?\s*[A-Za-z!][^>]*>|&(?:#\d+|#x[0-9A-Fa-f]+|[A-Za-z]+);")


def normalize_summary(text: str) -> str:
    """闸和出口共用的规范形：NFKC（全角→半角）+ 繁→简（闸关键词涉及的字）。"""
    return unicodedata.normalize("NFKC", text or "").translate(_TRAD_TO_SIMP)


def find_verdict_language(text: str) -> List[str]:
    """纪要里命中闸的片段（去重排序）；空列表 = 放行。

    查两个视图：规范形本身，以及去掉空白 / Markdown 强调符 / 链接语法后的"渲染视图"
    （防 `建*议*买入`、`建 议 买 入`、`[建](x)议买入`）。可疑标记直接算命中。
    ponytail: 同形异码字（西里尔字母冒充拉丁字母）不处理——要防就加 confusables 表
    """
    t = normalize_summary(text)
    rendered = re.sub(r"[\s*_~`]+", "", re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t))
    hits = {m.group(0) for view in (t, rendered) for m in _VERDICT_LANGUAGE.finditer(view)}
    if _SUSPICIOUS_MARKUP.search(t) or any(unicodedata.category(c) == "Cf" for c in t):
        hits.add("<可疑标记>")
    return sorted(hits)


def _scribe_summary(
    asset: Dict[str, Any],
    brief: str,
    debate_meta: Dict[str, Any],
    emit: Callable[..., None],
) -> Dict[str, Any]:
    """无裁决模式：书记员替代 CIO，只整理正反理由。

    命中闸 → 带着命中片段重写一次；还命中或 LLM 不可用 → 整份拦下返回 error（fail closed，
    不做局部删改——删掉半句的纪要比没有更误导）。不落盘：transcript 里有各角色的建议原话。
    """
    sym = asset["symbol"]
    emit("scribe_start", asset=sym)
    summary, hits = "", []
    for attempt in (1, 2):
        ask = brief
        if hits:
            ask += (
                "\n\n=== 上一版被服务端拦下 ===\n出现了禁止的表述："
                + "、".join(hits)
                + "。重写：只保留理由，不要任何结论、金额、仓位、价格目标、概率数字，"
                + "不要 HTML 或特殊控制字符。"
            )
        agent = _create_agent(
            build_scribe_prompt(asset), search_enabled=False, temperature=0.2,
            role="scribe", asset=sym, round_label=f"scribe_{attempt}",
        )
        summary = normalize_summary(_ask(agent, ask))   # 出口用的就是闸查过的这一份
        hits = find_verdict_language(summary)
        if not hits and AGENT_UNAVAILABLE_MARKER not in summary:
            emit("scribe_done", asset=sym)
            return {"asset": sym, "debate_summary": summary.strip() + _NO_VERDICT_DISCLAIMER,
                    "debate": debate_meta}
        log.warning(f"[no-verdict] {sym} 书记员第 {attempt} 版未放行: hits={hits}")
    emit("scribe_blocked", asset=sym)
    return {"asset": sym, "error": "辩论纪要未通过无裁决检查，已拦截", "debate": debate_meta}


def run_committee(
    asset: Dict[str, Any],
    market_data: str,
    macro_view: str,
    portfolio_summary: str,
    prior_insights: str = "",
    regime_brief: str = "",
    reentry_reference: str = "",
    current_price: Optional[float] = None,
    sentiment_brief: str = "",
    valuation_brief: str = "",
    *,
    atr_defense_on: bool = False,
    defense_dca: Optional[Dict[str, Any]] = None,
    persist_to_memory: bool = True,
    max_debate_rounds: int = 1,
    progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """对单个资产跑 Macro + Quant/Risk 多轮辩论 + CIO

    Args:
        regime_brief: core.regime.format_regime_brief 的输出
        atr_defense_on: 独立快崩防御的 ATR 腿（资产级，atr_pct ≥ per-asset
            sentiment.atr_defense_spike_ratio，判定在 core/runner/session.py，调用方算好传入）。
            与 sentiment_brief 里的 VIX 哨兵（市场级）OR 后进 parse_cio_memo
            做确定性买侧降级。
        defense_dca: 黄金防御分批 DCA 闸（2026-06-13 裁决，wiki18 §5）。非 None =
            本资产是黄金且 gold_defense_dca_enabled，service 层算好的本批放行/暂拦决定
            （{"allow", "fraction", "reason", "tranche_idx"}）。防御触发时改"全拦"为
            "放行一批 or 按 spacing/quota 拦"。None = 非黄金/未启用 → 旧全拦行为。
        max_debate_rounds: cross-challenge 轮数上限。
            - 1 = 旧行为（仅 1 轮 Round 2 cross-challenge），daily_report 用
            - 4 = 真讨论模式（live 端点用），允许 4 轮拉锯，收敛后提前退出
        progress_callback: 每个 stage 完成时调用，传 dict {phase, ...extra}
            phase 取值：
              - "round_1_start" / "round_1_done"
              - "round_N_start" / "round_N_done" (N=2..max_debate_rounds)
              - "converged" (提前退出)
              - "cio_start" / "cio_done"
    """
    sym = asset["symbol"]

    def emit(phase: str, **extra: Any) -> None:
        if progress_callback is None:
            return
        try:
            progress_callback({"phase": phase, **extra})
        except Exception as e:  # noqa: BLE001  callback 出错不能阻断主流程
            log.warning(f"progress callback fail: {e}")

    report = CommitteeReport(
        asset=asset,
        macro_view=macro_view,
        market_data=market_data,
        portfolio_summary=portfolio_summary,
        prior_insights=prior_insights,
        sentiment_brief=sentiment_brief,
        valuation_brief=valuation_brief,
    )

    quant_history: List[str] = []   # Round 1, 2, ..., N
    risk_history: List[str] = []
    regime_section = (
        f"# 市场 Regime (事实背景 + 历史概率参考):\n{regime_brief}\n\n"
        if regime_brief else ""
    )

    # ===== Round 1: Quant 和 Risk 独立陈述（信息分隔 + 真并行）=====
    emit("round_1_start", round=1, mode="independent")

    valuation_section = (
        f"# 估值 (确定性事实，必须纳入'贵不贵'判断):\n{valuation_brief}\n\n"
        if valuation_brief else ""
    )
    sentiment_section = (
        f"# 市场情绪表盘 (确定性事实，必须纳入):\n{sentiment_brief}\n\n"
        if sentiment_brief else ""
    )
    quant_input_r1 = (
        f"# 资产: {asset.get('display_name', sym)} ({sym})\n"
        f"{regime_section}"
        f"# 市场数据 (技术指标 + 多周期):\n{market_data}\n\n"
        f"{valuation_section}"
        f"{sentiment_section}"
        f"请按 Quant Analyst 格式输出技术信号。"
    )
    risk_input_r1 = (
        f"# 资产: {asset.get('display_name', sym)} ({sym})\n"
        f"# 用户当前持仓:\n{portfolio_summary}\n\n"
        f"# 长期行为模式 (Dreaming):\n{prior_insights or '(暂无)'}\n\n"
        f"请按 Risk Officer 格式输出风险评估。"
    )
    quant_agent_r1 = _create_agent(
        build_quant_prompt(asset, "opening"), search_enabled=False,
        role="quant", asset=sym, round_label="opening",
    )
    risk_agent_r1 = _create_agent(
        build_risk_officer_prompt(asset, "opening"), search_enabled=False,
        role="risk", asset=sym, round_label="opening",
    )
    quant_r1, risk_r1 = _parallel_ask([
        (quant_agent_r1, quant_input_r1),
        (risk_agent_r1, risk_input_r1),
    ])
    # SENTINEL 覆写：portfolio_summary 字面给了集中度数字，LLM 仍偶发 hallucinate
    # (2026-05-20 某持仓真实 33.6% 被编成 70.2%)。这里强制改回真值。
    _true_conc = _extract_concentration_from_summary(portfolio_summary, sym)
    risk_r1 = _override_concentration_in_risk_output(risk_r1, _true_conc)
    quant_history.append(quant_r1)
    risk_history.append(risk_r1)
    report.quant_view = quant_r1
    report.risk_view = risk_r1
    emit("round_1_done",
         round=1,
         quant_preview=quant_r1[:240],
         risk_preview=risk_r1[:240],
         quant_signal=_extract_signal_strength(quant_r1),
         risk_signal=_extract_signal_strength(risk_r1))

    # ===== Round 2..N: cross-challenge（每轮内 Quant + Risk 并行）=====
    converged = False
    final_round = 1
    for round_idx in range(2, max(2, max_debate_rounds + 1)):
        emit(f"round_{round_idx}_start", round=round_idx, mode="cross_challenge")

        debate_block = _format_debate_history(quant_history, risk_history)

        quant_input_rN = (
            regime_section
            + f"# 现在是第 {round_idx} 轮 cross-challenge（最多 {max_debate_rounds} 轮）\n\n"
            + debate_block
            + "\n\n# 任务\n"
            + "请基于完整辩论历史调整或维持你的 SIGNAL/STRENGTH。"
            + "REGIME 历史概率分布见 system prompt，作为背景数据参考（无方向硬锁）。"
            + "如果你认为意见已经稳定，重申当前判断即可。"
        )
        risk_input_rN = (
            f"# 现在是第 {round_idx} 轮 cross-challenge（最多 {max_debate_rounds} 轮）\n\n"
            + debate_block
            + "\n\n# 任务\n"
            + "请基于 Quant 的最新技术信号调整或维持你的止损建议和风险评级。"
            + "如果你认为意见已经稳定，重申当前判断即可。"
        )

        quant_agent_rN = _create_agent(
            build_quant_prompt(asset, "rebuttal"), search_enabled=False,
            temperature=0.2,
            role="quant", asset=sym, round_label=f"round_{round_idx}",
        )
        risk_agent_rN = _create_agent(
            build_risk_officer_prompt(asset, "rebuttal"), search_enabled=False,
            temperature=0.2,
            role="risk", asset=sym, round_label=f"round_{round_idx}",
        )
        quant_rN, risk_rN = _parallel_ask([
            (quant_agent_rN, quant_input_rN),
            (risk_agent_rN, risk_input_rN),
        ])
        # SENTINEL 覆写：Round 2+ 同样保护，防 LLM 在 rebuttal 里又编出新的集中度
        risk_rN = _override_concentration_in_risk_output(risk_rN, _true_conc)
        quant_history.append(quant_rN)
        risk_history.append(risk_rN)
        final_round = round_idx
        emit(f"round_{round_idx}_done",
             round=round_idx,
             quant_preview=quant_rN[:240],
             risk_preview=risk_rN[:240],
             quant_signal=_extract_signal_strength(quant_rN),
             risk_signal=_extract_signal_strength(risk_rN))

        # 检查收敛（至少需要 2 轮才能比较）
        if _check_convergence(quant_history, risk_history):
            converged = True
            emit("converged", at_round=round_idx)
            break

    # 兼容 report 旧字段（CIO 看的 brief 用最后一轮）
    if len(quant_history) > 1:
        report.quant_adjusted = quant_history[-1]
    if len(risk_history) > 1:
        report.risk_adjusted = risk_history[-1]

    if is_no_verdict_mode():
        brief = report.to_cio_brief()
        if len(quant_history) > 1:
            brief += (
                "\n\n=== 完整辩论历史（含所有 cross-challenge 轮）===\n"
                + _format_debate_history(quant_history, risk_history)
            )
        return _scribe_summary(asset, brief, {
            "max_rounds": max_debate_rounds, "final_round": final_round, "converged": converged,
        }, emit)

    # ===== CIO 综合所有 =====
    emit("cio_start", asset=sym)
    # P3 A/B: INVEST_CIO_THINKING=1 给终裁 CIO 开思考模式（分析师仍 fast path）。
    # 默认关——委员会 4 worker 已思考过，且无 alpha 证据下加思考未必值（见 wiki 17）。
    _cio_thinking = os.getenv("INVEST_CIO_THINKING") == "1"
    # P1: CIO 走 DeepSeek JSON Output 结构化裁决，替正则解析（治 TRIM 负号等一类 bug）。
    # 与思考 A/B 互斥（思考路径留文本+regex）。不支持的 provider 由下方运行时优雅回退兜底。
    from openinvest.utils.llm import supports_json_output
    _want_json = supports_json_output() and not _cio_thinking
    cio_agent = _create_agent(
        build_cio_prompt(asset, json_mode=_want_json), search_enabled=False, temperature=0.1,
        role="cio", asset=sym, round_label="cio", enable_thinking=_cio_thinking,
        response_format=({"type": "json_object"} if _want_json else None),
    )
    # CIO 看完整辩论历史（不只是最后一轮），让它能识别 agent 是否在讨论中漂移
    cio_brief = report.to_cio_brief()
    if len(quant_history) > 1:
        cio_brief += (
            "\n\n=== 完整辩论历史（含所有 cross-challenge 轮）===\n"
            + _format_debate_history(quant_history, risk_history)
        )
        cio_brief += (
            f"\n\n=== 辩论元信息 ===\n"
            f"实际跑了 {final_round} 轮，"
            f"{'已收敛（连续 2 轮意见稳定）' if converged else '未收敛（达到上限）'}。"
        )
    if reentry_reference:
        cio_brief += f"\n\n=== 卖出后路径 / 买回点参考 ===\n{reentry_reference}"
    report.cio_memo = _ask(cio_agent, cio_brief)

    # P1 优雅回退：JSON 模式下试解析；provider 没真支持 / 没吐合法 JSON（含 _ask 失败返回
    # 的 [WORKER_UNAVAILABLE] 哨兵）→ 退回文本模式重问一次，走 regex。DeepSeek 正常一次过。
    cio_fields: Optional[Dict[str, Any]] = None
    if _want_json:
        try:
            cio_fields = json.loads(report.cio_memo)
            if not isinstance(cio_fields, dict):
                cio_fields = None
        except (ValueError, TypeError):
            cio_fields = None
        if cio_fields is None:
            log.warning("CIO JSON Output 未拿到合法 JSON（provider 不支持？）→ 优雅回退文本模式重问")
            cio_agent = _create_agent(
                build_cio_prompt(asset, json_mode=False), search_enabled=False, temperature=0.1,
                role="cio", asset=sym, round_label="cio", enable_thinking=_cio_thinking,
            )
            report.cio_memo = _ask(cio_agent, cio_brief)
        else:
            # prose 保住：GUI/transcript 展示 memo 字段（缺失则留原始 JSON 文本）
            _memo = cio_fields.get("memo")
            if isinstance(_memo, str) and _memo.strip():
                report.cio_memo = _memo
    emit("cio_done",
         memo_preview=report.cio_memo[:240])

    cio_parsed = parse_cio_memo(
        report.cio_memo,
        fields=cio_fields,
        worker_brief=cio_brief,
        current_price=current_price,
        # risk_profile / 快崩防御 后处理输入：regime 标签来自确定性 regime_brief
        # 首行；防御 = VIX 哨兵（市场级, sentiment_brief）OR ATR 腿（资产级, 调用方算好）
        regime=regime_label_from_text(regime_brief),
        defense_flag_on=(
            "INDEP_DEFENSE_FLAG: on" in sentiment_brief or atr_defense_on
        ),
        # 黄金分批 DCA 闸（service 层算好的本批放行/暂拦；非黄金/未启用为 None=旧全拦）
        defense_dca=defense_dca,
    )

    debate_meta = {
        "max_rounds": max_debate_rounds,
        "final_round": final_round,
        "converged": converged,
        "quant_history": quant_history,
        "risk_history": risk_history,
    }
    if persist_to_memory:
        _persist(report, cio_parsed, debate_meta=debate_meta)

    return {
        "asset": sym,
        "verdict": cio_parsed,
        "report": report,
        "debate": debate_meta,
    }


__all__ = [
    "CommitteeReport",
    "find_verdict_language",
    "_extract_signal_strength",
    "_SIGNAL_RE",
    "_STRENGTH_RE",
    "_check_convergence",
    "_format_debate_history",
    "run_committee",
]
