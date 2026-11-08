"""Execution claims stay separate from plans, estimates and paper simulations."""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from openinvest.core.decision_calc import _match_trades, history_as_trades, summarize_decisions
from openinvest.core.decision_ledger import list_decisions, record_execution


def _trade(**overrides):
    return {
        "id": 1, "ts": "2026-07-03T12:00:00", "symbol": "AAPL",
        "direction": "BUY", "status": "executed", "units": 2, "price": 200,
        **overrides,
    }


@pytest.mark.parametrize("status", ["planned", "cancelled", None])
def test_explicit_verdict_link_does_not_make_a_plan_executed(status):
    assert _match_trades(
        [_trade(status=status, verdict_id="2026-07-03/AAPL")],
        "2026-07-03/AAPL", "2026-07-03", "AAPL", "BUY",
    ) == []


@pytest.mark.parametrize("domain", ["broker_paper", "broker_unknown", "projection", "unknown"])
def test_non_live_domains_never_match_as_real_adoption(domain):
    assert _match_trades(
        [_trade(execution_domain=domain, verdict_id="2026-07-03/AAPL")],
        "2026-07-03/AAPL", "2026-07-03", "AAPL", "BUY",
    ) == []


def test_paper_explicit_link_does_not_hide_real_window_match():
    paper = _trade(id=2, source="paper_autopilot", execution_domain="client_reported",
                   verdict_id="2026-07-03/AAPL", evidence_basis="paper_simulated_fill")
    real = _trade(execution_domain="client_reported")
    args = ("2026-07-03/AAPL", "2026-07-03", "AAPL", "BUY")
    assert _match_trades([paper, real], *args) == [real]
    assert _match_trades([paper, real], *args, execution_domain="paper_simulation") == [paper]


def test_projection_does_not_become_a_fill_inside_paper_domain():
    projected = _trade(execution_domain="paper_simulation", evidence_basis="unobserved_dca_projection")
    assert _match_trades(
        [projected], "2026-07-03/AAPL", "2026-07-03", "AAPL", "BUY",
        execution_domain="paper_simulation",
    ) == []


@pytest.mark.parametrize("row", [
    {"evidence_basis": "unobserved_dca_projection"},
    {"evidence_basis": "nav_calculated_without_execution_confirmation"},
    {"source": "dca_daily", "action": "buy", "execution_confirmed": True,
     "evidence_basis": "client_reported_not_independently_verified"},
])
def test_unobserved_basis_cannot_match_even_with_explicit_verdict(row):
    assert _match_trades(
        [_trade(**row, verdict_id="2026-07-03/AAPL")],
        "2026-07-03/AAPL", "2026-07-03", "AAPL", "BUY",
    ) == []


def test_history_preserves_source_and_simulation_evidence():
    records = [{
        "ts": "2026-07-03T12:00:00", "action": "buy", "symbol": "AAPL",
        "units": 2, "price": 200, "source": "paper_autopilot",
        "evidence_basis": "paper_simulated_fill", "execution_domain": "paper_simulation",
        "source_reference": "simulation-run/123", "fee_status": "estimated",
        "verdict_id": "2026-07-03/AAPL",
    }]
    got, = history_as_trades(records)
    assert got["status"] == "simulated"
    assert got["source"] == "paper_autopilot"
    assert got["evidence_basis"] == "paper_simulated_fill"
    assert got["execution_domain"] == "paper_simulation"
    assert got["source_reference"] == "simulation-run/123"
    assert got["fee_status"] == "estimated"
    assert got["verdict_id"] == "2026-07-03/AAPL"


def test_fund_order_is_an_intent_until_observed_confirmation():
    rows = [
        {"ts": "2026-07-03T12:00:00", "action": "fund_order_placed",
         "symbol": "FUND:000001", "amount": 200, "order_id": "x"},
        {"ts": "2026-07-04T12:00:00", "action": "fund_order_confirmed",
         "symbol": "FUND:000001", "units": 100, "price": 2, "amount": 200,
         "order_id": "x", "source": "fund_auto_confirm"},
    ]
    got = history_as_trades(rows)
    assert [t["status"] for t in got] == ["planned", "unobserved"]
    assert _match_trades(got, "2026-07-03/FUND:000001", "2026-07-03", "FUND:000001", "BUY") == []


def test_history_does_not_upgrade_explicit_planned_status():
    got = history_as_trades([{
        "ts": "2026-07-03T12:00:00", "action": "buy", "symbol": "AAPL",
        "units": 2, "price": 200, "source": "mcp", "status": "planned",
    }])
    assert got[0]["status"] == "planned"
    assert _match_trades(got, "2026-07-03/AAPL", "2026-07-03", "AAPL", "BUY") == []


def test_malformed_paper_history_cannot_claim_even_simulated_execution():
    got = history_as_trades([{
        "ts": "2026-07-03T12:00:00", "action": "buy", "symbol": "AAPL",
        "units": "unavailable", "price": 200, "source": "paper_autopilot",
        "verdict_id": "2026-07-03/AAPL",
    }])
    assert got[0]["status"] == "unobserved"
    assert _match_trades(got, "2026-07-03/AAPL", "2026-07-03", "AAPL", "BUY",
                         execution_domain="paper_simulation") == []


def test_record_execution_is_idempotent_within_each_domain(tmp_path):
    path = tmp_path / "executions.jsonl"
    did = "2026-07-03/AAPL"
    real = record_execution(did, True, reason="用户明确确认", path=path)
    paper = record_execution(did, True, reason="paper_autopilot", path=path,
                             execution_domain="paper_simulation")
    assert record_execution(did, True, reason="用户明确确认", path=path) == real
    assert record_execution(did, True, reason="paper_autopilot", path=path,
                            execution_domain="paper_simulation") == paper
    assert len(path.read_text().splitlines()) == 2
    assert real["executed"] is True and real["execution_domain"] == "client_reported"
    assert paper["executed"] is False and paper["simulated_execution"] is True
    assert paper["domain_execution_state"] == "executed"


def _seed_decision(monkeypatch, tmp_path):
    from openinvest.core import memory_store
    from openinvest.db import trades_db

    class EmptyTrades:
        def list_trades(self, limit=10000):
            return []

    monkeypatch.setattr(memory_store, "MEMORY_ROOT", tmp_path)
    monkeypatch.setattr(trades_db, "TradesDB", EmptyTrades)
    day = datetime.now().strftime("%Y-%m-%d")
    target = tmp_path / ".committee" / day
    target.mkdir(parents=True)
    (target / "AAPL.md").write_text(
        f"**Symbol**: AAPL\n**Verdict**: BUY (confidence 0.70)\n"
        "**Suggested allocation CNY**: 1000\n")
    return f"{day}/AAPL"


@pytest.mark.parametrize("real_executed", [True, False])
def test_newer_paper_feedback_does_not_overwrite_explicit_user_feedback(monkeypatch, tmp_path, real_executed):
    did = _seed_decision(monkeypatch, tmp_path)
    record_execution(did, real_executed, reason="用户明确反馈")
    record_execution(did, True, reason="paper_autopilot", execution_domain="paper_simulation")
    decision, = list_decisions()
    assert decision["executed"] is real_executed
    assert decision["execution"]["reason"] == "用户明确反馈"
    assert decision["simulated_execution"]["executed"] is False
    assert decision["simulated_executed"] is True
    assert summarize_decisions([decision])["executed"] == int(real_executed)


def test_legacy_paper_feedback_stays_visible_without_real_adoption(monkeypatch, tmp_path):
    did = _seed_decision(monkeypatch, tmp_path)
    path = tmp_path / ".dreams" / "executions.jsonl"
    path.parent.mkdir()
    path.write_text(json.dumps({"decision_id": did, "executed": True, "reason": "paper_autopilot"}) + "\n")
    decision, = list_decisions()
    assert decision["execution"] is None and decision["executed"] is None
    assert decision["simulated_executed"] is True
    assert decision["simulated_execution"]["execution_domain"] == "paper_simulation"
    assert decision["simulated_execution"]["executed"] is False
    assert summarize_decisions([decision])["executed"] == 0


def test_unknown_broker_feedback_does_not_claim_real_adoption(monkeypatch, tmp_path):
    did = _seed_decision(monkeypatch, tmp_path)
    record_execution(did, True, execution_domain="broker_unknown")
    decision, = list_decisions()
    assert decision["executed"] is None and decision["execution"] is None
    assert decision["domain_executions"]["broker_unknown"]["domain_execution_state"] == "executed"


def test_explicit_planned_link_keeps_domain_without_claiming_execution(monkeypatch, tmp_path):
    from openinvest.db import trades_db

    did = _seed_decision(monkeypatch, tmp_path)
    planned = _trade(status="planned", verdict_id=did, execution_domain="broker_paper")
    monkeypatch.setattr(trades_db.TradesDB, "list_trades", lambda self, limit=10000: [planned])
    decision, = list_decisions()
    assert decision["executed"] is None and decision["matched_trades"] == []
    assert decision["linked_trades"][0]["status"] == "planned"
    assert decision["linked_trades"][0]["execution_domain"] == "broker_paper"
