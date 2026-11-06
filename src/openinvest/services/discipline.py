"""discipline — 决策与规则干预的只读描述统计。

ADR-028：不把 HOLD 多或拦截次数多当成已证明的价值，也不以增加交易频率为目标。
收益、风险与机会成本须用来源合格、成本一致的独立评估验证。

读两个已有台账,零 LLM:
- memory/.dreams/verdict_review.jsonl → 只数 source==live 行,按 verdict 计数(HOLD 占比=不作为率)
- interventions.jsonl(经 jobs.intervention_review 聚合)→ 拦截次数 + 反事实省/费钱
"""
from __future__ import annotations

import json
from collections import Counter
from typing import Any, Dict, Optional

from openinvest.core.memory_store import MemoryStore

# 拦截分桶的人话标签 + 90→60→30 取最长已结算窗
_FAMILY_LABEL = {"buy_defense": "拦加仓(快崩防御)", "trim_blocked": "拦减仓(集中度/买回点)"}


def _inaction() -> Dict[str, Any]:
    """从 verdict_review.jsonl 的 **live** 行按 verdict 计数 → 不作为率(HOLD 占比)。

    2026-10 D4:回测/污染行是模拟持仓下的回放,不是"委员会对你做了什么"——混进来时
    显示的 52% 有 99.9% 是回测行。jsonl 若被手动全量重建(--include-backtest)也不受影响。
    周末决议(weekend_dup)故意**保留**:这里数的是委员会实际发出了几次什么 verdict(频率),
    不是事后命中——周末那次委员会确实跑了、verdict 确实产出;"周五样本重复"只污染命中率分母(D8)。
    """
    p = MemoryStore().root / ".dreams" / "verdict_review.jsonl"
    rows = []
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("source") == "live":
                rows.append(r)
    c = Counter(r.get("verdict", "UNCLEAR") for r in rows)
    n = len(rows)
    return {
        "total_verdicts": n,
        "by_verdict": dict(c),
        "hold": int(c.get("HOLD", 0)),
        "hold_rate": round(c.get("HOLD", 0) / n, 3) if n else None,
    }


def _interventions() -> Dict[str, Any]:
    """从 interventions.jsonl 聚合拦截次数 + 反事实损益(复用 jobs.intervention_review)。"""
    from openinvest.jobs.intervention_review import WINDOWS, load_interventions, score, summarize
    rows = load_interventions()
    if not rows:
        return {"total": 0, "by_family": {}, "windows": list(WINDOWS)}
    by_family = summarize(score(rows), key="rule_family")
    return {
        "total": len(rows),
        "by_family": by_family,
        "windows": list(WINDOWS),
        "caveat": "正=拦错(少赚/多亏) 负=拦对(避损);每类 <20 条独立干预前不下结论",
    }


def discipline_summary() -> Dict[str, Any]:
    """委员会纪律价值聚合(只读,零 LLM)。供邮件 / CLI / Web API 共用。"""
    return {"inaction": _inaction(), "interventions": _interventions()}


def _net_settled(a: Dict[str, Any]) -> Optional[tuple]:
    """取最长已结算窗的反事实合计 → (window_days, sum_pnl);都未结算 → None。"""
    for w in (90, 60, 30):
        if a.get(f"settled_{w}d", 0):
            return w, a[f"sum_pnl_{w}d"]
    return None


def render_discipline_md(s: Optional[Dict[str, Any]] = None) -> str:
    """渲染成 markdown 小节(邮件 / CLI 人话视图)。s 缺省现算。"""
    if s is None:
        s = discipline_summary()
    ia, iv = s["inaction"], s["interventions"]
    lines = ["## 决策与规则干预记录（描述统计，不代表策略有效）"]
    if ia["total_verdicts"]:
        lines.append(
            f"- **默认不作为**(live 决议):HOLD {ia['hold']}/{ia['total_verdicts']} = "
            f"**{ia['hold_rate'] * 100:.0f}%**。"
            "HOLD 占比不能证明风险控制或收益优势，需结合持仓、成本和可执行替代方案评估。"
        )
    else:
        lines.append("- 默认不作为:暂无 verdict 记录。")
    if iv["total"]:
        parts = []
        for fam, a in sorted(iv["by_family"].items()):
            tag = _FAMILY_LABEL.get(fam, fam)
            ns = _net_settled(a)
            if ns:
                parts.append(f"{tag} {a['n']} 次(反事实 {ns[0]}d 合计 ¥{ns[1]:+,.0f})")
            else:
                parts.append(f"{tag} {a['n']} 次(未到结算窗)")
        lines.append(f"- **规则改写**:共 {iv['total']} 次——" + ";".join(parts) + "。")
        lines.append(f"  _{iv['caveat']}_")
    else:
        lines.append("- 拦截冲动操作:暂无干预记录(新机制,从近期起攒)。")
    return "\n".join(lines)


__all__ = ["discipline_summary", "render_discipline_md"]
