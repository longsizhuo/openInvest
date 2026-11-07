import json
import zipfile

import pytest

from openinvest.services.restore_guard import restore_archive
from openinvest.core.decision_evidence import prepare_context


def test_restore_retains_new_exclusions_and_blocks_automatic_use(tmp_path):
    root=tmp_path/"data";integrity=root/"integrity";integrity.mkdir(parents=True)
    (integrity/"exclusions.jsonl").write_text('{"artifact_id":"revoked"}\n')
    (integrity/"eligibility.json").write_text('{"schema":1,"artifacts":{}}')
    archive=tmp_path/"old.zip"
    with zipfile.ZipFile(archive,"w") as z:
        z.writestr("memory/portfolio.md","old portfolio")
        z.writestr("integrity/exclusions.jsonl","")
        z.writestr("integrity/eligibility.json",'{"schema":1,"artifacts":{"revoked":{"allowed_uses":["advice"]}}}')
    result=restore_archive(root,archive)
    assert result["status"] == "restored_pending_reconciliation"
    assert "revoked" in (integrity/"exclusions.jsonl").read_text()
    assert json.loads((integrity/"eligibility.json").read_text())["artifacts"] == {}
    with pytest.raises(ValueError,match="reconciliation"):
        prepare_context(root/"memory","SYNTHETIC",{"price":100})


def test_restore_rejects_paths_outside_data_home(tmp_path):
    archive=tmp_path/"bad.zip"
    with zipfile.ZipFile(archive,"w") as z:z.writestr("../outside","bad")
    with pytest.raises(ValueError,match="unsafe"):
        restore_archive(tmp_path/"data",archive)
    assert not (tmp_path/"outside").exists()


def test_newer_target_revocation_propagates_through_older_backup_graph(tmp_path):
    from openinvest.core.evidence_eligibility import Eligibility
    root=tmp_path/"restored";root.mkdir()
    Eligibility(root).revoke("source:P","withdrawn after backup")
    archive=tmp_path/"older.zip"
    with zipfile.ZipFile(archive,"w") as z:
        z.writestr("integrity/exclusions.jsonl","")
        z.writestr("integrity/eligibility.json",json.dumps({"schema":1,"artifacts":{
            "event:E":{"parents":["source:P"]},"insight:I":{"parents":["event:E"]}}}))
    restore_archive(root,archive)
    assert {"source:P","event:E","insight:I"}<=Eligibility(root).excluded_ids()
    assert json.loads((root/"integrity/eligibility.json").read_text())["artifacts"]=={}


def test_existing_directory_alias_cannot_overwrite_governance_via_memory(tmp_path):
    root=tmp_path/"data";(root/"memory").mkdir(parents=True)
    integrity=root/"integrity";integrity.mkdir()
    before='{"artifact_id":"must-stay-revoked"}\n'
    (integrity/"exclusions.jsonl").write_text(before)
    (root/"memory/alias").symlink_to("../integrity",target_is_directory=True)
    archive=tmp_path/"alias.zip"
    with zipfile.ZipFile(archive,"w") as z:z.writestr("memory/alias/exclusions.jsonl","")
    with pytest.raises(ValueError,match="protected integrity|symlink"):
        restore_archive(root,archive)
    assert (integrity/"exclusions.jsonl").read_text()==before


def test_fresh_restore_preserves_event_revocation_and_spent_windows(tmp_path):
    from datetime import datetime, timezone
    from pathlib import Path
    from openinvest.db.event_store import EventStore
    from openinvest.core.evidence_eligibility import Eligibility, digest
    from openinvest.core.learning_control import ResearchJournal
    from openinvest.jobs.backup import run
    source = tmp_path / "source"
    (source / "memory").mkdir(parents=True)
    store = EventStore(str(source / "db/events.db"))
    event = {"event_id":"revoked-source", "one_line_claim":"synthetic event", "stance":"risk",
             "severity":"high", "affected_symbols":["SYNTHETIC"], "ts":datetime.now(timezone.utc).isoformat()}
    store.upsert_event(event)
    Eligibility(source).revoke("event:revoked-source", "synthetic withdrawal")
    store.conn.close()
    (source / "integrity/final_set_uses.json").write_text(json.dumps({
        "datasets":{"a"*64:"old-trial"},
        "windows":[{"candidate":"old-trial","start":"2026-05-01T00:00:00+00:00","end":"2026-07-01T00:00:00+00:00"}]}))
    archive = Path(run(root=source)["path"])
    restored = tmp_path / "fresh"
    restore_archive(restored,archive)
    # Even a later completed financial reconciliation cannot revive the event.
    (restored / "integrity/restore_pending.json").unlink()
    recovered = EventStore(str(restored / "db/events.db"))
    assert recovered.get_event("revoked-source") is None
    assert recovered.get_event("revoked-source",purpose="audit") is not None
    assert recovered.list_recent() == []
    assert recovered.recall("SYNTHETIC") == []
    recovered.conn.close()
    journal = ResearchJournal(restored)
    (journal.root / "candidates").mkdir()
    (journal.root / "candidates/new-trial.json").write_text(json.dumps({"payload":{},"sha256":digest({})}))
    with pytest.raises(ValueError,match="overlapping"):
        journal.reserve_final_set("b"*64,"new-trial",final_start="2026-06-01T00:00:00+00:00",final_end="2026-08-01T00:00:00+00:00")


def test_legacy_unmerged_wal_cannot_bypass_revocation_reconstruction(tmp_path):
    from openinvest.db.event_store import EventStore
    from datetime import datetime, timezone
    db = tmp_path / "source/events.db"
    store = EventStore(str(db))
    store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    store.upsert_event({"event_id":"wal-only", "one_line_claim":"synthetic", "stance":"risk",
                       "severity":"high", "affected_symbols":["SYNTHETIC"], "ts":datetime.now(timezone.utc).isoformat()})
    archive = tmp_path / "legacy.zip"
    with zipfile.ZipFile(archive,"w") as z:
        z.write(db,"db/events.db")
        z.write(str(db)+"-wal","db/events.db-wal")
    with pytest.raises(ValueError,match="offline consolidation"):
        restore_archive(tmp_path / "fresh",archive)
    assert not (tmp_path / "fresh/db/events.db").exists()
    store.conn.close()
