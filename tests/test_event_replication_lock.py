"""All public replication callers share one nonblocking, stable destination lock."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Event
import time

import pytest

from openinvest.services import event_replication as replication
from tests.test_event_replication_performance import reviewed_batch


def assert_skipped(result):
    assert result['status']=='skipped' and result['completed'] is False
    assert result['reason']=='target_replication_lock_held'
    assert all(result[key]==0 for key in ('imported_public_events','propagated_exclusions','publication_review_required'))


def test_identical_roots_are_rejected_before_creating_a_lock_or_opening_a_database(tmp_path,monkeypatch):
    root=tmp_path/'absent'
    monkeypatch.setattr(replication,'EventStore',lambda *a,**kw:pytest.fail('database accessed before root validation'))
    with pytest.raises(ValueError,match='must differ'):
        replication.sync_public_events(root,root,{'reuters.com'})
    assert not root.exists()


@pytest.mark.parametrize('different_source',[False,True])
def test_threads_skip_without_database_side_effects(tmp_path,monkeypatch,different_source):
    source,target=tmp_path/'source',tmp_path/'target';reviewed_batch(source,1)
    entered,release=Event(),Event();owned=replication._sync_public_events_locked
    def paused(*args):
        entered.set()
        assert release.wait(timeout=10)
        return owned(*args)
    monkeypatch.setattr(replication,'_sync_public_events_locked',paused)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(replication.sync_public_events,source,target,{'reuters.com'})
        try:
            assert entered.wait(timeout=5)
            guard=target/replication._SYNC_LOCK_NAME;inode=guard.stat().st_ino
            with monkeypatch.context() as scoped:
                scoped.setattr(replication,'EventStore',lambda *a,**kw:pytest.fail('busy caller opened a database'))
                contender=tmp_path/'another-source' if different_source else source
                second=pool.submit(replication.sync_public_events,contender,target,{'reuters.com'})
                assert_skipped(second.result(timeout=3))
            assert list(target.iterdir())==[guard] and not (target/'db').exists()
            assert guard.stat().st_ino==inode and guard.read_bytes()==b''
        finally:release.set()
        assert first.result(timeout=10)['imported_public_events']==1
    assert guard.stat().st_ino==inode
    again=replication.sync_public_events(source,target,{'reuters.com'})
    assert again['completed'] is True and again['imported_public_events']==0
    assert guard.stat().st_ino==inode


@pytest.mark.parametrize('exit_mode',['release','crash'])
def test_real_subprocess_lock_is_nonblocking_and_survives_release_or_crash(tmp_path,monkeypatch,exit_mode):
    source,target=tmp_path/'source',tmp_path/'target';reviewed_batch(source,1)
    ready=tmp_path/'child-ready'
    code='''
import json,sys
from pathlib import Path
from openinvest.services import event_replication as r
owned=r._sync_public_events_locked
def pause(*args):
    Path(sys.argv[3]).write_text('locked')
    sys.stdin.readline()
    return owned(*args)
r._sync_public_events_locked=pause
print(json.dumps(r.sync_public_events(Path(sys.argv[1]),Path(sys.argv[2]),{'reuters.com'})),flush=True)
'''
    child=subprocess.Popen([sys.executable,'-c',code,str(source),str(target),str(ready)],
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
        env={**os.environ,'INVEST_HOME':str(tmp_path/'child-home')})
    try:
        deadline=time.monotonic()+10
        while not ready.exists():
            if child.poll() is not None:pytest.fail(child.communicate()[1])
            if time.monotonic()>=deadline:pytest.fail('child did not acquire the lock')
            time.sleep(.01)
        guard=target/replication._SYNC_LOCK_NAME;inode=guard.stat().st_ino
        with monkeypatch.context() as scoped:
            scoped.setattr(replication,'EventStore',lambda *a,**kw:pytest.fail('subprocess contention opened a database'))
            for contender in (source,tmp_path/'different-source'):
                assert_skipped(replication.sync_public_events(contender,target,{'reuters.com'}))
        assert list(target.iterdir())==[guard] and guard.stat().st_ino==inode
        if exit_mode=='crash':
            child.kill();child.communicate(timeout=10)
            assert child.returncode!=0
        else:
            stdout,stderr=child.communicate('continue\n',timeout=10)
            assert child.returncode==0,stderr
            assert json.loads(stdout)['imported_public_events']==1
        assert guard.exists() and guard.stat().st_ino==inode
        result=replication.sync_public_events(source,target,{'reuters.com'})
        assert result['status']=='completed' and result['completed'] is True
        assert result['imported_public_events']==(1 if exit_mode=='crash' else 0)
        assert guard.stat().st_ino==inode and guard.read_bytes()==b''
    finally:
        if child.poll() is None:child.kill();child.communicate(timeout=10)


def test_an_exception_releases_the_same_lock_inode(tmp_path,monkeypatch):
    source,target=tmp_path/'source',tmp_path/'target';reviewed_batch(source,1)
    owned=replication._sync_public_events_locked
    def failed(*args):raise ValueError('synthetic failure after acquiring the lock')
    monkeypatch.setattr(replication,'_sync_public_events_locked',failed)
    with pytest.raises(ValueError,match='synthetic failure'):
        replication.sync_public_events(source,target,{'reuters.com'})
    guard=target/replication._SYNC_LOCK_NAME;inode=guard.stat().st_ino
    monkeypatch.setattr(replication,'_sync_public_events_locked',owned)
    assert replication.sync_public_events(source,target,{'reuters.com'})['imported_public_events']==1
    assert guard.exists() and guard.stat().st_ino==inode
