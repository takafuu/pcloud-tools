import json
import os
import time
import pytest
from test_event_sync import setup, initial, put, event
from pcloud_tools.event_sync import EventSync, Scope
from pcloud_tools.conflict_archive import capture, listing, prune, connection
from pcloud_tools.event_sync_remote import local_version
from pcloud_tools.sync_scope import hard_safety_filter_rules


def archived(cfg, path):
    return list((cfg.core_dir/'.conflict').glob('*/data/'+path))


@pytest.mark.parametrize('priority,expected,loser',[('local',b'local',b'cloud'),('cloud',b'cloud',b'local')])
def test_equal_time_priority_and_losing_version_archive(setup,priority,expected,loser):
    cfg,remote,path=initial(setup);cfg.conflict_same_time=priority
    put(cfg.core_dir,path,b'local',200);put(remote.root,path,b'cloud',200)
    event(cfg,'pushd',path,'upload','both')
    result=EventSync(cfg,remote).tick()
    assert not result['reviews']
    assert (cfg.core_dir/path).read_bytes()==(remote.root/path).read_bytes()==expected
    assert archived(cfg,path)[0].read_bytes()==loser
    assert int((cfg.core_dir/path).stat().st_mtime)==200
    assert not Scope(cfg).allows('.conflict/a')
    assert '- /.conflict/**' in hard_safety_filter_rules(cfg)


@pytest.mark.parametrize('side',['local','cloud'])
def test_delete_wins_over_edit_and_saves_edit(setup,side):
    cfg,remote,path=initial(setup)
    deleted,survivor=(cfg.core_dir,remote.root) if side=='local' else (remote.root,cfg.core_dir)
    (deleted/path).unlink();put(survivor,path,b'edited survivor',300)
    event(cfg,'pushd' if side=='local' else 'diffd',path,'delete','confirmed-deletion')
    assert not EventSync(cfg,remote).tick()['reviews']
    assert not (cfg.core_dir/path).exists() and not (remote.root/path).exists()
    assert archived(cfg,path)[0].read_bytes()==b'edited survivor'


def test_archive_failure_does_not_stop_sync(setup,monkeypatch):
    cfg,remote,path=initial(setup)
    put(cfg.core_dir,path,b'local',200);put(remote.root,path,b'cloud',200)
    import pcloud_tools.conflict_archive as archive
    def fail(*args):raise PermissionError('fixture')
    monkeypatch.setattr(archive,'safe_root',fail)
    event(cfg,'pushd',path,'upload','both')
    result=EventSync(cfg,remote).tick()
    assert (remote.root/path).read_bytes()==b'local'
    assert not result['reviews']
    assert result['conflict archives'][0]['status']=='failed'
    assert json.loads((cfg.state_dir/'conflict-archive-failures.jsonl').read_text())['error_type']=='PermissionError'


def test_edit_during_archive_defers_only_that_path(setup):
    cfg,remote,path=initial(setup)
    put(cfg.core_dir,path,b'local',200);put(remote.root,path,b'cloud',200)
    def edit():
        put(cfg.core_dir,path,b'edited while archiving',400)
        remote.after_copy=None
    remote.after_copy=edit
    event(cfg,'pushd',path,'upload','both')
    other='Documents/other';put(cfg.core_dir,other,b'other',200);event(cfg,'pushd',other,'upload','other')
    result=EventSync(cfg,remote).tick()
    assert (remote.root/path).read_bytes()==b'cloud'
    assert (remote.root/other).read_bytes()==b'other'
    assert any(r['path']==path and r['action']=='waiting' for r in result['results'])


def test_retention_age_and_oldest_capacity_eviction(setup):
    cfg,remote=setup;path='Documents/a';put(cfg.core_dir,path,b'1234')
    cfg.conflict_max_bytes=8
    first=capture(cfg,remote,path,'local',local_version(cfg.core_dir/path))
    second=capture(cfg,remote,path,'local',local_version(cfg.core_dir/path))
    third=capture(cfg,remote,path,'local',local_version(cfg.core_dir/path))
    assert {r['id'] for r in listing(cfg)['records']}=={second['id'],third['id']}
    assert not (cfg.core_dir/'.conflict'/first['id']).exists()
    with connection(cfg) as db:db.execute('UPDATE archives SET created=? WHERE id=?',(time.time()-15*86400,second['id']))
    prune(cfg)
    assert [r['id'] for r in listing(cfg)['records']]==[third['id']]


def test_oversize_file_skips_archive_without_copy(setup):
    cfg,remote=setup;cfg.conflict_max_bytes=2;path='Documents/a';put(cfg.core_dir,path,b'1234')
    result=capture(cfg,remote,path,'local',local_version(cfg.core_dir/path))
    assert result['status']=='failed'
    assert not listing(cfg)['records']


def test_archive_symlink_never_targets_external_directory(setup,tmp_path):
    cfg,remote=setup;outside=tmp_path/'outside';outside.mkdir();(outside/'keep').write_text('keep')
    (cfg.core_dir/'.conflict').symlink_to(outside,target_is_directory=True)
    path='Documents/a';put(cfg.core_dir,path)
    assert capture(cfg,remote,path,'local',local_version(cfg.core_dir/path))['status']=='failed'
    assert list(outside.iterdir())==[outside/'keep']
