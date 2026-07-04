"""backtest_oracle_v2.1.py — oracle verdict paper trading 回测（v2.1 alloc mapping）

V3 audit 诊断 v2 oracle 在 2024-05~2026-05 大牛市 10/10 全输 BAH（avg alpha -8.21%）。
诊断结论：oracle 永远到不了 full deployment + 偶尔 TRIM/SELL 错过反弹。

路径 a 假设：把 BUY 改成 100% cash 全部入场（高确定性窗口直接打满），
TRIM/SELL 减半，能否拉平或反超 BAH？

新 mapping vs 旧：
| verdict     | 旧 v2          | 新 v2.1            |
|-------------|----------------|--------------------|
| BUY         | 50% cash       | 100% cash          |
| ACCUMULATE  | 10% cash       | 30% cash           |
| HOLD        | 0              | 0                  |
| TRIM        | -15% holdings  | -10% holdings      |
| SELL        | -50% holdings  | -30% holdings      |

约束 / 实现细节与 v2 backtest 完全一致：严格 walk-forward / yfinance auto_adjust /
core.strategy_metrics / BAH = first decision_date 全仓持有到底。

输出：
- experiments/audits/v3_oracle_backtest_v2.1.json   (schema 与 v2 一致 + 新增 v2 对比段)
- stdout 打印 ≤30 行 v2 vs v2.1 对比表 + 判定
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
OUTPUT = ROOT / "experiments" / "audits" / "v3_oracle_backtest_v2.1.json"
V2_RESULTS = ROOT / "experiments" / "audits" / "v3_oracle_backtest.json"

START_CAPITAL = 100_000.0
START_DATE = "2024-05-01"
END_DATE = "2026-05-01"

# ===== v2.1 新 mapping =====
# verdict → (action, ratio) ratio 表示对 base（现金 or 持仓市值）的比例
VERDICT_ACTIONS = {
    "BUY":        ("buy",  1.00),   # 100% cash 全仓
    "ACCUMULATE": ("buy",  0.30),   # 30% cash
    "HOLD":       ("hold", 0.00),
    "TRIM":       ("sell", 0.10),   # 减仓 10%
    "SELL":       ("sell", 0.30),   # 减仓 30%
}

# 用于 JSON 输出
ALLOC_MAPPING = {
    "BUY": 1.0,
    "ACCUMULATE": 0.30,
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


def main() -> None:
    print(f"[backtest v2.1] loading trainset from {TRAINSET}")
    by_symbol = load_oracle_decisions()
    symbols = sorted(by_symbol.keys())
    print(f"[backtest v2.1] {len(symbols)} symbols: {symbols}")
    print(f"[backtest v2.1] alloc mapping: {ALLOC_MAPPING}")

    v2_alphas = load_v2_alphas()
    v2_avg_alpha = round(float(np.mean(list(v2_alphas.values()))), 4) if v2_alphas else None

    results_per_symbol = {}
    alphas = []
    wins = 0
    losses = 0

    for sym in symbols:
        decisions = by_symbol[sym]
        print(f"[backtest v2.1] {sym}: {len(decisions)} decision_dates  fetching prices ...")
        try:
            close = fetch_prices(sym, START_DATE, END_DATE)
        except Exception as e:
            print(f"[backtest v2.1] {sym}: FAILED to fetch prices: {e}")
            continue

        sim = simulate_oracle(decisions, close)
        if not sim["daily_values"]:
            print(f"[backtest v2.1] {sym}: empty daily_values, skipping")
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
            "oracle_v2.1": oracle_metrics,
            "buy_and_hold": bah_metrics,
            "alpha_pct": alpha,
            "v2_alpha_pct": v2_alphas.get(sym),
            "alpha_improvement_pp": round(alpha - v2_alphas[sym], 4) if sym in v2_alphas else None,
            "verdict_distribution": sim["verdict_counts"],
            "n_transactions": len(sim["transactions"]),
            "final_value_oracle": round(sim["final_value"], 2),
            "final_value_bah": round(bah[-1][1] if bah else START_CAPITAL, 2),
            "period_start": sim["first_traded_date"],
            "period_end": sim["daily_values"][-1][0],
        }

    avg_alpha = round(float(np.mean(alphas)), 4) if alphas else 0.0
    median_alpha = round(float(np.median(alphas)), 4) if alphas else 0.0

    improvement_pp = (
        round(avg_alpha - v2_avg_alpha, 4) if v2_avg_alpha is not None else None
    )

    # 验收判定
    if avg_alpha > 0:
        verdict = "PATH_A_SUCCESS"
        conclusion = (
            f"v2.1 mapping 跑赢 BAH +{avg_alpha:.2f}%（v2 是 {v2_avg_alpha:+.2f}%, "
            f"提升 {improvement_pp:+.2f}pp）。{wins}/{len(alphas)} 赢 BAH。"
            "路径 a 成功 → 可以用 v2.1 mapping 重 build trainset 再训 LLM。"
        )
    elif avg_alpha >= -1:
        verdict = "PATH_A_OK"
        conclusion = (
            f"v2.1 mapping 基本持平 BAH ({avg_alpha:+.2f}%, v2 是 {v2_avg_alpha:+.2f}%, "
            f"提升 {improvement_pp:+.2f}pp)。路径 a 可以接受 → "
            "重 build trainset，让 LLM 拟合这个上限，剩下的 gap 看能否再调 mapping 微调。"
        )
    elif avg_alpha >= -3:
        verdict = "PATH_A_MARGINAL"
        conclusion = (
            f"v2.1 mapping 仍输 BAH ({avg_alpha:+.2f}%, v2 是 {v2_avg_alpha:+.2f}%, "
            f"提升 {improvement_pp:+.2f}pp)。改善明显但未达持平。"
            "建议：先在此基础上加 bear leg（路径 b）或进一步降低 TRIM/SELL 系数。"
        )
    else:
        verdict = "PATH_A_INSUFFICIENT"
        conclusion = (
            f"v2.1 mapping 依然显著输 BAH ({avg_alpha:+.2f}%, v2 是 {v2_avg_alpha:+.2f}%, "
            f"提升 {improvement_pp:+.2f}pp)。路径 a 不够 → "
            "走路径 b（加 bear leg / 短线 timing 信号），或承认在牛市纯多策略无法跑赢 BAH。"
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
        "comparison_to_v2_old": {
            "v2_avg_alpha": v2_avg_alpha,
            "v2.1_avg_alpha": avg_alpha,
            "improvement_pp": improvement_pp,
        },
        "path_a_verdict": verdict,
        "conclusion": conclusion,
    }

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"\n[backtest v2.1] wrote {OUTPUT}")

    # ≤30 行 human-readable 对比表
    print("\n" + "=" * 78)
    print(f"Oracle v2.1 vs v2  ({START_DATE} -> {END_DATE})  starting ¥{START_CAPITAL:,.0f}")
    print("v2  : BUY 50% / ACC 10% / TRIM -15% / SELL -50%")
    print("v2.1: BUY 100% / ACC 30% / TRIM -10% / SELL -30%")
    print("=" * 78)
    print(f"{'symbol':<9}{'bah_cum':>9}{'v2_orcl':>9}{'v2_a':>8}{'v2.1_o':>9}{'v2.1_a':>9}{'delta_pp':>10}")
    print("-" * 78)
    for sym, r in sorted(results_per_symbol.items(), key=lambda kv: -kv[1]["alpha_pct"]):
        b = r["buy_and_hold"]["cum_return"]
        v21o = r["oracle_v2.1"]["cum_return"]
        v21a = r["alpha_pct"]
        v2a = r["v2_alpha_pct"]
        v2o = b + v2a if v2a is not None else None
        d = r["alpha_improvement_pp"]
        v2o_s = f"{v2o:>8.2f}%" if v2o is not None else "     n/a"
        v2a_s = f"{v2a:>+6.2f}%" if v2a is not None else "    n/a"
        d_s = f"{d:>+8.2f}" if d is not None else "     n/a"
        print(f"{sym:<9}{b:>8.2f}%{v2o_s}{v2a_s}{v21o:>8.2f}%{v21a:>+8.2f}%{d_s}")
    print("-" * 78)
    print(f"Avg alpha   v2: {v2_avg_alpha:+.2f}%   v2.1: {avg_alpha:+.2f}%   delta: {improvement_pp:+.2f}pp")
    print(f"Median      v2.1: {median_alpha:+.2f}%   |   v2.1 beats BAH: {wins}/{len(alphas)}")
    print(f"\nVerdict: {verdict}")
    print(f"{conclusion}")


if __name__ == "__main__":
    main()
