"""scripts/analyze_news_attribution —— 根因标注助手

读 memory/.dreams/verdict_review.jsonl（jobs.verdict_review 跑出来的命中率数据），
对每条 miss verdict（hits['7d'] == False 且没有 macro_shock）跑一遍人工 / LLM 标注，
归类 root_cause ∈ {data_stale, regime_misread, news_blindspot, model_bias, unknown}。

输出 news_blindspot 占比，给"事件 RAG 是否值得完整启用"的决策提供数据支撑。

用法（默认 interactive 人工模式）:
    python -m scripts.analyze_news_attribution
    python -m scripts.analyze_news_attribution --auto    # LLM 自动标注（v4-flash）
    python -m scripts.analyze_news_attribution --report  # 只读已有标注算占比

落盘 jsonl 路径: memory/.dreams/attribution.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

from openinvest.core.memory_store import MemoryStore

ROOT_CAUSES = (
    "data_stale",       # 决策时数据已经过期（节假日/周末/抓取失败）
    "regime_misread",   # regime 阈值过严或过松，把当时 regime 判错
    "news_blindspot",   # 实际市场被新闻驱动但 Macro 没看到（事件层缺位 ← RAG 解的就是这个）
    "model_bias",       # LLM 输出本身的偏见（过于保守 / cross-challenge 磨损 round 1 信号）
    "unknown",          # 无法归因
)

DEFAULT_JSONL = Path(MemoryStore().root) / ".dreams" / "verdict_review.jsonl"
ATTRIBUTION_FILE = Path(MemoryStore().root) / ".dreams" / "attribution.jsonl"


def load_reviews(path: Path = DEFAULT_JSONL) -> List[Dict[str, Any]]:
    if not path.exists():
        print(f"⚠️ verdict_review.jsonl 不存在: {path}")
        print("   先跑：python -m jobs.verdict_review")
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def filter_misses(reviews: List[Dict[str, Any]], *, window: str = "7d") -> List[Dict[str, Any]]:
    """只挑：window 内 hits==False、不是 macro_shock 触发的"""
    misses = []
    for r in reviews:
        hits = r.get("hits", {})
        if hits.get(window) is True:
            continue
        if r.get("macro_shock", {}).get("detected"):
            continue
        misses.append(r)
    return misses


def load_existing_attribution() -> Dict[str, str]:
    """已经标过的 (date, asset) → root_cause，避免重复问"""
    if not ATTRIBUTION_FILE.exists():
        return {}
    out = {}
    with open(ATTRIBUTION_FILE, "r", encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
                out[f"{rec['date']}|{rec['asset']}"] = rec["root_cause"]
            except (json.JSONDecodeError, KeyError):
                continue
    return out


def annotate_interactive(misses: List[Dict[str, Any]], existing: Dict[str, str]) -> List[Dict[str, Any]]:
    """人工标注：每条 miss 展示信息，让人选 root_cause"""
    results = []
    print(f"\n准备标注 {len(misses)} 条 miss verdict（已标 {len(existing)} 条会跳过）。")
    print(f"标签：{', '.join(ROOT_CAUSES)}")
    print("快捷输入：1=data_stale 2=regime_misread 3=news_blindspot 4=model_bias 5=unknown")
    print("Ctrl-C 可中断；已标的会落盘。\n")

    shortcuts = {str(i + 1): c for i, c in enumerate(ROOT_CAUSES)}

    for i, m in enumerate(misses, 1):
        key = f"{m['date']}|{m['asset']}"
        if key in existing:
            continue
        print(f"\n--- {i}/{len(misses)} ---")
        print(f"date={m['date']} asset={m['asset']} verdict={m['verdict']} "
              f"conf={m['confidence']:.2f} expected={m.get('expected_direction', '?')}")
        print(f"actual_returns={m.get('actual_returns', {})}")
        print(f"macro_at_decision keys: {list(m.get('macro_at_decision', {}).keys())}")
        try:
            ans = input("root_cause [1-5 or label, blank=skip]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n中断，已标部分保存。")
            break
        if not ans:
            continue
        cause = shortcuts.get(ans) or (ans if ans in ROOT_CAUSES else None)
        if cause is None:
            print(f"无效标签 {ans!r}，跳过")
            continue
        rec = {"date": m["date"], "asset": m["asset"], "root_cause": cause}
        results.append(rec)
        _append_attribution(rec)
    return results


def _append_attribution(rec: Dict[str, Any]) -> None:
    ATTRIBUTION_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(ATTRIBUTION_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def annotate_auto(misses: List[Dict[str, Any]], existing: Dict[str, str]) -> List[Dict[str, Any]]:
    """LLM 自动标注（v4-flash，无 ground truth 只能粗略归因）"""
    # 统一从 utils.llm 读 LLM 配置（默认 DeepSeek，可通过 LLM_* env 换千问/智谱）
    from openinvest.utils.llm import get_llm_config_safe
    api_key, base_url, model, _provider = get_llm_config_safe()
    if not api_key:
        print("❌ --auto 需要 LLM_API_KEY 或 DEEPSEEK_API_KEY，跳过")
        return []
    from openai import OpenAI
    client = OpenAI(api_key=api_key, base_url=base_url)

    results = []
    for i, m in enumerate(misses, 1):
        key = f"{m['date']}|{m['asset']}"
        if key in existing:
            continue
        prompt = (
            f"Classify the most likely root cause of this missed verdict. Output ONLY one of:\n"
            f"{', '.join(ROOT_CAUSES)}\n\n"
            f"date: {m['date']}\nasset: {m['asset']}\nverdict: {m['verdict']}\n"
            f"confidence: {m['confidence']}\nexpected_direction: {m.get('expected_direction')}\n"
            f"actual_returns: {m.get('actual_returns')}\n"
            f"macro_at_decision keys: {list(m.get('macro_at_decision', {}).keys())}\n"
        )
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content":
                        "You classify verdict mismatches in financial forecasting. Output one label only."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
                extra_body={"thinking": {"type": "disabled"}} if "v4" in model else None,
            )
            cause = (resp.choices[0].message.content or "").strip().lower().split()[0]
        except Exception as e:
            print(f"⚠️ {key} LLM 失败: {e}")
            cause = "unknown"
        if cause not in ROOT_CAUSES:
            cause = "unknown"
        rec = {"date": m["date"], "asset": m["asset"], "root_cause": cause, "labeled_by": "auto"}
        results.append(rec)
        _append_attribution(rec)
        print(f"[{i}/{len(misses)}] {key} → {cause}")
    return results


def report() -> None:
    """读 attribution.jsonl 算占比"""
    if not ATTRIBUTION_FILE.exists():
        print("还没有标注数据。先跑 --auto 或人工标注模式。")
        return
    causes: Counter = Counter()
    with open(ATTRIBUTION_FILE, "r", encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
                causes[rec["root_cause"]] += 1
            except (json.JSONDecodeError, KeyError):
                continue
    total = sum(causes.values())
    if total == 0:
        print("attribution.jsonl 没有有效记录")
        return
    print(f"\n=== 根因占比 (n={total}) ===")
    for c in ROOT_CAUSES:
        n = causes.get(c, 0)
        pct = n / total * 100
        print(f"  {c:<18} {n:>4} ({pct:5.1f}%)")

    news_pct = causes.get("news_blindspot", 0) / total
    print("\n=== 事件 RAG 投资回报判断 ===")
    if news_pct >= 0.30:
        print(f"  news_blindspot {news_pct*100:.1f}% ≥ 30% → 上完整 RAG，开 INVEST_EVENT_RAG_ENABLED=true")
    elif news_pct >= 0.10:
        print(f"  news_blindspot {news_pct*100:.1f}% 10–30% → 只开事件 trigger 路径（盘中通知），RAG 暂不开")
    else:
        print(f"  news_blindspot {news_pct*100:.1f}% < 10% → 瓶颈不在新闻，先优化别处")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--auto", action="store_true", help="LLM 自动标注（粗）")
    parser.add_argument("--report", action="store_true", help="只汇报已有标注的占比")
    parser.add_argument("--window", default="7d", help="hit window，默认 7d")
    args = parser.parse_args()

    if args.report:
        report()
        return 0

    reviews = load_reviews()
    if not reviews:
        return 1
    misses = filter_misses(reviews, window=args.window)
    existing = load_existing_attribution()

    if args.auto:
        annotate_auto(misses, existing)
    else:
        annotate_interactive(misses, existing)

    print()
    report()
    return 0


if __name__ == "__main__":
    sys.exit(main())
