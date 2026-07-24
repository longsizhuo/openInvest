"""rl_optimize_prompts_v5 —— MIPROv2 优化 CIO,教材换 path_v4 计算式仓位标签(ADR-007 重开)

与 v2 的全部差异(其余逐字复用 rl_optimize_prompts_v2):
- Trainset: experiments/dspy_trainset_v5.jsonl(build_dspy_trainset_v5 产出),
  **只用 bucket=clean**(>2025-05-31,deepseek-v4-flash 未见过的行情)——优化过程
  本身就是在给候选 prompt 打分,喂污染段会选出"背历史"的 prompt(v3 教训)。
- Metric: 对齐 path_v4 金标(风险调整最优),不再用 verdict_oracle_accuracy
  (那是"方向对就给分"的 path_c 哲学,会把训练拉回 BUY/SELL 二极管):
  完全命中 1.0 / 同方向邻档(BUY↔ACC、TRIM↔SELL)0.5 / 其余 0。
- alloc 字段: 由 target_exposure 换算成 v2 语义(加仓=占子弹比例,减仓=占持仓比例)。

用法:
    uv run --with dspy python -m scripts.research.rl_optimize_prompts_v5 --smoke-test
    uv run --with dspy python -m scripts.research.rl_optimize_prompts_v5
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, List

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.research import rl_optimize_prompts_v2 as v2  # noqa: E402

TRAINSET = ROOT / "experiments" / "dspy_trainset_v5.jsonl"
OUTPUT = ROOT / "experiments" / "dspy_optimized_v5.json"

TRAIN_CAP_PER_VERDICT = 300   # MIPRO 成本闸:分层截 1500 train
_DIRECTION = {"BUY": "up", "ACCUMULATE": "up", "HOLD": "flat", "TRIM": "down", "SELL": "down"}


def _load_v5(path: Path) -> List[Any]:
    """jsonl → DSPy Example;只收 clean 桶;target_exposure → v2 的 alloc 语义"""
    import dspy
    examples: List[Any] = []
    n_head = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        s = json.loads(line)
        if s.get("bucket") != "clean":
            n_head += 1
            continue
        a0 = float(s["asset_pct"])
        delta = float(s["delta_exposure"])
        if delta > 0:
            alloc = delta / max(1e-9, 1.0 - a0)      # 加仓:占子弹比例
        elif delta < 0:
            alloc = delta / max(1e-9, a0)             # 减仓:占持仓比例(负)
        else:
            alloc = 0.0
        examples.append(dspy.Example(
            macro_context=s["macro_context"],
            market_context=s["market_context"],
            portfolio_state=s["portfolio_state"],
            verdict=str(s["verdict"]).upper(),
            alloc_pct_of_dry_powder=round(alloc, 4),
            reasoning="",
            forward_30d_return_pct=float(s.get("forward_30d_return_pct", 0.0)),
            asset_pct=a0,
            decision_date=s.get("decision_date", ""),
        ).with_inputs("macro_context", "market_context", "portfolio_state"))
    print(f"📦 v5 trainset: clean {len(examples)} 条(另有 head {n_head} 条未用)")
    return examples


def _metric_v5(gold, pred, trace=None) -> float:
    """对齐 path_v4 金标:全对 1.0 / 同方向邻档 0.5 / 其余 0"""
    p = str(getattr(pred, "verdict", "")).upper().strip()
    g = str(getattr(gold, "verdict", "")).upper().strip()
    if p == g:
        return 1.0
    if _DIRECTION.get(p) is not None and _DIRECTION.get(p) == _DIRECTION.get(g) \
            and _DIRECTION[g] != "flat":
        return 0.5
    return 0.0


def _cap_train(train_set: List[Any], per_verdict: int, seed: int = 42) -> List[Any]:
    bucket = defaultdict(list)
    for ex in train_set:
        bucket[ex.verdict].append(ex)
    capped: List[Any] = []
    for vd in ("BUY", "ACCUMULATE", "HOLD", "TRIM", "SELL"):
        capped.extend(bucket.get(vd, [])[:per_verdict])
    random.Random(seed).shuffle(capped)
    print(f"  train capped: {len(train_set)} → {len(capped)}({per_verdict}/verdict,MIPRO 成本闸)")
    return capped


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--auto", dest="auto_level", choices=["light", "medium", "heavy"], default="light")
    p.add_argument("--num-threads", type=int, default=8)
    p.add_argument("--smoke-test", action="store_true")
    args = p.parse_args()

    # 猴补 v5 差异件,主流程逐字走 v2(同一套 split/eval/MIPRO/落盘/摘要)
    v2._load_trainset = lambda _path: _load_v5(TRAINSET)
    v2._make_metric = lambda: _metric_v5
    _orig_split = v2._stratified_temporal_split

    def _split_and_cap(examples, train_ratio=0.8, seed=42):
        train_set, dev_set = _orig_split(examples, train_ratio=train_ratio, seed=seed)
        if not args.smoke_test:
            train_set = _cap_train(train_set, TRAIN_CAP_PER_VERDICT, seed)
        return train_set, dev_set
    v2._stratified_temporal_split = _split_and_cap

    v2.run_dspy_optimization_v2(
        trainset_path=TRAINSET,
        output_path=OUTPUT,
        auto_level=args.auto_level,
        smoke_test=args.smoke_test,
        num_threads=args.num_threads,
    )


if __name__ == "__main__":
    main()
