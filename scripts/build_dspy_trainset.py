"""build_dspy_trainset.py —— 从 backtest verdict_log + 实际 7d return 构造 DSPy trainset

输入：
- 一个 walk-forward workspace（含 verdict_log.json）

输出：
- trainset.json：每条样本 = (decision_date, sym, market_context, verdict, alloc, actual_7d_return, reward_score)

reward_score 给 DSPy metric 用：
  +1: verdict 方向对（BUY/ACCUMULATE 后涨 ≥ 2%；SELL/TRIM 后跌 ≥ 2%；HOLD 时 ±2% 区间）
  -1: 方向错（BUY/ACCUMULATE 后跌 ≥ 2%；HOLD 时市场涨/跌 ≥ 5%）
   0: 中性（涨跌幅介于）
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional


def _get_price_at(symbol: str, target_date: str) -> Optional[float]:
    """从 DB 拿 target_date 的收盘价（不在则拿之后最近的）"""
    from db.market_store import MarketStore
    import pandas as pd
    store = MarketStore()
    df = store.get_history_df(symbol)
    if df.empty:
        return None
    target_dt = pd.Timestamp(target_date)
    # 拿 >= target_date 的第一个交易日
    after = df[df.index >= target_dt]
    if after.empty:
        return None
    return float(after["Close"].iloc[0])


def _score_verdict(verdict: str, actual_return_pct: float) -> int:
    """根据 verdict 方向 + 实际 7d return 打分"""
    v = verdict.upper()
    r = actual_return_pct  # 百分比，如 +3.5 表示涨 3.5%

    if v in ("BUY", "ACCUMULATE"):
        if r >= 2.0:
            return 1  # 看涨且涨了
        elif r <= -2.0:
            return -1  # 看涨但跌了
        else:
            return 0
    elif v in ("SELL", "TRIM"):
        if r <= -2.0:
            return 1
        elif r >= 2.0:
            return -1
        else:
            return 0
    elif v == "HOLD":
        if abs(r) <= 2.0:
            return 1  # 横盘 HOLD 对
        elif abs(r) >= 5.0:
            return -1  # 应该有动作但没动
        else:
            return 0
    return 0


def build_trainset(workspace: Path, output_path: Path) -> List[Dict[str, Any]]:
    """主入口：从 workspace 的 verdict_log 构造 trainset"""
    verdict_log_path = workspace / "verdict_log.json"
    if not verdict_log_path.exists():
        raise SystemExit(f"❌ {verdict_log_path} 不存在")

    verdict_log = json.loads(verdict_log_path.read_text())
    print(f"📦 读 {len(verdict_log)} 个 verdict 从 {verdict_log_path}")

    samples = []
    for v in verdict_log:
        date = v["date"]
        sym = v["sym"]
        verdict = v["verdict"]
        alloc = v.get("alloc", 0)
        conf = v.get("conf", 0)

        # 算 7d 后实际 return
        date_dt = datetime.strptime(date, "%Y-%m-%d")
        target_dt = (date_dt + timedelta(days=10)).strftime("%Y-%m-%d")  # 留 10 天 buffer 防周末

        price_t = _get_price_at(sym, date)
        price_t7 = _get_price_at(sym, target_dt)

        if price_t is None or price_t7 is None or price_t <= 0:
            print(f"  ⚠ {date} {sym}: 价格数据缺失，跳过")
            continue

        actual_return_pct = (price_t7 / price_t - 1) * 100
        score = _score_verdict(verdict, actual_return_pct)

        # 读 verdict memo 拿 LLM 当时看到的 market context（给 DSPy few-shot 用）
        memo_path = workspace / "memory" / ".backtest" / date / f"{sym.replace('=','_').replace('.','_')}.md"
        market_context_summary = ""
        if memo_path.exists():
            text = memo_path.read_text(encoding="utf-8")
            # 截取 Quant 段（KEY_DATA 部分），作为简要 context
            if "### Quant Analyst" in text:
                quant_section = text.split("### Quant Analyst")[1].split("###")[0]
                market_context_summary = quant_section.strip()[:400]

        samples.append({
            "date": date,
            "sym": sym,
            "verdict": verdict,
            "alloc_cny": alloc,
            "confidence": conf,
            "price_at_decision": round(price_t, 4),
            "price_7d_later": round(price_t7, 4),
            "actual_7d_return_pct": round(actual_return_pct, 2),
            "reward_score": score,  # +1 / 0 / -1
            "market_context_summary": market_context_summary,
        })

    # 统计
    from collections import Counter
    score_dist = Counter(s["reward_score"] for s in samples)
    print(f"\n📊 trainset 统计 ({len(samples)} 样本):")
    print(f"  ✅ 方向对 (+1): {score_dist.get(1, 0)} ({score_dist.get(1, 0)/len(samples)*100:.1f}%)")
    print(f"  ⚪ 中性  ( 0): {score_dist.get(0, 0)} ({score_dist.get(0, 0)/len(samples)*100:.1f}%)")
    print(f"  ❌ 方向错 (-1): {score_dist.get(-1, 0)} ({score_dist.get(-1, 0)/len(samples)*100:.1f}%)")

    verdict_dist = Counter(s["verdict"] for s in samples)
    print(f"\n按 verdict 拆:")
    for v, n in verdict_dist.most_common():
        good = sum(1 for s in samples if s["verdict"] == v and s["reward_score"] == 1)
        print(f"  {v}: {n} 个 (其中 {good} 方向对，{good/n*100:.0f}%)")

    avg_return = sum(s["actual_7d_return_pct"] for s in samples) / len(samples) if samples else 0
    print(f"\n  平均 7d return: {avg_return:+.2f}% (无差别基准)")
    print(f"  策略 hit rate: {(score_dist.get(1, 0) - score_dist.get(-1, 0))/len(samples)*100:+.1f}%")

    # 落盘
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(samples, ensure_ascii=False, indent=2))
    print(f"\n💾 落盘 {output_path}")

    return samples


def main():
    import sys
    sys.path.insert(0, "/home/ubuntu/projects-review/invest")

    p = argparse.ArgumentParser()
    p.add_argument("--workspace", required=True, type=Path,
                   help="包含 verdict_log.json 的 backtest workspace 目录")
    p.add_argument("--output", required=True, type=Path,
                   help="trainset 落盘路径")
    args = p.parse_args()

    build_trainset(args.workspace, args.output)


if __name__ == "__main__":
    main()
