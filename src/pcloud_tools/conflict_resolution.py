"""Generation-bound conflict decisions; transfers remain on the normal gated executor.

Only one queue is atomically changed per decision. Both originals are saved
outside the sync tree before releasing either direction. No remote writes are
performed here. Unknown/delete/rename records deliberately remain for review.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import uuid

from .io_utils import atomic_write_json
from .transfer_executor import run_transfer_batch
from .transfer_recovery import inspect_recovery
from .transfer_state import (TransferStateError, read_queue_snapshot, state_lock,
                             transfer_tick_lock, transfer_path_lock, writer_state_lock, writer_process_session)


class ResolutionError(TransferStateError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def safe_local(config, path):
    if not isinstance(path, str) or not path or '\\' in path or any(ord(c) < 32 for c in path):
        raise ResolutionError('invalid relative file path')
    parts = PurePosixPath(path).parts
    if path.startswith('/') or '..' in parts or str(PurePosixPath(path)) != path:
        raise ResolutionError('invalid relative file path')
    root = config.core_dir.resolve()
    target = root
    for part in parts:
        target = target / part
        if target.is_symlink():
            raise ResolutionError('symlinks cannot be resolved automatically')
    if not target.is_relative_to(root):
        raise ResolutionError('file is outside core directory')
    return target


def fingerprint(path):
    with path.open('rb') as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ResolutionError('only regular files are supported')
        h = hashlib.sha256()
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            h.update(chunk)
        after = os.fstat(handle.fileno())
    fields = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    if fields(before) != fields(after) or fields(after) != fields(path.stat()):
        raise ResolutionError('local file changed while reading; preview again')
    return {'sha256': h.hexdigest(), 'size': after.st_size,
            'mtime_ns': after.st_mtime_ns, 'inode': after.st_ino, 'ctime_ns': after.st_ctime_ns}


def queue_paths(config):
    return {'pushd': config.state_dir / 'pushd' / 'queue.json',
            'diffd': config.state_dir / 'diffd' / 'remote-changes.json',
            'pending': config.state_dir / 'daemon' / 'pending-downloads.json'}


def queues(config, path):
    result = {}
    for name, file in queue_paths(config).items():
        snapshot = read_queue_snapshot(file)
        if snapshot.issue:
            raise ResolutionError(snapshot.issue.message)
        result[name] = [item for item in snapshot.raw_records
                        if (item if isinstance(item, str) else item.get('path') if isinstance(item, dict) else None) == path]
    if result['pending']:
        raise ResolutionError('legacy pending downloads exist for this path; automatic resolution is unavailable')
    for name, allowed in [('pushd', {'upload', 'sync', 'change', 'create', 'created', 'update', 'updated', 'modify', 'modified'}), ('diffd', {'download'})]:
        if not result[name]:
            raise ResolutionError('both-side conflict no longer exists; refresh the list')
        for item in result[name]:
            action = item.get('action', item.get('op', 'sync')) if isinstance(item, dict) else 'sync'
            if action not in allowed:
                raise ResolutionError('delete/rename/unknown actions require separate review')
    return result


@contextlib.contextmanager
def queue_locks(config):
    with contextlib.ExitStack() as stack:
        for name, path in queue_paths(config).items():
            stack.enter_context(state_lock(path) if name == 'pending' else writer_state_lock(path))
        yield


class RemoteReader:
    def __init__(self, config):
        self.config = config
        self.binary = config.rclone_bin or shutil.which('rclone')
        if not self.binary:
            raise ResolutionError('rclone was not found')

    def run(self, args):
        batch = run_transfer_batch([{'path': 'conflict-backup', 'command': [self.binary, *args]}],
                                   timeout_seconds=self.config.transfer_exec_timeout_seconds, concurrency=1)
        item = batch.results[0] if batch.results else {}
        if item.get('returncode') != 0 or item.get('timed_out') or item.get('requires_child_exit_confirmation'):
            raise ResolutionError('cloud inspection/backup failed; queues were not changed')
        return str(item.get('stdout', ''))

    def remote(self, path):
        return self.config.core_remote.rstrip('/') + '/' + path

    def inspect(self, path):
        try:
            item = json.loads(self.run(['lsjson', '--stat', '--hash', self.remote(path)]))
        except (ValueError, TypeError) as exc:
            raise ResolutionError('invalid cloud metadata') from exc
        if not isinstance(item, dict) or item.get('IsDir') or not isinstance(item.get('Size'), int):
            raise ResolutionError('cloud source must be an existing regular file')
        hashes = {k.lower().replace('-', ''): v.lower() for k, v in item.get('Hashes', {}).items()
                  if isinstance(v, str) and v and k.lower().replace('-', '') in {'sha1', 'sha256', 'md5'}}
        if not hashes:
            raise ResolutionError('cloud content hash unavailable; cannot safely approve this version')
        return {'size': item['Size'], 'modified': item.get('ModTime'), 'id': item.get('ID'), 'hashes': hashes}

    def backup(self, path, destination, expected):
        self.run(['copyto', self.remote(path), str(destination)])
        if destination.stat().st_size != expected['size']:
            raise ResolutionError('cloud backup size changed; preview again')
        for name, expected_hash in expected['hashes'].items():
            h = hashlib.new(name)
            with destination.open('rb') as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                    h.update(chunk)
            if h.hexdigest() != expected_hash:
                raise ResolutionError('cloud backup content changed; preview again')
        destination.chmod(0o600)


def preview(config, path, strategy, remote, validate):
    if strategy not in {'local', 'cloud', 'both'}:
        raise ResolutionError('unknown strategy')
    target = safe_local(config, path)
    validate(path)
    with queue_locks(config):
        selected = queues(config, path)
    local = fingerprint(target)
    cloud = remote.inspect(path)
    document = {'schema': 1, 'path': path, 'strategy': strategy,
                'core': str(config.core_dir.resolve()), 'remote': config.core_remote,
                'state': str(config.state_dir.resolve()), 'queues': selected,
                'local': local, 'cloud': cloud}
    return {**document, 'token': digest(document)}


def apply(config, path, strategy, token, remote, validate):
    with contextlib.ExitStack() as stack:
        # Order is fixed; running batches cause a retryable busy error, never a kill.
        for service in ('pushd', 'diffd'):
            stack.enter_context(transfer_tick_lock(config.state_dir, service))
            recovery = inspect_recovery(config.state_dir, service)
            if recovery.candidates or recovery.issues:
                raise ResolutionError('unfinished transfer recovery must complete first')
        for service in ('pushd', 'diffd'):
            stack.enter_context(writer_process_session(config.state_dir, service))
        stack.enter_context(transfer_path_lock(config.state_dir, 'pushd', path))
        current = preview(config, path, strategy, remote, validate)
        if current['token'] != token:
            raise ResolutionError('file or queued event changed after confirmation; preview again')
        target = safe_local(config, path)
        receipts = config.state_dir / 'conflict-resolutions'
        receipts.mkdir(mode=0o700, parents=True, exist_ok=True)
        if receipts.is_symlink():
            raise ResolutionError('backup directory must not be a symlink')
        receipts.chmod(0o700)
        receipt_dir = receipts / uuid.uuid4().hex
        receipt_dir.mkdir(mode=0o700)
        local_backup = receipt_dir / 'local'
        shutil.copy2(target, local_backup)
        local_backup.chmod(0o600)
        if fingerprint(local_backup)['sha256'] != current['local']['sha256']:
            raise ResolutionError('local file changed while backing up; preview again')
        remote.backup(path, receipt_dir / 'cloud', current['cloud'])
        if remote.inspect(path) != current['cloud']:
            raise ResolutionError('cloud changed while backing up; preview again')
        sibling = None
        if strategy == 'both':
            sibling = str(PurePosixPath(path).with_name(
                PurePosixPath(path).stem + '.local-conflict-' + receipt_dir.name + PurePosixPath(path).suffix))
            validate(sibling, sibling=True)
        with queue_locks(config):
            if queues(config, path) != current['queues'] or fingerprint(safe_local(config, path)) != current['local']:
                raise ResolutionError('local file or queued event changed; preview again')
            validate(path)
            if sibling:
                validate(sibling, sibling=True)
            service = 'diffd' if strategy == 'local' else 'pushd'
            file = queue_paths(config)[service]
            snapshot = read_queue_snapshot(file)
            if snapshot.issue:
                raise ResolutionError(snapshot.issue.message)
            selected = current['queues'][service]
            retained = [item for item in snapshot.raw_records if item not in selected]
            receipt = {**current, 'backup_directory': str(receipt_dir), 'sibling': sibling,
                       'queue': str(file), 'status': 'prepared', 'removed': selected}
            atomic_write_json(receipt_dir / 'receipt.json', receipt)
            if sibling:
                sibling_file = safe_local(config, sibling)
                # Exclusive create: never clobber another file, even after a race.
                with sibling_file.open('xb') as output, local_backup.open('rb') as source:
                    os.fchmod(output.fileno(), 0o600)
                    shutil.copyfileobj(source, output)
                    output.flush()
                    os.fsync(output.fileno())
                retained.append({'path': sibling, 'action': 'upload', 'reason': 'conflict resolution: keep both',
                                 'event_id': uuid.uuid4().hex})
            # This is the only queue commit. Interruption before it leaves the
            # original conflict; interruption after it leaves the chosen direction.
            atomic_write_json(file, retained)
            receipt['status'] = 'queued'
            atomic_write_json(receipt_dir / 'receipt.json', receipt)
        return {'path': path, 'strategy': strategy, 'status': 'queued', 'backup_directory': str(receipt_dir),
                'sibling': sibling, 'transfer_started': False,
                'message': '選択を反映しました。実際の転送は通常の同期処理で行います。'}
