"""agent_io — 委员会 LLM 调用层（从 core/committee.py 拆分，逻辑逐字不变）。

职责：SDKAgent 工厂 `_create_agent` + 重试封装 `_ask` + 并行调用 `_parallel_ask`
+ transient 错误判定 `_is_transient` + LLM 重试参数常量 + 失败哨兵
`AGENT_UNAVAILABLE_MARKER`。inner import（utils.llm / core.llm_telemetry）保持
函数内，行为与扁平模块等价。
"""
from __future__ import annotations

import logging
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional, Tuple

from capabilities.sdk_agent import SDKAgent

log = logging.getLogger(__name__)

# LLM 调用重试参数（覆盖 DeepSeek 偶发的 429 / 5xx / 网络抖动）。
# 设计目标：3 次尝试在 ~14s 内完成，失败后才把空字符串回给 CIO 让它判 garbage。
LLM_MAX_ATTEMPTS = int(os.getenv("INVEST_LLM_MAX_ATTEMPTS", "3"))
LLM_BASE_DELAY = float(os.getenv("INVEST_LLM_BASE_DELAY", "2.0"))
LLM_MAX_DELAY = float(os.getenv("INVEST_LLM_MAX_DELAY", "20.0"))


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
    enable_thinking: bool = False,
    response_format: Optional[dict] = None,
) -> Optional[SDKAgent]:
    """从 LangChain SimpleAgent 迁移到 SDKAgent（OpenAI 兼容协议直连，模型由 LLM_MODEL 决定）。

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
    # provider 既是客户端构造选择，也是 telemetry 标签——从 LLM_PROVIDER 读（默认 "openai"）。
    # MiMo / DeepSeek / 千问 / 智谱 / Kimi 都走 OpenAI 兼容协议（"openai" 分支 + base_url）。
    meta = TelemetryMeta(
        agent_role=role,
        asset=asset,
        round=round_label,
        provider=provider_litellm,
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
        provider=provider_litellm,
        telemetry_meta=meta,
        enable_thinking=enable_thinking,
        response_format=response_format,
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


__all__ = [
    "LLM_MAX_ATTEMPTS",
    "LLM_BASE_DELAY",
    "LLM_MAX_DELAY",
    "_create_agent",
    "_is_transient",
    "AGENT_UNAVAILABLE_MARKER",
    "_ask",
    "_parallel_ask",
]
