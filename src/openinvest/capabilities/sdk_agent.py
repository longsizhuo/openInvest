"""SDK 直连 LLM agent，替代 LangChain SimpleAgent — 支持原生 tool calling。

设计：
- OpenAI 兼容端点（MiMo / DeepSeek / 千问 / 智谱 / Kimi）走 `openai.OpenAI(base_url=...)`，function calling 支持
- 未来加 Anthropic provider 时，本文件加 if provider == 'anthropic' 分支即可
- Tool calling loop：LLM 每次返回 tool_calls → 我们调对应 impl → 把结果当 message 塞回去 → 继续直到 LLM 输出文本

Hybrid 设计（防 DeepSeek tool calling 不稳）：
- caller 仍传 user_prompt 含 baseline brief（最低保障）
- LLM 可选择调 tool 补充查询，也可选择不调直接回答
- max_tool_iterations 限上限，避免 LLM 死循环
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from openai import OpenAI

from openinvest.capabilities.tools import TOOL_DEFINITIONS, execute_tool_call
from openinvest.core.llm_telemetry import TelemetryMeta, record_llm_call, record_tool_call
from openinvest.utils.llm import get_thinking_disable_kwargs


@dataclass
class ToolCallTrace:
    """记录 LLM 主动调用了哪些 tool，给 transcript / debug / GUI 透明化用"""
    tool_name: str
    arguments: Dict[str, Any]
    result_preview: str  # 前 200 字
    iteration: int
    latency_ms: int = 0          # 该 tool 执行耗时（v3 透明化加）
    ts: str = ""                 # 该 tool 调用的时间戳


class SDKAgent:
    """单角色 LLM agent（Quant / Risk / Macro / CIO 各一个实例）。

    与 LangChain SimpleAgent 接口对齐：构造时给 system_prompt + 配置，调用 .run(user_prompt)。
    返回纯文本（已经过 tool calling loop 综合）。

    .last_tool_calls 暴露本次 LLM 主动调用了哪些 tool（给 audit / transcript 用）。
    """

    def __init__(
        self,
        *,
        system_prompt: str,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        temperature: float = 0.2,
        enable_tools: bool = True,
        max_tool_iterations: int = 4,
        provider: str = "openai",
        telemetry_meta: Optional[TelemetryMeta] = None,
        enable_thinking: bool = False,
        response_format: Optional[Dict[str, Any]] = None,
    ):
        # caller 不传 model → 走 utils.llm.get_llm_config（按 LLM_MODEL 决定，兜底 deepseek-v4-flash）
        if model is None:
            from openinvest.utils.llm import get_llm_config_safe
            _api_key, _base, model, _provider = get_llm_config_safe()
        self.system_prompt = system_prompt
        self.model = model
        self.temperature = temperature
        self.enable_tools = enable_tools
        self.max_tool_iterations = max_tool_iterations
        # 默认沿用全局策略（committee 4 worker 已思考 → disable 走 fast path）。
        # enable_thinking=True 给单角色（如 CIO 终裁）开思考做 A/B；DeepSeek 思考的
        # reasoning_content 与 content 分开，不像 MiMo 会吃空 content。
        self.enable_thinking = enable_thinking
        # response_format={"type":"json_object"} 强制结构化输出（DeepSeek JSON Output）。
        # 仅 caller 确认 provider 支持时传（见 utils.llm.supports_json_output 门控）。
        self.response_format = response_format
        self.provider = provider
        self.last_tool_calls: List[ToolCallTrace] = []
        # v3 透明化：LLM 调用元数据；caller 不传则用默认匿名
        self.telemetry_meta = telemetry_meta or TelemetryMeta(
            provider=provider, model=model,
        )
        # 让 telemetry meta 与运行时字段同步
        self.telemetry_meta.provider = provider
        self.telemetry_meta.model = model

        if provider in ("openai", "deepseek"):
            # 任意 OpenAI 兼容端点（MiMo / DeepSeek / 千问 / 智谱 / Kimi / OpenAI 官方）。
            # caller 没显式传 key/base_url 就回落到通用 LLM_* 配置（再 fallback DEEPSEEK_*）。
            if api_key is None or base_url is None:
                from openinvest.utils.llm import get_llm_config_safe
                _ak, _bu, _m, _p = get_llm_config_safe()
                api_key = api_key or _ak
                base_url = base_url or _bu
            if not api_key:
                # 不裸抛 openai.OpenAIError——那条消息只说"api_key 没设"，对一个
                # agentic 调用方（如 Hermes cron）是个死胡同：与其看懂这句话去
                # 配置一个它可能拿不到的密钥，agent 更可能"自己想办法把它跑起来"
                # ——绕过这个错误、拼凑一个我们看不懂的变通方案、然后从此这个
                # 用户的委员会跑在一套没人维护、没人能诊断的路径上，我们永远
                # 失去了帮他排障或推送修复的机会（2026-07-14 用户指出的真实风险，
                # 不是假设）。这条消息必须直接给出两条正规出路，不留"自己想办法"
                # 的空间。
                raise ValueError(
                    "未配置 LLM_API_KEY / DEEPSEEK_API_KEY，无法跑委员会 Direct 路径。"
                    "不要尝试绕过这个限制自行拼凑变通方案（如编造 verdict、写脚本"
                    "伪造结果）——只有两条正规出路："
                    "1) 在 INVEST_HOME/.env 配置 DEEPSEEK_API_KEY 后重试；"
                    "2) 如果你（调用方 agent）支持子任务委派"
                    "（Claude Code 的 Agent({...})、Hermes 的 delegate_task 等），"
                    "改走 Coordinator 协议——用 prepare_committee 取 brief，自己"
                    "spawn 4 个隔离子任务扮演 Macro/Quant/Risk/CIO，"
                    "再用 save_committee 落盘，全程不需要这个 key。"
                    "具体步骤见 references/committee-protocol.md"
                    "（Claude Code）或 references/committee-protocol-hermes.md"
                    "（Hermes / 其他支持子任务委派的 agent）。"
                )
            self.client = OpenAI(api_key=api_key, base_url=base_url)
        else:
            raise ValueError(f"未支持的 provider: {provider}（目前 openai / deepseek，均走 OpenAI 兼容协议）")

    def _call_llm_with_telemetry(self, kwargs: Dict[str, Any], iteration: int):
        """v3 透明化：统一的 LLM 调用 + telemetry 记录入口

        统一记录 input/output tokens、延迟、cost；失败也记一条（错误信息进 error 字段）。
        """
        start = time.perf_counter()
        try:
            response = self.client.chat.completions.create(**kwargs)
            latency_ms = int((time.perf_counter() - start) * 1000)

            usage = getattr(response, "usage", None)
            input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0
            output_tokens = int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0
            # KVCache 命中 token：DeepSeek 用 usage.prompt_cache_hit_tokens；
            # OpenAI 风格用 usage.prompt_tokens_details.cached_tokens。两种都抓，算真实成本。
            cache_hit_tokens = 0
            if usage:
                cache_hit_tokens = int(getattr(usage, "prompt_cache_hit_tokens", 0) or 0)
                if not cache_hit_tokens:
                    details = getattr(usage, "prompt_tokens_details", None)
                    if details:
                        cache_hit_tokens = int(getattr(details, "cached_tokens", 0) or 0)

            # 这一轮里 assistant 主动要调多少 tool（next-step）
            msg = response.choices[0].message
            tool_calls_planned = len(getattr(msg, "tool_calls", None) or [])

            record_llm_call(
                self.telemetry_meta,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=latency_ms,
                tool_calls=tool_calls_planned,
                iteration=iteration,
                ok=True,
                cache_hit_tokens=cache_hit_tokens,
            )
            return response
        except Exception as e:
            latency_ms = int((time.perf_counter() - start) * 1000)
            record_llm_call(
                self.telemetry_meta,
                input_tokens=0, output_tokens=0,
                latency_ms=latency_ms,
                iteration=iteration,
                ok=False,
                error=f"{type(e).__name__}: {e}",
            )
            raise

    def run(self, user_prompt: str) -> str:
        """跑一次 LLM 推理。如果 enable_tools=True 且 LLM 主动调 tool，自动 loop。
        每次 LLM 调用都会落 telemetry record（input/output tokens / latency / cost）"""
        self.last_tool_calls = []
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        kwargs: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        # DeepSeek v4 / MiMo v2.5 系列默认 thinking 模式 → committee 场景 disable 走 fast 路径
        # （MiMo CIO long prompt 下 thinking 会吃完 max_tokens 导致 content 为空）。
        # 不同 provider 的 extra_body 格式不同（DeepSeek 用 dict，MiMo 用 string），helper 统一处理。
        # enable_thinking=True 的角色跳过 disable，保留思考模式（P3 CIO A/B）。
        if not self.enable_thinking:
            kwargs.update(get_thinking_disable_kwargs(self.model))
        if self.response_format:
            kwargs["response_format"] = self.response_format
        if self.enable_tools:
            kwargs["tools"] = TOOL_DEFINITIONS
            kwargs["tool_choice"] = "auto"

        for iteration in range(self.max_tool_iterations + 1):
            response = self._call_llm_with_telemetry(kwargs, iteration)
            msg = response.choices[0].message

            # LLM 决定调 tool
            tool_calls = getattr(msg, "tool_calls", None) or []
            if not tool_calls:
                # 没调 tool，直接返回文本
                return msg.content or ""

            # 把 assistant message（含 tool_calls）塞回 messages，准备喂 tool 结果
            assistant_msg: Dict[str, Any] = {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in tool_calls
                ],
            }
            # Response-driven reasoning carry：上一轮 API response 含 reasoning_content
            # 就 carry 回 next-turn。不靠 model name 嗅探：
            #   - MiMo / DeepSeek-R1 / 未来 reasoning model 自动 work（API 强制要求 carry）
            #   - 千问 / 智谱 / GPT 不返回该字段 → 啥都不 carry，零侵入
            #   - OpenAI spec 规定 unknown fields 应被 ignore，所以 carry 给不需要的 provider 也无害
            reasoning_content = getattr(msg, "reasoning_content", None)
            if reasoning_content:
                assistant_msg["reasoning_content"] = reasoning_content
            messages.append(assistant_msg)

            # 执行每个 tool call（含耗时记录给 GUI 透明化）
            for tc in tool_calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                except json.JSONDecodeError:
                    args = {}
                tool_start = time.perf_counter()
                result = execute_tool_call(name, args)
                tool_latency_ms = int((time.perf_counter() - tool_start) * 1000)
                self.last_tool_calls.append(ToolCallTrace(
                    tool_name=name,
                    arguments=args,
                    result_preview=result[:200],
                    iteration=iteration,
                    latency_ms=tool_latency_ms,
                    ts=datetime.now().astimezone().isoformat(timespec="seconds"),
                ))
                # v3 透明化：tool call 持久化到 audit jsonl，给 GUI 用
                record_tool_call(
                    self.telemetry_meta,
                    tool_name=name,
                    arguments=args,
                    result_preview=result[:200],
                    latency_ms=tool_latency_ms,
                    iteration=iteration,
                )
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })

            # 进入下一轮（更新 messages，继续问 LLM）
            kwargs["messages"] = messages

        # 用完上限还没收敛——强制最后一轮不带 tool 让 LLM 出文本
        kwargs.pop("tools", None)
        kwargs.pop("tool_choice", None)
        kwargs["messages"] = messages + [{
            "role": "user",
            "content": "已达 tool 调用上限，请基于以上结果直接给出最终结论。",
        }]
        final = self._call_llm_with_telemetry(
            kwargs, iteration=self.max_tool_iterations + 1,
        )
        return final.choices[0].message.content or ""

    def tool_call_summary(self) -> str:
        """给 transcript 加一段'本次 LLM 主动调了哪些 tool'摘要"""
        if not self.last_tool_calls:
            return ""
        lines = [f"\n📞 LLM 主动调用 {len(self.last_tool_calls)} 次 tool："]
        for tc in self.last_tool_calls:
            args_str = json.dumps(tc.arguments, ensure_ascii=False)[:100]
            lines.append(f"  · {tc.tool_name}({args_str}) → {tc.result_preview[:80]}...")
        return "\n".join(lines)


__all__ = ["SDKAgent", "ToolCallTrace"]
