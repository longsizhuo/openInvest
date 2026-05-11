"""rl_train.py — Optuna Bayesian Optimization 搜参数空间

跑 N 个 trial，每个 trial：
1. 用 Optuna 建议一组参数（REGIME 阈值 / cross-challenge 轮数 / cio_confidence_cap 等）
2. 在隔离的 workspace 跑 walk-forward paper trading
3. evaluate_strategy 算 metrics
4. compute_strategy_reward 给单数值 reward
5. Optuna 用所有 trial 的 (params, reward) 学一个高斯过程，下次建议更 promising 的方向

Train/Val/Test 集分割（防 overfit）：
- 训练：2024-01-01 ~ 2024-04-30（Optuna 看这个 reward 调参）
- 验证：2024-05-01 ~ 2024-06-30（每 5 个 trial 在 val 上测一次，看是否过拟合）
- 测试：留给阶段 4.6 最终对比（这里**不碰**）

用法：
  python -m scripts.backtest_runner \\
    --workspace /tmp/rl_train \\
    --rl-train --n-trials 30 --train-start 2024-01-02 --train-end 2024-04-30

注：必须由 backtest_runner 调用（隔离 workspace）。

成本警告：
- 每个 trial 跑 walk-forward ≈ N decision_dates × M assets × 6 LLM calls
- 30 trials × ~100 dates × 4 assets × 6 ≈ 72,000 调用 ≈ ¥250-300
- 建议先 --n-trials 3 短跑验证 pipeline，再 30 trials 正式跑
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict

log = logging.getLogger(__name__)


def _build_objective(
    workspace: Path, train_start: str, train_end: str,
    assets: list, step_days: int = 7,
):
    """生成 Optuna objective function 闭包，使用指定 workspace + 日期范围"""
    import optuna
    from scripts.run_walk_forward import run_walk_forward
    from core.backtest_reward import compute_strategy_reward, explain_reward

    def objective(trial: optuna.Trial) -> float:
        # === 参数空间 ===
        # 1. REGIME 阈值（影响 Quant 的硬约束）
        regime_uptrend = trial.suggest_float("regime_uptrend", 3.0, 6.0)
        regime_atr = trial.suggest_float("regime_atr", 2.5, 5.0)

        # 2. cross-challenge 轮数（影响 LLM 互相纠正深度）
        max_rounds = trial.suggest_int("max_rounds", 1, 3)

        # 3. CIO confidence cap（高了倾向激进，低了倾向保守）
        cio_confidence_cap = trial.suggest_float("cio_confidence_cap", 0.7, 0.95)

        # 4. alloc 激进度（占 dry_powder 多少比例）
        alloc_aggressiveness = trial.suggest_float("alloc_aggressiveness", 0.05, 0.30)

        # === apply 参数到代码（monkey-patch 全局常量）===
        import core.regime as regime
        regime.THRESHOLDS["uptrend_trend_score"] = regime_uptrend
        regime.THRESHOLDS["uptrend_atr_pct"] = regime_atr

        # max_rounds 通过环境或参数传给 run_walk_forward（暂用 env）
        import os
        os.environ["INVEST_MAX_DEBATE_ROUNDS"] = str(max_rounds)
        # cio_confidence_cap / alloc_aggressiveness 也通过 env 透传给 agents
        os.environ["INVEST_CIO_CONFIDENCE_CAP"] = str(cio_confidence_cap)
        os.environ["INVEST_ALLOC_AGGRESSIVENESS"] = str(alloc_aggressiveness)

        # === 跑 walk-forward ===
        trial_workspace = workspace / f"trial_{trial.number:03d}"
        trial_workspace.mkdir(parents=True, exist_ok=True)

        try:
            result = run_walk_forward(
                start=train_start, end=train_end,
                step_days=step_days, assets=assets,
                initial_cash_cny=100_000.0,
                output_path=trial_workspace / "result.json",
            )
        except Exception as e:
            log.error(f"trial {trial.number} failed: {e}")
            # 失败给个 reward 大负数让 Optuna 远离这块
            return -10.0

        metrics = result["metrics"]
        reward = compute_strategy_reward(metrics)

        # 落 reward 分解
        (trial_workspace / "reward.txt").write_text(
            explain_reward(metrics) + f"\n\nfinal reward: {reward}",
            encoding="utf-8",
        )

        # Optuna 报告中间信号（trial.report 让 pruner 能 early-stop）
        trial.set_user_attr("annualized_return_pct", metrics["annualized_return_pct"])
        trial.set_user_attr("max_drawdown_pct", metrics["max_drawdown_pct"])
        trial.set_user_attr("sharpe_ratio", metrics["sharpe_ratio"])

        return reward

    return objective


def run_optuna_training(
    workspace: Path, train_start: str, train_end: str,
    assets: list, n_trials: int = 30, step_days: int = 7,
) -> Dict[str, Any]:
    """主入口：跑 Optuna study + 返回 best params + 所有 trial 数据"""
    import optuna

    log.info(f"🎯 RL Training")
    log.info(f"   workspace: {workspace}")
    log.info(f"   train period: {train_start} → {train_end} (step={step_days})")
    log.info(f"   assets: {assets}")
    log.info(f"   n_trials: {n_trials}")

    objective = _build_objective(workspace, train_start, train_end, assets, step_days)

    # 用 SQLite storage 让 trial 断点续传
    storage_url = f"sqlite:///{workspace}/optuna_study.db"
    study = optuna.create_study(
        study_name="invest_committee",
        storage=storage_url,
        load_if_exists=True,
        direction="maximize",
        sampler=optuna.samplers.TPESampler(seed=42),
    )

    study.optimize(objective, n_trials=n_trials, show_progress_bar=False)

    # 汇总
    best_trial = study.best_trial
    log.info(f"\n🏆 Best trial: #{best_trial.number}")
    log.info(f"   reward: {best_trial.value:.4f}")
    log.info(f"   params: {best_trial.params}")
    log.info(f"   annualized: {best_trial.user_attrs.get('annualized_return_pct', '?')}%")
    log.info(f"   max DD: {best_trial.user_attrs.get('max_drawdown_pct', '?')}%")
    log.info(f"   Sharpe: {best_trial.user_attrs.get('sharpe_ratio', '?')}")

    result = {
        "n_trials_completed": len(study.trials),
        "best_trial_number": best_trial.number,
        "best_reward": best_trial.value,
        "best_params": best_trial.params,
        "best_metrics": dict(best_trial.user_attrs),
        "all_trials": [
            {
                "number": t.number,
                "params": t.params,
                "reward": t.value,
                "metrics": dict(t.user_attrs),
                "state": t.state.name,
            }
            for t in study.trials
        ],
    }

    # 落 summary
    summary_path = workspace / "training_summary.json"
    summary_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    log.info(f"💾 Summary: {summary_path}")

    return result


def main():
    """CLI 入口（由 backtest_runner 透传调用）"""
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--workspace", required=True, type=Path)
    p.add_argument("--train-start", required=True)
    p.add_argument("--train-end", required=True)
    p.add_argument("--assets", required=True, help="逗号分隔")
    p.add_argument("--n-trials", type=int, default=30)
    p.add_argument("--step", type=int, default=7)
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    assets = [s.strip() for s in args.assets.split(",") if s.strip()]

    run_optuna_training(
        args.workspace, args.train_start, args.train_end,
        assets, n_trials=args.n_trials, step_days=args.step,
    )


if __name__ == "__main__":
    main()
