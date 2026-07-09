"""v2 DSPy trainset 构造 — 从 yfinance 历史 + oracle labeling 直接构造（不依赖 backtest）

每个 (date, symbol) 采样 5 个 portfolio_state，结合宏观 + market metrics + forward window
P&L，按 oracle 规则打 verdict label。
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
from collections import Counter
from pathlib import Path
from statistics import mean, stdev
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import yfinance as yf

# 让脚本可以 from utils / core / ... import
ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openinvest.core.regime import format_regime_brief  # noqa: E402
from openinvest.utils.market_metrics import compute_metrics  # noqa: E402


DEFAULT_SYMBOLS = [
    "NDQ.AX", "GC=F", "^GSPC", "QQQ", "AAPL",
    "TSLA", "BTC-USD", "TLT", "IWM", "EEM",
]
DEFAULT_START = "2024-05-01"
DEFAULT_END = "2026-05-01"

# 算 MA120 + forward 90d 需要前后 buffer
PRE_BUFFER_DAYS = 130   # 前置 ~4.5 月（trading days ≥ 120）
POST_BUFFER_DAYS = 110  # 后置 ~3.5 月（trading days ≥ 90）

# 决策日按 4 个 trading day 采样一次（≈ 周中 + 周末交替），保证总量 ≥ 5000
SAMPLE_EVERY_N_TRADING_DAYS = 4

# 每个 (date, symbol) 采样 5 个 portfolio
PORTFOLIO_STATES = [
    # (asset_pct, cash_pct, pnl_low, pnl_high)
    (0.00, 1.00, None, None),
    (0.25, 0.75, -0.05, 0.10),
    (0.50, 0.50, -0.08, 0.15),
    (0.75, 0.25, -0.10, 0.20),
    (1.00, 0.00, -0.12, 0.25),
]

SOLVENCY_BUFFERS = ["strong", "weak", "unknown"]

MACRO_TICKERS = {
    "VIX": "^VIX",
    "TNX": "^TNX",
    "DXY": "DX-Y.NYB",
    "USDCNY": "USDCNY=X",
}
MACRO_FALLBACK = {
    "DXY": "DX=F",
}


def _safe_history(symbol: str, start: str, end: str, retries: int = 2) -> pd.DataFrame:
    """yfinance 拉历史，带 retry + sleep。失败返空 DF。"""
    for attempt in range(retries + 1):
        try:
            df = yf.Ticker(symbol).history(start=start, end=end, auto_adjust=True)
            if df is not None and not df.empty:
                # tz-aware → tz-naive，统一 index
                df.index = pd.to_datetime(df.index).tz_localize(None)
                return df
        except Exception as e:
            print(f"  ⚠ {symbol} attempt {attempt+1} 失败: {e}")
            time.sleep(2)
    return pd.DataFrame()


def _fetch_macro(start: str, end: str) -> pd.DataFrame:
    """拉 4 个宏观指标 + close 列，inner join 成 wide df（index=date）。"""
    series = {}
    for label, sym in MACRO_TICKERS.items():
        df = _safe_history(sym, start, end)
        if df.empty or "Close" not in df.columns:
            fb = MACRO_FALLBACK.get(label)
            if fb:
                print(f"  ⚠ macro {label} ({sym}) 空，尝试 fallback {fb}")
                df = _safe_history(fb, start, end)
        if df.empty or "Close" not in df.columns:
            print(f"  ❌ macro {label} 拉不到，整列将缺失")
            series[label] = pd.Series(dtype=float)
        else:
            series[label] = df["Close"]
        time.sleep(1)
    macro = pd.DataFrame(series)
    macro = macro.dropna(how="all")
    return macro


def _format_macro_context(row: pd.Series) -> Optional[str]:
    """row 形如 {VIX, TNX, DXY, USDCNY}。任一缺失返 None。"""
    needed = ["VIX", "TNX", "DXY"]
    for k in needed:
        if k not in row or pd.isna(row[k]):
            return None
    parts = [
        f"VIX {row['VIX']:.1f}",
        f"TNX {row['TNX']:.2f}",
        f"DXY {row['DXY']:.1f}",
    ]
    if "USDCNY" in row and not pd.isna(row["USDCNY"]):
        parts.append(f"USDCNY {row['USDCNY']:.2f}")
    return " / ".join(parts)


def _compute_market_context(
    df: pd.DataFrame, t_idx: int, symbol: str,
) -> Optional[str]:
    """t_idx 处的 market context 摘要。需要至少 120 条历史。"""
    if t_idx < 120:
        return None
    window = df.iloc[: t_idx + 1]
    metrics = compute_metrics(window)
    if metrics.get("ma120") is None or metrics.get("rsi14") is None:
        return None

    # 算 1m return（约 21 trading days）
    if t_idx >= 21:
        p_now = df["Close"].iloc[t_idx]
        p_1m = df["Close"].iloc[t_idx - 21]
        if p_1m > 0:
            ret_1m_pct = (p_now / p_1m - 1) * 100
        else:
            ret_1m_pct = 0.0
    else:
        ret_1m_pct = 0.0

    # regime brief (从 core.regime)
    regime_brief = format_regime_brief(metrics, symbol=symbol)
    # 只取 REGIME / REASON 一行简化
    regime_line = regime_brief.splitlines()[0] if regime_brief else "REGIME: unknown"

    return (
        f"{regime_line}\n"
        f"RSI {metrics['rsi14']:.1f}\n"
        f"MA20={metrics['ma20']:.2f} {'>' if metrics['ma20'] > metrics['ma120'] else '<'} "
        f"MA120={metrics['ma120']:.2f}\n"
        f"price_quantile_2y={metrics['price_quantile_2y']:.2f}\n"
        f"1m_return={ret_1m_pct:+.1f}%"
    )


def _format_portfolio_state(
    symbol: str, asset_pct: float, cash_pct: float,
    pnl_pct: Optional[float], solvency: str,
) -> str:
    if asset_pct == 0.0:
        return (
            f"cash {cash_pct*100:.0f}%, no {symbol} position, "
            f"solvency_buffer={solvency}"
        )
    concentration = asset_pct
    pnl_str = f"PnL_since_entry {pnl_pct*100:+.1f}%" if pnl_pct is not None else "PnL N/A"
    return (
        f"cash {cash_pct*100:.0f}%, {symbol} {asset_pct*100:.0f}% "
        f"(concentration {concentration*100:.0f}%, {pnl_str}), "
        f"solvency_buffer={solvency}"
    )


def _compute_forward_window(
    closes: pd.Series, t_idx: int, n_days: int,
) -> Optional[Tuple[float, float, float]]:
    """从 closes[t_idx] 到 closes[t_idx + n_days] 算 return / sharpe / mdd。

    return (return_pct, sharpe_annualized, mdd_pct) 都是百分比，mdd 负数。
    数据不足返 None。
    """
    end_idx = t_idx + n_days
    if end_idx >= len(closes):
        return None
    p_now = closes.iloc[t_idx]
    p_fwd = closes.iloc[end_idx]
    if p_now <= 0 or pd.isna(p_now) or pd.isna(p_fwd):
        return None

    ret_pct = (p_fwd / p_now - 1) * 100

    # daily returns over t+1 .. t+N
    forward_slice = closes.iloc[t_idx : end_idx + 1]
    daily_rets = forward_slice.pct_change().dropna()
    if len(daily_rets) < 2:
        return None
    std = daily_rets.std()
    if std == 0 or pd.isna(std):
        sharpe = 0.0
    else:
        sharpe = float(daily_rets.mean() / std * math.sqrt(252))

    # MDD on the forward path
    roll_max = forward_slice.cummax()
    drawdown = (forward_slice - roll_max) / roll_max
    mdd_pct = float(drawdown.min() * 100)  # 负数

    return (round(float(ret_pct), 3), round(sharpe, 3), round(mdd_pct, 3))


def oracle_verdict(
    asset_pct: float, fwd_30d_return: float, fwd_30d_mdd: float,
) -> Tuple[str, float]:
    """oracle labeling 规则（spec 内）。返回 (verdict, alloc_pct_of_dry_powder)。"""
    if fwd_30d_return >= 8 and fwd_30d_mdd >= -3 and asset_pct < 0.3:
        return ("BUY", 0.5)
    if fwd_30d_return >= 4 and asset_pct < 0.7:
        return ("ACCUMULATE", 0.10)
    if fwd_30d_return <= -8 and asset_pct >= 0.5:
        return ("SELL", -0.5)
    if fwd_30d_return <= -4 and asset_pct >= 0.3:
        return ("TRIM", -0.15)
    return ("HOLD", 0.0)


def _reward(sharpe_30d: float, mdd_30d_pct: float) -> float:
    """spec: forward_30d_sharpe - 1.0 * abs(forward_30d_mdd_pct/100)"""
    return round(sharpe_30d - 1.0 * abs(mdd_30d_pct / 100), 4)


def build_for_symbol(
    symbol: str,
    macro: pd.DataFrame,
    decision_start: pd.Timestamp,
    decision_end: pd.Timestamp,
    rng: random.Random,
) -> List[Dict[str, Any]]:
    """单个 symbol 跑一遍。"""
    # 前后 buffer 拉历史
    fetch_start = (decision_start - pd.Timedelta(days=PRE_BUFFER_DAYS + 60)).strftime("%Y-%m-%d")
    fetch_end = (decision_end + pd.Timedelta(days=POST_BUFFER_DAYS + 10)).strftime("%Y-%m-%d")
    df = _safe_history(symbol, fetch_start, fetch_end)
    time.sleep(1)
    if df.empty or "Close" not in df.columns:
        print(f"  ❌ {symbol} 价格数据空，跳过")
        return []

    df = df[~df.index.duplicated(keep="first")].sort_index()
    closes = df["Close"]

    samples: List[Dict[str, Any]] = []
    in_window = df[(df.index >= decision_start) & (df.index <= decision_end)]
    if in_window.empty:
        print(f"  ❌ {symbol} 决策窗口内无交易日")
        return []

    decision_indices = [df.index.get_loc(d) for d in in_window.index]
    sampled = decision_indices[::SAMPLE_EVERY_N_TRADING_DAYS]

    n_skip_macro = 0
    n_skip_fwd = 0
    n_skip_ctx = 0

    for t_idx in sampled:
        date = df.index[t_idx]
        date_str = date.strftime("%Y-%m-%d")

        # 1. macro context — 取最近 ≤ date 的一行
        macro_slice = macro[macro.index <= date]
        if macro_slice.empty:
            n_skip_macro += 1
            continue
        macro_row = macro_slice.iloc[-1]
        macro_ctx = _format_macro_context(macro_row)
        if macro_ctx is None:
            n_skip_macro += 1
            continue

        # 2. market context
        market_ctx = _compute_market_context(df, t_idx, symbol)
        if market_ctx is None:
            n_skip_ctx += 1
            continue

        # 3. forward windows
        fwd_30 = _compute_forward_window(closes, t_idx, 30)
        fwd_90 = _compute_forward_window(closes, t_idx, 90)
        if fwd_30 is None or fwd_90 is None:
            n_skip_fwd += 1
            continue
        ret_30, sharpe_30, mdd_30 = fwd_30
        ret_90, sharpe_90, mdd_90 = fwd_90

        # 4. 对每个 portfolio state 生成一条样本
        for (asset_pct, cash_pct, pnl_lo, pnl_hi) in PORTFOLIO_STATES:
            if pnl_lo is None:
                pnl = None
            else:
                pnl = rng.uniform(pnl_lo, pnl_hi)
            solvency = rng.choice(SOLVENCY_BUFFERS)
            verdict, alloc = oracle_verdict(asset_pct, ret_30, mdd_30)

            sample = {
                "decision_date": date_str,
                "symbol": symbol,
                "macro_context": macro_ctx,
                "market_context": market_ctx,
                "portfolio_state": _format_portfolio_state(
                    symbol, asset_pct, cash_pct, pnl, solvency,
                ),
                "verdict": verdict,
                "alloc_pct_of_dry_powder": round(float(alloc), 4),
                "forward_30d_return_pct": ret_30,
                "forward_30d_sharpe": sharpe_30,
                "forward_30d_mdd_pct": mdd_30,
                "forward_90d_return_pct": ret_90,
                "forward_90d_sharpe": sharpe_90,
                "forward_90d_mdd_pct": mdd_90,
                "reward": _reward(sharpe_30, mdd_30),
            }
            samples.append(sample)

    print(
        f"  ✓ {symbol}: {len(samples)} 样本 "
        f"(skip macro={n_skip_macro} ctx={n_skip_ctx} fwd={n_skip_fwd})"
    )
    return samples


def build(
    symbols: List[str],
    start: str,
    end: str,
    output: Path,
    seed: int = 42,
) -> List[Dict[str, Any]]:
    rng = random.Random(seed)
    decision_start = pd.Timestamp(start)
    decision_end = pd.Timestamp(end)

    # 拉宏观（覆盖整段决策窗口）
    macro_start = (decision_start - pd.Timedelta(days=15)).strftime("%Y-%m-%d")
    macro_end = (decision_end + pd.Timedelta(days=5)).strftime("%Y-%m-%d")
    print(f"📡 拉宏观 {macro_start} → {macro_end}")
    macro = _fetch_macro(macro_start, macro_end)
    if macro.empty:
        raise SystemExit("❌ macro 全空，无法继续")
    print(f"  macro {len(macro)} 行，cols={list(macro.columns)}")

    all_samples: List[Dict[str, Any]] = []
    for sym in symbols:
        print(f"📈 处理 {sym}")
        all_samples.extend(build_for_symbol(sym, macro, decision_start, decision_end, rng))

    # 统计
    if not all_samples:
        raise SystemExit("❌ 没有任何样本，检查 yfinance / macro 数据")

    verdict_dist = Counter(s["verdict"] for s in all_samples)
    symbol_dist = Counter(s["symbol"] for s in all_samples)
    rewards = [s["reward"] for s in all_samples]

    print(f"\n📊 总样本数: {len(all_samples)}")
    print("\nverdict 分布:")
    for v, n in sorted(verdict_dist.items(), key=lambda x: -x[1]):
        print(f"  {v:12s} {n:6d} ({n/len(all_samples)*100:.1f}%)")
    print("\nsymbol 分布:")
    for s, n in sorted(symbol_dist.items(), key=lambda x: -x[1]):
        print(f"  {s:12s} {n:6d} ({n/len(all_samples)*100:.1f}%)")
    print(
        f"\nreward: mean={mean(rewards):.4f} "
        f"std={stdev(rewards) if len(rewards) > 1 else 0:.4f} "
        f"min={min(rewards):.4f} max={max(rewards):.4f}"
    )

    # 落盘
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(all_samples, ensure_ascii=False, indent=2))
    print(f"\n💾 落 {output}")

    stats_path = output.with_suffix(".stats.json")
    stats = {
        "total_samples": len(all_samples),
        "verdict_distribution": dict(verdict_dist),
        "symbol_distribution": dict(symbol_dist),
        "reward_stats": {
            "mean": round(mean(rewards), 4),
            "std": round(stdev(rewards), 4) if len(rewards) > 1 else 0.0,
            "min": round(min(rewards), 4),
            "max": round(max(rewards), 4),
        },
        "config": {
            "symbols": symbols,
            "start": start,
            "end": end,
            "sample_every_n_trading_days": SAMPLE_EVERY_N_TRADING_DAYS,
            "portfolio_states_per_date": len(PORTFOLIO_STATES),
            "seed": seed,
        },
    }
    stats_path.write_text(json.dumps(stats, ensure_ascii=False, indent=2))
    print(f"💾 落 {stats_path}")

    # 验收检查
    warnings = []
    if len(all_samples) < 5000:
        warnings.append(f"总样本 {len(all_samples)} < 5000")
    for v in ("BUY", "ACCUMULATE", "HOLD", "TRIM", "SELL"):
        if verdict_dist.get(v, 0) < 50:
            warnings.append(f"verdict {v} 只有 {verdict_dist.get(v, 0)} 条 (< 50)")
    if warnings:
        print("\n⚠️ 验收 warning:")
        for w in warnings:
            print(f"  - {w}")
    else:
        print("\n✅ 验收通过")

    return all_samples


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--output", type=Path,
                   default=ROOT / "experiments" / "dspy_trainset_v2.json")
    p.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS),
                   help="逗号分隔 symbol 列表")
    p.add_argument("--start", default=DEFAULT_START)
    p.add_argument("--end", default=DEFAULT_END)
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args()

    syms = [s.strip() for s in args.symbols.split(",") if s.strip()]
    build(syms, args.start, args.end, args.output, seed=args.seed)


if __name__ == "__main__":
    main()
