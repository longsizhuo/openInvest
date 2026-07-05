"""backtest_oracle_paper_trading.py — oracle verdict paper trading 回测

目的：在历史窗口里"完美"执行 v2 trainset 给的 oracle verdict，看是否跑赢
buy-and-hold（BAH）。这是 v2 训练目标的 *上限*——LLM 永远不可能比 oracle 更
准，所以 oracle 都跑不过 BAH 的话规则本身就有问题。

约束：
1. 严格 walk-forward（按 decision_date 顺序逐个推进）
2. 不使用 trainset 里的 forward_30d_* 字段做仓位决策（那是 oracle 的内部数据）
3. 每个 decision_date 只取 portfolio_state = "cash 100%" 那条（去重，每天每符号
   只有一个 verdict）
4. 用 yfinance 拉真实 close 价成交，不算手续费

verdict → 仓位调整：
- BUY:         用 50% 当前现金 一次性买入
- ACCUMULATE:  用 10% 当前现金 加仓
- HOLD:        不动
- TRIM:        卖出 15% 当前持仓 (按市值)
- SELL:        卖出 50% 当前持仓 (按市值)

输出：
- experiments/audits/v3_oracle_backtest.json
- stdout 打印 ≤30 行总结
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

# 让脚本可以独立跑（不依赖项目作为 package import）
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from openinvest.core.strategy_metrics import (  # noqa: E402
    annualized_return_pct,
    max_drawdown_pct,
    sharpe_ratio,
    total_return_pct,
)

TRAINSET = ROOT / "experiments" / "dspy_trainset_v2.json"
OUTPUT = ROOT / "experiments" / "audits" / "v3_oracle_backtest.json"

START_CAPITAL = 100_000.0
START_DATE = "2024-05-01"
END_DATE = "2026-05-01"

# verdict → (action, ratio) ratio 表示对 base（现金 or 持仓市值）的比例
VERDICT_ACTIONS = {
    "BUY":        ("buy",  0.50),
    "ACCUMULATE": ("buy",  0.10),
    "HOLD":       ("hold", 0.00),
    "TRIM":       ("sell", 0.15),
    "SELL":       ("sell", 0.50),
}


def load_oracle_decisions() -> dict[str, list[dict]]:
    """读 trainset，按 symbol 分组，每个 (symbol, decision_date) 只保留 cash=100%
    那条（避免一个 decision_date 重复 5 条 portfolio_state）。
    """
    samples = json.loads(TRAINSET.read_text())
    by_symbol: dict[str, dict[str, dict]] = defaultdict(dict)
    for s in samples:
        # 只挑初始 100% cash 的那条（每个日期每个 symbol 唯一一条 oracle verdict）
        if "cash 100%" not in s["portfolio_state"]:
            continue
        by_symbol[s["symbol"]][s["decision_date"]] = s

    # 转 list，按 decision_date 排序
    out: dict[str, list[dict]] = {}
    for sym, by_date in by_symbol.items():
        out[sym] = sorted(by_date.values(), key=lambda r: r["decision_date"])
    return out


def fetch_prices(symbol: str, start: str, end: str) -> pd.Series:
    """拉 yfinance 历史 close，返回 date -> close 的 Series（auto_adjust=True 用
    复权 close，避免分红/拆股扭曲）。
    """
    # 多拉一周防止 decision_date 是非交易日
    start_dt = (datetime.fromisoformat(start) - timedelta(days=10)).strftime("%Y-%m-%d")
    end_dt = (datetime.fromisoformat(end) + timedelta(days=10)).strftime("%Y-%m-%d")
    df = yf.download(symbol, start=start_dt, end=end_dt, progress=False, auto_adjust=True)
    if df.empty:
        raise RuntimeError(f"no data for {symbol}")
    close = df["Close"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]  # 单 ticker 也会被 yfinance 搞成 MultiIndex
    close.index = close.index.strftime("%Y-%m-%d")
    return close


def get_price_on_or_after(close: pd.Series, date_str: str) -> tuple[str, float] | None:
    """找 >= date_str 的第一个交易日 close。decision_date 是日历日，可能落在
    周末，要往后顺延到下一个交易日。
    """
    later = close.index[close.index >= date_str]
    if len(later) == 0:
        return None
    d = later[0]
    return d, float(close.loc[d])


def simulate_oracle(decisions: list[dict], close: pd.Series) -> dict:
    """对单个 symbol 跑 paper trading。返回 daily_values + transactions +
    verdict_distribution + 最后市值。
    """
    cash = START_CAPITAL
    shares = 0.0
    transactions = []
    verdict_counts: Counter = Counter()
    # 记录每个 decision_date 的成交价（用于 BAH 对齐）
    first_traded_date: str | None = None
    first_traded_price: float | None = None

    for s in decisions:
        verdict = s["verdict"]
        verdict_counts[verdict] += 1
        action, ratio = VERDICT_ACTIONS[verdict]
        priced = get_price_on_or_after(close, s["decision_date"])
        if priced is None:
            continue
        traded_date, price = priced
        if first_traded_date is None:
            first_traded_date = traded_date
            first_traded_price = price

        if action == "buy":
            spend = cash * ratio
            if spend > 0 and price > 0:
                bought = spend / price
                cash -= spend
                shares += bought
                transactions.append({
                    "date": traded_date, "verdict": verdict, "side": "BUY",
                    "shares": bought, "price": price, "cash_after": cash,
                })
        elif action == "sell":
            sell_shares = shares * ratio
            if sell_shares > 0 and price > 0:
                proceeds = sell_shares * price
                cash += proceeds
                shares -= sell_shares
                transactions.append({
                    "date": traded_date, "verdict": verdict, "side": "SELL",
                    "shares": sell_shares, "price": price, "cash_after": cash,
                })
        # HOLD 不动

    # 收尾：用 close 数据每日 mark-to-market 算 daily_values（用于 sharpe/mdd）
    # 仅取 [first_traded_date, END_DATE] 区间
    if first_traded_date is None:
        return {"daily_values": [], "transactions": [], "verdict_counts": dict(verdict_counts),
                "final_value": START_CAPITAL, "first_traded_date": None,
                "first_traded_price": None}

    # 按交易顺序重新模拟 daily_values
    daily_values: list[tuple[str, float]] = []
    cash2 = START_CAPITAL
    shares2 = 0.0
    tx_iter = iter(transactions)
    next_tx = next(tx_iter, None)

    for d, p in close.items():
        if d < first_traded_date:
            continue
        if d > END_DATE:
            break
        # 把这一天落地的所有 tx 执行掉
        while next_tx is not None and next_tx["date"] <= d:
            if next_tx["side"] == "BUY":
                cash2 -= next_tx["shares"] * next_tx["price"]
                shares2 += next_tx["shares"]
            else:  # SELL
                cash2 += next_tx["shares"] * next_tx["price"]
                shares2 -= next_tx["shares"]
            next_tx = next(tx_iter, None)
        portfolio_value = cash2 + shares2 * float(p)
        daily_values.append((d, portfolio_value))

    return {
        "daily_values": daily_values,
        "transactions": transactions,
        "verdict_counts": dict(verdict_counts),
        "final_value": daily_values[-1][1] if daily_values else START_CAPITAL,
        "first_traded_date": first_traded_date,
        "first_traded_price": first_traded_price,
    }


def simulate_buy_and_hold(close: pd.Series, first_date: str, first_price: float) -> list[tuple[str, float]]:
    """BAH：first_date 全仓买入，持有到 END_DATE。"""
    shares = START_CAPITAL / first_price
    daily_values: list[tuple[str, float]] = []
    for d, p in close.items():
        if d < first_date:
            continue
        if d > END_DATE:
            break
        daily_values.append((d, shares * float(p)))
    return daily_values


def compute_metrics(daily_values: list[tuple[str, float]]) -> dict:
    return {
        "cum_return": total_return_pct(daily_values),
        "annualized": annualized_return_pct(daily_values),
        "mdd": -max_drawdown_pct(daily_values),  # 用负号表示回撤方向
        "sharpe": sharpe_ratio(daily_values),
    }


def main() -> None:
    print(f"[backtest] loading trainset from {TRAINSET}")
    by_symbol = load_oracle_decisions()
    symbols = sorted(by_symbol.keys())
    print(f"[backtest] {len(symbols)} symbols: {symbols}")

    results_per_symbol = {}
    alphas = []
    wins = 0
    losses = 0

    for sym in symbols:
        decisions = by_symbol[sym]
        print(f"[backtest] {sym}: {len(decisions)} decision_dates  fetching prices ...")
        try:
            close = fetch_prices(sym, START_DATE, END_DATE)
        except Exception as e:
            print(f"[backtest] {sym}: FAILED to fetch prices: {e}")
            continue

        sim = simulate_oracle(decisions, close)
        if not sim["daily_values"]:
            print(f"[backtest] {sym}: empty daily_values, skipping")
            continue

        bah = simulate_buy_and_hold(close, sim["first_traded_date"], sim["first_traded_price"])
        oracle_metrics = compute_metrics(sim["daily_values"])
        bah_metrics = compute_metrics(bah)
        alpha = round(oracle_metrics["cum_return"] - bah_metrics["cum_return"], 4)

        if alpha > 0:
            wins += 1
        else:
            losses += 1
        alphas.append(alpha)

        results_per_symbol[sym] = {
            "n_decisions": len(decisions),
            "oracle_strategy": oracle_metrics,
            "buy_and_hold": bah_metrics,
            "alpha_pct": alpha,
            "verdict_distribution": sim["verdict_counts"],
            "n_transactions": len(sim["transactions"]),
            "final_value_oracle": round(sim["final_value"], 2),
            "final_value_bah": round(bah[-1][1] if bah else START_CAPITAL, 2),
            "period_start": sim["first_traded_date"],
            "period_end": sim["daily_values"][-1][0],
        }

    avg_alpha = round(float(np.mean(alphas)), 4) if alphas else 0.0
    median_alpha = round(float(np.median(alphas)), 4) if alphas else 0.0

    # 写结论：自动判断
    if avg_alpha > 5:
        conclusion = (
            f"oracle 平均跑赢 BAH +{avg_alpha:.2f}%，{wins}/{len(alphas)} 个 symbol 赢 BAH。"
            "v2 训练目标合理，LLM 学习这种规则有上限收益。"
        )
    elif avg_alpha > 0:
        conclusion = (
            f"oracle 仅小幅跑赢 BAH +{avg_alpha:.2f}%（中位 {median_alpha:+.2f}%）。"
            f"{wins} 赢 / {losses} 输。规则有正 alpha 但不大，LLM 学到也只能贴近 BAH。"
        )
    else:
        conclusion = (
            f"oracle 跑输 BAH {avg_alpha:+.2f}%，{losses}/{len(alphas)} 输。"
            "v2 规则本身有问题——TRIM/SELL 过度，BUY 时机也未必胜过满仓持有。"
            "继续训练 LLM 拟合这个 oracle 是在追求一个负 alpha 的上限。"
        )

    out = {
        "period": f"{START_DATE} to {END_DATE}",
        "starting_capital": START_CAPITAL,
        "n_symbols": len(results_per_symbol),
        "results_per_symbol": results_per_symbol,
        "average_alpha_vs_bah": avg_alpha,
        "median_alpha_vs_bah": median_alpha,
        "symbols_where_oracle_beats_bah": wins,
        "symbols_where_oracle_loses_bah": losses,
        "conclusion": conclusion,
    }

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"\n[backtest] wrote {OUTPUT}")

    # human-readable 总结 (≤30 行)
    print("\n" + "=" * 70)
    print(f"Oracle vs Buy-and-Hold backtest  ({START_DATE} → {END_DATE})")
    print(f"Starting capital: ¥{START_CAPITAL:,.0f}   |   verdict→action: BUY 50%, ACC 10%, TRIM -15%, SELL -50%")
    print("=" * 70)
    print(f"{'symbol':<10}{'n_dec':>6}{'oracle_cum':>12}{'bah_cum':>11}{'alpha':>10}{'orcl_mdd':>11}{'orcl_sh':>10}")
    print("-" * 70)
    for sym, r in sorted(results_per_symbol.items(), key=lambda kv: -kv[1]["alpha_pct"]):
        o = r["oracle_strategy"]
        b = r["buy_and_hold"]
        print(f"{sym:<10}{r['n_decisions']:>6}{o['cum_return']:>11.2f}%{b['cum_return']:>10.2f}%"
              f"{r['alpha_pct']:>+9.2f}%{o['mdd']:>+10.2f}%{o['sharpe']:>10.2f}")
    print("-" * 70)
    print(f"Average alpha: {avg_alpha:+.2f}%   Median alpha: {median_alpha:+.2f}%")
    print(f"Oracle beats BAH: {wins}/{len(alphas)}   Loses: {losses}/{len(alphas)}")
    print(f"\nConclusion: {conclusion}")


if __name__ == "__main__":
    main()
