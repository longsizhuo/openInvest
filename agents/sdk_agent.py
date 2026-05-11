"""SDK 直连 LLM agent，替代 LangChain SimpleAgent — 支持原生 tool calling。

设计：
- DeepSeek 走 OpenAI 兼容协议（`openai.OpenAI(base_url=...)`），function calling 支持
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

from agents.tools import TOOL_DEFINITIONS, execute_tool_call
from core.llm_telemetry import TelemetryMeta, record_llm_call, record_tool_call


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
        model: str = "deepseek-v4-flash",
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        temperature: float = 0.2,
        enable_tools: bool = True,
        max_tool_iterations: int = 4,
        provider: str = "deepseek",
        telemetry_meta: Optional[TelemetryMeta] = None,
    ):
        self.system_prompt = system_prompt
        self.model = model
        self.temperature = temperature
        self.enable_tools = enable_tools
        self.max_tool_iterations = max_tool_iterations
        self.provider = provider
        self.last_tool_calls: List[ToolCallTrace] = []
        # v3 透明化：LLM 调用元数据；caller 不传则用默认匿名
        self.telemetry_meta = telemetry_meta or TelemetryMeta(
            provider=provider, model=model,
        )
        # 让 telemetry meta 与运行时字段同步
        self.telemetry_meta.provider = provider
        self.telemetry_meta.model = model

        if provider == "deepseek":
            self.client = OpenAI(
                api_key=api_key or os.getenv("DEEPSEEK_API_KEY"),
                base_url=base_url or os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            )
        elif provider == "openai":
            self.client = OpenAI(api_key=api_key or os.getenv("OPENAI_API_KEY"))
        else:
            raise ValueError(f"未支持的 provider: {provider}（目前 deepseek/openai）")

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
        # v4-flash 默认 thinking 模式，multi-turn 需要把 reasoning_content 回传；
        # 我们的 tool-loop 没传 → 报 "reasoning_content must be passed back"。
        # 关掉 thinking → 跟旧 deepseek-chat 行为完全一致（fast / non-thinking）
        if "v4" in self.model.lower() or self.model == "deepseek-chat":
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
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
            messages.append({
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
            })

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
