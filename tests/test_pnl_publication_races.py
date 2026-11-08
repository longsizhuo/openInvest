"""Publication consumes verified buffers and dynamically respects revoked facts."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import subprocess
from unittest.mock import Mock

import pytest

from openinvest.calc.series import BenchmarkSeries
from openinvest.connectors.web_api.routers import read
from openinvest.core.evidence_eligibility import Eligibility
from openinvest.core.portfolio_manager import PortfolioManager
from openinvest.jobs import pnl_snapshot as job
from openinvest.services.balance_observations import record_observation
from openinvest.services.financial_publication import VERIFICATION_PENDING_SVG, qualified_point
from openinvest.services.ledger_evidence import account_source_status, record_tracking_estimate
from tests.test_pnl_publication_routes import _client
from tests.test_pnl_snapshot import _g, pnl_clone, qualified_pnl_ledger  # noqa: F401
from tests.test_pnl_snapshot_evidence import _calculate_points, _files, _world


def _render_verified(store, monkeypatch):
    monkeypatch.setattr(job, "_ensure_benchmarks_fresh", lambda *args: None)
    monkeypatch.setattr(job, "get_all_series", lambda *args: [])
    with monkeypatch.context() as patch:
        patch.setenv("INVEST_PNL_AUTOPUSH", "0")
        assert job.render_only()["status"] == "ok_render_only"
    return job.SVG_PATH.read_bytes()


def _remote_blob(repository, branch):
    return subprocess.run(["git", "show", f"{branch}:docs/pnl_chart.svg"], cwd=repository,
                          check=True, capture_output=True).stdout


def _normal_render_loses_qualification(store, monkeypatch):
    """Real estimate write inside rendering produces B, then the real seal rejects it."""
    original = job.render_svg
    heads = store.root / ".state/pnl_artifact_heads.json"
    previous_heads = heads.read_bytes()
    original_reference = json.loads(previous_heads)[str(job.SVG_PATH.resolve())]
    manifest_path = store.root / ".evidence/pnl_publication/render" / (original_reference["id"] + ".json")
    original_manifest = manifest_path.read_bytes()
    assert json.loads(original_manifest)["payload"]["artifact_utf8"].encode() == job.SVG_PATH.read_bytes()
    day = datetime.now(timezone.utc).date().isoformat()

    def render(points):
        record_tracking_estimate(store, symbol="SYNTHETIC", day=day,
                                 plan={"amount": 100}, quote={"projected_quantity": 20})
        return original(points)

    with monkeypatch.context() as patch:
        patch.setattr(job, "get_all_series", lambda *args: [BenchmarkSeries(
            key="synthetic source change", color="#888888", group="index", dash="", points={day: 1.0})])
        patch.setattr(job, "render_svg", render)
        result = job.render_only()
    assert result["status"] == "unverified"
    assert account_source_status(PortfolioManager(store))["historical_execution_eligible"] is False
    assert heads.read_bytes() == previous_heads
    assert manifest_path.read_bytes() == original_manifest
    return job.SVG_PATH.read_bytes()


def test_http_consumes_a_frozen_svg_when_a_normal_failed_render_replaces_its_path(tmp_path, monkeypatch):
    owner, store = _world(tmp_path, monkeypatch, "client")
    _calculate_points(store, monkeypatch)
    original_bytes = _render_verified(store, monkeypatch)
    real_loader = read.frozen_render_bytes
    replaced = []

    def load_then_render(*args):
        frozen = real_loader(*args)
        replaced.append(_normal_render_loses_qualification(store, monkeypatch))
        return frozen

    monkeypatch.setattr(read, "frozen_render_bytes", load_then_render)
    with _client(owner, monkeypatch) as client:
        response = client.get("/api/pnl_chart.svg")
        assert response.status_code == 200 and response.content == original_bytes
        assert replaced[0] != original_bytes and job.SVG_PATH.read_bytes() == replaced[0]
        assert client.get("/api/pnl_chart.svg").status_code == 409


@pytest.mark.parametrize("branch", ["main", "pnl-data"])
def test_git_interleaving_publishes_only_fixed_notice_and_preserves_user_index(
        pnl_clone, qualified_pnl_ledger, monkeypatch, branch):
    bare, clone = pnl_clone
    monkeypatch.setenv("INVEST_PNL_PUSH_BRANCH", branch)
    monkeypatch.setattr(job, "_ensure_benchmarks_fresh", lambda *args: None)
    monkeypatch.setattr(job, "get_all_series", lambda *args: [])
    (clone / "user-staged.txt").write_text("keep this staged user work\n")
    _g("add", "user-staged.txt", cwd=clone)
    index_before = (clone / ".git/index").read_bytes()
    head_before = _g("rev-parse", "HEAD", cwd=clone)
    original_bytes = job.SVG_PATH.read_bytes()
    real_run = job.subprocess.run
    interleaved, staged_inputs = [], []

    def run(cmd, **kwargs):
        result = real_run(cmd, **kwargs)
        if cmd[1:3] == ["hash-object", "-w"]:
            staged_inputs.append(kwargs.get("input"))
        if cmd[1:3] == ["config", "--get"] and not interleaved:
            interleaved.append(True)
            replaced = _normal_render_loses_qualification(qualified_pnl_ledger, monkeypatch)
            assert replaced != original_bytes
        return result

    monkeypatch.setattr(job.subprocess, "run", run)
    result = job._auto_push_svg()
    assert result["status"] == "unverified" and result["pushed"] is False
    assert VERIFICATION_PENDING_SVG in staged_inputs
    assert job.SVG_PATH.read_bytes() not in staged_inputs
    assert (clone / ".git/index").read_bytes() == index_before
    assert _g("rev-parse", "HEAD", cwd=clone) == head_before
    assert _remote_blob(bare, branch) == VERIFICATION_PENDING_SVG
    assert "user-staged.txt" not in _g("ls-tree", "-r", "--name-only", branch, cwd=bare).splitlines()
    assert _g("diff", "--cached", "--name-only", cwd=clone) == "user-staged.txt"


def test_main_repeated_publications_use_frozen_blobs_without_consuming_user_staged_work(
        pnl_clone, qualified_pnl_ledger, monkeypatch):
    bare, clone = pnl_clone
    monkeypatch.setenv("INVEST_PNL_PUSH_BRANCH", "main")
    (clone / "user-staged.txt").write_text("private staged source change\n")
    _g("add", "user-staged.txt", cwd=clone)
    index_before = (clone / ".git/index").read_bytes()
    head_before = _g("rev-parse", "HEAD", cwd=clone)
    first = job.SVG_PATH.read_bytes()
    assert job._auto_push_svg()["pushed"] is True
    assert _remote_blob(bare, "main") == first
    second = _render_verified(qualified_pnl_ledger, monkeypatch)
    assert second != first and job._auto_push_svg()["pushed"] is True
    assert _remote_blob(bare, "main") == second
    assert job._auto_push_svg()["reason"] == "no svg change"
    assert (clone / ".git/index").read_bytes() == index_before
    assert _g("rev-parse", "HEAD", cwd=clone) == head_before
    assert "user-staged.txt" not in _g("ls-tree", "-r", "--name-only", "main", cwd=bare).splitlines()


def _points_with_superseded_balance(tmp_path, monkeypatch):
    owner, store = _world(tmp_path, monkeypatch, "client")
    def observe(key, parent):
        return record_observation(PortfolioManager(store), "SYNTHETIC", as_of=datetime.now(timezone.utc).isoformat(),
            source_text="Independent synthetic statement " + key, source_locator={"parent_ids": [parent]},
            idempotency_key=key, reported_value=50, value_currency="CNY")
    old = observe("old", "source:pnl-old-balance")
    _calculate_points(store, monkeypatch)
    _render_verified(store, monkeypatch)
    point = job._read_history()[-1]
    job._persist_outperform([{"ts": point["ts"], "benchmark": "synthetic", "user_pct": 5,
                             "bench_pct": 0, "diff_pct": 5, "label": "synthetic event"}])
    observe("new", "source:pnl-new-balance")
    assert account_source_status(PortfolioManager(store))["historical_execution_eligible"] is True
    assert len(job._read_history()) == 2  # An independent new head preserves the original sealed facts.
    return owner, store, old


@pytest.mark.parametrize("damage", ["revoke", "missing", "corrupt"])
def test_old_point_parent_withdrawal_propagates_to_history_svg_and_events(tmp_path, monkeypatch, damage):
    owner, store, old = _points_with_superseded_balance(tmp_path, monkeypatch)
    point = job._read_history()[0]
    parent = store.root / ".evidence/account_observations" / (old["observation_id"] + ".json")
    if damage == "revoke":
        Eligibility(store.data_root).revoke("source:pnl-old-balance", "synthetic source withdrawal")
        assert account_source_status(PortfolioManager(store))["historical_execution_eligible"] is True
    elif damage == "missing":
        parent.unlink()
    else:
        parent.write_text('{"broken":true}')
    before = _files(owner)
    with pytest.raises(ValueError, match="parent unverified"):
        qualified_point(store, point)
    assert job._read_history() == []
    with _client(owner, monkeypatch) as client:
        assert all(client.get(path).status_code == 409 for path in (
            "/api/pnl_history", "/api/pnl_chart.svg", "/api/outperform_events"))
    assert _files(owner) == before


def test_fresh_calculations_can_replace_revoked_derived_outputs_without_rewriting_old_history(tmp_path, monkeypatch):
    owner, store, _ = _points_with_superseded_balance(tmp_path, monkeypatch)
    old_history = job.HISTORY_PATH.read_bytes()
    Eligibility(store.data_root).revoke("source:pnl-old-balance", "synthetic source withdrawal")
    assert account_source_status(PortfolioManager(store))["historical_execution_eligible"] is True
    fresh = _calculate_points(store, monkeypatch, indexes=(100, 106))
    assert len(fresh) == 2 and job.HISTORY_PATH.read_bytes().startswith(old_history)
    _render_verified(store, monkeypatch)
    event = {"ts": fresh[-1]["ts"], "benchmark": "synthetic", "user_pct": 6,
             "bench_pct": 2, "diff_pct": 4, "label": "fresh synthetic event"}
    job._persist_outperform([event])
    with _client(owner, monkeypatch) as client:
        assert client.get("/api/pnl_history").json()["count"] == 2
        assert client.get("/api/pnl_chart.svg").status_code == 200
        response = client.get("/api/outperform_events")
        assert response.status_code == 200 and response.json()["events"] == [event]


@pytest.mark.parametrize("enabled", ["0", "1"])
def test_revoked_points_request_only_code_owned_notice_under_the_existing_publish_switch(tmp_path, monkeypatch, enabled):
    owner, store, _ = _points_with_superseded_balance(tmp_path, monkeypatch)
    Eligibility(store.data_root).revoke("source:pnl-old-balance", "synthetic source withdrawal")
    monkeypatch.setenv("INVEST_PNL_AUTOPUSH", enabled)
    publish = Mock(return_value={"pushed": True, "publication_kind": "verification_notice"})
    monkeypatch.setattr(job, "_publish_svg_buffer", publish)
    before = _files(owner)
    result = job.render_only()
    assert result["status"] == "unverified"
    if enabled == "1":
        publish.assert_called_once_with(VERIFICATION_PENDING_SVG, notice_only=True)
    else:
        publish.assert_not_called()
    assert _files(owner) == before


def test_notice_publisher_cannot_accept_a_numeric_svg_as_a_status_bypass(tmp_path, monkeypatch):
    _world(tmp_path, monkeypatch, "projection")
    monkeypatch.setenv("INVEST_PNL_AUTOPUSH", "1")
    git = Mock(side_effect=AssertionError("arbitrary notice payload reached Git"))
    monkeypatch.setattr(job.subprocess, "run", git)
    result = job._publish_svg_buffer(b"<svg>unverified profit +99%</svg>", notice_only=True)
    assert result["pushed"] is False and result["reason"] == "invalid_verification_notice_payload"
    assert result["learning_eligible"] is result["historical_performance_eligible"] is False
    git.assert_not_called()


def test_non_trading_skip_still_withdraws_a_revoked_public_curve_when_already_opted_in(tmp_path, monkeypatch):
    owner, store, _ = _points_with_superseded_balance(tmp_path, monkeypatch)
    Eligibility(store.data_root).revoke("source:pnl-old-balance", "synthetic source withdrawal")
    monkeypatch.setenv("INVEST_PNL_AUTOPUSH", "1")
    monkeypatch.setattr(job, "_is_trading_window", lambda: False)
    monkeypatch.setattr(job, "_compute_snapshot", Mock(side_effect=AssertionError("off-hours calculation")))
    publish = Mock(return_value={"pushed": True, "publication_kind": "verification_notice"})
    monkeypatch.setattr(job, "_publish_svg_buffer", publish)
    before = _files(owner)
    result = job.run()
    assert result["status"] == "unverified"
    publish.assert_called_once_with(VERIFICATION_PENDING_SVG, notice_only=True)
    assert _files(owner) == before
