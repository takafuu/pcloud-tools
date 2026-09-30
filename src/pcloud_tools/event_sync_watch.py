"""NUL-framed fswatch/FSEvents adapter. No path-only debounce or guessed moves."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import tempfile
import time
import uuid

from .event_sync import Scope, request_reconciliation
from .event_sync_remote import SyncError, relative_path
from .io_utils import atomic_write_json
from .transfer_state import writer_state_lock
from .transfer_executor import _cleanup_process_group


def command(config, binary):
    return [binary, "--monitor=fsevents_monitor", "--recursive", "--latency=0.3",
            "--print0", "--event-flag-separator=,", "--format=%c\t%f\t%p", str(config.core_dir)]


def parse_record(raw: bytes, root: Path):
    parts = raw.decode("utf-8").split("\t", 2)
    if len(parts) != 3 or not parts[0].isdigit():
        raise SyncError("invalid rich fswatch record")
    absolute = Path(parts[2])
    try:
        path = absolute.relative_to(root).as_posix()
    except ValueError as exc:
        raise SyncError("fswatch path outside watch root") from exc
    flags = set(parts[1].split(","))
    return {"path": path, "file_id": int(parts[0]), "flags": flags}


def records_for_batch(events, root):
    """A confirmed pair needs two names, one existing inode, and a missing old name."""
    renames = defaultdict(list)
    for e in events:
        if "Renamed" in e["flags"] and e["file_id"]:
            renames[e["file_id"]].append(e)
    paired = set()
    records = []
    for file_id, names in renames.items():
        paths = list(dict.fromkeys(e["path"] for e in names))
        if len(paths) != 2:
            continue
        present = []
        missing = []
        for p in paths:
            try:
                s = (root / p).lstat()
                if s.st_ino == file_id and not (root / p).is_symlink():
                    present.append(p)
            except FileNotFoundError:
                missing.append(p)
        if len(present) != 1 or len(missing) != 1:
            continue
        old, new = missing[0], present[0]
        records.append({"path": old, "action": "move", "destination": new,
                        "file_id": file_id, "is_dir": (root / new).is_dir(), "reason": "fswatch:confirmed-move"})
        # Recheck contents at the final name even when the modified flag was coalesced.
        if not (root / new).is_dir():
            records.append({"path": new, "action": "upload", "reason": "fswatch:after-move"})
        paired.update(paths)
    for e in events:
        path, flags = e["path"], e["flags"]
        if path in paired or path == ".":
            continue
        if "IsDir" in flags:
            # Directory creation is enumerated by a scoped reconciliation; directory
            # deletion without a paired move cannot be inferred from child absence.
            if flags & {"Created", "Removed", "Renamed"}:
                records.append({"path": path, "action": "directory", "reason": "fswatch:directory", "flags": sorted(flags)})
            continue
        if "Renamed" in flags:
            action = "upload" if (root / path).is_file() else "delete"
        elif "Removed" in flags:
            action = "delete"
        else:
            action = "upload"
        records.append({"path": path, "action": action, "file_id": e["file_id"],
                        "flags": sorted(flags), "reason": "fswatch:" + ",".join(sorted(flags))})
    return records


def append_records(config, records):
    scope = Scope(config)
    queue = config.state_dir / "pushd" / "queue.json"
    from .sqlite_state import database_for
    store = database_for(queue)
    if store:
        with writer_state_lock(queue):
            for record in records:
                destination = record.get('destination')
                if not scope.allows(record['path']) and not (destination and scope.allows(destination)):
                    continue
                _, _, appended = store.append('pushd', {**record, 'event_id': uuid.uuid4().hex,
                    'observed_at': datetime.now(timezone.utc).isoformat()},
                    replace_action=record['action'] not in {'move','directory'})
                if not appended:
                    request_reconciliation(config, 'local event queue overflow')
                    break
        return
    with writer_state_lock(queue):
        data = json.loads(queue.read_text()) if queue.exists() else []
        if not isinstance(data, list):
            raise SyncError("local event queue is not a list")
        for record in records:
            path = record["path"]
            destination = record.get("destination")
            if not scope.allows(path) and not (destination and scope.allows(destination)):
                continue
            # Preserve move dependencies. Ordinary same-path updates replace only
            # their own generation; never drop updates merely due to a debounce timer.
            if record["action"] not in {"move", "directory"}:
                data = [r for r in data if not (isinstance(r, dict) and r.get("path") == path
                                              and r.get("action") == record["action"])]
            if len(data) >= config.pushd_queue_limit:
                request_reconciliation(config, "local event queue overflow")
                break
            data.append({**record, "event_id": uuid.uuid4().hex,
                         "observed_at": datetime.now(timezone.utc).isoformat()})
        atomic_write_json(queue, data)


def run(config, binary, max_events=None):
    state_file = config.state_dir / "pushd" / "fswatch-resident-last-run.json"
    result = {"command": command(config, binary), "started_at": datetime.now(timezone.utc).isoformat(),
              "status": "running", "watch format": "file-id,flags,path,NUL", "events": 0}
    request_reconciliation(config, "local monitor started or resumed")
    stopped = False
    eof = False
    def stop(_signum, _frame):
        nonlocal stopped
        stopped = True
    old_handlers = {s: signal.signal(s, stop) for s in (signal.SIGTERM, signal.SIGINT)}
    process = None
    try:
        with tempfile.TemporaryFile() as stderr:
            process = subprocess.Popen(result["command"], stdout=subprocess.PIPE, stderr=stderr,
                                       start_new_session=True)
            result["pid"] = process.pid
            atomic_write_json(state_file, result)
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
            pending, buffer = [], b""
            flush_at = time.monotonic() + 0.6
            while not stopped and process.poll() is None:
                for key, _ in selector.select(timeout=0.3):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        eof = True
                        break
                    buffer += chunk
                    while b"\0" in buffer:
                        raw, buffer = buffer.split(b"\0", 1)
                        event = parse_record(raw, config.core_dir)
                        if event["path"] == "." or event["flags"] & {"PlatformSpecific", "Overflow"}:
                            request_reconciliation(config, "FSEvents rescan or root change")
                        pending.append(event)
                        result["events"] += 1
                    if len(buffer) > 1024 * 1024:
                        raise SyncError("unterminated fswatch record")
                if eof:
                    break
                if pending and time.monotonic() >= flush_at:
                    append_records(config, records_for_batch(pending, config.core_dir))
                    pending.clear()
                    flush_at = time.monotonic() + 0.6
                    result["updated_at"] = datetime.now(timezone.utc).isoformat()
                    atomic_write_json(state_file, result)
                if max_events is not None and result["events"] >= max_events:
                    stopped = True
            if pending:
                append_records(config, records_for_batch(pending, config.core_dir))
            selector.close()
            result["cleanup"] = _cleanup_process_group(process, process.pid)
            result["returncode"] = process.poll()
            result["status"] = "stopped" if stopped else "failed"
    except BaseException:
        result["status"] = "failed"
        raise
    finally:
        if process is not None and process.poll() is None:
            _cleanup_process_group(process, process.pid)
        for s, handler in old_handlers.items():
            signal.signal(s, handler)
        request_reconciliation(config, "local monitor stopped")
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        atomic_write_json(state_file, result)
    return result
