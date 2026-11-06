"""Failures must not become evidence, and defensive exits need no round trip."""
import json
from unittest.mock import MagicMock, patch

import pytest

from openinvest.core.committee.cio_parse import parse_cio_memo
from openinvest.core.config import set_config_override
from openinvest.core.memory_store import MemoryStore
from openinvest.jobs import dreaming


def candidate():
    return dict(asset="SYNTHETIC", verdict="BUY", regime=["test"], window_days=7,
                count=10, hit_rate=0.95, avg_return_pct=4, score=0.915)


@pytest.mark.parametrize("payload", [
    {}, {"verdicts": []}, {"verdicts": [{"id": 0}]},
    {"verdicts": [{"id": True, "decision": "KEEP"}]},
    {"verdicts": [{"id": -1, "decision": "KEEP"}]},
    {"verdicts": [{"id": 9, "decision": "KEEP"}]},
    {"verdicts": [{"id": 0, "decision": "MAYBE"}]},
    {"verdicts": ["KEEP"]}, {"verdicts": "KEEP"}, [], None,
])
def test_malformed_verifier_result_never_promotes(payload, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-test-key")
    client = MagicMock()
    client.chat.completions.create.return_value.choices[0].message.content = json.dumps(payload)
    with patch("openai.OpenAI", return_value=client):
        mask, results = dreaming._llm_verify_candidates([candidate()])
    assert mask == [False]
    assert results[0]["decision"] == "PENDING"


def test_duplicate_ids_in_complete_length_response_are_pending(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-test-key")
    client = MagicMock()
    client.chat.completions.create.return_value.choices[0].message.content = json.dumps(
        {"verdicts": [{"id": 0, "decision": "KEEP"}, {"id": 0, "decision": "REJECT"}]})
    with patch("openai.OpenAI", return_value=client):
        mask, results = dreaming._llm_verify_candidates([candidate(), candidate()])
    assert mask == [False, False]
    assert all(r["decision"] == "PENDING" for r in results)


def test_injected_store_never_opens_default_database(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    with patch("openinvest.db.insights_db.InsightsDB", side_effect=AssertionError("implicit DB")):
        assert dreaming.deep_sleep(store, [candidate()]) == []  # unqualified candidate is pending


def test_database_is_an_explicit_dependency(tmp_path, monkeypatch):
    monkeypatch.setattr("openinvest.core.evidence_eligibility.Eligibility.allows", lambda *a, **k: True)
    monkeypatch.setattr("openinvest.core.evidence_eligibility.Eligibility.derive", lambda *a, **k: None)
    from openinvest.db.insights_db import InsightsDB
    db = InsightsDB(str(tmp_path / "explicit.db"))
    try:
        dreaming.deep_sleep(MemoryStore(tmp_path / "memory"), [candidate()], insights_db=db)
        assert db.count() == 1
    finally:
        db.close()


@pytest.mark.parametrize("reason", ["stop_loss", "concentration"])
@pytest.mark.parametrize("reentry", [None, 100.0, 120.0])
@pytest.mark.parametrize("structured", [False, True])
def test_risk_exit_survives_live_price_check(reason, reentry, structured):
    set_config_override({"verdict": {"concentration_lens_enabled": True}})
    fields = dict(verdict="TRIM", confidence=0.7, trim_reason=reason,
                  suggested_alloc_cny=-1000, reentry_price=reentry)
    text = f"VERDICT: TRIM\nCONFIDENCE: 0.7\nTRIM_REASON: {reason}\nSUGGESTED_ALLOC_CNY: -1000"
    if reentry is not None:
        text += f"\nREENTRY_PRICE: {reentry}"
    result = parse_cio_memo(text, fields=fields if structured else None, current_price=100.0)
    assert result["verdict"] == "TRIM"
    assert result["alloc_cny"] == -1000
    assert "_sanity5_reason" not in result


def test_user_disabled_concentration_constraint_still_applies():
    set_config_override({"verdict": {"concentration_lens_enabled": False}})
    result = parse_cio_memo("VERDICT: TRIM\nTRIM_REASON: concentration\nCONFIDENCE: 0.7\nSUGGESTED_ALLOC_CNY: -1000", current_price=100)
    assert result["verdict"] == "HOLD" and result["alloc_cny"] == 0


@pytest.mark.parametrize("language", ["zh", "en"])
def test_rendered_json_prompt_allows_risk_exit_without_reentry(language, monkeypatch):
    from openinvest.capabilities.committee.cio import build_cio_prompt
    monkeypatch.setenv("INVEST_LANG", language)
    prompt = build_cio_prompt({"symbol": "SYNTHETIC", "display_name": "Synthetic"}, json_mode=True)
    price_line = next(line for line in prompt.splitlines() if '"reentry_price"' in line)
    assert "stop_loss / concentration" in price_line and "null" in price_line
    assert "TRIM 必须低于现价" not in price_line
    assert "must be below the current price for TRIM" not in price_line
