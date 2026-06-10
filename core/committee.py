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
    wealth_context_view: str = ""     # 跨资产共享 (off-portfolio 真实流动性)
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
        if self.wealth_context_view:
            lines.append(
                f"\n=== WEALTH CONTEXT OFFICER (真实流动性，跨资产共享) ===\n{self.wealth_context_view}"
            )
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
    # 统一从 utils.llm 读 LLM 配置（默认 DeepSeek，支持 LLM_* env 切换千问/智谱/Kimi）
    from utils.llm import get_llm_config_safe
    api_key, base_url, model_name, provider_litellm = get_llm_config_safe()
    if not api_key:
        log.error("LLM_API_KEY 或 DEEPSEEK_API_KEY 缺失")
        return None
    # v3 透明化：把 role/asset/round 传进 telemetry meta，让 LLM 调用记录可按维度切片
    from core.llm_telemetry import TelemetryMeta
    # SDKAgent.provider 目前只支持 "deepseek" / "openai"（决定客户端怎么造）；
    # 千问 / 智谱 / Kimi 都走 OpenAI 兼容协议，沿用 "deepseek" 分支即可（传 api_key+base_url）
    meta = TelemetryMeta(
        agent_role=role,
        asset=asset,
        round=round_label,
        provider="deepseek",
        model=model_name,
    )
    return SDKAgent(
        system_prompt=system_prompt,
        model=model_name,
        api_key=api_key,
        base_url=base_url,
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
            log.warning(
                "Agent retry %d/%d: %s: %s → sleep %.1fs",
                attempt, LLM_MAX_ATTEMPTS - 1, type(e).__name__, e, delay,
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
TRIM_REASON_RE = re.compile(r"TRIM_REASON:\s*(concentration|stop_loss|bearish)", re.I)
# TRIM 路径化：买回点 + 预期路径（EXECUTION_PLAN/RISK_PLAN 仍丢弃，只解析这三个）
REENTRY_PRICE_RE = re.compile(r"REENTRY_PRICE:\s*[¥$]?\s*(-?[\d,]+(?:\.\d+)?)", re.I)
REENTRY_CONDITION_RE = re.compile(r"REENTRY_CONDITION:\s*(.+)")
EXPECTED_PATH_RE = re.compile(r"EXPECTED_PATH:\s*(.+)")
# regime 标签（format_regime_brief 输出首行 / coordinator transcript 里的同款行）
REGIME_LABEL_RE = re.compile(r"^REGIME:\s*([a-z_]+)\s*$", re.MULTILINE)


def regime_label_from_text(text: str) -> Optional[str]:
    """从确定性文本（regime_brief / coordinator transcript）提取 regime 标签。

    给 parse_cio_memo 的 risk_profile 后处理用：来源是系统自己
    format_regime_brief 生成的 `REGIME: <label>` 行，不是 LLM 输出。
    """
    m = REGIME_LABEL_RE.search(text or "")
    return m.group(1) if m else None


# ============ Sanity-check 阈值（单点维护，便于调参）============
#
# 统一抽到这里，避免散落 magic number。每条都附"为什么这个数"的来源说明，
# 否则未来无法判断该改还是不该改。
#
# 调参流程：
#   1. 改 core/config/defaults.yaml（或 set_config_override()）
#   2. 同步 docs/wiki/02-agents.md "CIO Sanity Check" 表
#   3. tests/test_committee_parser.py 跑一遍
#
# Step 3a: 从 config 读取，向后兼容保留 dict 形式
def _build_thresholds_from_config() -> Dict[str, float]:
    """从 config 构建 THRESHOLDS dict。"""
    from core.config import load_config
    cfg = load_config().verdict
    return {
        "buy_confidence_overdrive": cfg.buy_confidence_overdrive,
        "buy_confidence_downgrade_to": cfg.buy_confidence_downgrade_to,
        "alloc_cny_ceiling": cfg.alloc_cny_ceiling,
        "worker_unavailable_confidence_floor": cfg.worker_unavailable_confidence_floor,
    }


THRESHOLDS: Dict[str, float] = _build_thresholds_from_config()


def _force_hold(out: Dict[str, Any], *, confidence_ceiling: float) -> None:
    """统一 force-HOLD 后处理：verdict→HOLD、confidence 压到 ≤ ceiling、alloc_cny→0。

    多个 sanity check（worker 输入失败 / 兜底充足覆盖集中度减仓）最终都要把裁决
    钉成"HOLD 且不带方向性信号强度"。集中到一处，避免每个 check 各自手抄时漏步
    —— Sanity 3 历史上 force HOLD 却没归零 alloc_cny，导致 HOLD 仍带建议金额。
    调用方负责记录各自的 _original_* 溯源（语义不同，不放进这里）。
    """
    out["verdict"] = "HOLD"
    out["confidence"] = min(out["confidence"], confidence_ceiling)
    out["alloc_cny"] = 0


def parse_cio_memo(
    text: str,
    *,
    solvency_strong: bool = False,
    current_price: Optional[float] = None,
    regime: Optional[str] = None,
    defense_flag_on: bool = False,
) -> Dict[str, Any]:
    out: Dict[str, Any] = {"raw": text}
    m = VERDICT_RE.search(text)
    out["verdict"] = m.group(1).upper() if m else "UNCLEAR"
    m = CONFIDENCE_RE.search(text)
    out["confidence"] = float(m.group(1)) if m else 0.0
    m = DOMINANT_RE.search(text)
    out["dominant_view"] = m.group(1).lower() if m else "tie"
    m = ALLOC_RE.search(text)
    out["alloc_cny"] = int(m.group(1)) if m else 0
    m = TRIM_REASON_RE.search(text)
    out["trim_reason"] = m.group(1).lower() if m else None

    # TRIM 路径化字段：买回价 / 买回条件 / 预期路径（真正解析并保留，不再全丢）
    m = REENTRY_PRICE_RE.search(text)
    out["reentry_price"] = float(m.group(1).replace(",", "")) if m else None
    m = REENTRY_CONDITION_RE.search(text)
    rc = m.group(1).strip() if m else None
    out["reentry_condition"] = rc if (rc and rc.upper() != "N/A") else None
    m = EXPECTED_PATH_RE.search(text)
    ep = m.group(1).strip() if m else None
    out["expected_path"] = ep if (ep and ep.upper() != "N/A") else None

    # Sanity check 0: INVEST_CIO_CONFIDENCE_CAP（Optuna 训练参数）clamp confidence 上限
    cap_env = os.getenv("INVEST_CIO_CONFIDENCE_CAP")
    if cap_env:
        try:
            cap = float(cap_env)
            if out["confidence"] > cap:
                out["_original_confidence_cap"] = out["confidence"]
                out["confidence"] = cap
        except ValueError:
            pass

    # Sanity check 1: 防 prompt injection / LLM 过度自信
    # 从 config 读取（set_config_override() 实时生效）
    from core.config import load_config
    _verdict_cfg = load_config().verdict
    confidence_threshold = _verdict_cfg.buy_confidence_overdrive
    confidence_downgrade = _verdict_cfg.buy_confidence_downgrade_to
    if out["verdict"] == "BUY" and out["confidence"] >= confidence_threshold:
        out["_original_verdict"] = "BUY"
        out["_original_confidence"] = out["confidence"]
        out["verdict"] = "ACCUMULATE"
        out["confidence"] = confidence_downgrade
        log.warning(
            "parse_cio_memo: 降级 BUY(%s) → ACCUMULATE(%s) — 防 LLM 过度自信 / prompt injection",
            out["_original_confidence"], confidence_downgrade,
        )

    # Sanity check 2: alloc_cny 合理性 clamp
    # INVEST_ALLOC_AGGRESSIVENESS（Optuna 训练参数，0.05~0.30）会按 baseline ¥100k
    # 收紧 ceiling 到 100_000 × agg。例如 agg=0.10 → 单笔 ≤ ¥10k，避免 LLM 给
    # ¥50k alloc 导致 simulator 大量 SKIP。
    alloc_ceiling = _verdict_cfg.alloc_cny_ceiling
    agg_env = os.getenv("INVEST_ALLOC_AGGRESSIVENESS")
    if agg_env:
        try:
            alloc_ceiling = min(alloc_ceiling, int(100_000 * float(agg_env)))
        except ValueError:
            pass
    if abs(out["alloc_cny"]) > alloc_ceiling:
        log.warning(
            "parse_cio_memo: alloc_cny=%s 超出合理区间，clamp 到 ±%s",
            out["alloc_cny"], alloc_ceiling,
        )
        out["_original_alloc"] = out["alloc_cny"]
        out["alloc_cny"] = max(-alloc_ceiling, min(alloc_ceiling, out["alloc_cny"]))

    # Sanity check 3（audit algo M4）: worker 输入失败时 confidence 降级
    # 上游传来的 raw 是 brief，含 macro/quant/risk 内容；如果 brief 里出现 worker
    # unavailable 哨兵，CIO 大概率是在 garbage 上综合
    floor = _verdict_cfg.worker_unavailable_confidence_floor
    if "[WORKER_UNAVAILABLE]" in text and out["confidence"] > floor:
        out["_original_confidence_unavailable"] = out["confidence"]
        _force_hold(out, confidence_ceiling=floor)
        log.warning("parse_cio_memo: 检测到 [WORKER_UNAVAILABLE] 标记，"
                    "强制 verdict=HOLD + confidence≤floor + alloc=0")

    # Sanity check 4: SOLVENCY=strong + TRIM + TRIM_REASON=concentration → 强制 HOLD
    # 兜底充足时，"账户内集中度高"不应触发减仓（真实财富风险不存在），
    # 只应限制加仓。确定性后处理，不依赖 prompt。
    if (solvency_strong
            and out["verdict"] == "TRIM"
            and out.get("trim_reason") == "concentration"):
        out["_original_verdict"] = "TRIM"
        out["_original_trim_reason"] = "concentration"
        # setdefault: 若 Sanity 1/2 已记录更早的原值（如 Sanity 2 的 pre-clamp
        # alloc），不要被这里覆盖丢掉真正的原始值。
        out.setdefault("_original_confidence", out["confidence"])
        out.setdefault("_original_alloc", out["alloc_cny"])
        out["trim_reason"] = None
        _force_hold(out, confidence_ceiling=_verdict_cfg.forced_hold_confidence_ceiling)
        log.warning("parse_cio_memo: SOLVENCY=strong + TRIM(concentration) → "
                    "强制 HOLD（兜底充足，集中度不触发减仓）")

    # Sanity check 5: TRIM 必须给出"低于现价的买回点"，否则降级 HOLD
    # 卖出后买回点缺失 or 不低于现价 = 卖了高价大概率接回 = 纯亏，TRIM 不成立。
    # 只在拿得到 current_price 的 live 路径校验（re-parse 存档时 current_price=None 跳过）。
    if (current_price is not None
            and current_price > 0
            and out["verdict"] == "TRIM"):
        rp = out.get("reentry_price")
        if rp is None or rp >= current_price:
            out["_original_verdict"] = "TRIM"
            out["_sanity5_reason"] = (
                "reentry_missing" if rp is None else "reentry_not_below_current"
            )
            out["_current_price"] = current_price
            out["verdict"] = "HOLD"
            log.warning(
                "parse_cio_memo: TRIM 但买回点%s → 强制 HOLD（卖出后买不回更低 = 纯亏，TRIM 不成立）",
                "缺失" if rp is None else f"¥{rp} ≥ 现价 ¥{current_price}",
            )

    # 风险档 aggressive: uptrend 顺势加仓杠杆（显式风险偏好，不是 regime 智能）。
    # 2026-06 消融结论：原 prompt 层 uptrend 方向锁 = 纯杠杆（牛市 +CR，代价
    # −Sharpe + 深 MaxDD + 熊市更亏）。拆锁后把杠杆效应保留为显式 config 开关，
    # 默认 steady（不开）。确定性后处理，不依赖 prompt。约束：
    # - 只升级 HOLD→ACCUMULATE，不动 TRIM/SELL（防御性卖出不被杠杆覆盖）、不动 BUY
    # - INDEP_DEFENSE_FLAG=on（VIX 快崩哨兵）时跳过——MA regime 看不见快速崩盘，
    #   杠杆挂在滞后的假 uptrend 上正是最大伤害源
    if (_verdict_cfg.risk_profile == "aggressive"
            and regime == "uptrend"
            and not defense_flag_on
            and out["verdict"] == "HOLD"):
        out.setdefault("_original_verdict", "HOLD")
        out["_risk_profile_applied"] = "aggressive_uptrend_hold_to_accumulate"
        out["verdict"] = "ACCUMULATE"
        log.warning(
            "parse_cio_memo: risk_profile=aggressive + uptrend → HOLD 升级 ACCUMULATE"
            "（显式杠杆档，消融口径 +CR −Sharpe）",
        )

    return out


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------

def run_macro_view(macro_data_brief: str, *, event_brief: str = "") -> str:
    """跨资产共享的 Macro 评估，跑一次后 CIO 各自引用

    event_brief: 事件层（第一层）注入的盘中事件上下文（结构化文本，按时间排序，
                 含 supersedes 标记）。空字符串 = 不注入，行为完全等价于现状。
                 只有 Macro 看到（事件 RAG 严格隔离原则）。
    """
    agent = _create_agent(PROMPT_MACRO_STRATEGIST, role="macro", round_label="macro")
    event_section = (
        f"\n\n## 当前事件上下文（按时间排序，最新在前；可能含 supersedes 标记）\n{event_brief}\n"
        if event_brief else ""
    )
    return _ask(agent, f"# 当前宏观数据参考:\n{macro_data_brief}{event_section}\n\n请按格式输出 Macro 评估。")


# ============================================================================
# Shared Input Loaders — 所有 production entry 必经层
# ============================================================================
# 防漂移核心：所有"跨 entry 共享的输入"在这里统一定义。
# daily_report / committee_runner / backtest_committee / web_api 全用这些 loader,
# 永远不要在 entry 层重复读 user.md / portfolio.md / event_store。
#
# 加新的 cross-entry 参数（类似 wealth_context_view / event_brief）时：
#   1. 在 run_committee() 加 explicit 参数（默认 ""）
#   2. 在这里加一个 load_<name>() helper, graceful 退化空字符串
#   3. **所有 entry 调用 run_committee 之前先调 load_<name>()**
#   4. 加 e2e contract test 验证每个 entry 都传了
#
# 2026-05-15 漂移事故：wealth_context_view 只接了 prompt + 测试，没接调用链
# → 三个月 user.md 的 wealth_context 没进入任何 production 决策。
# Import rule（pyproject.toml）已禁止 entry 直接 import run_committee 跳过这层。
# ============================================================================


def load_wealth_context_view() -> str:
    """读 user.md.wealth_context + portfolio cash → WealthContextOfficer view。

    Graceful: 任何异常都返回空 str, 委员会照常跑（Risk Officer 退化为只看
    portfolio cash 的老逻辑）。
    """
    try:
        from core.memory_store import MemoryStore
        from core.portfolio_manager import PortfolioManager
        store = MemoryStore()
        user_doc = store.read("user")
        wealth_context = user_doc.metadata.get("wealth_context") if user_doc else None
        pm = PortfolioManager()
        portfolio_cash_cny = pm.cash_amount("CNY")
        return run_wealth_context_view(wealth_context, portfolio_cash_cny)
    except Exception as e:  # noqa: BLE001
        log.warning(f"load_wealth_context_view graceful 退化 '': {type(e).__name__}: {e}")
        return ""


def load_backup_cny(pm: Optional[Any] = None) -> float:
    """读 user.md.wealth_context.emergency_buffer_cny → off-portfolio 兜底金额（CNY）。

    用于 portfolio_summary_text 的"真实总财富占比"注释，三路径（cron / skill /
    service）单一可信源。正式字段是 `emergency_buffer_cny`（WealthContextRequest /
    invest-setup / GUI 写入）；历史上 daily_report / skill 误读不存在的
    `backup_amount_cny`，导致 backup_cny 恒为 0、注释从不渲染 —— 本 loader 修掉这个
    key 漂移并消除三处重复。

    Graceful: 读不到 / 异常 → 0.0（退化到"无兜底"逻辑，不阻断主流程）。

    Args:
        pm: 复用已有 PortfolioManager 的 store（避免重复 new MemoryStore）；
            None 时自建 MemoryStore() 读 user.md。
    """
    try:
        store = pm.store if pm is not None else MemoryStore()
        user_doc = store.read("user")
        wealth_context = user_doc.metadata.get("wealth_context") if user_doc else None
        if not wealth_context:
            return 0.0
        return float(wealth_context.get("emergency_buffer_cny", 0) or 0)
    except Exception as e:  # noqa: BLE001
        log.warning(f"load_backup_cny graceful 退化 0.0: {type(e).__name__}: {e}")
        return 0.0


def run_wealth_context_view(wealth_context: Optional[Dict[str, Any]],
                            portfolio_cash_cny: float) -> str:
    """跨资产共享的 WealthContextOfficer 评估，跑一次后 Risk Officer + CIO 引用。

    wealth_context 为 None / 空 → 直接返回 portfolio_only stub（不调 LLM，省成本）。

    **production 调用方走 load_wealth_context_view()** —— 它会自动读 user.md
    + portfolio cash 后调本函数. 本函数留 explicit 接口给测试 / backtest 注入用.
    """
    from agents.wealth_context_officer import PROMPT_WEALTH_CONTEXT_OFFICER

    if not wealth_context:
        return (
            f"SOLVENCY_BUFFER_LEVEL: unknown\n"
            f"ACCOUNT_PURPOSE: N/A\n"
            f"PORTFOLIO_CASH_CNY: {portfolio_cash_cny:.2f}\n"
            f"INVESTABLE_CASH_CNY: {portfolio_cash_cny:.2f}\n"
            f"BACKUP_BUFFER_CNY: 0\n"
            f"EXPLANATION_TO_RISK: user.md 没填 wealth_context，按 portfolio cash 判断流动性 + 风险。\n"
            f"EXPLANATION_TO_CIO: 加仓决策受 portfolio cash 限制。"
        )

    agent = _create_agent(PROMPT_WEALTH_CONTEXT_OFFICER,
                          role="wealth_context", round_label="wealth_context")
    import json as _json
    ctx_brief = (
        f"# 用户 wealth_context（user.md frontmatter）：\n"
        f"```json\n{_json.dumps(wealth_context, ensure_ascii=False, indent=2)}\n```\n\n"
        f"# Portfolio cash 现状：¥{portfolio_cash_cny:.2f} CNY\n\n"
        f"请按格式输出真实流动性评估。"
    )
    return _ask(agent, ctx_brief)


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


def _extract_concentration_from_summary(
    portfolio_summary: str, sym: str,
) -> Optional[float]:
    """从 portfolio_summary 文本里提取指定 asset 的集中度数字（百分比，float）

    portfolio_summary 由 utils.portfolio_summary 拼装，含每行 asset 字面写出：
      - **<display_name>** (SYM) (channel): ..., **集中度 33.6%** (CNY 市值 ...)

    抓不到（asset 不在 summary / 格式异常）返回 None，让上层不做覆写。
    """
    if not portfolio_summary or not sym:
        return None
    # 同一行：(SYM) ... **集中度 N%**
    pattern = re.compile(
        rf"\({re.escape(sym)}\)[^\n]*?\*\*集中度\s+([\d.]+)\s*%\*\*",
    )
    m = pattern.search(portfolio_summary)
    if not m:
        return None
    try:
        return float(m.group(1))
    except (ValueError, IndexError):
        return None


# Risk Officer 输出里的集中度行匹配（容忍 % 缺失、空白变化）
_RISK_CONCENTRATION_RE = re.compile(
    r"^(\s*CONCENTRATION_PCT\s*:\s*)([\d.]+)\s*%?",
    re.MULTILINE,
)


def _override_concentration_in_risk_output(
    risk_output: str, true_pct: Optional[float],
) -> str:
    """把 Risk Officer 输出里的 CONCENTRATION_PCT 强制覆写为 portfolio_summary 字面值

    背景（2026-05-20 漂移修复）：portfolio_summary 已显式喂入"**集中度 33.6%**"，
    但 Risk Officer LLM（provider 无关，当前 MiMo / 历史 DeepSeek 都见过）仍偶发
    hallucinate 编成 70.2%（同 prompt 前一日还能输出 33.4%）。Prompt 强约束 +
    service layer 覆写形成双层防御——LLM 不听话也能保证 CIO 看到真数。

    true_pct=None（portfolio_summary 也没给）时不动；
    Risk 没输出该字段时不补救（避免凭空注入）。
    """
    if true_pct is None or not risk_output:
        return risk_output
    formatted = f"{true_pct:.1f}%"

    def _sub(m: "re.Match[str]") -> str:
        llm_val = m.group(2)
        try:
            llm_float = float(llm_val)
        except ValueError:
            llm_float = -1.0
        # 容差 0.3% 内视为一致，不覆写（避免把 33.4 / 33.6 这种正常浮点 rounding
        # 误差当 hallucination 误改；33.6 - 33.4 在 IEEE 754 下 = 0.2000000...284，
        # 用 0.2 会卡边界，0.3 留余量但仍远低于真实 hallucination 量级 30%+）
        if abs(llm_float - true_pct) <= 0.3:
            return m.group(0)
        log.warning(
            "Risk Officer 集中度 hallucination 已覆写: LLM=%s%% → 真实=%s "
            "(portfolio_summary 字面值)",
            llm_val, formatted,
        )
        return f"{m.group(1)}{formatted}"

    new_output, _n = _RISK_CONCENTRATION_RE.subn(_sub, risk_output)
    return new_output


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
    wealth_context_view: str = "",
    reentry_reference: str = "",
    current_price: Optional[float] = None,
    sentiment_brief: str = "",
    valuation_brief: str = "",
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
        wealth_context_view=wealth_context_view,
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
    wealth_section = (
        f"# 用户真实流动性 (WealthContextOfficer):\n{wealth_context_view}\n\n"
        if wealth_context_view else ""
    )
    risk_input_r1 = (
        f"# 资产: {asset.get('display_name', sym)} ({sym})\n"
        f"# 用户当前持仓:\n{portfolio_summary}\n\n"
        f"{wealth_section}"
        f"# 长期行为模式 (Dreaming):\n{prior_insights or '(暂无)'}\n\n"
        f"请按 Risk Officer 格式输出风险评估。"
        f"**注意**：如果 WealthContextOfficer 报告 TRUE_LIQUIDITY=ample 或 moderate，"
        f"不要因为 portfolio cash 低就喊 high_risk—— 看 EXPLANATION_TO_RISK。"
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
    # (2026-05-20 NDQ 真实 33.6% 编成 70.2%)。这里强制改回真值。
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
    if reentry_reference:
        cio_brief += f"\n\n=== 卖出后路径 / 买回点参考 ===\n{reentry_reference}"
    report.cio_memo = _ask(cio_agent, cio_brief)
    emit("cio_done",
         memo_preview=report.cio_memo[:240])

    # 从 wealth_context_view 提取 SOLVENCY_BUFFER_LEVEL 用于 sanity check 4
    _solvency_strong = bool(
        wealth_context_view
        and "SOLVENCY_BUFFER_LEVEL: strong" in wealth_context_view
    )
    cio_parsed = parse_cio_memo(
        report.cio_memo,
        solvency_strong=_solvency_strong,
        current_price=current_price,
        # risk_profile 后处理输入：regime 标签来自确定性 regime_brief 首行；
        # 防御哨兵来自确定性 sentiment_brief（INDEP_DEFENSE_FLAG）
        regime=regime_label_from_text(regime_brief),
        defense_flag_on="INDEP_DEFENSE_FLAG: on" in sentiment_brief,
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


def _capture_macro_context(as_of_date: Optional[str] = None) -> Dict[str, Any]:
    """快照决议时的 macro 状态（给 verdict_review 做事后归因用）。

    audit A1: verdict 错时能区分'模型预判错' vs '宏观突变黑天鹅'。
    例：BUY 后 60 天跌 8%，但同期 VIX +60% → 不是模型差，是黑天鹅冲击。

    Args:
        as_of_date: backtest 模式必传。如果不传，captured_at 用 datetime.now()
            + macro 值用最新 close —— 跟 backtest 的 decision_date 脱节，
            verdict_review 事后归因会用错时间窗口的 VIX 算 macro_shock。
            backtest 模式应传 decision_date（ISO 'YYYY-MM-DD'），让 macro
            快照时间戳和数值都对齐到历史那一天。
    """
    # captured_at 用 decision_date（backtest）或当下（实盘）
    if as_of_date:
        snapshot: Dict[str, Any] = {"captured_at": as_of_date}
    else:
        snapshot = {"captured_at": datetime.now().isoformat(timespec="seconds")}
    try:
        from utils.exchange_fee import get_history_data
        for sym, label in [("^VIX", "vix"), ("^TNX", "tnx"),
                           ("USDCNY=X", "usdcny"), ("AUDCNY=X", "audcny")]:
            df = get_history_data(sym, "5d", as_of_date=as_of_date)
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

    # backtest 时 macro_ctx 必须用 decision_date，不是当下 now()。否则
    # verdict_review 事后归因会用错时间窗口的 VIX 值算 macro_shock。
    macro_ctx = _capture_macro_context(as_of_date=date_override)

    lines = [
        f"# Committee: {report.asset.get('display_name', report.asset.get('symbol'))}",
        f"\n**Date**: {today}",
        # 真实 yfinance symbol（机器可读）。文件名 safe_sym 转义有损（GC=F→GC_F、
        # NDQ.AX→NDQ_AX），verdict_review 事后复盘要靠这行拿回原 symbol 才能拉对行情。
        f"**Symbol**: {report.asset.get('symbol', '')}",
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
    ])
    # WealthContextOfficer 落盘（跨资产共享，可能为空 stub）— GUI 用来显示流动性 panel
    if report.wealth_context_view:
        lines.append(
            f"\n\n---\n\n## Wealth Context Officer (real liquidity)\n{report.wealth_context_view}"
        )
    # 确定性事实块落盘（审计 + verdict_review 事后归因 + GUI 展示）
    if report.valuation_brief:
        lines.append(
            f"\n\n---\n\n## Valuation (deterministic)\n{report.valuation_brief}"
        )
    if report.sentiment_brief:
        lines.append(
            f"\n\n---\n\n## Market Sentiment (deterministic)\n{report.sentiment_brief}"
        )
    lines.extend([
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
    "regime_label_from_text",
]
