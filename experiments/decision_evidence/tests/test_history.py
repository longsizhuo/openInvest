import importlib.util
import json
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location("decision_evidence_history", Path(__file__).parents[1] / "history.py")
history = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(history)


def make_doc(root, *, day="2026-01-02", stem="PRIVATE_ASSET", raw="VERDICT: BUY\nCONFIDENCE: 0.95", header="HOLD", conf="0.40", health=None):
    folder = root / day
    folder.mkdir(parents=True, exist_ok=True)
    blocks = [f"# Committee\n**Symbol**: PRIVATE_ASSET\n**Verdict**: {header} (confidence {conf})\n**Run at**: {day}T10:00:00"]
    if health is not None:
        blocks.append("## Input Health (deterministic)\n```json\n" + json.dumps(health) + "\n```")
    blocks.append("## CIO Memo (Round 3)\n" + raw)
    path = folder / (stem + ".md")
    path.write_text("\n\n".join(blocks))
    return path


def test_raw_and_final_are_distinct_without_reapplying_rules(tmp_path):
    root = tmp_path / ".committee"
    make_doc(root, health={"model": "known", "forced_hold": True, "rewritten": ["defense"], "worker_unavailable": []})
    result = history.audit_history(root, cutoff="2026-01-02")
    counts = result["summary"]["counts"]
    assert counts["verdict_differs"] == counts["confidence_differs_beyond_header_rounding"] == 1
    assert counts["rule_hold_evidence"] == 1
    assert result["private_records"][0]["raw_proposal"]["confidence"] == .95
    assert counts["historical_replay_eligible"] == counts["probability_score_eligible"] == 0
    assert "PRIVATE_ASSET" not in json.dumps(result["summary"])
    assert str(tmp_path) not in json.dumps(result["summary"])


def test_header_echo_in_memo_is_not_trusted_metadata(tmp_path):
    root = tmp_path / ".committee"
    path = make_doc(root)
    path.write_text("# Committee\n## CIO Memo\n**Verdict**: BUY (confidence 0.95)\nVERDICT: BUY\nCONFIDENCE: 0.95")
    result = history.audit_history(root)
    assert result["summary"]["counts"]["header_missing"] == 1
    assert not result["private_records"][0]["paired_observation_eligible"]


def test_conflicting_json_and_field_lines_are_excluded(tmp_path):
    root = tmp_path / ".committee"
    make_doc(root, raw='```json\n{"verdict":"BUY","confidence":0.7}\n```\nVERDICT: HOLD\nCONFIDENCE: 0.4')
    result = history.audit_history(root)
    assert result["summary"]["counts"]["raw_proposal_conflicting_sources"] == 1
    assert not result["private_records"][0]["paired_observation_eligible"]


def test_missing_raw_fields_are_not_recovered_from_header(tmp_path):
    root = tmp_path / ".committee"
    make_doc(root, raw="An unstructured narrative without event or confidence fields.")
    result = history.audit_history(root)
    assert result["summary"]["counts"]["raw_proposal_unparseable"] == 1
    assert result["summary"]["counts"]["model_unknown"] == 1
    assert result["private_records"][0]["raw_proposal"]["confidence"] is None


def test_same_day_sidecars_are_weak_and_conflicts_exclude(tmp_path):
    root = tmp_path / ".committee"
    path = make_doc(root)
    (path.parent / "PRIVATE_ASSET_committee.json").write_text(json.dumps({"symbol": "PRIVATE_ASSET", "verdict": {"verdict": "BUY", "confidence": .95}}))
    (path.parent / "PRIVATE_ASSET_path.json").write_text(json.dumps({"symbol": "PRIVATE_ASSET", "profile": {}}))
    result = history.audit_history(root)
    record = result["private_records"][0]
    assert record["source_conflict"]
    assert not record["paired_observation_eligible"]
    assert record["sidecars"]["path"]["association"] == "weak_date_symbol"
    assert not record["sidecars"]["path"]["same_run_verified"]


def test_never_reads_archives_or_blind_experiments_and_honors_cutoff(tmp_path, monkeypatch):
    root = tmp_path / ".committee"
    path = make_doc(root)
    make_doc(root, day="2026-01-03")
    (path.parent / "runs").mkdir()
    archived = path.parent / "runs" / "PRIVATE_ASSET_100000.md"
    archived.write_text("MUST NOT READ")
    blind = root / ".backtest_sealed"
    blind.mkdir()
    (blind / "secret.md").write_text("MUST NOT READ")
    original = Path.read_bytes
    def guarded(self):
        assert "runs" not in self.parts and ".backtest_sealed" not in self.parts
        return original(self)
    monkeypatch.setattr(Path, "read_bytes", guarded)
    result = history.audit_history(root, cutoff="2026-01-02")
    assert result["summary"]["counts"]["documents"] == 1
    assert result["summary"]["independent_sample_count"] is None


def test_duplicate_day_asset_not_counted_as_independent(tmp_path):
    root = tmp_path / ".committee"
    make_doc(root)
    make_doc(root, stem="ALIAS")
    result = history.audit_history(root)
    assert result["summary"]["counts"]["duplicate_day_asset_groups"] == 1
    assert not any(r["paired_observation_eligible"] for r in result["private_records"])


def test_execution_declaration_is_not_fill_and_fault_hold_is_separate(tmp_path):
    root = tmp_path / ".committee"
    make_doc(root, raw="VERDICT: HOLD\nCONFIDENCE: 0.4\n[WORKER_UNAVAILABLE] reason=synthetic", health={"model":"unknown", "forced_hold":True, "worker_unavailable":["quant"], "rewritten":["worker"]})
    ledger = tmp_path / ".dreams"
    ledger.mkdir()
    (ledger / "executions.jsonl").write_text(json.dumps({"decision_id":"2026-01-02/PRIVATE_ASSET", "executed":True, "trade_ids":[1]})+"\n")
    result = history.audit_history(root)
    assert result["summary"]["counts"]["fault_hold_evidence"] == 1
    assert result["summary"]["counts"]["rule_hold_evidence"] == 0
    assert result["summary"]["counts"]["actual_execution_verified"] == 0
    assert result["private_records"][0]["execution_declaration"]["evidence_type"] == "user_declaration_only"


def test_mutation_is_a_hard_error(tmp_path, monkeypatch):
    root = tmp_path / ".committee"
    target = make_doc(root)
    original = Path.read_bytes
    calls = 0
    def unstable(self):
        nonlocal calls
        value = original(self)
        if self == target:
            calls += 1
            if calls >= 2:
                return value + b"\nchanged"
        return value
    monkeypatch.setattr(Path, "read_bytes", unstable)
    with pytest.raises(history.HistoryAuditError, match="changed"):
        history.audit_history(root)


def test_blind_root_and_symlink_refused(tmp_path):
    root = tmp_path / ".backtest_debate"
    root.mkdir()
    with pytest.raises(history.HistoryAuditError, match="blind"):
        history.audit_history(root)
    safe = tmp_path / ".committee"
    safe.mkdir()
    link = tmp_path / "alias"
    link.symlink_to(safe, target_is_directory=True)
    with pytest.raises(history.HistoryAuditError, match="symlink"):
        history.audit_history(link)


@pytest.mark.parametrize("model", [" unknown ", " UNKNOWN ", " none ", " null ", "  "])
def test_whitespace_sentinels_are_unknown_models(tmp_path, model):
    root = tmp_path / ".committee"
    make_doc(root, health={"model": model})
    result = history.audit_history(root)
    assert result["summary"]["counts"]["model_unknown"] == 1
    assert result["private_records"][0]["model"] == "unknown"


@pytest.mark.parametrize("timestamp", ["not-a-timestamp", "2026-01-02", "2026-02-30T10:00:00"])
def test_invalid_run_timestamp_does_not_qualify_a_pair(tmp_path, timestamp):
    root = tmp_path / ".committee"
    path = make_doc(root)
    path.write_text(path.read_text().replace("2026-01-02T10:00:00", timestamp))
    result = history.audit_history(root)
    counts = result["summary"]["counts"]
    assert counts["run_timestamp_invalid"] == 1
    assert counts["run_timestamp_present"] == 0
    assert not result["private_records"][0]["paired_observation_eligible"]


def test_orphan_invalid_sidecar_is_counted_and_hashed(tmp_path):
    root = tmp_path / ".committee"
    path = make_doc(root)
    orphan = path.parent / "ORPHAN_path.json"
    orphan.write_bytes(b"\xffbroken")
    unrelated = path.parent / "secret_experiment.json"
    unrelated.write_text("MUST NOT READ")
    result = history.audit_history(root)
    counts = result["summary"]["counts"]
    assert counts["path_sidecar_present"] == counts["path_sidecar_orphan"] == counts["path_sidecar_invalid"] == 1
    assert any(source["path"] == str(orphan) and len(source["sha256"]) == 64 for source in result["private_sources"])
    assert not any(source["path"] == str(unrelated) for source in result["private_sources"])


def test_invalid_utf8_document_is_in_inventory_but_ineligible(tmp_path):
    root = tmp_path / ".committee"
    path = make_doc(root)
    path.write_bytes(b"\xff\xfe malformed PRIVATE_ASSET")
    result = history.audit_history(root)
    assert result["summary"]["counts"]["documents"] == 1
    assert result["summary"]["counts"]["encoding_invalid"] == 1
    record = result["private_records"][0]
    assert record["encoding_status"] == "invalid"
    assert len(record["source_sha256"]) == 64
    assert not record["paired_observation_eligible"]


def test_symlink_ancestor_refused_before_resolution(tmp_path):
    real = tmp_path / "real"
    root = real / "memory" / ".committee"
    make_doc(root)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(history.HistoryAuditError, match="ancestor"):
        history.audit_history(alias / "memory" / ".committee")


@pytest.mark.parametrize("change", ["add", "remove"])
def test_inventory_change_is_a_hard_error(tmp_path, monkeypatch, change):
    root = tmp_path / ".committee"
    path = make_doc(root)
    orphan = path.parent / "ORPHAN_path.json"
    if change == "remove":
        orphan.write_text("{}")
    original = history._read_stable
    changed = False
    def mutate_after_read(source, sources):
        nonlocal changed
        data = original(source, sources)
        if source == path and not changed:
            changed = True
            if change == "add":
                orphan.write_text("{}")
            else:
                orphan.unlink()
        return data
    monkeypatch.setattr(history, "_read_stable", mutate_after_read)
    with pytest.raises(history.HistoryAuditError, match="inventory|disappeared"):
        history.audit_history(root)


def test_sidecar_run_id_disagreement_is_conflict(tmp_path):
    root = tmp_path / ".committee"
    path = make_doc(root)
    path.write_text(path.read_text().replace("# Committee", "# Committee\n**Run ID**: original-run", 1))
    (path.parent / "PRIVATE_ASSET_path.json").write_text(json.dumps({"symbol":"PRIVATE_ASSET", "run_id":"different-run"}))
    result = history.audit_history(root)
    assert result["summary"]["counts"]["path_sidecar_conflict"] == 1
    assert not result["private_records"][0]["paired_observation_eligible"]


def test_explicit_invalid_sidecar_confidence_is_conflict(tmp_path):
    root = tmp_path / ".committee"
    path = make_doc(root)
    (path.parent / "PRIVATE_ASSET_committee.json").write_text(json.dumps({"symbol":"PRIVATE_ASSET", "verdict":{"verdict":"HOLD", "confidence":"bogus"}}))
    result = history.audit_history(root)
    assert result["summary"]["counts"]["committee_sidecar_invalid_fields"] == 1
    assert result["private_records"][0]["source_conflict"]


@pytest.mark.parametrize("workers", ["false", False, ["invented-role"], [False]])
def test_invalid_worker_health_is_not_structured_failure(tmp_path, workers):
    root = tmp_path / ".committee"
    make_doc(root, raw="VERDICT: HOLD\nCONFIDENCE: 0.4\n[WORKER_UNAVAILABLE] reason=quoted-example", health={"worker_unavailable":workers, "forced_hold":True, "rewritten":["worker"]})
    result = history.audit_history(root)
    counts = result["summary"]["counts"]
    assert counts["body_marker_present"] == 1
    assert counts["worker_failure_field_invalid"] == 1
    assert counts["worker_failure_reported"] == counts["fault_hold_evidence"] == 0
    assert counts["hold_cause_unknown"] == 1
    assert not result["private_records"][0]["worker_failure_evidence"]


def test_body_marker_alone_never_proves_fault_hold(tmp_path):
    root = tmp_path / ".committee"
    make_doc(root, raw="VERDICT: HOLD\nCONFIDENCE: 0.4\n[WORKER_UNAVAILABLE] reason=quoted-example")
    result = history.audit_history(root)
    assert result["summary"]["counts"]["body_marker_present"] == 1
    assert result["summary"]["counts"]["fault_hold_evidence"] == 0
    assert result["summary"]["counts"]["hold_cause_unknown"] == 1


@pytest.mark.parametrize("amount, expected", [("¥10,000", 10000), ("-¥3,000", -3000), ("AUD 500", 500), ("1e4", 10000), ("0", 0)])
def test_raw_text_allocation_is_private_single_finite_number(tmp_path, amount, expected):
    root = tmp_path / ".committee"
    make_doc(root, raw="VERDICT: BUY\nCONFIDENCE: 0.95\nSUGGESTED_ALLOC_AUD: " + amount)
    result = history.audit_history(root)
    raw = result["private_records"][0]["raw_proposal"]
    assert raw["status"] == "parsed"
    assert raw["alloc_cny"] == expected
    assert raw["alloc_currency"] == "AUD"
    assert result["summary"]["counts"]["raw_allocation_present"] == 1
    assert "AUD" not in json.dumps(result["summary"])


@pytest.mark.parametrize("tail", ["SUGGESTED_ALLOC_CNY: 1,00", "SUGGESTED_ALLOC_CNY: 1 and 2", "SUGGESTED_ALLOC_CNY: NaN", "SUGGESTED_ALLOC_CNY: 1e9999", "SUGGESTED_ALLOC_CNY: 1\nSUGGESTED_ALLOC_AUD: 2", ""])
def test_missing_or_ambiguous_amount_never_changes_raw_parse_status(tmp_path, tail):
    root = tmp_path / ".committee"
    make_doc(root, raw="VERDICT: BUY\nCONFIDENCE: 0.95\n" + tail)
    result = history.audit_history(root)
    raw = result["private_records"][0]["raw_proposal"]
    assert raw["status"] == "parsed"
    assert raw["alloc_cny"] is None
    assert result["summary"]["counts"]["raw_allocation_present"] == 0


@pytest.mark.parametrize("fields, amount, currency", [({"suggested_alloc_cny": "1,000"}, 1000, "CNY"), ({"alloc_cny": 500}, 500, None), ({"alloc_cny":500, "alloc_currency":"AUD"}, 500, "AUD"), ({"alloc_cny":True}, None, None), ({"alloc_cny":float("inf")}, None, None), ({"alloc_cny":1,"suggested_alloc_aud":1}, None, None)])
def test_raw_json_amount_is_extracted_without_header_fallback(tmp_path, fields, amount, currency):
    root = tmp_path / ".committee"
    make_doc(root, raw=json.dumps({"verdict":"BUY", "confidence":0.95, **fields}))
    result = history.audit_history(root)
    raw = result["private_records"][0]["raw_proposal"]
    assert raw["status"] == "parsed"
    assert raw["alloc_cny"] == amount
    assert raw["alloc_currency"] == currency
