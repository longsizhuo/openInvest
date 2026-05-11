"""holdout_validate.py —— 用 Optuna best params 在 hold-out 集上验证

防止 overfit：训练集 (2024-05-13 ~ 11-15) 上 best 的参数，是否在 hold-out
(2024-11-15 ~ 12-31) 上也跑得好？

不在 hold-out 上调任何参数——只跑一次报告 reward + 跑赢基准情况。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict

sys.path.insert(0, "/home/ubuntu/projects-review/invest")


def run_holdout(
    optuna_workspace: Path,
    holdout_workspace: Path,
    holdout_start: str = "2024-11-18",
    holdout_end: str = "2024-12-31",
    assets: list = None,
):
    """从 Optuna study 拿 best params，在 hold-out 范围跑一次 walk-forward"""
    import optuna
    from scripts.run_walk_forward import run_walk_forward
    from core.backtest_reward import compute_strategy_reward, explain_reward

    assets = assets or ["NDQ.AX", "GC=F"]

    # 1. 拿 Optuna best params
    storage = f"sqlite:///{optuna_workspace / 'optuna_study.db'}"
    study = optuna.load_study(study_name="invest_committee", storage=storage)
    best = study.best_trial
    print(f"🏆 Best trial #{best.number} from {len(study.trials)} trials")
    print(f"   train reward: {best.value:.4f}")
    print(f"   params: {best.params}")

    # 2. monkey-patch + env vars
    import core.regime as regime
    regime.THRESHOLDS["trend_ma_spread_pct"] = best.params["regime_uptrend"]
    regime.THRESHOLDS["crash_atr_pct_min"] = best.params["regime_atr"]
    os.environ["INVEST_MAX_DEBATE_ROUNDS"] = str(best.params["max_rounds"])
    os.environ["INVEST_CIO_CONFIDENCE_CAP"] = str(best.params["cio_confidence_cap"])
    os.environ["INVEST_ALLOC_AGGRESSIVENESS"] = str(best.params["alloc_aggressiveness"])

    # 3. setup hold-out workspace
    import core.memory_store as ms
    ms.MEMORY_ROOT = holdout_workspace / "memory"
    import db.trades_db as t
    t.DB_PATH = str(holdout_workspace / "db" / "trades.db")
    import db.insights_db as i
    i.DEFAULT_DB_PATH = str(holdout_workspace / "db" / "insights.db")
    (holdout_workspace / "memory").mkdir(parents=True, exist_ok=True)
    (holdout_workspace / "db").mkdir(parents=True, exist_ok=True)

    import scripts.backtest_runner as br
    br._seed_workspace_memory(holdout_workspace)
    br._warmup_market_data(assets)

    # 4. 跑 hold-out walk-forward
    print(f"\n📅 Hold-out: {holdout_start} → {holdout_end}")
    result = run_walk_forward(
        start=holdout_start, end=holdout_end,
        step_days=7, assets=assets,
        initial_cash_cny=100_000.0,
        output_path=holdout_workspace / "holdout_result.json",
    )

    metrics = result["metrics"]
    holdout_reward = compute_strategy_reward(metrics)

    print("\n" + "=" * 70)
    print(f"📊 Hold-out 验证结果（用 train best params）")
    print(f"   train reward (2024-05~11): {best.value:.4f}")
    print(f"   holdout reward (2024-11~12): {holdout_reward:.4f}")
    print(f"   {'✅ 泛化 OK' if holdout_reward > best.value * 0.5 else '⚠ 可能 overfit'}（hold-out > 50% train reward 视为 OK）")
    print(f"\n   总收益:     {metrics['total_return_pct']:+.2f}%")
    print(f"   年化:       {metrics['annualized_return_pct']:+.2f}%")
    print(f"   最大回撤:   {metrics['max_drawdown_pct']:.2f}%")
    print(f"   Sharpe:     {metrics['sharpe_ratio']:.2f}")
    print(f"   交易:       BUY={metrics['n_buys']}  SELL={metrics['n_sells']}  HOLD={metrics['n_holds']}  SKIP={metrics['n_skips']}")
    print(f"\n   vs 基准:")
    for name, vs in metrics["vs_benchmarks"].items():
        print(f"   - {name:12}: {vs['alpha_pct']:+.2f}% (赢 {vs['beat_days_pct']:.0f}% 天)")
    print("=" * 70)

    # 落 summary
    summary = {
        "train_reward": best.value,
        "train_params": best.params,
        "holdout_reward": holdout_reward,
        "holdout_metrics": metrics,
        "generalization_ok": holdout_reward > best.value * 0.5,
    }
    (holdout_workspace / "validation_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str),
    )

    return summary


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--optuna-workspace", required=True, type=Path,
                   help="Optuna study 所在 workspace（含 optuna_study.db）")
    p.add_argument("--holdout-workspace", required=True, type=Path,
                   help="hold-out workspace（独立）")
    p.add_argument("--start", default="2024-11-18")
    p.add_argument("--end", default="2024-12-31")
    p.add_argument("--assets", default="NDQ.AX,GC=F")
    args = p.parse_args()

    run_holdout(
        args.optuna_workspace, args.holdout_workspace,
        args.start, args.end,
        assets=[s.strip() for s in args.assets.split(",")],
    )


if __name__ == "__main__":
    main()
