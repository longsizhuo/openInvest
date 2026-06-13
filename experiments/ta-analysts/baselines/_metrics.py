"""共享回测指标（baselines 三/四臂共用，避免逐字复制漂移）。"""
from __future__ import annotations

import numpy as np
import pandas as pd


def metrics(daily_ret: pd.Series, in_market: pd.Series) -> dict:
    """日收益序列 → CAGR / 年化 Sharpe(rf=0) / MaxDD / 持仓占比。年化用 252。"""
    r = daily_ret.dropna()
    eq = (1 + r).cumprod()
    years = len(r) / 252
    cagr = eq.iloc[-1] ** (1 / years) - 1 if years > 0 else float("nan")
    sharpe = (r.mean() / r.std() * np.sqrt(252)) if r.std() > 0 else float("nan")
    mdd = (eq / eq.cummax() - 1).min()
    return {
        "cagr_pct": round(cagr * 100, 2),
        "sharpe": round(float(sharpe), 2),
        "max_dd_pct": round(float(mdd) * 100, 2),
        "time_in_market_pct": round(float(in_market.mean()) * 100, 1),
        "n_days": int(len(r)),
    }
