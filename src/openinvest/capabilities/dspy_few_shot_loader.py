"""DSPy optimized few-shot demos 加载器 —— 给 production CIO prompt 用

2026-05-18 加入。从 experiments/dspy_optimized_v2.json 读 demos，format 成
markdown examples block，给 capabilities/committee/cio.py:build_cio_prompt 注入到 cio.md
的 {{few_shot_examples}} 占位符。

为什么需要这层
==============
- DSPy 训出的 demos 直接当 few-shot 拼到现有 cio.md 是最低风险的接入方式
- 不动 production 5 角色架构（committee debate）
- demos 来自 5565 样本 × forward-window Sharpe-MDD reward 训练，verdict 分布
  覆盖全 5 种（不是 v1 的 HOLD/ACC 塌缩）

graceful 退化
=============
找不到 v2 artifact / 解析失败 → 返回空字符串，CIO 用纯 cio.md prompt 跑，
不影响 production。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List
from openinvest.paths import INVEST_ROOT

log = logging.getLogger(__name__)


DEFAULT_ARTIFACT_PATH = INVEST_ROOT / "experiments" / "dspy_optimized_v2.json"


def load_v2_few_shot_examples(
    artifact_path: Path = DEFAULT_ARTIFACT_PATH,
    max_demos: int = 6,
) -> str:
    """读 DSPy v2 artifact 提取 demos，format 成 markdown examples block

    Args:
        artifact_path: dspy_optimized_v2.json 路径
        max_demos: 最多取多少个 demo（控制 prompt 长度）

    Returns:
        markdown 字符串，包含 examples 块，前缀含"以下是历史 backtest 提炼的
        decision 样例（forward-window Sharpe-MDD 优化）"说明。
        artifact 不存在 / 解析失败时返回 ""。
    """
    if not artifact_path.exists():
        return ""

    try:
        data = json.loads(artifact_path.read_text())
        demos = data.get("predict", {}).get("demos", [])
        if not demos:
            return ""

        # 优先用 verdict 分布均匀的 demos
        demos_by_verdict: Dict[str, List[Dict[str, Any]]] = {}
        for d in demos:
            v = d.get("verdict", "UNKNOWN")
            demos_by_verdict.setdefault(v, []).append(d)

        # 每个 verdict 最多取 max_demos/5（≈1-2 个）保证覆盖
        per_verdict_cap = max(1, max_demos // 5)
        picked: List[Dict[str, Any]] = []
        for v in ("BUY", "ACCUMULATE", "HOLD", "TRIM", "SELL"):
            for d in demos_by_verdict.get(v, [])[:per_verdict_cap]:
                picked.append(d)
                if len(picked) >= max_demos:
                    break
            if len(picked) >= max_demos:
                break

        if not picked:
            picked = demos[:max_demos]

        return _format_demos_markdown(picked)

    except Exception as exc:  # noqa: BLE001
        log.warning(f"加载 v2 demos 失败，CIO 用纯 cio.md prompt: {type(exc).__name__}: {exc}")
        return ""


def _format_demos_markdown(demos: List[Dict[str, Any]]) -> str:
    """format demos 成 markdown，跟 cio.md 风格对齐"""
    lines: List[str] = [
        "",
        "---",
        "",
        "## 历史 Backtest 决策样例 (DSPy v2 优化)",
        "",
        "以下样例来自 5565 个跨资产 / 跨 regime backtest 样本，用 forward 30d "
        "Sharpe-MDD reward 训练筛选。**学习 reasoning pattern，不要照抄 verdict** —— "
        "你当前的 portfolio_state 跟这些样例可能完全不同。",
        "",
    ]
    for i, d in enumerate(demos, 1):
        lines.append(f"### 样例 {i}")
        if d.get("market_context"):
            lines.append(f"**Market context**: {_truncate(d['market_context'], 200)}")
        if d.get("portfolio_state"):
            lines.append(f"**Portfolio state**: {_truncate(d['portfolio_state'], 150)}")
        if d.get("macro_context"):
            lines.append(f"**Macro**: {_truncate(d['macro_context'], 100)}")
        if d.get("reasoning"):
            lines.append(f"**Reasoning**: {_truncate(d['reasoning'], 200)}")
        if d.get("verdict"):
            lines.append(f"**Verdict**: `{d['verdict']}`")
        lines.append("")
    return "\n".join(lines)


def _truncate(s: str, n: int) -> str:
    s = str(s).strip().replace("\n", " ")
    return s if len(s) <= n else s[:n] + "..."


__all__ = ["load_v2_few_shot_examples", "DEFAULT_ARTIFACT_PATH"]
