"""strategy_metrics.py — paper trading 策略级评估指标

输入：PaperAccount.daily_values（[(date, cny_value), ...]）+ 基准曲线 dict
输出：年化收益 / 回撤 / Sharpe / Sortino / vs 基准 alpha / HOLD 机会成本

设计：纯数学计算，零依赖 LLM / 网络。
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Tuple

import numpy as np

DailyValues = List[Tuple[str, float]]


def _to_returns(daily_values: DailyValues) -> np.ndarray:
    """daily_values → 日收益率序列"""
    if len(daily_values) < 2:
        return np.array([])
    vals = np.array([v for _, v in daily_values], dtype=float)
    # 日收益率 = (v_t / v_{t-1}) - 1
    rets = np.diff(vals) / vals[:-1]
    return rets


def total_return_pct(daily_values: DailyValues) -> float:
    """总收益率 (%)。空数据返回 0"""
    if len(daily_values) < 2:
        return 0.0
    start = daily_values[0][1]
    end = daily_values[-1][1]
    if start <= 0:
        return 0.0
    return round((end / start - 1) * 100, 4)


def annualized_return_pct(daily_values: DailyValues, trading_days_per_year: int = 252) -> float:
    """年化收益率 (%)"""
    if len(daily_values) < 2:
        return 0.0
    n_days = len(daily_values)  # 数据点数（假设每日一个）
    total = total_return_pct(daily_values) / 100
    if n_days <= 1 or total <= -1:
        return 0.0
    annualized = (1 + total) ** (trading_days_per_year / n_days) - 1
    return round(annualized * 100, 4)


def max_drawdown_pct(daily_values: DailyValues) -> float:
    """最大回撤 (%)。返回的是绝对值（如 15.3 表示最大回撤 -15.3%）"""
    if len(daily_values) < 2:
        return 0.0
    vals = np.array([v for _, v in daily_values], dtype=float)
    cummax = np.maximum.accumulate(vals)
    drawdowns = (vals - cummax) / cummax
    return round(abs(drawdowns.min()) * 100, 4)


def volatility_pct(daily_values: DailyValues, trading_days_per_year: int = 252) -> float:
    """年化波动率 (%)"""
    rets = _to_returns(daily_values)
    if len(rets) == 0:
        return 0.0
    daily_vol = np.std(rets, ddof=1) if len(rets) > 1 else 0.0
    return round(daily_vol * math.sqrt(trading_days_per_year) * 100, 4)


def sharpe_ratio(
    daily_values: DailyValues,
    risk_free_annual: float = 0.013,  # 余额宝当前 1.3%
    trading_days_per_year: int = 252,
) -> float:
    """Sharpe = (年化收益 - 无风险利率) / 年化波动率"""
    rets = _to_returns(daily_values)
    if len(rets) < 2:
        return 0.0
    daily_rf = risk_free_annual / trading_days_per_year
    excess = rets - daily_rf
    std = np.std(excess, ddof=1)
    # 分母接近 0（账户全程没动 / 数据点太少）会爆掉
    if std < 1e-9:
        return 0.0
    sharpe = np.mean(excess) / std * math.sqrt(trading_days_per_year)
    # 保护性 clip，理论 Sharpe > 10 都极罕见
    if abs(sharpe) > 100:
        return 0.0
    return round(float(sharpe), 4)


def sortino_ratio(
    daily_values: DailyValues,
    risk_free_annual: float = 0.013,
    trading_days_per_year: int = 252,
) -> float:
    """Sortino = (年化收益 - 无风险) / 年化下行波动率（只算亏的日子）"""
    rets = _to_returns(daily_values)
    if len(rets) < 2:
        return 0.0
    daily_rf = risk_free_annual / trading_days_per_year
    excess = rets - daily_rf
    downside = excess[excess < 0]
    if len(downside) == 0:
        return 0.0  # 没亏过，Sortino 无穷大；返 0 表示"不可计算"
    # downside 样本太少 (< 5) 时 std 不稳定，会导致 Sortino 爆炸
    if len(downside) < 5:
        return 0.0
    downside_dev = np.std(downside, ddof=1)
    if downside_dev < 1e-6:
        return 0.0
    sortino = np.mean(excess) / downside_dev * math.sqrt(trading_days_per_year)
    # 保护性 clip：理论上 Sortino > 10 都极罕见
    if abs(sortino) > 100:
        return 0.0
    return round(float(sortino), 4)


def vs_benchmark(daily_values: DailyValues, benchmark_curve: DailyValues) -> Dict[str, float]:
    """跟基准曲线对比，返回 {alpha_pct, beat_days_pct}

    alpha_pct = 策略总收益 - 基准总收益
    beat_days_pct = 策略 daily return 高于基准的天数比例
    """
    if not daily_values or not benchmark_curve:
        return {"alpha_pct": 0.0, "beat_days_pct": 0.0}

    strat_return = total_return_pct(daily_values)
    bench_return = total_return_pct(benchmark_curve)
    alpha = strat_return - bench_return

    # 对齐日期算 beat_days
    strat_dict = dict(daily_values)
    bench_dict = dict(benchmark_curve)
    common_dates = sorted(set(strat_dict.keys()) & set(bench_dict.keys()))
    if len(common_dates) < 2:
        return {"alpha_pct": round(alpha, 4), "beat_days_pct": 0.0}

    strat_rets, bench_rets = [], []
    for i in range(1, len(common_dates)):
        d_prev, d = common_dates[i - 1], common_dates[i]
        if strat_dict[d_prev] > 0 and bench_dict[d_prev] > 0:
            strat_rets.append(strat_dict[d] / strat_dict[d_prev] - 1)
            bench_rets.append(bench_dict[d] / bench_dict[d_prev] - 1)

    if not strat_rets:
        return {"alpha_pct": round(alpha, 4), "beat_days_pct": 0.0}

    beat = sum(1 for s, b in zip(strat_rets, bench_rets) if s > b)
    beat_pct = beat / len(strat_rets) * 100

    return {"alpha_pct": round(alpha, 4), "beat_days_pct": round(beat_pct, 2)}


def win_rate_per_trade(transactions: List[Any]) -> float:
    """单笔成交盈利比例 = 卖出价 > 买入均价 的卖单 / 总卖单。

    BUY 单不能算 win/loss（要等卖了才知道）。
    SKIP / HOLD 不计入。
    """
    sells = [t for t in transactions if t.action == "SELL"]
    if not sells:
        return 0.0
    # 简化：当前 sell.price > sim 上一笔同 asset 的 BUY avg_cost 算 win
    # （真严格要按 FIFO 算 realized PnL，这里近似）
    wins = 0
    for s in sells:
        # 找之前最近的同 asset BUY
        for b in reversed(transactions):
            if b is s:
                continue
            if b.asset == s.asset and b.action == "BUY":
                if s.price > b.price:
                    wins += 1
                break
    return round(wins / len(sells) * 100, 2)


def hold_opportunity_cost(
    transactions: List[Any], market_curves: Dict[str, DailyValues],
) -> float:
    """HOLD 期间市场实际涨了多少（机会成本）

    对每个 HOLD verdict：找该资产在 decision_date 之后 7 天的 return，累加。
    如果 HOLD 期间市场总共涨了 5%，opportunity_cost ~= 5%。
    """
    holds = [t for t in transactions if t.action == "HOLD"]
    if not holds:
        return 0.0

    total_missed_pct = 0.0
    count = 0
    for h in holds:
        curve = market_curves.get(h.asset)
        if not curve:
            continue
        # 找 decision_date 之后 7 天的 return
        curve_dict = dict(curve)
        dates_after = sorted([d for d in curve_dict.keys() if d > h.decision_date])
        if len(dates_after) < 5:
            continue
        try:
            start_val = curve_dict[h.decision_date]
            end_val = curve_dict[dates_after[6]] if len(dates_after) > 6 else curve_dict[dates_after[-1]]
            if start_val > 0:
                total_missed_pct += (end_val / start_val - 1) * 100
                count += 1
        except (KeyError, IndexError):
            continue

    if count == 0:
        return 0.0
    return round(total_missed_pct / count, 4)


def evaluate_strategy(
    daily_values: DailyValues,
    transactions: List[Any],
    benchmark_curves: Dict[str, DailyValues],
    market_curves: Dict[str, DailyValues] | None = None,
) -> Dict[str, Any]:
    """跑完一次 walk-forward 后，输出完整 metrics dict。

    Args:
        daily_values: 账户每日 (date, cny_value) 时序
        transactions: 所有成交流水
        benchmark_curves: {名称: daily_values} 如 {"沪深300": [...], "余额宝": [...]}
        market_curves: 每个资产的 daily_values（给 hold_opportunity_cost 用）
    """
    metrics: Dict[str, Any] = {
        "total_return_pct": total_return_pct(daily_values),
        "annualized_return_pct": annualized_return_pct(daily_values),
        "max_drawdown_pct": max_drawdown_pct(daily_values),
        "volatility_pct": volatility_pct(daily_values),
        "sharpe_ratio": sharpe_ratio(daily_values),
        "sortino_ratio": sortino_ratio(daily_values),
        "win_rate_per_trade": win_rate_per_trade(transactions),
        "n_transactions": len(transactions),
        "n_buys": sum(1 for t in transactions if t.action == "BUY"),
        "n_sells": sum(1 for t in transactions if t.action == "SELL"),
        "n_holds": sum(1 for t in transactions if t.action == "HOLD"),
        "n_skips": sum(1 for t in transactions if t.action == "SKIP"),
        "vs_benchmarks": {
            name: vs_benchmark(daily_values, curve)
            for name, curve in benchmark_curves.items()
        },
    }

    if market_curves:
        metrics["hold_opportunity_cost_pct"] = hold_opportunity_cost(transactions, market_curves)

    return metrics

# 完整历史导出面（含下划线名/常量）——façade `import *` 的完备性依赖本列表
__all__ = [
    "DailyValues",
    "_to_returns",
    "total_return_pct",
    "annualized_return_pct",
    "max_drawdown_pct",
    "volatility_pct",
    "sharpe_ratio",
    "sortino_ratio",
    "vs_benchmark",
    "win_rate_per_trade",
    "hold_opportunity_cost",
    "evaluate_strategy",
]
