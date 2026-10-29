"""Offline scores for explicitly defined events; no portfolio or production imports.

The functions never infer a probability or a forecast event from an action,
``HOLD`` label, natural-language confidence, or the evaluation-period base rate.
Malformed rows fail the whole call rather than silently changing its cohort.
All time checks use caller-supplied, timezone-aware timestamps, never wall time.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timezone
import math
from numbers import Real
import random
from typing import Any


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite number, not a boolean or string")
    try:
        result = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    if value != value.strip():
        raise ValueError(f"{name} must not have surrounding whitespace")
    return value


def _required(row: Mapping[str, Any], name: str, context: str) -> Any:
    if name not in row:
        raise ValueError(f"{context}: missing required field {name}")
    return row[name]


def _timestamp(value: Any, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO timezone-aware timestamp string")
    try:
        result = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO timezone-aware timestamp") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone offset")
    return result.astimezone(timezone.utc)


def _mean(values: list[float], name: str) -> float:
    # Scaling before summing also avoids overflowing a sum of finite losses.
    result = math.fsum(value / len(values) for value in values)
    if not math.isfinite(result):
        raise ValueError(f"{name} is not finite")
    return result


def brier(p: float, y: bool | int) -> float:
    """Return (p-y)**2 for a binary event; smaller is better.

    This scalar helper accepts a boolean or numeric 0/1 label. The record API
    below deliberately requires JSON-style booleans to avoid label coercion.
    """
    probability = _finite(p, "probability")
    if not 0.0 <= probability <= 1.0:
        raise ValueError("probability must be between 0 and 1")
    if isinstance(y, bool):
        outcome = int(y)
    else:
        outcome = _finite(y, "binary outcome")
        if outcome not in (0.0, 1.0):
            raise ValueError("binary outcome must be 0 or 1")
    return (probability - outcome) ** 2


def interval_score(lower: float, upper: float, y: float, alpha: float = 0.2) -> float:
    """Winkler interval score for nominal coverage 1-alpha; smaller is better.

    The score is width + 2/alpha times the distance outside the interval.
    It penalizes both arbitrarily wide bands and missed outcomes. Units follow
    the caller's lower/upper/outcome units and must be held fixed across methods.
    """
    lo = _finite(lower, "lower")
    hi = _finite(upper, "upper")
    actual = _finite(y, "outcome")
    tail_mass = _finite(alpha, "alpha")
    if lo > hi:
        raise ValueError("lower must not exceed upper")
    if not 0.0 < tail_mass < 1.0:
        raise ValueError("alpha must be strictly between 0 and 1")
    result = hi - lo
    if actual < lo:
        result += (2.0 / tail_mass) * (lo - actual)
    elif actual > hi:
        result += (2.0 / tail_mass) * (actual - hi)
    if not math.isfinite(result):
        raise ValueError("interval score is not finite")
    return result


def score_records(
    records: Iterable[Mapping[str, Any]], *, event_id: str, horizon: str
) -> dict[str, Any]:
    """Score one caller-defined, mature binary event and horizon.

    Required fields on *every* record: record_id, event_id, horizon, probability, outcome
    (strict bool), forecast_at, information_available_at, outcome_at, evaluated_at.
    record_id is nonempty and unique within this event/horizon batch; repeated
    mature predictions are rejected rather than counted as new evidence.
    Timestamp fields are ISO timezone-aware strings. Information must be known
    by forecast_at; the outcome must occur after the forecast and have matured
    by evaluated_at. Events/horizons must exactly equal the caller's selection.

    A horizon is an opaque identifier: this function does not silently turn
    calendar days into trading days or choose a price/return convention. Those
    belong to the frozen event definition and label-producing adapter.
    The returned row mean is descriptive, not an independence-adjusted interval.
    """
    expected_event = _text(event_id, "event_id")
    expected_horizon = _text(horizon, "horizon")
    scores: list[float] = []
    forecast_dates: set[date] = set()
    seen_record_ids: set[str] = set()
    for index, row in enumerate(records):
        context = f"record {index}"
        if not isinstance(row, Mapping):
            raise ValueError(f"{context} must be a mapping")
        record_id = _text(_required(row, "record_id", context), f"{context}.record_id")
        if record_id in seen_record_ids:
            raise ValueError(f"{context}: duplicate record_id")
        seen_record_ids.add(record_id)
        if _required(row, "event_id", context) != expected_event:
            raise ValueError(f"{context}: event_id does not match selected event")
        if _required(row, "horizon", context) != expected_horizon:
            raise ValueError(f"{context}: horizon does not match selected horizon")
        actual = _required(row, "outcome", context)
        if type(actual) is not bool:
            raise ValueError(f"{context}: outcome must be a boolean")
        times = {
            key: _timestamp(_required(row, key, context), f"{context}.{key}")
            for key in (
                "forecast_at", "information_available_at", "outcome_at", "evaluated_at"
            )
        }
        if times["information_available_at"] > times["forecast_at"]:
            raise ValueError(f"{context}: information was unavailable at forecast time")
        if times["outcome_at"] <= times["forecast_at"]:
            raise ValueError(f"{context}: outcome must occur strictly after forecast")
        if times["outcome_at"] > times["evaluated_at"]:
            raise ValueError(f"{context}: outcome is not mature at evaluation time")
        scores.append(brier(_required(row, "probability", context), actual))
        forecast_dates.add(times["forecast_at"].date())
    if not scores:
        raise ValueError("records must contain at least one mature event")
    return {
        "event_id": expected_event,
        "horizon": expected_horizon,
        "n_records": len(scores),
        "unique_forecast_dates": len(forecast_dates),
        "mean_brier": _mean(scores, "mean_brier"),
        "total_brier": math.fsum(scores),
        "test_base_rate_used": False,
        "scope": "descriptive_row_mean; event and labels supplied by caller",
    }


def _integer(value: Any, name: str, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _quantile(sorted_values: list[float], q: float) -> float:
    position = (len(sorted_values) - 1) * q
    low = math.floor(position)
    high = math.ceil(position)
    fraction = position - low
    return sorted_values[low] * (1.0 - fraction) + sorted_values[high] * fraction


def paired_day_bootstrap(
    rows: Iterable[Mapping[str, Any]], *, block_days: int = 30,
    repetitions: int = 2000, seed: int = 20261029,
) -> dict[str, Any]:
    """Descriptive, paired circular moving-block bootstrap on observed dates.

    Each row supplies date (YYYY-MM-DD), entity_id, loss_a and loss_b. Duplicate
    (date, entity_id) pairs are rejected. Average paired A-minus-B losses equally
    over entities *within* each date, then equally over observed dates. Bootstrap
    contiguous blocks of sorted observed dates, shared across the entire panel;
    calendar gaps are not synthesized. Negative differences favor A.

    block_days counts OBSERVED dates, not calendar days or independent periods.
    The caller must predefine a length appropriate to its outcome overlap. With
    fewer than two complete blocks, no interval is emitted. Even when an interval
    is emitted, stationarity, power, asset sampling, and scientific validity have
    not been established. This function never declares strategy effectiveness.
    """
    length = _integer(block_days, "block_days", 1)
    count = _integer(repetitions, "repetitions", 100)
    if type(seed) is not int:
        raise ValueError("seed must be an integer")
    grouped: dict[date, list[float]] = defaultdict(list)
    entities: set[str] = set()
    seen: set[tuple[date, str]] = set()
    for index, row in enumerate(rows):
        context = f"row {index}"
        if not isinstance(row, Mapping):
            raise ValueError(f"{context} must be a mapping")
        day_string = _required(row, "date", context)
        if not isinstance(day_string, str) or len(day_string) != 10:
            raise ValueError(f"{context}: date must be YYYY-MM-DD")
        try:
            day = date.fromisoformat(day_string)
        except ValueError as exc:
            raise ValueError(f"{context}: date must be YYYY-MM-DD") from exc
        if day.isoformat() != day_string:
            raise ValueError(f"{context}: date must be YYYY-MM-DD")
        entity = _text(_required(row, "entity_id", context), f"{context}.entity_id")
        key = (day, entity)
        if key in seen:
            raise ValueError(f"{context}: duplicate date/entity_id pair")
        seen.add(key)
        loss_a = _finite(_required(row, "loss_a", context), f"{context}.loss_a")
        loss_b = _finite(_required(row, "loss_b", context), f"{context}.loss_b")
        difference = loss_a - loss_b
        if not math.isfinite(difference):
            raise ValueError(f"{context}: paired loss difference must be finite")
        grouped[day].append(difference)
        entities.add(entity)
    if not seen:
        raise ValueError("rows must contain at least one paired observation")
    dates = sorted(grouped)
    daily = [_mean(grouped[day], "daily paired loss") for day in dates]
    n_dates = len(dates)
    complete_blocks = n_dates // length
    limitations = [
        "Conditional on the supplied fixed predictions and existing asset pool.",
        "Observed-date blocks do not prove independence or remove regime/model drift.",
        "No model refitting, asset-family resampling, or multiple-testing correction.",
        "A percentile interval does not establish equivalence, noninferiority, or profit.",
    ]
    result: dict[str, Any] = {
        "mean_difference": _mean(daily, "mean paired loss"),
        "difference_convention": "loss_a_minus_loss_b; negative favors a",
        "n_rows": len(seen),
        "n_dates": n_dates,
        "n_entities": len(entities),
        "block_days": length,
        "block_unit": "observed_dates",
        "n_nonoverlapping_blocks": complete_blocks,
        "repetitions": count,
        "seed": seed,
        "ci95": None,
        "method": "paired_equal_entity_within_date_equal_date_circular_moving_blocks",
        "resampling_supported": complete_blocks >= 2,
        "scientific_validity_verified": False,
        "status": "insufficient_date_blocks" if complete_blocks < 2 else "descriptive_only",
        "limitations": limitations,
    }
    if complete_blocks < 2:
        limitations.append("Fewer than two complete date blocks; no interval or inference.")
        return result
    if complete_blocks < 10:
        limitations.append("Fewer than ten complete date blocks; effective period count is small.")
    rng = random.Random(seed)
    samples: list[float] = []
    for _ in range(count):
        draw: list[float] = []
        while len(draw) < n_dates:
            start = rng.randrange(n_dates)
            take = min(length, n_dates - len(draw))
            draw.extend(daily[(start + offset) % n_dates] for offset in range(take))
        samples.append(_mean(draw, "bootstrap mean"))
    samples.sort()
    result["ci95"] = [_quantile(samples, 0.025), _quantile(samples, 0.975)]
    return result
