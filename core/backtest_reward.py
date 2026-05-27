"""backtest_reward.py — strategy-level reward function

把 strategy_metrics 的 dict 转成单个数值 reward，给 Optuna / DSPy 用。

设计原则（跟用户 insight 一致）：
- **主信号：年化收益**（账户真的赚了多少 — 用户最关心）
- 风险惩罚：最大回撤过大要扣分（避免高收益高波动 policy）
- 基准超额：vs 余额宝 alpha（避免 LLM 输给"什么都不做"）
- Sharpe 加成：单位风险下收益

这不是逐 verdict 命中率（那是噪音），是完整 P&L 曲线综合评分。

v2 追加（per-sample forward-window reward）：
- `forward_window_reward`: 单个决策的 forward window Sharpe + MDD + return 合成 reward
- `verdict_oracle_accuracy`: verdict 方向 vs 实际 outcome 一致性（DSPy metric）
"""
from __future__ import annotations

from typing import Any, Dict

__all__ = [
    "compute_strategy_reward",
    "explain_reward",
    "forward_window_reward",
    "verdict_oracle_accuracy",
]


def _get_reward_config():
    """读 reward tunable config（实时，支持 set_config_override）。"""
    from core.config import load_config
    return load_config().reward


def _get_oracle_config():
    """读 oracle accuracy tunable config（实时，支持 set_config_override）。"""
    from core.config import load_config
    return load_config().oracle_accuracy


def compute_strategy_reward(metrics: Dict[str, Any]) -> float:
    """从 strategy_metrics 算 reward。

    公式：
        reward = annualized_return
               - 0.5 × max_drawdown
               + 0.5 × alpha_vs_yuebao
               + 0.2 × max(0, sharpe - 1.0)

    数值范围（典型）：
        - 实盘水平好（年化 15%, 回撤 -10%, alpha +13%, Sharpe 1.2）→ reward ≈ 0.21
        - 平庸（年化 5%, 回撤 -15%, alpha +3%, Sharpe 0.5）→ reward ≈ 0.005
        - 亏损（年化 -10%, 回撤 -25%, alpha -11%, Sharpe -0.5）→ reward ≈ -0.18

    Optuna direction="maximize"。
    """
    cfg = _get_reward_config()
    # 都是百分比（如 12.5 = 12.5%），转成小数
    annualized = metrics.get("annualized_return_pct", 0) / 100
    max_dd = metrics.get("max_drawdown_pct", 0) / 100  # 已是绝对值
    sharpe = metrics.get("sharpe_ratio", 0)

    # vs 余额宝 alpha（如果有）
    vs = metrics.get("vs_benchmarks", {}).get("余额宝", {})
    alpha_yuebao = vs.get("alpha_pct", 0) / 100

    reward = (
        cfg.weight_annualized_return * annualized
        + cfg.weight_max_drawdown * max_dd
        + cfg.weight_alpha_vs_yuebao * alpha_yuebao
        + cfg.weight_sharpe_bonus * max(0, sharpe - cfg.sharpe_bonus_threshold)
    )

    return round(reward, 6)


def explain_reward(metrics: Dict[str, Any]) -> str:
    """人类可读的 reward 分解（debug / 训练报告用）"""
    cfg = _get_reward_config()
    annualized = metrics.get("annualized_return_pct", 0)
    max_dd = metrics.get("max_drawdown_pct", 0)
    sharpe = metrics.get("sharpe_ratio", 0)
    vs = metrics.get("vs_benchmarks", {}).get("余额宝", {})
    alpha_yuebao = vs.get("alpha_pct", 0)

    reward = compute_strategy_reward(metrics)

    return (
        f"reward = {reward:.4f}\n"
        f"  ├─ 年化收益: {annualized:+.2f}%  →  +{cfg.weight_annualized_return * annualized/100:.4f}\n"
        f"  ├─ 最大回撤: {max_dd:.2f}%   →  {cfg.weight_max_drawdown * max_dd/100:.4f}\n"
        f"  ├─ vs 余额宝: {alpha_yuebao:+.2f}%  →  +{cfg.weight_alpha_vs_yuebao * alpha_yuebao/100:.4f}\n"
        f"  └─ Sharpe ratio: {sharpe:.2f}  →  +{cfg.weight_sharpe_bonus * max(0, sharpe-cfg.sharpe_bonus_threshold):.4f}"
    )


# ---------------------------------------------------------------------------
# v2: per-sample forward-window reward (DSPy 训练用)
# ---------------------------------------------------------------------------

def forward_window_reward(
    fwd_sharpe: float,
    fwd_mdd_pct: float,
    fwd_return_pct: float = 0.0,
    lam_mdd: float | None = None,
    lam_return: float | None = None,
) -> float:
    """v2 per-sample reward — forward window Sharpe penalty by MDD + small return weight

    公式:
        reward = fwd_sharpe - lam_mdd × |fwd_mdd_pct| / 100 + lam_return × fwd_return_pct / 100

    典型值:
        - 好决策 (Sharpe 1.5, MDD -3%, return +8%): 1.5 - 0.03 + 0.004 ≈ 1.47
        - 平庸 (Sharpe 0.3, MDD -8%, return +1%): 0.3 - 0.08 + 0.0005 ≈ 0.22
        - 烂决策 (Sharpe -0.8, MDD -15%, return -10%): -0.8 - 0.15 - 0.005 ≈ -0.96

    DSPy metric 用 sign(reward) 或者直接当连续 score。

    Args:
        fwd_sharpe: annualized Sharpe 在 forward window 上，无量纲
        fwd_mdd_pct: max drawdown 百分比，负数（如 -8.5 表示 -8.5%）
        fwd_return_pct: 累计 return 百分比，可正可负
        lam_mdd: MDD 惩罚权重，None 时从 config 读（默认 1.0）
        lam_return: 累计 return 加成权重（小权重防双计 Sharpe），None 时从 config 读（默认 0.05）

    Returns:
        float, 通常范围 [-2.0, +2.5]
    """
    cfg = _get_reward_config()
    effective_lam_mdd = lam_mdd if lam_mdd is not None else cfg.lam_mdd
    effective_lam_return = lam_return if lam_return is not None else cfg.lam_return
    # abs() 防止用户传 +5 / -5 两种风格（MDD 语义上一律是惩罚项）
    mdd_penalty = effective_lam_mdd * abs(fwd_mdd_pct) / 100.0
    return_bonus = effective_lam_return * fwd_return_pct / 100.0
    # 不 round —— 保留精度给训练（DSPy / Optuna 梯度近似需要）
    return float(fwd_sharpe) - mdd_penalty + return_bonus


def verdict_oracle_accuracy(verdict: str, fwd_return_pct: float, asset_pct: float = 0.0) -> int:
    """v2 DSPy metric — verdict 跟 forward outcome 一致性

    返回:
        +1: verdict 跟实际 outcome 一致（方向对）
         0: 中性
        -1: 方向错

    规则（必须跟 build_dspy_trainset_v2.py 的 oracle_verdict 对齐）:
        BUY:        return ≥ +5% → +1; return ≤ -3% → -1; else 0
        ACCUMULATE: return ≥ +3% → +1; return ≤ -3% → -1; else 0
        HOLD:       |return| ≤ 3% → +1; |return| ≥ 8% → -1; else 0
        TRIM:       return ≤ -3% → +1; return ≥ +5% → -1; else 0
        SELL:       return ≤ -5% → +1; return ≥ +3% → -1; else 0

    Args:
        verdict: 委员会决议，大小写不敏感（BUY/ACCUMULATE/HOLD/TRIM/SELL）
        fwd_return_pct: forward window 累计 return 百分比（如 +6.0 表示 +6%）
        asset_pct: 当前持仓占比（保留参数，未来可能用来调阈值），目前未启用

    Returns:
        int in {-1, 0, +1}；未知 verdict 返回 0
    """
    cfg = _get_oracle_config()
    v = (verdict or "").strip().upper()
    r = float(fwd_return_pct)

    if v == "BUY":
        if r >= cfg.buy_positive:
            return 1
        if r <= cfg.buy_negative:
            return -1
        return 0
    if v == "ACCUMULATE":
        if r >= cfg.accumulate_positive:
            return 1
        if r <= cfg.accumulate_negative:
            return -1
        return 0
    if v == "HOLD":
        if abs(r) <= cfg.hold_neutral:
            return 1
        if abs(r) >= cfg.hold_wrong:
            return -1
        return 0
    if v == "TRIM":
        if r <= cfg.trim_positive:
            return 1
        if r >= cfg.trim_negative:
            return -1
        return 0
    if v == "SELL":
        if r <= cfg.sell_positive:
            return 1
        if r >= cfg.sell_negative:
            return -1
        return 0
    # 未知 verdict 安全退化
    return 0
