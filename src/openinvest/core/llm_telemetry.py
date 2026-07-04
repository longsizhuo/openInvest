"""LLM 调用 telemetry — 记录每次 LLM 调用的成本/时长/tool calls

设计目标：
- 透明化：让 GUI 能告诉用户「跑一次委员会花了 X 元 / Y 秒 / 调了 Z 个 tool」
- 零侵入：用 record_llm_call() 函数包装，调用方不改原逻辑
- 统一存储：所有 record append 到 memory/.state/llm_usage.jsonl，每行一条 JSON
- 价格估算：DeepSeek 公开定价（input ¥0.5/1M / output ¥1.5/1M），其他 provider 加分支

事件 schema:
{
  "ts": "2026-05-06T18:00:00+08:00",
  "agent_role": "macro" | "quant" | "risk" | "cio" | "skill" | "unknown",
  "asset": "NDQ.AX" | null,
  "round": "opening" | "rebuttal" | "macro" | "cio" | null,
  "provider": "deepseek" | "openai",
  "model": "deepseek-v4-flash",
  "input_tokens": int,
  "output_tokens": int,
  "total_tokens": int,
  "latency_ms": int,
  "cost_cny": float,
  "tool_calls": int,           # 这一次 LLM 调用主动调了多少 tool
  "iteration": int,            # 多轮 tool calling 时是第几轮
  "ok": bool,                  # 失败也记一条（input_tokens=0, output_tokens=0）
  "error": str | null,
}
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional
from openinvest.paths import INVEST_ROOT

log = logging.getLogger(__name__)

# DeepSeek 公开定价（官方 pricing page，单位 CNY / 1M tokens，2026-06 核对）
# https://api-docs.deepseek.com/zh-cn/quick_start/pricing
# v4-flash = 旧 deepseek-chat 非 thinking 模式；v4-pro = 旧 deepseek-reasoner thinking 模式
# input_cache_hit = KVCache 命中价（共享前缀复用，便宜 ~50×）；input = 未命中价。
# 官方页原文：flash 命中 0.02 / 未命中 1 / 输出 2；pro 命中 0.025 / 未命中 3 / 输出 6。
PRICING_CNY_PER_M_TOKENS: Dict[str, Dict[str, float]] = {
    "deepseek-v4-flash": {"input": 1.0, "input_cache_hit": 0.02, "output": 2.0},
    "deepseek-v4-pro": {"input": 3.0, "input_cache_hit": 0.025, "output": 6.0},
    # legacy 名（兼容旧 telemetry 日志；2026-07-24 弃用，等价 v4-flash/v4-pro）
    "deepseek-chat": {"input": 1.0, "input_cache_hit": 0.02, "output": 2.0},
    "deepseek-reasoner": {"input": 3.0, "input_cache_hit": 0.025, "output": 6.0},
    # OpenAI 价格更贵，先用 gpt-4o 作为占位（如真用上要更新）
    "gpt-4o": {"input": 17.0, "output": 70.0},        # ~$2.5/$10 ≈ ¥17/¥70
    "gpt-4o-mini": {"input": 1.1, "output": 4.3},
}

# 落盘路径
TELEMETRY_FILE = INVEST_ROOT / "memory" / ".state" / "llm_usage.jsonl"
TOOL_CALLS_FILE = INVEST_ROOT / "memory" / ".state" / "tool_calls.jsonl"


@dataclass
class TelemetryMeta:
    """每个 agent 实例的元数据，构造时塞进，所有 LLM 调用都带上"""
    agent_role: str = "unknown"     # macro / quant / risk / cio / skill / unknown
    asset: Optional[str] = None     # NDQ.AX / GC=F / null
    round: Optional[str] = None     # opening / rebuttal / macro / cio
    provider: str = "deepseek"
    model: str = "deepseek-v4-flash"
    extra: Dict[str, Any] = field(default_factory=dict)


def estimate_cost_cny(model: str, input_tokens: int, output_tokens: int,
                      cache_hit_tokens: int = 0) -> float:
    """根据公开定价估算单次调用成本。

    cache_hit_tokens（KVCache 命中的输入 token 数，DeepSeek usage 返回）按命中价计，
    其余输入按未命中价——不传则全按未命中（保守上界，旧行为）。
    """
    pricing = PRICING_CNY_PER_M_TOKENS.get(model)
    if not pricing:
        return 0.0
    hit = min(max(cache_hit_tokens or 0, 0), input_tokens or 0)
    miss = (input_tokens or 0) - hit
    hit_rate = pricing.get("input_cache_hit", pricing["input"])
    return round(
        miss / 1_000_000 * pricing["input"]
        + hit / 1_000_000 * hit_rate
        + (output_tokens or 0) / 1_000_000 * pricing["output"],
        6,
    )


def record_llm_call(
    meta: TelemetryMeta,
    *,
    input_tokens: int,
    output_tokens: int,
    latency_ms: int,
    tool_calls: int = 0,
    iteration: int = 0,
    ok: bool = True,
    error: Optional[str] = None,
    cache_hit_tokens: int = 0,
) -> Dict[str, Any]:
    """记录一次 LLM 调用。线程安全（单 append fcntl-style 不严格，jsonl 容忍并发追加）

    cache_hit_tokens：KVCache 命中的输入 token（DeepSeek usage 返回）→ 按命中价计真实成本。

    返回 record dict（caller 可用作 logging）。
    """
    cost = estimate_cost_cny(meta.model, input_tokens, output_tokens, cache_hit_tokens)
    record: Dict[str, Any] = {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "agent_role": meta.agent_role,
        "asset": meta.asset,
        "round": meta.round,
        "provider": meta.provider,
        "model": meta.model,
        "input_tokens": int(input_tokens or 0),
        "cache_hit_tokens": int(cache_hit_tokens or 0),
        "output_tokens": int(output_tokens or 0),
        "total_tokens": int((input_tokens or 0) + (output_tokens or 0)),
        "latency_ms": int(latency_ms),
        "cost_cny": cost,
        "tool_calls": tool_calls,
        "iteration": iteration,
        "ok": ok,
        "error": error,
    }
    if meta.extra:
        record["extra"] = meta.extra

    try:
        TELEMETRY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(TELEMETRY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001  落盘失败不能阻断业务
        log.warning(f"telemetry 落盘失败: {e}")

    return record


def read_telemetry(since: int = 200) -> list:
    """读最近 N 条 telemetry record（按 append 顺序倒序）"""
    if not TELEMETRY_FILE.exists():
        return []
    try:
        with open(TELEMETRY_FILE, encoding="utf-8") as f:
            lines = f.readlines()
    except Exception as e:  # noqa: BLE001
        log.warning(f"读 telemetry 失败: {e}")
        return []
    out = []
    for line in lines[-since:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def record_tool_call(
    meta: TelemetryMeta,
    *,
    tool_name: str,
    arguments: Dict[str, Any],
    result_preview: str,
    latency_ms: int,
    iteration: int = 0,
) -> Dict[str, Any]:
    """记录 LLM 主动调用某个 tool 的 audit log

    给 GUI 透明化用：用户能看到「Quant Round 1 在 18:05 调了 analyze_multi_timeframe(NDQ.AX)
    耗时 350ms，返回 [前 200 字...]」
    """
    record: Dict[str, Any] = {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "agent_role": meta.agent_role,
        "asset": meta.asset,
        "round": meta.round,
        "tool_name": tool_name,
        "arguments": arguments,
        "result_preview": result_preview,
        "latency_ms": int(latency_ms),
        "iteration": iteration,
    }
    try:
        TOOL_CALLS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(TOOL_CALLS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception as e:  # noqa: BLE001
        log.warning(f"tool_calls 落盘失败: {e}")
    return record


def read_tool_calls(since: int = 200) -> list:
    """读最近 N 条 tool call 记录"""
    if not TOOL_CALLS_FILE.exists():
        return []
    try:
        with open(TOOL_CALLS_FILE, encoding="utf-8") as f:
            lines = f.readlines()
    except Exception as e:  # noqa: BLE001
        log.warning(f"读 tool_calls 失败: {e}")
        return []
    out = []
    for line in lines[-since:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def telemetry_summary(since_records: int = 1000) -> Dict[str, Any]:
    """汇总：今日 / 本月 / 全期 token + cost + 调用次数；按 agent_role 拆分"""
    records = read_telemetry(since=since_records)
    if not records:
        return {
            "total_calls": 0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_cost_cny": 0.0,
            "by_role": {},
        }

    total_calls = len(records)
    total_input = sum(r.get("input_tokens", 0) for r in records)
    total_output = sum(r.get("output_tokens", 0) for r in records)
    total_cost = round(sum(r.get("cost_cny", 0) for r in records), 4)

    by_role: Dict[str, Dict[str, Any]] = {}
    for r in records:
        role = r.get("agent_role", "unknown")
        d = by_role.setdefault(role, {
            "calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_cny": 0.0,
            "avg_latency_ms": 0,
        })
        d["calls"] += 1
        d["input_tokens"] += r.get("input_tokens", 0)
        d["output_tokens"] += r.get("output_tokens", 0)
        d["cost_cny"] = round(d["cost_cny"] + r.get("cost_cny", 0), 4)
        # 用累加平均的中间结果（最后再除）
        d["_lat_sum"] = d.get("_lat_sum", 0) + r.get("latency_ms", 0)
    for d in by_role.values():
        if d["calls"]:
            d["avg_latency_ms"] = d["_lat_sum"] // d["calls"]
            d.pop("_lat_sum", None)

    return {
        "total_calls": total_calls,
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_cost_cny": total_cost,
        "by_role": by_role,
    }
