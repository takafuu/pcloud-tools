import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pcloud_tools import conflict_resolution as cr
from pcloud_tools.io_utils import atomic_write_json
from pcloud_tools.transfer_state import create_attempt


class Remote:
    content = b'cloud contents'
    after_backup = None

    def inspect(self, path):
        return {'size': len(self.content), 'hashes': {'sha1': hashlib.sha1(self.content).hexdigest()}, 'id': 'file1'}

    def backup(self, path, destination, expected):
        destination.write_bytes(self.content)
        if self.after_backup:
            self.after_backup()


@pytest.fixture
def setup(tmp_path):
    config = SimpleNamespace(core_dir=tmp_path / 'core', state_dir=tmp_path / 'state', core_remote='fixture:core')
    local = config.core_dir / 'Documents' / 'quotes " and 日本語.txt'
    local.parent.mkdir(parents=True)
    local.write_bytes(b'local contents')
    path = local.relative_to(config.core_dir).as_posix()
    files = cr.queue_paths(config)
    for name in ('pushd', 'diffd'):
        atomic_write_json(files[name], [
            {'path': path, 'action': 'upload' if name == 'pushd' else 'download', 'event_id': name + '-1', 'unknown': {'keep': 1}},
            {'path': 'unrelated.txt', 'action': 'upload' if name == 'pushd' else 'download', 'event_id': name + '-other'}])
    return config, path, local, Remote(), lambda path, **kwargs: None


def approve(setup, strategy):
    config, path, local, remote, validate = setup
    return cr.preview(config, path, strategy, remote, validate)['token']


@pytest.mark.parametrize('strategy', ['local', 'cloud', 'both'])
def test_decision_preserves_both_and_changes_one_queue(setup, strategy):
    config, path, local, remote, validate = setup
    token = approve(setup, strategy)
    before = {name: file.read_bytes() for name, file in cr.queue_paths(config).items() if file.exists()}
    result = cr.apply(config, path, strategy, token, remote, validate)
    backup = Path(result['backup_directory'])
    assert (backup / 'local').read_bytes() == b'local contents'
    assert (backup / 'cloud').read_bytes() == b'cloud contents'
    assert local.read_bytes() == b'local contents'
    assert not result['transfer_started']
    changed = 'diffd' if strategy == 'local' else 'pushd'
    unchanged = 'pushd' if strategy == 'local' else 'diffd'
    assert cr.queue_paths(config)[unchanged].read_bytes() == before[unchanged]
    items = json.loads(cr.queue_paths(config)[changed].read_text())
    assert not any(item['path'] == path for item in items)
    assert any(item['path'] == 'unrelated.txt' for item in items)
    if strategy == 'both':
        assert (config.core_dir / result['sibling']).read_bytes() == b'local contents'
        assert (config.core_dir / result['sibling']).stat().st_mode & 0o777 == 0o600
        assert any(item['path'] == result['sibling'] and item['event_id'] for item in items)
    with pytest.raises(cr.ResolutionError):
        cr.apply(config, path, strategy, token, remote, validate)


@pytest.mark.parametrize('change', ['local', 'remote', 'queue'])
def test_stale_confirmation_refuses_without_queue_mutation(setup, change):
    config, path, local, remote, validate = setup
    token = approve(setup, 'cloud')
    if change == 'local':
        local.write_bytes(b'new edit')
    elif change == 'remote':
        remote.content = b'new remote'
    else:
        file = cr.queue_paths(config)['pushd']
        items = json.loads(file.read_text())
        items[0]['event_id'] = 'new-generation'
        atomic_write_json(file, items)
    before = cr.queue_paths(config)['pushd'].read_bytes()
    with pytest.raises(cr.ResolutionError, match='changed'):
        cr.apply(config, path, 'cloud', token, remote, validate)
    assert cr.queue_paths(config)['pushd'].read_bytes() == before


def test_new_event_during_backup_preserved(setup):
    config, path, local, remote, validate = setup
    token = approve(setup, 'cloud')
    file = cr.queue_paths(config)['pushd']
    def append():
        items = json.loads(file.read_text())
        items.append({'path': path, 'action': 'upload', 'event_id': 'new'})
        atomic_write_json(file, items)
    remote.after_backup = append
    with pytest.raises(cr.ResolutionError, match='changed'):
        cr.apply(config, path, 'cloud', token, remote, validate)
    assert len(json.loads(file.read_text())) == 3


def test_unrelated_arrivals_are_retained(setup):
    config, path, local, remote, validate = setup
    token = approve(setup, 'cloud')
    file = cr.queue_paths(config)['pushd']
    items = json.loads(file.read_text())
    items.append({'path': 'new-unrelated', 'action': 'upload', 'event_id': 'new'})
    atomic_write_json(file, items)
    cr.apply(config, path, 'cloud', token, remote, validate)
    assert {i['path'] for i in json.loads(file.read_text())} == {'unrelated.txt', 'new-unrelated'}


def test_backup_failure_never_releases_conflict(setup):
    config, path, local, remote, validate = setup
    token = approve(setup, 'local')
    def fail(*args):
        raise cr.ResolutionError('offline')
    remote.backup = fail
    with pytest.raises(cr.ResolutionError, match='offline'):
        cr.apply(config, path, 'local', token, remote, validate)
    assert cr.queues(config, path)['diffd']


def test_recovery_and_delete_are_not_resolved(setup):
    config, path, local, remote, validate = setup
    token = approve(setup, 'local')
    create_attempt(config.state_dir, 'diffd', [], concurrency=1)
    with pytest.raises(cr.ResolutionError, match='recovery'):
        cr.apply(config, path, 'local', token, remote, validate)
    file = cr.queue_paths(config)['pushd']
    atomic_write_json(file, [{'path': path, 'action': 'delete', 'event_id': 'delete'}])
    with pytest.raises(cr.ResolutionError, match='delete'):
        approve(setup, 'local')


def test_symlink_and_traversal_rejected(setup):
    config, path, local, remote, validate = setup
    with pytest.raises(cr.ResolutionError):
        cr.safe_local(config, '../outside')
    saved = local.with_suffix('.saved')
    local.rename(saved)
    local.symlink_to(saved)
    with pytest.raises(cr.ResolutionError, match='symlink'):
        approve(setup, 'cloud')


def test_failed_atomic_commit_leaves_original_conflict(setup, monkeypatch):
    config, path, local, remote, validate = setup
    token = approve(setup, 'both')
    real = cr.atomic_write_json
    def fail_queue(file, payload):
        if file == cr.queue_paths(config)['pushd']:
            raise OSError('disk full')
        return real(file, payload)
    monkeypatch.setattr(cr, 'atomic_write_json', fail_queue)
    with pytest.raises(OSError):
        cr.apply(config, path, 'both', token, remote, validate)
    assert cr.queues(config, path)['pushd']
    assert local.read_bytes() == b'local contents'


def test_remote_reader_checks_content_hash(setup, monkeypatch):
    config, path, local, remote, validate = setup
    config.rclone_bin = '/fixture/rclone'
    config.transfer_exec_timeout_seconds = 1
    reader = cr.RemoteReader(config)
    destination = config.state_dir / 'backup'
    def run(args):
        destination.write_bytes(b'incorrect data')
    monkeypatch.setattr(reader, 'run', run)
    with pytest.raises(cr.ResolutionError):
        reader.backup(path, destination, remote.inspect(path))


def test_cli_preview_and_apply_with_fake_remote(tmp_path):
    import subprocess
    import sys
    from conftest import _base_env
    env = _base_env(tmp_path)
    core = Path(env['PCLOUD_TOOLS_WORKSPACE_ROOT'])
    env['PCLOUD_TOOLS_CORE_DIR'] = str(core)
    state = Path(env['PCLOUD_TOOLS_STATE_DIR'])
    target = core / 'Documents' / 'file.txt'
    target.parent.mkdir()
    target.write_text('local')
    fake = tmp_path / 'fake-rclone'
    fake.write_text('#!' + sys.executable + '\nimport json,sys,pathlib,hashlib\n'
                    'data=b"cloud"\n'
                    'if sys.argv[1]=="lsjson": print(json.dumps({"Size":5,"ID":"1","Hashes":{"sha1":hashlib.sha1(data).hexdigest()}}))\n'
                    'elif sys.argv[1]=="copyto": pathlib.Path(sys.argv[-1]).write_bytes(data)\n'
                    'else: sys.exit(2)\n')
    fake.chmod(0o755)
    env['PCLOUD_TOOLS_RCLONE_BIN'] = str(fake)
    for service, filename, action in [('pushd', 'queue.json', 'upload'), ('diffd', 'remote-changes.json', 'download')]:
        atomic_write_json(state / service / filename, [{'path': 'Documents/file.txt', 'action': action, 'event_id': service}])
    def run(*args):
        result = subprocess.run([sys.executable, '-m', 'pcloud_tools.cli', 'pushd', 'transfer', 'resolve', *args, '--json'],
                                env=env, capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stdout + result.stderr
        return json.loads(result.stdout)['details']
    assert run('list')['records'][0]['available']
    plan = run('preview', '--path', 'Documents/file.txt', '--strategy', 'both')
    result = run('apply', '--path', 'Documents/file.txt', '--strategy', 'both', '--token', plan['token'], '--execute')
    assert result['status'] == 'queued'
    assert (core / result['sibling']).read_text() == 'local'
    assert run('list')['count'] == 0
