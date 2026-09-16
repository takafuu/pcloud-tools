"""Explicit, version-bound cloud review; automatic downloads are a separate policy."""
import contextlib
from datetime import datetime
import os
from pathlib import Path
import shutil
import tempfile
import uuid

from . import conflict_resolution as cr
from .download_suppression import local_fingerprint, mark_download_started, mark_download_completed
from .io_utils import atomic_write_json
from .transfer_recovery import inspect_recovery
from .transfer_state import (read_queue_snapshot, transfer_tick_lock, transfer_path_lock,
                             writer_process_session, create_attempt, update_attempt)


def local_version(path):
    if not path.exists():
        return {'exists': False}
    return {'exists': True, **cr.fingerprint(path)}


def selected_queues(config, path):
    selected = {}
    for service, file in cr.queue_paths(config).items():
        snapshot = read_queue_snapshot(file)
        if snapshot.issue:
            raise cr.ResolutionError(snapshot.issue.message)
        selected[service] = [r for r in snapshot.raw_records
                             if (r if isinstance(r, str) else r.get('path') if isinstance(r, dict) else None) == path]
    if selected['pending']:
        raise cr.ResolutionError('legacy pending downloads require separate review')
    if not selected['diffd']:
        raise cr.ResolutionError('cloud event is no longer queued; refresh')
    if any(not isinstance(r, dict) or r.get('action') != 'download' or not r.get('event_id') for r in selected['diffd']):
        raise cr.ResolutionError('delete/rename or legacy events require separate review')
    allowed = {'upload', 'sync', 'change', 'create', 'created', 'update', 'updated', 'modify', 'modified'}
    if any(not isinstance(r, dict) or r.get('action', 'upload') not in allowed for r in selected['pushd']):
        raise cr.ResolutionError('local delete/rename events require separate review')
    return selected


def preview(config, path, choice, remote, validate):
    if choice not in {'pull', 'local'}:
        raise cr.ResolutionError('choice must be pull or local')
    target = cr.safe_local(config, path)
    validate(path)
    with cr.queue_locks(config):
        queues = selected_queues(config, path)
    local = local_version(target)
    if choice == 'local' and not local['exists']:
        raise cr.ResolutionError('local source is missing; cloud deletion requires separate review')
    cloud = remote.inspect(path)
    document = {'schema': 1, 'path': path, 'choice': choice,
                'core': str(config.core_dir.resolve()), 'remote': config.core_remote,
                'state': str(config.state_dir.resolve()), 'queues': queues, 'local': local, 'cloud': cloud}
    return {**document, 'token': cr.digest(document)}


def _attempt_update(config, attempt_id, **kwargs):
    result = update_attempt(config.state_dir, 'diffd', attempt_id, **kwargs)
    if result.issue:
        raise cr.ResolutionError(result.issue.message)


def _remove_selected(file, selected):
    snapshot = read_queue_snapshot(file)
    if snapshot.issue:
        raise cr.ResolutionError(snapshot.issue.message)
    atomic_write_json(file, [r for r in snapshot.raw_records if r not in selected])


def apply(config, path, choice, token, remote, validate):
    with contextlib.ExitStack() as stack:
        for service in ('pushd', 'diffd'):
            stack.enter_context(transfer_tick_lock(config.state_dir, service))
            recovery = inspect_recovery(config.state_dir, service)
            if recovery.candidates or recovery.issues:
                raise cr.ResolutionError('unfinished transfer recovery must complete first')
        for service in ('pushd', 'diffd'):
            stack.enter_context(writer_process_session(config.state_dir, service))
        stack.enter_context(transfer_path_lock(config.state_dir, 'diffd', path))
        current = preview(config, path, choice, remote, validate)
        if current['token'] != token:
            raise cr.ResolutionError('local/cloud/queued event changed after confirmation; preview again')
        target = cr.safe_local(config, path)
        receipts = config.state_dir / 'manual-pulls'
        if receipts.is_symlink():
            raise cr.ResolutionError('backup directory must not be a symlink')
        receipts.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory = receipts / uuid.uuid4().hex
        directory.mkdir(mode=0o700)
        attempt = create_attempt(config.state_dir, 'diffd', [{'path': path, 'event_id': r['event_id'],
                                 'pre_transfer_fingerprint': current['local']} for r in current['queues']['diffd']], concurrency=1)
        if attempt.issue:
            raise cr.ResolutionError(attempt.issue.message)
        receipt = {**current, 'status': 'preparing', 'attempt_id': attempt.attempt_id}
        atomic_write_json(directory / 'receipt.json', receipt)
        staging = None
        try:
            if current['local']['exists']:
                shutil.copy2(target, directory / 'local')
                (directory / 'local').chmod(0o600)
                if cr.fingerprint(directory / 'local')['sha256'] != current['local']['sha256']:
                    raise cr.ResolutionError('local changed while saving original')
            remote.backup(path, directory / 'cloud', current['cloud'])
            if remote.inspect(path) != current['cloud']:
                raise cr.ResolutionError('cloud changed while saving original')
            if choice == 'pull':
                cr.safe_local(config, path)
                target.parent.mkdir(parents=True, exist_ok=True)
                cr.safe_local(config, path)
                fd, staging_name = tempfile.mkstemp(prefix='.pcloud-manual-', suffix='.tmp', dir=target.parent)
                os.close(fd)
                staging = Path(staging_name)
                shutil.copyfile(directory / 'cloud', staging)
                if current['local']['exists']:
                    staging.chmod(target.stat().st_mode & 0o777)
                modified = current['cloud'].get('modified')
                if modified:
                    stamp = datetime.fromisoformat(modified.replace('Z', '+00:00')).timestamp()
                    os.utime(staging, (stamp, stamp))
            with cr.queue_locks(config):
                validate(path)
                if selected_queues(config, path) != current['queues'] or local_version(cr.safe_local(config, path)) != current['local']:
                    raise cr.ResolutionError('local or queued event changed; preview again')
                receipt['status'] = 'prepared'
                atomic_write_json(directory / 'receipt.json', receipt)
                files = cr.queue_paths(config)
                if choice == 'pull':
                    mark_download_started(config, path)
                    os.replace(staging, target)
                    staging = None
                    mark_download_completed(config, path, local_fingerprint(target))
                    _remove_selected(files['pushd'], current['queues']['pushd'])
                else:
                    snapshot = read_queue_snapshot(files['pushd'])
                    if snapshot.issue:
                        raise cr.ResolutionError(snapshot.issue.message)
                    retained = [r for r in snapshot.raw_records if r not in current['queues']['pushd']]
                    retained.append({'path': path, 'action': 'upload', 'event_id': uuid.uuid4().hex,
                                     'reason': 'manual cloud review: local selected'})
                    atomic_write_json(files['pushd'], retained)
                _remove_selected(files['diffd'], current['queues']['diffd'])
            receipt['status'] = 'completed' if choice == 'pull' else 'upload-queued'
            atomic_write_json(directory / 'receipt.json', receipt)
            _attempt_update(config, attempt.attempt_id, phase='completed', status='completed', requires_child_exit_confirmation=False)
            return {'path': path, 'choice': choice, 'status': receipt['status'], 'backup_directory': str(directory),
                    'message': '選択したクラウド版を取り込みました。' if choice == 'pull' else 'ローカル版のアップロードを予約しました。'}
        except Exception:
            _attempt_update(config, attempt.attempt_id, phase='needs-recovery', status='blocked', receipt=str(directory / 'receipt.json'))
            raise
        finally:
            if staging is not None:
                staging.unlink(missing_ok=True)
