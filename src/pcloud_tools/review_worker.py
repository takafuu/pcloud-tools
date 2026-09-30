"""Independent read-only verification; only generation-bound bookkeeping is committed."""
from contextlib import ExitStack, contextmanager
import fcntl
import json
import uuid
import sqlite3
from concurrent.futures import ThreadPoolExecutor

from .config import EVENT_RECHECK_BATCH_LIMIT
from .event_sync import ABSENT, ID_MISMATCH, Scope, now, obsolete_identity_event, choose, CONTENT_EVENTS
from .event_sync_remote import local_version, safe_local, stat_version
from .io_utils import atomic_write_json
from .sqlite_state import Store, DB_NAME, active
from .transfer_state import writer_process_session


@contextmanager
def worker_lock(state_dir):
    path = state_dir / 'event-sync' / 'review-worker.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def snapshot(db, path):
    rows = {}
    for namespace in ('reviews', 'baseline'):
        row = db.execute('SELECT value FROM kv WHERE namespace=? AND key=?', (namespace, path)).fetchone()
        rows[namespace] = row[0] if row else None
    rows['queues'] = list(db.execute('SELECT service,seq,payload FROM queue WHERE path=? ORDER BY seq', (path,)))
    return rows


def commit_result(store, config, path, before, local, cloud, equal, resync=False):
    # No network or content hashing while SQLite is locked. The worker never
    # writes file contents or baseline, and never consumes a newer event.
    with store.connection(write=True) as db:
        if snapshot(db, path) != before:
            return False
        fingerprint = {k: v for k, v in local.items() if k not in ('hashes', 'second')}
        if stat_version(safe_local(config.core_dir, path)) != fingerprint:
            return False
        if equal or resync:
            db.execute("DELETE FROM kv WHERE namespace='reviews' AND key=?", (path,))
            db.executemany('DELETE FROM queue WHERE service=? AND seq=?', [(s, seq) for s, seq, _ in before['queues']])
            if resync:
                record = {'path': path, 'action': 'delete' if resync in ('delete-cloud','delete-local') else 'sync', 'event_id': uuid.uuid4().hex}
                if cloud.get('id'):record['remote_file_id'] = cloud['id']
                store.insert_queue(db, 'pushd' if resync == 'delete-cloud' else 'diffd', record)
        else:
            review = json.loads(before['reviews'])
            review.update(local=local, cloud=cloud, reason='更新日時では採用する版を決められません。内容を確認してください')
            store.put(db, 'reviews', path, review)
        summary = db.execute("SELECT value FROM kv WHERE namespace='summary' AND key='event'").fetchone()
        if summary:
            from .review_classification import counts
            value = json.loads(summary[0])
            scope = Scope(config)
            value.update(counts(json.loads(raw) for p, raw in db.execute("SELECT key,value FROM kv WHERE namespace='reviews'") if scope.allows(p)))
            store.put(db, 'summary', 'event', value)
    return True


class ReviewWorker:
    def __init__(self, config, remote):
        self.config, self.remote = config, remote
        self.progress_file = config.state_dir / 'event-sync' / 'review-progress.json'

    def tick(self, limit=EVENT_RECHECK_BATCH_LIMIT):
        if not active(self.config.state_dir):
            return {'status': 'unavailable', 'message': 'SQLiteへの移行が必要です'}
        with worker_lock(self.config.state_dir) as owned:
            if not owned:
                return {'status': 'busy'}
            with ExitStack() as stack:
                for service in ('pushd', 'diffd'):
                    stack.enter_context(writer_process_session(self.config.state_dir, service, generation='review-worker'))
                return self._run(min(max(1, limit), EVENT_RECHECK_BATCH_LIMIT))

    def _run(self, limit):
        store = Store(self.config.state_dir / DB_NAME)
        scope = Scope(self.config)
        state = store.get('review-worker', 'state', {})
        request_file = self.config.state_dir / 'event-sync' / 'review-recheck-request.json'
        request = json.loads(request_file.read_text()) if request_file.exists() else {}
        if state.get('request_id') != request.get('id'):
            state = {'request_id': request.get('id')}
        reasons = (ID_MISMATCH, 'クラウド側の版が変わっています。内容を確認して採用する版を選んでください', '更新日時では採用する版を決められません。内容を確認してください')
        paths, cursor = store.review_candidates(reasons, state.get('cursor'), limit, scope.allows)
        def remaining():
            with store.connection() as db:
                return db.execute("SELECT count(*) FROM kv WHERE namespace='reviews' AND json_extract(value,'$.reason') IN (?,?,?)", reasons).fetchone()[0]
        progress = {'remaining': remaining(), 'status': 'running', 'started_at': now(), 'completed': 0, 'total': len(paths),
                    'cleared': 0, 'sync_queued': 0, 'needs_choice': 0, 'deferred': 0, 'failed': 0}
        def publish():
            progress['updated_at'] = now()
            atomic_write_json(self.progress_file, progress)
        publish()
        try:
            snapshots = {}
            with store.connection() as db:
                db.execute('BEGIN')
                for path in paths:
                    value = snapshot(db, path)
                    if value['reviews'] and json.loads(value['reviews']).get('reason') in reasons:
                        snapshots[path] = value
            cloud_versions = self.remote.inventory(list(snapshots), hashes=True) if snapshots else {}
            for path, before in snapshots.items():
                try:
                    cloud = cloud_versions.get(path, ABSENT)
                    target = safe_local(self.config.core_dir, path)
                    baseline = json.loads(before['baseline']) if before['baseline'] else None
                    cached = (baseline or {}).get('local', {})
                    current = stat_version(target)
                    local = cached if cached.get('hashes') and all(cached.get(k) == v for k, v in current.items()) else local_version(target)
                    queues = {s: [json.loads(raw) for service, _, raw in before['queues'] if service == s] for s in ('pushd', 'diffd')}
                    baseline = json.loads(before['baseline']) if before['baseline'] else None
                    equal = obsolete_identity_event(local, cloud, baseline, queues['pushd'], queues['diffd'])
                    decision, _ = choose(local, cloud, local_delete=any(r.get('action') == 'delete' for r in queues['pushd']),
                                         cloud_delete=any(r.get('action') == 'delete' for r in queues['diffd']),
                                         same_time=getattr(self.config, 'conflict_same_time', 'local'))
                    resync = (decision if not equal and decision != 'hold'
                              and all(r.get('action') in CONTENT_EVENTS for rows in queues.values() for r in rows) else False)
                    committed = commit_result(store, self.config, path, before, local, cloud, equal, resync)
                    progress['cleared' if equal else 'sync_queued' if resync else 'needs_choice'] += int(committed)
                    progress['deferred'] += int(not committed)
                except (OSError, ValueError, RuntimeError, sqlite3.Error):
                    progress['failed'] += 1
                progress['completed'] += 1
                publish()
            state['cursor'] = cursor
            with store.connection(write=True) as db:
                store.put(db, 'review-worker', 'state', state)
            progress['status'] = 'complete'
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            progress.update(status='failed', error='再確認の情報取得に失敗しました。次回に再試行します。', error_type=type(exc).__name__)
        progress['remaining'] = remaining()
        publish()
        return progress


def run_parallel(config, remote_factory, sync, limit=EVENT_RECHECK_BATCH_LIMIT):
    """Each lane has its own process-wide lock and budget; neither waits to start."""
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix='pcloud-review') as pool:
        future = pool.submit(ReviewWorker(config, remote_factory()).tick, limit)
        result = sync()
        try:
            result['review worker'] = future.result()
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            result['review worker'] = {'status': 'failed', 'error_type': type(exc).__name__}
        return result


def status(config):
    path = config.state_dir / 'event-sync' / 'review-progress.json'
    try:
        result = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    lock = config.state_dir / 'event-sync' / 'review-worker.lock'
    try:
        with lock.open('r') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                result['active'] = True
            else:
                result['active'] = False
                fcntl.flock(handle, fcntl.LOCK_UN)
    except OSError:
        result['active'] = None
    return result
