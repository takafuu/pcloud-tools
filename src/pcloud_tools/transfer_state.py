"""Concurrency-safe state helpers for queue based transfers.

The transfer queue predates the bounded executor and contains both strings and
objects.  This module deliberately treats ``event_id`` as an additive field:
old readers can continue to consume the existing path/action/reason keys and
new readers can identify the exact queue generation that was selected.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import atexit
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .io_utils import atomic_write_json

try:  # pragma: no cover - fcntl is present on the supported Unix hosts.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


EVENT_ID_KEY = "event_id"
_LOCK_TIMEOUT_SECONDS = 30.0
_LOCAL_LOCKS: dict[str, tuple[threading.RLock, int, Any]] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()
_PROCESS_WRITER_LEASES: dict[str, Any] = {}
_PROCESS_WRITER_SHARED_GATES: dict[str, list[Any]] = {}
_PROCESS_WRITER_LEASES_GUARD = threading.Lock()


class TransferStateError(RuntimeError):
    """Raised when a state lock or state transition cannot be completed."""


@dataclass(frozen=True)
class StateIssue:
    key: str
    message: str
    level: str = "error"


@dataclass(frozen=True)
class EventIdUpdateResult:
    file: Path
    before_count: int
    after_count: int
    assigned_count: int
    event_ids: tuple[str, ...] = ()
    issue: StateIssue | None = None


@dataclass(frozen=True)
class QueueRecord:
    """A queue item together with its preserved raw payload."""

    event_id: str
    path: str
    action: str
    reason: str
    payload: dict[str, Any] = field(default_factory=dict)
    index: int = 0


@dataclass(frozen=True)
class QueueSnapshot:
    file: Path
    generation: str
    records: tuple[QueueRecord, ...]
    raw_records: tuple[Any, ...]
    issue: StateIssue | None = None


@dataclass(frozen=True)
class ConsumeResult:
    file: Path
    before_count: int
    after_count: int
    removed_count: int
    removed_event_ids: tuple[str, ...]
    stale_event_ids: tuple[str, ...]
    issue: StateIssue | None = None


@dataclass(frozen=True)
class AttemptResult:
    attempt_id: str
    file: Path
    phase: str
    status: str
    issue: StateIssue | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_generation(records: list[Any]) -> str:
    encoded = json.dumps(records, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _lock_path(target: Path) -> Path:
    # Keep lock files beside the state file.  A persistent lock inode avoids
    # the unlink/recreate race that directory locks can have between waiters.
    return target.with_name(f".{target.name}.lock")


def new_event_id(prefix: str | None = None) -> str:
    """Return a unique queue-generation identifier for a newly observed event."""

    value = uuid.uuid4().hex
    clean_prefix = str(prefix or "").strip()
    return f"{clean_prefix}-{value}" if clean_prefix else value


@contextlib.contextmanager
def state_lock(
    target: Path,
    *,
    timeout_seconds: float = _LOCK_TIMEOUT_SECONDS,
    blocking: bool = True,
) -> Iterator[None]:
    """Acquire a process and thread safe lock for *target*.

    The lock is advisory and short lived.  Callers must not hold it while a
    child transfer waits on the network.  A lock file is retained for stable
    inode identity; ownership is released by ``flock`` when the descriptor
    closes, so a crashed writer cannot leave a permanently held queue lock.
    """

    key = str(target.expanduser().resolve())
    with _LOCAL_LOCKS_GUARD:
        entry = _LOCAL_LOCKS.get(key)
        if entry is None:
            entry = (threading.RLock(), 0, None)
            _LOCAL_LOCKS[key] = entry
        local_lock, depth, fd = entry
    acquired_local = local_lock.acquire(blocking=blocking)
    if not acquired_local:
        raise TransferStateError(f"state lock is busy: {target}")
    try:
        with _LOCAL_LOCKS_GUARD:
            _, current_depth, current_fd = _LOCAL_LOCKS[key]
            if current_depth > 0:
                _LOCAL_LOCKS[key] = (local_lock, current_depth + 1, current_fd)
                nested = True
            else:
                nested = False
        fd_handle = None
        if not nested:
            lock_file = _lock_path(target)
            lock_file.parent.mkdir(parents=True, exist_ok=True)
            fd_handle = lock_file.open("a+")
            if fcntl is not None:
                flags = fcntl.LOCK_EX
                if not blocking:
                    flags |= fcntl.LOCK_NB
                deadline = time.monotonic() + max(0.0, timeout_seconds)
                while True:
                    try:
                        fcntl.flock(fd_handle.fileno(), flags)
                        break
                    except BlockingIOError:
                        if not blocking or time.monotonic() >= deadline:
                            fd_handle.close()
                            raise TransferStateError(f"state lock is busy: {target}")
                        time.sleep(0.01)
            fd_handle.seek(0)
            fd_handle.truncate()
            fd_handle.write(json.dumps({"pid": os.getpid(), "acquired_at": _utc_now()}))
            fd_handle.flush()
            with _LOCAL_LOCKS_GUARD:
                _, current_depth, _ = _LOCAL_LOCKS[key]
                _LOCAL_LOCKS[key] = (local_lock, current_depth + 1, fd_handle)
        try:
            yield
        finally:
            with _LOCAL_LOCKS_GUARD:
                _, current_depth, current_fd = _LOCAL_LOCKS[key]
                new_depth = max(0, current_depth - 1)
                _LOCAL_LOCKS[key] = (local_lock, new_depth, current_fd)
            if not nested and fd_handle is not None:
                if fcntl is not None:
                    with contextlib.suppress(OSError):
                        fcntl.flock(fd_handle.fileno(), fcntl.LOCK_UN)
                fd_handle.close()
    finally:
        local_lock.release()


def writer_cutover_lock_path(state_dir: Path, service: str) -> Path:
    """Return the shared writer barrier for one queue/journal family.

    Every writer for a service uses this same barrier, regardless of whether
    it is a watcher, poller, executor, manual command, or backfill process.
    A cutover can therefore hold one lock while it proves that no old writer
    can start alongside the replacement.
    """

    return state_dir / service / "writer-cutover.lock"


def writer_lifetime_lock_path(state_dir: Path, service: str) -> Path:
    """Return the service gate used by explicit writer lifecycle sessions.

    Ordinary queue updates do not use this file.  A writer lifecycle session
    takes a shared flock on it for the duration of a process, while a
    cutover takes an exclusive flock.  That makes a cutover a real stop
    boundary without turning every short queue update into a process lease.
    """

    return state_dir / service / "writer-lifetime.lock"


def writer_process_lease_path(state_dir: Path, service: str, target: Path) -> Path:
    """Return the legacy deterministic lease path for one writer target.

    The path is retained for compatibility with status and older operators.
    New writer processes use a unique lease from :func:`writer_process_session`
    so several same-generation writers can coexist safely.
    """

    resolved_target = target.expanduser().resolve()
    digest = hashlib.sha256(str(resolved_target).encode()).hexdigest()
    return state_dir / service / "writer-leases" / f"{digest}.lock"


def writer_process_lease_dir(state_dir: Path, service: str) -> Path:
    """Return the directory containing explicit process-session leases."""

    return state_dir / service / "writer-leases"


def _writer_session_lease_path(state_dir: Path, service: str) -> Path:
    token = f"{os.getpid()}-{uuid.uuid4().hex}"
    return writer_process_lease_dir(state_dir, service) / f"{token}.lock"


def _write_writer_lease_metadata(
    handle: Any,
    *,
    mode: str,
    generation: str | None = None,
) -> None:
    payload: dict[str, object] = {
        "pid": os.getpid(),
        "mode": mode,
        "acquired_at": _utc_now(),
    }
    if generation is not None:
        payload["generation"] = generation
    handle.seek(0)
    handle.truncate()
    handle.write(json.dumps(payload, sort_keys=True))
    handle.flush()


def _acquire_flock(
    handle: Any,
    operation: int,
    *,
    blocking: bool,
    timeout_seconds: float,
    description: str,
) -> None:
    if fcntl is None:  # pragma: no cover - supported hosts provide fcntl.
        raise TransferStateError(f"{description} requires fcntl")
    flags = operation
    if not blocking:
        flags |= fcntl.LOCK_NB
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while True:
        try:
            fcntl.flock(handle.fileno(), flags)
            return
        except BlockingIOError as exc:
            if not blocking or time.monotonic() >= deadline:
                raise TransferStateError(f"{description} is busy") from exc
            time.sleep(0.01)


def _open_writer_lifetime_lease(
    state_dir: Path,
    service: str,
    *,
    mode: str,
    target: Path | None = None,
    blocking: bool = False,
    timeout_seconds: float = _LOCK_TIMEOUT_SECONDS,
) -> Any:
    """Open an exclusive service or compatibility writer lease."""

    path = (
        writer_process_lease_path(state_dir, service, target)
        if target is not None
        else writer_lifetime_lock_path(state_dir, service)
    )
    key = str(path.expanduser().resolve())
    with _PROCESS_WRITER_LEASES_GUARD:
        if key in _PROCESS_WRITER_LEASES:
            raise TransferStateError(f"writer lifetime lease is already held by this process: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        _acquire_flock(
            handle,
            fcntl.LOCK_EX if fcntl is not None else 0,
            blocking=blocking,
            timeout_seconds=timeout_seconds,
            description=f"writer lifetime lease {path}",
        )
        _write_writer_lease_metadata(handle, mode=mode)
    except BaseException:
        with contextlib.suppress(OSError):
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
        raise
    with _PROCESS_WRITER_LEASES_GUARD:
        # A second thread can only reach this point after the flock operation;
        # keep the first local owner authoritative and close the duplicate.
        existing = _PROCESS_WRITER_LEASES.get(key)
        if existing is not None:
            if fcntl is not None:
                with contextlib.suppress(OSError):
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
            raise TransferStateError(f"writer lifetime lease is already held by this process: {path}")
        _PROCESS_WRITER_LEASES[key] = handle
    return handle


def _open_writer_shared_gate(
    state_dir: Path,
    service: str,
    *,
    blocking: bool,
    timeout_seconds: float,
) -> tuple[Path, Any]:
    """Open a shared writer gate held for one explicit process session."""

    path = writer_lifetime_lock_path(state_dir, service)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        _acquire_flock(
            handle,
            fcntl.LOCK_SH if fcntl is not None else 0,
            blocking=blocking,
            timeout_seconds=timeout_seconds,
            description=f"writer process session gate {path}",
        )
    except BaseException:
        handle.close()
        raise
    key = str(path.expanduser().resolve())
    with _PROCESS_WRITER_LEASES_GUARD:
        _PROCESS_WRITER_SHARED_GATES.setdefault(key, []).append(handle)
    return path, handle


def _open_writer_process_lease(
    state_dir: Path,
    service: str,
    *,
    generation: str | None,
) -> tuple[Path, Any]:
    """Create a unique lease for an explicit writer process session."""

    path = _writer_session_lease_path(state_dir, service)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        _acquire_flock(
            handle,
            fcntl.LOCK_EX if fcntl is not None else 0,
            blocking=False,
            timeout_seconds=0,
            description=f"writer process lease {path}",
        )
        _write_writer_lease_metadata(handle, mode="writer-session", generation=generation)
    except BaseException:
        with contextlib.suppress(OSError):
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
        with contextlib.suppress(OSError):
            path.unlink()
        raise
    key = str(path.expanduser().resolve())
    with _PROCESS_WRITER_LEASES_GUARD:
        _PROCESS_WRITER_LEASES[key] = handle
    return path, handle


def _release_writer_handle(path: Path, handle: Any) -> None:
    key = str(path.expanduser().resolve())
    with _PROCESS_WRITER_LEASES_GUARD:
        current = _PROCESS_WRITER_LEASES.get(key)
        if current is not handle:
            return
        _PROCESS_WRITER_LEASES.pop(key, None)
    if fcntl is not None:
        with contextlib.suppress(OSError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    with contextlib.suppress(OSError):
        handle.close()


def _release_shared_gate(path: Path, handle: Any) -> None:
    key = str(path.expanduser().resolve())
    with _PROCESS_WRITER_LEASES_GUARD:
        handles = _PROCESS_WRITER_SHARED_GATES.get(key, [])
        with contextlib.suppress(ValueError):
            handles.remove(handle)
        if handles:
            _PROCESS_WRITER_SHARED_GATES[key] = handles
        else:
            _PROCESS_WRITER_SHARED_GATES.pop(key, None)
    if fcntl is not None:
        with contextlib.suppress(OSError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    with contextlib.suppress(OSError):
        handle.close()


def _release_writer_lifetime_lease(
    state_dir: Path,
    service: str,
    handle: Any,
    *,
    target: Path | None = None,
) -> None:
    path = (
        writer_process_lease_path(state_dir, service, target)
        if target is not None
        else writer_lifetime_lock_path(state_dir, service)
    )
    _release_writer_handle(path, handle)


def _release_all_writer_lifetime_leases() -> None:  # pragma: no cover - atexit.
    with _PROCESS_WRITER_LEASES_GUARD:
        leases = list(_PROCESS_WRITER_LEASES.items())
        _PROCESS_WRITER_LEASES.clear()
        shared = [
            (key, handle)
            for key, handles in _PROCESS_WRITER_SHARED_GATES.items()
            for handle in handles
        ]
        _PROCESS_WRITER_SHARED_GATES.clear()
    for _, handle in leases + shared:
        if fcntl is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            handle.close()


atexit.register(_release_all_writer_lifetime_leases)


def _active_writer_process_leases(state_dir: Path, service: str) -> tuple[Path, ...]:
    """Return explicit process-session leases held by any process."""

    lease_dir = writer_process_lease_dir(state_dir, service)
    if not lease_dir.exists():
        return ()
    active: list[Path] = []
    with _PROCESS_WRITER_LEASES_GUARD:
        local_keys = {
            key
            for key in _PROCESS_WRITER_LEASES
            if key.startswith(str(lease_dir.expanduser().resolve()) + os.sep)
        }
    for path in sorted(lease_dir.glob("*.lock")):
        resolved_key = str(path.expanduser().resolve())
        if resolved_key in local_keys:
            active.append(path)
            continue
        if fcntl is None:  # pragma: no cover
            active.append(path)
            continue
        try:
            handle = path.open("a+")
        except OSError:
            active.append(path)
            continue
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                active.append(path)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
    return tuple(active)


def _active_writer_state_locks(state_dir: Path, service: str) -> tuple[Path, ...]:
    """Return state locks held by ordinary writers during a cutover check."""

    service_root = (state_dir / service).expanduser().resolve()
    shared_path_root = (state_dir / "transfer-path-locks").expanduser().resolve()
    barrier = writer_cutover_lock_path(state_dir, service).expanduser().resolve()
    lifetime = writer_lifetime_lock_path(state_dir, service).expanduser().resolve()
    excluded = {
        barrier,
        lifetime,
        _lock_path(barrier).expanduser().resolve(),
        _lock_path(lifetime).expanduser().resolve(),
    }
    lease_dir = writer_process_lease_dir(state_dir, service).expanduser().resolve()
    paths: set[Path] = set()
    with _LOCAL_LOCKS_GUARD:
        local_entries = {
            key: entry[1]
            for key, entry in _LOCAL_LOCKS.items()
            if entry[1] > 0
        }
    scan_roots = (service_root, shared_path_root)
    for scan_root in scan_roots:
        if not scan_root.exists():
            continue
        for raw in scan_root.rglob("*.lock"):
            path = raw.expanduser().resolve()
            if path in excluded or lease_dir in path.parents:
                continue
            key = str(path)
            if local_entries.get(key, 0) > 0:
                paths.add(path)
                continue
            if fcntl is None:  # pragma: no cover
                paths.add(path)
                continue
            try:
                handle = path.open("a+")
            except OSError:
                paths.add(path)
                continue
            try:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    paths.add(path)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
    return tuple(sorted(paths))


@contextlib.contextmanager
def writer_lifetime_lock(
    state_dir: Path,
    service: str,
    *,
    mode: str = "writer",
) -> Iterator[None]:
    """Hold an explicit exclusive service gate.

    Prefer :func:`writer_process_session` for an ordinary long-lived writer
    and :func:`writer_cutover_session` for an update or rollback boundary.
    """

    handle = _open_writer_lifetime_lease(state_dir, service, mode=mode)
    try:
        yield
    finally:
        _release_writer_lifetime_lease(state_dir, service, handle)


@contextlib.contextmanager
def writer_cutover_lock(
    state_dir: Path,
    service: str,
    *,
    blocking: bool = True,
    timeout_seconds: float = _LOCK_TIMEOUT_SECONDS,
) -> Iterator[None]:
    """Hold the cross-role writer barrier for one short state transition."""

    with state_lock(
        writer_cutover_lock_path(state_dir, service),
        blocking=blocking,
        timeout_seconds=timeout_seconds,
    ):
        yield


@contextlib.contextmanager
def writer_process_session(
    state_dir: Path,
    service: str,
    *,
    generation: str | None = None,
    blocking: bool = False,
    timeout_seconds: float = _LOCK_TIMEOUT_SECONDS,
) -> Iterator[None]:
    """Register one long-lived writer process or generation.

    Sessions take a shared service gate and a unique lease.  Multiple writers
    from the same generation can therefore coexist, while an explicit
    cutover takes the exclusive service gate and waits for every session to
    release.  The lease is held across network waits; ``writer_state_lock``
    remains a short read-modify-write lock and never creates this lease.
    """

    gate_path, gate_handle = _open_writer_shared_gate(
        state_dir,
        service,
        blocking=blocking,
        timeout_seconds=timeout_seconds,
    )
    lease_path: Path | None = None
    lease_handle: Any | None = None
    try:
        with writer_cutover_lock(
            state_dir,
            service,
            blocking=blocking,
            timeout_seconds=timeout_seconds,
        ):
            lease_path, lease_handle = _open_writer_process_lease(
                state_dir,
                service,
                generation=generation,
            )
        yield
    finally:
        if lease_path is not None and lease_handle is not None:
            _release_writer_handle(lease_path, lease_handle)
        _release_shared_gate(gate_path, gate_handle)


# The generation name is useful to callers that model a package update as a
# writer generation rather than as a process.  Keep one implementation so the
# two APIs cannot drift.
writer_generation_session = writer_process_session


@contextlib.contextmanager
def writer_cutover_session(
    state_dir: Path,
    service: str,
    *,
    blocking: bool = False,
    timeout_seconds: float = _LOCK_TIMEOUT_SECONDS,
) -> Iterator[None]:
    """Hold the complete stop-and-switch boundary for one service.

    The exclusive lifetime gate prevents replacement sessions from joining
    while the barrier is held.  Existing process sessions and ordinary state
    locks are checked before the caller is allowed to update or downgrade.
    """

    active_before_gate = _active_writer_process_leases(state_dir, service)
    if active_before_gate:
        raise TransferStateError(
            "writer cutover requires all writer processes to stop: "
            + ", ".join(str(path) for path in active_before_gate)
        )
    handle = _open_writer_lifetime_lease(
        state_dir,
        service,
        mode="cutover",
        blocking=blocking,
        timeout_seconds=timeout_seconds,
    )
    try:
        with writer_cutover_lock(
            state_dir,
            service,
            blocking=blocking,
            timeout_seconds=timeout_seconds,
        ):
            active_leases = _active_writer_process_leases(state_dir, service)
            active_state = _active_writer_state_locks(state_dir, service)
            if active_leases or active_state:
                active = (*active_leases, *active_state)
                raise TransferStateError(
                    "writer cutover requires all writer processes to stop: "
                    + ", ".join(str(path) for path in active)
                )
            yield
    finally:
        _release_writer_lifetime_lease(state_dir, service, handle)


def writer_lifetime_lock_status(state_dir: Path, service: str) -> dict[str, object]:
    """Inspect explicit writer sessions and the cutover gate without writes."""

    path = writer_lifetime_lock_path(state_dir, service)
    key = str(path.expanduser().resolve())
    with _PROCESS_WRITER_LEASES_GUARD:
        owned_here = key in _PROCESS_WRITER_LEASES
        shared_here = bool(_PROCESS_WRITER_SHARED_GATES.get(key))
    process_leases = _active_writer_process_leases(state_dir, service)
    details: dict[str, object] = {
        "path": str(path),
        "active": owned_here or shared_here or bool(process_leases),
        "status": "active" if owned_here or shared_here or process_leases else "free",
        "owner_pid": os.getpid() if owned_here or shared_here else None,
        "mode": "cutover" if owned_here else "writer-session" if shared_here else None,
        "target_lease_count": len(process_leases),
        "target_lease_paths": [str(item) for item in process_leases],
        "process_lease_count": len(process_leases),
        "process_lease_paths": [str(item) for item in process_leases],
    }
    if fcntl is None or owned_here or shared_here:
        return details
    if not path.exists():
        return details
    try:
        handle = path.open("a+")
    except OSError as exc:
        details.update({"status": "unknown", "issue": str(exc)})
        return details
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            details.update({"active": True, "status": "active"})
            try:
                handle.seek(0)
                payload = json.loads(handle.read() or "{}")
            except (OSError, json.JSONDecodeError):
                payload = {}
            if isinstance(payload, dict):
                details["owner_pid"] = payload.get("pid")
                details["mode"] = payload.get("mode")
            return details
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return details
    finally:
        handle.close()


def _writer_service_for_target(target: Path) -> tuple[Path, str] | None:
    resolved = target.expanduser().resolve()
    for index in range(len(resolved.parts) - 1, -1, -1):
        service = resolved.parts[index]
        if service in {"pushd", "diffd"} and index > 0:
            return Path(*resolved.parts[:index]), service
    return None


@contextlib.contextmanager
def writer_state_lock(
    target: Path,
    *,
    service: str | None = None,
    state_dir: Path | None = None,
    timeout_seconds: float = _LOCK_TIMEOUT_SECONDS,
    blocking: bool = True,
    hold_process_lease: bool | None = None,
) -> Iterator[None]:
    """Acquire only the short service barrier and target state lock.

    Network work must never run under this context.  Long-lived writer
    processes that need update/downgrade fencing should opt into
    :func:`writer_process_session`; ``hold_process_lease`` is accepted for
    source compatibility but deliberately has no effect.
    """

    del hold_process_lease
    resolved_target = target.expanduser().resolve()
    inferred = _writer_service_for_target(resolved_target)
    resolved_service = service or (inferred[1] if inferred else None)
    resolved_state_dir = state_dir or (inferred[0] if inferred else None)
    if resolved_service and resolved_state_dir:
        with writer_cutover_lock(
            resolved_state_dir,
            resolved_service,
            blocking=blocking,
            timeout_seconds=timeout_seconds,
        ):
            with state_lock(
                resolved_target,
                blocking=blocking,
                timeout_seconds=timeout_seconds,
            ):
                yield
    else:
        with state_lock(
            resolved_target,
            blocking=blocking,
            timeout_seconds=timeout_seconds,
        ):
            yield

def _read_payload(path: Path, key_prefix: str) -> tuple[list[Any], StateIssue | None]:
    if not path.exists():
        return [], None
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return [], StateIssue(key=key_prefix, message=f"cannot read state file {path}: {exc}")
    if not isinstance(payload, list):
        return [], StateIssue(key=key_prefix, message=f"state must be a JSON list: {path}")
    return payload, None


def _record_from_payload(item: Any, index: int) -> QueueRecord | None:
    if isinstance(item, str):
        return QueueRecord(event_id="", path=item.strip().replace("\\", "/").lstrip("./"), action="sync", reason="-", payload={"path": item}, index=index)
    if not isinstance(item, dict):
        return None
    event_id = item.get(EVENT_ID_KEY)
    return QueueRecord(
        event_id=str(event_id).strip() if event_id else "",
        path=str(item.get("path", "")).strip().replace("\\", "/").lstrip("./"),
        action=str(item.get("action", item.get("op", "sync"))),
        reason=str(item.get("reason", "-")),
        payload=dict(item),
        index=index,
    )


def read_queue_snapshot(path: Path, key_prefix: str = "PCLOUD_TOOLS_TRANSFER_STATE") -> QueueSnapshot:
    payload, issue = _read_payload(path, key_prefix)
    records = tuple(
        record
        for index, item in enumerate(payload)
        if (record := _record_from_payload(item, index)) is not None
    )
    return QueueSnapshot(
        file=path,
        generation=_json_generation(payload),
        records=records,
        raw_records=tuple(payload),
        issue=issue,
    )


def ensure_event_ids(
    path: Path,
    key_prefix: str = "PCLOUD_TOOLS_TRANSFER_STATE",
    *,
    write: bool = True,
) -> EventIdUpdateResult:
    """Assign IDs to legacy items under the queue lock.

    ``write=False`` is suitable for preview/status and never mutates state.
    Existing IDs and all unknown fields are preserved byte-for-byte at the
    object level (the JSON writer may reformat whitespace).
    """

    try:
        lock_context = writer_state_lock(path) if write else contextlib.nullcontext()
        with lock_context:
            payload, issue = _read_payload(path, key_prefix)
            if issue:
                return EventIdUpdateResult(path, 0, 0, 0, issue=issue)
            ids: list[str] = []
            assigned = 0
            updated: list[Any] = []
            for item in payload:
                if isinstance(item, dict):
                    current = str(item.get(EVENT_ID_KEY) or "").strip()
                    if not current:
                        current = uuid.uuid4().hex
                        item = {**item, EVENT_ID_KEY: current}
                        assigned += 1
                    ids.append(current)
                    updated.append(item)
                else:
                    current = uuid.uuid4().hex
                    ids.append(current)
                    updated.append(
                        {
                            "path": str(item),
                            "action": "sync",
                            "reason": "-",
                            EVENT_ID_KEY: current,
                        }
                    )
                    assigned += 1
            if write and assigned:
                atomic_write_json(path, updated)
            return EventIdUpdateResult(path, len(payload), len(updated), assigned, tuple(ids))
    except (OSError, TransferStateError) as exc:
        return EventIdUpdateResult(
            path,
            0,
            0,
            0,
            issue=StateIssue(key=key_prefix, message=f"cannot update state file {path}: {exc}"),
        )


def consume_event_ids(
    path: Path,
    event_ids: set[str] | tuple[str, ...] | list[str],
    key_prefix: str = "PCLOUD_TOOLS_TRANSFER_STATE",
    *,
    write: bool = True,
) -> ConsumeResult:
    """Remove only the selected queue generations.

    A record with a matching path but a newer ``event_id`` remains queued.  A
    missing ID is reported as stale and is never interpreted as success.
    """

    wanted = {str(value).strip() for value in event_ids if str(value).strip()}
    if not wanted:
        return ConsumeResult(path, 0, 0, 0, (), ())
    try:
        lock_context = writer_state_lock(path) if write else contextlib.nullcontext()
        with lock_context:
            payload, issue = _read_payload(path, key_prefix)
            if issue:
                return ConsumeResult(path, 0, 0, 0, (), tuple(sorted(wanted)), issue)
            current_ids = {
                str(item.get(EVENT_ID_KEY)).strip()
                for item in payload
                if isinstance(item, dict) and item.get(EVENT_ID_KEY)
            }
            removed_ids = tuple(sorted(wanted & current_ids))
            stale_ids = tuple(sorted(wanted - current_ids))
            updated = [
                item
                for item in payload
                if not (isinstance(item, dict) and str(item.get(EVENT_ID_KEY) or "").strip() in wanted)
            ]
            if write and removed_ids:
                atomic_write_json(path, updated)
            return ConsumeResult(path, len(payload), len(updated), len(removed_ids), removed_ids, stale_ids)
    except (OSError, TransferStateError) as exc:
        return ConsumeResult(
            path,
            0,
            0,
            0,
            (),
            tuple(sorted(wanted)),
            StateIssue(key=key_prefix, message=f"cannot consume state file {path}: {exc}"),
        )


def path_lock_path(state_dir: Path, service: str, path: str) -> Path:
    normalized = str(path).strip().replace("\\", "/").lstrip("./")
    digest = hashlib.sha256(normalized.casefold().encode()).hexdigest()
    # Direction is deliberately absent from this path.  pushd and diffd can
    # otherwise write the same remote/local path at the same time from
    # separate processes, defeating the transfer safety contract.
    return state_dir / "transfer-path-locks" / f"{digest}.lock"


def transfer_tick_lock_path(state_dir: Path, service: str) -> Path:
    """Return the per-service executor lock used to prevent overlapping ticks."""

    return state_dir / service / "transfer-tick.lock"


def transfer_tick_lock_status(state_dir: Path, service: str) -> dict[str, object]:
    """Inspect whether another process currently owns the service tick lock.

    The executor uses a persistent lock inode, so probing the lock sidecar is
    safe after a process crash. This function never acquires ``state_lock``
    and never updates the sidecar metadata; callers use it only to distinguish
    normal scheduler contention from an attempt whose lock has been released.
    """

    target = transfer_tick_lock_path(state_dir, service)
    lock_path = _lock_path(target)
    key = str(target.expanduser().resolve())
    with _LOCAL_LOCKS_GUARD:
        owned_here = bool(_LOCAL_LOCKS.get(key, (None, 0, None))[1])
    details: dict[str, object] = {
        "path": str(target),
        "lock path": str(lock_path),
        "active": owned_here,
        "owned here": owned_here,
        "owner pid": os.getpid() if owned_here else None,
        "status": "active" if owned_here else "free",
    }
    if fcntl is None or owned_here:
        if fcntl is None:
            details.update({"status": "unknown", "issue": "fcntl is unavailable"})
        return details
    if not lock_path.exists():
        return details
    try:
        handle = lock_path.open("r")
    except OSError as exc:
        details.update({"status": "unknown", "issue": str(exc)})
        return details
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            details.update({"active": True, "status": "active"})
            try:
                handle.seek(0)
                payload = json.loads(handle.read() or "{}")
            except (OSError, json.JSONDecodeError):
                payload = {}
            if isinstance(payload, dict):
                details["owner pid"] = payload.get("pid")
            return details
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return details
    finally:
        handle.close()


@contextlib.contextmanager
def transfer_tick_lock(
    state_dir: Path,
    service: str,
    *,
    blocking: bool = False,
    timeout_seconds: float = 0.0,
) -> Iterator[None]:
    """Hold one service's executor tick lock for the selected batch."""

    with state_lock(
        transfer_tick_lock_path(state_dir, service),
        blocking=blocking,
        timeout_seconds=timeout_seconds,
    ):
        yield


@contextlib.contextmanager
def transfer_path_lock(
    state_dir: Path,
    service: str,
    path: str,
    *,
    blocking: bool = False,
    timeout_seconds: float = 0.0,
) -> Iterator[None]:
    """Lock one normalized path without holding a queue lock during transfer."""

    with state_lock(
        path_lock_path(state_dir, service, path),
        blocking=blocking,
        timeout_seconds=timeout_seconds,
    ):
        yield


def attempt_state_file(state_dir: Path, service: str) -> Path:
    return state_dir / service / "transfer-attempts.json"


def _read_attempt_payload(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []


def create_attempt(
    state_dir: Path,
    service: str,
    records: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    concurrency: int,
) -> AttemptResult:
    path = attempt_state_file(state_dir, service)
    attempt_id = uuid.uuid4().hex
    payload = {
        "attempt_id": attempt_id,
        "service": service,
        "owner_pid": os.getpid(),
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
        "phase": "started",
        "status": "in_progress",
        "child_pids": [],
        "records": list(records),
        "concurrency": int(concurrency),
        "requires_child_exit_confirmation": True,
    }
    try:
        with state_lock(path):
            attempts = _read_attempt_payload(path)
            attempts.append(payload)
            atomic_write_json(path, attempts, sort_keys=True)
        return AttemptResult(attempt_id, path, "started", "in_progress")
    except (OSError, TransferStateError) as exc:
        return AttemptResult(
            attempt_id,
            path,
            "unknown",
            "blocked",
            StateIssue(key="PCLOUD_TOOLS_TRANSFER_ATTEMPT", message=str(exc)),
        )


def update_attempt(
    state_dir: Path,
    service: str,
    attempt_id: str,
    *,
    phase: str | None = None,
    status: str | None = None,
    **fields: Any,
) -> AttemptResult:
    path = attempt_state_file(state_dir, service)
    try:
        with state_lock(path):
            attempts = _read_attempt_payload(path)
            found = False
            current_phase = "unknown"
            current_status = "unknown"
            for item in attempts:
                if str(item.get("attempt_id")) != attempt_id:
                    continue
                found = True
                if phase is not None:
                    item["phase"] = phase
                if status is not None:
                    item["status"] = status
                item.update(fields)
                item["updated_at"] = _utc_now()
                current_phase = str(item.get("phase", "unknown"))
                current_status = str(item.get("status", "unknown"))
                break
            if not found:
                return AttemptResult(
                    attempt_id,
                    path,
                    "unknown",
                    "blocked",
                    StateIssue(key="PCLOUD_TOOLS_TRANSFER_ATTEMPT", message=f"attempt not found: {attempt_id}"),
                )
            atomic_write_json(path, attempts, sort_keys=True)
            return AttemptResult(attempt_id, path, current_phase, current_status)
    except (OSError, TransferStateError) as exc:
        return AttemptResult(
            attempt_id,
            path,
            "unknown",
            "blocked",
            StateIssue(key="PCLOUD_TOOLS_TRANSFER_ATTEMPT", message=str(exc)),
        )


def read_attempts(state_dir: Path, service: str) -> tuple[dict[str, Any], ...]:
    return tuple(_read_attempt_payload(attempt_state_file(state_dir, service)))


def unresolved_attempts(state_dir: Path, service: str) -> tuple[dict[str, Any], ...]:
    terminal = {"completed", "failed", "cancelled", "released"}
    return tuple(
        item
        for item in read_attempts(state_dir, service)
        if str(item.get("status", "in_progress")) not in terminal
        or str(item.get("phase", "unknown")) in {"unknown", "child-uncertain", "needs-recovery"}
    )


def mark_attempt_child(
    state_dir: Path,
    service: str,
    attempt_id: str,
    pid: int,
    *,
    phase: str = "running",
) -> AttemptResult:
    path = attempt_state_file(state_dir, service)
    try:
        with state_lock(path):
            attempts = _read_attempt_payload(path)
            for item in attempts:
                if str(item.get("attempt_id")) != attempt_id:
                    continue
                child_pids = [int(value) for value in item.get("child_pids", []) if isinstance(value, int)]
                if pid not in child_pids:
                    child_pids.append(pid)
                item.update({"phase": phase, "status": "in_progress", "child_pids": child_pids, "updated_at": _utc_now()})
                atomic_write_json(path, attempts, sort_keys=True)
                return AttemptResult(attempt_id, path, phase, "in_progress")
            return AttemptResult(
                attempt_id,
                path,
                "unknown",
                "blocked",
                StateIssue(key="PCLOUD_TOOLS_TRANSFER_ATTEMPT", message=f"attempt not found: {attempt_id}"),
            )
    except (OSError, TransferStateError) as exc:
        return AttemptResult(
            attempt_id,
            path,
            "unknown",
            "blocked",
            StateIssue(key="PCLOUD_TOOLS_TRANSFER_ATTEMPT", message=str(exc)),
        )


def clear_attempt_child(
    state_dir: Path,
    service: str,
    attempt_id: str,
    pid: int,
) -> AttemptResult:
    """Remove one child PID from an attempt after it has been reaped."""

    path = attempt_state_file(state_dir, service)
    try:
        with state_lock(path):
            attempts = _read_attempt_payload(path)
            for item in attempts:
                if str(item.get("attempt_id")) != attempt_id:
                    continue
                child_pids = [
                    int(value)
                    for value in item.get("child_pids", [])
                    if isinstance(value, int) and value != pid
                ]
                item.update(
                    {
                        "phase": "running" if child_pids else item.get("phase", "running"),
                        "status": "in_progress",
                        "child_pids": child_pids,
                        "updated_at": _utc_now(),
                    }
                )
                atomic_write_json(path, attempts, sort_keys=True)
                return AttemptResult(attempt_id, path, str(item.get("phase", "running")), "in_progress")
            return AttemptResult(
                attempt_id,
                path,
                "unknown",
                "blocked",
                StateIssue(key="PCLOUD_TOOLS_TRANSFER_ATTEMPT", message=f"attempt not found: {attempt_id}"),
            )
    except (OSError, TransferStateError) as exc:
        return AttemptResult(
            attempt_id,
            path,
            "unknown",
            "blocked",
            StateIssue(key="PCLOUD_TOOLS_TRANSFER_ATTEMPT", message=str(exc)),
        )


__all__ = [
    "AttemptResult",
    "ConsumeResult",
    "EVENT_ID_KEY",
    "EventIdUpdateResult",
    "new_event_id",
    "QueueRecord",
    "QueueSnapshot",
    "StateIssue",
    "TransferStateError",
    "attempt_state_file",
    "consume_event_ids",
    "clear_attempt_child",
    "create_attempt",
    "ensure_event_ids",
    "mark_attempt_child",
    "path_lock_path",
    "read_attempts",
    "read_queue_snapshot",
    "state_lock",
    "writer_cutover_lock",
    "writer_cutover_lock_path",
    "writer_cutover_session",
    "writer_lifetime_lock",
    "writer_lifetime_lock_path",
    "writer_lifetime_lock_status",
    "writer_generation_session",
    "writer_process_lease_dir",
    "writer_process_lease_path",
    "writer_process_session",
    "writer_state_lock",
    "transfer_tick_lock",
    "transfer_tick_lock_path",
    "transfer_tick_lock_status",
    "transfer_path_lock",
    "unresolved_attempts",
    "update_attempt",
]
