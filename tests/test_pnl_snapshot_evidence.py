"""Old P&L points remain auditable without bypassing current source eligibility."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from unittest.mock import Mock

import pytest

from openinvest.core.memory_store import MemoryStore
from openinvest.core.portfolio_manager import PortfolioManager
from openinvest.jobs import pnl_snapshot as job
from openinvest.services.ledger_evidence import account_source_status
from openinvest.services.financial_publication import SOURCE_STATEMENT, VERIFICATION_PENDING_SVG, seal_calculated_point


def _world(tmp_path, monkeypatch, mode="projection"):
    owner = tmp_path / "owner"
    store = MemoryStore(owner / "memory")
    monkeypatch.setattr("openinvest.core.memory_store.MEMORY_ROOT", store.root)
    monkeypatch.setattr(job, "ROOT", owner)
    monkeypatch.setattr(job, "HISTORY_PATH", store.root / ".state/pnl_history.jsonl")
    monkeypatch.setattr(job, "SVG_PATH", owner / "docs/pnl_chart.svg")
    store.write("user", "user", {"display_name": "Synthetic"}, "")
    store.write("strategy", "strategy", {"target_assets": []}, "")
    store.write("portfolio", "state", {"schema_version": 2, "cash": {"CNY": 1000},
        **({"ledger_domain": "paper_simulation"} if mode == "paper" else {}), "holdings": [
        {"symbol": "SYNTHETIC", "units": 10, "avg_cost": 5, "cost_currency": "CNY"}]}, "")
    source = "paper_autopilot" if mode == "paper" else "dca_daily" if mode == "projection" else "skill_cli"
    store.append_history({"ts": "2020-01-01T00:00:00+00:00", "action": "buy", "symbol": "SYNTHETIC",
                          "source": source, "units": 10, "price": 5, "currency": "CNY"})
    now = datetime.now(timezone.utc)
    job.HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    job.HISTORY_PATH.write_text("".join(json.dumps({"ts": (now - timedelta(days=days)).isoformat(),
        "equity_idx": index, "total_pnl_pct": index - 100}) + "\n" for days, index in [(2, 100), (1, 105)]))
    job.SVG_PATH.parent.mkdir(parents=True)
    job.SVG_PATH.write_text("<svg>previous chart retained for audit</svg>")
    (job.SVG_PATH.parent / "outperform_events.jsonl").write_text('{"label":"old audit event"}\n')
    if mode == "unavailable":
        (store.root / ".state/account_observation_heads.json").write_text('{"broken":true}')
    return owner, store


def _files(owner):
    return {str(path.relative_to(owner)): path.read_bytes() for path in owner.rglob("*") if path.is_file()}


def _calculate_points(store, monkeypatch, indexes=(100.0, 105.0)):
    with monkeypatch.context() as patch:
        patch.setattr(job, "_refresh_market", lambda pm, base: None)
        for index in indexes:
            patch.setattr(job, "build_account_performance", lambda pm, base=None, value=index: {
                "status": "ok", "twr_all": value - 100, "financial_performance_eligible": True,
                "source_authority": "client_recorded_not_independently_verified",
            })
            job._append_history(job._compute_snapshot(store))
    return job._read_history()


@pytest.mark.parametrize("mode", ["projection", "unavailable"])
def test_render_only_does_not_render_or_publish_unverified_existing_profit_points(tmp_path, monkeypatch, mode):
    owner, store = _world(tmp_path, monkeypatch, mode)
    assert account_source_status(PortfolioManager(store))["historical_execution_eligible"] is False
    assert len(job.HISTORY_PATH.read_text().splitlines()) == 2 and job._read_history() == []
    render = Mock(side_effect=AssertionError("unverified history reached renderer"))
    publish = Mock(side_effect=AssertionError("unverified history reached publisher"))
    monkeypatch.setattr(job, "render_svg", render)
    monkeypatch.setattr(job, "_auto_push_svg", publish)
    before = _files(owner)
    result = job.render_only()
    assert result["status"] == "unverified" and result["reason"] == "unverified_execution_history"
    assert result["historical_performance_eligible"] is False and result["pushed"] is False
    render.assert_not_called()
    publish.assert_not_called()
    assert _files(owner) == before


def test_new_snapshot_entry_uses_the_same_gate_before_refresh_append_render_or_publish(tmp_path, monkeypatch):
    owner, _ = _world(tmp_path, monkeypatch)
    monkeypatch.setattr(job, "_is_trading_window", lambda: True)
    spies = {}
    for name in ("_refresh_market", "_append_history", "render_svg", "_auto_push_svg", "_persist_outperform"):
        spies[name] = Mock(side_effect=AssertionError("unverified account reached " + name))
        monkeypatch.setattr(job, name, spies[name])
    before = _files(owner)
    result = job.run()
    assert result["status"] == "unverified" and result["publication_eligible"] is False
    for spy in spies.values():
        spy.assert_not_called()
    assert _files(owner) == before


@pytest.mark.parametrize("mode", ["projection", "client"])
def test_direct_republish_checks_source_and_original_artifact_before_any_git_operation(tmp_path, monkeypatch, mode):
    owner, _ = _world(tmp_path, monkeypatch, mode)
    monkeypatch.setenv("INVEST_PNL_AUTOPUSH", "1")
    monkeypatch.setenv("GITHUB_TOKEN", "synthetic-publish-token")
    publish = Mock(return_value={"pushed": True, "publication_kind": "verification_notice"})
    monkeypatch.setattr(job, "_publish_svg_buffer", publish)
    before = _files(owner)
    result = job._auto_push_svg()
    assert result["status"] == "unverified" and result["pushed"] is False
    publish.assert_called_once_with(VERIFICATION_PENDING_SVG, notice_only=True)
    assert result["withdrawal"]["publication_kind"] == "verification_notice"
    assert _files(owner) == before


def test_qualified_paper_book_is_not_rerendered_as_an_actual_performance_chart(tmp_path, monkeypatch):
    owner, store = _world(tmp_path, monkeypatch, "paper")
    grade = account_source_status(PortfolioManager(store))
    assert grade["paper_performance_eligible"] is True and grade["historical_execution_eligible"] is False
    render, publish = Mock(), Mock()
    monkeypatch.setattr(job, "render_svg", render)
    monkeypatch.setattr(job, "_auto_push_svg", publish)
    before = _files(owner)
    result = job.render_only()
    assert result["status"] == "simulation_only"
    assert result["execution_domain"] == "paper_simulation" and result["publication_eligible"] is False
    render.assert_not_called()
    publish.assert_not_called()
    assert _files(owner) == before


def test_qualified_client_book_can_rerender_without_changing_original_points(tmp_path, monkeypatch):
    _, store = _world(tmp_path, monkeypatch, "client")
    assert account_source_status(PortfolioManager(store))["historical_execution_eligible"] is True
    _calculate_points(store, monkeypatch)
    original = job.HISTORY_PATH.read_bytes()
    chart = f"<svg><text>qualified client chart</text><text>{SOURCE_STATEMENT}</text></svg>"
    render = Mock(return_value=chart)
    publish = Mock(return_value={"pushed": False, "reason": "INVEST_PNL_AUTOPUSH != 1"})
    monkeypatch.setattr(job, "render_svg", render)
    monkeypatch.setattr(job, "_auto_push_svg", publish)
    result = job.render_only()
    assert result["status"] == "ok_render_only" and result["history_points"] == 2
    render.assert_called_once()
    publish.assert_called_once()
    assert job.HISTORY_PATH.read_bytes() == original
    assert job.SVG_PATH.read_text() == chart


def test_current_ledger_qualification_cannot_upgrade_old_unreceipted_returns(tmp_path, monkeypatch):
    owner, store = _world(tmp_path, monkeypatch, "client")
    assert account_source_status(PortfolioManager(store))["historical_execution_eligible"] is True
    render, publish = Mock(), Mock()
    monkeypatch.setattr(job, "render_svg", render)
    monkeypatch.setattr(job, "_auto_push_svg", publish)
    before = _files(owner)
    result = job.render_only()
    assert result["status"] == "unverified" and result["history_points"] == 0
    assert result["reason"] == "insufficient_qualified_history"
    render.assert_not_called()
    publish.assert_not_called()
    assert _files(owner) == before


@pytest.mark.parametrize("duplicate", ["same_reference", "same_time"])
def test_duplicate_receipt_or_timestamp_cannot_supply_the_second_point(tmp_path, monkeypatch, duplicate):
    owner, store = _world(tmp_path, monkeypatch, "client")
    point, = _calculate_points(store, monkeypatch, indexes=(100,))
    repeated = dict(point)
    if duplicate == "same_time":
        repeated["calculation_receipt"] = seal_calculated_point(store, point,
            {"status": "ok", "twr_all": 0, "financial_performance_eligible": True},
            account_source_status(PortfolioManager(store)))
    with job.HISTORY_PATH.open("a") as history:
        history.write(json.dumps(repeated) + "\n")
    render, publish = Mock(), Mock()
    monkeypatch.setattr(job, "render_svg", render)
    monkeypatch.setattr(job, "_auto_push_svg", publish)
    before = _files(owner)
    assert len(job._read_history()) == 1
    result = job.render_only()
    assert result["status"] == "insufficient_history" and result["history_points"] == 1
    render.assert_not_called()
    publish.assert_not_called()
    assert _files(owner) == before


@pytest.mark.parametrize("tamper", ["point", "receipt"])
def test_changed_point_or_receipt_cannot_be_rerendered(tmp_path, monkeypatch, tamper):
    owner, store = _world(tmp_path, monkeypatch, "client")
    points = _calculate_points(store, monkeypatch)
    if tamper == "point":
        rows = [json.loads(line) for line in job.HISTORY_PATH.read_text().splitlines()]
        rows[-1]["equity_idx"] += 1
        job.HISTORY_PATH.write_text("".join(json.dumps(row) + "\n" for row in rows))
    else:
        path = store.root / ".evidence/pnl_publication/point" / (points[-1]["calculation_receipt"]["id"] + ".json")
        envelope = json.loads(path.read_text())
        envelope["payload"]["point"]["equity_idx"] += 1
        path.write_text(json.dumps(envelope))
    render, publish = Mock(), Mock()
    monkeypatch.setattr(job, "render_svg", render)
    monkeypatch.setattr(job, "_auto_push_svg", publish)
    before = _files(owner)
    assert job.render_only()["status"] == "unverified"
    render.assert_not_called()
    publish.assert_not_called()
    assert _files(owner) == before


def test_new_receipt_retains_calculation_scope_and_does_not_grant_prediction_or_learning(tmp_path, monkeypatch):
    _, store = _world(tmp_path, monkeypatch, "client")
    point, = _calculate_points(store, monkeypatch, indexes=(105,))
    path = store.root / ".evidence/pnl_publication/point" / (point["calculation_receipt"]["id"] + ".json")
    payload = json.loads(path.read_text())["payload"]
    assert payload["point"]["equity_idx"] == 105 and payload["performance_result"]["twr_all"] == 5
    assert payload["ledger_input_fingerprint"] == payload["ledger_input"]["content_sha256"]
    assert payload["execution_domain"] == "client_reported" and len(payload["code_version"]) == 64
    assert payload["market_input_vintage_complete"] is False
    assert payload["learning_eligible"] is payload["prediction_eligible"] is False
    assert payload["allowed_uses"] == ["display_client_recorded_returns"]
