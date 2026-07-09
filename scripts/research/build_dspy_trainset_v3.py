"""v3 DSPy trainset 构造 — path c (alpha-based oracle 重设)

背景：
- v2 / v2.1 / v2.2 oracle 都用 forward return 绝对阈值标 verdict
  （fwd_30d_return >= +8% → BUY，etc）。V3 paper trading 证明这种 oracle 跑不过 BAH：
  - v2 (BUY=50% / ACC=10%):   -8.21% alpha
  - v2.1 (BUY=100% / ACC=30%): -2.23% alpha
  - v2.2 (BUY=100% / ACC=50%): -1.37% alpha（撞墙）
- 根因：5 个 ACC-only symbol（EEM/IWM/TLT/BTC/GC=F）oracle 结构性看不到 BUY 信号，
  永远部分入场。

path c 新设计：
- oracle 标 verdict = 使 forward-30d portfolio terminal value 最大的 verdict
  （per-sample optimum，考虑 starting state）。
- 不需要重拉 yfinance — 直接读 v2 trainset，对每条 sample 用 path_c_oracle
  重算 verdict + alloc，其它字段（macro/market/portfolio_state/forward_*/reward）
  原样保留。

ALLOC_MAP（与 V3 backtest 共用）:
  BUY:         +1.00   # 用 100% cash 加仓
  ACCUMULATE:  +0.50   # 用 50% cash 加仓
  HOLD:         0.00   # 不动
  TRIM:        -0.10   # 减 10% holdings
  SELL:        -0.30   # 减 30% holdings

简化假设：cash 不算收益（实际 risk-free rate ~5%/year ≈ 0.4%/30d，可忽略）。
HOLD 是 tiebreaker（多个 verdict 终值相同时优先 HOLD）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean, median, stdev
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ========== path c oracle ==========

ALLOC_MAP: Dict[str, float] = {
    "BUY":         1.00,
    "ACCUMULATE":  0.50,
    "HOLD":        0.00,
    "TRIM":       -0.10,
    "SELL":       -0.30,
}

# verdict 输出顺序（统计打印用）
VERDICT_ORDER = ("BUY", "ACCUMULATE", "HOLD", "TRIM", "SELL")


def path_c_oracle(
    asset_pct: float, fwd_30d_return_pct: float
) -> Tuple[str, float, float]:
    """返回 (best_verdict, alloc, alpha_vs_state_held)

    per-sample optimum：找使 forward-30d portfolio terminal value 最大的 verdict。

    简化模型：
    - portfolio_value = (new_asset_pct * (1 + fwd_30d_return)) + (1 - new_asset_pct) * 1
      => 收益部分 = new_asset_pct * fwd_30d_return（cash 不算收益）
    - baseline (do nothing) = asset_pct * fwd_30d_return
    - alpha = best - baseline

    tiebreaker：当多个 verdict 终值相同（最常见 fwd=0 或 asset_pct=0 时所有 sell
    verdict 都返 0），优先选 HOLD（最少 transaction cost，最 robust）。
    """
    candidates: Dict[str, float] = {}
    for verdict, alloc in ALLOC_MAP.items():
        if alloc > 0:
            # 加仓：用 alloc 比例的 cash 买
            cash = 1.0 - asset_pct
            new_asset = asset_pct + cash * alloc
        elif alloc < 0:
            # 减仓：减 |alloc| 比例的 holdings
            new_asset = asset_pct * (1 + alloc)
        else:
            new_asset = asset_pct
        candidates[verdict] = new_asset * fwd_30d_return_pct

    baseline_terminal = asset_pct * fwd_30d_return_pct
    max_val = max(candidates.values())
    EPS = 1e-9
    # tiebreaker：HOLD 优先
    if abs(candidates["HOLD"] - max_val) <= EPS:
        best_verdict = "HOLD"
    else:
        best_verdict = max(candidates, key=candidates.get)
    best_alpha = candidates[best_verdict] - baseline_terminal
    return best_verdict, ALLOC_MAP[best_verdict], best_alpha


# ========== sanity test ==========

def run_sanity_tests() -> None:
    """5 个 sanity test — 入参跟任务文档完全一致。"""
    cases = [
        # (asset_pct, fwd_30d_return_pct, expected_verdict, expected_alpha)
        (0.0, 10.0, "BUY", 10.0),
        (1.0, -8.0, "SELL", 2.4),
        (0.5, 5.0, "BUY", 2.5),
        (0.5, -3.0, "SELL", 0.45),
        (0.5, 0.0, "HOLD", 0.0),
    ]
    print("\n[sanity test]")
    all_pass = True
    for ap, fwd, exp_v, exp_alpha in cases:
        v, alloc, alpha = path_c_oracle(ap, fwd)
        ok = (v == exp_v and abs(alpha - exp_alpha) < 0.01)
        status = "OK" if ok else "FAIL"
        print(
            f"  asset_pct={ap:.2f}, fwd={fwd:+.1f}% → verdict={v:<11s} "
            f"alloc={alloc:+.2f}  alpha={alpha:+.3f}  "
            f"(expected {exp_v} alpha={exp_alpha:+.3f}) [{status}]"
        )
        if not ok:
            all_pass = False
    if not all_pass:
        raise SystemExit("sanity test 失败！请检查 path_c_oracle 实现。")
    print("  [sanity test] 全部 5/5 通过\n")


# ========== trainset 处理 ==========

def parse_asset_pct(portfolio_state: str) -> float:
    """从 portfolio_state 字符串反解 asset_pct（与 v2.2 build 一致）。"""
    if "no " in portfolio_state and "position" in portfolio_state:
        return 0.0
    m = re.search(r"concentration\s+(\d+)%", portfolio_state)
    if m:
        return int(m.group(1)) / 100.0
    raise ValueError(f"无法解析 portfolio_state: {portfolio_state}")


def relabel_sample(sample: Dict[str, Any]) -> Tuple[Dict[str, Any], str, str, float]:
    """对一条 sample 用 path c 重算 verdict + alloc。

    返回 (new_sample, new_verdict, old_verdict, oracle_alpha)。
    """
    asset_pct = parse_asset_pct(sample["portfolio_state"])
    fwd_30d_return = sample["forward_30d_return_pct"]

    new_verdict, new_alloc, oracle_alpha = path_c_oracle(asset_pct, fwd_30d_return)
    old_verdict = sample["verdict"]

    new_sample = dict(sample)
    new_sample["verdict"] = new_verdict
    new_sample["alloc_pct_of_dry_powder"] = round(float(new_alloc), 4)
    # 额外字段方便后续分析；不强求训练用
    new_sample["oracle_alpha_pct"] = round(float(oracle_alpha), 4)
    return new_sample, new_verdict, old_verdict, oracle_alpha


def load_v22_stats(stats_path: Path) -> Dict[str, Any]:
    """读 v2.2 stats，用于做 verdict 分布对比。"""
    if not stats_path.exists():
        return {}
    return json.loads(stats_path.read_text())


def build(input_path: Path, output_path: Path) -> None:
    print(f"[build_v3] 读 v2 trainset: {input_path}")
    with input_path.open("r", encoding="utf-8") as fh:
        samples: List[Dict[str, Any]] = json.load(fh)
    print(f"[build_v3] 载入 {len(samples)} 条 sample")

    new_samples: List[Dict[str, Any]] = []
    # verdict 漂移矩阵：old_verdict -> new_verdict -> count
    migration: Dict[str, Counter] = defaultdict(Counter)
    new_verdict_counts: Counter = Counter()
    new_alloc_by_verdict: Dict[str, List[float]] = defaultdict(list)
    oracle_alphas: List[float] = []
    fwd_returns_by_verdict: Dict[str, List[float]] = defaultdict(list)

    for s in samples:
        new_s, new_v, old_v, oracle_alpha = relabel_sample(s)
        new_samples.append(new_s)
        migration[old_v][new_v] += 1
        new_verdict_counts[new_v] += 1
        new_alloc_by_verdict[new_v].append(new_s["alloc_pct_of_dry_powder"])
        oracle_alphas.append(oracle_alpha)
        fwd_returns_by_verdict[new_v].append(new_s["forward_30d_return_pct"])

    # 落 trainset
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(new_samples, ensure_ascii=False, indent=2))
    print(f"[build_v3] 落 trainset: {output_path}")

    # ===== Step 2: 统计 + 对比 v2.2 =====
    v22_stats = load_v22_stats(ROOT / "experiments" / "dspy_trainset_v2.2.stats.json")
    v22_dist = v22_stats.get("verdict_distribution", {})
    v22_total = v22_stats.get("total_samples", sum(v22_dist.values()) or len(samples))

    total = len(new_samples)
    print(f"\n[step 2] 总样本: {total}")
    print(f"[step 2] avg oracle alpha (path c per-sample optimum): {mean(oracle_alphas):+.4f}%")
    print(f"[step 2] median oracle alpha: {median(oracle_alphas):+.4f}%")
    print(f"[step 2] min/max oracle alpha: {min(oracle_alphas):+.4f}% / {max(oracle_alphas):+.4f}%")

    print(f"\n[step 2] verdict 分布对比 (v2.2 → v3):")
    print(f"  {'verdict':<12s}{'v2.2_n':>9s}{'v2.2_%':>9s}  -> {'v3_n':>7s}{'v3_%':>9s}"
          f"   fwd_30d_mean   fwd_30d_med")
    print(f"  {'-'*72}")
    for v in VERDICT_ORDER:
        n22 = v22_dist.get(v, 0)
        p22 = (n22 / v22_total * 100) if v22_total else 0.0
        n3 = new_verdict_counts.get(v, 0)
        p3 = (n3 / total * 100) if total else 0.0
        fwd_list = fwd_returns_by_verdict.get(v, [])
        fwd_mean = mean(fwd_list) if fwd_list else 0.0
        fwd_med = median(fwd_list) if fwd_list else 0.0
        print(f"  {v:<12s}{n22:>9d}{p22:>8.1f}%  -> {n3:>7d}{p3:>8.1f}%   "
              f"{fwd_mean:>+9.2f}%    {fwd_med:>+9.2f}%")

    # verdict 漂移矩阵
    print(f"\n[step 2] verdict migration matrix (v2.2 → v3, row=old, col=new):")
    header = "  old\\new   " + "".join(f"{v:>12s}" for v in VERDICT_ORDER) + "    total"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for old_v in VERDICT_ORDER:
        row = migration.get(old_v, Counter())
        row_total = sum(row.values())
        cells = "".join(f"{row.get(new_v, 0):>12d}" for new_v in VERDICT_ORDER)
        print(f"  {old_v:<10s}{cells}{row_total:>9d}")

    # 落 stats
    stats_path = output_path.with_suffix(".stats.json")
    stats = {
        "total_samples": total,
        "source_trainset": str(input_path.name),
        "mapping_version": "v3_path_c_alpha_based",
        "alloc_mapping": ALLOC_MAP,
        "design": (
            "oracle 标 verdict = 使 forward-30d portfolio terminal value 最大的 verdict "
            "（per-sample optimum，考虑 starting state asset_pct）。HOLD 是 tiebreaker。"
        ),
        "verdict_distribution": dict(new_verdict_counts),
        "verdict_distribution_pct": {
            v: round(new_verdict_counts.get(v, 0) / total * 100, 2)
            for v in VERDICT_ORDER
        },
        "v22_verdict_distribution": v22_dist,
        "migration_matrix_old_to_new": {
            old: dict(row) for old, row in migration.items()
        },
        "oracle_alpha_stats": {
            "mean": round(mean(oracle_alphas), 4),
            "median": round(median(oracle_alphas), 4),
            "stdev": round(stdev(oracle_alphas), 4) if len(oracle_alphas) > 1 else 0.0,
            "min": round(min(oracle_alphas), 4),
            "max": round(max(oracle_alphas), 4),
            "pct_positive": round(
                sum(1 for a in oracle_alphas if a > 1e-9) / len(oracle_alphas) * 100, 2
            ),
            "pct_zero": round(
                sum(1 for a in oracle_alphas if abs(a) <= 1e-9) / len(oracle_alphas) * 100, 2
            ),
        },
        "fwd_30d_return_by_new_verdict": {
            v: {
                "n": len(fwd_returns_by_verdict.get(v, [])),
                "mean": round(mean(fwd_returns_by_verdict[v]), 4) if fwd_returns_by_verdict.get(v) else 0.0,
                "median": round(median(fwd_returns_by_verdict[v]), 4) if fwd_returns_by_verdict.get(v) else 0.0,
            }
            for v in VERDICT_ORDER
        },
        "note": (
            "v3 用 path c (alpha-based oracle)。非 verdict/alloc/oracle_alpha_pct 字段"
            "全部从 v2 trainset 原样复用。"
        ),
    }
    stats_path.write_text(json.dumps(stats, ensure_ascii=False, indent=2))
    print(f"\n[build_v3] 落 stats: {stats_path}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--input", type=Path,
                   default=ROOT / "experiments" / "dspy_trainset_v2.json")
    p.add_argument("--output", type=Path,
                   default=ROOT / "experiments" / "dspy_trainset_v3.json")
    p.add_argument("--skip-sanity", action="store_true",
                   help="跳过 sanity test（不建议）")
    args = p.parse_args()

    if not args.skip_sanity:
        run_sanity_tests()
    build(args.input, args.output)


if __name__ == "__main__":
    main()
