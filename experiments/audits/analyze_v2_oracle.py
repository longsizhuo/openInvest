"""
v2 oracle labeling fairness audit.

分析 dspy_trainset_v2.json 的 oracle_verdict 是否对不同 portfolio_state 公平。
纯静态分析，不烧 LLM token，不调 yfinance。

输出:
  - experiments/audits/v2_oracle_analysis.json
  - stdout 上 ≤40 行 human-readable 总结
"""

from __future__ import annotations

import json
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

# ---------- 路径 ----------
ROOT = Path(__file__).resolve().parents[2]
TRAINSET = ROOT / "experiments" / "dspy_trainset_v2.json"
OUT = ROOT / "experiments" / "audits" / "v2_oracle_analysis.json"

# ---------- 工具函数 ----------
PCT_RE = re.compile(r"cash\s+(\d+)%")


def parse_cash_pct(portfolio_state: str) -> int | None:
    """从 'cash 75%, NDQ.AX 25% ...' 抽 cash 百分比。"""
    m = PCT_RE.search(portfolio_state)
    if not m:
        return None
    return int(m.group(1))


def asset_pct_from_cash(cash_pct: int) -> float:
    """cash 75% → asset_pct 0.25。"""
    return (100 - cash_pct) / 100.0


def percentile(xs: list[float], p: float) -> float:
    """简单百分位（线性插值）。p in [0,100]。"""
    if not xs:
        return float("nan")
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def summarize(xs: list[float]) -> dict[str, float]:
    if not xs:
        return {"n": 0}
    return {
        "n": len(xs),
        "mean": round(statistics.fmean(xs), 4),
        "std": round(statistics.pstdev(xs), 4) if len(xs) > 1 else 0.0,
        "min": round(min(xs), 4),
        "p10": round(percentile(xs, 10), 4),
        "p50": round(percentile(xs, 50), 4),
        "p90": round(percentile(xs, 90), 4),
        "max": round(max(xs), 4),
    }


# ---------- 加载 ----------
def load_samples() -> list[dict[str, Any]]:
    with open(TRAINSET, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------- 分析 1: portfolio_state bucket × verdict 分布 ----------
def analysis_1_state_verdict(samples: list[dict]) -> dict:
    """
    按 cash% 分桶（0/25/50/75/100），看每个桶的 verdict 分布。

    预期：
      - 100% cash (asset_pct=0) 不应出现 TRIM / SELL
      - 0% cash (asset_pct=1.0) 不应出现 BUY
    """
    buckets: dict[int, Counter] = defaultdict(Counter)
    unknown = 0

    for s in samples:
        cash = parse_cash_pct(s["portfolio_state"])
        if cash is None:
            unknown += 1
            continue
        buckets[cash][s["verdict"]] += 1

    # 标签
    bucket_labels = {
        100: "bucket_100pct_cash_0pct_asset",
        75: "bucket_75pct_cash_25pct_asset",
        50: "bucket_50pct_cash_50pct_asset",
        25: "bucket_25pct_cash_75pct_asset",
        0: "bucket_0pct_cash_100pct_asset",
    }
    out: dict[str, Any] = {}
    violations: list[str] = []

    for cash_pct in sorted(buckets.keys(), reverse=True):
        label = bucket_labels.get(cash_pct, f"bucket_{cash_pct}pct_cash")
        dist = dict(buckets[cash_pct])
        out[label] = dist

        asset_pct = asset_pct_from_cash(cash_pct)
        # 满 cash (asset=0)：不能 TRIM / SELL
        if asset_pct == 0.0:
            for forbidden in ("TRIM", "SELL"):
                cnt = dist.get(forbidden, 0)
                if cnt > 0:
                    violations.append(
                        f"100% cash bucket has {cnt} {forbidden} samples (should be 0)"
                    )
        # 满仓 (asset=1.0)：不能 BUY
        if asset_pct == 1.0:
            cnt = dist.get("BUY", 0)
            if cnt > 0:
                violations.append(
                    f"0% cash (100% asset) bucket has {cnt} BUY samples (should be 0)"
                )

    out["unknown_portfolio_state_format"] = unknown
    out["violations"] = violations
    return out


# ---------- 分析 2: 同 (date, symbol) 跨 portfolio_state 一致性 ----------
def analysis_2_consistency(samples: list[dict]) -> dict:
    """
    同 (decision_date, symbol) 应有 5 个 state，fwd_30d_return 应相同。

    检查：
      - fwd_30d_return ≥ +5% 时至少一个 state 给 BUY 或 ACCUMULATE
      - fwd_30d_return ≤ -5% 时至少一个 state 给 TRIM 或 SELL
      - fwd_30d_return 在 5 个 state 内是否真的相同
    """
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for s in samples:
        groups[(s["decision_date"], s["symbol"])].append(s)

    extreme_up_all_hold: list[dict] = []   # fwd≥5 但全 HOLD
    extreme_down_all_hold: list[dict] = []  # fwd≤-5 但全 HOLD
    fwd_return_mismatched: list[dict] = []  # 同 (date,sym) fwd 不同
    state_count_mismatch = 0

    for key, group in groups.items():
        fwd_returns = [g["forward_30d_return_pct"] for g in group]
        # 同一时刻同一资产 fwd 应当一致
        if max(fwd_returns) - min(fwd_returns) > 1e-6:
            fwd_return_mismatched.append({
                "decision_date": key[0],
                "symbol": key[1],
                "fwd_30d_returns": fwd_returns,
            })

        if len(group) != 5:
            state_count_mismatch += 1

        verdicts = [g["verdict"] for g in group]
        fwd = fwd_returns[0]
        if fwd >= 5.0 and not any(v in ("BUY", "ACCUMULATE") for v in verdicts):
            extreme_up_all_hold.append({
                "decision_date": key[0],
                "symbol": key[1],
                "fwd_30d_return": round(fwd, 3),
                "all_verdicts": verdicts,
                "cash_pcts": [parse_cash_pct(g["portfolio_state"]) for g in group],
            })
        if fwd <= -5.0 and not any(v in ("TRIM", "SELL") for v in verdicts):
            extreme_down_all_hold.append({
                "decision_date": key[0],
                "symbol": key[1],
                "fwd_30d_return": round(fwd, 3),
                "all_verdicts": verdicts,
                "cash_pcts": [parse_cash_pct(g["portfolio_state"]) for g in group],
            })

    return {
        "total_unique_date_symbol_pairs": len(groups),
        "state_count_not_5": state_count_mismatch,
        "fwd_return_inconsistent_pairs": len(fwd_return_mismatched),
        "fwd_return_inconsistent_examples": fwd_return_mismatched[:5],
        "extreme_up_fwd_ge_5pct_all_HOLD_count": len(extreme_up_all_hold),
        "extreme_up_examples": extreme_up_all_hold[:5],
        "extreme_down_fwd_le_neg5pct_all_HOLD_count": len(extreme_down_all_hold),
        "extreme_down_examples": extreme_down_all_hold[:5],
    }


# ---------- 分析 3: verdict × forward return 分布 ----------
def analysis_3_verdict_return_alignment(samples: list[dict]) -> dict:
    """
    按 verdict 看 forward_30d_return 分布。
    预期: BUY mean > ACCUMULATE > HOLD ≈ 0 > TRIM > SELL
    """
    by_v: dict[str, list[float]] = defaultdict(list)
    for s in samples:
        by_v[s["verdict"]].append(s["forward_30d_return_pct"])

    summary = {v: summarize(xs) for v, xs in by_v.items()}

    # 单调性检查
    order = ["BUY", "ACCUMULATE", "HOLD", "TRIM", "SELL"]
    means = {v: summary[v]["mean"] for v in order if v in summary}
    failures: list[str] = []
    if "BUY" in means and "ACCUMULATE" in means and means["BUY"] < means["ACCUMULATE"]:
        failures.append(f"BUY mean ({means['BUY']}) < ACCUMULATE mean ({means['ACCUMULATE']})")
    if "ACCUMULATE" in means and "HOLD" in means and means["ACCUMULATE"] < means["HOLD"]:
        failures.append(f"ACCUMULATE mean ({means['ACCUMULATE']}) < HOLD mean ({means['HOLD']})")
    if "HOLD" in means and "TRIM" in means and means["HOLD"] < means["TRIM"]:
        failures.append(f"HOLD mean ({means['HOLD']}) < TRIM mean ({means['TRIM']})")
    if "TRIM" in means and "SELL" in means and means["TRIM"] < means["SELL"]:
        failures.append(f"TRIM mean ({means['TRIM']}) < SELL mean ({means['SELL']})")

    alignment_check = "PASS" if not failures else "FAIL: " + "; ".join(failures)

    return {
        **summary,
        "_means_in_order": means,
        "alignment_check": alignment_check,
    }


# ---------- 分析 4: reward × verdict ----------
def analysis_4_reward_per_verdict(samples: list[dict]) -> dict:
    """
    reward = market forward Sharpe-MDD（不 verdict-conditioned）。
    所以 SELL verdict 在大跌行情下 reward 是负的，并不是看对了奖励。
    """
    by_v: dict[str, list[float]] = defaultdict(list)
    for s in samples:
        by_v[s["verdict"]].append(s["reward"])

    out: dict[str, Any] = {}
    for v, xs in by_v.items():
        out[v] = summarize(xs)

    out["note"] = (
        "reward = market-side forward Sharpe-MDD, NOT verdict-conditioned. "
        "A SELL verdict in a falling market still gets negative reward (market Sharpe is negative), "
        "not a positive 'you-were-right' bonus. Same reward is shared across all 5 portfolio_state "
        "samples for one (date, symbol), regardless of their verdicts. "
        "Implication: BUY/ACCUMULATE rewards should correlate positively with forward outcomes, "
        "but TRIM/SELL rewards are inverted relative to verdict correctness."
    )
    return out


# ---------- main ----------
def main() -> None:
    samples = load_samples()
    print(f"[v2 audit] loaded {len(samples)} samples from {TRAINSET}")

    a1 = analysis_1_state_verdict(samples)
    a2 = analysis_2_consistency(samples)
    a3 = analysis_3_verdict_return_alignment(samples)
    a4 = analysis_4_reward_per_verdict(samples)

    # 总评
    issues: list[str] = []
    if a1["violations"]:
        issues.extend(a1["violations"])
    if a2["fwd_return_inconsistent_pairs"] > 0:
        issues.append(
            f"{a2['fwd_return_inconsistent_pairs']} (date,sym) pairs have inconsistent fwd_30d_return across states"
        )
    if a2["state_count_not_5"] > 0:
        issues.append(f"{a2['state_count_not_5']} (date,sym) pairs don't have exactly 5 states")
    if a3["alignment_check"].startswith("FAIL"):
        issues.append(f"verdict-return alignment: {a3['alignment_check']}")
    if a2["extreme_up_fwd_ge_5pct_all_HOLD_count"] > 0:
        issues.append(
            f"{a2['extreme_up_fwd_ge_5pct_all_HOLD_count']} (date,sym) with fwd≥+5% but ALL HOLD (oracle blind spot)"
        )
    if a2["extreme_down_fwd_le_neg5pct_all_HOLD_count"] > 0:
        issues.append(
            f"{a2['extreme_down_fwd_le_neg5pct_all_HOLD_count']} (date,sym) with fwd≤-5% but ALL HOLD (oracle blind spot)"
        )

    overall = "CLEAN" if not issues else "ISSUES: " + " | ".join(issues)

    result = {
        "analysis_1_state_verdict_distribution": a1,
        "analysis_2_consistency": a2,
        "analysis_3_verdict_return_alignment": a3,
        "analysis_4_reward_per_verdict": a4,
        "overall_assessment": overall,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(f"[v2 audit] wrote {OUT}")

    # ---------- human readable ≤40 lines ----------
    print()
    print("=" * 70)
    print("v2 ORACLE LABELING FAIRNESS AUDIT")
    print("=" * 70)
    print()
    print("[1] portfolio_state bucket × verdict distribution")
    print("-" * 70)
    bucket_labels = [
        "bucket_100pct_cash_0pct_asset",
        "bucket_75pct_cash_25pct_asset",
        "bucket_50pct_cash_50pct_asset",
        "bucket_25pct_cash_75pct_asset",
        "bucket_0pct_cash_100pct_asset",
    ]
    header = f"{'bucket':<35} {'BUY':>5} {'ACC':>5} {'HOLD':>5} {'TRIM':>5} {'SELL':>5}"
    print(header)
    for lbl in bucket_labels:
        d = a1.get(lbl, {})
        if not d:
            continue
        print(f"{lbl:<35} {d.get('BUY',0):>5} {d.get('ACCUMULATE',0):>5} "
              f"{d.get('HOLD',0):>5} {d.get('TRIM',0):>5} {d.get('SELL',0):>5}")
    print(f"violations: {len(a1['violations'])}")
    for v in a1["violations"][:3]:
        print(f"  - {v}")

    print()
    print("[2] cross-state consistency for each (date, symbol)")
    print("-" * 70)
    print(f"unique (date,symbol) pairs:          {a2['total_unique_date_symbol_pairs']}")
    print(f"pairs without exactly 5 states:      {a2['state_count_not_5']}")
    print(f"pairs with mismatched fwd_30d_return:{a2['fwd_return_inconsistent_pairs']}")
    print(f"fwd≥+5%  but all HOLD (blind spot):  {a2['extreme_up_fwd_ge_5pct_all_HOLD_count']}")
    print(f"fwd≤-5%  but all HOLD (blind spot):  {a2['extreme_down_fwd_le_neg5pct_all_HOLD_count']}")

    print()
    print("[3] verdict × forward_30d_return (mean / p10 / p50 / p90)")
    print("-" * 70)
    print(f"{'verdict':<12} {'n':>6} {'mean':>8} {'p10':>8} {'p50':>8} {'p90':>8}")
    for v in ["BUY", "ACCUMULATE", "HOLD", "TRIM", "SELL"]:
        s = a3.get(v)
        if not s:
            continue
        print(f"{v:<12} {s['n']:>6} {s['mean']:>8} {s['p10']:>8} {s['p50']:>8} {s['p90']:>8}")
    print(f"alignment: {a3['alignment_check']}")

    print()
    print("[4] verdict × reward (market Sharpe-MDD, NOT verdict-conditioned)")
    print("-" * 70)
    print(f"{'verdict':<12} {'n':>6} {'mean':>8} {'min':>8} {'max':>8}")
    for v in ["BUY", "ACCUMULATE", "HOLD", "TRIM", "SELL"]:
        s = a4.get(v)
        if not s:
            continue
        print(f"{v:<12} {s['n']:>6} {s['mean']:>8} {s['min']:>8} {s['max']:>8}")
    print("note: SELL reward < 0 means market dropped (consistent with selling), not 'reward correct'")

    print()
    print(f"OVERALL: {overall}")


if __name__ == "__main__":
    main()
