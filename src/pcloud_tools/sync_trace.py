"""Opt-in, bounded timing records without paths, command arguments or content."""
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from functools import wraps
from pathlib import Path
import fcntl
import json
import os
import resource
import time
import uuid

MAX_BYTES = 16 * 1024 * 1024
MAX_DAYS = 7


def now():
    return datetime.now(timezone.utc).isoformat()


def folder(root):
    return Path(root) / 'diagnostics' / 'sync-trace'


def read(root):
    try:
        value = json.loads((folder(root) / 'session.json').read_text())
        if not isinstance(value, dict) or value.get('schema') != 'pcloud-sync-trace.v1':
            raise ValueError('invalid trace session')
        identifier = value.get('id', '')
        if not isinstance(identifier, str) or len(identifier) != 32 or any(c not in '0123456789abcdef' for c in identifier):
            raise ValueError('invalid trace identifier')
        return value
    except FileNotFoundError:
        return {'status': 'disabled'}


@contextmanager
def locked(root):
    directory = folder(root)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / 'control.lock').open('a+') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield directory


def write(directory, data):
    from .io_utils import atomic_write_json
    atomic_write_json(directory / 'session.json', data)
    (directory / 'session.json').chmod(0o600)


def valid(data):
    return data.get('status') == 'active' and datetime.fromisoformat(data['expires_at']) > datetime.now(timezone.utc)


def start(root, reconcile_id):
    if not isinstance(reconcile_id, str) or not reconcile_id:
        raise ValueError('An active reconciliation is required; tracing does not start a new reconciliation.')
    with locked(root) as directory:
        previous = read(root)
        if valid(previous):
            raise ValueError('A trace is already active. Use trace status or trace stop.')
        data = {'schema': 'pcloud-sync-trace.v1', 'id': uuid.uuid4().hex,
                'status': 'active', 'reconcile_id': reconcile_id, 'started_at': now(),
                'expires_at': (datetime.now(timezone.utc) + timedelta(days=MAX_DAYS)).isoformat(),
                'max_bytes': MAX_BYTES}
        write(directory, data)
        return data


def stop(root, reason='stopped', expected=None):
    with locked(root) as directory:
        data = read(root)
        if data.get('status') == 'active' and (expected is None or data.get('id') == expected):
            data.update(status=reason, finished_at=now())
            write(directory, data)
        return data


def status(root):
    data = read(root)
    if data.get('status') == 'active' and not valid(data):
        data = {**data, 'status': 'expired'}
    identifier = data.get('id', '')
    if identifier and (len(identifier) != 32 or any(c not in '0123456789abcdef' for c in identifier)):
        raise ValueError('invalid trace identifier')
    path = folder(root) / (identifier + '.jsonl') if identifier else None
    return {**data, 'directory': str(folder(root)), 'log': str(path) if path else None,
            'bytes': path.stat().st_size if path and path.exists() else 0}


class Recorder:
    def __init__(self, root):
        self.root = root
        self.session = None
        self.metrics = {}
        self.phase = None
        self.batch = uuid.uuid4().hex
        try:
            data = read(root)
            if valid(data):
                self.session = data
        except (OSError, ValueError, KeyError, TypeError):
            pass  # Diagnostic failure must not interrupt synchronization.

    def emit(self, event, **values):
        if not self.session:
            return
        try:
            with locked(self.root) as directory:
                current = read(self.root)
                if current.get('id') != self.session['id'] or not valid(current):
                    self.session = None
                    return
                path = directory / (current['id'] + '.jsonl')
                record = {'at': now(), 'batch': self.batch, 'event': event, **values}
                raw = (json.dumps(record, separators=(',', ':')) + '\n').encode()
                if (path.stat().st_size if path.exists() else 0) + len(raw) > MAX_BYTES:
                    current.update(status='size-limit', finished_at=now())
                    write(directory, current)
                    self.session = None
                    return
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, 'ab') as handle:
                    handle.write(raw)
        except (OSError, ValueError, KeyError, TypeError):
            self.session = None

    @contextmanager
    def span(self, name, detail=False):
        if not self.session:
            yield
            return
        started, cpu = time.monotonic(), time.process_time()
        child = resource.getrusage(resource.RUSAGE_CHILDREN)
        if detail:
            self.emit('start', operation=name, phase=self.phase)
        outcome = 'ok'
        try:
            yield
        except BaseException as exc:
            outcome = type(exc).__name__
            raise
        finally:
            elapsed, used = time.monotonic() - started, time.process_time() - cpu
            after = resource.getrusage(resource.RUSAGE_CHILDREN)
            children = after.ru_utime + after.ru_stime - child.ru_utime - child.ru_stime
            metric = self.metrics.setdefault(name, {'calls': 0, 'wall_seconds': 0, 'python_cpu_seconds': 0, 'child_cpu_seconds': 0})
            metric['calls'] += 1
            metric['wall_seconds'] += elapsed
            metric['python_cpu_seconds'] += used
            metric['child_cpu_seconds'] += children
            if detail:
                self.emit('finish', operation=name, phase=self.phase, wall_seconds=elapsed,
                          python_cpu_seconds=used, child_cpu_seconds=children, outcome=outcome)

    def set_phase(self, phase):
        if self.session and phase != self.phase:
            self.emit('phase', phase=phase, metrics=self.metrics)
            self.phase = phase

    def finish(self, state, success):
        if not self.session:
            return
        self.emit('batch-finish', success=success, metrics=self.metrics)
        if not self.session:
            return
        target = self.session['reconcile_id']
        if success and state.get('reconciled_request') == target and not state.get('reconcile'):
            stop(self.root, 'completed', self.session['id'])
        elif state.get('reconcile') and state['reconcile'].get('id') != target:
            stop(self.root, 'replaced', self.session['id'])


def timed(name):
    def decorate(function):
        @wraps(function)
        def wrapper(self, *args, **kwargs):
            trace = getattr(self, 'trace', None)
            if trace is None:
                return function(self, *args, **kwargs)
            with trace.span(name):
                return function(self, *args, **kwargs)
        return wrapper
    return decorate


def report(root):
    data = status(root)
    totals = {}
    batches = 0
    if data['log'] and Path(data['log']).exists():
        with Path(data['log']).open() as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get('event') != 'batch-finish':
                    continue
                batches += 1
                for name, values in row.get('metrics', {}).items():
                    metric = totals.setdefault(name, {key: 0 for key in values})
                    for key, value in values.items():
                        metric[key] += value
    return {**data, 'finished_batches': batches, 'metrics': totals,
            'note': 'Nested timings overlap. Wall minus CPU includes waits; it does not identify network, disk or lock waits by itself. Active/interrupted commands remain start-only in JSONL.'}
