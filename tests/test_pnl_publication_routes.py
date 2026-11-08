"""Legacy HTTP return files obey the same source gate as the publication job."""
from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from openinvest.connectors.web_api.routers import read, state
from openinvest.jobs import pnl_snapshot
from tests.test_pnl_snapshot_evidence import _calculate_points, _files, _world
from openinvest.services.financial_publication import SOURCE_STATEMENT


def _client(owner, monkeypatch):
    monkeypatch.setattr(read, "PNL_CHART_PATH", pnl_snapshot.SVG_PATH)
    monkeypatch.setattr(state, "INVEST_ROOT", owner)
    app = FastAPI()
    app.include_router(read.router)
    app.include_router(state.router)
    return TestClient(app)


@pytest.mark.parametrize("mode", ["projection", "unavailable", "paper", "client"])
def test_legacy_http_paths_cannot_republish_unqualified_actual_return_files(tmp_path, monkeypatch, mode):
    owner, _ = _world(tmp_path, monkeypatch, mode)
    before = _files(owner)
    with _client(owner, monkeypatch) as client:
        for path in ("/api/pnl_chart.svg", "/api/pnl_history", "/api/outperform_events"):
            response = client.get(path)
            assert response.status_code == 409, (path, response.text)
            detail = response.json()["detail"]
            assert detail["status"] == ("simulation_only" if mode == "paper" else "unverified")
            assert detail["historical_performance_eligible"] is False
            assert detail["publication_eligible"] is False
            assert "equity_idx" not in response.text and "old audit event" not in response.text
        assert client.get("/api/dreams/state").status_code == 200
    assert _files(owner) == before


def test_qualified_client_http_responses_keep_the_existing_chart_points_and_event_shape(tmp_path, monkeypatch):
    owner, store = _world(tmp_path, monkeypatch, "client")
    _calculate_points(store, monkeypatch)
    monkeypatch.setattr(pnl_snapshot, "render_svg", lambda points: f"<svg><text>{SOURCE_STATEMENT}</text></svg>")
    assert pnl_snapshot.render_only()["status"] == "ok_render_only"
    event = {"ts": "2026-11-01T12:00:00+00:00", "benchmark": "synthetic",
             "user_pct": 5, "bench_pct": 1, "diff_pct": 4, "label": "synthetic client event"}
    pnl_snapshot._persist_outperform([event])
    before = _files(owner)
    with _client(owner, monkeypatch) as client:
        chart = client.get("/api/pnl_chart.svg")
        assert chart.status_code == 200 and chart.text == pnl_snapshot.SVG_PATH.read_text()
        history = client.get("/api/pnl_history")
        assert history.status_code == 200 and history.json()["count"] == 2
        assert history.json()["points"][-1]["total_pnl_pct"] == 5
        events = client.get("/api/outperform_events")
        assert events.status_code == 200 and events.json()["count"] == 1
        assert events.json()["events"][0] == event
    assert _files(owner) == before


def test_modified_svg_is_unverified_even_with_a_current_qualified_ledger_and_points(tmp_path, monkeypatch):
    owner, store = _world(tmp_path, monkeypatch, "client")
    _calculate_points(store, monkeypatch)
    monkeypatch.setattr(pnl_snapshot, "render_svg", lambda points: f"<svg><text>{SOURCE_STATEMENT}</text></svg>")
    assert pnl_snapshot.render_only()["status"] == "ok_render_only"
    pnl_snapshot.SVG_PATH.write_text("<svg>unbound replacement</svg>")
    before = _files(owner)
    with _client(owner, monkeypatch) as client:
        assert client.get("/api/pnl_chart.svg").status_code == 409
        assert client.get("/api/pnl_history").status_code == 200
    assert _files(owner) == before


def test_new_qualified_points_do_not_retroactively_attest_old_svg_or_events(tmp_path, monkeypatch):
    owner, store = _world(tmp_path, monkeypatch, "client")
    _calculate_points(store, monkeypatch)
    before = _files(owner)
    with _client(owner, monkeypatch) as client:
        assert client.get("/api/pnl_history").status_code == 200
        assert client.get("/api/pnl_chart.svg").status_code == 409
        assert client.get("/api/outperform_events").status_code == 409
    assert _files(owner) == before
