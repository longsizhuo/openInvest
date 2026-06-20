"""rl_optimize_prompts_v2.py —— DSPy v2 自动 prompt 优化（MIPROv2 升级）

v1 vs v2 关键差异：
- Optimizer: BootstrapFewShotWithRandomSearch → **MIPROv2**（贝叶斯优化，同时
  优化 instructions + few-shot demos，理论上限更高）
- Input fields: market_context + portfolio_state（2 个） → **3 个**：
  macro_context (VIX/TNX/DXY/USDCNY) + market_context + portfolio_state
- Output fields: verdict + reasoning（2 个） → **3 个**：
  verdict + alloc_pct_of_dry_powder + reasoning
- Metric: 7d return ±2% 阈值 → **`core.backtest_reward.verdict_oracle_accuracy`**
  （+1/0/-1 → 1.0/0.5/0.0 给 DSPy）
- Trainset: 66 样本 → ≥ 5000 样本（experiments/dspy_trainset_v2.json）
- Split: 70/30 random → stratified by verdict（5 桶各 80/20） + 时序顺序
  （早期 80% train、后期 20% dev，防 look-ahead bias）

⚠️ 注意：
- 不破坏 v1：scripts/rl_optimize_prompts.py 原样保留
- trainset 由另一个 subagent build（experiments/dspy_trainset_v2.json）
- verdict_oracle_accuracy 由另一个 subagent 加在 core/backtest_reward.py

用法:
    # Smoke test（不要在 trainset 还没 build 完前跑）
    python -m scripts.rl_optimize_prompts_v2 \
        --trainset experiments/dspy_trainset_v2.json \
        --output experiments/dspy_optimized_v2.json \
        --smoke-test

    # 正式
    python -m scripts.rl_optimize_prompts_v2 \
        --trainset experiments/dspy_trainset_v2.json \
        --output experiments/dspy_optimized_v2.json \
        --auto light
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ---------- DSPy 配置 ----------

def _configure_dspy():
    """配 DSPy 用任意 OpenAI 兼容 LLM（默认 DeepSeek，可换千问/智谱/Kimi），
    返回 lm 引用以供 MIPROv2 复用为 prompt_model
    """
    import dspy
    from utils.llm import get_dspy_lm, get_llm_config
    _api_key, base_url, model, _provider = get_llm_config()
    lm = get_dspy_lm(temperature=0.2)
    dspy.configure(lm=lm)
    print(f"✓ DSPy 配 {model} @ {base_url}")
    return lm


# ---------- Trainset 加载 ----------

def _load_trainset(path: Path) -> List[Any]:
    """trainset.json → DSPy Example list

    期望字段（由 build_dspy_trainset_v2.py 产出）：
        - decision_date: ISO 日期字符串（用于时序排序，防 look-ahead）
        - macro_context: VIX/TNX/DXY/USDCNY 摘要
        - market_context: REGIME/RSI/MA/分位/动量
        - portfolio_state: cash% + asset% + concentration% + PnL% + solvency_buffer
        - verdict: BUY|ACCUMULATE|HOLD|TRIM|SELL
        - alloc_pct_of_dry_powder: -1.0 ~ +1.0
        - reasoning: 一句话理由（gold reference，optimizer 用来生成 demos）
        - forward_30d_return_pct: 决策后 30 天实际收益（reward 用）
        - asset_pct: 决策时的资产仓位百分比（reward 用）
    """
    import dspy
    raw = json.loads(path.read_text())
    print(f"📦 trainset: {len(raw)} 样本")

    REQUIRED = ("macro_context", "market_context", "portfolio_state", "verdict")
    examples: List[Any] = []
    skipped = 0
    for s in raw:
        # 必填字段缺失就跳过（不让坏样本污染训练）
        if not all(s.get(k) for k in REQUIRED):
            skipped += 1
            continue
        ex = dspy.Example(
            # === input ===
            macro_context=s["macro_context"],
            market_context=s["market_context"],
            portfolio_state=s["portfolio_state"],
            # === output (gold) ===
            verdict=str(s["verdict"]).upper(),
            alloc_pct_of_dry_powder=float(s.get("alloc_pct_of_dry_powder", 0.0)),
            reasoning=s.get("reasoning", ""),
            # === metric 用的辅助字段（不是 input/output）===
            forward_30d_return_pct=float(s.get("forward_30d_return_pct", 0.0)),
            asset_pct=float(s.get("asset_pct", 0.0)),
            decision_date=s.get("decision_date", ""),
        ).with_inputs("macro_context", "market_context", "portfolio_state")
        examples.append(ex)

    print(f"  有效样本: {len(examples)}（跳过 {skipped}）")
    return examples


# ---------- Split：stratified by verdict + 时序顺序 ----------

def _stratified_temporal_split(
    examples: List[Any],
    train_ratio: float = 0.8,
    seed: int = 42,
) -> tuple[List[Any], List[Any]]:
    """按 verdict 分 5 桶 + 每桶按时间排序 + 前 80% train / 后 20% dev

    动机：
    1. **stratified**：5 种 verdict 各自 train/dev 都有，避免 dev 全是 HOLD
    2. **时序顺序**：每桶按 decision_date 升序，早期作 train、后期作 dev，
       防止 look-ahead bias（如果 random，dev 可能落到 train 之前，
       而 trainset 是 backtest 跑出来的，时序上后面的样本"知道"前面的市场）
    """
    rng = random.Random(seed)

    # 按时间排（同日期保持稳定）
    sorted_exs = sorted(examples, key=lambda e: getattr(e, "decision_date", ""))

    # 5 桶分
    by_verdict: Dict[str, List[Any]] = defaultdict(list)
    for ex in sorted_exs:
        by_verdict[ex.verdict].append(ex)

    train_set: List[Any] = []
    dev_set: List[Any] = []
    for v, exs in sorted(by_verdict.items()):
        n = len(exs)
        split = int(n * train_ratio)
        train_set.extend(exs[:split])
        dev_set.extend(exs[split:])
        print(f"  {v:12s}: total={n:5d}  train={split:5d}  dev={n - split:5d}")

    # train/dev 内部 shuffle（同一 batch 里 verdict 不连续，避免训练偏向）
    # ⚠️ shuffle 不会破坏"早期作 train"的时序意图，只是打乱桶内顺序
    rng.shuffle(train_set)
    rng.shuffle(dev_set)
    print(f"  total: train={len(train_set)}  dev={len(dev_set)}")
    return train_set, dev_set


# ---------- Metric：用 v2 reward ----------

def _make_metric():
    """DSPy metric：调 `core.backtest_reward.verdict_oracle_accuracy`

    verdict_oracle_accuracy 返回 +1 / 0 / -1（方向对/中性/反向），
    DSPy metric 期望 0~1（or 0~∞），故映射为 (score + 1) / 2.0 → 1.0 / 0.5 / 0.0
    """
    # 延迟 import：由另一个 subagent 实现，import 在脚本启动时不存在不影响 --help
    from core.backtest_reward import verdict_oracle_accuracy

    def metric(gold, pred, trace=None) -> float:
        predicted = str(getattr(pred, "verdict", "")).upper()
        fwd_return = float(getattr(gold, "forward_30d_return_pct", 0.0))
        asset_pct = float(getattr(gold, "asset_pct", 0.0))

        score = verdict_oracle_accuracy(predicted, fwd_return, asset_pct)
        # +1 / 0 / -1 → 1.0 / 0.5 / 0.0
        return (score + 1) / 2.0

    return metric


# ---------- Module / Signature ----------

def _build_program():
    """build CIOPredictorV2（dspy.Module 子类）"""
    import dspy

    class CIOSignaturev2(dspy.Signature):
        """根据宏观环境 + 市场技术面 + 用户持仓状态，给出投资建议。

        verdict 必须从以下 5 选 1：
          - BUY: 一次建满仓（强 bullish + 低估 + 低仓位）
          - ACCUMULATE: 分批建仓 / 加仓（趋势好 + 有空间）
          - HOLD: 维持现状（仓位与机会匹配，等待更好信号）
          - TRIM: 部分减仓（超配 + 风险升温）
          - SELL: 全部清仓（极端 risk_off + 重仓）

        alloc_pct_of_dry_powder: -1.0 ~ +1.0
          - 正数 = 加仓占子弹百分比（如 0.1 = 10% 子弹）
          - 负数 = 减仓占持仓百分比（如 -0.15 = 减 15% 持仓）
          - 0 = HOLD
        """
        macro_context: str = dspy.InputField(desc="VIX/TNX/DXY/USDCNY 4 个宏观指标")
        market_context: str = dspy.InputField(desc="REGIME/RSI/MA/分位/动量")
        portfolio_state: str = dspy.InputField(
            desc="cash% + asset% + concentration% + PnL% + solvency_buffer level"
        )
        reasoning: str = dspy.OutputField(desc="一句话理由，含 verdict 与 portfolio_state 的关系")
        verdict: str = dspy.OutputField(desc="BUY | ACCUMULATE | HOLD | TRIM | SELL")
        alloc_pct_of_dry_powder: float = dspy.OutputField(desc="-1.0 ~ +1.0")

    class CIOPredictorV2(dspy.Module):
        def __init__(self):
            super().__init__()
            self.predict = dspy.ChainOfThought(CIOSignaturev2)

        def forward(self, macro_context, market_context, portfolio_state):
            return self.predict(
                macro_context=macro_context,
                market_context=market_context,
                portfolio_state=portfolio_state,
            )

    return CIOPredictorV2()


# ---------- 评估 helper ----------

def _eval_program(program, dev_set, metric, label: str, n_workers: int = 8) -> tuple[float, Dict[str, Dict[str, float]]]:
    """跑 program over dev_set，并行 n_workers 调 LLM，返回 (avg_score, per_verdict_breakdown)"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    scores: List[float] = []
    per_v_scores: Dict[str, List[float]] = defaultdict(list)
    errors = 0

    def _one(ex):
        try:
            pred = program(
                macro_context=ex.macro_context,
                market_context=ex.market_context,
                portfolio_state=ex.portfolio_state,
            )
            return (ex.verdict, metric(ex, pred), None)
        except Exception as e:
            return (ex.verdict, None, e)

    print(f"  [{label}] eval {len(dev_set)} samples with {n_workers} threads...", flush=True)
    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futures = [pool.submit(_one, ex) for ex in dev_set]
        for i, fut in enumerate(as_completed(futures), 1):
            verdict, score, exc = fut.result()
            if exc is not None:
                errors += 1
                if errors <= 3:
                    print(f"  ⚠ [{label}] {type(exc).__name__}: {exc}", flush=True)
                continue
            scores.append(score)
            per_v_scores[verdict].append(score)
            if i % 100 == 0:
                print(f"    [{label}] {i}/{len(dev_set)} done", flush=True)

    avg = sum(scores) / len(scores) if scores else 0.0
    breakdown: Dict[str, Dict[str, float]] = {}
    for v, ss in per_v_scores.items():
        breakdown[v] = {
            "n": len(ss),
            "avg_score": sum(ss) / len(ss) if ss else 0.0,
        }

    print(f"  [{label}] avg score: {avg:.3f}  (n={len(scores)}, errors={errors})")
    for v in ("BUY", "ACCUMULATE", "HOLD", "TRIM", "SELL"):
        if v in breakdown:
            b = breakdown[v]
            print(f"    {v:12s}: n={int(b['n']):4d}  acc={b['avg_score']:.3f}")
    return avg, breakdown


# ---------- 主流程 ----------

def run_dspy_optimization_v2(
    trainset_path: Path,
    output_path: Path,
    auto_level: str = "light",
    smoke_test: bool = False,
    num_threads: int = 4,
):
    """主入口：MIPROv2 同时优化 instructions + demos"""
    import dspy
    from dspy.teleprompt import MIPROv2

    lm = _configure_dspy()

    examples = _load_trainset(trainset_path)
    if not examples:
        raise SystemExit("❌ trainset 没有有效样本")

    print(f"\n🪓 stratified + temporal split:")
    train_set, dev_set = _stratified_temporal_split(examples, train_ratio=0.8, seed=42)
    # 限 dev cap，避免 1000+ 顺序 LLM 调用拖时间 (sandbox 已验证 200 statistical power 够用)
    if not smoke_test and len(dev_set) > 300:
        # stratified 取最近 300：按 verdict 桶各取 60
        from collections import defaultdict
        bucket = defaultdict(list)
        for ex in dev_set:
            bucket[ex.verdict].append(ex)
        capped = []
        per_v = 60
        for v in ("BUY", "ACCUMULATE", "HOLD", "TRIM", "SELL"):
            capped.extend(bucket.get(v, [])[-per_v:])  # 取最近的 per_v 个
        random.Random(42).shuffle(capped)
        print(f"  dev capped: {len(dev_set)} → {len(capped)} (stratified, recent samples per verdict)")
        dev_set = capped

    if smoke_test:
        # smoke：每桶最多取 20 + auto=light，纯验证 pipeline
        train_set = train_set[:50]
        dev_set = dev_set[:20]
        auto_level = "light"
        print(f"🔥 smoke test：auto={auto_level}, train={len(train_set)}, dev={len(dev_set)}")

    metric = _make_metric()
    program = _build_program()

    # ---- baseline ----
    print(f"\n📊 baseline (unoptimized program):")
    baseline_avg, baseline_breakdown = _eval_program(program, dev_set, metric, "baseline")

    # ---- MIPROv2 优化 ----
    print(f"\n🎯 MIPROv2 (auto={auto_level}, num_threads={num_threads})...")
    optimizer = MIPROv2(
        metric=metric,
        auto=auto_level,            # light / medium / heavy
        num_threads=num_threads,
        prompt_model=lm,            # 复用同一个 DeepSeek LM 生成 instruction proposals
        max_bootstrapped_demos=4,   # 每个候选 program 最多 4 个 bootstrapped demos
        max_labeled_demos=4,        # 最多 4 个直接标注的 demos
        seed=42,
        verbose=False,
    )

    optimized = optimizer.compile(
        program,
        trainset=train_set,
        minibatch_size=40,                  # 5000 样本规模适配的 minibatch
        requires_permission_to_run=False,   # 非交互式跑
    )

    # ---- 落盘 ----
    print(f"\n💾 落 optimized program → {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    optimized.save(str(output_path))

    # ---- eval optimized ----
    print(f"\n📊 optimized (MIPROv2 compiled):")
    opt_avg, opt_breakdown = _eval_program(optimized, dev_set, metric, "optimized")

    delta_pp = (opt_avg - baseline_avg) * 100
    print(
        f"\n📈 Δ baseline → optimized: {baseline_avg:.3f} → {opt_avg:.3f} "
        f"({delta_pp:+.1f}pp)"
    )

    # ---- summary ----
    # per_verdict_breakdown：合并 baseline + optimized 两套
    merged_breakdown: Dict[str, Dict[str, Any]] = {}
    for v in ("BUY", "ACCUMULATE", "HOLD", "TRIM", "SELL"):
        merged_breakdown[v] = {
            "n_dev": int(baseline_breakdown.get(v, {}).get("n", 0))
                     or int(opt_breakdown.get(v, {}).get("n", 0)),
            "baseline_acc": baseline_breakdown.get(v, {}).get("avg_score", 0.0),
            "optimized_acc": opt_breakdown.get(v, {}).get("avg_score", 0.0),
        }

    summary = {
        "n_train": len(train_set),
        "n_dev": len(dev_set),
        "optimizer": "MIPROv2",
        "auto_level": auto_level,
        "num_threads": num_threads,
        "baseline_avg_score": round(baseline_avg, 6),
        "optimized_avg_score": round(opt_avg, 6),
        "improvement_pp": round(delta_pp, 4),
        "per_verdict_breakdown": merged_breakdown,
        "smoke_test": smoke_test,
        "trainset_path": str(trainset_path),
        "output_path": str(output_path),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    summary_path = output_path.with_suffix(".summary.json")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"💾 summary → {summary_path}")


# ---------- CLI ----------

def main():
    p = argparse.ArgumentParser(
        description="DSPy v2 prompt 优化（MIPROv2 升级版）",
    )
    p.add_argument(
        "--trainset", required=True, type=Path,
        help="trainset.json 路径（由 build_dspy_trainset_v2.py 生成，≥ 5000 样本）",
    )
    p.add_argument(
        "--output", required=True, type=Path,
        help="optimized program 落盘路径（如 experiments/dspy_optimized_v2.json）",
    )
    p.add_argument(
        "--auto", dest="auto_level", choices=["light", "medium", "heavy"],
        default="light",
        help="MIPROv2 auto 模式：light(~20min) / medium(~1h) / heavy(~3h)，默认 light",
    )
    p.add_argument(
        "--num-threads", type=int, default=4,
        help="MIPROv2 并发线程数（默认 4）",
    )
    p.add_argument(
        "--smoke-test", action="store_true",
        help="只跑前 50 train + 20 dev × auto=light，纯验证 pipeline",
    )
    args = p.parse_args()

    run_dspy_optimization_v2(
        trainset_path=args.trainset,
        output_path=args.output,
        auto_level=args.auto_level,
        smoke_test=args.smoke_test,
        num_threads=args.num_threads,
    )


if __name__ == "__main__":
    main()
