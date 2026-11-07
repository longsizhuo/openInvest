"""Describe unusual downside against past returns, independently of trade intent."""
from __future__ import annotations

import math
import numpy as np


def return_risk_observations(prior_closes, current_price, *, tail_probability=.05,
                             minimum_samples=60, lookback=252, horizons=(1, 5)) -> dict:
    """Caller supplies only completed sessions before the current session.

    The quantile is an alert-sensitivity policy; the price-change threshold adapts
    to the asset's past distribution. Neither a tail rank nor an alert predicts
    a further decline or authorizes a trade.
    """
    if not 0 < tail_probability < .5 or minimum_samples < 2 or lookback < minimum_samples:
        raise ValueError("invalid observation policy")
    prices = [float(x) for x in prior_closes]
    if (not math.isfinite(current_price) or current_price <= 0
            or any(not math.isfinite(x) or x <= 0 for x in prices)):
        return {"status": "invalid_price_history", "observations": []}
    observations = []
    for horizon in horizons:
        if horizon < 1 or len(prices) < horizon:
            observations.append({"horizon_sessions": horizon, "status": "insufficient_history"})
            continue
        baseline = [prices[i] / prices[i-horizon] - 1 for i in range(horizon, len(prices))][-lookback:]
        if len(baseline) < minimum_samples:
            observations.append({"horizon_sessions": horizon, "status": "insufficient_history", "sample_count": len(baseline)})
            continue
        change = current_price / prices[-horizon] - 1
        threshold = float(np.quantile(baseline, tail_probability))
        rank = sum(value <= change for value in baseline) / len(baseline)
        observations.append({"horizon_sessions": horizon, "status": "observed", "return_pct": change*100,
                             "reference_price": prices[-horizon], "threshold_pct": threshold*100,
                             "historical_tail_rank": rank, "sample_count": len(baseline),
                             "breached": change < 0 and change <= threshold,
                             "forecast_probability": None, "execution_authorized": False})
    return {"status": "ok" if any(x["status"] == "observed" for x in observations) else "insufficient_history",
            "observations": observations}
