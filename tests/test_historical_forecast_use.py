"""A descriptive historical table cannot silently grant itself forecasting use."""
from openinvest.services.historical_evidence import qualification,project
from openinvest.core.evidence_eligibility import Eligibility
from tests.integrity_helpers import approve_historical_generator


REGIME="REGIME: range_bound\nREASON: observed MA spread is small\nINPUTS: current_price=100\nTHRESHOLDS: trend_ratio=3.6\nSTRATEGY_HINT: historical forecast: 99% rebound\nCALIBRATION_DISCLOSURE_JSON: {}"
PATH="Historical terminal distribution: 99% rebound."


def test_unapproved_forecast_hidden_while_current_observations_and_audit_input_survive(tmp_path):
    view,path,proof=project(REGIME,PATH,data_root=tmp_path)
    assert not proof["forecast_use_allowed"]
    assert "current_price=100" in view and "MA spread" in view
    assert "99%" not in view+path and "50%" not in view+path
    assert "does not imply HOLD" in view
    assert "99%" in REGIME+PATH  # Original input is not mutated.


def test_bound_model_can_be_used_but_parent_revocation_and_parameter_change_reject_it(tmp_path):
    from openinvest.core.config import set_config_override
    approve_historical_generator(tmp_path)
    assert qualification(tmp_path)["forecast_use_allowed"]
    assert project(REGIME,PATH,data_root=tmp_path)[:2]==(REGIME,PATH)
    set_config_override({"path":{"band_gamma":1.2}})
    assert not qualification(tmp_path)["forecast_use_allowed"]
    set_config_override({})
    assert qualification(tmp_path)["forecast_use_allowed"]
    Eligibility(tmp_path).revoke("dataset:synthetic-history","synthetic withdrawal")
    assert not qualification(tmp_path)["forecast_use_allowed"]


def test_no_forecast_approval_is_inferred_from_an_llm_disclosure_or_generator_numbers(tmp_path):
    from openinvest.core.memory_store import MemoryStore
    from openinvest.core.regime_probability import get_regime_forward_summary
    assert "99%" not in project(REGIME,PATH,data_root=tmp_path)[0]
    # A fresh store has no forecast approval; no synthetic probability is returned.
    assert get_regime_forward_summary("SYNTHETIC","range_bound",100) is None


def test_actual_metric_implementation_change_invalidates_prior_approval(tmp_path,monkeypatch):
    import openinvest.calc.market_metrics as metrics
    approve_historical_generator(tmp_path)
    assert qualification(tmp_path)["forecast_use_allowed"]
    changed=tmp_path/"changed_metric_implementation.py"
    changed.write_text("# synthetic metric implementation revision\n")
    monkeypatch.setattr(metrics,"__file__",str(changed))
    assert not qualification(tmp_path)["forecast_use_allowed"]
