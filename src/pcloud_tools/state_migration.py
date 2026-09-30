"""Explicit, offline, bounded-memory import of legacy sync JSON."""

from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import uuid

from .sqlite_state import Store, DB_NAME, stamp
from .transfer_state import writer_cutover_session, transfer_tick_lock_status


class JsonStream:
    """Decode one array item/object member at a time using stdlib's decoder."""

    def __init__(self, handle):
        self.handle = handle
        self.buf = ""
        self.pos = 0
        self.eof = False
        self.decoder = json.JSONDecoder()

    def fill(self):
        self.buf = self.buf[self.pos :]
        self.pos = 0
        chunk = self.handle.read(65536)
        self.buf += chunk
        if not chunk:
            self.eof = True

    def peek(self):
        while True:
            while self.pos < len(self.buf) and self.buf[self.pos].isspace():
                self.pos += 1
            if self.pos < len(self.buf):
                return self.buf[self.pos]
            if self.eof:
                return ""
            self.fill()

    def take(self, char):
        if self.peek() != char:
            raise ValueError("invalid legacy JSON structure")
        self.pos += 1

    def value(self):
        self.peek()
        while True:
            try:
                value, end = self.decoder.raw_decode(self.buf, self.pos)
                # A number might be split at a chunk boundary.
                if end == len(self.buf) and not self.eof:
                    self.fill()
                    continue
                self.pos = end
                return value
            except json.JSONDecodeError:
                if self.eof:
                    raise ValueError("invalid or truncated legacy JSON")
                self.fill()

    def array(self):
        self.take("[")
        if self.peek() == "]":
            self.take("]")
            return
        while True:
            yield self.value()
            if self.peek() == "]":
                self.take("]")
                return
            self.take(",")

    def keys(self):
        self.take("{")
        if self.peek() == "}":
            self.take("}")
            return
        while True:
            key = self.value()
            if not isinstance(key, str):
                raise ValueError("non-string object key")
            self.take(":")
            yield key
            if self.peek() == "}":
                self.take("}")
                return
            self.take(",")

    def finish(self):
        if self.peek():
            raise ValueError("trailing legacy JSON data")


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def sources(root):
    return [
        root / "event-sync/state.json",
        root / "pushd/queue.json",
        root / "diffd/remote-changes.json",
        root / "pushd/transfer-attempts.json",
        root / "diffd/transfer-attempts.json",
    ]


def preview(root):
    return {
        "database": str(root / DB_NAME),
        "active": (root / DB_NAME).exists(),
        "sources": [
            {"path": str(p), "bytes": p.stat().st_size}
            for p in sources(root)
            if p.exists()
        ],
        "requires": "stop all writers and finish/recover active transfers before migration",
    }


def migrate(root, config=None):
    root = Path(root)
    if (root / DB_NAME).exists():
        return {"already_migrated": True, **Store(root / DB_NAME).check()}
    root.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        for service in ("pushd", "diffd"):
            if transfer_tick_lock_status(root, service)["active"]:
                raise ValueError("executor must finish before migration")
            stack.enter_context(writer_cutover_session(root, service))
        backup = (
            root
            / "migrations"
            / (
                "sqlite-"
                + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                + "-"
                + uuid.uuid4().hex[:8]
            )
        )
        backup.mkdir(parents=True, mode=0o700)
        manifest = []
        for p in sources(root):
            if p.exists():
                dest = backup / p.relative_to(root)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, dest)
                sha = digest(p)
                if sha != digest(dest):
                    raise ValueError("migration backup mismatch")
                manifest.append(
                    {
                        "relative_path": str(p.relative_to(root)),
                        "sha256": sha,
                        "bytes": p.stat().st_size,
                    }
                )
        tmp = backup / "import.sqlite3"
        store = Store(tmp)
        store.initialize()
        with store.connection(write=True) as db:
            for source in sources(root):
                if not source.exists():
                    continue
                with source.open(encoding="utf-8") as handle:
                    stream = JsonStream(handle)
                    if source.name == "state.json":
                        schema = None
                        baseline_seen = False
                        for key in stream.keys():
                            if key in ("baseline", "reviews", "settling"):
                                if key == "baseline":
                                    baseline_seen = True
                                for path in stream.keys():
                                    value = stream.value()
                                    if not isinstance(value, dict):
                                        raise ValueError("invalid per-path state")
                                    Store.put(db, key, path, value)
                            elif key == "reconcile":
                                meta = {}
                                for field in stream.keys():
                                    if field == "pending":
                                        for path in stream.array():
                                            if not isinstance(path, str):
                                                raise ValueError("invalid pending path")
                                            db.execute(
                                                "INSERT INTO pending(path) VALUES (?)",
                                                (path,),
                                            )
                                    elif field == "captured":
                                        for service in stream.keys():
                                            if service not in ("pushd", "diffd"):
                                                raise ValueError(
                                                    "invalid captured service"
                                                )
                                            for record in stream.array():
                                                if isinstance(
                                                    record, dict
                                                ) and record.get("event_id"):
                                                    db.execute(
                                                        "INSERT OR IGNORE INTO captured VALUES (?,?)",
                                                        (service, record["event_id"]),
                                                    )
                                    else:
                                        meta[field] = stream.value()
                                Store.put(db, "meta", "reconcile", meta)
                            else:
                                value = stream.value()
                                Store.put(db, "meta", key, value)
                                if key == "schema":
                                    schema = value
                        if schema != "pcloud-event-sync.v1" or not baseline_seen:
                            raise ValueError("unsupported legacy event state")
                    elif source.name == "transfer-attempts.json":
                        for record in stream.array():
                            if not isinstance(record, dict) or not record.get(
                                "attempt_id"
                            ):
                                raise ValueError("invalid attempt record")
                            if record.get("status", "in_progress") not in (
                                "completed",
                                "failed",
                                "cancelled",
                                "released",
                            ) or record.get("phase", "unknown") in (
                                "unknown",
                                "child-uncertain",
                                "needs-recovery",
                            ):
                                raise ValueError(
                                    "unfinished transfer must be recovered before migration"
                                )
                            Store.put_attempt(db, source.parent.name, record)
                    else:
                        for row in stream.array():
                            Store.insert_queue(db, source.parent.name, row)
                        Store.put(
                            db,
                            "queue_updated",
                            source.parent.name,
                            datetime.fromtimestamp(
                                source.stat().st_mtime, timezone.utc
                            ).isoformat(),
                        )
                    stream.finish()
            Store.put(db, "migration", "completed_at", stamp())
        if config is not None:
            from .event_sync_status import aggregate

            state = store.load_state()
            store.save_state(state, aggregate(config, state))
        report = {
            **store.check(),
            "backup": str(backup),
            "sources": manifest,
            "queues": {s: store.count("queue", s) for s in ("pushd", "diffd")},
            "remaining": store.count("pending"),
        }
        # All input was checked and committed before the single activation rename.
        for row in manifest:
            if digest(root / row["relative_path"]) != row["sha256"]:
                raise ValueError("source changed during migration")
        with tmp.open("rb") as f:
            os.fsync(f.fileno())
        os.replace(tmp, root / DB_NAME)
        fd = os.open(root, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        # Fail closed for old runtimes; originals remain in the verified backup.
        from .io_utils import atomic_write_json

        for p in sources(root):
            # Use text writer deliberately: these are old-runtime guard files,
            # not writes through the SQLite collection adapter.
            from .io_utils import atomic_write_text

            atomic_write_text(
                p,
                json.dumps(
                    {
                        "storage": "sqlite",
                        "database": DB_NAME,
                        "migration_backup": str(backup),
                    }
                )
                + "\n",
            )
        report["database"] = str(root / DB_NAME)
        atomic_write_json(backup / "receipt.json", report)
        return report
