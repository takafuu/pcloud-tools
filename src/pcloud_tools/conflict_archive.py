"""Best-effort losing-version archive with bounded, indexed retention."""
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import time
import uuid
from contextlib import contextmanager

from .event_sync_remote import safe_local, local_version, same_content, SyncError
from .io_utils import atomic_write_json


def archive_root(config):
    return config.core_dir / '.conflict'


@contextmanager
def connection(config):
    config.state_dir.mkdir(parents=True, exist_ok=True)
    path = config.state_dir / 'conflict-archive.sqlite3'
    db = sqlite3.connect(path, timeout=30)
    os.chmod(path, 0o600)
    db.execute('CREATE TABLE IF NOT EXISTS archives (id TEXT PRIMARY KEY, created REAL NOT NULL, size INTEGER NOT NULL, path TEXT NOT NULL, side TEXT NOT NULL)')
    try:
        with db:
            yield db
    finally:
        db.close()


def record_failure(config, path, operation, exc):
    # Local operational evidence only; no exception text/command output/secrets.
    config.state_dir.mkdir(parents=True, exist_ok=True)
    log = config.state_dir / 'conflict-archive-failures.jsonl'
    line = json.dumps({'at': time.time(), 'path': path, 'operation': operation, 'error_type': type(exc).__name__}, ensure_ascii=False) + '\n'
    fd = os.open(log, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'a') as handle:
        handle.write(line)


def safe_root(config):
    root = archive_root(config)
    if root.is_symlink():
        raise SyncError('conflict archive must not be a symlink')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


def prune(config):
    root = safe_root(config)
    cutoff = time.time() - getattr(config, 'conflict_retention_days', 14) * 86400
    budget = getattr(config, 'conflict_max_bytes', 100_000_000_000)
    with connection(config) as db:
        rows = list(db.execute('SELECT id,created,size FROM archives ORDER BY created,id'))
        total = sum(row[2] for row in rows)
        for identifier, created, size in rows:
            if created >= cutoff and total <= budget:
                continue
            if not re.fullmatch('[0-9a-f]{32}', identifier):
                raise SyncError('invalid archive index entry')
            directory = root / identifier
            if directory.is_symlink():
                raise SyncError('archive entry must not be a symlink')
            if directory.exists():
                shutil.rmtree(directory)
            db.execute('DELETE FROM archives WHERE id=?', (identifier,))
            total -= size
    # A single file larger than the budget is pruned too. Sync still proceeds.


def capture(config, remote, path, side, expected):
    identifier = uuid.uuid4().hex
    directory = None
    registered = False
    operation = 'capture'
    try:
        if expected.get("size", 0) > getattr(config, "conflict_max_bytes", 100_000_000_000):
            operation = "capacity_limit"
            raise SyncError("file exceeds archive capacity")
        root = safe_root(config)
        directory = root / identifier
        directory.mkdir(mode=0o700)
        with connection(config) as db:
            db.execute('INSERT INTO archives VALUES (?,?,?,?,?)', (identifier, time.time(), expected.get('size', 0), path, side))
        prune(config)
        payload = directory / 'data'
        payload.mkdir(mode=0o700)
        destination = safe_local(payload, path)
        if side == 'local':
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(safe_local(config.core_dir, path), destination)
        else:
            result = remote.copy('download', [path], payload)
            if result.get('returncode') != 0:
                raise SyncError('cloud archive copy failed')
        actual = local_version(destination, expected.get('hashes', {}))
        if same_content(actual, expected) is not True:
            raise SyncError('archived version changed or could not be verified')
        created = time.time()
        atomic_write_json(directory / 'record.json', {'path': path, 'side': side, 'created': created, 'version': expected})
        with connection(config) as db:
            db.execute('UPDATE archives SET created=?,size=? WHERE id=?', (created, actual['size'], identifier))
        registered = True
        prune(config)
        return {'status': 'saved' if directory.exists() else 'pruned', 'id': identifier}
    except Exception as exc:
        try:record_failure(config, path, operation, exc)
        except OSError:pass
        if not registered and directory is not None and directory.is_dir() and not directory.is_symlink():
            try:shutil.rmtree(directory)
            except OSError:pass
            try:
                with connection(config) as db:db.execute('DELETE FROM archives WHERE id=?',(identifier,))
            except (OSError, sqlite3.Error):pass
        return {'status': 'failed', 'operation': operation, 'error_type': type(exc).__name__}


def maintain(config):
    if not archive_root(config).exists():
        return
    try:prune(config)
    except Exception as exc:
        try:record_failure(config, '', 'retention', exc)
        except OSError:pass


def listing(config):
    if not (config.state_dir / 'conflict-archive.sqlite3').exists():
        return {'directory': str(archive_root(config)), 'records': [], 'retained_bytes': 0,
                'failure_log': str(config.state_dir / 'conflict-archive-failures.jsonl')}
    with connection(config) as db:
        records = [dict(zip(('id','created','size','path','side'), row)) for row in db.execute('SELECT id,created,size,path,side FROM archives ORDER BY created DESC')]
    return {'directory': str(archive_root(config)), 'records': records, 'retained_bytes': sum(r['size'] for r in records),
            'failure_log': str(config.state_dir / 'conflict-archive-failures.jsonl')}
