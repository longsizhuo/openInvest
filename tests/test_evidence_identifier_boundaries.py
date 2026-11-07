"""Each filesystem-owning helper validates identifiers before any read or write."""
import pytest

from openinvest.services.agent_queue import acknowledge_context_decision
from openinvest.services.reasoning_review import preserve_submission


@pytest.mark.parametrize("context_id",["../escape","../"+"a"*32,"a"*31+"/","abc","a"*33,None,42])
def test_invalid_context_cannot_leave_the_owned_evidence_namespace(tmp_path,context_id):
    owner=tmp_path/"memory";owner.mkdir()
    outside=tmp_path/"escape.json";outside.write_text("unchanged")
    with pytest.raises(ValueError,match="invalid_context_id"):
        preserve_submission(owner,context_id,"SYNTHETIC","raw","provider","model")
    with pytest.raises(ValueError,match="invalid_context_id"):
        acknowledge_context_decision(owner,context_id,"b"*32)
    assert list(owner.iterdir())==[] and outside.read_text()=="unchanged"
