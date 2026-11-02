"""Check the mechanism experiment, including its ability to report a bad gate."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from experiments.decision_evidence.sensitivity import run_sensitivity


@pytest.fixture(scope="module")
def baseline():
    return run_sensitivity()


def test_real_calculation_and_parser_mechanisms(baseline):
    assert baseline["mechanism_checks_passed"]
    required = {
        "calibration_identity": 1,
        "calibration_matches_frozen_shrinkage_equation": 12,
        "smaller_sample_shrinks_more": 1,
        "gamma_alone_preserves_probabilities_and_median": 9,
        "config_override_reaches_real_parser": 3,
        "buy_overconfidence_inclusive_boundary": 3,
        "absolute_allocation_ceiling_boundary": 6,
        "single_investment_cap_boundary": 3,
        "negated_marker_is_negative_control": 2,
    }
    for name, count in required.items():
        assert baseline["checks"][name] == {
            "category": "mechanism", "count": count, "failures": 0, "passed": True,
        }
    assert baseline["profitability_or_predictive_value_tested"] is False
    assert baseline["historical_configuration_reconstructed"] is False


def test_worker_gate_includes_low_equal_high_boundaries(baseline):
    gate = baseline["checks"]["worker_failure_always_holds_and_zeros_allocation"]
    assert gate == {"category": "safety", "count": 160, "failures": 0, "passed": True}
    for profile in ("steady", "aggressive"):
        for regime in ("range_bound", "uptrend"):
            assert baseline["checks"][f"worker_failure_safety_{profile}_{regime}"] == {
                "category": "safety", "count": 40, "failures": 0, "passed": True,
            }
    assert baseline["synthetic_safety_boundary_failures"] == []
    assert baseline["safety_gates_passed"] is True
    assert baseline["all_checks_passed"] is True


def test_experiment_detects_legacy_floor_regression(tmp_path):
    # Reintroduce the old boundary behavior only in the original steady/range
    # subgroup.  Retain the original 40/16 baseline; interactions with aggressive
    # upgrading are a separate mechanism, not the floor-regression simulation.
    root = Path(__file__).resolve().parents[3]
    program = """
import json,os
from pathlib import Path
from experiments.decision_evidence.sensitivity import _install_guard,_experiment
attempts=_install_guard(Path(os.environ['INVEST_HOME']).parent)
import openinvest.core.config._loader as loader
loader._read_persisted_overrides=lambda:{}
import openinvest.core.committee.cio_parse as cio
original=cio.parse_cio_memo
floor=loader.load_config().verdict.worker_unavailable_confidence_floor
def legacy(text,**kwargs):
 if (loader.load_config().verdict.risk_profile == 'steady'
     and kwargs.get('regime') == 'range_bound'
     and kwargs['fields']['confidence'] <= floor):
  text=text.replace('[WORKER_UNAVAILABLE] reason=timeout','')
  kwargs['worker_brief']=None
 return original(text,**kwargs)
cio.parse_cio_memo=legacy
print(json.dumps(_experiment([],attempts)))
"""
    env = {"PATH": os.defpath, "PYTHONPATH": os.pathsep.join((str(root / "src"), str(root))),
           "PYTHONDONTWRITEBYTECODE": "1", "PYTHON_DOTENV_DISABLED": "1",
           "INVEST_HOME": str(tmp_path / "home")}
    proc = subprocess.run([sys.executable, "-B", "-c", program], env=env,
                          text=True, capture_output=True, timeout=15, check=True)
    report = json.loads(proc.stdout)
    gate = report["checks"]["worker_failure_always_holds_and_zeros_allocation"]
    assert gate == {"category": "safety", "count": 160, "failures": 16, "passed": False}
    expected = {("steady", "range_bound", boundary, verdict, location)
                for boundary in ("below_floor", "at_floor")
                for verdict in ("BUY", "ACCUMULATE", "TRIM", "SELL")
                for location in ("text", "worker_brief")}
    actual = {(r["risk_profile"], r["regime"], r["boundary"], r["verdict"], r["marker_location"])
              for r in report["synthetic_safety_boundary_failures"]}
    assert actual == expected
    assert report["checks"]["worker_failure_safety_steady_range_bound"]["failures"] == 16
    for name in ("steady_uptrend", "aggressive_range_bound", "aggressive_uptrend"):
        assert report["checks"]["worker_failure_safety_" + name]["passed"] is True
    assert report["mechanism_checks_passed"] is True
    assert report["safety_gates_passed"] is False


def test_parent_environment_cannot_change_experiment(monkeypatch, baseline):
    monkeypatch.setenv("INVEST_CIO_CONFIDENCE_CAP", "0")
    monkeypatch.setenv("INVEST_ALLOC_AGGRESSIVENESS", "0.000001")
    monkeypatch.setenv("INVEST_VERDICT_RISK_PROFILE", "aggressive")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "not-a-real-key-do-not-forward")
    monkeypatch.setenv("INVEST_HOME", "/unrelated/production-like-home")
    assert run_sensitivity() == baseline


def test_saved_proposals_are_replayed_without_disclosing_inputs():
    cases = [{
        "case_id": "private-identifier-never-return",
        "proposal": {"verdict": "BUY", "confidence": .7, "alloc_cny": 8765432,
                     "expected_path": "private-narrative-never-return"},
        "context": {"current_price": 100, "alloc_ceiling": 10000, "single_invest_cap": 1000},
    }]
    out = run_sensitivity(cases)
    assert out["saved_proposals"] == 1
    for key in ("saved_proposal_current_rule_repeatability", "saved_proposal_current_defense_replay"):
        assert out["checks"][key]["passed"]
        assert out["checks"][key]["count"] == 1
    serialized = json.dumps(out)
    for private in ("private-identifier-never-return", "private-narrative-never-return", "8765432"):
        assert private not in serialized


@pytest.mark.parametrize("case", [
    {"proposal": {"verdict": "BUY", "confidence": float("nan"), "alloc_cny": 100}},
    {"proposal": {"verdict": "BUY", "confidence": .7, "alloc_cny": float("inf")}},
    {"proposal": {"verdict": "BUY", "confidence": 1.1, "alloc_cny": 100}},
    {"proposal": {"verdict": "BUY", "confidence": .7, "alloc_cny": 100},
     "context": {"current_price": -1}},
    {"proposal": {"verdict": "BUY", "confidence": .7, "alloc_cny": 100},
     "context": {"worker_brief": "unrequested-input"}},
])
def test_invalid_input_is_rejected_before_worker(case):
    with pytest.raises(ValueError):
        run_sensitivity([case])


def test_guard_blocks_real_network_private_reads_and_outside_writes(tmp_path):
    root = Path(__file__).resolve().parents[3]
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    outside = tmp_path / "must-not-exist"
    program = """
import json,socket,sys
from pathlib import Path
from experiments.decision_evidence.sensitivity import _install_guard
attempts=_install_guard(Path(sys.argv[1]))
for operation in (
 lambda: socket.create_connection(('127.0.0.1',9),timeout=.1),
 lambda: open(sys.argv[2],'w'),
 lambda: open(sys.argv[3],'r'),
):
 try:
  operation()
 except PermissionError:
  pass
 else:
  raise AssertionError('isolation did not block operation')
print(json.dumps(attempts))
"""
    env = {"PATH": os.defpath, "PYTHONPATH": str(root), "PYTHONDONTWRITEBYTECODE": "1"}
    proc = subprocess.run([sys.executable, "-B", "-c", program, str(sandbox), str(outside), str(root / ".env")],
                          env=env, text=True, capture_output=True, timeout=15, check=True)
    assert json.loads(proc.stdout) == {
        "network_attempts": 1, "outside_write_attempts": 1,
        "private_read_attempts": 1, "child_process_attempts": 0,
    }
    assert not outside.exists()


def test_default_worker_has_no_forbidden_attempts(baseline):
    assert baseline["isolation"] == {
        "network_attempts": 0, "outside_write_attempts": 0,
        "private_read_attempts": 0, "child_process_attempts": 0,
    }
