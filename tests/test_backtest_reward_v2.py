"""smoke tests for backtest_reward v2 (forward_window_reward + verdict_oracle_accuracy)

跑法:
    uv run pytest tests/test_backtest_reward_v2.py -v
"""
from __future__ import annotations

import pytest

from core.backtest_reward import (
    compute_strategy_reward,
    forward_window_reward,
    verdict_oracle_accuracy,
)
from core.config import reset_config, set_config_override


@pytest.fixture(autouse=True)
def _reset_config():
    """每个 test 重置 config 隔离状态。"""
    reset_config()
    yield
    reset_config()


# ---------------------------------------------------------------------------
# forward_window_reward
# ---------------------------------------------------------------------------

def test_forward_window_reward_good_decision():
    """好决策: Sharpe 1.5, MDD -3%, return +8% → reward 应该 > 1.0

    手算: 1.5 - 1.0 * 0.03 + 0.05 * 0.08 = 1.5 - 0.03 + 0.004 = 1.474
    """
    r = forward_window_reward(fwd_sharpe=1.5, fwd_mdd_pct=-3.0, fwd_return_pct=8.0)
    assert r > 1.0, f"好决策 reward 应 > 1.0，实际 {r}"
    # 同时验证公式精度（用 isclose 避免浮点比较直接 ==）
    assert abs(r - 1.474) < 1e-9, f"公式精度偏差: {r}"


def test_forward_window_reward_bad_decision():
    """烂决策: Sharpe -1.0, MDD -15%, return -10% → reward 应该 < -0.5

    手算: -1.0 - 1.0 * 0.15 + 0.05 * (-0.10) = -1.0 - 0.15 - 0.005 = -1.155
    """
    r = forward_window_reward(fwd_sharpe=-1.0, fwd_mdd_pct=-15.0, fwd_return_pct=-10.0)
    assert r < -0.5, f"烂决策 reward 应 < -0.5，实际 {r}"
    assert abs(r - (-1.155)) < 1e-9, f"公式精度偏差: {r}"


def test_forward_window_reward_abs_mdd():
    """MDD 传 +5 还是 -5 都应该当成 5% 惩罚（防用户传符号风格不一致）"""
    r_neg = forward_window_reward(fwd_sharpe=1.0, fwd_mdd_pct=-5.0, fwd_return_pct=0.0)
    r_pos = forward_window_reward(fwd_sharpe=1.0, fwd_mdd_pct=5.0, fwd_return_pct=0.0)
    assert abs(r_neg - r_pos) < 1e-12, f"MDD abs 不稳定: neg={r_neg}, pos={r_pos}"


# ---------------------------------------------------------------------------
# verdict_oracle_accuracy
# ---------------------------------------------------------------------------

def test_verdict_oracle_accuracy_basic():
    """三个 spec 里点名的典型 case"""
    # BUY + return +6% → 方向对（≥+5%）
    assert verdict_oracle_accuracy("BUY", 6.0) == 1
    # HOLD + return +10% → 方向错（|r| ≥ 8%，应该上车没上）
    assert verdict_oracle_accuracy("HOLD", 10.0) == -1
    # TRIM + return -8% → 方向对（≤-3%，减仓躲过下跌）
    assert verdict_oracle_accuracy("TRIM", -8.0) == 1


def test_verdict_oracle_accuracy_all_verdicts():
    """覆盖所有 5 个 verdict 的 +1/-1/0 三个分支，守住 oracle 规则不漂移"""
    # BUY: ≥+5 → +1, ≤-3 → -1, else 0
    assert verdict_oracle_accuracy("BUY", 5.0) == 1
    assert verdict_oracle_accuracy("BUY", -3.0) == -1
    assert verdict_oracle_accuracy("BUY", 1.0) == 0

    # ACCUMULATE: ≥+3 → +1, ≤-3 → -1, else 0
    assert verdict_oracle_accuracy("ACCUMULATE", 3.0) == 1
    assert verdict_oracle_accuracy("ACCUMULATE", -3.0) == -1
    assert verdict_oracle_accuracy("ACCUMULATE", 0.0) == 0

    # HOLD: |r|≤3 → +1, |r|≥8 → -1, else 0
    assert verdict_oracle_accuracy("HOLD", 2.0) == 1
    assert verdict_oracle_accuracy("HOLD", -2.0) == 1
    assert verdict_oracle_accuracy("HOLD", 8.0) == -1
    assert verdict_oracle_accuracy("HOLD", -8.0) == -1
    assert verdict_oracle_accuracy("HOLD", 5.0) == 0

    # TRIM: ≤-3 → +1, ≥+5 → -1, else 0
    assert verdict_oracle_accuracy("TRIM", -3.0) == 1
    assert verdict_oracle_accuracy("TRIM", 5.0) == -1
    assert verdict_oracle_accuracy("TRIM", 0.0) == 0

    # SELL: ≤-5 → +1, ≥+3 → -1, else 0
    assert verdict_oracle_accuracy("SELL", -5.0) == 1
    assert verdict_oracle_accuracy("SELL", 3.0) == -1
    assert verdict_oracle_accuracy("SELL", 0.0) == 0


def test_verdict_oracle_accuracy_case_insensitive():
    """大小写 / 空格不敏感"""
    assert verdict_oracle_accuracy("buy", 6.0) == 1
    assert verdict_oracle_accuracy(" Sell ", -6.0) == 1


def test_verdict_oracle_accuracy_unknown_verdict():
    """未知 verdict 安全返回 0（不抛异常）"""
    assert verdict_oracle_accuracy("INVEST", 10.0) == 0
    assert verdict_oracle_accuracy("", 10.0) == 0


# ---------- config override 实时生效 ----------

def test_oracle_config_override_changes_buy_threshold():
    """set_config_override() 改 buy_positive 影响 verdict_oracle_accuracy"""
    # 默认 buy_positive=5.0，return=4.0 → 0
    assert verdict_oracle_accuracy("BUY", 4.0) == 0

    # 把 buy_positive 降到 3.0，4.0 现在是 +1
    set_config_override({"oracle_accuracy": {"buy_positive": 3.0}})
    assert verdict_oracle_accuracy("BUY", 4.0) == 1


def test_oracle_config_override_changes_hold_neutral():
    """set_config_override() 改 hold_neutral 影响 HOLD 判定"""
    # 默认 hold_neutral=3.0，|5.0| = 5 → 0（中间地带）
    assert verdict_oracle_accuracy("HOLD", 5.0) == 0

    # 把 hold_neutral 提到 6.0，|5.0| ≤ 6 → +1
    set_config_override({"oracle_accuracy": {"hold_neutral": 6.0}})
    assert verdict_oracle_accuracy("HOLD", 5.0) == 1


def test_reward_config_override_changes_weights():
    """set_config_override() 改 reward 权重影响 compute_strategy_reward"""
    metrics = {
        "annualized_return_pct": 10.0,
        "max_drawdown_pct": 10.0,
        "sharpe_ratio": 1.5,
        "vs_benchmarks": {"余额宝": {"alpha_pct": 8.0}},
    }
    # 默认权重算一次
    r_default = compute_strategy_reward(metrics)

    # 改成全 0 权重（除了 annualized=1）→ reward = annualized = 0.1
    set_config_override({"reward": {
        "weight_annualized_return": 1.0,
        "weight_max_drawdown": 0.0,
        "weight_alpha_vs_yuebao": 0.0,
        "weight_sharpe_bonus": 0.0,
    }})
    r_override = compute_strategy_reward(metrics)

    assert r_default != r_override, "override 权重应改变 reward"
    assert abs(r_override - 0.1) < 1e-4, f"全 0 + annualized=1 → reward≈0.1, 实际 {r_override}"


def test_forward_window_reward_config_override_lam_mdd():
    """set_config_override() 改 lam_mdd 影响 forward_window_reward"""
    # 默认 lam_mdd=1.0
    r_default = forward_window_reward(fwd_sharpe=1.0, fwd_mdd_pct=-10.0)

    # 把 lam_mdd 提到 5.0 → 惩罚更重
    set_config_override({"reward": {"lam_mdd": 5.0}})
    r_override = forward_window_reward(fwd_sharpe=1.0, fwd_mdd_pct=-10.0)

    assert r_override < r_default, "lam_mdd 更大 → 惩罚更重 → reward 更低"
