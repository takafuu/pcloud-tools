"""Bounded transfer execution and performance accounting.

Only the waiting for independent child processes is parallel.  Callers keep
queue and journal mutations in short callbacks before or after a child and can
therefore avoid holding a queue lock across network I/O.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, ContextManager, Iterable, Optional, Tuple, Union

from .config import ConfigIssue


_CLEANUP_WAIT_SECONDS = 1.0
_PROCESS_GROUP_POLL_SECONDS = 0.01
_REAL_POPEN = subprocess.Popen


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace").strip()
    return value.strip()


def _process_group_state(process_group_id: int) -> bool | None:
    """Return whether a captured process group still has a member.

    ``Popen.poll()`` only describes the group leader.  A child may have
    exited while a descendant inherited the same session and remains alive,
    so group membership is checked separately whenever the group id is
    available.
    """

    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return None
    except OSError:
        return None
    # A terminated descendant can remain as a zombie briefly after its
    # parent exits.  ``killpg(..., 0)`` still sees that zombie, so inspect the
    # process table when available and count only live members.  The captured
    # Popen reference is used deliberately: tests and callers may wrap the
    # public module attribute to inject cancellation races.
    try:
        probe = _REAL_POPEN(
            ["ps", "-axo", "pid=,pgid=,stat="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        output, _ = probe.communicate(timeout=0.25)
    except (OSError, subprocess.TimeoutExpired):
        return True
    found = False
    for line in output.splitlines():
        fields = line.split(None, 2)
        if len(fields) != 3:
            continue
        try:
            group = int(fields[1])
        except ValueError:
            continue
        if group != process_group_id:
            continue
        found = True
        if "Z" not in fields[2].split()[0]:
            return True
    return found


def _wait_for_process_group_exit(
    process_group_id: int | None,
    *,
    timeout_seconds: float,
) -> bool:
    if process_group_id is None:
        return False
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while True:
        state = _process_group_state(process_group_id)
        if state is False:
            return True
        if state is None or time.monotonic() >= deadline:
            return False
        time.sleep(_PROCESS_GROUP_POLL_SECONDS)


def _cleanup_process_group(
    process: subprocess.Popen[str],
    process_group_id: int | None = None,
) -> dict[str, object]:
    details: dict[str, object] = {
        "process group cleanup": "attempted",
        "terminate attempted": False,
        "kill attempted": False,
        "terminated": False,
    }
    pgid = process_group_id
    if pgid is None:
        try:
            pgid = os.getpgid(process.pid)
        except ProcessLookupError:
            # The parent may already have exited.  Without a captured group
            # id we cannot prove that descendants have exited as well.
            details["process group cleanup"] = "pgid-unavailable"
            details["cleanup error"] = "process group id unavailable"
            return details
        except OSError as exc:
            details["process group cleanup"] = "pgid-unavailable"
            details["cleanup error"] = str(exc)
            return details
    details["process_group_id"] = pgid

    if _wait_for_process_group_exit(pgid, timeout_seconds=0.0):
        details.update({"process group cleanup": "already-exited", "terminated": True})
        return details

    def _send(method: Callable[[], None], label: str) -> bool:
        try:
            method()
            details[f"{label} attempted"] = True
            return True
        except ProcessLookupError:
            details.update({"process group cleanup": "already-exited", "terminated": True})
            return False
        except OSError as exc:
            details["cleanup error"] = str(exc)
            details["process group cleanup"] = f"{label}-failed"
            return False

    if not _send(lambda: os.killpg(pgid, signal.SIGTERM), "terminate"):
        if details.get("terminated"):
            return details
    try:
        process.wait(timeout=_CLEANUP_WAIT_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    if _wait_for_process_group_exit(pgid, timeout_seconds=_CLEANUP_WAIT_SECONDS):
        details["terminated"] = True
        details["process group cleanup"] = "terminated"
        return details
    details["process group cleanup"] = "terminate-timeout"

    if not _send(lambda: os.killpg(pgid, signal.SIGKILL), "kill"):
        if details.get("terminated"):
            return details
    try:
        process.wait(timeout=_CLEANUP_WAIT_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    if _wait_for_process_group_exit(pgid, timeout_seconds=_CLEANUP_WAIT_SECONDS):
        details["terminated"] = True
        details["process group cleanup"] = "killed"
    else:
        details["process group cleanup"] = "kill-timeout"
    return details


@dataclass(frozen=True)
class TransferBatchResult:
    results: list[dict[str, object]]
    issues: list[ConfigIssue]
    performance: dict[str, object]


BeforeItem = Callable[[dict[str, object]], Optional[dict[str, object]]]
AfterItem = Callable[
    [dict[str, object]],
    Optional[Union[Tuple[dict[str, object], Iterable[ConfigIssue]], dict[str, object]]],
]
LockFactory = Callable[[dict[str, object]], ContextManager[None]]
ProcessStarted = Callable[[dict[str, object], int], None]
ProcessFinished = Callable[[dict[str, object], int], None]


def transfer_concurrency(config: object, service: str) -> int:
    """Resolve a service's bounded worker count defensively."""

    field = "pushd_transfer_concurrency" if service == "pushd" else "diffd_transfer_concurrency"
    try:
        value = int(getattr(config, field))
    except (TypeError, ValueError, AttributeError):
        return 1
    return max(1, min(4, value))


def _deferred(item: dict[str, object], reason: str) -> dict[str, object]:
    now = _utc_now()
    return {
        **item,
        "attempt_id": str(item.get("attempt_id") or uuid.uuid4().hex),
        "returncode": None,
        "timed_out": False,
        "deferred": True,
        "deferred_reason": reason,
        "phase": "deferred",
        "started_at": None,
        "finished_at": now,
        "execution_seconds": 0.0,
        "stdout": "",
        "stderr": "",
    }


def _run_one(
    item: dict[str, object],
    *,
    timeout_seconds: int,
    before_item: BeforeItem | None,
    after_item: AfterItem | None,
    lock_factory: LockFactory | None,
    cancel_event: threading.Event,
    active_counter: list[int],
    active_guard: threading.Lock,
    peak_counter: list[int],
    process_registry: dict[int, subprocess.Popen[str]],
    process_guard: threading.Lock,
    on_process_started: ProcessStarted | None,
    on_process_finished: ProcessFinished | None,
) -> tuple[dict[str, object], list[ConfigIssue]]:
    if cancel_event.is_set():
        return _deferred(item, "executor stopping"), []

    lock_context: ContextManager[None] = contextlib.nullcontext()
    if lock_factory is not None:
        try:
            lock_context = lock_factory(item)
            lock_context.__enter__()
        except Exception:
            return _deferred(item, "path lock busy"), []

    prepared = dict(item)
    process: subprocess.Popen[str] | None = None
    try:
        if before_item is not None:
            changed = before_item(prepared)
            if changed is not None:
                prepared = changed
        raw_command = prepared.get("actual_command", prepared.get("command", []))
        command = [str(part) for part in raw_command] if isinstance(raw_command, (list, tuple)) else []
        if not command:
            result = _deferred(prepared, "empty transfer command")
            return result, [
                ConfigIssue(
                    key="PCLOUD_TOOLS_TRANSFER_EXEC",
                    level="error",
                    message=f"transfer command is empty for {prepared.get('path', '-')}",
                )
            ]

        started_mono = time.monotonic()
        started_at = _utc_now()
        cancelled_after_start = False
        cancellation_cleanup: dict[str, object] | None = None
        process_group_id: int | None = None
        process_group_confirmed = False
        with active_guard:
            active_counter[0] += 1
            peak_counter[0] = max(peak_counter[0], active_counter[0])
        issues: list[ConfigIssue] = []
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            # ``start_new_session=True`` makes the child its own process-group
            # leader. Capture the id before callbacks can observe cancellation;
            # the leader may exit before registration while descendants remain.
            process_group_id = process.pid
            with contextlib.suppress(OSError):
                process_group_id = os.getpgid(process.pid)
            with process_guard:
                process_registry[process.pid] = process
            if on_process_started is not None:
                with contextlib.suppress(Exception):
                    on_process_started(prepared, process.pid)
            # SIGTERM can arrive after Popen has created the child but before
            # the parent returns from that call. The signal handler cannot see
            # the child until it is registered, so close that small window by
            # rechecking cancellation immediately after registration and
            # cleaning the new process group before waiting on it.
            cancelled_after_start = cancel_event.is_set()
            if cancelled_after_start:
                cancellation_cleanup = _cleanup_process_group(process, process_group_id)
                process_group_confirmed = bool(cancellation_cleanup.get("terminated"))
                try:
                    stdout, stderr = process.communicate(timeout=_CLEANUP_WAIT_SECONDS)
                except subprocess.TimeoutExpired as cancel_exc:
                    stdout = cancel_exc.stdout or ""
                    stderr = cancel_exc.stderr or ""
                    cancellation_cleanup["communicate timeout"] = True
            else:
                stdout, stderr = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            assert process is not None
            cleanup = _cleanup_process_group(process, process_group_id)
            process_group_confirmed = bool(cleanup.get("terminated"))
            try:
                stdout, stderr = process.communicate(timeout=_CLEANUP_WAIT_SECONDS)
            except subprocess.TimeoutExpired as second_exc:
                stdout = second_exc.stdout if second_exc.stdout is not None else exc.stdout
                stderr = second_exc.stderr if second_exc.stderr is not None else exc.stderr
                cleanup["communicate timeout"] = True
            finished_at = _utc_now()
            result = {
                **prepared,
                "executed_command": command,
                "attempt_id": str(prepared.get("attempt_id") or uuid.uuid4().hex),
                "returncode": None,
                "timed_out": True,
                "timeout seconds": timeout_seconds,
                "cleanup": cleanup,
                "process_group_id": process_group_id,
                "requires_child_exit_confirmation": not process_group_confirmed,
                "stdout": _text(stdout),
                "stderr": _text(stderr),
                "phase": "timeout",
                "started_at": started_at,
                "finished_at": finished_at,
                "execution_seconds": max(0.0, time.monotonic() - started_mono),
            }
            issues.append(
                ConfigIssue(
                    key="PCLOUD_TOOLS_TRANSFER_EXEC_TIMEOUT",
                    level="error",
                    message=f"transfer command timed out for {prepared.get('path', '-') } after {timeout_seconds}s",
                )
            )
        except OSError as exc:
            result = {
                **prepared,
                "executed_command": command,
                "attempt_id": str(prepared.get("attempt_id") or uuid.uuid4().hex),
                "returncode": None,
                "timed_out": False,
                "stdout": "",
                "stderr": str(exc),
                "phase": "start-failed",
                "started_at": started_at,
                "finished_at": _utc_now(),
                "execution_seconds": max(0.0, time.monotonic() - started_mono),
            }
            issues.append(
                ConfigIssue(
                    key="PCLOUD_TOOLS_TRANSFER_EXEC",
                    level="error",
                    message=f"transfer command could not start for {prepared.get('path', '-')}: {exc}",
                )
            )
        else:
            cancelled = cancel_event.is_set()
            if not cancelled_after_start and process_group_id is not None:
                process_group_confirmed = _wait_for_process_group_exit(
                    process_group_id,
                    timeout_seconds=0.0,
                )
                if not process_group_confirmed:
                    cancellation_cleanup = _cleanup_process_group(process, process_group_id)
                    process_group_confirmed = bool(cancellation_cleanup.get("terminated"))
            child_uncertain = not process_group_confirmed
            result = {
                **prepared,
                "executed_command": command,
                "attempt_id": str(prepared.get("attempt_id") or uuid.uuid4().hex),
                "returncode": process.returncode if process is not None else None,
                "timed_out": False,
                "stdout": _text(stdout),
                "stderr": _text(stderr),
                "phase": (
                    "child-uncertain"
                    if child_uncertain
                    else "cancelled"
                    if cancelled_after_start or cancelled
                    else "completed"
                    if process is not None and process.returncode == 0
                    else "failed"
                ),
                "deferred": child_uncertain or cancelled_after_start or cancelled,
                "started_at": started_at,
                "finished_at": _utc_now(),
                "execution_seconds": max(0.0, time.monotonic() - started_mono),
                "process_group_id": process_group_id,
                "requires_child_exit_confirmation": child_uncertain,
            }
            if cancellation_cleanup is not None:
                result["cleanup"] = cancellation_cleanup
            if child_uncertain:
                result["deferred_reason"] = "process group exit could not be confirmed"
                issues.append(
                    ConfigIssue(
                        key="PCLOUD_TOOLS_TRANSFER_EXEC_CHILD_UNCERTAIN",
                        level="error",
                        message=(
                            f"process group exit could not be confirmed for "
                            f"{prepared.get('path', '-') }"
                        ),
                    )
                )
            if process is not None and process.returncode != 0 and not cancelled:
                issues.append(
                    ConfigIssue(
                        key="PCLOUD_TOOLS_TRANSFER_EXEC",
                        level="error",
                        message=f"transfer command failed for {prepared.get('path', '-')} with exit {process.returncode}",
                    )
                )
        if after_item is not None:
            after = after_item(result)
            if isinstance(after, tuple) and len(after) == 2:
                result, callback_issues = after
                issues.extend(callback_issues)
            elif isinstance(after, dict):
                result = after
            if result.get("missing_remote_source"):
                # The exact queue generation has been retained for review.
                # Keep the nonzero result/failed metric, but don't repeatedly
                # fail an otherwise runnable batch on this missing source.
                issues = [issue for issue in issues if issue.key != "PCLOUD_TOOLS_TRANSFER_EXEC"]
        return result, issues
    finally:
        if process is not None:
            with process_guard:
                process_registry.pop(process.pid, None)
            if on_process_finished is not None and process.poll() is not None and process_group_confirmed:
                with contextlib.suppress(Exception):
                    on_process_finished(prepared, process.pid)
        with active_guard:
            active_counter[0] = max(0, active_counter[0] - 1)
        with contextlib.suppress(Exception):
            lock_context.__exit__(None, None, None)


def run_transfer_batch(
    commands: list[dict[str, object]],
    *,
    timeout_seconds: int,
    concurrency: int = 1,
    before_item: BeforeItem | None = None,
    after_item: AfterItem | None = None,
    lock_factory: LockFactory | None = None,
    cancel_event: threading.Event | None = None,
    on_process_started: ProcessStarted | None = None,
    on_process_finished: ProcessFinished | None = None,
) -> TransferBatchResult:
    """Run one selected batch with a bounded number of child processes.

    Futures are collected in the same order as ``commands`` so state and
    report arrays retain selection order even when transfers complete out of
    order.  ``peak_concurrency`` is measured from actual running children.
    """

    selected = list(commands)
    workers = max(1, min(4, int(concurrency)))
    stop = cancel_event or threading.Event()
    batch_started_at = _utc_now()
    batch_mono = time.monotonic()
    active = [0]
    peak = [0]
    active_guard = threading.Lock()
    process_registry: dict[int, subprocess.Popen[str]] = {}
    process_guard = threading.Lock()
    futures: list[Future[tuple[dict[str, object], list[ConfigIssue]]]] = []
    results: list[dict[str, object]] = []
    issues: list[ConfigIssue] = []
    def _signal_handler(signum: int, _frame: object) -> None:
        del signum
        stop.set()
        with process_guard:
            active_processes = tuple(process_registry.values())
        for process in active_processes:
            _cleanup_process_group(process)

    previous_handlers: dict[int, Any] = {}
    can_handle_signals = threading.current_thread() is threading.main_thread()
    if can_handle_signals:
        for signum in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(ValueError, OSError):
                previous_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, _signal_handler)
    try:
        with ThreadPoolExecutor(max_workers=min(workers, len(selected) or 1), thread_name_prefix="pcloud-transfer") as pool:
            for item in selected:
                futures.append(
                    pool.submit(
                        _run_one,
                        dict(item),
                        timeout_seconds=max(1, int(timeout_seconds)),
                        before_item=before_item,
                        after_item=after_item,
                        lock_factory=lock_factory,
                        cancel_event=stop,
                        active_counter=active,
                        active_guard=active_guard,
                        peak_counter=peak,
                        process_registry=process_registry,
                        process_guard=process_guard,
                        on_process_started=on_process_started,
                        on_process_finished=on_process_finished,
                    )
                )
            for future in futures:
                try:
                    result, result_issues = future.result()
                except Exception as exc:  # callback bugs must retain an explicit failure.
                    result = {
                        "attempt_id": uuid.uuid4().hex,
                        "returncode": None,
                        "timed_out": False,
                        "phase": "worker-failed",
                        "deferred": True,
                        "deferred_reason": "worker exception",
                        "stdout": "",
                        "stderr": str(exc),
                    }
                    result_issues = [
                        ConfigIssue(
                            key="PCLOUD_TOOLS_TRANSFER_WORKER",
                            level="error",
                            message=f"transfer worker failed: {exc}",
                        )
                    ]
                results.append(result)
                issues.extend(result_issues)
    finally:
        for signum, handler in previous_handlers.items():
            with contextlib.suppress(ValueError, OSError):
                signal.signal(signum, handler)
    elapsed = max(0.0, time.monotonic() - batch_mono)
    success = sum(
        1
        for item in results
        if item.get("returncode") == 0
        and not item.get("deferred")
        and not item.get("manual_review")
        and not item.get("conflict")
    )
    timeout = sum(1 for item in results if item.get("timed_out"))
    deferred = sum(1 for item in results if item.get("deferred"))
    conflicts = sum(
        1 for item in results
        if item.get("conflict") or (item.get("manual_review") and not item.get("missing_remote_source"))
    )
    failures = sum(
        1
        for item in results
        if item.get("phase") not in {"cancelled", "deferred"}
        and (item.get("returncode") not in {0, None} or item.get("phase") in {"start-failed", "worker-failed"})
    )
    performance = {
        "schema_version": "pcloud-tools-transfer-performance.v1",
        "batch_started_at": batch_started_at,
        "batch_elapsed_seconds": elapsed,
        "concurrency": workers,
        "peak_concurrency": peak[0],
        "selected": len(selected),
        "started": sum(1 for item in results if item.get("started_at")),
        "succeeded": success,
        "failed": failures,
        "timeout": timeout,
        "deferred": deferred,
        "conflict": conflicts,
        "manual_review": sum(1 for item in results if item.get("manual_review")),
    }
    return TransferBatchResult(results=results, issues=issues, performance=performance)


def execute_transfer_commands(*args: Any, **kwargs: Any) -> TransferBatchResult:
    """Public compatibility alias for the bounded batch runner."""

    return run_transfer_batch(*args, **kwargs)


__all__ = [
    "TransferBatchResult",
    "execute_transfer_commands",
    "run_transfer_batch",
    "transfer_concurrency",
]
