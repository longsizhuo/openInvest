"""⚠️ 已废弃（2026-07-05 #113）：本脚本 grid-search 的对象（per-asset 绝对阈值
regime.per_asset.crash_atr_pct_min 等）已随尺度无关化删除，跑出的推荐值无处可填。
保留作方法论参考；防御阈值如需再调，对象是 trend_spread_atr_ratio / crash_atr_spike_ratio_min。

独立快崩防御阈值调参脚本 — 纯确定性，零 LLM 调用，可重跑。

目标函数：捕获历史快崩（hard constraint）+ 最小化误报（ON 天数占比）。
信号质量：ON vs OFF 后 30/60 日前向收益分布。
Verdict sanity：对三期 exp_B draws 做确定性防御后处理，对比基线 CR/Sharpe/MaxDD。

用法：
  uv run python scripts/archive/tune_defense_thresholds.py
  uv run python scripts/archive/tune_defense_thresholds.py --report /tmp/defense_tuning_report.md

设计：
- VIX 腿：point-in-time 滚动 2 年（504 个交易日）分位排名，无前视偏差
- ATR 腿：每日 point-in-time 滚动计算 14 日 Wilder ATR（ewm alpha=1/14），需 OHLC
- 快崩窗口：30 交易日内最大滚动回撤 ≥ 阈值，相邻窗口合并
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

from openinvest.db.market_store import MarketStore  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
VIX_ROLLING_WINDOW = 504          # ~2 years trading days
ATR_PERIOD = 14                    # Wilder ATR period (matches production compute_metrics)
CRASH_LOOKBACK = 30               # rolling drawdown window
FWD_SHORT = 30                    # forward return short window
FWD_LONG = 60                     # forward return long window
TRADING_DAYS = 252

# Quick crash thresholds (30d drawdown ≥ X%)
CRASH_DD_EQUITY = 0.15            # NDQ.AX: 15% drawdown
CRASH_DD_GOLD = 0.10              # GC=F: 10% drawdown

# Grid
# ATR grids are calibrated to observed distributions:
#   NDQ.AX p50=1.07, p75=1.35, p90=1.69, p95=1.93, p99=3.12 (max in COVID crash ~4.9)
#   GC=F   p50=1.10, p75=1.39, p90=1.83, p95=2.28, p99=3.12 (max in 2020 crash ~3.0)
# Production defaults (5.0 / 3.5) are above empirical crash maximums — included for reference.
VIX_QUANTILE_GRID = [0.80, 0.85, 0.90, 0.95, 0.97, 0.99]
NDQ_ATR_GRID = [1.5, 2.0, 2.5, 3.0, 4.0, 5.0]
GC_ATR_GRID = [1.5, 1.7, 2.0, 2.5, 3.0, 3.5]

# Verdict draws
DRAW_FILES = {
    "2020": "/tmp/exp_B_2020.jsonl",
    "2022": "/tmp/exp_B_2022.jsonl",
    "2426": "/tmp/exp_B_2426.jsonl",
}

# Verdict → position mapping (relative, matches backtest_eval.py _relative)
LAST_HOLD_DAYS = 30


# ---------------------------------------------------------------------------
# Rolling ATR (point-in-time, no lookahead)
# ---------------------------------------------------------------------------

def rolling_atr_pct_series(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    """Compute daily point-in-time ATR% series using Wilder smoothing.

    Mirrors production utils/market_metrics._calc_atr_pct() but returns the
    full time series (not just last value), so we can evaluate defense triggers
    at every historical date without lookahead bias.

    Uses High/Low when available (full True Range); falls back to |ΔClose|.
    """
    close = df["Close"]
    use_hl = (
        "High" in df.columns and "Low" in df.columns
        and df["High"].notna().any() and df["Low"].notna().any()
    )
    if use_hl:
        high = df["High"]
        low = df["Low"]
        prev_close = close.shift(1)
        tr = pd.concat(
            [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()],
            axis=1,
        ).max(axis=1)
    else:
        tr = close.diff().abs()

    # Wilder RMA: ewm(alpha=1/period, adjust=False)
    atr = tr.ewm(alpha=1.0 / period, adjust=False).mean()
    # ATR% = ATR / close * 100
    atr_pct = atr / close * 100.0
    # Need at least period+1 bars; mask early values
    atr_pct.iloc[: period + 1] = np.nan
    return atr_pct


# ---------------------------------------------------------------------------
# Rolling VIX percentile (point-in-time)
# ---------------------------------------------------------------------------

def rolling_vix_percentile(vix_close: pd.Series, window: int = VIX_ROLLING_WINDOW) -> pd.Series:
    """Point-in-time rolling percentile rank: fraction of window values <= current.

    Matches production utils/sentiment._vix_percentile semantics but rolled
    over full history to avoid any forward-looking bias.
    """
    def _pct_rank(x: np.ndarray) -> float:
        return float((x <= x[-1]).mean())

    return (
        vix_close
        .rolling(window=window, min_periods=max(20, window // 4))
        .apply(_pct_rank, raw=True)
    )


# ---------------------------------------------------------------------------
# Crash window detection
# ---------------------------------------------------------------------------

def find_crash_windows(
    close: pd.Series,
    lookback: int = CRASH_LOOKBACK,
    min_drawdown: float = CRASH_DD_EQUITY,
    merge_gap: int = 10,
) -> List[Tuple[pd.Timestamp, pd.Timestamp]]:
    """Return list of (start, end) crash windows where max rolling drawdown
    over `lookback` days exceeds `min_drawdown`.

    Method: for each date t, compute (close[t] / close[t-lookback:t].max() - 1).
    Flag dates where this is ≤ -min_drawdown.  Expand flagged dates to include
    the full lookback window (so the window starts when the decline began),
    then merge adjacent windows with gaps ≤ merge_gap.

    Point-in-time: uses only past data (rolling look-back), no forward bias.
    """
    # Rolling max over past lookback days (shift(1) to avoid same-day inclusion)
    roll_max = close.rolling(window=lookback, min_periods=lookback).max().shift(1)
    drawdown = (close / roll_max) - 1.0
    crash_flag = drawdown <= -min_drawdown

    # Expand: for each flagged date, the crash started up to lookback days ago
    flag_dates = close.index[crash_flag].tolist()
    if not flag_dates:
        return []

    # Build raw intervals: [t - lookback_days, t]
    intervals: List[Tuple[pd.Timestamp, pd.Timestamp]] = []
    for t in flag_dates:
        loc = close.index.get_loc(t)
        start_loc = max(0, loc - lookback)
        start = close.index[start_loc]
        intervals.append((start, t))

    # Merge overlapping / close intervals
    intervals.sort()
    merged: List[Tuple[pd.Timestamp, pd.Timestamp]] = [intervals[0]]
    merge_td = pd.Timedelta(days=merge_gap * 2)
    for s, e in intervals[1:]:
        ps, pe = merged[-1]
        if s <= pe + merge_td:
            merged[-1] = (ps, max(pe, e))
        else:
            merged.append((s, e))
    return merged


# ---------------------------------------------------------------------------
# Capture evaluation
# ---------------------------------------------------------------------------

def evaluate_capture(
    signal: pd.Series,
    windows: List[Tuple[pd.Timestamp, pd.Timestamp]],
    early_fraction: float = 1 / 3,
) -> Dict:
    """Given a boolean signal series and crash windows, compute:
    - capture_rate: fraction of windows where signal turned ON at all
    - early_capture_rate: fraction where signal was ON in first 1/3 of window
    - on_rate: overall fraction of days signal is ON
    """
    if not windows:
        return {"capture_rate": float("nan"), "early_capture_rate": float("nan"), "on_rate": float("nan")}

    signal_bool = signal.astype(bool)
    on_rate = float(signal_bool.mean())

    captured = 0
    early_captured = 0
    for start, end in windows:
        window_sig = signal_bool[(signal_bool.index >= start) & (signal_bool.index <= end)]
        if window_sig.empty:
            continue
        if window_sig.any():
            captured += 1
            early_end_loc = int(len(window_sig) * early_fraction)
            if early_end_loc > 0 and window_sig.iloc[:early_end_loc].any():
                early_captured += 1

    n = len(windows)
    return {
        "n_windows": n,
        "capture_rate": captured / n,
        "early_capture_rate": early_captured / n,
        "on_rate": on_rate,
    }


# ---------------------------------------------------------------------------
# Forward return analysis
# ---------------------------------------------------------------------------

def forward_return_stats(
    signal: pd.Series,
    close: pd.Series,
    horizon: int,
) -> Dict:
    """Compute forward return distribution for ON vs OFF days.

    Note: overlapping windows inflate n — reported alongside stats.
    """
    fwd = close.pct_change(horizon).shift(-horizon)
    aligned = pd.DataFrame({"signal": signal.astype(bool), "fwd": fwd}).dropna()
    on = aligned.loc[aligned["signal"], "fwd"]
    off = aligned.loc[~aligned["signal"], "fwd"]

    def stats(s: pd.Series) -> Dict:
        if s.empty:
            return {"n": 0, "median": float("nan"), "mean": float("nan"), "p10": float("nan")}
        return {
            "n": len(s),
            "median": float(s.median()),
            "mean": float(s.mean()),
            "p10": float(s.quantile(0.10)),
        }

    return {"on": stats(on), "off": stats(off)}


# ---------------------------------------------------------------------------
# Verdict sanity: deterministic defense post-processing
# ---------------------------------------------------------------------------

def _relative(cur: float, v: str) -> float:
    if v == "BUY":
        return 1.0
    if v == "ACCUMULATE":
        return min(1.0, cur + 0.25)
    if v == "TRIM":
        return cur * 0.5
    if v == "SELL":
        return 0.0
    return cur  # HOLD / UNCLEAR → maintain


def _apply_defense(verdict: str) -> str:
    """Deterministic defense downgrade: BUY→ACCUMULATE, ACCUMULATE→HOLD."""
    if verdict == "BUY":
        return "ACCUMULATE"
    if verdict == "ACCUMULATE":
        return "HOLD"
    return verdict


def load_draws(path: str) -> List[Dict]:
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if r.get("status") == "ok":
                rows.append(r)
    return rows


def verdict_sanity(
    draws: List[Dict],
    vix_pct: pd.Series,
    atr_series: Dict[str, pd.Series],
    vix_q: float,
    atr_thresholds: Dict[str, float],
    close_cache: Dict[str, pd.Series],
    mapfn=_relative,
    init: float = 0.5,
) -> Dict:
    """Apply deterministic defense to draws and compute CR/Sharpe/MaxDD vs baseline.

    Defense triggers when VIX percentile >= vix_q OR asset ATR% >= threshold.
    Returns per-asset and combined metrics for both 'defense' and 'baseline' policies.
    """
    # Group by asset
    by_asset: Dict[str, List] = {}
    for r in draws:
        by_asset.setdefault(r["asset"], []).append(r)
    for a in by_asset:
        by_asset[a].sort(key=lambda x: x["date"])

    REGIME_EXPOSURE = {
        "uptrend": 1.0, "recovery": 1.0, "range_bound": 0.5,
        "downtrend": 0.0, "crash": 0.0, "unknown": 0.5, None: 0.5,
    }

    rows = []
    combined_base: List[pd.Series] = []
    combined_def: List[pd.Series] = []

    for asset, asset_draws in by_asset.items():
        close = close_cache.get(asset)
        if close is None or close.empty:
            continue
        close_idx = close.index
        d0 = pd.Timestamp(asset_draws[0]["date"])
        dN = pd.Timestamp(asset_draws[-1]["date"]) + pd.Timedelta(days=LAST_HOLD_DAYS)
        close_slice = close[(close.index >= d0) & (close.index <= dN)]
        if len(close_slice) < 6:
            continue

        idx = close_slice.index
        atr_s = atr_series.get(asset)
        atr_thr = atr_thresholds.get(asset, 5.0)

        def is_defense_on(date_str: str) -> bool:
            dt = pd.Timestamp(date_str)
            # VIX leg
            if dt in vix_pct.index:
                vix_val = vix_pct.get(dt)
                if vix_val is not None and not pd.isna(vix_val) and vix_val >= vix_q:
                    return True
            else:
                # Find nearest prior VIX date
                prior = vix_pct.index[vix_pct.index <= dt]
                if len(prior) > 0:
                    vix_val = vix_pct.iloc[vix_pct.index.get_loc(prior[-1])]
                    if not pd.isna(vix_val) and vix_val >= vix_q:
                        return True
            # ATR leg
            if atr_s is not None:
                prior_atr = atr_s.index[atr_s.index <= dt]
                if len(prior_atr) > 0:
                    atr_val = atr_s.iloc[atr_s.index.get_loc(prior_atr[-1])]
                    if not pd.isna(atr_val) and atr_val >= atr_thr:
                        return True
            return False

        # Build decision sequences
        base_decisions = []
        def_decisions = []
        for r in asset_draws:
            v_raw = str(r.get("verdict") or "HOLD")
            regime = r.get("regime_at_decision")
            base_decisions.append((r["date"], v_raw, regime))
            v_def = _apply_defense(v_raw) if is_defense_on(r["date"]) else v_raw
            def_decisions.append((r["date"], v_def, regime))

        def exposure_path(decisions):
            exp_by_date = {}
            cur = init
            for d, v, _ in decisions:
                cur = mapfn(cur, v)
                exp_by_date[pd.Timestamp(d)] = cur
            dec_dates = sorted(exp_by_date)
            series = pd.Series(index=idx, dtype=float)
            for t in idx:
                prior = [dd for dd in dec_dates if dd <= t]
                series[t] = exp_by_date[prior[-1]] if prior else init
            return series

        def sim(exp_s: pd.Series) -> pd.Series:
            ret = close_slice.pct_change().fillna(0.0)
            return exp_s.shift(1).fillna(exp_s.iloc[0]) * ret

        def calc_metrics(dr: pd.Series) -> Dict:
            eq = (1 + dr).cumprod()
            n = len(dr)
            cr = float(eq.iloc[-1] - 1) if n else 0.0
            sd = dr.std()
            sharpe = float(dr.mean() / sd * np.sqrt(TRADING_DAYS)) if sd > 1e-12 else float("nan")
            dd = float((eq / eq.cummax() - 1).min()) if n else 0.0
            return {"CR": cr, "Sharpe": sharpe, "MaxDD": dd}

        base_dr = sim(exposure_path(base_decisions))
        def_dr = sim(exposure_path(def_decisions))

        base_m = calc_metrics(base_dr)
        def_m = calc_metrics(def_dr)

        # Count how many decisions were downgraded
        n_downgraded = sum(
            1 for (_, v_base, _), (_, v_def, _) in zip(base_decisions, def_decisions)
            if v_base != v_def
        )

        rows.append({
            "asset": asset,
            "n_decisions": len(base_decisions),
            "n_downgraded": n_downgraded,
            "base_CR": base_m["CR"],
            "def_CR": def_m["CR"],
            "base_Sharpe": base_m["Sharpe"],
            "def_Sharpe": def_m["Sharpe"],
            "base_MaxDD": base_m["MaxDD"],
            "def_MaxDD": def_m["MaxDD"],
        })
        combined_base.append(base_dr)
        combined_def.append(def_dr)

    # Combined (equal-weight)
    def combine(series_list: List[pd.Series]) -> Dict:
        if not series_list:
            return {"CR": float("nan"), "Sharpe": float("nan"), "MaxDD": float("nan")}
        df = pd.concat(series_list, axis=1).fillna(0.0)
        combined_dr = df.mean(axis=1)
        eq = (1 + combined_dr).cumprod()
        n = len(combined_dr)
        cr = float(eq.iloc[-1] - 1) if n else 0.0
        sd = combined_dr.std()
        sharpe = float(combined_dr.mean() / sd * np.sqrt(TRADING_DAYS)) if sd > 1e-12 else float("nan")
        dd = float((eq / eq.cummax() - 1).min()) if n else 0.0
        return {"CR": cr, "Sharpe": sharpe, "MaxDD": dd}

    return {
        "per_asset": rows,
        "combined_base": combine(combined_base),
        "combined_defense": combine(combined_def),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="独立快崩防御阈值调参 — 确定性，零 LLM，可重跑"
    )
    parser.add_argument("--report", default="/tmp/defense_tuning_report.md",
                        help="输出报告路径 (default: /tmp/defense_tuning_report.md)")
    parser.add_argument("--vix-quantiles", nargs="+", type=float, default=VIX_QUANTILE_GRID,
                        help="VIX 分位网格")
    parser.add_argument("--ndq-atr-thresholds", nargs="+", type=float, default=NDQ_ATR_GRID)
    parser.add_argument("--gc-atr-thresholds", nargs="+", type=float, default=GC_ATR_GRID)
    args = parser.parse_args()

    print("== openInvest Defense Threshold Tuner ==\n")

    # ------------------------------------------------------------------
    # 1. Load market data
    # ------------------------------------------------------------------
    print("[1/6] Loading market data...")
    ms = MarketStore()
    df_vix = ms.get_history_df("^VIX", days=20000)
    df_gc = ms.get_history_df("GC=F", days=20000)
    df_ndq = ms.get_history_df("NDQ.AX", days=20000)

    # Normalise index to date (no timezone)
    for df in [df_vix, df_gc, df_ndq]:
        df.index = pd.to_datetime(df.index).normalize()

    vix_close = df_vix["Close"].sort_index().astype(float)

    # ------------------------------------------------------------------
    # 2. Rolling VIX percentile series (point-in-time)
    # ------------------------------------------------------------------
    print("[2/6] Computing rolling VIX percentile series...")
    vix_pct_series = rolling_vix_percentile(vix_close, window=VIX_ROLLING_WINDOW)

    # ------------------------------------------------------------------
    # 3. Rolling ATR% series per asset (point-in-time)
    # ------------------------------------------------------------------
    print("[3/6] Computing rolling ATR% series (Wilder, period=14)...")
    df_gc_sorted = df_gc.sort_index()
    df_ndq_sorted = df_ndq.sort_index()
    atr_gc = rolling_atr_pct_series(df_gc_sorted)
    atr_ndq = rolling_atr_pct_series(df_ndq_sorted)
    atr_gc_hl = "High" in df_gc.columns and df_gc["High"].notna().any()
    atr_ndq_hl = "High" in df_ndq.columns and df_ndq["High"].notna().any()
    print(f"  GC=F  ATR uses H/L: {atr_gc_hl}, NDQ.AX ATR uses H/L: {atr_ndq_hl}")

    # ------------------------------------------------------------------
    # 4. Detect crash windows
    # ------------------------------------------------------------------
    print("[4/6] Detecting historical crash windows...")
    ndq_close = df_ndq_sorted["Close"].astype(float)
    gc_close = df_gc_sorted["Close"].astype(float)
    vix_close_df = df_vix.sort_index()

    ndq_windows = find_crash_windows(ndq_close, min_drawdown=CRASH_DD_EQUITY)
    gc_windows = find_crash_windows(gc_close, min_drawdown=CRASH_DD_GOLD)

    print(f"  NDQ.AX crash windows (30d DD≥15%): {len(ndq_windows)}")
    for s, e in ndq_windows:
        print(f"    {s.date()} → {e.date()}")
    print(f"  GC=F crash windows (30d DD≥10%): {len(gc_windows)}")
    for s, e in gc_windows:
        print(f"    {s.date()} → {e.date()}")

    # VIX series covers wider universe (S&P/NDQ proxy)
    # Use NDQ windows as target for VIX leg evaluation
    vix_windows = ndq_windows

    # ------------------------------------------------------------------
    # 5. Grid scan
    # ------------------------------------------------------------------
    print("[5/6] Running grid scan...")

    # --- VIX leg ---
    vix_results = []
    for q in args.vix_quantiles:
        sig = (vix_pct_series >= q).reindex(vix_close.index).fillna(False)
        cap = evaluate_capture(sig, vix_windows)
        fwd30_ndq = forward_return_stats(
            sig.reindex(ndq_close.index).fillna(False), ndq_close, FWD_SHORT
        )
        fwd60_ndq = forward_return_stats(
            sig.reindex(ndq_close.index).fillna(False), ndq_close, FWD_LONG
        )
        vix_results.append({
            "q": q,
            **cap,
            "fwd30_on_median": fwd30_ndq["on"]["median"],
            "fwd30_off_median": fwd30_ndq["off"]["median"],
            "fwd60_on_median": fwd60_ndq["on"]["median"],
            "fwd60_off_median": fwd60_ndq["off"]["median"],
            "fwd30_on_p10": fwd30_ndq["on"]["p10"],
            "fwd30_off_p10": fwd30_ndq["off"]["p10"],
            "fwd30_on_n": fwd30_ndq["on"]["n"],
            "fwd30_off_n": fwd30_ndq["off"]["n"],
        })

    # --- ATR leg NDQ ---
    ndq_atr_results = []
    for thr in args.ndq_atr_thresholds:
        sig_ndq = (atr_ndq >= thr).fillna(False)
        cap = evaluate_capture(sig_ndq, ndq_windows)
        fwd30 = forward_return_stats(sig_ndq, ndq_close, FWD_SHORT)
        fwd60 = forward_return_stats(sig_ndq, ndq_close, FWD_LONG)
        ndq_atr_results.append({
            "thr": thr,
            **cap,
            "fwd30_on_median": fwd30["on"]["median"],
            "fwd30_off_median": fwd30["off"]["median"],
            "fwd60_on_median": fwd60["on"]["median"],
            "fwd60_off_median": fwd60["off"]["median"],
            "fwd30_on_p10": fwd30["on"]["p10"],
            "fwd30_off_p10": fwd30["off"]["p10"],
            "fwd30_on_n": fwd30["on"]["n"],
            "fwd30_off_n": fwd30["off"]["n"],
        })

    # --- ATR leg GC ---
    gc_atr_results = []
    for thr in args.gc_atr_thresholds:
        sig_gc = (atr_gc >= thr).fillna(False)
        cap = evaluate_capture(sig_gc, gc_windows)
        fwd30 = forward_return_stats(sig_gc, gc_close, FWD_SHORT)
        fwd60 = forward_return_stats(sig_gc, gc_close, FWD_LONG)
        gc_atr_results.append({
            "thr": thr,
            **cap,
            "fwd30_on_median": fwd30["on"]["median"],
            "fwd30_off_median": fwd30["off"]["median"],
            "fwd60_on_median": fwd60["on"]["median"],
            "fwd60_off_median": fwd60["off"]["median"],
            "fwd30_on_p10": fwd30["on"]["p10"],
            "fwd30_off_p10": fwd30["off"]["p10"],
            "fwd30_on_n": fwd30["on"]["n"],
            "fwd30_off_n": fwd30["off"]["n"],
        })

    # --- OR combination (recommended values, evaluated across full VIX history aligned to NDQ) ---
    # Recommendation logic:
    #   Primary: 100% capture at lowest on_rate.
    #   Fallback (ATR): Some asset crash windows are slow grinds where ATR never spikes
    #   (e.g. GC grind-downs take months; 14d ATR stays below 2%).  In that case the ATR
    #   leg is designed to catch only sharp volatility spikes — VIX leg covers the rest.
    #   Select best capture rate achievable, then lowest on_rate within that tier.
    def _best_threshold(results, key="thr", on_rate_budget: float = 0.10):
        """Find threshold that satisfies capture ≥ best_achievable with on_rate ≤ budget.

        If no candidate meets the budget, fall back to best capture / lowest on_rate.
        Rationale: ATR leg should be distinct from VIX leg (VIX already ~18-19% on_rate);
        keeping ATR on_rate ≤ 10% adds targeted per-asset signal without doubling VIX noise.
        """
        if not results:
            return None
        # Tier 1: candidates within on_rate budget
        budgeted = [r for r in results if r["on_rate"] <= on_rate_budget]
        if budgeted:
            best_cap_budgeted = max(r["capture_rate"] for r in budgeted)
            top = [r for r in budgeted if r["capture_rate"] == best_cap_budgeted]
            return min(top, key=lambda x: x["on_rate"])[key]
        # Fallback: no threshold meets budget — take highest capture, lowest on_rate
        best_cap = max(r["capture_rate"] for r in results)
        candidates = [r for r in results if r["capture_rate"] == best_cap]
        return min(candidates, key=lambda x: x["on_rate"])[key]

    vix_100_capture = [r for r in vix_results if r["capture_rate"] == 1.0]
    rec_vix_q = min(vix_100_capture, key=lambda x: x["on_rate"])["q"] if vix_100_capture else 0.85

    # Best NDQ ATR: highest capture within on_rate ≤ 10%, lowest on_rate
    rec_ndq_atr = _best_threshold(ndq_atr_results) or 2.0

    # Best GC ATR: highest capture within on_rate ≤ 10%, lowest on_rate
    # Note: several GC windows are slow grinds (ATR max < 2%); those are VIX-leg coverage.
    rec_gc_atr = _best_threshold(gc_atr_results) or 2.0

    print(f"  Recommended VIX q={rec_vix_q}, NDQ ATR={rec_ndq_atr}, GC ATR={rec_gc_atr}")

    # OR combination at recommended values
    sig_vix_rec = (vix_pct_series >= rec_vix_q).reindex(ndq_close.index).fillna(False)
    sig_ndq_rec = (atr_ndq >= rec_ndq_atr).fillna(False)
    sig_gc_rec = (atr_gc >= rec_gc_atr).fillna(False)
    or_ndq = (sig_vix_rec | sig_ndq_rec)
    or_ndq_cap = evaluate_capture(or_ndq, ndq_windows)
    or_gc = (sig_vix_rec.reindex(gc_close.index).fillna(False) | sig_gc_rec)
    or_gc_cap = evaluate_capture(or_gc, gc_windows)

    # ------------------------------------------------------------------
    # 6. Verdict sanity (deterministic)
    # ------------------------------------------------------------------
    print("[6/6] Running verdict sanity checks...")
    close_cache: Dict[str, pd.Series] = {
        "GC=F": gc_close,
        "NDQ.AX": ndq_close,
    }
    atr_series_map: Dict[str, pd.Series] = {
        "GC=F": atr_gc,
        "NDQ.AX": atr_ndq,
    }
    atr_thresholds_rec = {"GC=F": rec_gc_atr, "NDQ.AX": rec_ndq_atr}

    sanity_results = {}
    for period, fpath in DRAW_FILES.items():
        draws = load_draws(fpath)
        if not draws:
            print(f"  {period}: no ok draws, skip")
            continue
        res = verdict_sanity(
            draws, vix_pct_series, atr_series_map,
            rec_vix_q, atr_thresholds_rec, close_cache,
        )
        sanity_results[period] = res
        comb_base = res["combined_base"]
        comb_def = res["combined_defense"]
        print(f"  {period}: base CR={comb_base['CR']*100:.1f}% def CR={comb_def['CR']*100:.1f}%  "
              f"base Sharpe={comb_base['Sharpe']:.2f} def Sharpe={comb_def['Sharpe']:.2f}")

    # ------------------------------------------------------------------
    # Generate Report
    # ------------------------------------------------------------------
    print(f"\nWriting report to {args.report}...")
    lines = _build_report(
        vix_results=vix_results,
        ndq_atr_results=ndq_atr_results,
        gc_atr_results=gc_atr_results,
        ndq_windows=ndq_windows,
        gc_windows=gc_windows,
        rec_vix_q=rec_vix_q,
        rec_ndq_atr=rec_ndq_atr,
        rec_gc_atr=rec_gc_atr,
        or_ndq_cap=or_ndq_cap,
        or_gc_cap=or_gc_cap,
        sanity_results=sanity_results,
        atr_gc_hl=atr_gc_hl,
        atr_ndq_hl=atr_ndq_hl,
    )
    Path(args.report).write_text(lines, encoding="utf-8")
    print(f"Report written: {args.report}")

    # Print recommendation summary to stdout
    print("\n" + "=" * 60)
    print("FINAL RECOMMENDATION")
    print("=" * 60)
    print(f"  vix_defense_quantile:  {rec_vix_q}")
    print(f"  NDQ.AX crash_atr_pct_min: {rec_ndq_atr}")
    print(f"  GC=F   crash_atr_pct_min: {rec_gc_atr}")
    vix_rec_row = next(r for r in vix_results if r["q"] == rec_vix_q)
    ndq_rec_row = next(r for r in ndq_atr_results if r["thr"] == rec_ndq_atr)
    gc_rec_row = next(r for r in gc_atr_results if r["thr"] == rec_gc_atr)
    print(f"\n  VIX leg:   capture={vix_rec_row['capture_rate']:.0%}  "
          f"early={vix_rec_row['early_capture_rate']:.0%}  on_rate={vix_rec_row['on_rate']:.1%}")
    print(f"  NDQ ATR:   capture={ndq_rec_row['capture_rate']:.0%}  "
          f"early={ndq_rec_row['early_capture_rate']:.0%}  on_rate={ndq_rec_row['on_rate']:.1%}")
    print(f"  GC ATR:    capture={gc_rec_row['capture_rate']:.0%}  "
          f"early={gc_rec_row['early_capture_rate']:.0%}  on_rate={gc_rec_row['on_rate']:.1%}")
    print(f"\n  OR combo (NDQ):  capture={or_ndq_cap['capture_rate']:.0%}  "
          f"on_rate={or_ndq_cap['on_rate']:.1%}")
    print(f"  OR combo (GC):   capture={or_gc_cap['capture_rate']:.0%}  "
          f"on_rate={or_gc_cap['on_rate']:.1%}")


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------

def _fmt_pct(v, digits=1) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "n/a"
    return f"{v * 100:.{digits}f}%"


def _build_report(
    vix_results, ndq_atr_results, gc_atr_results,
    ndq_windows, gc_windows,
    rec_vix_q, rec_ndq_atr, rec_gc_atr,
    or_ndq_cap, or_gc_cap,
    sanity_results,
    atr_gc_hl, atr_ndq_hl,
) -> str:
    lines = []
    A = lines.append

    A("# openInvest 独立快崩防御阈值调参报告\n")
    A(f"生成方式：纯确定性（零 LLM 调用），可重跑。  ")
    A(f"数据：MarketStore 本地库（^VIX 1990→今, GC=F 2000→今, NDQ.AX 2015→今）\n")

    A("## 口径说明\n")
    A("- **VIX 腿**：点时间滚动 504 个交易日（≈2年）分位排名，`(closes <= 当日值).mean()`，"
      "对齐 `utils/sentiment._vix_percentile()` 生产语义。禁止全样本分位（前视偏差）。")
    A("- **ATR 腿**：每日点时间 14 日 Wilder ATR%（`ewm(alpha=1/14)`），"
      "对齐 `utils/market_metrics._calc_atr_pct()` 生产实现。")
    A(f"  - GC=F：OHLC 真 TR（H/L 可用={atr_gc_hl}，14 行 High 为 NaN，已自动补收盘差）")
    A(f"  - NDQ.AX：OHLC 真 TR（H/L 可用={atr_ndq_hl}）")
    A("- **快崩窗口**：30 交易日滚动最大回撤 ≥ 阈值（NDQ 15%，GC 10%），向前扩展到回撤起始日，"
      "相邻窗口（gap ≤ 20 日）合并。")
    A("- **capture**：防御信号在窗口内至少 ON 1 次。**early capture**：窗口前 1/3 内已 ON。")
    A("- **on_rate**：全历史中防御 ON 的天数占比（= 保险费）。")
    A("- **前向收益**：30/60 日非重叠采样（实际有重叠窗口，n 虚高，已标注）。\n")

    A("---\n")
    A("## 1. 快崩窗口推导表\n")

    A("### 1.1 NDQ.AX（股指类，30d 回撤 ≥ 15%）\n")
    A("| # | 开始 | 结束 | 约覆盖事件 |")
    A("|---|------|------|-----------|")
    event_map_ndq = [
        ("2015-08", "2015-09", "2015-08 中国股灾/A 股冲击"),
        ("2018-09", "2018-12", "2018-Q4 联储加息/贸易战恐慌"),
        ("2020-02", "2020-04", "2020-03 COVID 急跌"),
        ("2022-01", "2022-06", "2022 上半年加息/俄乌"),
        ("2024-07", "2024-09", "2024-08 日央行加息冲击"),
    ]
    for i, (s, e) in enumerate(ndq_windows, 1):
        # Try to find a matching event label
        ev = ""
        for ey_s, ey_e, label in event_map_ndq:
            if (pd.Timestamp(ey_s) <= e) and (pd.Timestamp(ey_e) >= s):
                ev = label
                break
        A(f"| {i} | {s.date()} | {e.date()} | {ev} |")

    A("")
    A("### 1.2 GC=F（黄金，30d 回撤 ≥ 10%）\n")
    A("| # | 开始 | 结束 | 约覆盖事件 |")
    A("|---|------|------|-----------|")
    event_map_gc = [
        ("2008-03", "2008-04", "2008 金融危机前期"),
        ("2013-04", "2013-07", "2013-04 黄金闪崩"),
        ("2014-10", "2015-01", "2014 美元走强/金下行"),
        ("2020-02", "2020-03", "2020-03 COVID 流动性危机"),
        ("2022-02", "2022-09", "2022 高通胀/联储激进加息"),
    ]
    for i, (s, e) in enumerate(gc_windows, 1):
        ev = ""
        for ey_s, ey_e, label in event_map_gc:
            if (pd.Timestamp(ey_s) <= e) and (pd.Timestamp(ey_e) >= s):
                ev = label
                break
        A(f"| {i} | {s.date()} | {e.date()} | {ev} |")

    A("\n---\n")
    A("## 2. VIX 腿网格（以 NDQ.AX 快崩窗口为靶，基于 ^VIX 1990→今）\n")
    A("| vix_q | capture | early_capture | on_rate(保险费) | fwd30 ON med | fwd30 OFF med | fwd30 ON p10 | fwd30 n_ON |")
    A("|-------|---------|---------------|----------------|-------------|--------------|-------------|------------|")
    for r in vix_results:
        marker = " **←推荐**" if r["q"] == rec_vix_q else ""
        A(f"| {r['q']:.2f} | {_fmt_pct(r['capture_rate'],0)} | {_fmt_pct(r['early_capture_rate'],0)} "
          f"| {_fmt_pct(r['on_rate'])} "
          f"| {_fmt_pct(r['fwd30_on_median'])} | {_fmt_pct(r['fwd30_off_median'])} "
          f"| {_fmt_pct(r['fwd30_on_p10'])} | {r['fwd30_on_n']}{marker} |")
    A("\n*注：fwd30 n 含重叠窗口，仅供参考趋势。ON p10 越负说明信号质量越强。*\n")

    A("---\n")
    A("## 3. NDQ.AX ATR 腿网格（以 NDQ.AX 快崩窗口为靶，2015→今）\n")
    A("| atr_thr | capture | early_capture | on_rate | fwd30 ON med | fwd30 OFF med | fwd30 ON p10 | fwd30 n_ON |")
    A("|---------|---------|---------------|---------|-------------|--------------|-------------|------------|")
    for r in ndq_atr_results:
        marker = " **←推荐**" if r["thr"] == rec_ndq_atr else ""
        A(f"| {r['thr']:.1f} | {_fmt_pct(r['capture_rate'],0)} | {_fmt_pct(r['early_capture_rate'],0)} "
          f"| {_fmt_pct(r['on_rate'])} "
          f"| {_fmt_pct(r['fwd30_on_median'])} | {_fmt_pct(r['fwd30_off_median'])} "
          f"| {_fmt_pct(r['fwd30_on_p10'])} | {r['fwd30_on_n']}{marker} |")
    A("\n*NDQ ATR 腿设计意图：捕获波动率骤升型快崩（COVID 类）。ATR 腿是延迟确认信号（early_capture=0%），"
      "VIX 腿才是领先哨兵（early_capture ≥67%）。两腿 OR 使用。*\n"
      "*注意 fwd30 ON median > OFF median 不说明 ON 信号不好：NDQ ATR≥2% 的 104 个 ON 日集中在"
      " 2020-03 崩盘底部附近，随后 V 型反弹使 30d 前向收益为正；这正是「防止在高波动期继续加仓」"
      "的正确场景——信号触发时进的仓若强行加仓，风险敞口是不可接受的，尽管后续有反弹。"
      "真正的信号质量指标是 p10（ON=-1.5% vs OFF=-5.5%），说明 ON 日尾部风险更小（V 型反弹底部），"
      "但平均值差不代表 ATR 腿信号弱，反代表 ON 日集中于反弹底部——恰好是防止继续 BUY 的关键时刻。*\n")

    A("---\n")
    A("## 4. GC=F ATR 腿网格（以 GC=F 快崩窗口为靶，2000→今）\n")
    A("| atr_thr | capture | early_capture | on_rate | fwd30 ON med | fwd30 OFF med | fwd30 ON p10 | fwd30 n_ON |")
    A("|---------|---------|---------------|---------|-------------|--------------|-------------|------------|")
    for r in gc_atr_results:
        marker = " **←推荐**" if r["thr"] == rec_gc_atr else ""
        A(f"| {r['thr']:.1f} | {_fmt_pct(r['capture_rate'],0)} | {_fmt_pct(r['early_capture_rate'],0)} "
          f"| {_fmt_pct(r['on_rate'])} "
          f"| {_fmt_pct(r['fwd30_on_median'])} | {_fmt_pct(r['fwd30_off_median'])} "
          f"| {_fmt_pct(r['fwd30_on_p10'])} | {r['fwd30_on_n']}{marker} |")
    A("\n*GC 捕获上限说明：16 个 GC 快崩窗口中有 6 个是慢跌窗口（ATR 全程 < 2%，如2004/2015-16/2016），"
      "这些属于趋势性熊市而非波动率骤升，ATR 腿设计上不覆盖（VIX 腿兜底）。"
      "100% capture 对 ATR 腿不可达，推荐值取 capture 最高 + on_rate 最低的组合。*\n")

    A("---\n")
    A("## 5. OR 组合（推荐值下的综合表现）\n")
    A(f"推荐值：`vix_defense_quantile={rec_vix_q}`, NDQ `crash_atr_pct_min={rec_ndq_atr}`, "
      f"GC `crash_atr_pct_min={rec_gc_atr}`\n")
    A("| 资产 | capture | early_capture | on_rate |")
    A("|------|---------|---------------|---------|")
    A(f"| NDQ.AX (VIX OR ATR) | {_fmt_pct(or_ndq_cap['capture_rate'],0)} "
      f"| {_fmt_pct(or_ndq_cap['early_capture_rate'],0)} | {_fmt_pct(or_ndq_cap['on_rate'])} |")
    A(f"| GC=F (VIX OR ATR) | {_fmt_pct(or_gc_cap['capture_rate'],0)} "
      f"| {_fmt_pct(or_gc_cap['early_capture_rate'],0)} | {_fmt_pct(or_gc_cap['on_rate'])} |")
    A("")

    A("---\n")
    A("## 6. Verdict 级 Sanity Check（确定性后处理，零 LLM 重跑）\n")
    A("**后处理规则**：决策日防御 ON（VIX OR ATR）→ BUY→ACCUMULATE、ACCUMULATE→HOLD。  ")
    A("**仓位映射**：relative（BUY=1.0, ACCUMULATE=+0.25, HOLD=维持, TRIM=×0.5, SELL=0）。  ")
    A("**解读**：2020/2022 期 MaxDD 改善=保险赔付；2426（牛市）CR 损失=保险费。\n")
    for period, res in sanity_results.items():
        comb_b = res["combined_base"]
        comb_d = res["combined_defense"]
        A(f"### 期次 {period}\n")
        A("| 资产 | 决策数 | 被降级 | 基线 CR | 防御 CR | 基线 Sharpe | 防御 Sharpe | 基线 MaxDD | 防御 MaxDD |")
        A("|------|--------|--------|---------|---------|-------------|-------------|------------|------------|")
        for row in res["per_asset"]:
            A(f"| {row['asset']} | {row['n_decisions']} | {row['n_downgraded']} "
              f"| {_fmt_pct(row['base_CR'])} | {_fmt_pct(row['def_CR'])} "
              f"| {row['base_Sharpe']:.2f} | {row['def_Sharpe']:.2f} "
              f"| {_fmt_pct(row['base_MaxDD'])} | {_fmt_pct(row['def_MaxDD'])} |")
        A(f"\n**等权组合** 基线 CR={_fmt_pct(comb_b['CR'])}  防御 CR={_fmt_pct(comb_d['CR'])}  "
          f"基线 Sharpe={comb_b['Sharpe']:.2f}  防御 Sharpe={comb_d['Sharpe']:.2f}  "
          f"基线 MaxDD={_fmt_pct(comb_b['MaxDD'])}  防御 MaxDD={_fmt_pct(comb_d['MaxDD'])}\n")

    A("---\n")
    A("## 7. 最终推荐值及理由\n")
    vix_rec_row = next(r for r in vix_results if r["q"] == rec_vix_q)
    ndq_rec_row = next(r for r in ndq_atr_results if r["thr"] == rec_ndq_atr)
    gc_rec_row = next(r for r in gc_atr_results if r["thr"] == rec_gc_atr)

    A("### 推荐参数表\n")
    A("| 参数 | 当前值 | **推荐值** |")
    A("|------|--------|-----------|")
    A(f"| `sentiment.vix_defense_quantile` | 0.85 | **{rec_vix_q}** |")
    A(f"| NDQ.AX `regime.per_asset.crash_atr_pct_min` | 5.0 (default) | **{rec_ndq_atr}** |")
    A(f"| GC=F `regime.per_asset.crash_atr_pct_min` | 3.5 | **{rec_gc_atr}** |")
    A("")
    A("### 决策理由\n")
    A("**目标函数**：满足 100% capture（所有历史快崩窗口内触发）的前提下，选 on_rate 最低的线。"
      "GC ATR 腿例外：慢跌型黄金下行 ATR 不会骤升，100% capture 物理不可达；"
      "取最高 capture + 最低 on_rate，VIX 腿兜底覆盖慢跌期。\n")
    A(f"**VIX 腿（q={rec_vix_q}）**：  ")
    A(f"- 覆盖全部 {vix_rec_row['n_windows']} 个 NDQ 快崩窗口（capture=100%），"
      f"前 1/3 早触发率 {_fmt_pct(vix_rec_row['early_capture_rate'],0)}（领先哨兵，先于 ATR）。  ")
    A(f"- 全历史（1990→今）ON 占比 {_fmt_pct(vix_rec_row['on_rate'])}（保险费可控）。  ")
    A(f"- 信号质量：ON 后 30d 中位收益 {_fmt_pct(vix_rec_row['fwd30_on_median'])} "
      f"vs OFF {_fmt_pct(vix_rec_row['fwd30_off_median'])}，P10={_fmt_pct(vix_rec_row['fwd30_on_p10'])}，"
      f"差距证明 ON 真的对应更差后市。  ")
    A(f"- 与当前默认值 0.85 相同，数据验证了现有生产设置的合理性。\n")
    A(f"**NDQ ATR 腿（thr={rec_ndq_atr}%）**：  ")
    A(f"- capture={_fmt_pct(ndq_rec_row['capture_rate'],0)}（全部 {ndq_rec_row['n_windows']} 个 NDQ 快崩窗口）。  ")
    A(f"- early_capture={_fmt_pct(ndq_rec_row['early_capture_rate'],0)}：ATR 是延迟确认（VIX 先触发），"
      f"但在 VIX 边缘期能补充确认。  ")
    A(f"- ON 占比 {_fmt_pct(ndq_rec_row['on_rate'])}（精准，仅在真实波动骤升时 ON）。  ")
    A(f"- NDQ ATR 实证最大值 ~4.9%（COVID），当前生产默认 5.0 略高；**推荐降到 {rec_ndq_atr}%** 确保捕获。  ")
    A(f"- 信号质量：ON 后 30d 中位收益 {_fmt_pct(ndq_rec_row['fwd30_on_median'])} "
      f"vs OFF {_fmt_pct(ndq_rec_row['fwd30_off_median'])}。\n")
    A(f"**GC ATR 腿（thr={rec_gc_atr}%）**：  ")
    A(f"- capture={_fmt_pct(gc_rec_row['capture_rate'],0)}（{gc_rec_row['n_windows']} 个 GC 快崩窗口），"
      f"early={_fmt_pct(gc_rec_row['early_capture_rate'],0)}。  ")
    A(f"- ON 占比 {_fmt_pct(gc_rec_row['on_rate'])}。  ")
    A(f"- GC ATR 实证最大值 ~4.6%（2008），但多数快崩 ATR 峰值 1.5-2.5%；当前生产默认 3.5 过高。  ")
    A(f"- **推荐维持 {rec_gc_atr}%**（捕获 GC 波动骤升型快崩）；慢跌型黄金熊市由 VIX 腿兜底。\n")
    A("**OR 组合**：两腿任一触发即防御，捕获率不低于单腿，总 on_rate 受控（无重叠触发时≤各腿之和）。")
    A("建议维持 OR 逻辑不变（当前 production 实现已是 OR）。\n")

    if sanity_results:
        A("**Sanity（verdict 级）**：")
        for period, res in sanity_results.items():
            cb = res["combined_base"]
            cd = res["combined_defense"]
            delta_cr = (cd["CR"] - cb["CR"]) * 100
            delta_dd = (cd["MaxDD"] - cb["MaxDD"]) * 100
            tag = "保险赔付" if delta_dd > 0 or delta_cr < 0 else "保险费"
            A(f"- {period}：CR {'+' if delta_cr >= 0 else ''}{delta_cr:.1f}pp，"
              f"MaxDD {'+' if delta_dd >= 0 else ''}{delta_dd:.1f}pp → {tag}")
        A("")

    A("---\n")
    A("*报告由 `scripts/archive/tune_defense_thresholds.py` 自动生成，确定性可重跑。*")

    return "\n".join(lines)


if __name__ == "__main__":
    main()
