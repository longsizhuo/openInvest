"""Investment Committee 编排 - 4 角色（Quant / Macro / Risk Officer / CIO）

替代旧的 Bull-vs-Bear-vs-Judge 辩论。设计要点：
- 信息分隔（每人只看自己领域的数据）
- 结构化输出（SIGNAL + STRENGTH + KEY_DATA）
- CIO 强制综合三方 + 用户上下文 → 投行级 memo
- 持久化到 memory/.committee/<date>/<asset>.md（旧 .debate/ 保留作 archive）

每个资产 4 次 LLM 调用（quant + macro 共享 + risk + cio）。
但 Macro 只跑一次（多资产共享），所以总调用数 = 1 macro + 3*N (asset) + 0 manager。
"""
from __future__ import annotations

import os
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from agents.agent import SimpleAgent
from agents.cio import build_cio_prompt
from agents.macro_strategist import PROMPT_MACRO_STRATEGIST
from agents.quant import build_quant_prompt
from agents.risk_officer import build_risk_officer_prompt
from core.memory_store import MemoryStore

# LLM 调用重试参数（覆盖 DeepSeek 偶发的 429 / 5xx / 网络抖动）。
# 设计目标：3 次尝试在 ~14s 内完成，失败后才把空字符串回给 CIO 让它判 garbage。
LLM_MAX_ATTEMPTS = int(os.getenv("INVEST_LLM_MAX_ATTEMPTS", "3"))
LLM_BASE_DELAY = float(os.getenv("INVEST_LLM_BASE_DELAY", "2.0"))
LLM_MAX_DELAY = float(os.getenv("INVEST_LLM_MAX_DELAY", "20.0"))


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

    def to_cio_brief(self) -> str:
        """组装给 CIO 看的输入 - 含 cross-challenge round 后的调整"""
        lines = [
            f"=== ASSET: {self.asset.get('display_name', self.asset.get('symbol'))} ===",
            f"\n=== MACRO STRATEGIST (跨资产共享) ===\n{self.macro_view}",
            "\n=== ROUND 1 (独立陈述) ===",
            f"\n--- QUANT ---\n{self.quant_view}",
            f"\n--- RISK OFFICER ---\n{self.risk_view}",
            "\n=== ROUND 2 (cross-challenge 后的调整) ===",
            f"\n--- QUANT 调整 ---\n{self.quant_adjusted}",
            f"\n--- RISK 调整 ---\n{self.risk_adjusted}",
            f"\n=== USER PORTFOLIO CONTEXT ===\n{self.portfolio_summary}",
        ]
        if self.prior_insights:
            lines.append(f"\n=== LONG-TERM INSIGHTS (Dreaming) ===\n{self.prior_insights}")
        return "\n".join(lines)


# ----------------------------------------------------------------------
# Agent factory
# ----------------------------------------------------------------------

def _create_agent(system_prompt: str, *, search_enabled: bool = True,
                  temperature: float = 0.2) -> Optional[SimpleAgent]:
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        print("❌ DEEPSEEK_API_KEY 缺失")
        return None
    return SimpleAgent(
        temperature=temperature,
        enable_search=search_enabled,
        model="deepseek-chat",
        openai_api_key=api_key,
        openai_api_base=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        system_prompt=system_prompt,
        debug=False,
    )


def _is_transient(exc: BaseException) -> bool:
    """是否值得重试。auth/quota 类错误重试也没用，立刻放弃；
    网络/超时/限流是常见 transient，重试有效。
    DeepSeek/openai 客户端会把不同 HTTP 错误包成不同 *Error 类，名字里通常含
    'Timeout' / 'Connection' / 'RateLimit' / 'APIStatusError'。"""
    name = type(exc).__name__.lower()
    if any(k in name for k in ("auth", "permission", "invalidrequest", "notfound")):
        return False
    if any(k in name for k in ("timeout", "connection", "ratelimit", "apistatus", "apierror")):
        return True
    # 默认重试——LLM SDK 错误类型多变，宁可重试 3 次也不要静默失败
    return True


# 失败哨兵：让 CIO 上下文里能识别"这个 worker 没产出"，避免 CIO 在错误消息上面综合
AGENT_UNAVAILABLE_MARKER = "[WORKER_UNAVAILABLE]"


def _ask(agent: Optional[SimpleAgent], context: str) -> str:
    """LLM 调用 + 重试。失败时返回明确的哨兵字符串，让 CIO prompt 可识别降权。

    audit (algo M4): 之前失败返回 'Agent error: ...' 这种自然语言，CIO 会
    礼貌地尝试综合错误消息，输出 silent corruption 的 verdict。现在返回
    带 [WORKER_UNAVAILABLE] 前缀，CIO prompt 已加 hard rule 看到此标记必须
    把 confidence 压到 ≤ 0.4 + verdict 必须 HOLD。
    """
    if agent is None:
        return f"{AGENT_UNAVAILABLE_MARKER} reason=agent_not_constructed"
    last_exc: Optional[BaseException] = None
    for attempt in range(1, LLM_MAX_ATTEMPTS + 1):
        try:
            return agent.run(context)
        except Exception as e:
            last_exc = e
            if attempt >= LLM_MAX_ATTEMPTS or not _is_transient(e):
                break
            # 指数退避 + jitter（避免多个并发 agent 同时撞重试窗口）
            delay = min(LLM_BASE_DELAY * (2 ** (attempt - 1)), LLM_MAX_DELAY)
            delay *= 0.5 + random.random()  # 0.5x ~ 1.5x jitter
            print(
                f"⚠️ Agent retry {attempt}/{LLM_MAX_ATTEMPTS - 1}: "
                f"{type(e).__name__}: {e} → sleep {delay:.1f}s"
            )
            time.sleep(delay)
    return (
        f"{AGENT_UNAVAILABLE_MARKER} "
        f"reason=retry_exhausted exc_type={type(last_exc).__name__} "
        f"exc_msg={str(last_exc)[:120]}"
    )


# ----------------------------------------------------------------------
# Verdict 解析
# ----------------------------------------------------------------------

VERDICT_RE = re.compile(r"VERDICT:\s*(BUY|ACCUMULATE|HOLD|TRIM|SELL)", re.I)
CONFIDENCE_RE = re.compile(r"CONFIDENCE:\s*([\d.]+)")
DOMINANT_RE = re.compile(r"DOMINANT_VIEW:\s*(quant|macro|risk)", re.I)
ALLOC_RE = re.compile(r"SUGGESTED_ALLOC_CNY:\s*(-?\d+)")


def parse_cio_memo(text: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {"raw": text}
    m = VERDICT_RE.search(text)
    out["verdict"] = m.group(1).upper() if m else "UNCLEAR"
    m = CONFIDENCE_RE.search(text)
    out["confidence"] = float(m.group(1)) if m else 0.0
    m = DOMINANT_RE.search(text)
    out["dominant_view"] = m.group(1).lower() if m else "tie"
    m = ALLOC_RE.search(text)
    out["alloc_cny"] = int(m.group(1)) if m else 0

    # Sanity check 1（audit security M3）: 防 prompt injection / LLM 过度自信
    # confidence ≥ 0.95 + BUY 的组合在统计上不可能（60 天 sample size 信号太弱），
    # 99% 是 LLM hallucination 或 prompt 被新闻 / 行情字符串污染
    if out["verdict"] == "BUY" and out["confidence"] >= 0.95:
        out["_original_verdict"] = "BUY"
        out["_original_confidence"] = out["confidence"]
        out["verdict"] = "ACCUMULATE"
        out["confidence"] = 0.6
        print(f"⚠️ parse_cio_memo: 降级 BUY({out['_original_confidence']}) → ACCUMULATE(0.6) "
              f"防 LLM 过度自信 / prompt injection")

    # Sanity check 2（audit financial Minor）: alloc_cny 合理性 clamp
    # LLM 偶发输出无单位数字会被错解读，单笔超过 ¥100k 的提议大概率有问题
    if abs(out["alloc_cny"]) > 100000:
        print(f"⚠️ parse_cio_memo: alloc_cny={out['alloc_cny']} 超出合理区间，clamp 到 ±100000")
        out["_original_alloc"] = out["alloc_cny"]
        out["alloc_cny"] = max(-100000, min(100000, out["alloc_cny"]))

    # Sanity check 3（audit algo M4）: worker 输入失败时 confidence 降级
    # 上游传来的 raw 是 brief，含 macro/quant/risk 内容；如果 brief 里出现 worker
    # unavailable 哨兵，CIO 大概率是在 garbage 上综合
    if "[WORKER_UNAVAILABLE]" in text:
        if out["confidence"] > 0.4:
            out["_original_confidence_unavailable"] = out["confidence"]
            out["confidence"] = 0.4
            out["verdict"] = "HOLD"
            print("⚠️ parse_cio_memo: 检测到 [WORKER_UNAVAILABLE] 标记，"
                  "强制 verdict=HOLD + confidence≤0.4")

    return out


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------

def run_macro_view(macro_data_brief: str) -> str:
    """跨资产共享的 Macro 评估，跑一次后 CIO 各自引用"""
    agent = _create_agent(PROMPT_MACRO_STRATEGIST)
    return _ask(agent, f"# 当前宏观数据参考:\n{macro_data_brief}\n\n请按格式输出 Macro 评估。")


def run_committee(
    asset: Dict[str, Any],
    market_data: str,
    macro_view: str,
    portfolio_summary: str,
    prior_insights: str = "",
    *,
    persist_to_memory: bool = True,
) -> Dict[str, Any]:
    """对单个资产跑 Quant + Risk Officer + CIO（macro 是外部传进来共享的）"""

    report = CommitteeReport(
        asset=asset,
        macro_view=macro_view,
        market_data=market_data,
        portfolio_summary=portfolio_summary,
        prior_insights=prior_insights,
    )

    # === Round 1: Quant 和 Risk Officer 独立陈述（信息分隔，专业独立）===
    quant_input = (
        f"# 资产: {asset.get('display_name', asset['symbol'])} ({asset['symbol']})\n"
        f"# 市场数据 (技术指标 + 多周期):\n{market_data}\n\n"
        f"请按 Quant Analyst 格式输出技术信号。"
    )
    quant_agent = _create_agent(build_quant_prompt(asset, "opening"), search_enabled=False)
    report.quant_view = _ask(quant_agent, quant_input)

    risk_input = (
        f"# 资产: {asset.get('display_name', asset['symbol'])} ({asset['symbol']})\n"
        f"# 用户当前持仓:\n{portfolio_summary}\n\n"
        f"# 长期行为模式 (Dreaming):\n{prior_insights or '(暂无)'}\n\n"
        f"请按 Risk Officer 格式输出风险评估。"
    )
    risk_agent = _create_agent(build_risk_officer_prompt(asset, "opening"), search_enabled=False)
    report.risk_view = _ask(risk_agent, risk_input)

    # === Round 2: cross-challenge — 互相看到对方输出后调整 ===
    quant_rebut_input = (
        f"# Round 1 你自己的技术信号:\n{report.quant_view}\n\n"
        f"# Risk Officer 的报告:\n{report.risk_view}\n\n"
        f"请基于 Risk Officer 的输入调整或维持你的技术信号 STRENGTH。"
    )
    quant_rebut_agent = _create_agent(
        build_quant_prompt(asset, "rebuttal"), search_enabled=False, temperature=0.2
    )
    report.quant_adjusted = _ask(quant_rebut_agent, quant_rebut_input)

    risk_rebut_input = (
        f"# Round 1 你自己的风险评估:\n{report.risk_view}\n\n"
        f"# Quant 的技术信号:\n{report.quant_view}\n\n"
        f"请基于 Quant 的技术信号调整或维持你的止损建议。"
    )
    risk_rebut_agent = _create_agent(
        build_risk_officer_prompt(asset, "rebuttal"), search_enabled=False, temperature=0.2
    )
    report.risk_adjusted = _ask(risk_rebut_agent, risk_rebut_input)

    # === Round 3: CIO 综合所有（Macro + Round 1 + Round 2 调整 + portfolio）===
    cio_agent = _create_agent(build_cio_prompt(asset), search_enabled=False, temperature=0.1)
    report.cio_memo = _ask(cio_agent, report.to_cio_brief())

    cio_parsed = parse_cio_memo(report.cio_memo)

    if persist_to_memory:
        _persist(report, cio_parsed)

    return {
        "asset": asset.get("symbol"),
        "verdict": cio_parsed,
        "report": report,
    }


def _persist(report: CommitteeReport, verdict: Dict[str, Any]) -> None:
    store = MemoryStore()
    today = datetime.now().strftime("%Y-%m-%d")
    out_dir = store.root / ".committee" / today
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_sym = re.sub(r"[^a-zA-Z0-9_-]", "_", report.asset.get("symbol", "asset"))
    path = out_dir / f"{safe_sym}.md"

    lines = [
        f"# Committee: {report.asset.get('display_name', report.asset.get('symbol'))}",
        f"\n**Date**: {today}",
        f"**Verdict**: {verdict['verdict']} (confidence {verdict['confidence']:.2f})",
        f"**Dominant view**: {verdict['dominant_view']}",
        f"**Suggested allocation CNY**: {verdict['alloc_cny']}",
        "\n---\n\n## CIO Memo (Round 3)\n",
        report.cio_memo,
        "\n\n---\n\n## Macro Strategist (shared)\n",
        report.macro_view,
        "\n\n---\n\n## Round 1 — Independent Briefs\n",
        f"\n### Quant Analyst\n{report.quant_view}",
        f"\n### Risk Officer\n{report.risk_view}",
        "\n\n---\n\n## Round 2 — Cross-Challenge Adjustments\n",
        f"\n### Quant adjusted (after seeing Risk)\n{report.quant_adjusted}",
        f"\n### Risk adjusted (after seeing Quant)\n{report.risk_adjusted}",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    store.dream_event({
        "phase": "committee_finished",
        "asset": report.asset.get("symbol"),
        "verdict": verdict["verdict"],
        "confidence": verdict["confidence"],
    })


__all__ = [
    "CommitteeReport",
    "run_macro_view",
    "run_committee",
    "parse_cio_memo",
]
