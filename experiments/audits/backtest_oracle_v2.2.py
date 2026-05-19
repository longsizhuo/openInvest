"""backtest_oracle_v2.2.py — oracle verdict paper trading 回测（v2.2 alloc mapping）

V3 audit 跑过 v2 / v2.1：
- v2  (BUY=50%, ACC=10%):                avg alpha -8.21% vs BAH
- v2.1 (BUY=100%, ACC=30%, TRIM=-10%, SELL=-30%):  avg alpha -2.23%, 1/10 beats BAH

诊断 v2.1：5 个 symbol（EEM/IWM/GC=F/TLT/BTC-USD）oracle 从来没 issue BUY，
只 ACC+HOLD+TRIM。ACC=30% 在 2 年内永远到不了 full deploy → drag 残留。

v2.2 假设：把 ACCUMULATE 从 30% 拉到 50%，让没 BUY 的 symbol 也能在两年内
逐步 full deploy。其他 mapping 不动。

mapping 三版对比：
| verdict     | v2             | v2.1            | v2.2 (新)         |
|-------------|----------------|-----------------|--------------------|
| BUY         | 50% cash       | 100% cash       | 100% cash          |
| ACCUMULATE  | 10% cash       | 30% cash        | **50% cash**       |
| HOLD        | 0              | 0               | 0                  |
| TRIM        | -15% holdings  | -10% holdings   | -10% holdings      |
| SELL        | -50% holdings  | -30% holdings   | -30% holdings      |

约束 / 实现细节与 v2/v2.1 一致：严格 walk-forward / yfinance auto_adjust /
core.strategy_metrics / BAH = first decision_date 全仓持有到底。

输出：
- experiments/audits/v3_oracle_backtest_v2.2.json   (schema + v2/v2.1 三版对比段)
- stdout 打印 ≤20 行 v2 vs v2.1 vs v2.2 对比表 + 判定
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

from core.strategy_metrics import (  # noqa: E402
    annualized_return_pct,
    max_drawdown_pct,
    sharpe_ratio,
    total_return_pct,
)

TRAINSET = ROOT / "experiments" / "dspy_trainset_v2.json"
OUTPUT = ROOT / "experiments" / "audits" / "v3_oracle_backtest_v2.2.json"
V2_RESULTS = ROOT / "experiments" / "audits" / "v3_oracle_backtest.json"
V21_RESULTS = ROOT / "experiments" / "audits" / "v3_oracle_backtest_v2.1.json"

START_CAPITAL = 100_000.0
START_DATE = "2024-05-01"
END_DATE = "2026-05-01"

# ===== v2.2 新 mapping =====
# 唯一变化：ACCUMULATE 从 30% 拉到 50%，解决 5 个 symbol 永远到不了 full deploy 的问题
VERDICT_ACTIONS = {
    "BUY":        ("buy",  1.00),   # 100% cash 全仓
    "ACCUMULATE": ("buy",  0.50),   # **50% cash**（v2.1 是 30%）
    "HOLD":       ("hold", 0.00),
    "TRIM":       ("sell", 0.10),   # 减仓 10%
    "SELL":       ("sell", 0.30),   # 减仓 30%
}

# 用于 JSON 输出
ALLOC_MAPPING = {
    "BUY": 1.0,
    "ACCUMULATE": 0.50,
    "HOLD": 0.0,
    "TRIM": -0.10,
    "SELL": -0.30,
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

    out: dict[str, list[dict]] = {}
    for sym, by_date in by_symbol.items():
        out[sym] = sorted(by_date.values(), key=lambda r: r["decision_date"])
    return out


def fetch_prices(symbol: str, start: str, end: str) -> pd.Series:
    """拉 yfinance 历史 close。auto_adjust=True 用复权 close。"""
    start_dt = (datetime.fromisoformat(start) - timedelta(days=10)).strftime("%Y-%m-%d")
    end_dt = (datetime.fromisoformat(end) + timedelta(days=10)).strftime("%Y-%m-%d")
    df = yf.download(symbol, start=start_dt, end=end_dt, progress=False, auto_adjust=True)
    if df.empty:
        raise RuntimeError(f"no data for {symbol}")
    close = df["Close"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    close.index = close.index.strftime("%Y-%m-%d")
    return close


def get_price_on_or_after(close: pd.Series, date_str: str) -> tuple[str, float] | None:
    later = close.index[close.index >= date_str]
    if len(later) == 0:
        return None
    d = later[0]
    return d, float(close.loc[d])


def simulate_oracle(decisions: list[dict], close: pd.Series) -> dict:
    """对单个 symbol 跑 paper trading (v2.1 mapping)。"""
    cash = START_CAPITAL
    shares = 0.0
    transactions = []
    verdict_counts: Counter = Counter()
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
        while next_tx is not None and next_tx["date"] <= d:
            if next_tx["side"] == "BUY":
                cash2 -= next_tx["shares"] * next_tx["price"]
                shares2 += next_tx["shares"]
            else:
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
        "mdd": -max_drawdown_pct(daily_values),
        "sharpe": sharpe_ratio(daily_values),
    }


def load_v2_alphas() -> dict[str, float]:
    """从 v2 backtest JSON 读老 alpha，做对比。"""
    if not V2_RESULTS.exists():
        return {}
    data = json.loads(V2_RESULTS.read_text())
    return {
        sym: r["alpha_pct"]
        for sym, r in data.get("results_per_symbol", {}).items()
    }


def load_v21_alphas() -> dict[str, float]:
    """从 v2.1 backtest JSON 读 alpha。"""
    if not V21_RESULTS.exists():
        return {}
    data = json.loads(V21_RESULTS.read_text())
    return {
        sym: r["alpha_pct"]
        for sym, r in data.get("results_per_symbol", {}).items()
    }


def main() -> None:
    print(f"[backtest v2.2] loading trainset from {TRAINSET}")
    by_symbol = load_oracle_decisions()
    symbols = sorted(by_symbol.keys())
    print(f"[backtest v2.2] {len(symbols)} symbols: {symbols}")
    print(f"[backtest v2.2] alloc mapping: {ALLOC_MAPPING}")

    v2_alphas = load_v2_alphas()
    v2_avg_alpha = round(float(np.mean(list(v2_alphas.values()))), 4) if v2_alphas else None
    v21_alphas = load_v21_alphas()
    v21_avg_alpha = round(float(np.mean(list(v21_alphas.values()))), 4) if v21_alphas else None

    results_per_symbol = {}
    alphas = []
    wins = 0
    losses = 0

    for sym in symbols:
        decisions = by_symbol[sym]
        print(f"[backtest v2.2] {sym}: {len(decisions)} decision_dates  fetching prices ...")
        try:
            close = fetch_prices(sym, START_DATE, END_DATE)
        except Exception as e:
            print(f"[backtest v2.2] {sym}: FAILED to fetch prices: {e}")
            continue

        sim = simulate_oracle(decisions, close)
        if not sim["daily_values"]:
            print(f"[backtest v2.2] {sym}: empty daily_values, skipping")
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
            "oracle_v2.2": oracle_metrics,
            "buy_and_hold": bah_metrics,
            "alpha_pct": alpha,
            "v2_alpha_pct": v2_alphas.get(sym),
            "v2.1_alpha_pct": v21_alphas.get(sym),
            "alpha_improvement_vs_v2_pp": round(alpha - v2_alphas[sym], 4) if sym in v2_alphas else None,
            "alpha_improvement_vs_v2.1_pp": round(alpha - v21_alphas[sym], 4) if sym in v21_alphas else None,
            "verdict_distribution": sim["verdict_counts"],
            "n_transactions": len(sim["transactions"]),
            "final_value_oracle": round(sim["final_value"], 2),
            "final_value_bah": round(bah[-1][1] if bah else START_CAPITAL, 2),
            "period_start": sim["first_traded_date"],
            "period_end": sim["daily_values"][-1][0],
        }

    avg_alpha = round(float(np.mean(alphas)), 4) if alphas else 0.0
    median_alpha = round(float(np.median(alphas)), 4) if alphas else 0.0

    improvement_vs_v2_pp = (
        round(avg_alpha - v2_avg_alpha, 4) if v2_avg_alpha is not None else None
    )
    improvement_vs_v21_pp = (
        round(avg_alpha - v21_avg_alpha, 4) if v21_avg_alpha is not None else None
    )

    # 验收判定（按用户验收条款 — 验收基线是 v2.2 自己的 avg_alpha 绝对值）
    if avg_alpha >= 0:
        verdict = "PATH_A_SUCCESS"
        next_step = (
            "用 v2.2 mapping 重 build trainset (experiments/dspy_trainset_v2.2.json) "
            "→ 重训 oracle / DSPy → 接入 production。"
        )
        conclusion = (
            f"v2.2 跑赢 BAH ({avg_alpha:+.2f}%)。"
            f"v2 {v2_avg_alpha:+.2f}% → v2.1 {v21_avg_alpha:+.2f}% → v2.2 {avg_alpha:+.2f}%，"
            f"vs v2.1 提升 {improvement_vs_v21_pp:+.2f}pp。"
            f"{wins}/{len(alphas)} 赢 BAH。{next_step}"
        )
    elif avg_alpha >= -1:
        verdict = "PATH_A_MARGINAL"
        next_step = (
            "v2.2 marginal 可接入：先用此 mapping 重 build trainset 训 LLM，"
            "同时启动路径 b (bear leg) 或路径 c (alpha-based oracle) 探索剩余 gap。"
        )
        conclusion = (
            f"v2.2 marginal ({avg_alpha:+.2f}%, [-1%, 0%] 区间)。"
            f"v2 {v2_avg_alpha:+.2f}% → v2.1 {v21_avg_alpha:+.2f}% → v2.2 {avg_alpha:+.2f}%，"
            f"vs v2.1 提升 {improvement_vs_v21_pp:+.2f}pp。{next_step}"
        )
    else:
        verdict = "PATH_A_INSUFFICIENT"
        next_step = (
            "单调 alloc mapping 调不到 break-even。推荐：路径 b (bear leg / 短线 timing 信号) "
            "或路径 c (alpha-based oracle，直接训 LLM 预测 forward return 而非 verdict label)。"
        )
        conclusion = (
            f"v2.2 仍显著输 BAH ({avg_alpha:+.2f}%, < -1%)。"
            f"v2 {v2_avg_alpha:+.2f}% → v2.1 {v21_avg_alpha:+.2f}% → v2.2 {avg_alpha:+.2f}%，"
            f"vs v2.1 提升 {improvement_vs_v21_pp:+.2f}pp。{next_step}"
        )

    out = {
        "period": f"{START_DATE} to {END_DATE}",
        "starting_capital": START_CAPITAL,
        "n_symbols": len(results_per_symbol),
        "alloc_mapping": ALLOC_MAPPING,
        "results_per_symbol": results_per_symbol,
        "average_alpha_vs_bah": avg_alpha,
        "median_alpha_vs_bah": median_alpha,
        "symbols_where_oracle_beats_bah": wins,
        "symbols_where_oracle_loses_bah": losses,
        "comparison_three_versions": {
            "v2_avg_alpha": v2_avg_alpha,
            "v2.1_avg_alpha": v21_avg_alpha,
            "v2.2_avg_alpha": avg_alpha,
            "improvement_vs_v2_pp": improvement_vs_v2_pp,
            "improvement_vs_v2.1_pp": improvement_vs_v21_pp,
        },
        "path_a_verdict": verdict,
        "conclusion": conclusion,
        "next_step": next_step,
    }

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"\n[backtest v2.2] wrote {OUTPUT}")

    # ≤20 行 human-readable 三版对比表
    print("\n" + "=" * 78)
    print(f"Oracle v2 vs v2.1 vs v2.2  ({START_DATE} -> {END_DATE})  start ${START_CAPITAL:,.0f}")
    print("v2  : BUY 50%  / ACC 10% / TRIM -15% / SELL -50%")
    print("v2.1: BUY 100% / ACC 30% / TRIM -10% / SELL -30%")
    print("v2.2: BUY 100% / ACC 50% / TRIM -10% / SELL -30%   <- 本次")
    print("=" * 78)
    print(f"{'symbol':<10}{'bah':>9}{'v2_a':>9}{'v2.1_a':>9}{'v2.2_a':>9}{'Δ_v2.1':>10}")
    print("-" * 78)
    for sym, r in sorted(results_per_symbol.items(), key=lambda kv: -kv[1]["alpha_pct"]):
        b = r["buy_and_hold"]["cum_return"]
        v22a = r["alpha_pct"]
        v2a = r["v2_alpha_pct"]
        v21a = r["v2.1_alpha_pct"]
        d21 = r["alpha_improvement_vs_v2.1_pp"]
        v2a_s  = f"{v2a:>+7.2f}%" if v2a  is not None else "    n/a"
        v21a_s = f"{v21a:>+7.2f}%" if v21a is not None else "    n/a"
        d21_s  = f"{d21:>+8.2f}"  if d21  is not None else "     n/a"
        print(f"{sym:<10}{b:>+7.2f}%{v2a_s}{v21a_s}{v22a:>+7.2f}%{d21_s}")
    print("-" * 78)
    print(f"Avg alpha    v2: {v2_avg_alpha:+.2f}%   v2.1: {v21_avg_alpha:+.2f}%   v2.2: {avg_alpha:+.2f}%")
    print(f"Δ vs v2.1: {improvement_vs_v21_pp:+.2f}pp   |   v2.2 beats BAH: {wins}/{len(alphas)}   median: {median_alpha:+.2f}%")
    print(f"\nVerdict: {verdict}")
    print(f"{conclusion}")


if __name__ == "__main__":
    main()
