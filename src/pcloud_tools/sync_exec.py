from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import AppConfig, ConfigIssue
from .io_utils import atomic_write_text
from .sync_runtime import (
    BisyncListingRecovery,
    bisync_cache_entry_path,
    bisync_listing_recovery_state as inspect_bisync_listing_recovery_state,
    recover_bisync_listings_from_err,
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
    resync_mode: str | None
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
    listings_recovered: bool


class SyncExecutionError(ValueError):
    """Raised when a sync plan cannot be executed safely."""


RESYNC_MODES = ("path1", "path2", "newer", "older", "larger", "smaller")
DEFAULT_RESYNC_MODE = "path1"


@dataclass(frozen=True)
class BackgroundSyncLaunch:
    mode: str
    notify_on_finish: bool
    command: tuple[str, ...]
    stdout_log: Path
    stderr_log: Path
    pid: int


def sync_scope_mode_for_sync_mode(mode: str) -> str:
    return "full" if mode == "full-resync" else "allowlist"


def sync_mode_is_resync(mode: str) -> bool:
    return mode in {"resync", "full-resync"}


def record_sync_scope_mode(config: AppConfig, scope_mode: str) -> Path:
    scope_file = config.state_dir / "last-resync-scope"
    return atomic_write_text(scope_file, f"{scope_mode}\n")


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
    rules = prepare_sync_filter_rules(config, allowlist_entries)
    return atomic_write_text(filter_file, "".join(f"{rule}\n" for rule in rules))


def build_sync_plan(
    config: AppConfig,
    mode: str,
    allowlist_entries: tuple[str, ...],
    rclone_bin: str,
    resync_mode: str = DEFAULT_RESYNC_MODE,
) -> SyncPlan:
    if mode not in {"normal", "autosync", "resync", "full-resync", "track-renames"}:
        raise SyncExecutionError(f"invalid sync mode: {mode}")
    if sync_mode_is_resync(mode) and resync_mode not in RESYNC_MODES:
        choices = ", ".join(RESYNC_MODES)
        raise SyncExecutionError(f"invalid resync mode: {resync_mode} (expected one of: {choices})")
    if not sync_mode_is_resync(mode) and resync_mode != DEFAULT_RESYNC_MODE:
        raise SyncExecutionError(f"--resync-mode is only valid for resync modes: {mode}")

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

    plan_resync_mode: str | None = None
    if sync_mode_is_resync(mode):
        plan_resync_mode = resync_mode
        command.extend(["--resync-mode", resync_mode])
    elif mode == "track-renames":
        command.append("--track-renames")

    command.extend(["--log-file", str(rclone_log), "--log-level", "INFO"])
    return SyncPlan(
        mode=mode,
        scope_mode=scope_mode,
        resync_mode=plan_resync_mode,
        command=tuple(command),
        rclone_log=rclone_log,
        stdout_log=stdout_log,
        stderr_log=stderr_log,
        filter_file=filter_file,
    )


def _record_log_pointers(config: AppConfig, plan: SyncPlan) -> None:
    config.state_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(sync_last_rclone_log_file(config), f"{plan.rclone_log}\n")
    atomic_write_text(sync_last_stdout_log_file(config), f"{plan.stdout_log}\n")
    atomic_write_text(sync_last_stderr_log_file(config), f"{plan.stderr_log}\n")


def record_background_log_pointers(config: AppConfig, stdout_log: Path, stderr_log: Path) -> None:
    config.state_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(sync_last_rclone_log_file(config), "\n")
    atomic_write_text(sync_last_stdout_log_file(config), f"{stdout_log}\n")
    atomic_write_text(sync_last_stderr_log_file(config), f"{stderr_log}\n")


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
    atomic_write_text(sync_lock_pid_file(config), "pending\n")
    atomic_write_text(sync_lock_mode_file(config), f"{mode}\n")
    atomic_write_text(sync_lock_started_file(config), f"{_now()}\n")


def _record_sync_child_pid(config: AppConfig, pid: int) -> None:
    atomic_write_text(sync_lock_pid_file(config), f"{pid}\n")


def _release_sync_lock(config: AppConfig) -> None:
    lock_dir = sync_lock_dir(config)
    if lock_dir.exists():
        for child in lock_dir.iterdir():
            child.unlink()
        lock_dir.rmdir()


def launch_background_sync(
    config: AppConfig,
    mode: str,
    notify_on_finish: bool,
    child_command: tuple[str, ...],
) -> BackgroundSyncLaunch:
    ts = _timestamp()
    stdout_log = config.state_dir / f"sync-background-{mode}-{ts}.out"
    stderr_log = config.state_dir / f"sync-background-{mode}-{ts}.err"
    stdout_log.parent.mkdir(parents=True, exist_ok=True)
    stderr_log.parent.mkdir(parents=True, exist_ok=True)
    record_background_log_pointers(config, stdout_log, stderr_log)

    with stdout_log.open("w") as stdout_fh, stderr_log.open("w") as stderr_fh:
        process = subprocess.Popen(
            list(child_command),
            stdout=stdout_fh,
            stderr=stderr_fh,
            text=True,
            start_new_session=True,
        )

    return BackgroundSyncLaunch(
        mode=mode,
        notify_on_finish=notify_on_finish,
        command=child_command,
        stdout_log=stdout_log,
        stderr_log=stderr_log,
        pid=process.pid,
    )


def send_sync_notification(config: AppConfig, exit_code: int, mode: str) -> None:
    notify_bin = config.notify_bin
    if exit_code == 0:
        message = f"pCloud sync completed ({mode})"
    else:
        message = f"pCloud sync failed ({mode}) exit={exit_code}"

    if notify_bin.exists():
        result = subprocess.run(
            [str(notify_bin), "local", message],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return

    osascript = shutil.which("osascript")
    if osascript is None:
        return

    subprocess.run(
        [osascript, "-e", f'display notification "{message}" with title "pcloud-manager"'],
        check=False,
        capture_output=True,
        text=True,
    )


def execute_sync_plan(config: AppConfig, plan: SyncPlan) -> SyncExecutionResult:
    _acquire_sync_lock(config, plan.mode)
    _record_log_pointers(config, plan)
    plan.rclone_log.parent.mkdir(parents=True, exist_ok=True)
    plan.stdout_log.parent.mkdir(parents=True, exist_ok=True)
    plan.stderr_log.parent.mkdir(parents=True, exist_ok=True)

    exit_code = 0
    scope_recorded = False
    listings_recovered = False
    try:
        listing_recovery = recover_bisync_listings_from_err(config)
        listings_recovered = listing_recovery.recovered
        with plan.stdout_log.open("w") as stdout_fh, plan.stderr_log.open("w") as stderr_fh:
            process = subprocess.Popen(
                list(plan.command),
                stdout=stdout_fh,
                stderr=stderr_fh,
                text=True,
            )
            _record_sync_child_pid(config, process.pid)
            exit_code = process.wait()

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
        listings_recovered=listings_recovered,
    )
def bisync_listing_recovery_state(config: AppConfig) -> BisyncListingRecovery:
    return inspect_bisync_listing_recovery_state(config)
