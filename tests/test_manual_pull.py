import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pcloud_tools import conflict_resolution as cr, manual_pull as mp
from pcloud_tools.io_utils import atomic_write_json
from pcloud_tools.transfer_state import read_attempts, unresolved_attempts


class Remote:
    content = b'cloud version'
    callback = None

    def inspect(self, path):
        return {'id': 'file1', 'size': len(self.content), 'modified': '2026-09-01T00:00:00Z',
                'hashes': {'sha1': hashlib.sha1(self.content).hexdigest()}}

    def backup(self, path, destination, expected):
        destination.write_bytes(self.content)
        if self.callback:
            self.callback()


@pytest.fixture
def state(tmp_path):
    config = SimpleNamespace(core_dir=tmp_path/'core', state_dir=tmp_path/'state', core_remote='fixture:core',
                             download_suppression_ttl_seconds=86400)
    local = config.core_dir/'Documents'/'quotes " 日本語.txt'
    local.parent.mkdir(parents=True)
    local.write_bytes(b'local version')
    path = local.relative_to(config.core_dir).as_posix()
    files = cr.queue_paths(config)
    atomic_write_json(files['diffd'], [{'path': path, 'action': 'download', 'event_id': 'd1'},
                                      {'path': 'unrelated', 'action': 'download', 'event_id': 'other'}])
    atomic_write_json(files['pushd'], [])
    return config, path, local, Remote(), lambda path: None


@pytest.mark.parametrize('exists', [True, False])
def test_pull_exact_version_preserves_original_and_unrelated(state, exists):
    config,path,local,remote,validate = state
    if not exists:
        local.unlink()
    token = mp.preview(config,path,'pull',remote,validate)['token']
    result = mp.apply(config,path,'pull',token,remote,validate)
    assert local.read_bytes() == remote.content
    assert local.stat().st_mtime == 1788220800
    backup = Path(result['backup_directory'])
    assert (backup/'cloud').read_bytes() == remote.content
    assert (backup/'local').exists() == exists
    if exists:
        assert (backup/'local').read_bytes() == b'local version'
    assert [r['event_id'] for r in json.loads(cr.queue_paths(config)['diffd'].read_text())] == ['other']
    assert not unresolved_attempts(config.state_dir,'diffd')


def test_local_choice_enqueues_upload_without_modifying_either_file(state):
    config,path,local,remote,validate=state
    token=mp.preview(config,path,'local',remote,validate)['token']
    result=mp.apply(config,path,'local',token,remote,validate)
    assert result['status']=='upload-queued'
    assert local.read_bytes()==b'local version'
    assert remote.content==b'cloud version'
    queued=json.loads(cr.queue_paths(config)['pushd'].read_text())
    assert len(queued)==1 and queued[0]['path']==path and queued[0]['action']=='upload'


@pytest.mark.parametrize('changed',['local','cloud','queue'])
def test_stale_token_does_not_write_files_or_attempts(state,changed):
    config,path,local,remote,validate=state
    token=mp.preview(config,path,'pull',remote,validate)['token']
    if changed=='local':local.write_bytes(b'new local')
    elif changed=='cloud':remote.content=b'new cloud'
    else:
        file=cr.queue_paths(config)['diffd'];items=json.loads(file.read_text());items[0]['event_id']='d2';atomic_write_json(file,items)
    before=local.read_bytes()
    with pytest.raises(cr.ResolutionError,match='changed'):
        mp.apply(config,path,'pull',token,remote,validate)
    assert local.read_bytes()==before
    assert not read_attempts(config.state_dir,'diffd')


@pytest.mark.parametrize('changed',['local','cloud','queue'])
def test_change_during_backup_retains_files_and_queue(state,changed):
    config,path,local,remote,validate=state
    token=mp.preview(config,path,'pull',remote,validate)['token']
    def mutate():
        if changed=='local':local.write_bytes(b'new local')
        elif changed=='cloud':remote.content=b'new cloud'
        else:
            file=cr.queue_paths(config)['diffd'];items=json.loads(file.read_text());items[0]['event_id']='new';atomic_write_json(file,items)
    remote.callback=mutate
    with pytest.raises(cr.ResolutionError,match='changed'):
        mp.apply(config,path,'pull',token,remote,validate)
    assert local.read_bytes()==(b'new local' if changed=='local' else b'local version')
    assert len(json.loads(cr.queue_paths(config)['diffd'].read_text()))==2
    assert unresolved_attempts(config.state_dir,'diffd')


def test_unrelated_arrival_preserved(state):
    config,path,local,remote,validate=state
    token=mp.preview(config,path,'pull',remote,validate)['token']
    def unrelated():
        file=cr.queue_paths(config)['diffd'];items=json.loads(file.read_text());items.append({'path':'other-new','action':'download','event_id':'new'});atomic_write_json(file,items)
    remote.callback=unrelated
    mp.apply(config,path,'pull',token,remote,validate)
    assert {r['event_id'] for r in json.loads(cr.queue_paths(config)['diffd'].read_text())}=={'other','new'}


def test_missing_local_cannot_choose_local_and_delete_remote(state):
    config,path,local,remote,validate=state;local.unlink()
    with pytest.raises(cr.ResolutionError,match='missing'):
        mp.preview(config,path,'local',remote,validate)


def test_symlink_and_delete_are_not_supported(state):
    config,path,local,remote,validate=state
    local.unlink();local.symlink_to(local.parent/'other')
    with pytest.raises(cr.ResolutionError,match='symlink'):
        mp.preview(config,path,'pull',remote,validate)
    local.unlink();local.write_bytes(b'local')
    atomic_write_json(cr.queue_paths(config)['diffd'],[{'path':path,'action':'delete','event_id':'delete'}])
    with pytest.raises(cr.ResolutionError,match='delete/rename'):
        mp.preview(config,path,'pull',remote,validate)


def test_queue_commit_failure_preserves_original_and_blocks_recovery(state,monkeypatch):
    config,path,local,remote,validate=state
    token=mp.preview(config,path,'pull',remote,validate)['token']
    original=mp.atomic_write_json
    def fail(file,value,*args,**kwargs):
        if file==cr.queue_paths(config)['diffd']:raise OSError('fixture disk failure')
        return original(file,value,*args,**kwargs)
    monkeypatch.setattr(mp,'atomic_write_json',fail)
    with pytest.raises(OSError,match='disk failure'):
        mp.apply(config,path,'pull',token,remote,validate)
    assert local.read_bytes()==remote.content
    assert next((config.state_dir/'manual-pulls').glob('*/local')).read_bytes()==b'local version'
    assert unresolved_attempts(config.state_dir,'diffd')


def test_manual_automation_guard_never_calls_transfer(tmp_path,monkeypatch):
    from pcloud_tools import cli_service_daemon as cli
    config=SimpleNamespace(diffd_download_mode='manual')
    monkeypatch.setattr(cli,'load_config',lambda paths:SimpleNamespace(config=config,issues=[]))
    def forbidden(*args,**kwargs):raise AssertionError('must not inspect or execute transfer')
    monkeypatch.setattr(cli,'read_service_daemon_state',forbidden)
    report=cli._transfer_automation_run_report(SimpleNamespace(execute=True),None,SimpleNamespace(name='diffd'))
    assert report.status=='ok' and report.details['transfer started'] is False
