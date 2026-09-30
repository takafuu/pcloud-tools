import json
import threading
from concurrent.futures import ThreadPoolExecutor
import pytest
from test_event_sync import setup, put
from test_sqlite_state import stale_identity_fixture
from pcloud_tools.event_sync import EventSync, read_state
from pcloud_tools.review_worker import ReviewWorker, run_parallel
from pcloud_tools.transfer_state import transfer_tick_lock


def test_review_completes_while_transfer_lane_is_locked(setup):
    cfg,remote,store,path=stale_identity_fixture(setup)
    with transfer_tick_lock(cfg.state_dir,'pushd',blocking=False), transfer_tick_lock(cfg.state_dir,'diffd',blocking=False):
        result=ReviewWorker(cfg,remote).tick()
    assert result['cleared']==1
    assert path not in read_state(cfg)['reviews']
    assert not list(store.queue_rows('diffd',[path]))
    assert not remote.copies and not remote.deletes


def test_stale_cached_review_is_not_resurrected(setup):
    cfg,remote,store,path=stale_identity_fixture(setup)
    engine=EventSync(cfg,remote)
    engine.state['reviews'][path]
    ReviewWorker(cfg,remote).tick()
    engine.save()
    assert path not in read_state(cfg)['reviews']
    assert store.get('summary','event')['review_count']==0


@pytest.mark.parametrize('changed',['event','baseline','review','local'])
def test_result_discarded_when_generation_changes(setup,changed):
    cfg,remote,store,path=stale_identity_fixture(setup)
    def change():
        if changed=='event':store.append('pushd',{'path':path,'action':'upload','event_id':'new-generation'})
        elif changed=='local':put(cfg.core_dir,path,b'changed',300)
        else:
            with store.connection(write=True) as db:
                store.put(db,'baseline' if changed=='baseline' else 'reviews',path,{'new':'generation'})
    remote.before_inventory=change
    result=ReviewWorker(cfg,remote).tick()
    assert result['cleared']==0
    assert list(store.queue_rows('diffd',[path]))
    assert not remote.copies


def test_newer_cloud_is_queued_for_normal_sync_without_review_transfer(setup):
    cfg,remote,store,path=stale_identity_fixture(setup)
    put(remote.root,path,b'new cloud',200)
    result=ReviewWorker(cfg,remote).tick()
    assert result['sync_queued']==1 and not remote.copies
    rows=list(store.queue_rows('diffd',[path]))
    assert rows[0]['event_id']!='old-id-event' and rows[0]['action']=='sync'
    EventSync(cfg,remote).tick(max_records=10)
    assert (cfg.core_dir/path).read_bytes()==b'new cloud'


def test_inventory_failure_preserves_data_and_retry(setup):
    from pcloud_tools.event_sync_remote import SyncError
    cfg,remote,store,path=stale_identity_fixture(setup)
    remote.before_inventory=lambda: (_ for _ in ()).throw(SyncError('failure'))
    assert ReviewWorker(cfg,remote).tick()['status']=='failed'
    assert path in read_state(cfg)['reviews']
    assert list(store.queue_rows('diffd',[path]))
    assert ReviewWorker(cfg,remote).tick()['cleared']==1


def test_two_workers_have_one_capacity_slot(setup):
    cfg,remote,store,path=stale_identity_fixture(setup)
    entered=threading.Event();release=threading.Event()
    def block():
        entered.set();assert release.wait(5)
    remote.before_inventory=block
    with ThreadPoolExecutor(1) as pool:
        running=pool.submit(ReviewWorker(cfg,remote).tick)
        assert entered.wait(5)
        try:assert ReviewWorker(cfg,remote).tick()['status']=='busy'
        finally:release.set()
        assert running.result()['cleared']==1


def test_parallel_lane_starts_before_sync_finishes(setup):
    cfg,remote,store,path=stale_identity_fixture(setup)
    entered=threading.Event()
    remote.before_inventory=entered.set
    def sync():
        assert entered.wait(5)
        return {'results':[]}
    assert run_parallel(cfg,lambda:remote,sync)['review worker']['cleared']==1


def test_newer_local_requeues_then_uploads(setup):
    cfg,remote,store,path=stale_identity_fixture(setup)
    put(cfg.core_dir,path,b'new local',200)
    assert ReviewWorker(cfg,remote).tick()['sync_queued']==1
    EventSync(cfg,remote).tick(max_records=10)
    assert (remote.root/path).read_bytes()==b'new local'


def test_same_time_different_content_stays_reviewable(setup):
    cfg,remote,store,path=stale_identity_fixture(setup)
    put(remote.root,path,b'different',100)
    assert ReviewWorker(cfg,remote).tick()['sync_queued']==1
    assert path not in read_state(cfg)['reviews']
    assert list(store.queue_rows('diffd',[path]))
    assert not remote.copies


def test_stale_modified_review_cannot_overwrite_worker_result(setup):
    cfg,remote,store,path=stale_identity_fixture(setup)
    engine=EventSync(cfg,remote)
    engine.state['reviews'][path]['reason']='stale result'
    ReviewWorker(cfg,remote).tick()
    engine.save()
    assert path not in read_state(cfg)['reviews']
    assert store.get('summary','event')['review_count']==0


def test_unchanged_local_uses_verified_hash_cache(setup,monkeypatch):
    cfg,remote,store,path=stale_identity_fixture(setup)
    import pcloud_tools.review_worker as worker
    def unexpected(*args):raise AssertionError('unchanged content must not be reread')
    monkeypatch.setattr(worker,'local_version',unexpected)
    assert ReviewWorker(cfg,remote).tick()['cleared']==1
