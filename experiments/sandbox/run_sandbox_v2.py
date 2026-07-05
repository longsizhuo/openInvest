"""run_sandbox_v2.py — LLM A/B 沙盒 v2：三 arm 全对比（基于 v3 trainset + MIPROv2 optimized）

回答 4 个核心问题：
1. arm_2 accuracy 是否显著 > arm_0？（threshold: +5pp）
2. arm_2 vs arm_1（DSPy optimized vs random demos）有差异吗？验证 MIPROv2 优化的价值
3. **LLM 是否会泛化输出 ACC/TRIM 即使 v3 trainset 没教过**？
   （若是 → demos 没把 LLM 词汇 lock 到 3 verdict）
4. arm_2 alpha 是否优于 zero-shot baseline？

严格隔离 future data：LLM 只看 macro_context + market_context + portfolio_state，
**绝对**看不到 verdict / forward_30d_return_pct / forward_30d_sharpe。

注意 vs v1：
- trainset 换 v3（5565 samples，只 BUY/HOLD/SELL 3 verdict，没 ACC/TRIM）
- arm_2 换 dspy_optimized_v3.json（MIPROv2 light, baseline 0.483 → 0.492 +0.8pp）
- max_tokens 600 → 1500（v1 因 600 太短 21-31 calls truncated）
- arm_1 demos 改 3 verdict × 2 per verdict（v3 没 ACC/TRIM）
- 输出 LLM 的 verdict 词汇分布仍按 5 verdict 统计，看是否会"幻觉"出 ACC/TRIM

跑法：
    cd /home/ubuntu/projects-review/invest
    PYTHONUNBUFFERED=1 uv run --with dspy python experiments/sandbox/run_sandbox_v2.py
    # dry-run 只验证 import / 采样 / load：
    PYTHONUNBUFFERED=1 uv run --with dspy python experiments/sandbox/run_sandbox_v2.py --dry-run

输出：
    experiments/sandbox/sandbox_v2_results.json
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# 让脚本能 import core.*
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

TRAINSET_PATH = PROJECT_ROOT / "experiments" / "dspy_trainset_v3.json"
OPTIMIZED_V3_PATH = PROJECT_ROOT / "experiments" / "dspy_optimized_v3.json"
OUTPUT_PATH = PROJECT_ROOT / "experiments" / "sandbox" / "sandbox_v2_results.json"

EVAL_CUTOFF = "2025-11-01"
EVAL_SET_SIZE = 200
N_DEMOS = 6
N_THREADS = 8
SEED = 42
MAX_TOKENS = 1500  # v1 用 600 → 21-31 calls truncated；提到 1500 给 reasoning 留空间

# v3 trainset 只有这 3 个 verdict
TRAINSET_VERDICTS = ("BUY", "HOLD", "SELL")
# LLM 仍可能输出全部 5 个，按 5 个统计（看是否泛化出 ACC/TRIM）
ALL_VERDICTS = ("BUY", "ACCUMULATE", "HOLD", "TRIM", "SELL")

# alloc mapping (v2.2)
ALLOC_BY_VERDICT = {
    "BUY": 1.0,
    "ACCUMULATE": 0.5,
    "HOLD": 0.0,
    "TRIM": -0.1,
    "SELL": -0.3,
}


# ---------- DSPy / LM 配置 ----------

def configure_dspy():
    """配 DSPy + 任意 OpenAI 兼容 LLM（默认 DeepSeek，可换千问/智谱/Kimi）"""
    import dspy
    from openinvest.utils.llm import get_dspy_lm, get_llm_config
    _api_key, base_url, model, _provider = get_llm_config()
    lm = get_dspy_lm(temperature=0.2, max_tokens=MAX_TOKENS)
    dspy.configure(lm=lm)
    print(f"DSPy 配 {model} @ {base_url}  max_tokens={MAX_TOKENS}")
    return lm, model


# ---------- Signature（与 v1 一致：让 LLM 仍知道 5 verdict 词汇） ----------

def build_signature_and_module():
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

    return CIOSignaturev2, CIOPredictorV2


# ---------- 数据加载 + split ----------

def load_trainset() -> list[dict]:
    raw = json.loads(TRAINSET_PATH.read_text())
    print(f"trainset_v3: {len(raw)} 样本")
    return raw


def split_by_cutoff(raw: list[dict]) -> tuple[list[dict], list[dict]]:
    train_pool = [s for s in raw if s.get("decision_date", "") < EVAL_CUTOFF]
    eval_pool = [s for s in raw if s.get("decision_date", "") >= EVAL_CUTOFF]
    print(f"  train_pool ({EVAL_CUTOFF}- 之前): {len(train_pool)}")
    print(f"  eval_pool  ({EVAL_CUTOFF}+ 之后): {len(eval_pool)}")
    return train_pool, eval_pool


# ---------- Eval set 采样：stratified by symbol + verdict ----------

def sample_eval_set(eval_pool: list[dict], size: int, seed: int) -> list[dict]:
    """
    覆盖度策略（同 v1）：
    1. 先按 symbol 桶每桶 ~size/n_sym 个
    2. 在每桶内尽量保证 verdict 覆盖
    3. 最后 global 检查每个 trainset verdict 至少 20 个
    """
    rng = random.Random(seed)
    sym_buckets: dict[str, list[dict]] = defaultdict(list)
    for s in eval_pool:
        sym_buckets[s.get("symbol", "?")].append(s)

    target_per_sym = max(1, size // len(sym_buckets))
    chosen: list[dict] = []

    for sym, samples in sym_buckets.items():
        by_v: dict[str, list[dict]] = defaultdict(list)
        for s in samples:
            by_v[str(s.get("verdict", "?")).upper()].append(s)
        n_v_present = max(1, len(by_v))
        per_v = max(1, target_per_sym // n_v_present)
        picked: list[dict] = []
        for v, vs in by_v.items():
            rng.shuffle(vs)
            picked.extend(vs[:per_v])
        if len(picked) < target_per_sym:
            remaining = [s for s in samples if s not in picked]
            rng.shuffle(remaining)
            picked.extend(remaining[: target_per_sym - len(picked)])
        chosen.extend(picked[:target_per_sym])

    # 保证每 trainset verdict 至少 20 个
    counts = Counter(str(s.get("verdict", "?")).upper() for s in chosen)
    for v in TRAINSET_VERDICTS:
        avail = [
            s
            for s in eval_pool
            if str(s.get("verdict", "?")).upper() == v and s not in chosen
        ]
        deficit = max(0, 20 - counts.get(v, 0))
        if deficit > 0 and avail:
            rng.shuffle(avail)
            chosen.extend(avail[:deficit])

    # 去重 + trim
    seen = set()
    dedup = []
    for s in chosen:
        key = (
            s.get("decision_date"),
            s.get("symbol"),
            hash(s.get("portfolio_state", "")),
            float(s.get("alloc_pct_of_dry_powder", 0.0)),
        )
        if key not in seen:
            seen.add(key)
            dedup.append(s)
    rng.shuffle(dedup)
    dedup = dedup[:size]

    sym_c = Counter(s.get("symbol", "?") for s in dedup)
    v_c = Counter(str(s.get("verdict", "?")).upper() for s in dedup)
    print(f"eval_set: {len(dedup)} 个")
    print(f"   by symbol: {dict(sym_c)}")
    print(f"   by verdict: {dict(v_c)}")
    return dedup


# ---------- Demo 采样（arm 1：v3 random demos） ----------

def sample_random_demos(train_pool: list[dict], n_demos: int, seed: int):
    """从 v3 train pool 采 n_demos 个 dspy.Example。

    v3 只有 BUY/HOLD/SELL 3 verdict → n_demos=6 时每 verdict 2 个 stratified random
    """
    import dspy
    rng = random.Random(seed + 1)
    by_v: dict[str, list[dict]] = defaultdict(list)
    for s in train_pool:
        by_v[str(s.get("verdict", "?")).upper()].append(s)

    # 每 verdict 至少 1，目标 n_demos // n_verdicts 个/verdict
    per_v = max(1, n_demos // len(TRAINSET_VERDICTS))  # 6 // 3 = 2
    picked: list[dict] = []
    for v in TRAINSET_VERDICTS:
        pool_v = by_v.get(v, [])
        rng.shuffle(pool_v)
        picked.extend(pool_v[:per_v])

    # 不足 n_demos 时随机补
    if len(picked) < n_demos:
        remaining_pool = [s for s in train_pool if s not in picked]
        rng.shuffle(remaining_pool)
        while len(picked) < n_demos and remaining_pool:
            picked.append(remaining_pool.pop())
    picked = picked[:n_demos]

    demos = []
    for s in picked:
        ex = dspy.Example(
            macro_context=s["macro_context"],
            market_context=s["market_context"],
            portfolio_state=s["portfolio_state"],
            verdict=str(s["verdict"]).upper(),
            alloc_pct_of_dry_powder=float(s.get("alloc_pct_of_dry_powder", 0.0)),
            reasoning=_synth_reasoning(s),
        ).with_inputs("macro_context", "market_context", "portfolio_state")
        demos.append(ex)
    print(f"arm_1 v3 random demos: {len(demos)} 个")
    print(f"   verdicts: {Counter(d.verdict for d in demos)}")
    return demos


def _synth_reasoning(s: dict) -> str:
    v = str(s.get("verdict", "")).upper()
    mc = s.get("market_context", "")
    regime_hint = ""
    if "REGIME:" in mc:
        regime_hint = " (" + mc.split("REGIME:")[1].split("\n")[0].strip() + ")"
    return f"verdict {v}{regime_hint}; aligns portfolio_state with market signal."


# ---------- 评估 worker ----------

@dataclass
class CallResult:
    idx: int
    success: bool
    verdict: str = ""
    alloc: float = 0.0
    reasoning: str = ""
    error: str = ""
    latency_ms: float = 0.0


def call_one(program, sample: dict, idx: int) -> CallResult:
    t0 = time.time()
    try:
        pred = program(
            macro_context=sample["macro_context"],
            market_context=sample["market_context"],
            portfolio_state=sample["portfolio_state"],
        )
        verdict = str(getattr(pred, "verdict", "")).strip().upper()
        m = re.match(r"^(BUY|ACCUMULATE|HOLD|TRIM|SELL)", verdict)
        verdict_clean = m.group(1) if m else verdict
        alloc_raw = getattr(pred, "alloc_pct_of_dry_powder", 0.0)
        try:
            alloc = float(alloc_raw)
        except (ValueError, TypeError):
            alloc = 0.0
        reasoning = str(getattr(pred, "reasoning", "") or "")
        return CallResult(
            idx=idx,
            success=True,
            verdict=verdict_clean,
            alloc=alloc,
            reasoning=reasoning,
            latency_ms=(time.time() - t0) * 1000,
        )
    except Exception as e:
        return CallResult(
            idx=idx,
            success=False,
            error=f"{type(e).__name__}: {e}",
            latency_ms=(time.time() - t0) * 1000,
        )


def run_arm(program, eval_set: list[dict], arm_label: str, n_threads: int) -> list[CallResult]:
    results: list[CallResult | None] = [None] * len(eval_set)
    start = time.time()
    n_done = 0
    n_err = 0
    print(f"\n[{arm_label}] 跑 {len(eval_set)} 个 sample (threads={n_threads})...", flush=True)
    with ThreadPoolExecutor(max_workers=n_threads) as pool:
        futures = {
            pool.submit(call_one, program, s, i): i for i, s in enumerate(eval_set)
        }
        for f in as_completed(futures):
            r = f.result()
            results[r.idx] = r
            n_done += 1
            if not r.success:
                n_err += 1
                if n_err <= 3:
                    print(f"   WARN idx={r.idx} {r.error}", flush=True)
            if n_done % 25 == 0:
                elapsed = time.time() - start
                rate = n_done / max(0.01, elapsed)
                eta = (len(eval_set) - n_done) / max(0.01, rate)
                print(
                    f"   [{arm_label}] {n_done}/{len(eval_set)}  "
                    f"errors={n_err}  rate={rate:.1f}/s  ETA={eta:.0f}s",
                    flush=True,
                )
    elapsed = time.time() - start
    print(f"   [{arm_label}] done in {elapsed:.1f}s  errors={n_err}", flush=True)
    return [r for r in results if r is not None]


# ---------- Metrics 聚合 ----------

def compute_metrics(eval_set: list[dict], results: list[CallResult]) -> dict[str, Any]:
    from openinvest.core.backtest_reward import verdict_oracle_accuracy

    n = len(results)
    n_success = sum(1 for r in results if r.success)
    n_err = n - n_success

    # 1. verdict accuracy (oracle): +1 比例
    correct_count = 0
    score_sum = 0
    for r, gold in zip(results, eval_set):
        if not r.success:
            continue
        fwd = float(gold.get("forward_30d_return_pct", 0.0))
        score = verdict_oracle_accuracy(r.verdict, fwd)
        score_sum += score
        if score == 1:
            correct_count += 1
    verdict_accuracy_pos = correct_count / max(1, n_success)
    verdict_accuracy_avg = score_sum / max(1, n_success)

    # 2. verdict 分布（仍按 5 verdict 统计，看 LLM 是否泛化出 ACC/TRIM）
    v_counts = Counter(r.verdict for r in results if r.success)
    total_v = sum(v_counts.values())
    verdict_distribution = {
        v: round(v_counts.get(v, 0) / max(1, total_v), 4) for v in ALL_VERDICTS
    }
    other = total_v - sum(v_counts.get(v, 0) for v in ALL_VERDICTS)
    if other > 0:
        verdict_distribution["OTHER"] = round(other / max(1, total_v), 4)

    # 「LLM 是否泛化输出 v3 trainset 没教过的 ACC/TRIM」专门指标
    acc_trim_count = v_counts.get("ACCUMULATE", 0) + v_counts.get("TRIM", 0)
    pct_outside_trainset_verdicts = acc_trim_count / max(1, total_v)

    # 3. simulated alpha vs BAH
    pred_returns: list[float] = []
    bah_returns: list[float] = []
    for r, gold in zip(results, eval_set):
        fwd = float(gold.get("forward_30d_return_pct", 0.0))
        if r.success and r.verdict in ALLOC_BY_VERDICT:
            w = ALLOC_BY_VERDICT[r.verdict]
            pred_returns.append(w * fwd)
            bah_returns.append(fwd)
    mean_pred = sum(pred_returns) / max(1, len(pred_returns))
    mean_bah = sum(bah_returns) / max(1, len(bah_returns))
    simulated_avg_alpha_vs_bah = mean_pred - mean_bah

    # 4. reasoning quality
    reasonings = [r.reasoning for r in results if r.success and r.reasoning]
    avg_len = sum(len(x) for x in reasonings) / max(1, len(reasonings))
    n_mentions_regime = sum(
        1 for x in reasonings if re.search(r"regime|REGIME|趋势|bull|bear", x, re.I)
    )
    n_mentions_portfolio = sum(
        1 for x in reasonings if re.search(r"portfolio|cash|asset|仓位|持仓|position", x, re.I)
    )
    mentions_regime_pct = n_mentions_regime / max(1, len(reasonings))
    mentions_portfolio_pct = n_mentions_portfolio / max(1, len(reasonings))

    # 5. latency
    total_latency_ms = sum(r.latency_ms for r in results)
    avg_latency_ms = total_latency_ms / max(1, n)

    return {
        "n_eval": n,
        "n_success": n_success,
        "n_errors": n_err,
        "verdict_accuracy_pos_pct": round(verdict_accuracy_pos, 4),
        "verdict_accuracy_avg_oracle_score": round(verdict_accuracy_avg, 4),
        "verdict_distribution": verdict_distribution,
        "pct_outside_trainset_verdicts": round(pct_outside_trainset_verdicts, 4),
        "simulated_mean_return_pct": round(mean_pred, 4),
        "bah_mean_return_pct": round(mean_bah, 4),
        "simulated_avg_alpha_vs_bah_pct": round(simulated_avg_alpha_vs_bah, 4),
        "reasoning_avg_length": round(avg_len, 1),
        "reasoning_mentions_regime_pct": round(mentions_regime_pct, 4),
        "reasoning_mentions_portfolio_pct": round(mentions_portfolio_pct, 4),
        "n_llm_calls": n,
        "avg_latency_ms": round(avg_latency_ms, 1),
    }


# ---------- arm 2: load DSPy v3 optimized ----------

def load_optimized_program():
    if not OPTIMIZED_V3_PATH.exists():
        return None, "v3_optimized_not_found"
    try:
        _, ModuleCls = build_signature_and_module()
        prog = ModuleCls()
        prog.load(str(OPTIMIZED_V3_PATH))
        print(f"arm_2 加载 {OPTIMIZED_V3_PATH.name}")
        return prog, "loaded"
    except Exception as e:
        print(f"arm_2 load 失败: {type(e).__name__}: {e}")
        return None, f"load_failed: {type(e).__name__}"


# ---------- 主流程 ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="只 verify import + 采样 + arm 准备，不发 LLM 请求",
    )
    ap.add_argument("--eval-size", type=int, default=EVAL_SET_SIZE)
    ap.add_argument("--n-threads", type=int, default=N_THREADS)
    ap.add_argument("--seed", type=int, default=SEED)
    args = ap.parse_args()

    # ---- env：python-dotenv 读 .env（不要 bash source） ----
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")

    # ---- data ----
    raw = load_trainset()
    train_pool, eval_pool = split_by_cutoff(raw)
    eval_set = sample_eval_set(eval_pool, args.eval_size, args.seed)

    # ---- dspy / signature ----
    lm, model_name = configure_dspy()
    SignatureCls, ModuleCls = build_signature_and_module()
    arm1_demos = sample_random_demos(train_pool, N_DEMOS, args.seed)

    # ---- arm 0: zero-shot ----
    arm0_program = ModuleCls()

    # ---- arm 1: v3 random demos via LabeledFewShot ----
    from dspy.teleprompt import LabeledFewShot
    arm1_program = LabeledFewShot(k=N_DEMOS).compile(
        student=ModuleCls(),
        trainset=arm1_demos,
    )

    # ---- arm 2: v3 DSPy optimized ----
    arm2_program, arm2_status = load_optimized_program()

    if args.dry_run:
        print("\ndry-run 通过：import / 采样 / arm 0 + arm 1 + arm 2(load) 都 ok")
        print(
            f"   arm_0: program ready (no demos)\n"
            f"   arm_1: program ready ({N_DEMOS} v3 random demos)\n"
            f"   arm_2: {arm2_status}"
        )
        return

    # ---- 真跑 ----
    arm0_results = run_arm(arm0_program, eval_set, "arm_0_zero_shot", args.n_threads)
    arm0_metrics = compute_metrics(eval_set, arm0_results)

    arm1_results = run_arm(arm1_program, eval_set, "arm_1_v3_random_demos", args.n_threads)
    arm1_metrics = compute_metrics(eval_set, arm1_results)

    if arm2_program is not None:
        arm2_results = run_arm(arm2_program, eval_set, "arm_2_v3_dspy_optimized", args.n_threads)
        arm2_metrics = compute_metrics(eval_set, arm2_results)
    else:
        arm2_metrics = {"status": arm2_status}

    # ---- 落盘 ----
    eval_dates = sorted({s.get("decision_date", "") for s in eval_set})
    output = {
        "schema_version": "sandbox_v2",
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model": model_name,
        "max_tokens": MAX_TOKENS,
        "trainset": TRAINSET_PATH.name,
        "optimized_program": OPTIMIZED_V3_PATH.name,
        "eval_set_size": len(eval_set),
        "eval_period": f"{eval_dates[0]} to {eval_dates[-1]}" if eval_dates else "",
        "eval_symbol_distribution": dict(
            Counter(s.get("symbol", "?") for s in eval_set)
        ),
        "eval_verdict_distribution": dict(
            Counter(str(s.get("verdict", "?")).upper() for s in eval_set)
        ),
        "n_demos_random": N_DEMOS,
        "arm_0_zero_shot": arm0_metrics,
        "arm_1_v3_random_demos": arm1_metrics,
        "arm_2_v3_dspy_optimized": arm2_metrics,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2))
    print(f"\n  → {OUTPUT_PATH}")

    print_comparison(output)


def print_comparison(output: dict[str, Any]):
    """≤40 行对比表 + 4 个 verdict + 接入 production 推荐"""
    arm0 = output["arm_0_zero_shot"]
    arm1 = output["arm_1_v3_random_demos"]
    arm2 = output.get("arm_2_v3_dspy_optimized")
    arm2_has = isinstance(arm2, dict) and "verdict_accuracy_pos_pct" in arm2

    print("\n" + "=" * 92)
    print(f"  SANDBOX v2 RESULTS  (n={output['eval_set_size']}, "
          f"period={output['eval_period']}, model={output['model']}, mt={output['max_tokens']})")
    print("=" * 92)

    cols = ["metric", "arm_0_zero", "arm_1_v3_rand"]
    if arm2_has:
        cols.append("arm_2_v3_dspy")
    fmt = "  {:34s}  {:>14s}  {:>14s}" + ("  {:>14s}" if arm2_has else "")
    print(fmt.format(*cols))
    print("  " + "-" * 90)

    def row(label, k, fmt_str="{:.4f}"):
        cells = [label, fmt_str.format(arm0[k]), fmt_str.format(arm1[k])]
        if arm2_has:
            cells.append(fmt_str.format(arm2[k]))
        print(fmt.format(*cells))

    row("verdict_accuracy_pos_pct", "verdict_accuracy_pos_pct")
    row("avg_oracle_score [-1,+1]", "verdict_accuracy_avg_oracle_score")
    row("simulated_mean_return_pct", "simulated_mean_return_pct")
    row("bah_mean_return_pct", "bah_mean_return_pct")
    row("alpha_vs_bah_pct", "simulated_avg_alpha_vs_bah_pct")
    row("reasoning_avg_length", "reasoning_avg_length", "{:.0f}")
    row("mentions_regime_pct", "reasoning_mentions_regime_pct")
    row("mentions_portfolio_pct", "reasoning_mentions_portfolio_pct")
    row("pct_outside_trainset (ACC+TRIM)", "pct_outside_trainset_verdicts")
    row("n_errors", "n_errors", "{:d}")

    # verdict 分布对比
    print("  " + "-" * 90)
    print("  verdict distribution (LLM 输出，含 v3 trainset 没教的 ACC/TRIM):")
    for v in ALL_VERDICTS:
        a0 = arm0["verdict_distribution"].get(v, 0.0)
        a1 = arm1["verdict_distribution"].get(v, 0.0)
        cells = [f"    {v:32s}", f"{a0:.3f}", f"{a1:.3f}"]
        if arm2_has:
            a2 = arm2["verdict_distribution"].get(v, 0.0)
            cells.append(f"{a2:.3f}")
        print(("  {:34s}  {:>14s}  {:>14s}" + ("  {:>14s}" if arm2_has else "")).format(*cells))

    # ---- 4 个 verdict ----
    print("=" * 92)
    delta_acc_20 = (arm2["verdict_accuracy_pos_pct"] - arm0["verdict_accuracy_pos_pct"]) * 100 if arm2_has else 0.0
    delta_acc_21 = (arm2["verdict_accuracy_pos_pct"] - arm1["verdict_accuracy_pos_pct"]) * 100 if arm2_has else 0.0
    delta_alpha_20 = (arm2["simulated_avg_alpha_vs_bah_pct"] - arm0["simulated_avg_alpha_vs_bah_pct"]) if arm2_has else 0.0
    delta_alpha_21 = (arm2["simulated_avg_alpha_vs_bah_pct"] - arm1["simulated_avg_alpha_vs_bah_pct"]) if arm2_has else 0.0
    delta_acc_10 = (arm1["verdict_accuracy_pos_pct"] - arm0["verdict_accuracy_pos_pct"]) * 100

    print(f"  Δ accuracy arm_1 - arm_0 (random demos vs zero-shot): {delta_acc_10:+.2f}pp")
    if arm2_has:
        print(f"  Δ accuracy arm_2 - arm_0 (DSPy optimized vs zero-shot): {delta_acc_20:+.2f}pp  (threshold +5pp)")
        print(f"  Δ accuracy arm_2 - arm_1 (DSPy optimized vs random demos): {delta_acc_21:+.2f}pp")
        print(f"  Δ alpha    arm_2 - arm_0: {delta_alpha_20:+.2f}pp")
        print(f"  Δ alpha    arm_2 - arm_1: {delta_alpha_21:+.2f}pp")

    print()

    # Q1: arm 2 accuracy 显著 > arm 0？
    q1 = "YES" if delta_acc_20 > 5 else ("MARGINAL" if delta_acc_20 > 2 else "NO")
    print(f"  Q1 (arm_2 accuracy 显著 > arm_0 +5pp?): {q1}  (Δ={delta_acc_20:+.2f}pp)")

    # Q2: arm 2 vs arm 1
    if arm2_has:
        if abs(delta_acc_21) < 1 and abs(delta_alpha_21) < 1:
            q2 = "NO_DIFF (MIPROv2 optimization 价值不明显)"
        elif delta_acc_21 > 2 or delta_alpha_21 > 1:
            q2 = "OPTIMIZED_BETTER (MIPROv2 有用)"
        else:
            q2 = f"RANDOM_BETTER_OR_SIMILAR (Δacc={delta_acc_21:+.2f}pp, Δalpha={delta_alpha_21:+.2f}pp)"
    else:
        q2 = "arm_2 缺，无法对比"
    print(f"  Q2 (arm_2 vs arm_1 MIPROv2 优化值得?): {q2}")

    # Q3: LLM 是否泛化输出 ACC/TRIM
    pct0 = arm0["pct_outside_trainset_verdicts"] * 100
    pct1 = arm1["pct_outside_trainset_verdicts"] * 100
    pct2 = (arm2["pct_outside_trainset_verdicts"] * 100) if arm2_has else None
    q3_threshold = 5  # > 5% 算"显著泛化"
    leak = ((pct1 > q3_threshold) or (pct2 is not None and pct2 > q3_threshold))
    q3 = (
        f"YES, demos 没 lock 词汇 → ACC/TRIM 仍泄漏 (arm_0={pct0:.1f}%, arm_1={pct1:.1f}%"
        + (f", arm_2={pct2:.1f}%" if pct2 is not None else "") + ")"
    ) if leak else (
        f"NO, demos 把 LLM 词汇 lock 到 3 verdict (arm_0={pct0:.1f}%, arm_1={pct1:.1f}%"
        + (f", arm_2={pct2:.1f}%" if pct2 is not None else "") + ")"
    )
    print(f"  Q3 (LLM 是否输出 v3 没教的 ACC/TRIM?): {q3}")

    # Q4: arm 2 alpha 优于 zero-shot baseline？
    if arm2_has:
        q4 = "YES" if delta_alpha_20 > 0 else "NO"
        print(f"  Q4 (arm_2 alpha > arm_0 alpha?): {q4}  (Δ={delta_alpha_20:+.2f}pp)")
    else:
        print("  Q4: arm_2 缺，无法对比")

    # ---- 接入 production 推荐 ----
    print()
    if arm2_has:
        if delta_acc_20 > 5 and delta_alpha_20 > 0:
            rec = "推荐：上 arm_2 (DSPy optimized v3) → production，相比 zero-shot 显著 +acc & +alpha"
        elif delta_acc_21 > 2 or delta_alpha_21 > 1:
            rec = "建议：上 arm_2，MIPROv2 比 random demos 略有提升；继续训更长 trainset 或 auto=medium"
        elif (delta_acc_20 > 0 or delta_alpha_20 > 0) and abs(delta_acc_21) < 2:
            rec = "可上：arm_1 或 arm_2 等价（demos 比 zero-shot 略好，MIPROv2 light 没带来明显增量）"
        elif delta_acc_20 < -2:
            rec = "不推荐：arm_2 比 zero-shot 还差，保留 zero-shot（或回查训练集 reward 是否泄漏）"
        else:
            rec = "保留 zero-shot：arm_2 没显著优势，省 demos token & 训练成本"
    else:
        rec = "arm_2 缺，先用 arm_1 random demos 或 zero-shot"
    print(f"  接入 production 推荐：{rec}")
    print("=" * 92)


if __name__ == "__main__":
    main()
