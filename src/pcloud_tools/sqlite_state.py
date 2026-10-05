"""Row-oriented sync state. sqlite3 keeps launchd's runtime dependency-free.

All SQL values are bound parameters. Connections are short-lived; a network
transfer never holds a database transaction. Legacy JSON is imported explicitly
under the existing writer cutover barriers, never by a status read.
"""

from __future__ import annotations

from collections.abc import MutableMapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import uuid

DB_NAME = "sync-state.sqlite3"
VERSION = 1
TERMINAL = ("completed", "failed", "cancelled", "released")
QUEUE_NAMES = {("pushd", "queue.json"), ("diffd", "remote-changes.json")}


def stamp():
    return datetime.now(timezone.utc).isoformat()


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def database_for(path):
    path = Path(path)
    if (path.parent.name, path.name) in QUEUE_NAMES or (
        path.parent.name in ("pushd", "diffd") and path.name == "transfer-attempts.json"
    ):
        db = path.parent.parent / DB_NAME
        return Store(db) if db.exists() else None
    return None


def active(state_dir):
    return (Path(state_dir) / DB_NAME).exists()


class Store:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def connection(self, write=False, initialize=False):
        uri = self.path.resolve().as_uri() + (
            "?mode=rwc" if initialize else "?mode=rw" if write else "?mode=ro"
        )
        db = sqlite3.connect(uri, uri=True, timeout=30)
        try:
            db.execute("PRAGMA busy_timeout=30000")
            db.execute("PRAGMA cache_size=-4096")
            if not initialize:
                if db.execute("PRAGMA user_version").fetchone()[0] != VERSION:
                    raise ValueError("unsupported or incomplete sync state database")
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.commit()
        except BaseException:
            if write:
                db.rollback()
            raise
        finally:
            db.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        with self.connection(initialize=True) as db:
            db.executescript("""
                CREATE TABLE counts (kind TEXT NOT NULL, key TEXT NOT NULL, value INTEGER NOT NULL, PRIMARY KEY(kind,key));
                CREATE TABLE kv (namespace TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY(namespace,key));
                CREATE TABLE queue (seq INTEGER PRIMARY KEY AUTOINCREMENT, service TEXT NOT NULL, event_id TEXT, path TEXT, action TEXT, file_id TEXT, diffid TEXT, payload TEXT NOT NULL);
                CREATE INDEX queue_event ON queue(service,event_id);
                CREATE INDEX queue_path ON queue(service,path,action);
                CREATE INDEX queue_file ON queue(service,file_id);
                CREATE TABLE attempts (service TEXT NOT NULL, id TEXT NOT NULL, status TEXT, phase TEXT, payload TEXT NOT NULL, PRIMARY KEY(service,id));
                CREATE INDEX attempts_pending ON attempts(service,status,phase);
                CREATE TABLE pending (seq INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL UNIQUE);
                CREATE TABLE captured (service TEXT NOT NULL, event_id TEXT NOT NULL, PRIMARY KEY(service,event_id));
                CREATE TRIGGER queue_insert_count AFTER INSERT ON queue BEGIN
                  INSERT INTO counts VALUES ('queue',new.service,1) ON CONFLICT(kind,key) DO UPDATE SET value=value+1;
                END;
                CREATE TRIGGER queue_delete_count AFTER DELETE ON queue BEGIN
                  UPDATE counts SET value=value-1 WHERE kind='queue' AND key=old.service;
                END;
                CREATE TRIGGER pending_insert_count AFTER INSERT ON pending BEGIN
                  INSERT INTO counts VALUES ('pending','',1) ON CONFLICT(kind,key) DO UPDATE SET value=value+1;
                END;
                CREATE TRIGGER pending_delete_count AFTER DELETE ON pending BEGIN
                  UPDATE counts SET value=value-1 WHERE kind='pending' AND key='';
                END;
            """)
            db.execute(f"PRAGMA user_version={VERSION}")
            db.commit()

    @staticmethod
    def put(db, namespace, key, value):
        db.execute(
            "INSERT INTO kv VALUES (?,?,?) ON CONFLICT(namespace,key) DO UPDATE SET value=excluded.value",
            (namespace, key, encode(value)),
        )

    def get(self, namespace, key, default=None):
        with self.connection() as db:
            row = db.execute(
                "SELECT value FROM kv WHERE namespace=? AND key=?", (namespace, key)
            ).fetchone()
            return json.loads(row[0]) if row else default

    def count(self, table, service=None):
        if table not in ("queue", "pending", "attempts"):
            raise ValueError("invalid count table")
        with self.connection() as db:
            if table in ("queue", "pending"):
                row = db.execute(
                    "SELECT value FROM counts WHERE kind=? AND key=?",
                    (table, service or ""),
                ).fetchone()
                return row[0] if row else 0
            if service is None:
                return db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
            return db.execute(
                "SELECT COUNT(*) FROM attempts WHERE service=?", (service,)
            ).fetchone()[0]

    @staticmethod
    def insert_queue(db, service, payload):
        row = payload if isinstance(payload, dict) else {}
        db.execute(
            "INSERT INTO queue(service,event_id,path,action,file_id,diffid,payload) VALUES (?,?,?,?,?,?,?)",
            (
                service,
                row.get("event_id"),
                row.get("path", payload if isinstance(payload, str) else ""),
                row.get("action", "sync"),
                str(row.get("remote_file_id", "")),
                str(row.get("diffid", "")),
                encode(payload),
            ),
        )

    def queue_rows(self, service, paths=None):
        return QueueRows(self, service, paths)

    def candidate_paths(self, cursor, limit, allows):
        # Only names cross the Python boundary until the bounded batch is selected.
        if limit <= 0:
            return []
        with self.connection() as db:
            sql = "SELECT path FROM queue WHERE action NOT IN ('move','directory') UNION SELECT key FROM kv WHERE namespace='reviews'"
            result = []
            for comparison in (">", "<=") if cursor else (">",):
                for (path,) in db.execute(
                    "SELECT path FROM ("
                    + sql
                    + ") WHERE path "
                    + comparison
                    + " ? ORDER BY path",
                    (cursor or "",),
                ):
                    if allows(path):
                        result.append(path)
                    if len(result) >= limit:
                        return result
            return result

    def review_candidates(self, reason, cursor, limit, allows, *, include_structural=False):
        if limit <= 0:
            return [], cursor
        result, last = [], cursor
        with self.connection() as db:
            for comparison in ('>', '<=') if cursor else ('>',):
                for path, raw in db.execute(
                    "SELECT key,value FROM kv WHERE namespace='reviews' AND key " + comparison + " ? ORDER BY key LIMIT 200",
                    (cursor or '',),
                ):
                    last = path
                    review = json.loads(raw)
                    if review.get('reason') in ((reason,) if isinstance(reason, str) else reason) and (include_structural or not review.get('structural')) and allows(path):
                        result.append(path)
                    if len(result) >= limit:
                        return result, last
        return result, last

    def priority_paths(self, limit, allows, reviews):
        """Bounded recent/old queue sampling per side; unchanged review holds do not starve arrivals."""
        if limit <= 0:
            return []
        result = []
        with self.connection() as db:
            for service in ('pushd', 'diffd'):
                quota = max(1, limit // 2)
                added = 0
                for order, target in (('DESC', max(1, quota // 2)), ('ASC', quota)):
                    for path, event_id in db.execute(
                        'SELECT path,event_id FROM queue WHERE service=? ORDER BY seq ' + order + ' LIMIT ?',
                        (service, max(100, limit * 20)),
                    ):
                        if path in result or not allows(path):
                            continue
                        held = reviews.get(path, {}).get('event_ids', {}).get(service, [])
                        if event_id in held:
                            continue
                        result.append(path)
                        added += 1
                        if added >= target or len(result) >= limit:
                            break
                    if len(result) >= limit:
                        return result
        return result

    def structural_rows(self):
        with self.connection() as db:
            return [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT payload FROM queue WHERE service='pushd' AND action IN ('move','directory') ORDER BY seq"
                )
            ]

    def append(
        self, service, payload, coalesce=False, replace_action=False, limit=None
    ):
        with self.connection(write=True) as db:
            before = db.execute(
                "SELECT COALESCE((SELECT value FROM counts WHERE kind='queue' AND key=?),0)",
                (service,),
            ).fetchone()[0]
            file_id, diffid = (
                str(payload.get("remote_file_id", "")),
                str(payload.get("diffid", "")),
            )
            if coalesce and file_id.isdigit() and diffid.isdigit():
                rows = db.execute(
                    "SELECT seq,diffid FROM queue WHERE service=? AND file_id=?",
                    (service, file_id),
                ).fetchall()
                if any(d.isdigit() and int(d) > int(diffid) for _, d in rows):
                    return before, before, False
                db.executemany(
                    "DELETE FROM queue WHERE seq=?",
                    (
                        (seq,)
                        for seq, d in rows
                        if d.isdigit() and int(d) <= int(diffid)
                    ),
                )
            if replace_action:
                db.execute(
                    "DELETE FROM queue WHERE service=? AND path=? AND action=?",
                    (service, payload["path"], payload["action"]),
                )
            current = db.execute(
                "SELECT COALESCE((SELECT value FROM counts WHERE kind='queue' AND key=?),0)",
                (service,),
            ).fetchone()[0]
            if limit is not None and limit >= 0 and current >= limit:
                return before, current, False
            self.insert_queue(db, service, payload)
            self.put(db, "queue_updated", service, stamp())
            return before, current + 1, True

    def replace_queue(self, service, payload):
        with self.connection(write=True) as db:
            db.execute("DELETE FROM queue WHERE service=?", (service,))
            for row in payload:
                self.insert_queue(db, service, row)
            self.put(db, "queue_updated", service, stamp())

    def consume(self, service, ids, write=True):
        wanted = set(ids)
        with self.connection(write=write) as db:
            before = db.execute(
                "SELECT COALESCE((SELECT value FROM counts WHERE kind='queue' AND key=?),0)",
                (service,),
            ).fetchone()[0]
            found = set()
            for event_id in wanted:
                if db.execute(
                    "SELECT 1 FROM queue WHERE service=? AND event_id=? LIMIT 1",
                    (service, event_id),
                ).fetchone():
                    found.add(event_id)
            if write:
                db.executemany(
                    "DELETE FROM queue WHERE service=? AND event_id=?",
                    ((service, i) for i in found),
                )
                if found:
                    self.put(db, "queue_updated", service, stamp())
            # Legacy result counts describe the proposed state even in preview.
            removed = (
                sum(
                    db.execute(
                        "SELECT COUNT(*) FROM queue WHERE service=? AND event_id=?",
                        (service, i),
                    ).fetchone()[0]
                    for i in found
                )
                if not write
                else before
                - db.execute(
                    "SELECT COALESCE((SELECT value FROM counts WHERE kind='queue' AND key=?),0)",
                    (service,),
                ).fetchone()[0]
            )
            return (
                before,
                before - removed,
                tuple(sorted(found)),
                tuple(sorted(wanted - found)),
            )

    def ensure_ids(self, service, write=True):
        with self.connection(write=write) as db:
            count = db.execute(
                "SELECT COALESCE((SELECT value FROM counts WHERE kind='queue' AND key=?),0)",
                (service,),
            ).fetchone()[0]
            assigned = 0
            for seq, raw in db.execute(
                "SELECT seq,payload FROM queue WHERE service=? AND (event_id IS NULL OR trim(event_id)='')",
                (service,),
            ).fetchall():
                row = json.loads(raw)
                if not isinstance(row, dict):
                    row = {"path": str(row), "action": "sync", "reason": "-"}
                row["event_id"] = uuid.uuid4().hex
                assigned += 1
                if write:
                    db.execute(
                        "UPDATE queue SET event_id=?,path=?,action=?,payload=? WHERE seq=?",
                        (
                            row["event_id"],
                            row.get("path", ""),
                            row.get("action", "sync"),
                            encode(row),
                            seq,
                        ),
                    )
            return count, assigned

    def has_unresolved(self, service):
        with self.connection() as db:
            return (
                db.execute(
                    "SELECT 1 FROM attempts WHERE service=? AND (status NOT IN ('completed','failed','cancelled','released') OR phase IN ('unknown','child-uncertain','needs-recovery')) LIMIT 1",
                    (service,),
                ).fetchone()
                is not None
            )

    def attempts(self, service, attempt_id=None, pending=False):
        with self.connection() as db:
            sql = "SELECT payload FROM attempts WHERE service=?"
            args = [service]
            if attempt_id is not None:
                sql += " AND id=?"
                args.append(attempt_id)
            if pending:
                sql += " AND (status NOT IN ('completed','failed','cancelled','released') OR phase IN ('unknown','child-uncertain','needs-recovery'))"
            return [json.loads(r[0]) for r in db.execute(sql, args)]

    @staticmethod
    def put_attempt(db, service, item):
        db.execute(
            "INSERT INTO attempts VALUES (?,?,?,?,?) ON CONFLICT(service,id) DO UPDATE SET status=excluded.status,phase=excluded.phase,payload=excluded.payload",
            (
                service,
                item["attempt_id"],
                str(item.get("status", "in_progress")),
                str(item.get("phase", "unknown")),
                encode(item),
            ),
        )

    def save_attempt(self, service, item):
        with self.connection(write=True) as db:
            self.put_attempt(db, service, item)

    def load_state(self):
        with self.connection() as db:
            state = {
                k: json.loads(v)
                for k, v in db.execute(
                    "SELECT key,value FROM kv WHERE namespace='meta'"
                )
            }
        state.setdefault("schema", "pcloud-event-sync.v1")
        for namespace in ("baseline", "reviews", "settling"):
            state[namespace] = RowMap(self, namespace)
        if "reconcile" in state:
            state["reconcile"]["pending"] = PendingRows(self)
            state["reconcile"]["captured"] = (
                None  # Stored as event IDs; consumed inside SQLite.
            )
        return state

    def save_state(self, state, summary=None, review_filter=None):
        with self.connection(write=True) as db:
            db.execute("DELETE FROM kv WHERE namespace='meta'")
            for key, value in state.items():
                if key in ("baseline", "reviews", "settling"):
                    if isinstance(value, RowMap):
                        value.flush(db)
                    else:
                        db.execute("DELETE FROM kv WHERE namespace=?", (key,))
                        for p, row in value.items():
                            self.put(db, key, p, row)
                elif key == "reconcile":
                    self.put(
                        db,
                        "meta",
                        key,
                        {
                            k: v
                            for k, v in value.items()
                            if k not in ("pending", "captured")
                        },
                    )
                    pending = value.get("pending", [])
                    if isinstance(pending, PendingRows):
                        pending.flush(db)
                    else:
                        db.execute("DELETE FROM pending")
                        db.executemany(
                            "INSERT OR IGNORE INTO pending(path) VALUES (?)",
                            ((p,) for p in pending),
                        )
                    captured = value.get("captured")
                    if captured is not None:
                        db.execute("DELETE FROM captured")
                        for service, rows in captured.items():
                            db.executemany(
                                "INSERT OR IGNORE INTO captured VALUES (?,?)",
                                (
                                    (service, r["event_id"])
                                    for r in rows
                                    if isinstance(r, dict) and r.get("event_id")
                                ),
                            )
                else:
                    self.put(db, "meta", key, value)
            if "reconcile" not in state:
                db.execute("DELETE FROM pending")
                db.execute("DELETE FROM captured")
            if summary is not None:
                summary = dict(summary)
                from .review_classification import counts
                summary.update(counts(json.loads(raw) for path, raw in db.execute("SELECT key,value FROM kv WHERE namespace='reviews'")
                                      if review_filter is None or review_filter(path)))
                self.put(db, 'summary', 'event', summary)
        # Only clear dirty overlays after the transaction commits.
        for key in ("baseline", "reviews", "settling"):
            if isinstance(state.get(key), RowMap):
                state[key].reset()
            else:
                state[key] = RowMap(self, key)
        if "reconcile" in state:
            state["reconcile"]["pending"] = PendingRows(self)
            state["reconcile"]["captured"] = None

    def consume_captured(self):
        with self.connection(write=True) as db:
            for service in ("pushd", "diffd"):
                db.execute(
                    "DELETE FROM queue WHERE service=? AND event_id IN (SELECT event_id FROM captured WHERE service=?)",
                    (service, service),
                )
            db.execute("DELETE FROM captured")

    def check(self):
        with self.connection() as db:
            result = db.execute("PRAGMA quick_check").fetchone()[0]
            if result != "ok":
                raise ValueError("sync state database integrity check failed")
        return {
            "schema_version": VERSION,
            "integrity": result,
            "database": str(self.path),
        }


class QueueRows(Sequence):
    def __init__(self, store, service, paths=None):
        self.store, self.service, self.paths = store, service, paths
        with store.connection() as db:
            self.cut = db.execute(
                "SELECT COALESCE(MAX(seq),0) FROM queue WHERE service=?", (service,)
            ).fetchone()[0]

    def __len__(self):
        if self.paths is None:
            with self.store.connection() as db:
                return db.execute(
                    "SELECT COUNT(*) FROM queue WHERE service=? AND seq<=?",
                    (self.service, self.cut),
                ).fetchone()[0]
        return sum(1 for _ in self)

    def __iter__(self):
        with self.store.connection() as db:
            if self.paths is None:
                cursor = db.execute(
                    "SELECT payload FROM queue WHERE service=? AND seq<=? ORDER BY seq",
                    (self.service, self.cut),
                )
                for row in cursor:
                    yield json.loads(row[0])
            else:
                for path in self.paths:
                    for row in db.execute(
                        "SELECT payload FROM queue WHERE service=? AND path=? AND seq<=? ORDER BY seq",
                        (self.service, path, self.cut),
                    ):
                        yield json.loads(row[0])

    def __getitem__(self, index):
        if isinstance(index, slice):
            from itertools import islice

            return list(
                islice(iter(self), index.start or 0, index.stop, index.step or 1)
            )
        if index < 0:
            index += len(self)
        from itertools import islice

        try:
            return next(islice(iter(self), index, index + 1))
        except StopIteration:
            raise IndexError(index)


class RowMap(MutableMapping):
    def __init__(self, store, namespace):
        self.store, self.namespace = store, namespace
        self.reset()

    def reset(self):
        self.cache = {}
        self.original = {}
        self.deleted = set()

    def __getitem__(self, key):
        if key in self.deleted:
            raise KeyError(key)
        if key not in self.cache:
            missing = object()
            value = self.store.get(self.namespace, key, missing)
            if value is missing:
                raise KeyError(key)
            self.cache[key] = value
            self.original[key] = encode(value)
        return self.cache[key]

    def __setitem__(self, key, value):
        if key not in self.original:
            old = self.store.get(self.namespace, key)
            self.original[key] = encode(old) if old is not None else None
        self.deleted.discard(key)
        self.cache[key] = value

    def __delitem__(self, key):
        self[key]
        self.deleted.add(key)
        self.cache.pop(key, None)

    def __iter__(self):
        seen = set()
        with self.store.connection() as db:
            for (key,) in db.execute(
                "SELECT key FROM kv WHERE namespace=?", (self.namespace,)
            ):
                if key not in self.deleted:
                    seen.add(key)
                    yield key
        yield from (k for k in self.cache if k not in seen)

    def __len__(self):
        with self.store.connection() as db:
            n = db.execute(
                "SELECT COUNT(*) FROM kv WHERE namespace=?", (self.namespace,)
            ).fetchone()[0]
            added = sum(
                not db.execute(
                    "SELECT 1 FROM kv WHERE namespace=? AND key=?", (self.namespace, k)
                ).fetchone()
                for k in self.cache
            )
            removed = sum(
                bool(
                    db.execute(
                        "SELECT 1 FROM kv WHERE namespace=? AND key=?",
                        (self.namespace, k),
                    ).fetchone()
                )
                for k in self.deleted
            )
        return n + added - removed

    def flush(self, db):
        for k in self.deleted | set(self.cache):
            original = self.original.get(k)
            updated = None if k in self.deleted else encode(self.cache[k])
            if updated == original:
                continue
            if self.namespace == 'reviews':
                row = db.execute('SELECT value FROM kv WHERE namespace=? AND key=?', (self.namespace, k)).fetchone()
                if (row[0] if row else None) != original:
                    continue  # Independent verifier committed a newer review generation.
            if updated is None:
                db.execute('DELETE FROM kv WHERE namespace=? AND key=?', (self.namespace, k))
            else:
                self.store.put(db, self.namespace, k, self.cache[k])


class PendingRows(Sequence):
    def __init__(self, store):
        self.store = store
        self.removed = set()

    def __len__(self):
        return self.store.count("pending") - len(self.removed)

    def __iter__(self):
        with self.store.connection() as db:
            for (p,) in db.execute("SELECT path FROM pending ORDER BY seq"):
                if p not in self.removed:
                    yield p

    def __getitem__(self, index):
        if (
            isinstance(index, slice)
            and not self.removed
            and (index.step is None or index.step == 1)
        ):
            start = index.start or 0
            with self.store.connection() as db:
                return [
                    r[0]
                    for r in db.execute(
                        "SELECT path FROM pending ORDER BY seq LIMIT ? OFFSET ?",
                        (
                            -1 if index.stop is None else max(0, index.stop - start),
                            start,
                        ),
                    )
                ]
        from itertools import islice

        if isinstance(index, slice):
            return list(
                islice(iter(self), index.start or 0, index.stop, index.step or 1)
            )
        try:
            return next(islice(iter(self), index, index + 1))
        except StopIteration:
            raise IndexError(index)

    def discard_many(self, paths):
        with self.store.connection() as db:
            self.removed.update(
                p
                for p in paths
                if db.execute("SELECT 1 FROM pending WHERE path=?", (p,)).fetchone()
            )

    def flush(self, db):
        db.executemany("DELETE FROM pending WHERE path=?", ((p,) for p in self.removed))
