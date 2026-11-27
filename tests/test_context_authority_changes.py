from pathlib import Path
import json
import pytest

from openinvest.core.decision_evidence import prepare_context,preparation_state,load_context,_eligibility_version
from openinvest.core.evidence_eligibility import Eligibility,context_authority_snapshot,digest
from openinvest.core.memory_store import MemoryStore
from openinvest.db.event_store import EventStore
from tests.integrity_helpers import approve_event_reporting
from tests.test_evidence_eligibility import approve


def test_harmless_automatic_audit_does_not_invalidate_context_or_its_capture(tmp_path):
    root=tmp_path/'memory';root.mkdir();gate=Eligibility(tmp_path)
    gate.record_audit_lineage('source_snapshot:old',{'original':1},set())
    before=preparation_state(root);context=prepare_context(root,'SYNTHETIC',{})
    raw_version=_eligibility_version(tmp_path)
    gate.record_audit_lineage('source_snapshot:new',{'new_capture':2},set())
    assert raw_version!=_eligibility_version(tmp_path)  # Learning/consent contract is unchanged.
    assert before==preparation_state(root)
    assert load_context(root,context['context_id'],'SYNTHETIC')['context_id']==context['context_id']
    prepare_context(root,'SYNTHETIC',{},expected_state=before)


def test_audit_parent_revocation_still_changes_the_full_negative_closure(tmp_path):
    root=tmp_path/'memory';root.mkdir();gate=Eligibility(tmp_path)
    gate.record_audit_lineage('event:child',{'value':1},set())
    gate.revoke('source_snapshot:withdrawn','source withdrawn')
    context=prepare_context(root,'SYNTHETIC',{})
    gate.record_audit_lineage('event:child',{'value':2},{'source_snapshot:withdrawn'})
    assert 'event:child' in context_authority_snapshot(tmp_path)['denied']
    with pytest.raises(ValueError,match='authority changed'):load_context(root,context['context_id'],'SYNTHETIC')


def test_reviewed_use_changes_remain_authority_changes(tmp_path):
    root=tmp_path/'memory';root.mkdir();approve(tmp_path,'source_report:reviewed',{'body':1})
    context=prepare_context(root,'SYNTHETIC',{})
    path=tmp_path/'integrity/eligibility.json';state=json.loads(path.read_text());state['artifacts']['source_report:reviewed']['allowed_uses']=['audit'];path.write_text(json.dumps(state))
    with pytest.raises(ValueError,match='authority changed'):load_context(root,context['context_id'],'SYNTHETIC')


def test_public_context_load_rechecks_material_event_change_and_original_permission(tmp_path):
    store=MemoryStore(tmp_path/'memory');index=EventStore(str(tmp_path/'db/events.db'))
    index.upsert_event({'event_id':'retained','one_line_claim':'Original observation','stance':'risk','severity':'mid'})
    approve_event_reporting(index,'retained');version=index.get_event('retained')['_version_id']
    class Brief(str):event_versions={'retained':version}
    context=prepare_context(store.root,'SYNTHETIC',{'event_brief':Brief('original')})
    # Mutation of the DB can leave the registry bytes untouched. The original
    # global file-only guard missed this; the referenced view must be rechecked.
    with index.conn:index.conn.execute("UPDATE events SET one_line_claim='Materially different observation' WHERE event_id='retained'")
    with pytest.raises(ValueError,match='referenced_source'):load_context(store.root,context['context_id'],'SYNTHETIC')
    index.conn.close()


def test_unbound_source_capture_can_be_audited_but_cannot_become_current_advice(tmp_path):
    root=tmp_path/'memory';root.mkdir();context=prepare_context(root,'SYNTHETIC',{'event_brief':'unbound analyst claim'})
    with pytest.raises(ValueError,match='source_event_lineage_missing'):load_context(root,context['context_id'],'SYNTHETIC')


def test_missing_governance_ledger_and_malformed_registry_fail_closed(tmp_path):
    root=tmp_path/'memory';root.mkdir();gate=Eligibility(tmp_path)
    gate.record_audit_lineage('source_snapshot:old',{'value':1},set())
    context=prepare_context(root,'SYNTHETIC',{})
    (tmp_path/'integrity/exclusions.jsonl').unlink()
    with pytest.raises(ValueError):load_context(root,context['context_id'],'SYNTHETIC')


def test_reference_audit_deletion_is_not_ignored_as_unrelated_activity(tmp_path):
    store=MemoryStore(tmp_path/'memory');index=EventStore(str(tmp_path/'db/events.db'))
    index.upsert_event({'event_id':'retained','one_line_claim':'Retained','stance':'risk','severity':'mid'})
    approve_event_reporting(index,'retained');version=index.get_event('retained')['_version_id']
    gate=Eligibility(tmp_path);gate.record_audit_lineage('event:retained',{'audit':1},set())
    class Brief(str):event_versions={'retained':version}
    context=prepare_context(store.root,'SYNTHETIC',{'event_brief':Brief('retained')})
    path=tmp_path/'integrity/eligibility.json';state=json.loads(path.read_text());del state['artifacts']['event:retained'];path.write_text(json.dumps(state))
    with pytest.raises(ValueError,match='referenced_audit_lineage_removed'):load_context(store.root,context['context_id'],'SYNTHETIC')
    index.conn.close()


def test_referenced_source_snapshot_is_bound_from_the_versioned_sql_metadata(tmp_path):
    store=MemoryStore(tmp_path/'memory');index=EventStore(str(tmp_path/'db/events.db'))
    index.upsert_event({'event_id':'retained','one_line_claim':'Retained','stance':'risk','severity':'mid'})
    index.add_source('retained',src_name='synthetic',url='https://example.invalid/source',title='Retained',
        snippet='Original text',provenance={'source_snapshot_id':'snapshot'})
    approve_event_reporting(index,'retained');version=index.get_event('retained')['_version_id']
    gate=Eligibility(tmp_path);gate.record_audit_lineage('source_snapshot:snapshot',{'payload':1},set())
    class Brief(str):event_versions={'retained':version}
    context=prepare_context(store.root,'SYNTHETIC',{'event_brief':Brief('retained')})
    assert 'source_snapshot:snapshot' in context['referenced_audit_records']
    path=tmp_path/'integrity/eligibility.json';state=json.loads(path.read_text());del state['artifacts']['source_snapshot:snapshot'];path.write_text(json.dumps(state))
    with pytest.raises(ValueError,match='referenced_audit_lineage_removed'):load_context(store.root,context['context_id'],'SYNTHETIC')
    index.conn.close()
