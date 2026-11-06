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
