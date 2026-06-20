"""rl_optimize_prompts.py —— DSPy 自动 prompt 优化（plan 阶段 4.3）

目标：突破 Optuna 找到的 reward 天花板（0.42）—— 因为参数调不动 prompt 内容。

DSPy 怎么帮：
- 给它 trainset（66 样本：market_context + verdict + 实际 7d return）
- 给它 metric（方向对 +1 / 错 -1 / 中性 0）
- 它自动选最有效的 few-shot examples（BootstrapFewShotWithRandomSearch）
- 输出 optimized prompt（含被选中的 few-shot demos）
- 把这些 demos 注入回 agents/cio.py 的 system prompt

⚠️ 当前是 minimal viable scaffold：
- module 只 wrap "market context → verdict" 单步预测
- 不重做 4 角色 cross-challenge（那需要 1-2 天 refactor SDKAgent）
- 默认 --smoke-test 模式只跑 3 candidate，~5min ~¥0.5

用法:
    # Smoke test (推荐先跑这个，确认 pipeline 没炸)
    python -m scripts.rl_optimize_prompts \\
        --trainset experiments/dspy_trainset_v1_2024_05_to_11.json \\
        --output experiments/dspy_optimized_v1.json \\
        --smoke-test

    # 正式（耗 ~30min ~¥3）
    python -m scripts.rl_optimize_prompts \\
        --trainset experiments/dspy_trainset_v1_2024_05_to_11.json \\
        --output experiments/dspy_optimized_v1.json \\
        --n-candidates 10 --n-bootstrap 4
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _configure_dspy():
    """配 DSPy 用任意 OpenAI 兼容 LLM（默认 DeepSeek，可换千问/智谱/Kimi）"""
    import dspy
    from utils.llm import get_dspy_lm, get_llm_config
    _api_key, base_url, model, _provider = get_llm_config()
    lm = get_dspy_lm(temperature=0.2)
    dspy.configure(lm=lm)
    print(f"✓ DSPy 配 {model} @ {base_url}")


def _load_trainset(path: Path) -> List[Any]:
    """trainset.json → DSPy Example list"""
    import dspy
    raw = json.loads(path.read_text())
    print(f"📦 trainset: {len(raw)} 样本")

    examples = []
    for s in raw:
        if not s.get("market_context_summary"):
            continue  # 跳过没 context 的样本
        ex = dspy.Example(
            market_context=s["market_context_summary"],
            portfolio_state=f"100% 现金 ¥{100_000}（backtest baseline）",
            verdict=s["verdict"],
            confidence=s.get("confidence", 0.65),
            actual_7d_return=s["actual_7d_return_pct"],
            reward_score=s["reward_score"],
        ).with_inputs("market_context", "portfolio_state")
        examples.append(ex)

    print(f"  有 market_context 的: {len(examples)}")
    return examples


def _make_metric():
    """DSPy metric：根据 verdict 方向 vs 实际 7d return 打分"""
    def metric(gold, pred, trace=None) -> float:
        """gold = trainset Example, pred = module output. return 0~1"""
        predicted = str(getattr(pred, "verdict", "")).upper()
        actual_return = gold.actual_7d_return  # %

        # 方向对 / 中性 / 反向
        if predicted in ("BUY", "ACCUMULATE"):
            if actual_return >= 2.0:
                return 1.0
            elif actual_return <= -2.0:
                return 0.0
            else:
                return 0.5
        elif predicted in ("SELL", "TRIM"):
            if actual_return <= -2.0:
                return 1.0
            elif actual_return >= 2.0:
                return 0.0
            else:
                return 0.5
        elif predicted == "HOLD":
            if abs(actual_return) <= 2.0:
                return 1.0
            elif abs(actual_return) >= 5.0:
                return 0.2
            else:
                return 0.5
        else:
            return 0.0  # 未知 verdict

    return metric


class CIOPredictor:
    """DSPy module wrap CIO 决策：给 market context → 输出 verdict"""

    def __init__(self):
        import dspy

        class VerdictSignature(dspy.Signature):
            """根据市场技术面 + 用户持仓状态，给出投资建议。

            verdict 选项：
              - BUY: 一次建满仓（强 bullish + 低估）
              - ACCUMULATE: 分批建仓 / 加仓（默认；100% 现金时不允许 HOLD）
              - HOLD: 维持现状（仅当已有仓位 20%+ 且无明确方向）
              - TRIM: 部分减仓（超配 + 风险升温）
              - SELL: 全部清仓（极端 risk_off）
            """
            market_context: str = dspy.InputField(desc="技术面摘要：RSI / MA / 价格分位 / 趋势")
            portfolio_state: str = dspy.InputField(desc="持仓状态：现金 / 仓位 %")
            verdict: str = dspy.OutputField(desc="BUY | ACCUMULATE | HOLD | TRIM | SELL")
            reasoning: str = dspy.OutputField(desc="一句话理由")

        self.signature = VerdictSignature
        self.predict = dspy.ChainOfThought(VerdictSignature)

    def __call__(self, market_context, portfolio_state):
        return self.predict(market_context=market_context, portfolio_state=portfolio_state)


def run_dspy_optimization(
    trainset_path: Path,
    output_path: Path,
    n_candidates: int = 10,
    n_bootstrap: int = 4,
    smoke_test: bool = False,
):
    """主入口：跑 BootstrapFewShotWithRandomSearch"""
    import dspy
    from dspy.teleprompt import BootstrapFewShotWithRandomSearch

    _configure_dspy()

    examples = _load_trainset(trainset_path)
    if not examples:
        raise SystemExit("❌ trainset 没有有效样本")

    # train / dev split
    import random
    random.seed(42)
    random.shuffle(examples)
    split = int(len(examples) * 0.7)
    train_set = examples[:split]
    dev_set = examples[split:]
    print(f"  train: {len(train_set)}, dev: {len(dev_set)}")

    if smoke_test:
        n_candidates = 3
        n_bootstrap = 2
        train_set = train_set[:10]
        dev_set = dev_set[:5]
        print(f"🔥 smoke test：候选 {n_candidates} × demos {n_bootstrap}，"
              f"train={len(train_set)} dev={len(dev_set)}")

    metric = _make_metric()
    program = CIOPredictor()

    # 跑 baseline（不优化的 program）算 baseline score
    print(f"\n📊 baseline (unoptimized program):")
    baseline_scores = []
    for ex in dev_set:
        try:
            pred = program(market_context=ex.market_context, portfolio_state=ex.portfolio_state)
            score = metric(ex, pred)
            baseline_scores.append(score)
        except Exception as e:
            print(f"  ⚠ {e}")
    baseline_avg = sum(baseline_scores) / len(baseline_scores) if baseline_scores else 0
    print(f"  avg score: {baseline_avg:.3f} ({len(baseline_scores)} samples)")

    # 优化
    print(f"\n🎯 BootstrapFewShotWithRandomSearch...")
    teleprompter = BootstrapFewShotWithRandomSearch(
        metric=metric,
        max_bootstrapped_demos=n_bootstrap,
        num_candidate_programs=n_candidates,
        num_threads=1,
    )

    optimized = teleprompter.compile(
        program.predict,  # 优化 predict module
        trainset=train_set,
    )

    # 落 optimized program
    print(f"\n💾 落 optimized program → {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    optimized.save(str(output_path))

    # eval optimized 在 dev
    print(f"\n📊 optimized (post-search):")
    opt_scores = []
    for ex in dev_set:
        try:
            pred = optimized(market_context=ex.market_context, portfolio_state=ex.portfolio_state)
            score = metric(ex, pred)
            opt_scores.append(score)
        except Exception as e:
            print(f"  ⚠ {e}")
    opt_avg = sum(opt_scores) / len(opt_scores) if opt_scores else 0
    print(f"  avg score: {opt_avg:.3f} ({len(opt_scores)} samples)")

    print(f"\n📈 Δ baseline → optimized: {baseline_avg:.3f} → {opt_avg:.3f} "
          f"({(opt_avg - baseline_avg)*100:+.1f}pp)")

    # 落 summary
    summary_path = output_path.with_suffix(".summary.json")
    summary_path.write_text(json.dumps({
        "n_train": len(train_set),
        "n_dev": len(dev_set),
        "n_candidates": n_candidates,
        "n_bootstrap": n_bootstrap,
        "baseline_avg_score": baseline_avg,
        "optimized_avg_score": opt_avg,
        "improvement_pp": (opt_avg - baseline_avg) * 100,
        "smoke_test": smoke_test,
    }, ensure_ascii=False, indent=2))
    print(f"💾 summary → {summary_path}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--trainset", required=True, type=Path,
                   help="trainset.json 路径（用 scripts/build_dspy_trainset.py 生成）")
    p.add_argument("--output", required=True, type=Path,
                   help="optimized program 落盘路径")
    p.add_argument("--n-candidates", type=int, default=10,
                   help="DSPy 试多少个 candidate program（默认 10）")
    p.add_argument("--n-bootstrap", type=int, default=4,
                   help="每个 candidate 用多少 bootstrapped demos（默认 4）")
    p.add_argument("--smoke-test", action="store_true",
                   help="只跑 3 candidate × 10 train 验证 pipeline，~5min ~¥0.5")
    args = p.parse_args()

    run_dspy_optimization(
        args.trainset, args.output,
        args.n_candidates, args.n_bootstrap, args.smoke_test,
    )


if __name__ == "__main__":
    main()
