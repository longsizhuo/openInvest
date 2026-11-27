"""Describe risk relative to the asset's own past; never choose an action."""
from __future__ import annotations

import math
import numpy as np

from openinvest.calc.market_metrics import _atr_pct_series
from openinvest.calc.risk_observations import return_risk_observations


def comparative_risk_context(history, *, reference_sessions=252, minimum_samples=60):
    """The last supplied bar is the observation, not part of its baseline.

    The caller owns the as-of/source qualification. Window lengths describe a
    reference sample; no percentile or ratio here authorizes BUY/HOLD/SELL.
    """
    result = {"schema": "comparative_risk_v1", "scope": "descriptive_not_forecast_or_trade_rule",
              "market_risk_label": None, "account_loss_tolerance": "not_inferred",
              "reference_policy": {"maximum_sessions": reference_sessions, "minimum_samples": minimum_samples, "tail_probability_for_description": .05}}
    if (history is None or history.empty or "Close" not in history
            or not history.index.is_monotonic_increasing or history.index.has_duplicates):
        return {**result, "status": "invalid_or_missing_history"}
    if (history.attrs.get('source_observation') or {}).get('history_basis_qualified') is False:
        return {**result, 'status': 'history_basis_unqualified'}
    try:
        current = float(history["Close"].iloc[-1])
    except (TypeError, ValueError):
        return {**result, "status": "invalid_current_price"}
    if not math.isfinite(current) or current <= 0:
        return {**result, "status": "invalid_current_price"}
    prior = history.iloc[:-1]
    if any(not math.isfinite(float(x)) or float(x) <= 0 for x in prior["Close"]):
        return {**result, "status": "invalid_price_history"}
    result.update(observation_bar=str(history.index[-1]),
                  reference_last_bar=str(prior.index[-1]) if len(prior) else None)
    result["returns"] = return_risk_observations(prior["Close"].tolist(), current,
        minimum_samples=minimum_samples, lookback=reference_sessions)
    has_range = 'High' in history and 'Low' in history
    current_range = has_range and history[['High','Low']].iloc[-1].notna().all()
    series = _atr_pct_series(history) if has_range else None
    atr = {"status": "insufficient_reference" if current_range else "current_ohlc_unavailable", "sample_count": 0}
    if series is not None:
        observed_ranges = history['High'].notna() & history['Low'].notna()
        baseline = series.iloc[:-1][observed_ranges.iloc[:-1]].replace([np.inf, -np.inf], np.nan).dropna().tail(reference_sessions)
        value = float(series.iloc[-1])
        atr["sample_count"] = len(baseline)
        if current_range and math.isfinite(value) and len(baseline) >= minimum_samples:
            median = float(np.median(baseline))
            atr = {"status": "observed", "current_pct": value,
                   "prior_median_pct": median, "sample_count": len(baseline),
                   "ratio_to_prior_median": value / median if median > 0 else None,
                   "historical_percentile": float((baseline <= value).mean()),
                   "forecast_probability": None}
    result["atr"] = atr
    result["status"] = "observed" if atr["status"] == "observed" or result["returns"]["status"] == "ok" else "insufficient_reference"
    return result
