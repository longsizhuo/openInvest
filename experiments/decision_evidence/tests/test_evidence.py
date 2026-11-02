import json
from pathlib import Path

import pytest

from experiments.decision_evidence.evidence import canonical_bytes, digest, freeze_history, read_bundle, verify_history, write_bundle


def payload():
    return {"run_id": "attempt-001", "evidence_type": "synthetic", "inputs": {"observed": "fixed"},
            "proposal": {"action": "ACCUMULATE"}, "validated": {"action": "HOLD"}, "execution": None}


def test_run_retry_is_immutable_and_all_stages_are_preserved(tmp_path):
    original = payload()
    path = write_bundle(tmp_path, original)
    assert read_bundle(path) == original
    assert write_bundle(tmp_path, original) == path
    changed = {**original, "validated": {"action": "ACCUMULATE"}}
    with pytest.raises(ValueError, match="different evidence"):
        write_bundle(tmp_path, changed)
    assert read_bundle(path) == original


def test_tamper_and_identity_substitution_are_detected(tmp_path):
    path = write_bundle(tmp_path, payload())
    doc = json.loads(path.read_text())
    doc["payload"]["execution"] = {"status": "filled"}
    path.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match="integrity"):
        read_bundle(path)


def test_missing_stage_cannot_be_misreported_as_complete(tmp_path):
    p = payload()
    del p["execution"]
    with pytest.raises(ValueError, match="missing evidence stage"):
        write_bundle(tmp_path, p)


def test_correct_hash_does_not_make_an_incomplete_payload_valid(tmp_path):
    p = {"run_id": "attempt-001"}
    path = tmp_path / "attempt-001.json"
    path.write_bytes(canonical_bytes({"schema_version": 1, "payload": p,
                                     "payload_sha256": digest(canonical_bytes(p))}))
    with pytest.raises(ValueError, match="missing evidence stage"):
        read_bundle(path)


def test_snapshot_excludes_future_and_blinded_archived_runs(tmp_path):
    source = tmp_path / ".committee"
    day = source / "2026-10-27"
    day.mkdir(parents=True)
    (day / "EXAMPLE.md").write_text("private source")
    (day / "EXAMPLE_path.json").write_text("{}")
    (day / "runs").mkdir()
    (day / "runs" / "EXAMPLE.md").write_text("do not read")
    (source / "2026-10-29").mkdir()
    (source / "2026-10-29" / "EXAMPLE.md").write_text("not in cutoff")
    target = tmp_path / "capture"
    manifest = freeze_history(source, target, cutoff="2026-10-28")
    assert {x["path"] for x in manifest["files"]} == {
        "2026-10-27/EXAMPLE.md", "2026-10-27/EXAMPLE_path.json"}
    assert verify_history(source, target)
    (day / "EXAMPLE.md").write_text("changed")
    with pytest.raises(ValueError, match="integrity"):
        verify_history(source, target)


def test_snapshot_and_source_cannot_overlap_or_follow_symlink(tmp_path):
    source = tmp_path / ".committee"
    source.mkdir()
    with pytest.raises(ValueError, match="overlap"):
        freeze_history(source, source / "capture", cutoff="2026-10-28")
    external = tmp_path / "elsewhere"
    external.mkdir()
    day = source / "2026-10-27"
    day.mkdir()
    (day / "EXAMPLE.md").symlink_to(external)
    with pytest.raises(ValueError, match="symlink"):
        freeze_history(source, tmp_path / "capture", cutoff="2026-10-28")


def test_execution_declarations_are_frozen_without_claiming_fills(tmp_path):
    source = tmp_path / ".committee"
    source.mkdir()
    ledger = tmp_path / ".dreams" / "executions.jsonl"
    ledger.parent.mkdir()
    ledger.write_text('{"executed":true}\n')
    snapshot = tmp_path / "capture"
    manifest = freeze_history(source, snapshot, cutoff="2026-10-28")
    assert manifest["files"][0]["kind"] == "declarations"
    assert (snapshot / ".dreams" / "executions.jsonl").read_bytes() == ledger.read_bytes()
    assert verify_history(source, snapshot)


@pytest.mark.parametrize("blind_parent,blind_name", [("sealed", "EXAMPLE.md"), ("live", "EXAMPLE__sealed.md")])
def test_blinded_sources_are_rejected_before_any_content_read(tmp_path, monkeypatch, blind_parent, blind_name):
    source = tmp_path / blind_parent / ".committee"
    day = source / "2026-10-27"
    day.mkdir(parents=True)
    forbidden = day / blind_name
    forbidden.write_text("must never read this")
    real_read = Path.read_bytes
    reads = []
    def spy(path):
        reads.append(path)
        return real_read(path)
    monkeypatch.setattr(Path, "read_bytes", spy)
    with pytest.raises(ValueError, match="blind-experiment"):
        freeze_history(source, tmp_path / "capture", cutoff="2026-10-28")
    assert forbidden not in reads


@pytest.mark.parametrize("change", ["schema", "duplicate", "size", "sha"])
def test_manifest_structure_is_validated_not_just_file_content(tmp_path, change):
    source = tmp_path / ".committee"
    day = source / "2026-10-27"
    day.mkdir(parents=True)
    (day / "EXAMPLE.md").write_text("synthetic")
    snapshot = tmp_path / "capture"
    manifest = freeze_history(source, snapshot, cutoff="2026-10-28")
    if change == "schema":
        manifest["schema_version"] = 999
    elif change == "duplicate":
        manifest["files"].append(manifest["files"][0])
    elif change == "size":
        manifest["files"][0]["size"] = True
    else:
        manifest["files"][0]["sha256"] = "not-a-digest"
    (snapshot / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="manifest"):
        verify_history(source, snapshot)


def test_snapshot_cannot_include_additional_unfrozen_records(tmp_path):
    source = tmp_path / ".committee"
    day = source / "2026-10-27"
    day.mkdir(parents=True)
    (day / "EXAMPLE.md").write_text("synthetic")
    snapshot = tmp_path / "capture"
    freeze_history(source, snapshot, cutoff="2026-10-28")
    (snapshot / "files" / day.name / "EXTRA.md").write_text("not in frozen cohort")
    with pytest.raises(ValueError, match="snapshot inventory"):
        verify_history(source, snapshot)
