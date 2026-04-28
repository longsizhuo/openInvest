"""OpenClaw 风格 Dreaming 三阶段 - 把短期信号整合成长期 insights

每天凌晨 03:00 跑（jobs/dreaming.yml）。把过去交易 + 同期市场数据交叉，
找出可重复的"用户行为模式 vs 市场结果"，通过阈值门的写入 memory/insights/。

阶段：
  Light Sleep  — 读 portfolio_history + 当时市场价 → .dreams/short-term-recall.json
  REM Sleep    — 跨笔聚合找模式 → .dreams/candidates.json
  Deep Sleep   — 阈值门 (score≥0.8 / count≥3) → insights/*.md + MEMORY.md + DREAMS.md

输入：
  - memory/portfolio_history.jsonl  (实际交易)
  - utils.exchange_fee.get_history_data  (历史行情)
输出：
  - memory/.dreams/short-term-recall.json
  - memory/.dreams/candidates.json
  - memory/.dreams/events.jsonl       (审计)
  - memory/insights/<topic>.md         (Deep 通过)
  - memory/DREAMS.md                   (人类可读叙事)

故意不依赖 LLM — 评分纯统计 + 模板化叙事，零 token 成本。
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from core.consolidation_lock import (
    rollback_consolidation_lock,
    try_acquire_consolidation_lock,
)
from core.memory_store import MemoryStore
from utils.exchange_fee import get_history_data

LOOKBACK_DAYS = 90        # Light Sleep 摄入最近 N 天交易
WINDOWS = [7, 30]         # 回看交易后 N 天的市场表现
MIN_RECALL = 3            # 一个 pattern 至少出现 3 次
MIN_SCORE = 0.8           # Deep Sleep 阈值（OpenClaw 同款）

# 资产 → 行情对照符号
ASSET_PRICE_SYMBOL = {
    "GOLD-CNY": "GC=F",
    "NDQ.AX": "NDQ.AX",
}

# 上下文指标符号（每笔交易都拉一遍）
CONTEXT_SYMBOLS = {
    "vix": "^VIX",
    "tnx": "^TNX",
    "usdcny": "USDCNY=X",
}


# ----------------------------------------------------------------------
# Light Sleep — 摄入交易 + 同期市场上下文
# ----------------------------------------------------------------------

def _safe_close(df: pd.DataFrame, on_or_before: str) -> Optional[float]:
    """返回 <= 指定日期的最后一行 Close；没有则 None"""
    if df.empty:
        return None
    cutoff = pd.to_datetime(on_or_before)
    sub = df[df.index <= cutoff]
    if sub.empty:
        return None
    return float(sub["Close"].iloc[-1])


def _market_outcome(symbol: str, trade_date: str, days_ahead: int) -> Optional[float]:
    """交易日 close 到 N 天后 close 的涨跌幅（百分比）"""
    df = get_history_data(symbol, "2y")
    base = _safe_close(df, trade_date)
    if base is None or base <= 0:
        return None
    end_date = (datetime.strptime(trade_date[:10], "%Y-%m-%d")
                + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
    end = _safe_close(df, end_date)
    if end is None:
        return None
    return (end / base - 1) * 100


def light_sleep(store: MemoryStore) -> List[Dict[str, Any]]:
    """读历史交易，每笔补当时市场上下文 + 后续 7d/30d 表现"""
    history = store.read_history()
    cutoff = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")

    signals: List[Dict[str, Any]] = []
    for t in history:
        trade_date = str(t.get("ts_origin", t.get("ts", "")))[:10]
        if not trade_date or trade_date < cutoff:
            continue

        symbol = t.get("symbol", "")
        price_sym = ASSET_PRICE_SYMBOL.get(symbol)

        context: Dict[str, float] = {}
        for label, ctx_sym in CONTEXT_SYMBOLS.items():
            df = get_history_data(ctx_sym, "2y")
            v = _safe_close(df, trade_date)
            if v is not None:
                context[label] = round(v, 4)

        outcomes: Dict[str, Optional[float]] = {}
        if price_sym:
            for w in WINDOWS:
                # _market_outcome 内部每次都会拉 2y 历史；判空+round 复用同一次结果，
                # 避免 N 笔交易 × M window 把行情拉取量翻倍
                ret = _market_outcome(price_sym, trade_date, w)
                outcomes[f"return_{w}d"] = round(ret, 2) if ret is not None else None

        signals.append({
            "trade_date": trade_date,
            "asset": symbol,
            "action": t.get("action"),
            "units": t.get("units"),
            "price": t.get("price_per_unit"),
            "context": context,
            "outcomes": outcomes,
        })

    store.write_dream_state("short-term-recall", {"signals": signals,
                                                    "generated_at": datetime.now().isoformat()})
    store.dream_event({"phase": "light_sleep", "signals_collected": len(signals)})
    return signals


# ----------------------------------------------------------------------
# REM Sleep — 跨笔聚合找模式
# ----------------------------------------------------------------------

def _classify_regime(ctx: Dict[str, float]) -> Tuple[str, ...]:
    """把上下文离散化为 regime tag（用于聚合）"""
    tags = []
    if "vix" in ctx:
        if ctx["vix"] < 18:
            tags.append("vix_low")
        elif ctx["vix"] < 25:
            tags.append("vix_mid")
        else:
            tags.append("vix_high")
    if "tnx" in ctx:
        if ctx["tnx"] < 4.0:
            tags.append("tnx_low")
        elif ctx["tnx"] < 4.5:
            tags.append("tnx_mid")
        else:
            tags.append("tnx_high")
    return tuple(sorted(tags))


def rem_sleep(store: MemoryStore, signals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按 (asset, action, regime) 聚合，统计命中率"""
    buckets: Dict[Tuple, List[Dict[str, Any]]] = defaultdict(list)
    for s in signals:
        if s.get("action") not in {"bought", "sold"}:
            continue
        key = (s["asset"], s["action"], _classify_regime(s.get("context", {})))
        buckets[key].append(s)

    candidates: List[Dict[str, Any]] = []
    for (asset, action, regime), items in buckets.items():
        if len(items) < MIN_RECALL:
            continue
        # 收益评估：买入希望涨，卖出希望跌
        sign = 1 if action == "bought" else -1
        for window in WINDOWS:
            valid_outcomes = [
                i["outcomes"].get(f"return_{window}d")
                for i in items
                if i["outcomes"].get(f"return_{window}d") is not None
            ]
            if len(valid_outcomes) < MIN_RECALL:
                continue
            adjusted = [v * sign for v in valid_outcomes]
            hit_rate = sum(1 for v in adjusted if v > 0) / len(adjusted)
            avg_return = sum(adjusted) / len(adjusted)

            candidates.append({
                "asset": asset,
                "action": action,
                "regime": list(regime),
                "window_days": window,
                "count": len(adjusted),
                "hit_rate": round(hit_rate, 3),
                "avg_return_pct": round(avg_return, 2),
            })

    store.write_dream_state("candidates", {"candidates": candidates,
                                              "generated_at": datetime.now().isoformat()})
    store.dream_event({"phase": "rem_sleep", "candidates": len(candidates)})
    return candidates


# ----------------------------------------------------------------------
# Deep Sleep — 阈值门 + 写 insights
# ----------------------------------------------------------------------

def _score(c: Dict[str, Any]) -> float:
    """综合评分：命中率 0.5 + 平均收益绝对值（截断 5%）0.3 + 样本量 0.2"""
    hit = c["hit_rate"]
    avg = abs(c["avg_return_pct"]) / 5.0    # 5% 视为满分
    avg = min(avg, 1.0)
    sample = min(c["count"] / 10.0, 1.0)
    return round(hit * 0.5 + avg * 0.3 + sample * 0.2, 3)


def _slugify(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]+", "_", text).strip("_").lower()


def deep_sleep(store: MemoryStore, candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """阈值门通过的写 insights/*.md + 更新 MEMORY.md + 追加 DREAMS.md"""
    accepted: List[Dict[str, Any]] = []
    for c in candidates:
        score = _score(c)
        if score < MIN_SCORE or c["count"] < MIN_RECALL:
            continue
        c["score"] = score
        accepted.append(c)

    if not accepted:
        store.dream_event({"phase": "deep_sleep", "accepted": 0,
                          "note": "no_candidate_passed_threshold"})
        return []

    insights_dir = store.root / "insights"
    insights_dir.mkdir(parents=True, exist_ok=True)

    for c in accepted:
        regime_tag = "_".join(c["regime"]) or "any"
        slug = _slugify(f"{c['asset']}_{c['action']}_{regime_tag}_{c['window_days']}d")
        body = f"""# 长期洞察: {c['asset']} / {c['action']} 在 {regime_tag} 市场环境下

## 统计

- 样本数: {c['count']}
- 命中率: {c['hit_rate']*100:.1f}%
- 平均 {c['window_days']}天后收益（按操作方向调整）: {c['avg_return_pct']:+.2f}%
- 综合评分: {c['score']:.3f}

## 解读

历史上你在 **{regime_tag}** 时{c['action']} {c['asset']}，
{c['window_days']} 天后市场表现平均 {c['avg_return_pct']:+.2f}%
（对你 {c['action']} 的方向而言，命中率 {c['hit_rate']*100:.0f}%）。

> 这条洞察由 Dreaming Deep Sleep 自动生成，会在每日 manager agent 决策时
> 通过 `MEMORY.md` 索引被注入到 prompt 上下文，作为"用户实证数据"参考。
"""
        store.write(f"insights/{slug}", "insight", c, body)
        store.dream_event({"phase": "deep_sleep", "accepted": slug, "score": c["score"]})

    # 更新 MEMORY.md 索引
    _update_memory_index(store, accepted)

    # 追加 DREAMS.md 叙事日记
    _append_dreams_diary(store, accepted)

    return accepted


def _candidate_slug(c: Dict[str, Any]) -> str:
    regime_tag = "_".join(c["regime"]) or "any"
    return _slugify(f"{c['asset']}_{c['action']}_{regime_tag}_{c['window_days']}d")


def _update_memory_index(store: MemoryStore, accepted: List[Dict[str, Any]]) -> None:
    index_path = store.root / "MEMORY.md"
    existing = index_path.read_text(encoding="utf-8") if index_path.exists() else ""

    insight_lines = []
    for c in accepted:
        slug = _candidate_slug(c)
        regime_str = "_".join(c["regime"]) or "any"
        insight_lines.append(
            f"- [insights/{slug}.md](insights/{slug}.md) — "
            f"{c['asset']} / {c['action']} / {regime_str} / "
            f"{c['window_days']}d / score={c['score']:.2f}"
        )
    insight_block = (
        "\n## 长期洞察 (Deep Sleep 写入 - 自动维护)\n\n"
        + "\n".join(insight_lines) + "\n"
    )

    if "## 长期洞察 (Deep Sleep" in existing:
        # 替换旧块（到下一个 `## ` 或文件结尾）
        existing = re.sub(
            r"\n## 长期洞察 \(Deep Sleep.*?(?=\n## |\Z)",
            insight_block,
            existing,
            flags=re.DOTALL,
        )
    else:
        existing = existing.rstrip() + "\n" + insight_block
    index_path.write_text(existing, encoding="utf-8")


def _append_dreams_diary(store: MemoryStore, accepted: List[Dict[str, Any]]) -> None:
    """模板化叙事 - 不依赖 LLM"""
    today = datetime.now().strftime("%Y-%m-%d")
    diary_path = store.root / "DREAMS.md"
    is_new = not diary_path.exists()

    lines = [f"\n## {today} 梦日记\n"]
    if not accepted:
        lines.append("- 今晚平静，没有新的洞察通过阈值门。\n")
    else:
        lines.append(f"- 今晚 Deep Sleep 处理了 {len(accepted)} 条新洞察：\n")
        for c in accepted:
            regime_tag = "/".join(c["regime"]) or "任意"
            verb = "买入" if c["action"] == "bought" else "卖出"
            outcome_word = "上涨" if c["avg_return_pct"] > 0 else "下跌"
            lines.append(
                f"  - 在 {regime_tag} 环境下{verb} {c['asset']}，"
                f"{c['window_days']}天后市场平均{outcome_word} "
                f"{abs(c['avg_return_pct']):.1f}% "
                f"(命中率 {c['hit_rate']*100:.0f}%, 评分 {c['score']:.2f})\n"
            )

    with open(diary_path, "a", encoding="utf-8") as f:
        if is_new:
            f.write("# Dreams 梦日记\n\n这里记录 Dreaming Deep Sleep 每日的反思。\n")
        f.writelines(lines)


# ----------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------

def run() -> Dict[str, Any]:
    """跑三阶段 dreaming，带 consolidation lock 防止多进程并发撕裂数据

    锁仿 Claude Code v2.1.88 leaked 的 src/services/autoDream/consolidationLock.ts
    （PID + mtime 文件锁，60min stale guard）
    """
    store = MemoryStore()
    prior = try_acquire_consolidation_lock(store.root)
    if prior is None:
        return {"status": "skipped", "reason": "consolidation_lock_held"}

    try:
        store.dream_event({"phase": "start", "lock_acquired": True})
        signals = light_sleep(store)
        candidates = rem_sleep(store, signals)
        accepted = deep_sleep(store, candidates)
        store.dream_event({"phase": "end", "accepted": len(accepted)})
        return {
            "status": "success",
            "signals": len(signals),
            "candidates": len(candidates),
            "accepted_insights": len(accepted),
        }
    except Exception as e:
        # 出错就把 mtime 倒回去，下次还能跑
        rollback_consolidation_lock(store.root, prior)
        store.dream_event({"phase": "error", "error": str(e)})
        raise


if __name__ == "__main__":
    print(run())
