"""委员会辩论纯核（域绑定纯模块，ADR-026）

从 core/committee/debate.py 拆出的纯计算：信号强度抽取、多轮收敛判定、
辩论历史格式化。LLM 调用（_ask/_parallel_ask/run_committee）与
CommitteeReport（报告类型，随主编排走）留在 debate.py。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

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



__all__ = [
    "_SIGNAL_RE",
    "_STRENGTH_RE",
    "_extract_signal_strength",
    "_check_convergence",
    "_format_debate_history",
]
