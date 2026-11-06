"""Frozen prospective evaluation and release gates. No parameter writes or trades.

The protocol is data, hashed before candidates are scored. Numeric forecasts
must state their event; CIO confidence is never silently treated as probability.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
import math
import random
from statistics import mean
from typing import Any

from openinvest.core.evidence_eligibility import Eligibility, digest

VERSION_FIELDS = ("code", "data", "features", "model", "prompts", "rules", "parameters", "protocol", "consent")


def timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamps require an explicit timezone")
    return result.astimezone(timezone.utc)


def validate_protocol(p: dict) -> None:
    required = {"schema", "event", "horizon_days", "train_end", "validation_start", "validation_end",
                "final_start", "final_end", "minimum_independent_blocks", "bootstrap_repetitions", "seed",
                "cost_bps", "baseline", "primary_metric", "guard_max_drawdown", "model_version",
                "model_pinned_at", "power_rationale", "execution_rule", "ambiguity_rule", "block_days",
                "expected_observation_keys", "minimum_coverage", "designation"}
    if not required <= p.keys() or p["schema"] != 1:
        raise ValueError("incomplete frozen protocol")
    if p["designation"] not in ("engineering_validation", "prospective_investment_evaluation"):
        raise ValueError("invalid evaluation designation")
    if p["event"] != "net_exposure_utility":
        raise ValueError("Only net_exposure_utility is supported until binary event specifications are preregistered")
    if p["baseline"] != "unchanged_exposure" or p["primary_metric"] != "net_excess_return":
        raise ValueError("unsupported baseline or primary metric")
    if p["execution_rule"] != "next_observable_executable_quote" or p["ambiguity_rule"] != "exclude":
        raise ValueError("execution and ambiguity must be preregistered")
    if not isinstance(p["horizon_days"], int) or p["horizon_days"] <= 0:
        raise ValueError("invalid calendar-day horizon")
    if not isinstance(p["block_days"], int) or p["block_days"] < p["horizon_days"]:
        raise ValueError("date blocks must cover the label horizon")
    if not isinstance(p["minimum_independent_blocks"], int) or p["minimum_independent_blocks"] < 2:
        raise ValueError("invalid independent-block requirement")
    if not isinstance(p["bootstrap_repetitions"], int) or p["bootstrap_repetitions"] < 100:
        raise ValueError("invalid bootstrap protocol")
    if not str(p["power_rationale"]).strip():
        raise ValueError("sample adequacy must have an explicit rationale")
    dates = [timestamp(p[k]) for k in ("train_end", "validation_start", "validation_end", "final_start", "final_end")]
    if not dates[0] < dates[1] <= dates[2] < dates[3] <= dates[4]:
        raise ValueError("train/validation/final must be ordered and disjoint")
    for key in ("cost_bps", "guard_max_drawdown"):
        if not isinstance(p[key], (int, float)) or not math.isfinite(p[key]) or p[key] < 0:
            raise ValueError(f"invalid {key}")
    timestamp(p["model_pinned_at"])
    universe = p["expected_observation_keys"]
    if not isinstance(universe, list) or not universe or len(universe) != len(set(universe)):
        raise ValueError("freeze a non-empty unique observation universe before evaluation")
    if not 0 < p["minimum_coverage"] <= 1:
        raise ValueError("invalid predeclared coverage requirement")


def validate_sample(sample: dict, protocol: dict, now: datetime, split: str) -> str | None:
    try:
        if sample["event"] != protocol["event"] or sample["horizon_days"] != protocol["horizon_days"]:
            return "event_or_horizon_mismatch"
        if sample["origin"] != "prospective" or sample["model_version"] != protocol["model_version"]:
            return "model_or_prospective_provenance_unverified"
        predicted, end = timestamp(sample["prediction_at"]), timestamp(sample["horizon_end"])
        if (end-predicted).total_seconds() != protocol["horizon_days"] * 86400:
            return "horizon_semantics_mismatch"
        if not timestamp(protocol["model_pinned_at"]) <= predicted:
            return "prediction_precedes_model_pin"
        if not timestamp(sample["input_available_at"]) <= timestamp(sample["recorded_at"]) <= predicted:
            return "input_or_capture_lookahead"
        if not end <= timestamp(sample["label_available_at"]) <= now:
            return "outcome_unmatured"
        if not predicted <= timestamp(sample["entry_at"]) <= end <= timestamp(sample["exit_at"]):
            return "execution_precedes_prediction"
        if not end <= timestamp(sample["exit_at"]) <= timestamp(sample["label_available_at"]):
            return "outcome_precedes_executable_exit"
        timestamp(sample["event_at"])  # a scheduled future event can be announced today
        for name in ("published_at", "first_acquired_at", "revision_acquired_at"):
            if not timestamp(sample[name]) <= predicted:
                return "unavailable_input_vintage"
        if sample.get("revision_published_at") and timestamp(sample["revision_published_at"]) > predicted:
            return "future_revision"
        if not sample["feasible"] or sample.get("ambiguous"):
            return "execution_unverified"
        if sample["currency"] != sample["baseline_currency"]:
            return "currency_mismatch"
        bounds = {
            "train": (None, timestamp(protocol["train_end"])),
            "validation": (timestamp(protocol["validation_start"]), timestamp(protocol["validation_end"])),
            "final": (timestamp(protocol["final_start"]), timestamp(protocol["final_end"])),
        }
        lower, upper = bounds[split]
        if (lower and predicted < lower) or timestamp(sample["label_available_at"]) > upper:
            return "purged_boundary_overlap"
        for key in ("entry_value", "exit_value", "exposure_before", "target_exposure", "max_drawdown", "execution_cost_bps"):
            if not isinstance(sample[key], (int, float)) or not math.isfinite(sample[key]):
                return "invalid_economic_input"
        if sample["entry_value"] <= 0 or sample["exit_value"] < 0:
            return "unsupported_valuation_model"  # preserve valid negative-price futures for an appropriate margin/P&L model
        if sample["max_drawdown"] < 0:
            return "invalid_economic_input"
        if sample["execution_cost_bps"] < 0 or sample.get("cost_basis") not in ("quoted_estimate", "observed_fill"):
            return "execution_cost_unverified"
        if not (0 <= sample["exposure_before"] <= 1 and 0 <= sample["target_exposure"] <= 1):
            return "unsupported_leverage"
        if sample.get("probability") is not None:
            if protocol["event"] == "net_exposure_utility" or not 0 <= sample["probability"] <= 1:
                return "invalid_event_probability"
            if type(sample.get("event_outcome")) is not bool:
                return "missing_binary_event_outcome"
        return None
    except (KeyError, TypeError, ValueError, OverflowError):
        return "missing_or_invalid_evidence"


def evaluate(samples: list[dict], protocol: dict, policy: Eligibility, *, now: datetime, split="final", candidate: dict | None = None) -> dict:
    validate_protocol(protocol)
    if candidate and (timestamp(candidate["trained_until"]) > timestamp(protocol["train_end"])
                      or candidate["model_version"] != protocol["model_version"]):
        raise ValueError("candidate uses future training or a different model")
    dataset_hash = digest(sorted(samples, key=lambda s: str(s.get("sample_id", ""))))
    reservation = None
    if protocol["designation"] == "prospective_investment_evaluation":
        from openinvest.core.learning_control import ResearchJournal
        import json
        import re
        if not candidate or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", str(candidate.get("id", ""))):
            raise ValueError("preregister a candidate before investment evaluation")
        journal = ResearchJournal(policy.root)
        registered = json.loads((journal.root / "candidates" / f"{candidate['id']}.json").read_text())
        if (digest(registered["payload"]) != registered["sha256"]
                or registered["payload"]["candidate"] != candidate
                or registered["payload"]["protocol"] != protocol
                or timestamp(registered["registered_at"]) >= timestamp(protocol["final_start"])):
            raise ValueError("candidate/protocol was not preregistered before the final window")
        if split == "final":
            journal.reserve_final_set(dataset_hash, candidate["id"], final_start=protocol["final_start"], final_end=protocol["final_end"])
            reservation = {"dataset_sha256": dataset_hash, "candidate_id": candidate["id"]}
    accepted, excluded, seen = [], [], set()
    universe = set(protocol["expected_observation_keys"])
    for s in sorted(samples, key=lambda row: str(row.get("prediction_at", ""))):
        sample_id = s.get("sample_id", "")
        reason = validate_sample(s, protocol, now, split)
        observation_key = f"{s.get('asset')}|{str(s.get('prediction_at',''))[:10]}"
        if reason is None and observation_key not in universe:
            reason = "outside_preregistered_universe"
        if reason is None and candidate and (s.get("candidate_id") != candidate["id"]
                or s.get("parameters_sha256") != digest(candidate["parameters"])):
            reason = "candidate_binding_mismatch"
        if reason is None and not policy.allows(f"sample:{sample_id}", s, "evaluate"):
            reason = "source_not_qualified"
        # Same event/horizon, including reruns, is one observation.
        identity = (s.get("asset"), s.get("event"), str(s.get("prediction_at", ""))[:10], s.get("horizon_days"))
        if reason is None and (not identity[0] or not s.get("event_id") or identity in seen):
            reason = "duplicate_or_missing_event"
        origin = timestamp(protocol["model_pinned_at"] if split == "train" else protocol[f"{split}_start"])
        block = None
        if reason is None:
            block = int((timestamp(s["prediction_at"])-origin).total_seconds() // (protocol["block_days"] * 86400))
            block_end = origin + timedelta(days=(block+1)*protocol["block_days"])
            if timestamp(s["label_available_at"]) > block_end:
                reason = "purged_block_overlap"
        if reason:
            excluded.append({"sample_id": sample_id, "reason": reason})
            continue
        seen.add(identity)
        gross = s["exit_value"] / s["entry_value"] - 1
        cost = abs(s["target_exposure"]-s["exposure_before"]) * max(protocol["cost_bps"], s["execution_cost_bps"]) / 10000
        net_excess = (s["target_exposure"]-s["exposure_before"]) * gross - cost
        accepted.append({"sample_id": sample_id, "start": timestamp(s["prediction_at"]),
                         "end": timestamp(s["label_available_at"]), "net_excess": net_excess, "block": block, "observation_key": observation_key,
                         "max_drawdown": s["max_drawdown"],
                         "brier": ((s["probability"]-int(s["event_outcome"]))**2
                                   if s.get("probability") is not None else None)})
    # Fixed chronological blocks across ALL assets, with boundary-overlapping
    # labels purged above. Reruns and multi-asset events do not increase n_blocks.
    blocks = {}
    for row in accepted:
        blocks.setdefault(row["block"], []).append(row["net_excess"])
    values = [mean(v) for _, v in sorted(blocks.items())]
    enough = len(values) >= protocol["minimum_independent_blocks"]
    interval = None
    if enough:
        rng = random.Random(protocol["seed"])
        draws = sorted(mean(rng.choices(values, k=len(values))) for _ in range(protocol["bootstrap_repetitions"]))
        interval = [draws[int(.025*len(draws))], draws[min(len(draws)-1, int(.975*len(draws)))]]
    max_dd = max((r["max_drawdown"] for r in accepted), default=None)
    observed = {r["observation_key"] for r in accepted}
    coverage = len(observed) / len(universe)
    final_complete = now >= timestamp(protocol["final_end"])
    ready = enough and coverage >= protocol["minimum_coverage"] and (split != "final" or final_complete)
    passed = bool(ready and interval[0] > 0 and max_dd <= protocol["guard_max_drawdown"])
    return {"protocol_sha256": digest(protocol), "split": split, "sample_count": len(accepted),
            "data_sha256": dataset_hash, "final_reservation": reservation,
            "parameters_sha256": digest(candidate["parameters"]) if candidate else None,
            "candidate_id": candidate.get("id") if candidate else None,
            "candidate_sha256": digest(candidate) if candidate else None,
            "designation": protocol["designation"],
            "coverage": coverage, "missing_observation_keys": sorted(universe-observed),
            "final_window_complete": final_complete,
            "independent_blocks": len(values), "excluded": excluded, "paired_block_mean": mean(values) if values else None,
            "paired_interval_95": interval, "max_drawdown": max_dd,
            "brier": mean([r["brier"] for r in accepted if r["brier"] is not None]) if any(r["brier"] is not None for r in accepted) else None,
            "status": "passed" if passed else "insufficient_evidence" if not ready else "failed",
            "not_claimed": ["actual_execution", "guaranteed_profitability"]}


def release_gate(bundle: dict, policy: dict, evaluation: dict, reviews: list[dict]) -> list[str]:
    """Return every blocker. Frequency caps alone never authorize publication."""
    blocked = []
    if not policy.get("auto_publish", False):
        blocked.append("automatic_publication_disabled")
    if any(not isinstance(bundle.get(k), str) or len(bundle[k]) != 64 for k in VERSION_FIELDS):
        blocked.append("incomplete_version_binding")
    if evaluation.get("status") != "passed" or evaluation.get("protocol_sha256") != bundle.get("protocol"):
        blocked.append("independent_evaluation_not_passed")
    if evaluation.get("split") != "final" or not evaluation.get("final_window_complete") or evaluation.get("missing_observation_keys"):
        blocked.append("final_universe_incomplete")
    if evaluation.get("designation") != "prospective_investment_evaluation":
        blocked.append("engineering_results_are_not_investment_evidence")
    if bundle.get("shadow_trading_days", 0) < 5:
        blocked.append("engineering_shadow_window_incomplete")
    allow = policy.get("parameter_allowlist", {})
    for key, value in bundle.get("parameter_changes", {}).items():
        bounds = allow.get(key)
        if (not bounds or not isinstance(value, (int, float)) or isinstance(value, bool)
                or not math.isfinite(value) or not bounds[0] <= value <= bounds[1]):
            blocked.append(f"parameter_not_authorized:{key}")
    expected = digest(bundle)
    valid = [r for r in reviews if r.get("bundle_sha256") == expected and r.get("passed")
             and not r.get("unresolved_p0_p1") and isinstance(r.get("reviewer"), str) and r["reviewer"].strip()
             and isinstance(bundle.get("author"), str) and bundle["author"].strip() and r["reviewer"] != bundle["author"]]
    if len(valid) != 3 or {r.get("role") for r in valid} != {"engineering", "data", "statistics"} or len({r.get("reviewer") for r in valid}) != 3:
        blocked.append("independent_reviews_incomplete")
    if not bundle.get("rollback_target") or not bundle.get("retain_latest_exclusions"):
        blocked.append("unsafe_rollback")
    return blocked
