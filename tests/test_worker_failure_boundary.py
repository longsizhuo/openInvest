"""A failed worker must disable directional proposals at every confidence level."""
import pytest

from openinvest.core.committee.cio_parse import forced_hold, parse_cio_memo, rewrite_tags
from openinvest.core.config import set_config_override


@pytest.mark.parametrize("confidence", [0.0, 0.2, 0.399, 0.4, 0.401, 0.85, 0.99])
@pytest.mark.parametrize("verdict,amount", [("BUY", 1000), ("ACCUMULATE", 1000), ("SELL", -1000)])
@pytest.mark.parametrize("structured", [False, True])
@pytest.mark.parametrize("risk_profile", ["steady", "aggressive"])
def test_worker_failure_blocks_directional_action_below_at_above_ceiling(confidence, verdict, amount, structured, risk_profile):
    set_config_override({"verdict": {"risk_profile": risk_profile}})
    fields = {"verdict": verdict, "confidence": confidence, "suggested_alloc_cny": amount}
    memo = f"VERDICT: {verdict}\nCONFIDENCE: {confidence}\nSUGGESTED_ALLOC_CNY: {amount}"
    result = parse_cio_memo(
        "memo" if structured else memo,
        fields=fields if structured else None,
        worker_brief="RISK: [WORKER_UNAVAILABLE] reason=retry_exhausted",
        regime="uptrend",
    )
    assert result["verdict"] == "HOLD"
    assert result["alloc_cny"] == 0
    assert result["confidence"] <= min(confidence, 0.4)
    assert result["_original_confidence_unavailable"] <= confidence
    assert forced_hold(result)
    assert "sanity3_worker_unavailable" in rewrite_tags(result)
    from openinvest.core.runner.intervention import _intervention_record
    assert _intervention_record("SYNTHETIC", "uptrend", 100.0, result, False) is None


def test_negated_or_missing_worker_failure_does_not_disable_proposal():
    memo = "VERDICT: ACCUMULATE\nCONFIDENCE: 0.2\nSUGGESTED_ALLOC_CNY: 1000"
    result = parse_cio_memo(memo, worker_brief="There is no [WORKER_UNAVAILABLE] marker.")
    assert result["verdict"] == "ACCUMULATE"
    assert result["alloc_cny"] == 1000
    assert result["confidence"] == 0.2


def test_aggressive_profile_does_not_revive_original_hold_from_failed_inputs():
    set_config_override({"verdict": {"risk_profile": "aggressive"}})
    result = parse_cio_memo("VERDICT: HOLD\nCONFIDENCE: 0.2\nSUGGESTED_ALLOC_CNY: 0",
                            worker_brief="[WORKER_UNAVAILABLE] reason=timeout", regime="uptrend")
    assert result["verdict"] == "HOLD" and result["alloc_cny"] == 0
    assert not forced_hold(result)  # original action was already HOLD
    assert "_risk_profile_applied" not in result
