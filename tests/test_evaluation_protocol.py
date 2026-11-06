from datetime import datetime, timedelta, timezone

import pytest

from openinvest.core.evaluation_protocol import evaluate, release_gate
from openinvest.core.evidence_eligibility import Eligibility, digest
from openinvest.core.learning_control import ResearchJournal, DEFAULT_POLICY, publish_parameters
from openinvest.core.memory_store import MemoryStore
from tests.integrity_helpers import approve


def protocol():
    return dict(schema=1, designation="engineering_validation", event="net_exposure_utility", horizon_days=7, block_days=14,
                expected_observation_keys=["SYNTHETIC|2025-02-01", "SYNTHETIC|2025-02-15"], minimum_coverage=1.0,
                train_end="2025-01-01T00:00:00+00:00", validation_start="2025-01-02T00:00:00+00:00",
                validation_end="2025-01-30T00:00:00+00:00", final_start="2025-02-01T00:00:00+00:00",
                final_end="2025-04-30T00:00:00+00:00", minimum_independent_blocks=2,
                bootstrap_repetitions=199, seed=7, cost_bps=10, baseline="unchanged_exposure",
                primary_metric="net_excess_return", guard_max_drawdown=.05,
                model_version="synthetic-fixed-version", model_pinned_at="2024-12-01T00:00:00+00:00",
                power_rationale="Synthetic known-answer engineering test; not a deployment power claim",
                execution_rule="next_observable_executable_quote", ambiguity_rule="exclude")


def sample(day=0, asset="SYNTHETIC", exit_value=110):
    start = datetime(2025,2,1,tzinfo=timezone.utc)+timedelta(days=day)
    earlier=(start-timedelta(hours=1)).isoformat(); end=(start+timedelta(days=7)).isoformat()
    return dict(sample_id=f"{asset}_{day}", asset=asset, event_id=f"event_{day}",
                event="net_exposure_utility", horizon_days=7, origin="prospective",
                model_version="synthetic-fixed-version", prediction_at=start.isoformat(), horizon_end=end,
                input_available_at=earlier, recorded_at=earlier, event_at=earlier, published_at=earlier,
                first_acquired_at=earlier, revision_acquired_at=earlier, revision_published_at=None,
                label_available_at=end, entry_at=start.isoformat(), exit_at=end, execution_cost_bps=10,
                cost_basis="quoted_estimate", feasible=True, ambiguous=False, currency="CNY", baseline_currency="CNY",
                entry_value=100, exit_value=exit_value, exposure_before=0, target_exposure=1, max_drawdown=.01)


def score(root, rows):
    for r in rows:
        approve(root, f"sample:{r['sample_id']}", r)
    p = protocol()
    p["expected_observation_keys"] = sorted({f"{r['asset']}|{r['prediction_at'][:10]}" for r in rows})
    return evaluate(rows, p, Eligibility(root), now=datetime(2026,1,1,tzinfo=timezone.utc))


def test_same_day_assets_reruns_and_overlapping_windows_are_not_independent(tmp_path):
    a=sample(); duplicate={**a,"sample_id":"rerun"}; b=sample(asset="SECOND"); overlap=sample(10)
    r=score(tmp_path,[a,duplicate,b,overlap])
    assert r["sample_count"] == 2 and r["independent_blocks"] == 1
    assert r["status"] == "insufficient_evidence"
    assert {e["reason"] for e in r["excluded"]} == {"duplicate_or_missing_event","purged_block_overlap"}


def test_negative_outcome_kept_and_blocks_a_false_success(tmp_path):
    r=score(tmp_path,[sample(),sample(14,exit_value=90)])
    assert r["sample_count"] == 2 and r["excluded"] == []
    assert r["paired_block_mean"] == pytest.approx(-.001)
    assert r["status"] == "failed" and r["brier"] is None


def test_known_positive_control_and_future_data_rejection(tmp_path):
    assert score(tmp_path,[sample(),sample(14)])["status"] == "passed"
    future=sample();future["first_acquired_at"]="2025-02-02T00:00:00+00:00"
    r=score(tmp_path,[future])
    assert r["sample_count"] == 0 and r["excluded"][0]["reason"] == "unavailable_input_vintage"


def test_preregister_all_trials_and_do_not_reuse_final_windows(tmp_path):
    journal=ResearchJournal(tmp_path)
    journal.register({"id":"first"},protocol());journal.register({"id":"second"},protocol())
    bounds={"final_start":protocol()["final_start"],"final_end":protocol()["final_end"]}
    journal.reserve_final_set("a"*64,"first",**bounds)
    with pytest.raises(ValueError,match="overlapping"):
        journal.reserve_final_set("b"*64,"second",**bounds)
    with pytest.raises(ValueError,match="different"):
        journal.register({"id":"first","changed":True},protocol())


def test_default_publication_cannot_change_config_even_with_self_reported_pass(tmp_path):
    store=MemoryStore(tmp_path/"memory")
    result=publish_parameters(store,{"rollback_target":"prior"}, {"status":"passed", "candidate_id":"synthetic"},[{"passed":True}])
    assert result["status"] == "blocked"
    assert "automatic_publication_disabled" in result["reasons"]
    assert not (store.root/".state/config_overrides.json").exists()
