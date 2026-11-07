"""Final decisions must retain raw evidence even when earlier agents omit it."""
import pytest


@pytest.mark.parametrize("json_mode", [False, True])
def test_cio_gets_raw_market_time_and_regime_despite_worker_omission(monkeypatch, json_mode):
    from openinvest.core.committee import debate

    captured = []

    class Agent:
        def __init__(self, role):
            self.role = role

        def run(self, context):
            if self.role == "cio":
                captured.append(context)
                if json_mode:
                    return '{"verdict":"HOLD","confidence":0.5,"suggested_alloc_cny":0,"memo":"No edge"}'
                return "VERDICT: HOLD\nCONFIDENCE: 0.5\nSUGGESTED_ALLOC_CNY: 0"
            # Deliberately omit both market facts and timestamp from worker summaries.
            return "SIGNAL: neutral\nSTRENGTH: 4\nONE_LINER: No change"

    monkeypatch.setattr(debate, "_create_agent", lambda prompt, **kw: Agent(kw["role"]))
    monkeypatch.setattr("openinvest.utils.llm.supports_json_output", lambda *args, **kw: json_mode)
    debate.run_committee(
        asset={"symbol": "TEST.AX"}, market_data="PRICE=56.70; RVOL=2.99; MA250=57.31",
        macro_view="SIGNAL: neutral", portfolio_summary="portfolio supplied",
        regime_brief="REGIME: range_bound\nATR_SPIKE=1.74",
        input_health={"prepared_at": "2026-10-30T13:15:55+00:00"},
        max_debate_rounds=1, persist_to_memory=False,
    )
    assert captured
    for context in captured:
        assert "PRICE=56.70; RVOL=2.99; MA250=57.31" in context
        assert "ATR_SPIKE=1.74" in context
        assert "ANALYSIS_AS_OF: 2026-10-30T13:15:55+00:00" in context


@pytest.mark.parametrize("as_of", [None, "", "not-a-date", "2026-10-30T13:15:55"])
def test_unknown_or_naive_analysis_time_never_uses_wall_clock(as_of):
    from openinvest.core.committee.evidence import cio_source_context
    context = cio_source_context("provided facts", "", as_of)
    assert "ANALYSIS_AS_OF: unknown" in context
    assert "do not invent a calendar expiry" in context


def test_range_classification_does_not_assert_normal_risk():
    from openinvest.calc.regime import classify_regime
    metrics = {"ma20": 60.7775, "ma120": 61.0345, "atr_pct": 1.9408,
               "atr_spike_ratio": 1.7385, "atr_pct_median_1y": 1.1163,
               "return_30d": -0.105255, "price_quantile_2y": 0.72,
               "rebound_off_low_60d": 0.0}
    result = classify_regime(metrics, symbol="NDQ.AX")
    assert result["regime"] == "range_bound"
    assert "不代表波动或下跌风险正常" in result["reason"]
