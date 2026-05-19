"""v2.2 DSPy trainset 构造 — 重 build alloc mapping，保持其他字段不变

背景：
- v2 trainset 用了 BUY=50%/ACC=10%/TRIM=-15%/SELL=-50% 的 alloc mapping
- v3 backtest 显示 v2 mapping avg alpha -8.21% vs BAH
- v2.2 mapping (BUY=100%/ACC=50%/TRIM=-10%/SELL=-30%) backtest 上做到 -1.37%

策略：
- verdict 标签规则没改（只是 alloc 数字不同）→ 不需要重拉 yfinance
- 直接读 dspy_trainset_v2.json，用 v2.2 mapping 重算 alloc_pct_of_dry_powder
- 落 dspy_trainset_v2.2.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean, stdev
from typing import Any, Dict, List, Tuple

# 让脚本可以 from utils / core / ... import（与 v2 脚本保持一致）
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def oracle_verdict(
    asset_pct: float, fwd_30d_return: float, fwd_30d_mdd: float,
) -> Tuple[str, float]:
    """oracle labeling 规则（v2.2 mapping）。返回 (verdict, alloc_pct_of_dry_powder)。

    与 v2 的区别 — 仅 alloc 数字调整，verdict 触发条件完全一致：
    - BUY:   0.5  → 1.0
    - ACC:   0.10 → 0.5
    - SELL: -0.5  → -0.3
    - TRIM: -0.15 → -0.10
    - HOLD:  0.0  → 0.0（不变）
    """
    if fwd_30d_return >= 8 and fwd_30d_mdd >= -3 and asset_pct < 0.3:
        return ("BUY", 1.0)            # v2 是 0.5，v2.2 是 1.0
    if fwd_30d_return >= 4 and asset_pct < 0.7:
        return ("ACCUMULATE", 0.5)     # v2 是 0.10，v2.2 是 0.5
    if fwd_30d_return <= -8 and asset_pct >= 0.5:
        return ("SELL", -0.3)          # v2 是 -0.5，v2.2 是 -0.3
    if fwd_30d_return <= -4 and asset_pct >= 0.3:
        return ("TRIM", -0.10)         # v2 是 -0.15，v2.2 是 -0.10
    return ("HOLD", 0.0)


def _parse_asset_pct(portfolio_state: str) -> float:
    """从 portfolio_state 字符串反解 asset_pct。

    格式 1: "cash 100%, no NDQ.AX position, solvency_buffer=..."
    格式 2: "cash 75%, NDQ.AX 25% (concentration 25%, PnL_since_entry ...), solvency_buffer=..."

    用 "concentration XX%" 抽 asset_pct，零仓位时直接返 0.0。
    """
    if "no " in portfolio_state and "position" in portfolio_state:
        return 0.0
    # 抽 "concentration XX%"
    import re
    m = re.search(r"concentration\s+(\d+)%", portfolio_state)
    if m:
        return int(m.group(1)) / 100.0
    raise ValueError(f"无法解析 portfolio_state: {portfolio_state}")


def patch_alloc_in_sample(sample: Dict[str, Any]) -> Tuple[Dict[str, Any], str, float, float]:
    """对一条 sample 重算 alloc。

    返回 (新 sample, verdict, old_alloc, new_alloc)。
    顺便校验：用 v2.2 mapping 算出的 verdict 应该跟原 sample 的 verdict 一致（标签规则没改）。
    """
    asset_pct = _parse_asset_pct(sample["portfolio_state"])
    fwd_30d_return = sample["forward_30d_return_pct"]
    fwd_30d_mdd = sample["forward_30d_mdd_pct"]

    expected_verdict, new_alloc = oracle_verdict(asset_pct, fwd_30d_return, fwd_30d_mdd)
    original_verdict = sample["verdict"]
    if expected_verdict != original_verdict:
        raise RuntimeError(
            f"verdict 不匹配！sample (date={sample['decision_date']} sym={sample['symbol']} "
            f"asset_pct={asset_pct}): 原 verdict={original_verdict}, oracle 重算={expected_verdict}"
        )

    new_sample = dict(sample)
    old_alloc = sample["alloc_pct_of_dry_powder"]
    new_sample["alloc_pct_of_dry_powder"] = round(float(new_alloc), 4)
    return new_sample, original_verdict, float(old_alloc), float(new_alloc)


def build(input_path: Path, output_path: Path) -> None:
    print(f"📥 读 {input_path}")
    with input_path.open("r", encoding="utf-8") as fh:
        samples: List[Dict[str, Any]] = json.load(fh)
    print(f"  载入 {len(samples)} 条 sample")

    new_samples: List[Dict[str, Any]] = []
    # 跟踪新旧 alloc 分布
    old_allocs_by_verdict: Dict[str, List[float]] = defaultdict(list)
    new_allocs_by_verdict: Dict[str, List[float]] = defaultdict(list)
    mismatches = 0

    for s in samples:
        try:
            new_s, verdict, old_alloc, new_alloc = patch_alloc_in_sample(s)
        except RuntimeError as e:
            mismatches += 1
            print(f"  ⚠ verdict 不匹配: {e}")
            continue
        new_samples.append(new_s)
        old_allocs_by_verdict[verdict].append(old_alloc)
        new_allocs_by_verdict[verdict].append(new_alloc)

    if mismatches > 0:
        raise SystemExit(f"❌ 有 {mismatches} 条 verdict 校验失败，请检查 oracle_verdict 逻辑")

    print(f"  ✓ 全部 {len(new_samples)} 条 sample 通过 verdict 校验")

    # 统计
    verdict_dist = Counter(s["verdict"] for s in new_samples)
    symbol_dist = Counter(s["symbol"] for s in new_samples)
    rewards = [s["reward"] for s in new_samples]

    print(f"\n📊 总样本数: {len(new_samples)}")
    print("\nverdict 分布 + alloc 对比 (v2 → v2.2):")
    for v in ("BUY", "ACCUMULATE", "HOLD", "TRIM", "SELL"):
        n = verdict_dist.get(v, 0)
        if n == 0:
            continue
        old_mean = mean(old_allocs_by_verdict[v]) if old_allocs_by_verdict[v] else 0.0
        new_mean = mean(new_allocs_by_verdict[v]) if new_allocs_by_verdict[v] else 0.0
        print(
            f"  {v:12s} {n:6d} ({n/len(new_samples)*100:5.1f}%)  "
            f"alloc {old_mean:+.4f} → {new_mean:+.4f}"
        )

    print("\nsymbol 分布:")
    for s, n in sorted(symbol_dist.items(), key=lambda x: -x[1]):
        print(f"  {s:12s} {n:6d} ({n/len(new_samples)*100:.1f}%)")

    print(
        f"\nreward: mean={mean(rewards):.4f} "
        f"std={stdev(rewards) if len(rewards) > 1 else 0:.4f} "
        f"min={min(rewards):.4f} max={max(rewards):.4f}"
    )

    # 落 trainset
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(new_samples, ensure_ascii=False, indent=2))
    print(f"\n💾 落 {output_path}")

    # 落 stats（含新旧 alloc 对比）
    stats_path = output_path.with_suffix(".stats.json")
    alloc_comparison = {}
    for v in verdict_dist.keys():
        old_list = old_allocs_by_verdict[v]
        new_list = new_allocs_by_verdict[v]
        alloc_comparison[v] = {
            "count": len(new_list),
            "old_alloc_mean": round(mean(old_list), 4) if old_list else 0.0,
            "new_alloc_mean": round(mean(new_list), 4) if new_list else 0.0,
            "old_alloc_unique": sorted(set(round(x, 4) for x in old_list)),
            "new_alloc_unique": sorted(set(round(x, 4) for x in new_list)),
        }

    stats = {
        "total_samples": len(new_samples),
        "source_trainset": str(input_path.name),
        "mapping_version": "v2.2",
        "mapping_change": {
            "BUY":   {"v2": 0.5,  "v2.2": 1.0},
            "ACCUMULATE": {"v2": 0.10, "v2.2": 0.5},
            "HOLD":  {"v2": 0.0,  "v2.2": 0.0},
            "TRIM":  {"v2": -0.15, "v2.2": -0.10},
            "SELL":  {"v2": -0.5, "v2.2": -0.3},
        },
        "verdict_distribution": dict(verdict_dist),
        "symbol_distribution": dict(symbol_dist),
        "alloc_comparison_by_verdict": alloc_comparison,
        "reward_stats": {
            "mean": round(mean(rewards), 4),
            "std": round(stdev(rewards), 4) if len(rewards) > 1 else 0.0,
            "min": round(min(rewards), 4),
            "max": round(max(rewards), 4),
        },
        "note": (
            "verdict 触发规则不变（仅 alloc 数字调整），所有其他字段"
            "（macro_context / market_context / portfolio_state / forward_*）从 v2 原样复用"
        ),
    }
    stats_path.write_text(json.dumps(stats, ensure_ascii=False, indent=2))
    print(f"💾 落 {stats_path}")

    # 验收检查
    warnings = []
    if len(new_samples) != 5565:
        warnings.append(f"总样本 {len(new_samples)} ≠ 5565（v2 原数量）")
    expected_new_allocs = {
        "BUY": 1.0,
        "ACCUMULATE": 0.5,
        "HOLD": 0.0,
        "TRIM": -0.10,
        "SELL": -0.30,
    }
    for v, expected in expected_new_allocs.items():
        actual_list = new_allocs_by_verdict[v]
        if not actual_list:
            continue
        # 全部应该等于 expected
        unique = set(round(x, 4) for x in actual_list)
        if unique != {round(expected, 4)}:
            warnings.append(f"{v} alloc 不是 {expected}: 实际 unique={unique}")
    if warnings:
        print("\n⚠️ 验收 warning:")
        for w in warnings:
            print(f"  - {w}")
    else:
        print("\n✅ 验收通过 - 总样本数 + 每个 verdict 的新 alloc 全部符合预期")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input", type=Path,
                   default=ROOT / "experiments" / "dspy_trainset_v2.json",
                   help="v2 trainset 输入路径")
    p.add_argument("--output", type=Path,
                   default=ROOT / "experiments" / "dspy_trainset_v2.2.json",
                   help="v2.2 trainset 输出路径")
    args = p.parse_args()

    build(args.input, args.output)


if __name__ == "__main__":
    main()
