from datetime import datetime,timedelta,timezone
import json

from openinvest.core.memory_store import MemoryStore
from openinvest.services.decision_quality import findings
from openinvest.services.review_plans import update_plan,queue_plan_reviews


def test_no_plan_does_not_create_empty_artifact(tmp_path):
    store=MemoryStore(tmp_path/'memory')
    assert update_plan(store,'SYNTHETIC')=={}
    assert not (store.root/'.state/review_plan_SYNTHETIC.json').exists()


def test_empty_legacy_placeholders_not_corruption_but_bad_json_is(tmp_path):
    store=MemoryStore(tmp_path/'memory')
    store.state_set('review_plan_EMPTY',{})
    assert findings(store.root)==[]
    path=store.root/'.state/review_plan_BROKEN.json';path.write_text('{broken')
    assert any(row['key']=='quality:plan_corrupt:review_plan_BROKEN.json' for row in findings(store.root))


def test_unqualified_old_plan_cannot_request_paid_review(tmp_path):
    store=MemoryStore(tmp_path/'memory')
    record={'symbol':'SYNTHETIC','plan_id':'1'*32,'state':'triggered','purpose':'review'}
    assert queue_plan_reviews(store,'SYNTHETIC',[record]) is None
    assert not (store.data_root/'db/notification_outbox.sqlite').exists()


def test_normal_budget_deferral_not_fault_unknown_still_is(tmp_path):
    from openinvest.services.notification_outbox import NotificationOutbox
    now=datetime.now(timezone.utc);store=MemoryStore(tmp_path/'memory');box=NotificationOutbox(store)
    ident=box.enqueue('pending',{'subject':'x','plain':'x','html':'x','discord':'x',
        'followup':{'symbols':['SYNTHETIC'],'event_ids':[]}},category='market_risk',policy='decision',now=now-timedelta(hours=2))
    with box.connection() as db:
        db.execute("UPDATE followups SET status='pending',error='cooldown_or_cost_budget_deferred',defer_reason='cost_budget',next_eligible_at=? WHERE notification_id=?",(now.timestamp()+3600,ident))
    assert not any(row['key'].startswith('quality:followup_uncertain:') for row in findings(store.root))
    with box.connection() as db:
        db.execute("UPDATE followups SET status='unknown',error='dispatch_outcome_unknown' WHERE notification_id=?",(ident,))
    assert any(row['key'].startswith('quality:followup_uncertain:') for row in findings(store.root))
