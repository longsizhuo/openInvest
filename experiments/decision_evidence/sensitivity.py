"""Offline sensitivity of the current calculation and parser pathways.

This is a mechanism experiment, not a return backtest.  Saved proposals, when
supplied, are replayed through *current* rules; historical configuration and
missing inputs are not inferred.  The worker never calls an LLM or a broker.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any


_ROOT = Path(__file__).resolve().parents[2]
_PROPOSAL_KEYS = {
    "verdict", "confidence", "alloc_cny", "dominant_view", "trim_reason",
    "reentry_price", "reentry_condition", "expected_path",
}
_CONTEXT_KEYS = {"current_price", "regime", "alloc_ceiling", "single_invest_cap"}


def _validate_cases(cases: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if cases is None:
        return []
    if not isinstance(cases, list):
        raise ValueError("cases must be a list")
    result = []
    for case in cases:
        if not isinstance(case, dict) or set(case) - {"case_id", "proposal", "context"}:
            raise ValueError("unsupported case schema")
        proposal, context = case.get("proposal"), case.get("context", {})
        if not isinstance(proposal, dict) or set(proposal) - _PROPOSAL_KEYS:
            raise ValueError("unsupported proposal schema")
        if not {"verdict", "confidence", "alloc_cny"} <= proposal.keys():
            raise ValueError("proposal requires verdict, confidence and alloc_cny")
        if proposal["verdict"] not in {"BUY", "ACCUMULATE", "HOLD", "TRIM", "SELL"}:
            raise ValueError("unsupported proposal verdict")
        if not isinstance(context, dict) or set(context) - _CONTEXT_KEYS:
            raise ValueError("unsupported replay context")
        for key in ("confidence", "alloc_cny", "reentry_price"):
            value = proposal.get(key)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                      or not math.isfinite(value)):
                raise ValueError("proposal numbers must be finite")
        if not 0 <= proposal["confidence"] <= 1:
            raise ValueError("confidence must be in [0,1]")
        for key in ("current_price", "alloc_ceiling", "single_invest_cap"):
            value = context.get(key)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                      or not math.isfinite(value) or value < 0):
                raise ValueError("context numbers must be finite and nonnegative")
        # A caller's identifiers and narrative are unnecessary for these checks.
        sanitized = {k: v for k, v in proposal.items()
                     if k not in {"reentry_condition", "expected_path"}}
        result.append({"proposal": sanitized, "context": dict(context)})
    return result


def run_sensitivity(cases: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Run anonymous, deterministic aggregate checks in a clean subprocess.

    Optional cases have ``proposal`` (raw, saved CIO fields) and ``context``
    (current_price/regime/alloc_ceiling/single_invest_cap).  Do not pass a final
    verdict as though it were an original proposal.  No case fields are returned.
    """
    payload = {"cases": _validate_cases(cases)}
    with tempfile.TemporaryDirectory(prefix="openinvest-sensitivity-") as sandbox:
        env = {
            "PATH": os.defpath,
            "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
            "PYTHONPATH": os.pathsep.join((str(_ROOT / "src"), str(_ROOT))),
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHON_DOTENV_DISABLED": "1",
            "INVEST_HOME": str(Path(sandbox) / "home"),
            "TMPDIR": sandbox, "TMP": sandbox, "TEMP": sandbox,
        }
        worker = subprocess.run(
            [sys.executable, "-B", "-m", "experiments.decision_evidence.sensitivity", "--worker"],
            input=json.dumps(payload, allow_nan=False), text=True, capture_output=True,
            cwd=_ROOT, env=env, timeout=90, check=False,
        )
        if worker.returncode:
            # stderr can contain a parser's private diagnostic; never relay it.
            raise RuntimeError(f"isolated sensitivity worker failed (exit {worker.returncode})")
        try:
            return json.loads(worker.stdout)
        except (ValueError, TypeError) as exc:
            raise RuntimeError("isolated worker returned invalid aggregate JSON") from exc


def _install_guard(sandbox: Path) -> dict[str, int]:
    """Block network and private reads; permit writes only inside the sandbox."""
    sandbox = sandbox.resolve()
    attempts = {"network_attempts": 0, "outside_write_attempts": 0,
                "private_read_attempts": 0, "child_process_attempts": 0}

    def inside(path: Any) -> bool:
        if isinstance(path, int):
            return False
        try:
            return Path(os.fsdecode(path)).resolve().is_relative_to(sandbox)
        except (TypeError, ValueError):
            return False

    def write(path: Any) -> None:
        if not inside(path):
            attempts["outside_write_attempts"] += 1
            raise PermissionError("write outside sensitivity sandbox")

    def audit(event: str, args: tuple[Any, ...]) -> None:
        if event in {"socket.connect", "socket.connect_ex", "socket.bind",
                     "socket.getaddrinfo", "socket.sendto", "socket.sendmsg"}:
            attempts["network_attempts"] += 1
            raise PermissionError("sensitivity worker network is disabled")
        if event in {"subprocess.Popen", "os.system", "os.posix_spawn", "os.fork", "os.exec"}:
            attempts["child_process_attempts"] += 1
            raise PermissionError("nested processes are disabled")
        if event == "open":
            path, _mode, flags = args
            if isinstance(path, (str, bytes)):
                p = Path(os.fsdecode(path))
                if p.name == ".env" or "memory" in p.parts or p.suffix in {".db", ".sqlite", ".sqlite3"}:
                    attempts["private_read_attempts"] += 1
                    raise PermissionError("private inputs must be supplied explicitly")
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC):
                write(path)
        elif event in {"os.mkdir", "os.remove", "os.rmdir", "os.chmod", "os.truncate"}:
            write(args[0])
        elif event in {"os.rename", "os.link", "os.symlink"}:
            write(args[0])
            write(args[1])

    sys.addaudithook(audit)
    return attempts


def _experiment(cases: list[dict[str, Any]], attempts: dict[str, int]) -> dict[str, Any]:
    # Must precede committee package import: cio_parse builds THRESHOLDS at
    # import time, and the default loader otherwise constructs a MemoryStore.
    import logging
    logging.disable(logging.CRITICAL)
    import openinvest.core.config._loader as loader
    loader._read_persisted_overrides = lambda: {}
    from openinvest.core.config import load_config, set_config_override
    from openinvest.calc.regime_probability import calibrate_profile
    from openinvest.core.committee.cio_parse import parse_cio_memo

    config = load_config()
    checks: dict[str, dict[str, Any]] = {}

    def record(name: str, ok: bool, *, category: str = "mechanism") -> None:
        item = checks.setdefault(name, {"passed": True, "count": 0, "failures": 0,
                                        "category": category})
        item["count"] += 1
        item["failures"] += int(not ok)
        item["passed"] = item["failures"] == 0

    def close(a: float, b: float) -> bool:
        return math.isclose(a, b, rel_tol=0, abs_tol=0.000100001)

    expected_modules = {
        "openinvest.calc.regime_probability": _ROOT / "src/openinvest/calc/regime_probability.py",
        "openinvest.core.committee.cio_parse": _ROOT / "src/openinvest/core/committee/cio_parse.py",
    }
    record("functions_loaded_from_selected_checkout", all(
        Path(sys.modules[name].__file__).resolve() == path.resolve()
        for name, path in expected_modules.items()
    ), category="isolation")

    raw = {"effective_n": 1, "median_pct": 1.2, "p_below": .25, "p_down": .10,
           "p10_pct": -4., "p90_pct": 6., "downside_pct": -2.}
    unc = {"median_pct": .5, "p_below": .45, "p_down": .20,
           "p10_pct": -8., "p90_pct": 10., "downside_pct": -4.}
    fields = tuple(unc)
    profile = {"windows": {"30d": raw}, "uncond_windows": {"30d": unc}}
    original = copy.deepcopy(profile)
    identity = calibrate_profile(profile, shrinkage_k=0., band_gamma=1.)
    record("calibration_identity", identity == original)
    same = {"windows": {"30d": dict(raw)}, "uncond_windows": {"30d": dict(raw)}}
    equal = calibrate_profile(same, shrinkage_k=80., band_gamma=1.)["windows"]["30d"]
    record("calibration_equal_distributions_zero_change", equal == raw)
    distances = []
    for effective_n in (1, 80):
        p = copy.deepcopy(profile)
        p["windows"]["30d"]["effective_n"] = effective_n
        out = calibrate_profile(p, shrinkage_k=80., band_gamma=1.)["windows"]["30d"]
        lam = effective_n / (effective_n + 80.)
        for key in fields:
            record("calibration_matches_frozen_shrinkage_equation",
                   close(out[key], round(lam * raw[key] + (1 - lam) * unc[key], 4)))
        distances.append(abs(out["p_below"] - unc["p_below"]))
    record("smaller_sample_shrinks_more", distances[0] < distances[1])
    for gamma in (1., 1.1, 1.5):
        out = calibrate_profile(profile, shrinkage_k=0., band_gamma=gamma)["windows"]["30d"]
        for key in ("p_below", "p_down", "median_pct"):
            record("gamma_alone_preserves_probabilities_and_median", out[key] == raw[key])
        for key in ("p10_pct", "p90_pct", "downside_pct"):
            record("gamma_matches_frozen_expansion_equation",
                   close(out[key], raw["median_pct"] + gamma * (raw[key] - raw["median_pct"])))
    changed = copy.deepcopy(profile)
    changed["windows"]["30d"]["p_below"] = .65
    transformed = calibrate_profile(changed, shrinkage_k=80., band_gamma=1.)
    baseline = calibrate_profile(profile, shrinkage_k=80., band_gamma=1.)
    record("raw_perturbation_reaches_calibrated_probability",
           transformed["windows"]["30d"]["p_below"] != baseline["windows"]["30d"]["p_below"])
    record("raw_perturbation_does_not_mutate_unconditional_input", changed["uncond_windows"] == profile["uncond_windows"])
    record("calibrator_preserves_inputs", profile == original)

    def proposal(verdict: str = "ACCUMULATE", confidence: float = .7, amount: int = 1200) -> dict[str, Any]:
        return {"verdict": verdict, "confidence": confidence, "alloc_cny": amount,
                "dominant_view": "quant", "trim_reason": "bearish", "reentry_price": 90.}

    def parse(value: dict[str, Any], *, text: str = "", **kwargs: Any) -> dict[str, Any]:
        options = {"current_price": 100., "regime": "range_bound", "alloc_ceiling": 10000,
                   "single_invest_cap": 0}
        options.update(kwargs)
        cio_fields = copy.deepcopy(value)
        # Proposal storage uses the normalized amount name; the actual CIO JSON
        # schema consumes suggested_alloc_<currency>, not parser output alloc_cny.
        cio_fields["suggested_alloc_cny"] = cio_fields.pop("alloc_cny")
        return parse_cio_memo(text, fields=cio_fields, **options)

    def signature(out: dict[str, Any]) -> tuple[Any, ...]:
        return tuple(out.get(k) for k in ("verdict", "confidence", "alloc_cny", "trim_reason", "reentry_price"))

    for verdict, amount in (("BUY", 1200), ("ACCUMULATE", 1200), ("HOLD", 0), ("TRIM", -1200), ("SELL", -1200)):
        value = proposal(verdict, amount=amount)
        before = parse(value, defense_flag_on=False)
        after = parse(value, defense_flag_on=True)
        expected = {"BUY": ("ACCUMULATE", 1200), "ACCUMULATE": ("HOLD", 0)}.get(verdict, (verdict, amount))
        record("defense_switch_changes_only_buy_side_as_specified", (after["verdict"], after["alloc_cny"]) == expected)
        record("fixed_input_repeatability", signature(before) == signature(parse(value, defense_flag_on=False)))

    threshold = config.verdict.buy_confidence_overdrive
    for confidence in (threshold - 1e-6, threshold, threshold + 1e-6):
        out = parse(proposal("BUY", confidence))
        expected = "ACCUMULATE" if confidence >= threshold else "BUY"
        record("buy_overconfidence_inclusive_boundary", out["verdict"] == expected)
    set_config_override({"verdict": {"buy_confidence_overdrive": .8}})
    for confidence, expected in ((.799999, "BUY"), (.8, "ACCUMULATE"), (.800001, "ACCUMULATE")):
        record("config_override_reaches_real_parser", parse(proposal("BUY", confidence))["verdict"] == expected)
    set_config_override({})

    # An independent safety expectation deliberately includes <= floor.  A
    # mechanism experiment must be able to expose a gap in the current rule.
    floor = config.verdict.worker_unavailable_confidence_floor
    boundary_failures = []
    # Coverage expansion checks interaction with the downstream aggressive
    # upgrade.  It adds no outcome-based tuning and retains the original
    # steady/range_bound forty-case subgroup and its unchanged safety criterion.
    for risk_profile in ("steady", "aggressive"):
        set_config_override({"verdict": {"risk_profile": risk_profile}})
        for regime in ("range_bound", "uptrend"):
            for label, confidence in (("below_floor", floor - 1e-6), ("at_floor", floor),
                                      ("above_floor", floor + 1e-6), ("high_confidence", .7)):
                for verdict, amount in (("BUY", 1200), ("ACCUMULATE", 1200), ("HOLD", 0),
                                        ("TRIM", -1200), ("SELL", -1200)):
                    for location in ("text", "worker_brief"):
                        kw = {location: "[WORKER_UNAVAILABLE] reason=timeout", "regime": regime}
                        out = parse(proposal(verdict, confidence, amount), **kw)
                        safe = out["verdict"] == "HOLD" and out["alloc_cny"] == 0 and out["confidence"] <= floor
                        record("worker_failure_always_holds_and_zeros_allocation", safe, category="safety")
                        record(f"worker_failure_safety_{risk_profile}_{regime}", safe, category="safety")
                        if not safe:
                            boundary_failures.append({"risk_profile": risk_profile, "regime": regime,
                                                      "boundary": label, "verdict": verdict,
                                                      "marker_location": location})
    # Later amount and saved-proposal probes use the neutral current-rule
    # context; the final aggressive cell must not leak into those probes.
    set_config_override({"verdict": {"risk_profile": "steady"}})
    for location in ("text", "worker_brief"):
        value = proposal()
        out = parse(value, **{location: "无 [WORKER_UNAVAILABLE] 标记"})
        record("negated_marker_is_negative_control", signature(out) == signature(parse(value)))

    for amount in (9999, 10000, 10001, -9999, -10000, -10001):
        verdict = "ACCUMULATE" if amount > 0 else "SELL"
        out = parse(proposal(verdict, amount=amount))
        record("absolute_allocation_ceiling_boundary",
               out["alloc_cny"] == max(-10000, min(10000, amount)) and out["verdict"] == verdict)
    for amount in (999, 1000, 1001):
        out = parse(proposal(amount=amount), single_invest_cap=1000)
        record("single_investment_cap_boundary", out["alloc_cny"] == min(amount, 1000) and out["verdict"] == "ACCUMULATE")
    sell = parse(proposal("SELL", amount=-1200), single_invest_cap=1000)
    record("single_investment_cap_preserves_sells", sell["alloc_cny"] == -1200 and sell["verdict"] == "SELL")
    combined = parse(proposal(amount=20000), single_invest_cap=1000)
    record("combined_allocation_caps", combined["alloc_cny"] == 1000)
    held = parse(proposal(amount=20000), single_invest_cap=1000, defense_flag_on=True)
    record("defense_hold_has_no_stale_buy_allocation", held["verdict"] == "HOLD" and held["alloc_cny"] == 0)

    for case in cases:
        value, context = case["proposal"], case["context"]
        before = parse(value, **context)
        repeat = parse(value, **context)
        record("saved_proposal_current_rule_repeatability", signature(before) == signature(repeat))
        out = parse(value, defense_flag_on=True, **context)
        # Postprocessing can change BUY before defense.  Compare against the
        # current pipeline's defense-off output, not the historical final label.
        expected_v = {"BUY": "ACCUMULATE", "ACCUMULATE": "HOLD"}.get(before["verdict"], before["verdict"])
        expected_a = 0 if before["verdict"] == "ACCUMULATE" else before["alloc_cny"]
        record("saved_proposal_current_defense_replay", out["verdict"] == expected_v and out["alloc_cny"] == expected_a)

    record("isolation_no_network_or_private_io", not any(attempts.values()), category="isolation")
    return {
        "schema_version": 1,
        "claim_scope": "current_rule_pathway_sensitivity_only",
        "synthetic_inputs": not bool(cases),
        "synthetic_context": True,
        "saved_proposals": len(cases),
        "historical_configuration_reconstructed": False,
        "profitability_or_predictive_value_tested": False,
        "checks": checks,
        "all_checks_passed": all(item["passed"] for item in checks.values()),
        "mechanism_checks_passed": all(item["passed"] for item in checks.values() if item["category"] == "mechanism"),
        "safety_gates_passed": all(item["passed"] for item in checks.values() if item["category"] == "safety"),
        "synthetic_safety_boundary_failures": boundary_failures,
        "isolation": dict(attempts),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        home = Path(os.environ["INVEST_HOME"])
        attempts = _install_guard(home.parent)
        payload = json.load(sys.stdin)
        result = _experiment(_validate_cases(payload.get("cases")), attempts)
    else:
        result = run_sensitivity()
    print(json.dumps(result, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
