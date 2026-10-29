"""Reproduce the synthetic study and optionally inventory a private frozen live history.

Outputs must stay in this worktree's ignored experiment directory. Only the
synthetic public_demo.json may be deliberately copied into the public archive.
"""
from __future__ import annotations

import argparse
import copy
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any

from .evidence import canonical_bytes, digest, freeze_history, read_bundle, verify_history, write_bundle
from .history import audit_history
from .scoring import brier, interval_score, paired_day_bootstrap, score_records
from .sensitivity import run_sensitivity

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = Path(__file__).resolve().parent
PROTOCOL = PACKAGE / "inputs" / "protocol.json"


def _synthetic_events() -> list[dict[str, Any]]:
    rows = []
    for i, outcome in enumerate((True, False)):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(days=i)
        rows.append({"record_id": f"synthetic-{i}", "event_id": "terminal_return_positive", "horizon": "30_calendar_days",
                     "probability": 0.8, "outcome": outcome,
                     "forecast_at": start.isoformat(),
                     "information_available_at": (start - timedelta(hours=1)).isoformat(),
                     "outcome_at": (start + timedelta(days=30)).isoformat(),
                     "evaluated_at": "2025-01-01T00:00:00Z",
                     "action": "HOLD", "confidence": 0.99})
    return rows


def public_demo(output: Path, protocol: dict[str, Any]) -> dict[str, Any]:
    """Public known-answer experiment; contains no production data or identifiers."""
    payload = {"run_id": "synthetic-attempt-001", "evidence_type": "synthetic",
               "inputs": {"facts": {"scenario": "bullish_but_defense_active"},
                          "role_models": {"analyst": "synthetic", "cio": "synthetic"},
                          "prompt_sha256": digest(b"synthetic_prompt"),
                          "information_available_at": "2024-01-01T09:00:00Z",
                          "forecast_at": "2024-01-01T10:00:00Z"},
               "proposal": {"action": "ACCUMULATE", "action_confidence": .8},
               "validated": {"action": "HOLD", "reason": "defense"}, "execution": None}
    first = write_bundle(output / "bundles", payload)
    retry = write_bundle(output / "bundles", payload)
    conflict_rejected = False
    try:
        write_bundle(output / "bundles", {**payload, "validated": {"action": "BUY"}})
    except ValueError:
        conflict_rejected = True
    second_payload = copy.deepcopy(payload)
    second_payload["run_id"] = "synthetic-attempt-002"
    second = write_bundle(output / "bundles", second_payload)
    evidence_gates = {
        "identical_retry_preserved": first == retry and read_bundle(first) == payload,
        "conflicting_run_rewrite_rejected": conflict_rejected,
        "separate_attempts_preserved": first != second and read_bundle(second) == second_payload,
        "unknown_execution_not_invented": read_bundle(first)["execution"] is None,
    }

    events = _synthetic_events()
    scores = score_records(events, event_id=protocol["probability_event"], horizon=protocol["probability_horizon"])
    changed_actions = [{**row, "action": "BUY", "confidence": .1} for row in events]
    semantic_gates = {
        "known_brier": abs(scores["mean_brier"] - .34) < 1e-12,
        "actions_do_not_change_forecast_scores": scores == score_records(
            changed_actions, event_id=protocol["probability_event"], horizon=protocol["probability_horizon"]),
        "interval_width_penalty": interval_score(2, 5, 3) == 3,
        "interval_miss_penalty": interval_score(2, 5, 1) == 13,
    }
    for name, changes in {
        "wrong_event_rejected": {"event_id": "action_correct"},
        "wrong_horizon_rejected": {"horizon": "30_trading_days"},
        "unavailable_input_rejected": {"information_available_at": "2025-01-01T00:00:00Z"},
        "immature_label_rejected": {"evaluated_at": "2024-01-02T00:00:00Z"},
        "unknown_probability_rejected": {"probability": None},
    }.items():
        try:
            score_records([{**events[0], **changes}], event_id=protocol["probability_event"],
                          horizon=protocol["probability_horizon"])
        except ValueError:
            semantic_gates[name] = True
        else:
            semantic_gates[name] = False

    # Ground truth is generated, not chosen after looking at market returns.
    # The signal control is intentionally informative in this synthetic world;
    # the null and overconfident controls cannot claim live investment skill.
    panels: dict[str, list[dict[str, Any]]] = {"known_signal": [], "null": [], "constant_overconfident": []}
    for i in range(120):
        for entity in range(4):
            outcome = (i + entity) % 2 == 0
            signal = .9 if outcome else .1
            for name, probability in (("known_signal", signal), ("null", .5), ("constant_overconfident", .9)):
                panels[name].append({"date": (date(2024, 1, 1) + timedelta(days=i)).isoformat(),
                                     "entity_id": f"synthetic_{entity}",
                                     "loss_a": brier(probability, outcome), "loss_b": brier(.5, outcome)})
    params = protocol["bootstrap"]
    statistics = {name: paired_day_bootstrap(rows, **params) for name, rows in panels.items()}
    statistical_gates = {
        "known_signal_improves": abs(statistics["known_signal"]["mean_difference"] + .24) < 1e-12,
        "null_is_zero": statistics["null"]["mean_difference"] == 0.,
        "uninformative_confidence_is_worse": abs(statistics["constant_overconfident"]["mean_difference"] - .16) < 1e-12,
        "same_date_entities_not_independent_dates": statistics["known_signal"]["n_dates"] == 120,
        "no_strategy_effectiveness_claim": all(not x["scientific_validity_verified"] for x in statistics.values()),
    }
    sensitivity = run_sensitivity()
    passed = (all(evidence_gates.values()) and all(semantic_gates.values())
              and all(statistical_gates.values()) and sensitivity["all_checks_passed"])
    result = {"schema_version": 1, "data_origin": "synthetic_only",
              "protocol_sha256": digest(canonical_bytes(protocol)),
              "gates": {"immutable_evidence": evidence_gates, "event_semantics": semantic_gates,
                        "paired_statistics": statistical_gates},
              "scores": scores, "statistics": statistics, "sensitivity": sensitivity,
              "engineering_feasible": passed,
              "strategy_effectiveness_verified": False}
    (output / "public_demo.json").write_bytes(canonical_bytes(result))
    return result


def private_history(source: Path, output: Path, protocol: dict[str, Any]) -> dict[str, Any]:
    manifest = freeze_history(source, output / "snapshot", cutoff=protocol["historical_cutoff"])
    audit = audit_history(output / "snapshot" / "files", cutoff=protocol["historical_cutoff"])
    expected_sources = {
        str((output / "snapshot" / ("files" if item["kind"] == "committee" else ".dreams") / item["path"]).resolve()): item["sha256"]
        for item in manifest["files"]
    }
    observed_sources = {str(Path(item["path"]).resolve()): item["sha256"] for item in audit["private_sources"]}
    if expected_sources != observed_sources:
        raise ValueError("audited evidence is not the frozen manifest cohort")
    (output / "private_audit.json").write_bytes(canonical_bytes(audit))
    saved_cases = []
    for record in audit["private_records"]:
        raw = record["raw_proposal"]
        if (record["paired_observation_eligible"] and raw["verdict"] in {"BUY", "ACCUMULATE", "HOLD", "TRIM", "SELL"}
                and raw.get("alloc_cny") is not None and raw.get("alloc_currency") == "CNY"):
            saved_cases.append({"proposal": {"verdict": raw["verdict"], "confidence": raw["confidence"],
                                             "alloc_cny": raw["alloc_cny"]}, "context": {}})
        # These are captures of persisted observations, not retroactive new runs.
        write_bundle(output / "historical_bundles", {
            "run_id": "capture-" + record["source_sha256"], "evidence_type": "historical_capture",
            "inputs": None,
            "proposal": raw if raw["status"] == "parsed" else None,
            "validated": record["header"] if record["header"]["status"] == "parsed" else None,
            "execution": None,
        })
    sensitivity = run_sensitivity(saved_cases)
    stable = verify_history(source, output / "snapshot")
    result = {"schema_version": 1, "data_origin": "private_historical_observations",
              "protocol_sha256": digest(canonical_bytes(protocol)),
              "snapshot_manifest_sha256": digest(canonical_bytes(manifest)),
              "inventory": audit["summary"], "current_rule_sensitivity": sensitivity,
              "source_hashes_unchanged": stable,
              "historical_rules_reconstructed": False,
              "saved_proposal_context": "partial saved verdict/confidence/CNY amount with synthetic current context; not a full historical replay",
              "historical_prediction_or_profitability_claim": False,
              "measurement_feasible": stable and sensitivity["all_checks_passed"]}
    (output / "private_readout.json").write_bytes(canonical_bytes(result))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--committee-root", type=Path, help="optional read-only live .committee root")
    args = parser.parse_args()
    allowed = ROOT / "memory" / ".decision_evidence_experiment"
    resolved = args.output.resolve()
    if not resolved.is_relative_to(allowed.resolve()) or resolved == allowed.resolve():
        parser.error("output must be a new subdirectory of this worktree's memory/.decision_evidence_experiment")
    if args.output.exists() or any(p.is_symlink() for p in (args.output, *args.output.parents)):
        parser.error("output must be new and must not traverse symlinks")
    protocol_source = PROTOCOL.read_bytes()
    protocol = json.loads(protocol_source)
    source_paths = sorted([*PACKAGE.rglob("*.py"), *ROOT.glob("src/openinvest/**/*.py"),
                           *ROOT.glob("src/openinvest/core/config/*.yaml")])
    source_hashes = {p.relative_to(ROOT).as_posix(): digest(p.read_bytes()) for p in source_paths}
    resolved.mkdir(parents=True, exist_ok=False)
    (resolved / "protocol.json").write_bytes(canonical_bytes(protocol))
    result = public_demo(resolved, protocol)
    private = private_history(args.committee_root, resolved, protocol) if args.committee_root else None
    if (PROTOCOL.read_bytes() != protocol_source or any(
        digest((ROOT / relative).read_bytes()) != fingerprint for relative, fingerprint in source_hashes.items()
    )):
        raise RuntimeError("protocol or source changed during experiment")
    (resolved / "run_manifest.json").write_bytes(canonical_bytes({
        "schema_version": 1, "protocol_sha256": digest(canonical_bytes(protocol)),
        "protocol_source_sha256": digest(protocol_source), "source_sha256": source_hashes,
        "source_unchanged_after_run": True,
    }))
    # Only a boolean status is printed for private data, never counts or groups.
    status = {"synthetic_gates_passed": result["engineering_feasible"],
              "private_history_completed": private is not None,
              "private_measurement_gates_passed": private["measurement_feasible"] if private else None,
              "strategy_effectiveness_verified": False}
    print(json.dumps(status, sort_keys=True))
    if not result["engineering_feasible"] or (private and not private["measurement_feasible"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
