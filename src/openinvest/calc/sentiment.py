"""事件情绪打分纯核（calc 层，ADR-026）

从 utils/sentiment.py 拆出的纯计算：event_brief 头行解析 + 加权净分 + VIX 分位标签。
IO（VIX 拉取 / CNN urlopen / build_sentiment_brief 组装 / per-symbol 代理集合
匹配——依赖 services.symbol_map）留在 utils/sentiment.py（IO shell）。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


def _vix_label(pct: float) -> str:
    """VIX 自身分位 → 恐慌贪婪标签（高 VIX = 恐慌）。阈值走 config (sentiment 节)。"""
    from openinvest.core.config import load_config
    cfg = load_config().sentiment
    if pct >= cfg.vix_extreme_fear_q:
        return "extreme_fear"
    if pct >= cfg.vix_fear_q:
        return "fear"
    if pct <= cfg.vix_extreme_greed_q:
        return "extreme_greed"
    if pct <= cfg.vix_greed_q:
        return "greed"
    return "neutral"


# event_brief 头行解析（格式契约：core/committee_runner.py:format_event_brief 产出
# `[ts] [stance/severity] [SYM1, SYM2] (sources: ...)`，两处 docstring 互相引用；
# 改任何一边必须同步另一边 + tests/test_event_rag_resolve.py 的互解析回归测试）
_EVENT_HEADER_RE = re.compile(
    r"^\[(?P<ts>[^\]]*)\]\s+\[(?P<stance>risk|opportunity|neutral)/"
    r"(?P<sev>low|mid|high)\]\s+\[(?P<syms>[^\]]*)\]",
    re.MULTILINE,
)
_SEV_INDEX = {"low": 0, "mid": 1, "high": 2}
_STANCE_SIGN = {"opportunity": 1.0, "risk": -1.0, "neutral": 0.0}


def _parse_event_brief_entries(event_brief: str) -> List[Dict[str, Any]]:
    """解析 brief 头行 → [{ts, stance, sev, syms}]。纯文本解析，零新 IO。

    容错：ts 缺失/不可解析（LLM 透传的 ts 可能 naive/空/错标）→ ts=None，
    打分时按无衰减计（recall 7d 窗口兜底过权风险）。naive ts 当 UTC。
    """
    entries: List[Dict[str, Any]] = []
    for m in _EVENT_HEADER_RE.finditer(event_brief or ""):
        ts: Optional[datetime] = None
        raw_ts = m.group("ts").strip()
        if raw_ts:
            try:
                ts = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except ValueError:
                ts = None
        syms = {s.strip().upper() for s in m.group("syms").split(",") if s.strip()}
        entries.append({
            "ts": ts, "stance": m.group("stance"), "sev": m.group("sev"), "syms": syms,
        })
    return entries


def _stance_score(
    entries: List[Dict[str, Any]], *, now: datetime,
) -> Tuple[float, int, int]:
    """加权净分 (score, risk计数, opportunity计数)。

    score = Σ sign × w(severity) × 0.5^(age_h/half_life)；opportunity 正、risk 负、
    neutral 0；age 负值（错标未来）clamp 0。**默认 config（等权 + half_life=0 禁用
    衰减）下 score ≡ opportunity计数 − risk计数，net 判定与旧纯计数逐位一致**——
    加权公式经 scripts/research/eval_event_stance.py 验证前保持禁用（2026-06-11 基线判
    INSUFFICIENT_DATA，ADR-010 rule 4 纪律）。

    calc 纯核：`now` **必传**（时间是输入不是环境）——IO shell（utils/sentiment.py）
    的调用点负责补 `datetime.now(timezone.utc)`。
    """
    from openinvest.core.config import load_config
    cfg = load_config().sentiment
    weights = (cfg.event_stance_w_low, cfg.event_stance_w_mid, cfg.event_stance_w_high)
    half_life = cfg.event_stance_half_life_hours
    score, risk, opp = 0.0, 0, 0
    for e in entries:
        sign = _STANCE_SIGN.get(e["stance"], 0.0)
        if e["stance"] == "risk":
            risk += 1
        elif e["stance"] == "opportunity":
            opp += 1
        if sign == 0.0:
            continue
        w = weights[_SEV_INDEX.get(e["sev"], 0)]
        if half_life > 0 and e["ts"] is not None:
            age_h = max(0.0, (now - e["ts"]).total_seconds() / 3600.0)
            w *= 0.5 ** (age_h / half_life)
        score += sign * w
    return score, risk, opp


__all__ = [
    "_vix_label",
    "_EVENT_HEADER_RE",
    "_SEV_INDEX",
    "_STANCE_SIGN",
    "_parse_event_brief_entries",
    "_stance_score",
]
