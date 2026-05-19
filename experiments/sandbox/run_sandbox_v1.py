"""run_sandbox_v1.py — LLM A/B 沙盒：zero-shot vs random-demos vs DSPy-optimized

回答两个核心问题：
1. demos 本身有没有用？（arm 0 zero-shot vs arm 1 random-demos）
2. DSPy 优化的 demos 比随机 demos 好吗？（arm 1 vs arm 2，等主训练完）

严格隔离 future data：LLM 只看 macro_context + market_context + portfolio_state，
**绝对**看不到 verdict / forward_30d_return_pct / forward_30d_sharpe。

跑法：
    cd /home/ubuntu/projects-review/invest
    uv run --with dspy python experiments/sandbox/run_sandbox_v1.py            # 真跑
    uv run --with dspy python experiments/sandbox/run_sandbox_v1.py --dry-run  # 只 verify import & 采样

输出：
    experiments/sandbox/sandbox_v1_results.json
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 让脚本能 import core.* （和 rl_optimize_prompts_v2.py 同样套路）
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

TRAINSET_PATH = PROJECT_ROOT / "experiments" / "dspy_trainset_v2.json"
OPTIMIZED_V2_PATH = PROJECT_ROOT / "experiments" / "dspy_optimized_v2.json"
OUTPUT_PATH = PROJECT_ROOT / "experiments" / "sandbox" / "sandbox_v1_results.json"

EVAL_CUTOFF = "2025-11-01"
EVAL_SET_SIZE = 200
N_DEMOS = 6
N_THREADS = 8  # DeepSeek 并发；rl_optimize_prompts_v2 用 4，这里评估只跑一遍，可以略激进
SEED = 42

# alloc mapping (v2.2)
ALLOC_BY_VERDICT = {
    "BUY": 1.0,
    "ACCUMULATE": 0.5,
    "HOLD": 0.0,
    "TRIM": -0.1,
    "SELL": -0.3,
}

VERDICTS = ("BUY", "ACCUMULATE", "HOLD", "TRIM", "SELL")


# ---------- DSPy / LM 配置 ----------

def configure_dspy():
    """配 DSPy + 任意 OpenAI 兼容 LLM（默认 DeepSeek，可换千问/智谱/Kimi），返回 (lm, model)"""
    import dspy
    from utils.llm import get_dspy_lm, get_llm_config
    _api_key, base_url, model, _provider = get_llm_config()
    # token 上限给 reasoning 留空间
    lm = get_dspy_lm(temperature=0.2, max_tokens=600)
    dspy.configure(lm=lm)
    print(f"✓ DSPy 配 {model} @ {base_url}")
    return lm, model


# ---------- Signature (复刻 rl_optimize_prompts_v2.py CIOSignaturev2) ----------

def build_signature_and_module():
    """返回 (Signature class, Module class)"""
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
    print(f"📦 trainset_v2: {len(raw)} 样本")
    return raw


def split_by_cutoff(raw: list[dict]) -> tuple[list[dict], list[dict]]:
    """< EVAL_CUTOFF → train pool；>= EVAL_CUTOFF → eval pool"""
    train_pool = [s for s in raw if s.get("decision_date", "") < EVAL_CUTOFF]
    eval_pool = [s for s in raw if s.get("decision_date", "") >= EVAL_CUTOFF]
    print(f"  train_pool ({EVAL_CUTOFF}- 之前): {len(train_pool)}")
    print(f"  eval_pool  ({EVAL_CUTOFF}+ 之后): {len(eval_pool)}")
    return train_pool, eval_pool


# ---------- Eval set 采样：stratified by symbol + verdict ----------

def sample_eval_set(eval_pool: list[dict], size: int, seed: int) -> list[dict]:
    """
    覆盖度策略：
    1. 先按 symbol 桶（10 个）每桶 ~20 个
    2. 在每桶内尽量保证 verdict 覆盖（每桶按 verdict 子分层）
    3. 最后 global 检查每个 verdict 至少 20 个（如果该 verdict 在 eval_pool 太少则尽力而为）
    """
    rng = random.Random(seed)
    sym_buckets: dict[str, list[dict]] = defaultdict(list)
    for s in eval_pool:
        sym_buckets[s.get("symbol", "?")].append(s)

    target_per_sym = max(1, size // len(sym_buckets))
    chosen: list[dict] = []

    # ---- 一阶段：每 symbol 桶内按 verdict 子分层 ----
    for sym, samples in sym_buckets.items():
        by_v: dict[str, list[dict]] = defaultdict(list)
        for s in samples:
            by_v[str(s.get("verdict", "?")).upper()].append(s)
        # 每 verdict 桶取 max(1, target_per_sym // n_verdicts_present) 个
        n_v_present = max(1, len(by_v))
        per_v = max(1, target_per_sym // n_v_present)
        picked: list[dict] = []
        for v, vs in by_v.items():
            rng.shuffle(vs)
            picked.extend(vs[:per_v])
        # 凑足 target_per_sym（如果 verdict 桶数太少）
        if len(picked) < target_per_sym:
            remaining = [s for s in samples if s not in picked]
            rng.shuffle(remaining)
            picked.extend(remaining[: target_per_sym - len(picked)])
        chosen.extend(picked[:target_per_sym])

    # ---- 二阶段：保证每 verdict 至少 20 个（eval_pool 有的话） ----
    counts = Counter(str(s.get("verdict", "?")).upper() for s in chosen)
    for v in VERDICTS:
        avail = [
            s
            for s in eval_pool
            if str(s.get("verdict", "?")).upper() == v and s not in chosen
        ]
        deficit = max(0, 20 - counts.get(v, 0))
        if deficit > 0 and avail:
            rng.shuffle(avail)
            extra = avail[:deficit]
            chosen.extend(extra)

    # ---- 三阶段：去重 + trim 到 size ----
    # trainset_v2 同一 (decision_date, symbol) 会有多行（不同 portfolio_state
    # cash 占比 → 不同 alloc），所以 dedup key 必须把 portfolio_state 也算进去
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

    # ---- 报告 ----
    sym_c = Counter(s.get("symbol", "?") for s in dedup)
    v_c = Counter(str(s.get("verdict", "?")).upper() for s in dedup)
    print(f"📊 eval_set: {len(dedup)} 个")
    print(f"   by symbol: {dict(sym_c)}")
    print(f"   by verdict: {dict(v_c)}")
    return dedup


# ---------- Demo 采样（arm 1：random demos from train pool） ----------

def sample_random_demos(train_pool: list[dict], n_demos: int, seed: int):
    """从 train pool 采 n_demos 个 dspy.Example，stratified by verdict（每 verdict ≥ 1）"""
    import dspy
    rng = random.Random(seed + 1)
    by_v: dict[str, list[dict]] = defaultdict(list)
    for s in train_pool:
        by_v[str(s.get("verdict", "?")).upper()].append(s)

    # 每 verdict 至少 1
    picked: list[dict] = []
    for v in VERDICTS:
        if by_v[v]:
            picked.append(rng.choice(by_v[v]))
    # 剩余位随机
    remaining_pool = [s for s in train_pool if s not in picked]
    rng.shuffle(remaining_pool)
    while len(picked) < n_demos and remaining_pool:
        picked.append(remaining_pool.pop())
    picked = picked[:n_demos]

    # 转 dspy.Example
    demos = []
    for s in picked:
        ex = dspy.Example(
            macro_context=s["macro_context"],
            market_context=s["market_context"],
            portfolio_state=s["portfolio_state"],
            verdict=str(s["verdict"]).upper(),
            alloc_pct_of_dry_powder=float(s.get("alloc_pct_of_dry_powder", 0.0)),
            # trainset_v2 没 reasoning 字段，给一个合成的；demos 主要是教 LLM
            # input→verdict 的 pattern，reasoning 是 ChainOfThought 自动生成
            reasoning=_synth_reasoning(s),
        ).with_inputs("macro_context", "market_context", "portfolio_state")
        demos.append(ex)
    print(f"🎯 arm_1 random demos: {len(demos)} 个")
    print(f"   verdicts: {Counter(d.verdict for d in demos)}")
    return demos


def _synth_reasoning(s: dict) -> str:
    """trainset_v2 缺 reasoning 字段，按 verdict + 关键 context 合成一句模版理由"""
    v = str(s.get("verdict", "")).upper()
    fwd = s.get("forward_30d_return_pct", 0.0)
    # 注意：这是给 demo 用的合成理由，目的是教 LLM 输出格式。我们不暴露 fwd 给 LLM，
    # 但 demo 的 reasoning 可以参考 fwd 来写得更精准（demo 本身就是 oracle 标注的）
    regime_hint = ""
    mc = s.get("market_context", "")
    if "REGIME:" in mc:
        regime_hint = " (" + mc.split("REGIME:")[1].split("\n")[0].strip() + ")"
    return f"verdict {v}{regime_hint}; aligns portfolio_state with market signal."


# ---------- 评估 worker（单 sample LLM call） ----------

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
    """跑单 sample；严格不传 forward / verdict 字段给 LLM"""
    t0 = time.time()
    try:
        pred = program(
            macro_context=sample["macro_context"],
            market_context=sample["market_context"],
            portfolio_state=sample["portfolio_state"],
        )
        verdict = str(getattr(pred, "verdict", "")).strip().upper()
        # 容错：LLM 可能输出 "BUY." 或 "BUY (because ...)"
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
    """并行跑 program 在 eval_set 上，返回所有结果（保持顺序对齐 eval_set）"""
    results: list[CallResult | None] = [None] * len(eval_set)
    start = time.time()
    n_done = 0
    n_err = 0
    print(f"\n🚀 [{arm_label}] 跑 {len(eval_set)} 个 sample (threads={n_threads})...")
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
                    print(f"   ⚠ idx={r.idx} {r.error}")
            if n_done % 25 == 0:
                elapsed = time.time() - start
                rate = n_done / max(0.01, elapsed)
                eta = (len(eval_set) - n_done) / max(0.01, rate)
                print(
                    f"   [{arm_label}] {n_done}/{len(eval_set)}  "
                    f"errors={n_err}  rate={rate:.1f}/s  ETA={eta:.0f}s"
                )
    elapsed = time.time() - start
    print(f"   [{arm_label}] done in {elapsed:.1f}s  errors={n_err}")
    return [r for r in results if r is not None]  # 保险（理论上不会有 None）


# ---------- Metrics 聚合 ----------

def compute_metrics(eval_set: list[dict], results: list[CallResult]) -> dict[str, Any]:
    """对单 arm 算所有指标"""
    from core.backtest_reward import verdict_oracle_accuracy

    n = len(results)
    n_success = sum(1 for r in results if r.success)
    n_err = n - n_success

    # 1. verdict accuracy (oracle): +1 比例（按 problem statement: 取 +1 比例）
    correct_count = 0
    score_sum = 0  # +1/0/-1 平均
    for r, gold in zip(results, eval_set):
        if not r.success:
            continue
        fwd = float(gold.get("forward_30d_return_pct", 0.0))
        score = verdict_oracle_accuracy(r.verdict, fwd)
        score_sum += score
        if score == 1:
            correct_count += 1
    verdict_accuracy_pos = correct_count / max(1, n_success)
    verdict_accuracy_avg = score_sum / max(1, n_success)  # 范围 [-1, +1]

    # 2. verdict 分布
    v_counts = Counter(r.verdict for r in results if r.success)
    total_v = sum(v_counts.values())
    verdict_distribution = {
        v: round(v_counts.get(v, 0) / max(1, total_v), 4) for v in VERDICTS
    }
    # 兜底：把"非法"verdict（LLM 输出格式异常）单独报
    other = total_v - sum(v_counts.get(v, 0) for v in VERDICTS)
    if other > 0:
        verdict_distribution["OTHER"] = round(other / max(1, total_v), 4)

    # 3. simulated alpha vs BAH
    # 每 sample: predicted_return = alloc_weight × fwd_30d_return_pct
    #          bah_return = 1.0 × fwd_30d_return_pct
    # alpha = mean(predicted) - mean(bah)
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

    # 5. n_llm_calls / latency
    total_latency_ms = sum(r.latency_ms for r in results)
    avg_latency_ms = total_latency_ms / max(1, n)

    return {
        "n_eval": n,
        "n_success": n_success,
        "n_errors": n_err,
        "verdict_accuracy_pos_pct": round(verdict_accuracy_pos, 4),
        "verdict_accuracy_avg_oracle_score": round(verdict_accuracy_avg, 4),
        "verdict_distribution": verdict_distribution,
        "simulated_mean_return_pct": round(mean_pred, 4),
        "bah_mean_return_pct": round(mean_bah, 4),
        "simulated_avg_alpha_vs_bah_pct": round(simulated_avg_alpha_vs_bah, 4),
        "reasoning_avg_length": round(avg_len, 1),
        "reasoning_mentions_regime_pct": round(mentions_regime_pct, 4),
        "reasoning_mentions_portfolio_pct": round(mentions_portfolio_pct, 4),
        "n_llm_calls": n,
        "avg_latency_ms": round(avg_latency_ms, 1),
    }


# ---------- arm 2: 尝试 load DSPy optimized program ----------

def load_optimized_program():
    """返回 (program, status_str)；不存在或 load 失败返回 (None, status)"""
    if not OPTIMIZED_V2_PATH.exists():
        return None, "training_not_ready"
    try:
        import dspy
        _, ModuleCls = build_signature_and_module()
        prog = ModuleCls()
        prog.load(str(OPTIMIZED_V2_PATH))
        print(f"✓ arm_2 加载 {OPTIMIZED_V2_PATH.name}")
        return prog, "loaded"
    except Exception as e:
        print(f"⚠ arm_2 load 失败: {type(e).__name__}: {e}")
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

    # ---- env ----
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")

    # ---- data ----
    raw = load_trainset()
    train_pool, eval_pool = split_by_cutoff(raw)
    eval_set = sample_eval_set(eval_pool, args.eval_size, args.seed)

    # ---- arm 1 demos ----
    # 先 configure dspy（即使 dry-run 也 import 一下，验证 install 通）
    lm, model_name = configure_dspy()
    SignatureCls, ModuleCls = build_signature_and_module()
    arm1_demos = sample_random_demos(train_pool, N_DEMOS, args.seed)

    # ---- arm 0 program: 纯 module（没 demos） ----
    arm0_program = ModuleCls()

    # ---- arm 1 program: 同 module + LabeledFewShot demos ----
    import dspy
    from dspy.teleprompt import LabeledFewShot
    arm1_program = LabeledFewShot(k=N_DEMOS).compile(
        student=ModuleCls(),
        trainset=arm1_demos,
    )

    # ---- arm 2 program: 尝试 load ----
    arm2_program, arm2_status = load_optimized_program()

    if args.dry_run:
        print("\n✅ dry-run 通过：import / 采样 / arm 0 + arm 1 + arm 2(load) 都 ok")
        print(
            f"   arm_0: program ready (no demos)\n"
            f"   arm_1: program ready ({N_DEMOS} demos)\n"
            f"   arm_2: {arm2_status}"
        )
        return

    # ---- 真跑 ----
    arm0_results = run_arm(arm0_program, eval_set, "arm_0_zero_shot", args.n_threads)
    arm0_metrics = compute_metrics(eval_set, arm0_results)

    arm1_results = run_arm(arm1_program, eval_set, "arm_1_random_demos", args.n_threads)
    arm1_metrics = compute_metrics(eval_set, arm1_results)

    if arm2_program is not None:
        arm2_results = run_arm(arm2_program, eval_set, "arm_2_dspy_optimized", args.n_threads)
        arm2_metrics = compute_metrics(eval_set, arm2_results)
    else:
        arm2_metrics = None

    # ---- 落盘 ----
    eval_dates = sorted({s.get("decision_date", "") for s in eval_set})
    output = {
        "schema_version": "sandbox_v1",
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model": model_name,
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
        "arm_1_random_demos": arm1_metrics,
        "arm_2_dspy_optimized": arm2_metrics if arm2_metrics else {"status": arm2_status},
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2))
    print(f"\n💾 → {OUTPUT_PATH}")

    # ---- 人类可读对比 ----
    print_comparison(output)


def print_comparison(output: dict[str, Any]):
    """≤40 行的对比表 + verdict"""
    arm0 = output["arm_0_zero_shot"]
    arm1 = output["arm_1_random_demos"]
    arm2 = output.get("arm_2_dspy_optimized")
    arm2_has = isinstance(arm2, dict) and "verdict_accuracy_pos_pct" in arm2

    print("\n" + "=" * 78)
    print(f"  SANDBOX v1 RESULTS  (n={output['eval_set_size']}, "
          f"period={output['eval_period']}, model={output['model']})")
    print("=" * 78)

    fmt = "  {:32s}  {:>14s}  {:>14s}" + ("  {:>14s}" if arm2_has else "")
    head = ["metric", "arm_0_zero", "arm_1_random"]
    if arm2_has:
        head.append("arm_2_dspy")
    print(fmt.format(*head))
    print("  " + "-" * 76)

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
    row("n_errors", "n_errors", "{:d}")

    # verdict 分布对比
    print("  " + "-" * 76)
    print("  verdict distribution:")
    for v in VERDICTS:
        a0 = arm0["verdict_distribution"].get(v, 0.0)
        a1 = arm1["verdict_distribution"].get(v, 0.0)
        cells = [f"    {v:30s}", f"{a0:.3f}", f"{a1:.3f}"]
        if arm2_has:
            a2 = arm2["verdict_distribution"].get(v, 0.0)
            cells.append(f"{a2:.3f}")
        print(("  {:32s}  {:>14s}  {:>14s}" + ("  {:>14s}" if arm2_has else "")).format(*cells))

    # ---- judgement ----
    print("=" * 78)
    delta_acc = (arm1["verdict_accuracy_pos_pct"] - arm0["verdict_accuracy_pos_pct"]) * 100
    delta_alpha = arm1["simulated_avg_alpha_vs_bah_pct"] - arm0["simulated_avg_alpha_vs_bah_pct"]

    print(f"  Δ accuracy (arm_1 - arm_0): {delta_acc:+.2f}pp  (significant if > 5pp)")
    print(f"  Δ alpha    (arm_1 - arm_0): {delta_alpha:+.2f}pp  (significant if > 3pp)")

    sig_acc = abs(delta_acc) > 5
    sig_alpha = abs(delta_alpha) > 3

    if delta_acc > 5 or delta_alpha > 3:
        verdict = "demos HELP — random demos 显著优于 zero-shot，概念 work，可上 production"
    elif delta_acc < -5 or delta_alpha < -3:
        verdict = "demos HURT — random demos 显著劣于 zero-shot，思路有问题"
    elif sig_acc or sig_alpha:
        verdict = "MIXED — 一个维度显著一个不显著，继续观察"
    else:
        verdict = "NEUTRAL — demos 没明显帮助也没害，DSPy 优化的 demos 才是关键测试"

    print(f"  → {verdict}")

    if arm2_has:
        delta_acc_12 = (arm2["verdict_accuracy_pos_pct"] - arm1["verdict_accuracy_pos_pct"]) * 100
        delta_alpha_12 = arm2["simulated_avg_alpha_vs_bah_pct"] - arm1["simulated_avg_alpha_vs_bah_pct"]
        print(f"  Δ accuracy (arm_2 - arm_1): {delta_acc_12:+.2f}pp")
        print(f"  Δ alpha    (arm_2 - arm_1): {delta_alpha_12:+.2f}pp")
    else:
        status = arm2.get("status") if isinstance(arm2, dict) else "n/a"
        print(f"  arm_2: {status} (DSPy 主训练未完，暂不评估)")
    print("=" * 78)


if __name__ == "__main__":
    main()
