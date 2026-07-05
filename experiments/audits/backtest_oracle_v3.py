"""backtest_oracle_v3.py — oracle verdict paper trading 回测（v3 path c alpha-based）

V3 design：
- 不再用 forward return 绝对阈值标 verdict（v2/v2.1/v2.2 全是这种）。
- oracle 每个 sample 选使 forward-30d portfolio terminal value 最大的 verdict
  （per-sample optimum，考虑当前 starting state asset_pct）。
- ALLOC_MAP 沿用 v2.2：BUY=1.0 / ACC=0.5 / HOLD=0 / TRIM=-0.1 / SELL=-0.3。

期望：
- avg alpha >= 0%（per-sample optimum 在 in-sample 至少应该持平 BAH）。
- 10/10 symbol alpha >= 0。

判定（path c verdict）：
- avg alpha >= +1% 且 10/10 全部 alpha >= 0 → path c **大胜**（建议立刻训练）。
- avg alpha 0% 到 +1% → path c **持平 BAH**（符合理论上界）。
- avg alpha < 0% → path c 有 bug（debug）。

实现细节与 v2.2 backtest 对齐：严格 walk-forward / yfinance auto_adjust /
core.strategy_metrics / BAH = first decision_date 全仓持有到底 /
每个 (symbol, decision_date) 取 cash=100% 那条 sample。

由于 v3 trainset 已经按"oracle 选最优 verdict"重 label，回测时直接读 v3 即可。
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

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from openinvest.core.strategy_metrics import (  # noqa: E402
    annualized_return_pct,
    max_drawdown_pct,
    sharpe_ratio,
    total_return_pct,
)

TRAINSET = ROOT / "experiments" / "dspy_trainset_v3.json"
OUTPUT = ROOT / "experiments" / "audits" / "v3_oracle_backtest_v3.json"

V2_RESULTS = ROOT / "experiments" / "audits" / "v3_oracle_backtest.json"
V21_RESULTS = ROOT / "experiments" / "audits" / "v3_oracle_backtest_v2.1.json"
V22_RESULTS = ROOT / "experiments" / "audits" / "v3_oracle_backtest_v2.2.json"

START_CAPITAL = 100_000.0
START_DATE = "2024-05-01"
END_DATE = "2026-05-01"

# ===== v3 mapping（与 v2.2 共用，verdict labeling 走 path c） =====
VERDICT_ACTIONS = {
    "BUY":        ("buy",  1.00),
    "ACCUMULATE": ("buy",  0.50),
    "HOLD":       ("hold", 0.00),
    "TRIM":       ("sell", 0.10),
    "SELL":       ("sell", 0.30),
}

ALLOC_MAPPING = {
    "BUY": 1.0,
    "ACCUMULATE": 0.50,
    "HOLD": 0.0,
    "TRIM": -0.10,
    "SELL": -0.30,
}


def load_oracle_decisions() -> dict[str, list[dict]]:
    """读 v3 trainset，按 symbol 分组。

    v3 trainset 每个 (symbol, decision_date) 仍然有 5 条（5 种 portfolio_state），
    但 verdict 不同（path c oracle 依赖 starting state）。

    为了对齐 v2/v2.1/v2.2 backtest 协议（"oracle 从 cash=100% 起开始 paper trade"），
    我们只取 cash=100% 那条。但这会丢掉中间状态下 oracle 的真实"如果你已经持有 X%
    应该怎么做"的指导。

    备选方案：每天都用当前 simulated asset_pct 去 trainset 里找最匹配那条。
    本脚本选 **简单方案（cash=100% slice）** 保持与历史 backtest 协议一致，
    可比性优先。
    """
    samples = json.loads(TRAINSET.read_text())
    by_symbol: dict[str, dict[str, dict]] = defaultdict(dict)
    for s in samples:
        if "cash 100%" not in s["portfolio_state"]:
            continue
        by_symbol[s["symbol"]][s["decision_date"]] = s

    out: dict[str, list[dict]] = {}
    for sym, by_date in by_symbol.items():
        out[sym] = sorted(by_date.values(), key=lambda r: r["decision_date"])
    return out


def fetch_prices(symbol: str, start: str, end: str) -> pd.Series:
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
    """对单 symbol 跑 paper trading（path c mapping）。"""
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

    if first_traded_date is None:
        return {"daily_values": [], "transactions": [], "verdict_counts": dict(verdict_counts),
                "final_value": START_CAPITAL, "first_traded_date": None,
                "first_traded_price": None}

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


def load_alphas_from(path: Path) -> dict[str, float]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    return {sym: r["alpha_pct"] for sym, r in data.get("results_per_symbol", {}).items()}


def main() -> None:
    print(f"[backtest v3] loading trainset from {TRAINSET}")
    by_symbol = load_oracle_decisions()
    symbols = sorted(by_symbol.keys())
    print(f"[backtest v3] {len(symbols)} symbols: {symbols}")
    print(f"[backtest v3] alloc mapping: {ALLOC_MAPPING}")

    v2_alphas = load_alphas_from(V2_RESULTS)
    v21_alphas = load_alphas_from(V21_RESULTS)
    v22_alphas = load_alphas_from(V22_RESULTS)

    def _avg(d: dict[str, float]) -> float | None:
        if not d:
            return None
        return round(float(np.mean(list(d.values()))), 4)

    v2_avg = _avg(v2_alphas)
    v21_avg = _avg(v21_alphas)
    v22_avg = _avg(v22_alphas)

    results_per_symbol = {}
    alphas = []
    wins = 0
    losses = 0

    for sym in symbols:
        decisions = by_symbol[sym]
        print(f"[backtest v3] {sym}: {len(decisions)} decision_dates  fetching prices ...")
        try:
            close = fetch_prices(sym, START_DATE, END_DATE)
        except Exception as e:
            print(f"[backtest v3] {sym}: FAILED to fetch prices: {e}")
            continue

        sim = simulate_oracle(decisions, close)
        if not sim["daily_values"]:
            print(f"[backtest v3] {sym}: empty daily_values, skipping")
            continue

        bah = simulate_buy_and_hold(close, sim["first_traded_date"], sim["first_traded_price"])
        oracle_metrics = compute_metrics(sim["daily_values"])
        bah_metrics = compute_metrics(bah)
        alpha = round(oracle_metrics["cum_return"] - bah_metrics["cum_return"], 4)

        if alpha >= 0:
            wins += 1
        else:
            losses += 1
        alphas.append(alpha)

        results_per_symbol[sym] = {
            "n_decisions": len(decisions),
            "oracle_v3": oracle_metrics,
            "buy_and_hold": bah_metrics,
            "alpha_pct": alpha,
            "v2_alpha_pct": v2_alphas.get(sym),
            "v2.1_alpha_pct": v21_alphas.get(sym),
            "v2.2_alpha_pct": v22_alphas.get(sym),
            "alpha_improvement_vs_v2.2_pp": round(alpha - v22_alphas[sym], 4) if sym in v22_alphas else None,
            "verdict_distribution": sim["verdict_counts"],
            "n_transactions": len(sim["transactions"]),
            "final_value_oracle": round(sim["final_value"], 2),
            "final_value_bah": round(bah[-1][1] if bah else START_CAPITAL, 2),
            "period_start": sim["first_traded_date"],
            "period_end": sim["daily_values"][-1][0],
        }

    avg_alpha = round(float(np.mean(alphas)), 4) if alphas else 0.0
    median_alpha = round(float(np.median(alphas)), 4) if alphas else 0.0

    improvement_vs_v22 = (
        round(avg_alpha - v22_avg, 4) if v22_avg is not None else None
    )

    n_total = len(alphas)
    all_non_negative = (losses == 0)

    # 判定
    if avg_alpha >= 1.0 and all_non_negative:
        verdict = "PATH_C_BIG_WIN"
        conclusion = (
            f"path c BIG WIN: avg alpha {avg_alpha:+.2f}% >= +1% 且 {wins}/{n_total} "
            f"symbol 全部 alpha >= 0。建议立刻用 v3 trainset 训练 oracle / DSPy。"
        )
    elif avg_alpha >= 0:
        verdict = "PATH_C_PARITY"
        conclusion = (
            f"path c PARITY: avg alpha {avg_alpha:+.2f}% (0% 到 +1% 区间)，"
            f"{wins}/{n_total} 赢 BAH。符合 per-sample optimal in-sample = BAH ceiling 的理论预期，"
            f"path c 设计正确；想跑赢 BAH 还需要更激进的 alloc 或更多 sample"
            f"（如对每条 sample 5 种 starting state 全部参与训练而非只取 cash=100% slice）。"
        )
    else:
        verdict = "PATH_C_BUG"
        conclusion = (
            f"path c FAILED: avg alpha {avg_alpha:+.2f}% < 0%，但 oracle 是 per-sample optimal "
            f"原则上不应跑输 BAH。需要 debug：可能是 cash=100% slice 抛弃了中间状态指导，"
            f"或 forward-30d 单步 horizon 跟回测连续仓位演化脱节。"
        )

    out = {
        "period": f"{START_DATE} to {END_DATE}",
        "starting_capital": START_CAPITAL,
        "n_symbols": len(results_per_symbol),
        "alloc_mapping": ALLOC_MAPPING,
        "labeling_design": "path c — oracle 选使 forward-30d terminal value 最大的 verdict (per-sample optimum)",
        "results_per_symbol": results_per_symbol,
        "average_alpha_vs_bah": avg_alpha,
        "median_alpha_vs_bah": median_alpha,
        "symbols_where_oracle_beats_bah": wins,
        "symbols_where_oracle_loses_bah": losses,
        "comparison_four_versions": {
            "v2_avg_alpha":  v2_avg,
            "v2.1_avg_alpha": v21_avg,
            "v2.2_avg_alpha": v22_avg,
            "v3_avg_alpha":   avg_alpha,
            "improvement_v3_vs_v2.2_pp": improvement_vs_v22,
        },
        "path_c_verdict": verdict,
        "conclusion": conclusion,
    }

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"\n[backtest v3] wrote {OUTPUT}")

    # ≤25 行 human-readable 四版对比表
    print("\n" + "=" * 88)
    print(f"Oracle v2 vs v2.1 vs v2.2 vs v3 (path c)  ({START_DATE} -> {END_DATE})  start ${START_CAPITAL:,.0f}")
    print("v2  : BUY 50%  / ACC 10% / TRIM -15% / SELL -50%   (threshold labeling)")
    print("v2.1: BUY 100% / ACC 30% / TRIM -10% / SELL -30%   (threshold labeling)")
    print("v2.2: BUY 100% / ACC 50% / TRIM -10% / SELL -30%   (threshold labeling)")
    print("v3  : SAME alloc as v2.2,                          path c alpha-based labeling")
    print("=" * 88)
    print(f"{'symbol':<10}{'bah':>9}{'v2':>9}{'v2.1':>9}{'v2.2':>9}{'v3':>9}{'Δ_v2.2':>10}")
    print("-" * 88)
    for sym, r in sorted(results_per_symbol.items(), key=lambda kv: -kv[1]["alpha_pct"]):
        b = r["buy_and_hold"]["cum_return"]
        va = r.get("v2_alpha_pct")
        v1a = r.get("v2.1_alpha_pct")
        v2a = r.get("v2.2_alpha_pct")
        v3a = r["alpha_pct"]
        d22 = r["alpha_improvement_vs_v2.2_pp"]
        va_s  = f"{va:>+7.2f}%"  if va  is not None else "    n/a"
        v1a_s = f"{v1a:>+7.2f}%" if v1a is not None else "    n/a"
        v2a_s = f"{v2a:>+7.2f}%" if v2a is not None else "    n/a"
        d22_s = f"{d22:>+8.2f}"  if d22 is not None else "     n/a"
        print(f"{sym:<10}{b:>+7.2f}%{va_s}{v1a_s}{v2a_s}{v3a:>+7.2f}%{d22_s}")
    print("-" * 88)
    v2_s  = f"{v2_avg:+.2f}%"  if v2_avg  is not None else "n/a"
    v21_s = f"{v21_avg:+.2f}%" if v21_avg is not None else "n/a"
    v22_s = f"{v22_avg:+.2f}%" if v22_avg is not None else "n/a"
    print(f"Avg alpha   v2: {v2_s}   v2.1: {v21_s}   v2.2: {v22_s}   v3: {avg_alpha:+.2f}%")
    if improvement_vs_v22 is not None:
        print(f"Δ v3 vs v2.2: {improvement_vs_v22:+.2f}pp   |   v3 beats BAH: {wins}/{n_total}   median: {median_alpha:+.2f}%")
    print(f"\nVerdict: {verdict}")
    print(conclusion)


if __name__ == "__main__":
    main()
