"""多 agent 辩论编排 - 仿 Claude Code Agent Teams 的共享状态板模式

每场辩论是一个 DebateBoard：参与方读它、说话写它、judge 读全文裁决。
辩论记录会落到 memory/.debate/<date>/<asset>.md，供后续 dreaming 整合。

辩论流程（每个资产 5 次 LLM 调用）：
  Round 1 — Opening:    bull 开局陈述 → bear 开局陈述
  Round 2 — Rebuttals:  bull 反驳 bear → bear 反驳 bull
  Round 3 — Verdict:    judge 读全文裁决

仿 OpenClaw 的 shared task list 思路（OpenClaw 4.9: agents on a team
share state directly, not just through orchestrator）。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from agents.agent import SimpleAgent
from agents.bear import build_bear_prompt
from agents.bull import build_bull_prompt
from agents.judge import build_judge_prompt
from core.memory_store import MemoryStore


@dataclass
class DebateBoard:
    """所有参与方共享的状态板"""
    asset: Dict[str, Any]
    market_data_summary: str            # 该资产的多周期分析
    macro_summary: str                  # 共享的宏观摘要
    portfolio_summary: str              # 当前持仓上下文
    prior_insights: str                 # memory/insights/ 相关的长期洞察
    transcript: List[Dict[str, str]] = field(default_factory=list)

    def speak(self, role: str, content: str) -> None:
        self.transcript.append({
            "role": role,
            "ts": datetime.now().isoformat(timespec="seconds"),
            "content": content,
        })

    def render_for(self, role: str) -> str:
        """渲染给特定 agent 看的"对话上下文"
        bull 看 bear 的发言会被标记，反之亦然。
        """
        lines = [
            f"=== ASSET: {self.asset.get('display_name', self.asset.get('symbol'))} ===",
            f"\n=== MARKET DATA (shared) ===\n{self.market_data_summary}",
            f"\n=== MACRO SUMMARY (shared) ===\n{self.macro_summary}",
            f"\n=== PORTFOLIO CONTEXT (shared) ===\n{self.portfolio_summary}",
        ]
        if self.prior_insights:
            lines.append(f"\n=== PRIOR INSIGHTS (long-term memory) ===\n{self.prior_insights}")
        if self.transcript:
            lines.append("\n=== DEBATE SO FAR ===")
            for entry in self.transcript:
                lines.append(f"\n[{entry['role'].upper()}] ({entry['ts']}):\n{entry['content']}")
        return "\n".join(lines)


# ----------------------------------------------------------------------
# Agent factory（小工具，注入 DeepSeek 或后续可替换为 Claude）
# ----------------------------------------------------------------------

def _create_agent(system_prompt: str, *, search_enabled: bool = True) -> Optional[SimpleAgent]:
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        print("❌ DEEPSEEK_API_KEY 缺失，跳过辩论")
        return None
    return SimpleAgent(
        temperature=0.3,                        # bull/bear 立场固化，温度略提鼓励多样
        enable_search=search_enabled,
        model="deepseek-chat",
        openai_api_key=api_key,
        openai_api_base=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        system_prompt=system_prompt,
        debug=False,
    )


def _ask(agent: Optional[SimpleAgent], context: str) -> str:
    """安全调用 agent.run，失败返回错误占位"""
    if agent is None:
        return "⚠️ Agent unavailable"
    try:
        return agent.run(context)
    except Exception as e:
        return f"⚠️ Agent error: {type(e).__name__}: {e}"


# ----------------------------------------------------------------------
# Verdict 解析
# ----------------------------------------------------------------------

VERDICT_RE = re.compile(r"VERDICT:\s*(BUY|HOLD|SELL)", re.I)
CONFIDENCE_RE = re.compile(r"CONFIDENCE:\s*([\d.]+)")
DOMINANT_RE = re.compile(r"DOMINANT_SIDE:\s*(bull|bear|tie)", re.I)
ALLOC_RE = re.compile(r"SUGGESTED_ALLOC_PCT:\s*(\d+)")


def parse_verdict(text: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {"raw": text}
    m = VERDICT_RE.search(text)
    out["verdict"] = m.group(1).upper() if m else "UNCLEAR"
    m = CONFIDENCE_RE.search(text)
    out["confidence"] = float(m.group(1)) if m else 0.0
    m = DOMINANT_RE.search(text)
    out["dominant_side"] = m.group(1).lower() if m else "tie"
    m = ALLOC_RE.search(text)
    out["alloc_pct"] = int(m.group(1)) if m else 0
    return out


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------

def run_debate(
    asset: Dict[str, Any],
    market_data_summary: str,
    macro_summary: str,
    portfolio_summary: str,
    prior_insights: str = "",
    *,
    persist_to_memory: bool = True,
) -> Dict[str, Any]:
    """跑一场完整的 bull/bear/judge 辩论，返回 verdict 字典"""
    board = DebateBoard(
        asset=asset,
        market_data_summary=market_data_summary,
        macro_summary=macro_summary,
        portfolio_summary=portfolio_summary,
        prior_insights=prior_insights,
    )

    bull_agent = _create_agent(build_bull_prompt(asset, "opening"))
    bear_agent = _create_agent(build_bear_prompt(asset, "opening"))

    # Round 1 - Opening
    bull_open = _ask(bull_agent, board.render_for("bull"))
    board.speak("bull_opening", bull_open)
    bear_open = _ask(bear_agent, board.render_for("bear"))
    board.speak("bear_opening", bear_open)

    # Round 2 - Rebuttals (重新建 agent 以更新 system_prompt 到 rebuttal mode)
    bull_rebut_agent = _create_agent(build_bull_prompt(asset, "rebuttal"))
    bear_rebut_agent = _create_agent(build_bear_prompt(asset, "rebuttal"))
    bull_rebut = _ask(bull_rebut_agent, board.render_for("bull"))
    board.speak("bull_rebuttal", bull_rebut)
    bear_rebut = _ask(bear_rebut_agent, board.render_for("bear"))
    board.speak("bear_rebuttal", bear_rebut)

    # Round 3 - Verdict
    judge_agent = _create_agent(build_judge_prompt(asset), search_enabled=False)
    verdict_raw = _ask(judge_agent, board.render_for("judge"))
    board.speak("judge", verdict_raw)
    verdict = parse_verdict(verdict_raw)

    # 持久化辩论记录（给 dreaming 用 + 人类回看）
    if persist_to_memory:
        _persist(board, verdict)

    return {
        "asset": asset.get("symbol"),
        "verdict": verdict,
        "transcript": board.transcript,
    }


def _persist(board: DebateBoard, verdict: Dict[str, Any]) -> None:
    store = MemoryStore()
    today = datetime.now().strftime("%Y-%m-%d")
    debate_dir = store.root / ".debate" / today
    debate_dir.mkdir(parents=True, exist_ok=True)
    safe_sym = re.sub(r"[^a-zA-Z0-9_-]", "_", board.asset.get("symbol", "asset"))
    path = debate_dir / f"{safe_sym}.md"

    lines = [
        f"# Debate: {board.asset.get('display_name', board.asset.get('symbol'))}",
        f"\n**Date**: {today}",
        f"\n**Verdict**: {verdict['verdict']} (confidence {verdict['confidence']:.2f})",
        f"\n**Dominant**: {verdict['dominant_side']}",
        f"\n**Suggested allocation**: {verdict['alloc_pct']}% of single-trade cap",
        "\n\n---\n\n## Transcript\n",
    ]
    for entry in board.transcript:
        lines.append(f"\n### [{entry['role'].upper()}] @ {entry['ts']}\n\n{entry['content']}\n")

    path.write_text("\n".join(lines), encoding="utf-8")
    store.dream_event({
        "phase": "debate_finished",
        "asset": board.asset.get("symbol"),
        "verdict": verdict["verdict"],
        "confidence": verdict["confidence"],
    })


__all__ = ["DebateBoard", "run_debate", "parse_verdict"]
