"""Missed legacy work stays auditable; closure never impersonates analysis."""
from datetime import datetime, timedelta, timezone
import json
import os

import pytest

from openinvest.core.evidence_eligibility import digest
from openinvest.services import agent_queue as queue


NOW = datetime(2026, 11, 8, 12, tzinfo=timezone.utc)


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    root = tmp_path / "memory"
    folder = root / ".agent_queue"
    folder.mkdir(parents=True)
    monkeypatch.setattr("openinvest.core.memory_store.MEMORY_ROOT", root)
    monkeypatch.setattr(queue, "_now", lambda: NOW)
    record = {"id": "legacy", "symbols": ["SYNTH_A", "SYNTH_B"], "event_ids": ["old-source"],
              "reason": "historical request", "queued_at": (NOW-timedelta(days=2)).isoformat()}
    path = folder / "committees.jsonl"
    path.write_text(json.dumps(record) + "\n")
    return root, path, record


def close(root, record, symbol="SYNTH_A", **kwargs):
    return queue.record_legacy_committee_disposition(record["id"], symbol, operator="synthetic-operator",
        reason="Original request missed; current necessity assessed separately", request_sha256=digest(record),
        memory_root=root, now=NOW, **kwargs)


def test_disposition_is_pair_specific_idempotent_and_keeps_original_and_no_completion(legacy):
    root, path, record = legacy
    original = path.read_bytes()
    modern = {**record, "id": "current", "symbols": ["SYNTH_C"],
              "queued_at": NOW.isoformat(), "event_versions": {"old-source": 3}}
    with path.open("a") as stream:
        stream.write(json.dumps(modern) + "\n")
    before = path.read_bytes()
    receipt = close(root, record, followup_reference={"assessment": "current-assessment"})
    assert close(root, record, followup_reference={"assessment": "current-assessment"}) == receipt
    assert receipt["payload"]["completed"] is False
    assert receipt["payload"]["request_sha256"] == digest(record)
    assert path.read_bytes() == before and path.read_bytes().startswith(original)
    assert not (root / ".agent_queue/committee_completions.jsonl").exists()
    assert [row["symbol"] for row in queue.pending_committee_items(NOW, memory_root=root)] == ["SYNTH_C"]
    assert {row["symbol"] for row in queue.pending_committee_items(NOW, include_expired=True, memory_root=root)} == {"SYNTH_B", "SYNTH_C"}
    view = queue.pending_committees()
    assert view["historical_missed"] == 1 and view["pending"] == 1 and view["expired"] == 1
    assert len((root / ".agent_queue/committee_dispositions.jsonl").read_text().splitlines()) == 1
    if os.name != 'nt':
        assert (root / '.agent_queue/committee_dispositions.jsonl').stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("change", ["fresh", "version_bound", "empty_modern_versions", "wrong_hash", "already_completed"])
def test_operator_cannot_close_active_modern_unbound_or_completed_work(legacy, change):
    root, path, record = legacy
    if change == "fresh":
        record["queued_at"] = NOW.isoformat()
    elif change in ("version_bound", "empty_modern_versions"):
        record["event_versions"] = {"old-source": 1} if change == "version_bound" else {}
    path.write_text(json.dumps(record) + "\n")
    if change == "already_completed":
        (root / ".agent_queue/committee_completions.jsonl").write_text(json.dumps({"request_id": record["id"], "symbol": "SYNTH_A"}) + "\n")
    with pytest.raises(ValueError):
        if change == "wrong_hash":
            queue.record_legacy_committee_disposition(record["id"], "SYNTH_A", operator="operator", reason="review",
                request_sha256="0"*64, memory_root=root, now=NOW)
        else:
            close(root, record)
    assert not (root / ".agent_queue/committee_dispositions.jsonl").exists()


@pytest.mark.parametrize("change", ["receipt", "original", "remove_chain_head"])
def test_corrupt_disposition_cannot_silently_hide_unfinished_work(legacy, change):
    root, path, record = legacy
    close(root, record)
    receipts = root / ".agent_queue/committee_dispositions.jsonl"
    if change == "receipt":
        value = json.loads(receipts.read_text())
        value["payload"]["completed"] = True
        receipts.write_text(json.dumps(value) + "\n")
    elif change == "original":
        path.write_text(json.dumps({**record, "reason": "changed original"}) + "\n")
    else:
        close(root, record, "SYNTH_B")
        receipts.write_text(receipts.read_text().splitlines()[1] + "\n")
    with pytest.raises(ValueError, match="committee_disposition_integrity_failure"):
        queue.pending_committee_items(NOW, include_expired=True, memory_root=root)


def test_expiry_and_polling_alone_never_create_a_disposition(legacy):
    root, _, _ = legacy
    assert queue.pending_committee_items(NOW, include_expired=True, memory_root=root)
    assert queue.committee_dispositions(memory_root=root) == []
    assert not (root / ".agent_queue/committee_completions.jsonl").exists()
