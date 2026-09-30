from types import SimpleNamespace
import pytest
from test_event_sync import setup, put
from test_sqlite_state import migrated
from pcloud_tools.event_sync import EventSync, read_state, ID_MISMATCH
from pcloud_tools.event_sync_watch import append_records
from pcloud_tools.event_sync_remote import SyncError
from pcloud_tools import cli_manual_pull
from pcloud_tools.event_sync_status import saved_summary


def seed(cfg, remote, directory='Documents/folder'):
    (cfg.core_dir/directory).mkdir(parents=True)
    EventSync(cfg, remote).tick()
    store = migrated(cfg)
    append_records(cfg, [{'path': directory, 'action': 'upload'}])
    records = list(store.queue_rows('pushd', [directory]))
    engine = EventSync(cfg, remote)
    engine.hold(directory, 'only regular files may be synchronized automatically', {'pushd': records, 'diffd': []})
    engine.save()
    return store, directory


def test_old_directory_hold_expands_children_without_deleting_remote_only_file(setup):
    cfg, remote = setup
    store, directory = seed(cfg, remote)
    put(cfg.core_dir, directory+'/local.txt', b'local')
    put(remote.root, directory+'/cloud.txt', b'cloud')
    result = EventSync(cfg, remote).tick()
    assert any(r['action']=='directory-expanded' and r['children']==2 for r in result['results'])
    assert directory not in read_state(cfg)['reviews']
    assert not list(store.queue_rows('pushd', [directory]))
    assert {r['path'] for r in store.queue_rows('pushd')} == {directory+'/local.txt', directory+'/cloud.txt'}
    EventSync(cfg, remote).tick()
    assert (remote.root/directory/'local.txt').read_bytes()==b'local'
    assert (cfg.core_dir/directory/'cloud.txt').read_bytes()==b'cloud'
    assert not remote.deletes


def test_directory_enumeration_failure_keeps_old_event_and_review(setup):
    cfg, remote = setup
    store, directory = seed(cfg, remote)
    before = list(store.queue_rows('pushd'))
    original = remote.inventory
    def fail(paths=None, **kwargs):
        if paths is None:
            raise SyncError('listing unavailable')
        return original(paths, **kwargs)
    remote.inventory = fail
    EventSync(cfg, remote).tick()
    assert list(store.queue_rows('pushd'))==before
    assert directory in read_state(cfg)['reviews']
    assert not remote.copies and not remote.deletes


def test_directory_file_collision_is_diagnostic_and_never_overwritten(setup, monkeypatch):
    cfg, remote = setup
    store, directory = seed(cfg, remote)
    put(remote.root, directory, b'cloud file')
    EventSync(cfg, remote).tick()
    cfg.sync_policy='event'; cfg.diffd_download_mode='auto'
    monkeypatch.setattr(cli_manual_pull, 'load_config', lambda _: SimpleNamespace(config=cfg, issues=[]))
    listing=cli_manual_pull.run(SimpleNamespace(manual_command='list'), None)
    assert listing['records']==[] and len(listing['diagnostics'])==1
    assert not listing['diagnostics'][0]['available']
    assert store.get('summary','event')['review_count']==0
    assert store.get('summary','event')['diagnostic_count']==1
    with pytest.raises(SyncError):
        EventSync(cfg, remote).review_preview(directory, 'pull')
    assert (remote.root/directory).read_bytes()==b'cloud file'
    assert not remote.copies and not remote.deletes


def test_choices_rechecks_and_errors_are_separate(setup, monkeypatch):
    cfg, remote=setup
    EventSync(cfg,remote).tick()
    engine=EventSync(cfg,remote)
    engine.hold('Documents/choice','select version',{'pushd':[],'diffd':[]}, {'exists':False}, {'exists':True})
    engine.hold('Documents/error','remote inventory failed',{'pushd':[],'diffd':[]})
    engine.hold('Documents/recheck',ID_MISMATCH,{'pushd':[],'diffd':[]})
    engine.save()
    cfg.sync_policy='event'; cfg.diffd_download_mode='auto'
    monkeypatch.setattr(cli_manual_pull,'load_config',lambda _:SimpleNamespace(config=cfg,issues=[]))
    listing=cli_manual_pull.run(SimpleNamespace(manual_command='list'),None)
    assert [r['path'] for r in listing['records']]==['Documents/choice']
    assert [r['path'] for r in listing['diagnostics']]==['Documents/error']
    assert [r['path'] for r in listing['rechecks']]==['Documents/recheck']
    assert listing['records'][0]['available']
    assert listing['count']==1
    summary=saved_summary(cfg,read_state(cfg))
    assert summary['review_count']==summary['diagnostic_count']==summary['recheck_count']==1


def test_new_directory_generation_is_not_consumed_with_old_event(setup):
    cfg, remote=setup
    store, directory=seed(cfg,remote)
    original=remote.inventory
    def new_event(paths=None, **kwargs):
        if paths is None:
            append_records(cfg,[{'path':directory,'action':'upload','reason':'new event'}])
        return original(paths,**kwargs)
    remote.inventory=new_event
    EventSync(cfg,remote).tick()
    assert any(r.get('reason')=='new event' for r in store.queue_rows('pushd',[directory]))


def test_old_directory_is_repaired_during_long_reconciliation(setup):
    cfg,remote=setup
    store,directory=seed(cfg,remote)
    engine=EventSync(cfg,remote)
    engine.state['reconcile']={'id':'long-running','pending':['Documents/z'+str(i) for i in range(100)],'captured':{'pushd':[],'diffd':[]}}
    engine.save()
    EventSync(cfg,remote).tick(max_records=2)
    assert directory not in read_state(cfg)['reviews']
    assert len(read_state(cfg)['reconcile']['pending'])>90
