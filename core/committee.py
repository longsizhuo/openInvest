"""Investment Committee 编排 - 4 角色（Quant / Macro / Risk Officer / CIO）

设计要点：
- 信息分隔（每个 agent 只看自己领域的数据）
- 多轮 cross-challenge：Round 2..N agent 看对方上一轮输出后调整自己（max_debate_rounds 控制）
- Round 1 / Round 2..N 内部 Quant 和 Risk **真并行**（ThreadPoolExecutor）
- 收敛判定：连续 2 轮 SIGNAL+STRENGTH 都没变 → 提前退出
- progress_callback：每个 stage emit 一次（启动 / Round N / converged / cio_done）

LLM 调用数：
- max_debate_rounds=1（旧默认，daily_report 用）：5 LLM × N 资产
- max_debate_rounds=4（live 真讨论模式）：最多 (1 + 4*2 + 1) = 10 LLM，
  但收敛后会提前退出，平均 6-8 LLM
"""
from __future__ import annotations

import logging
import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

from agents.cio import build_cio_prompt
from agents.macro_strategist import PROMPT_MACRO_STRATEGIST
from agents.quant import build_quant_prompt
from agents.risk_officer import build_risk_officer_prompt
from agents.sdk_agent import SDKAgent
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

def _create_agent(
    system_prompt: str, *,
    search_enabled: bool = True,
    temperature: float = 0.2,
    role: str = "unknown",
    asset: Optional[str] = None,
    round_label: Optional[str] = None,
) -> Optional[SDKAgent]:
    """从 LangChain SimpleAgent 迁移到 SDKAgent（OpenAI 兼容协议直连 DeepSeek）。

    架构升级（用户原话: '我们还是有点 hack 了'）：
    - 不再 ReAct 文本协议，用原生 OpenAI/DeepSeek function calling
    - LLM 主动调 5 个 tool（get_history_data / analyze_multi_timeframe /
      get_macro_snapshot / query_dreaming_insights / get_recent_committee_verdicts）
    - search_enabled 参数兼容旧接口（现等价于 enable_tools）

    保留 Hybrid 设计：caller 仍传 baseline brief 做最低保障，LLM 主动 tool
    call 是补充查询；DeepSeek tool calling 弱时能 graceful 降级。
    """
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        print("❌ DEEPSEEK_API_KEY 缺失")
        return None
    # v3 透明化：把 role/asset/round 传进 telemetry meta，让 LLM 调用记录可按维度切片
    from core.llm_telemetry import TelemetryMeta
    meta = TelemetryMeta(
        agent_role=role,
        asset=asset,
        round=round_label,
        provider="deepseek",
        model="deepseek-chat",
    )
    return SDKAgent(
        system_prompt=system_prompt,
        model="deepseek-chat",
        api_key=api_key,
        base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        temperature=temperature,
        enable_tools=search_enabled,
        max_tool_iterations=4,
        provider="deepseek",
        telemetry_meta=meta,
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


def _ask(agent: Optional[SDKAgent], context: str) -> str:
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
    agent = _create_agent(PROMPT_MACRO_STRATEGIST, role="macro", round_label="macro")
    return _ask(agent, f"# 当前宏观数据参考:\n{macro_data_brief}\n\n请按格式输出 Macro 评估。")


def _parallel_ask(pairs: List[Tuple[Optional[SDKAgent], str]]) -> List[str]:
    """并行跑多个 (agent, input)，返回结果列表（按入参顺序）

    DeepSeek API 是 IO 密集型（HTTP），ThreadPool 不受 GIL 影响。
    Round 1 / Round 2..N 内部的 Quant + Risk 就用这个并行起来，省 50% 耗时。
    """
    if not pairs:
        return []
    if len(pairs) == 1:
        agent, inp = pairs[0]
        return [_ask(agent, inp)]
    with ThreadPoolExecutor(max_workers=len(pairs)) as pool:
        futures = [pool.submit(_ask, agent, inp) for agent, inp in pairs]
        return [f.result() for f in futures]


# 收敛判定用：从 agent 输出抓 SIGNAL + STRENGTH
_SIGNAL_RE = re.compile(r"SIGNAL:\s*(\w+)", re.IGNORECASE)
_STRENGTH_RE = re.compile(r"STRENGTH:\s*([\d.]+)", re.IGNORECASE)


def _extract_signal_strength(text: str) -> Tuple[Optional[str], Optional[float]]:
    """从 agent 输出抓 SIGNAL（大写归一）+ STRENGTH（float）。抓不到返回 (None, None)"""
    sig_m = _SIGNAL_RE.search(text or "")
    sig = sig_m.group(1).upper() if sig_m else None
    stren_m = _STRENGTH_RE.search(text or "")
    try:
        stren = float(stren_m.group(1)) if stren_m else None
    except (ValueError, AttributeError):
        stren = None
    return sig, stren


def _check_convergence(
    quant_history: List[str], risk_history: List[str],
) -> bool:
    """连续 2 轮 SIGNAL 一致 + STRENGTH 差距 ≤ 1.0 → 视为收敛"""
    if len(quant_history) < 2 or len(risk_history) < 2:
        return False
    qa = _extract_signal_strength(quant_history[-1])
    qb = _extract_signal_strength(quant_history[-2])
    ra = _extract_signal_strength(risk_history[-1])
    rb = _extract_signal_strength(risk_history[-2])

    def _stable(a: Tuple[Optional[str], Optional[float]],
                b: Tuple[Optional[str], Optional[float]]) -> bool:
        if a[0] != b[0]:
            return False
        if a[1] is None or b[1] is None:
            return a[1] == b[1]
        return abs(a[1] - b[1]) < 1.0

    return _stable(qa, qb) and _stable(ra, rb)


def _format_debate_history(
    quant_history: List[str], risk_history: List[str],
) -> str:
    """组装多轮辩论历史给下一轮 agent 看（最新一轮在最下面，强调）"""
    lines = ["# 辩论历史（按时间顺序，最新一轮在最下方）"]
    for i, (q, r) in enumerate(zip(quant_history, risk_history), 1):
        lines.append(f"\n## Round {i}")
        lines.append(f"\n### Quant\n{q}")
        lines.append(f"\n### Risk\n{r}")
    return "\n".join(lines)


def run_committee(
    asset: Dict[str, Any],
    market_data: str,
    macro_view: str,
    portfolio_summary: str,
    prior_insights: str = "",
    regime_brief: str = "",
    *,
    persist_to_memory: bool = True,
    max_debate_rounds: int = 1,
    progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """对单个资产跑 Macro + Quant/Risk 多轮辩论 + CIO

    Args:
        regime_brief: core.regime.format_regime_brief 的输出
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
    )

    quant_history: List[str] = []   # Round 1, 2, ..., N
    risk_history: List[str] = []
    regime_section = (
        f"# 市场 Regime (确定性算出，必须遵循):\n{regime_brief}\n\n"
        if regime_brief else ""
    )

    # ===== Round 1: Quant 和 Risk 独立陈述（信息分隔 + 真并行）=====
    emit("round_1_start", round=1, mode="independent")

    quant_input_r1 = (
        f"# 资产: {asset.get('display_name', sym)} ({sym})\n"
        f"{regime_section}"
        f"# 市场数据 (技术指标 + 多周期):\n{market_data}\n\n"
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
            + "严格遵守 REGIME 硬保护规则（详见 system prompt）。"
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

    # ===== CIO 综合所有 =====
    emit("cio_start", asset=sym)
    cio_agent = _create_agent(
        build_cio_prompt(asset), search_enabled=False, temperature=0.1,
        role="cio", asset=sym, round_label="cio",
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
    report.cio_memo = _ask(cio_agent, cio_brief)
    emit("cio_done",
         memo_preview=report.cio_memo[:240])

    cio_parsed = parse_cio_memo(report.cio_memo)

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


def _capture_macro_context() -> Dict[str, Any]:
    """快照决议时的 macro 状态（给 verdict_review 做事后归因用）。

    audit A1: verdict 错时能区分'模型预判错' vs '宏观突变黑天鹅'。
    例：BUY 后 60 天跌 8%，但同期 VIX +60% → 不是模型差，是黑天鹅冲击。
    """
    snapshot: Dict[str, Any] = {"captured_at": datetime.now().isoformat(timespec="seconds")}
    try:
        from utils.exchange_fee import get_history_data
        for sym, label in [("^VIX", "vix"), ("^TNX", "tnx"),
                           ("USDCNY=X", "usdcny"), ("AUDCNY=X", "audcny")]:
            df = get_history_data(sym, "5d")
            if not df.empty:
                snapshot[label] = round(float(df["Close"].iloc[-1]), 4)
    except Exception as e:
        snapshot["_capture_error"] = str(e)[:120]
    return snapshot


def _persist(report: CommitteeReport, verdict: Dict[str, Any],
             *, output_dir: Optional[Any] = None,
             date_override: Optional[str] = None,
             debate_meta: Optional[Dict[str, Any]] = None) -> None:
    """落盘 committee markdown

    output_dir: 默认 memory/.committee/<date>/；backtest 时覆盖到 memory/.backtest/
    date_override: backtest 穿越历史某天用
    debate_meta: 多轮辩论元信息 + 完整 quant/risk history（v3 新增）
    """
    import json as _json
    store = MemoryStore()
    today = date_override or datetime.now().strftime("%Y-%m-%d")
    out_dir = output_dir if output_dir is not None else (store.root / ".committee" / today)
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_sym = re.sub(r"[^a-zA-Z0-9_-]", "_", report.asset.get("symbol", "asset"))
    path = out_dir / f"{safe_sym}.md"

    macro_ctx = _capture_macro_context()

    lines = [
        f"# Committee: {report.asset.get('display_name', report.asset.get('symbol'))}",
        f"\n**Date**: {today}",
        f"**Verdict**: {verdict['verdict']} (confidence {verdict['confidence']:.2f})",
        f"**Dominant view**: {verdict['dominant_view']}",
        f"**Suggested allocation CNY**: {verdict['alloc_cny']}",
    ]

    # v3: 多轮辩论元信息
    if debate_meta:
        lines.append(
            f"**Debate**: {debate_meta['final_round']} round(s) "
            f"(max {debate_meta['max_rounds']}, "
            f"{'converged' if debate_meta['converged'] else 'hit limit'})"
        )

    lines.extend([
        "\n---\n\n## Macro Context Snapshot (for post-hoc attribution)\n",
        f"```json\n{_json.dumps(macro_ctx, ensure_ascii=False, indent=2)}\n```",
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
    ])

    # v3: 多轮辩论历史（仅 max_debate_rounds > 1 时有 Round 3+）
    if debate_meta and debate_meta.get("final_round", 1) > 2:
        quant_hist = debate_meta.get("quant_history", [])
        risk_hist = debate_meta.get("risk_history", [])
        lines.append("\n\n---\n\n## Extended Debate (Rounds 3+)\n")
        for i in range(2, len(quant_hist)):    # Round 3 起 (idx=2)
            round_no = i + 1
            lines.append(f"\n### Round {round_no} — Quant\n{quant_hist[i]}")
            lines.append(f"\n### Round {round_no} — Risk\n{risk_hist[i]}")

    path.write_text("\n".join(lines), encoding="utf-8")
    store.dream_event({
        "phase": "committee_finished",
        "asset": report.asset.get("symbol"),
        "verdict": verdict["verdict"],
        "confidence": verdict["confidence"],
        "macro_at_decision": macro_ctx,
        "is_backtest": output_dir is not None,
        "debate_rounds": debate_meta["final_round"] if debate_meta else 2,
        "debate_converged": debate_meta["converged"] if debate_meta else None,
    })


__all__ = [
    "CommitteeReport",
    "run_macro_view",
    "run_committee",
    "parse_cio_memo",
]
