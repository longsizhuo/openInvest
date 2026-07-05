"""R1 / ADR-022 T5:reward 锚必须是 vs 同资产 buy-and-hold,不是余额宝/现金。
旧锚=余额宝 → 牛市跑赢现金 trivial 且可同时输给 buy-hold,优化器被奖励"坐现金避险"。
"""
from openinvest.core.backtest_reward import compute_strategy_reward


def _metrics(buy_hold_alpha, yuebao_alpha):
    return {
        "annualized_return_pct": 10.0, "max_drawdown_pct": 10.0, "sharpe_ratio": 1.0,
        "vs_benchmarks": {
            "buy_hold": {"alpha_pct": buy_hold_alpha},
            "余额宝": {"alpha_pct": yuebao_alpha},
        },
    }


def test_reward_anchors_on_buyhold_not_yuebao():
    base = compute_strategy_reward(_metrics(buy_hold_alpha=0.0, yuebao_alpha=0.0))
    higher_bh = compute_strategy_reward(_metrics(buy_hold_alpha=20.0, yuebao_alpha=0.0))
    same_yb = compute_strategy_reward(_metrics(buy_hold_alpha=0.0, yuebao_alpha=20.0))
    assert higher_bh > base, "buy_hold alpha 升高应提高 reward(锚没接对)"
    assert same_yb == base, "余额宝 alpha 不应再影响 reward(旧锚未拆干净)"


def test_buy_hold_benchmark_key_exists():
    """_build_benchmark_curves 必须产出 buy_hold 键(reward 锚 + R1 assert 依赖它)。"""
    from scripts.run_walk_forward import _build_benchmark_curves
    curves = _build_benchmark_curves("2025-01-02", "2025-01-31", 100_000.0,
                                     assets=["GC=F", "510300.SS", "NDQ.AX"])
    assert "buy_hold" in curves and curves["buy_hold"], "缺 buy_hold 同资产基准曲线"
