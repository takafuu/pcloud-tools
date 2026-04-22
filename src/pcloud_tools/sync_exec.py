from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import AppConfig, ConfigIssue
from .sync_runtime import (
    sync_error_log_path,
    sync_last_rclone_log_file,
    sync_last_stderr_log_file,
    sync_last_stdout_log_file,
    sync_lock_dir,
    sync_lock_mode_file,
    sync_lock_pid_file,
    sync_lock_started_file,
    sync_status_log_path,
)
from .sync_scope import (
    prepare_sync_filter_rules,
    sync_filter_file,
    sync_scope_baseline_info,
)


@dataclass(frozen=True)
class SyncPlan:
    mode: str
    scope_mode: str
    command: tuple[str, ...]
    rclone_log: Path
    stdout_log: Path
    stderr_log: Path
    filter_file: Path | None


@dataclass(frozen=True)
class SyncExecutionResult:
    plan: SyncPlan
    exit_code: int
    issues: tuple[ConfigIssue, ...]
    scope_recorded: bool


class SyncExecutionError(ValueError):
    """Raised when a sync plan cannot be executed safely."""


def sync_scope_mode_for_sync_mode(mode: str) -> str:
    return "full" if mode == "full-resync" else "allowlist"


def sync_mode_is_resync(mode: str) -> bool:
    return mode in {"resync", "full-resync"}


def record_sync_scope_mode(config: AppConfig, scope_mode: str) -> Path:
    scope_file = config.state_dir / "last-resync-scope"
    scope_file.parent.mkdir(parents=True, exist_ok=True)
    scope_file.write_text(f"{scope_mode}\n")
    return scope_file


def enforce_sync_scope_guard(config: AppConfig, mode: str) -> tuple[ConfigIssue, ...]:
    if sync_mode_is_resync(mode):
        return ()

    requested_scope = sync_scope_mode_for_sync_mode(mode)
    baseline = sync_scope_baseline_info(config)
    if baseline.status == "invalid":
        return (
            ConfigIssue(
                key="PCLOUD_TOOLS_SCOPE_BASELINE",
                level="error",
                message="stored sync scope is invalid. Run sync resync to reset bisync state.",
            ),
        )
    if baseline.mode != requested_scope:
        hint = "sync resync" if requested_scope == "allowlist" else "sync full-resync"
        return (
            ConfigIssue(
                key="PCLOUD_TOOLS_SCOPE_GUARD",
                level="error",
                message=(
                    "resync required after scope change "
                    f"(last_resync_scope={baseline.mode} requested_scope={requested_scope}; hint: {hint})"
                ),
            ),
        )
    return ()


def _rclone_log_dir(config: AppConfig) -> Path:
    return config.log_dir


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _write_filter_file(config: AppConfig, allowlist_entries: tuple[str, ...]) -> Path:
    filter_file = sync_filter_file(config)
    filter_file.parent.mkdir(parents=True, exist_ok=True)
    rules = prepare_sync_filter_rules(config, allowlist_entries)
    filter_file.write_text("".join(f"{rule}\n" for rule in rules))
    return filter_file


def build_sync_plan(
    config: AppConfig,
    mode: str,
    allowlist_entries: tuple[str, ...],
    rclone_bin: str,
) -> SyncPlan:
    if mode not in {"normal", "resync", "full-resync", "track-renames"}:
        raise SyncExecutionError(f"invalid sync mode: {mode}")

    ts = _timestamp()
    rclone_log = _rclone_log_dir(config) / f"bisync-{mode}-{ts}.log"
    stdout_log = config.state_dir / f"sync-{mode}-{ts}.out"
    stderr_log = config.state_dir / f"sync-{mode}-{ts}.err"
    scope_mode = sync_scope_mode_for_sync_mode(mode)

    command = [
        rclone_bin,
        "bisync",
        str(config.core_dir),
        config.core_remote,
        "--conflict-resolve",
        "newer",
        "--resilient",
        "--skip-links",
    ]

    filter_file: Path | None = None
    if scope_mode == "allowlist":
        filter_file = _write_filter_file(config, allowlist_entries)
        command.extend(["--filter-from", str(filter_file)])

    if mode in {"resync", "full-resync"}:
        command.append("--resync")
    elif mode == "track-renames":
        command.append("--track-renames")

    command.extend(["--log-file", str(rclone_log), "--log-level", "INFO"])
    return SyncPlan(
        mode=mode,
        scope_mode=scope_mode,
        command=tuple(command),
        rclone_log=rclone_log,
        stdout_log=stdout_log,
        stderr_log=stderr_log,
        filter_file=filter_file,
    )


def _record_log_pointers(config: AppConfig, plan: SyncPlan) -> None:
    config.state_dir.mkdir(parents=True, exist_ok=True)
    sync_last_rclone_log_file(config).write_text(f"{plan.rclone_log}\n")
    sync_last_stdout_log_file(config).write_text(f"{plan.stdout_log}\n")
    sync_last_stderr_log_file(config).write_text(f"{plan.stderr_log}\n")


def _append_status_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(f"{line}\n")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _acquire_sync_lock(config: AppConfig, mode: str) -> None:
    lock_dir = sync_lock_dir(config)
    if lock_dir.exists():
        raise SyncExecutionError("sync already running")
    lock_dir.mkdir(parents=True, exist_ok=False)
    sync_lock_pid_file(config).write_text(f"{os.getpid()}\n")
    sync_lock_mode_file(config).write_text(f"{mode}\n")
    sync_lock_started_file(config).write_text(f"{_now()}\n")


def _release_sync_lock(config: AppConfig) -> None:
    lock_dir = sync_lock_dir(config)
    if lock_dir.exists():
        for child in lock_dir.iterdir():
            child.unlink()
        lock_dir.rmdir()


def execute_sync_plan(config: AppConfig, plan: SyncPlan) -> SyncExecutionResult:
    _acquire_sync_lock(config, plan.mode)
    _record_log_pointers(config, plan)
    plan.rclone_log.parent.mkdir(parents=True, exist_ok=True)
    plan.stdout_log.parent.mkdir(parents=True, exist_ok=True)
    plan.stderr_log.parent.mkdir(parents=True, exist_ok=True)

    exit_code = 0
    scope_recorded = False
    try:
        with plan.stdout_log.open("w") as stdout_fh, plan.stderr_log.open("w") as stderr_fh:
            completed = subprocess.run(
                list(plan.command),
                check=False,
                stdout=stdout_fh,
                stderr=stderr_fh,
                text=True,
            )
        exit_code = completed.returncode

        if exit_code == 0:
            _append_status_line(sync_status_log_path(config), f"{_now()} SUCCESS mode={plan.mode}")
            if sync_mode_is_resync(plan.mode):
                record_sync_scope_mode(config, plan.scope_mode)
                scope_recorded = True
        else:
            _append_status_line(sync_status_log_path(config), f"{_now()} ERROR mode={plan.mode}")
            _append_status_line(
                sync_error_log_path(config),
                f"{_now()}: bisync failed (mode={plan.mode} exit={exit_code}) log={plan.rclone_log}",
            )
    finally:
        _release_sync_lock(config)

    return SyncExecutionResult(
        plan=plan,
        exit_code=exit_code,
        issues=(),
        scope_recorded=scope_recorded,
    )
