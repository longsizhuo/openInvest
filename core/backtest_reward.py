"""backtest_reward.py — strategy-level reward function

把 strategy_metrics 的 dict 转成单个数值 reward，给 Optuna / DSPy 用。

设计原则（跟用户 insight 一致）：
- **主信号：年化收益**（账户真的赚了多少 — 用户最关心）
- 风险惩罚：最大回撤过大要扣分（避免高收益高波动 policy）
- 基准超额：vs 余额宝 alpha（避免 LLM 输给"什么都不做"）
- Sharpe 加成：单位风险下收益

这不是逐 verdict 命中率（那是噪音），是完整 P&L 曲线综合评分。
"""
from __future__ import annotations

from typing import Any, Dict


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
    # 都是百分比（如 12.5 = 12.5%），转成小数
    annualized = metrics.get("annualized_return_pct", 0) / 100
    max_dd = metrics.get("max_drawdown_pct", 0) / 100  # 已是绝对值
    sharpe = metrics.get("sharpe_ratio", 0)

    # vs 余额宝 alpha（如果有）
    vs = metrics.get("vs_benchmarks", {}).get("余额宝", {})
    alpha_yuebao = vs.get("alpha_pct", 0) / 100

    reward = (
        annualized
        - 0.5 * max_dd
        + 0.5 * alpha_yuebao
        + 0.2 * max(0, sharpe - 1.0)
    )

    return round(reward, 6)


def explain_reward(metrics: Dict[str, Any]) -> str:
    """人类可读的 reward 分解（debug / 训练报告用）"""
    annualized = metrics.get("annualized_return_pct", 0)
    max_dd = metrics.get("max_drawdown_pct", 0)
    sharpe = metrics.get("sharpe_ratio", 0)
    vs = metrics.get("vs_benchmarks", {}).get("余额宝", {})
    alpha_yuebao = vs.get("alpha_pct", 0)

    reward = compute_strategy_reward(metrics)

    return (
        f"reward = {reward:.4f}\n"
        f"  ├─ 年化收益: {annualized:+.2f}%  →  +{annualized/100:.4f}\n"
        f"  ├─ 最大回撤: {max_dd:.2f}%   →  -{0.5 * max_dd/100:.4f}\n"
        f"  ├─ vs 余额宝: {alpha_yuebao:+.2f}%  →  +{0.5 * alpha_yuebao/100:.4f}\n"
        f"  └─ Sharpe ratio: {sharpe:.2f}  →  +{0.2 * max(0, sharpe-1.0):.4f}"
    )
