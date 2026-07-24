"""build_dspy_trainset_v5 — path_v4 计算式仓位打标的训练教材(ADR-007 重开线)

与 v2/v3 的差异:
- 标签:path_v4(均值-方差 f*=μ/(γσ²) + 成本楔子 + 显著性闸 κ),γ=10/κ=0.25/c=0.003
  (experiments/oracle_redesign/ 标定,五档全活、HOLD≈34% 贴近 live)。每条样本同时
  记录 verdict(语言层)与 target_exposure(计算层)——教材教"词",执行学"数"。
- 数据源:MarketStore 本地缓存(768 标的全历史,零 yfinance 请求;macro 两只除外)。
- 覆盖:与委员会语料同窗(2025-01-02→2026-04-19),每 8 交易日 × 每点随机 2 个
  起始仓位状态 ≈ 6 万级样本;(date,symbol) 可与 memory/.backtest 的委员会 transcript
  逐条 join。
- 输出:experiments/dspy_trainset_v5.jsonl(gitignored,同 v2/v3 惯例)+ .stats.json。

用法:
    uv run python -m scripts.research.build_dspy_trainset_v5
"""
from __future__ import annotations

import json
import random
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.research.build_dspy_trainset_v2 import (  # noqa: E402
    PORTFOLIO_STATES, SOLVENCY_BUFFERS,
    _compute_forward_window, _compute_market_context, _fetch_macro,
    _format_macro_context, _format_portfolio_state,
)

DECISION_START = pd.Timestamp("2025-01-02")
DECISION_END = pd.Timestamp("2026-04-19")
SAMPLE_EVERY_N = 8
STATES_PER_POINT = 2
CUTOFF = "2025-05-31"          # 与 review_calc.CONTAMINATION_CUTOFF 同值,样本携带分桶标记
GAMMA, KAPPA, COST = 10.0, 0.25, 0.003
TRAIL_DAYS, FWD_DAYS = 60, 21
EPS = 1e-9

OUT = ROOT / "experiments" / "dspy_trainset_v5.jsonl"
STATS = ROOT / "experiments" / "dspy_trainset_v5.stats.json"


def path_v4(a0: float, fwd: float, sigma30: float) -> tuple:
    """(verdict, target_exposure)。canonical 定义与推导:
    experiments/oracle_redesign/{calibrate_gamma.py, REPORT.md}(γ/κ 标定见 calibration.json)"""
    var = sigma30 * sigma30
    if var <= 0 or abs(fwd) <= KAPPA * sigma30:
        return "HOLD", a0
    f_star = fwd / (GAMMA * var)
    wedge = COST / (GAMMA * var)
    if abs(f_star - a0) <= wedge:
        return "HOLD", a0
    f_c = f_star - (wedge if f_star > a0 else -wedge)
    if f_c >= 1.0:
        return ("HOLD", a0) if a0 >= 1.0 - EPS else ("BUY", 1.0)
    if f_c <= 0.0:
        return ("HOLD", a0) if a0 <= EPS else ("SELL", 0.0)
    return ("ACCUMULATE", f_c) if f_c > a0 else ("TRIM", f_c)


def load_universe() -> List[str]:
    import yaml
    syms: List[str] = []
    for f in ("universe.yml", "universe_l2.yml", "universe_l3.yml", "universe_l4.yml"):
        p = ROOT / "experiments" / "paper_fleet" / f
        if p.exists():
            syms += yaml.safe_load(p.read_text(encoding="utf-8"))["symbols"]
    return list(dict.fromkeys(syms))


def main() -> None:
    from openinvest.db.market_store import MarketStore
    ms = MarketStore()
    rng = random.Random(42)
    macro = _fetch_macro((DECISION_START - pd.Timedelta(days=10)).strftime("%Y-%m-%d"),
                         (DECISION_END + pd.Timedelta(days=1)).strftime("%Y-%m-%d"))
    universe = load_universe()
    print(f"universe {len(universe)} 标的;窗口 {DECISION_START.date()}→{DECISION_END.date()}"
          f",每 {SAMPLE_EVERY_N} 交易日 × {STATES_PER_POINT} 状态")

    n_written = 0
    label_dist: Counter = Counter()
    bucket_dist: Counter = Counter()
    skipped: Dict[str, str] = {}
    with OUT.open("w", encoding="utf-8") as fh:
        for i, sym in enumerate(universe, 1):
            df = ms.get_history_df(sym, days=100000)
            if df is None or df.empty or "Close" not in df.columns or len(df) < 400:
                skipped[sym] = f"rows={0 if df is None else len(df)}"
                continue
            df = df[~df.index.duplicated(keep="first")].sort_index()
            closes = df["Close"]
            logret = np.diff(np.log(closes.to_numpy(dtype=float)))
            in_win = df[(df.index >= DECISION_START) & (df.index <= DECISION_END)]
            if in_win.empty:
                skipped[sym] = "no-trading-days-in-window"
                continue
            idxs = [df.index.get_loc(d) for d in in_win.index][::SAMPLE_EVERY_N]
            for t in idxs:
                if t < TRAIL_DAYS or t + FWD_DAYS >= len(closes):
                    continue
                date_str = df.index[t].strftime("%Y-%m-%d")
                m = macro[macro.index <= df.index[t]]
                macro_ctx = _format_macro_context(m.iloc[-1]) if not m.empty else None
                if macro_ctx is None:
                    continue
                market_ctx = _compute_market_context(df, t, sym)
                if market_ctx is None:
                    continue
                fwd30 = _compute_forward_window(closes, t, 30)
                if fwd30 is None:
                    continue
                ret30, sharpe30, mdd30 = fwd30
                fwd = float(closes.iloc[t + FWD_DAYS] / closes.iloc[t] - 1.0)
                sigma30 = float(np.std(logret[t - TRAIL_DAYS:t], ddof=1) * np.sqrt(FWD_DAYS))
                for (a0, cash, plo, phi) in rng.sample(PORTFOLIO_STATES, STATES_PER_POINT):
                    pnl = None if plo is None else rng.uniform(plo, phi)
                    verdict, target = path_v4(a0, fwd, sigma30)
                    bucket = "clean" if date_str > CUTOFF else "head"
                    row = {
                        "decision_date": date_str,
                        "symbol": sym,
                        "bucket": bucket,
                        "macro_context": macro_ctx,
                        "market_context": market_ctx,
                        "portfolio_state": _format_portfolio_state(sym, a0, cash, pnl,
                                                                   rng.choice(SOLVENCY_BUFFERS)),
                        "asset_pct": a0,
                        "verdict": verdict,
                        "target_exposure": round(float(target), 4),
                        "delta_exposure": round(float(target - a0), 4),
                        "sigma30": round(sigma30, 5),
                        "forward_30d_return_pct": ret30,
                        "forward_30d_mdd_pct": mdd30,
                        "oracle": f"path_v4(gamma={GAMMA},kappa={KAPPA},cost={COST})",
                    }
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                    n_written += 1
                    label_dist[verdict] += 1
                    bucket_dist[bucket] += 1
            if i % 100 == 0:
                print(f"  [{i}/{len(universe)}] 已写 {n_written}")

    dist_pct = {k: round(100 * v / max(1, n_written), 2) for k, v in label_dist.most_common()}
    stats = {
        "n_samples": n_written,
        "n_symbols": len(universe) - len(skipped),
        "skipped": skipped,
        "label_dist_pct": dist_pct,
        "bucket_dist": dict(bucket_dist),
        "all_five_alive": all(dist_pct.get(v, 0) >= 3.0
                              for v in ("BUY", "ACCUMULATE", "HOLD", "TRIM", "SELL")),
        "oracle": f"path_v4(gamma={GAMMA},kappa={KAPPA},cost={COST})",
        "window": f"{DECISION_START.date()}..{DECISION_END.date()}",
        "grid": f"every {SAMPLE_EVERY_N} trading days x {STATES_PER_POINT} random states",
    }
    STATS.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(stats | {"skipped": f"{len(skipped)} 个(见 stats 文件)"},
                     ensure_ascii=False, indent=2))
    print(f"→ {OUT}")


if __name__ == "__main__":
    main()
