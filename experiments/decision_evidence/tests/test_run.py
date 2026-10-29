import json
from pathlib import Path

import pytest

from experiments.decision_evidence.evidence import canonical_bytes
from experiments.decision_evidence.run import PACKAGE, PROTOCOL, private_history, public_demo


def test_public_demo_reproduces_checked_in_synthetic_artifact(tmp_path):
    result = public_demo(tmp_path, json.loads(PROTOCOL.read_text()))
    assert result["engineering_feasible"]
    assert not result["strategy_effectiveness_verified"]
    reference = PACKAGE / "data" / "public_demo.json"
    assert canonical_bytes(result) == reference.read_bytes()


def test_private_capture_does_not_invent_historical_provenance(tmp_path):
    source = tmp_path / "live" / ".committee"
    day = source / "2026-10-20"
    day.mkdir(parents=True)
    document = ("# Committee\n**Symbol**: SYNTHETIC\n"
                "**Run at**: 2026-10-20T10:00:00+00:00\n"
                "**Verdict**: HOLD (confidence 0.40)\n\n"
                "## CIO Memo\nVERDICT: ACCUMULATE\nCONFIDENCE: 0.80\n"
                "SUGGESTED_ALLOC_CNY: 1000\n")
    original = document.encode()
    (day / "SYNTHETIC.md").write_bytes(original)
    out = tmp_path / "private"
    out.mkdir()
    result = private_history(source, out, json.loads(PROTOCOL.read_text()))
    assert result["measurement_feasible"]
    assert result["source_hashes_unchanged"]
    assert not result["historical_rules_reconstructed"]
    assert not result["historical_prediction_or_profitability_claim"]
    assert result["inventory"]["counts"]["historical_replay_eligible"] == 0
    assert result["inventory"]["counts"]["probability_score_eligible"] == 0
    assert result["current_rule_sensitivity"]["saved_proposals"] == 1
    assert (day / "SYNTHETIC.md").read_bytes() == original


def test_conflicting_snapshot_never_becomes_a_replay_case(tmp_path):
    source = tmp_path / "live" / ".committee"
    day = source / "2026-10-20"
    day.mkdir(parents=True)
    (day / "SYNTHETIC.md").write_text(
        "# Committee\n**Symbol**: SYNTHETIC\n"
        "**Run at**: 2026-10-20T10:00:00+00:00\n"
        "**Verdict**: HOLD (confidence 0.40)\n\n"
        "## CIO Memo\nVERDICT: ACCUMULATE\nCONFIDENCE: 0.80\n"
        "SUGGESTED_ALLOC_CNY: 1000\n")
    (day / "SYNTHETIC_committee.json").write_text(json.dumps({"symbol": "OTHER"}))
    out = tmp_path / "private"
    out.mkdir()
    result = private_history(source, out, json.loads(PROTOCOL.read_text()))
    assert result["current_rule_sensitivity"]["saved_proposals"] == 0
