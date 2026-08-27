from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from ..autosync_runtime import read_autosync_state
from ..cli_common import (
    action_command,
    entrypoint_command,
    exit_code_for_report,
    has_errors,
    has_warnings,
    issue_sort_key,
    report_issues,
    shell_command,
    sort_issues,
    status_from_issues,
)
from ..config import AppConfig, ConfigIssue, load_config
from ..io_utils import atomic_write_json
from ..output import CommandReport, ReportAction, render_report
from ..runtime import RuntimePaths
from ..sync_exec import (
    DEFAULT_RESYNC_MODE,
    RESYNC_MODES,
    SyncExecutionError,
    bisync_listing_recovery_state,
    build_sync_plan,
    enforce_sync_scope_guard,
    execute_sync_plan,
    launch_background_sync,
    send_sync_notification,
)
from ..sync_runtime import (
    clear_sync_lock,
    parse_sync_progress,
    read_latest_sync_logs,
    read_sync_lock_state,
    read_sync_state,
    sync_last_error_status,
)
from ..sync_scope import (
    prepare_sync_filter_rules,
    scope_issues,
    sync_allowlist_info,
    sync_filter_file,
    write_sync_filter_file,
)

def _command_path(name: str) -> str | None:
    try:
        result = subprocess.run(
            ["/bin/sh", "-c", f"command -v {shlex.quote(name)}"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        for directory in ("/usr/local/bin", "/opt/homebrew/bin"):
            candidate = Path(directory) / name
            if candidate.is_file() and candidate.stat().st_mode & 0o111:
                return str(candidate)
        return None
    path = result.stdout.strip().splitlines()
    return path[0] if path else None


_DAEMON_MODE_LABELS = (
    "com.takafumi.pcloud-pushd",
    "com.takafumi.pcloud-pushd-executor",
    "com.takafumi.pcloud-diffd",
    "com.takafumi.pcloud-diffd-executor",
)


def _daemon_mode_loaded() -> bool:
    launchctl = _command_path("launchctl")
    if not launchctl:
        return False
    for label in _DAEMON_MODE_LABELS:
        result = subprocess.run(
            [launchctl, "print", f"gui/{os.getuid()}/{label}"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return True
    return False


def _sync_state_issues(sync_state) -> list[ConfigIssue]:
    if sync_state.state != "sync_error":
        return []
    message = sync_state.reason if sync_state.reason else sync_state.last_sync
    return [
        ConfigIssue(
            key="PCLOUD_TOOLS_SYNC_STATE",
            level="warning",
            message=f"last sync failed: {message}",
        )
    ]


def _sync_status_actions(paths: RuntimePaths, lock_status: str) -> list[ReportAction]:
    actions = [
        ReportAction(
            id="sync.status.refresh",
            label="Refresh sync status",
            command=action_command(paths, "sync.status.refresh"),
        ),
        ReportAction(
            id="sync.preview",
            label="Preview normal sync",
            command=action_command(paths, "sync.preview"),
            terminal=True,
            refresh=False,
        ),
        ReportAction(
            id="sync.progress",
            label="Show sync progress",
            command=action_command(paths, "sync.progress"),
            terminal=True,
            refresh=False,
        ),
        ReportAction(
            id="sync.scope",
            label="Show sync scope",
            command=action_command(paths, "sync.scope"),
            terminal=True,
            refresh=False,
        ),
        ReportAction(
            id="sync.background.preview",
            label="Preview background sync",
            command=action_command(paths, "sync.background.preview"),
            terminal=True,
            refresh=False,
        ),
        ReportAction(
            id="sync.autosync.gate",
            label="Check autosync launchd gate",
            command=action_command(paths, "sync.autosync.gate"),
            terminal=True,
            refresh=False,
        ),
        ReportAction(
            id="sync.autosync-run.preview",
            label="Preview autosync launchd run",
            command=action_command(paths, "sync.autosync-run.preview"),
            terminal=True,
            refresh=False,
        ),
        ReportAction(
            id="sync.migration.gate",
            label="Check sync migration gate",
            command=action_command(paths, "sync.migration.gate"),
            terminal=True,
            refresh=False,
        ),
        ReportAction(
            id="sync.migration-run.preview",
            label="Preview sync migration run",
            command=action_command(paths, "sync.migration-run.preview"),
            terminal=True,
            refresh=False,
        ),
    ]
    if lock_status in {"stale", "invalid"}:
        actions.append(
            ReportAction(
                id="sync.clear-stale-lock.preview",
                label="Preview clear stale lock",
                command=action_command(paths, "sync.clear-stale-lock.preview"),
                terminal=True,
                refresh=False,
            )
        )
    return actions


def _readable_sync_scope_mode(mode: str) -> str:
    if mode == "allowlist":
        return "scope-file"
    return mode


def _readable_baseline(info_file: Path, mode: str, status: str) -> str:
    readable_mode = _readable_sync_scope_mode(mode)
    if status == "defaulted":
        return f"{readable_mode} (default)"
    if status == "invalid":
        return f"invalid ({info_file})"
    return readable_mode


def _sync_status_report(paths: RuntimePaths) -> CommandReport:
    load_result = load_config(paths)
    sync_state = read_sync_state(load_result.config)
    lock_state = read_sync_lock_state(load_result.config)
    log_pointers = read_latest_sync_logs(load_result.config)
    scope_info = sync_allowlist_info(load_result.config)
    issues = sort_issues(
        list(load_result.issues) + _sync_state_issues(sync_state) + scope_issues(scope_info)
    )
    autosync = read_autosync_state(load_result.config)
    details = {
        "runtime": "development" if paths.dev_mode else "default",
        "sync engine": "bisync fallback scaffold",
        "running": "yes" if sync_state.state == "syncing" else "no",
        "config source": load_result.source,
        "state dir": str(load_result.config.state_dir),
        "sync scope file": str(load_result.config.allowlist_file),
        "scope status": scope_info.allowlist_status,
        "scope entries": scope_info.allowlist_count,
        "last resync scope": _readable_baseline(
            scope_info.baseline.file,
            scope_info.baseline.mode,
            scope_info.baseline.status,
        ),
        "filter file": str(sync_filter_file(load_result.config)),
        "core remote": load_result.config.core_remote,
        "sync state": sync_state.state,
        "sync activity": sync_state.activity,
        "current log": sync_state.current_log,
        "sync lock active": "yes" if lock_state.active else "no",
        "sync lock status": lock_state.status,
        "sync lock pid": lock_state.pid,
        "sync lock mode": lock_state.mode,
        "sync lock started": lock_state.started_at,
        "last result": sync_state.last_sync,
        "last error": sync_state.last_error,
        "last error status": sync_last_error_status(sync_state),
        "last log": log_pointers.latest_rclone_log,
        "stdout log": log_pointers.latest_stdout_log,
        "stderr log": log_pointers.latest_stderr_log,
        "autosync state": autosync.state,
        "autosync runs": autosync.runs,
        "autosync label": autosync.label,
        "autosync plist": autosync.plist,
    }
    return CommandReport(
        command="sync status",
        status=status_from_issues(issues),
        summary="sync status is available for migration diagnostics",
        details=details,
        issues=report_issues(issues),
        actions=_sync_status_actions(paths, lock_state.status),
    )


def cmd_sync_status(args: argparse.Namespace, paths: RuntimePaths) -> int:
    report = _sync_status_report(paths)
    print(render_report(report, output_format="xbar" if args.xbar else "json" if args.json else "human"))
    return exit_code_for_report(report)


def _sync_progress_report(paths: RuntimePaths) -> CommandReport:
    load_result = load_config(paths)
    issues = sort_issues(list(load_result.issues))
    progress = parse_sync_progress(load_result.config)
    sync_state = read_sync_state(load_result.config)

    if progress is None:
        details: dict[str, object] = {
            "sync state": sync_state.state,
            "progress source": "-",
            "reason": "no sync log available for progress",
        }
        warning_issues = issues + [
            ConfigIssue(
                key="PCLOUD_TOOLS_SYNC_PROGRESS",
                level="warning" if not issues else "error" if has_errors(issues) else "warning",
                message="no sync log available for progress",
            )
        ]
        return CommandReport(
            command="sync progress",
            status=status_from_issues(sort_issues(warning_issues)),
            summary="sync progress is not available yet",
            details=details,
            issues=report_issues(sort_issues(warning_issues)),
        )

    details = {
        "sync state": sync_state.state,
        "progress source": str(progress.log_path),
        "activity": progress.activity,
        "scanned entries": progress.scanned_entries,
        "compared entries": progress.compared_entries,
        "files transferred": progress.files_transferred,
        "bytes transferred": progress.bytes_transferred,
        "rate": progress.rate,
        "eta": progress.eta,
        "elapsed": progress.elapsed,
        "transferring block": list(progress.trailing_lines),
    }
    return CommandReport(
        command="sync progress",
        status=status_from_issues(issues),
        summary="sync progress is available",
        details=details,
        issues=report_issues(issues),
    )


def cmd_sync_progress(args: argparse.Namespace, paths: RuntimePaths) -> int:
    report = _sync_progress_report(paths)
    print(render_report(report, as_json=args.json))
    return exit_code_for_report(report)


def _sync_execution_report(args: argparse.Namespace, paths: RuntimePaths, mode: str) -> CommandReport:
    command_name = "sync" if mode == "normal" else f"sync {mode}"
    resync_mode = getattr(args, "resync_mode", DEFAULT_RESYNC_MODE)
    load_result = load_config(paths)
    lock_state = read_sync_lock_state(load_result.config)
    scope_info = sync_allowlist_info(load_result.config)
    issues = sort_issues(
        list(load_result.issues)
        + scope_issues(scope_info)
        + list(enforce_sync_scope_guard(load_result.config, mode))
    )
    base_details = {
        "mode": mode,
        "execute": "yes" if args.execute else "no",
        "sync lock status": lock_state.status,
        "sync lock active": "yes" if lock_state.active else "no",
        "sync lock pid": lock_state.pid,
    }

    if issues and has_errors(issues):
        return CommandReport(
            command=command_name,
            status="error",
            summary="sync command cannot run until configuration issues are resolved",
            details=base_details,
            issues=report_issues(issues),
        )

    if args.execute and paths.dev_mode:
        return CommandReport(
            command=command_name,
            status="error",
            summary="dev mode refuses to execute bisync against a configured remote",
            details={
                **base_details,
                "core remote": load_result.config.core_remote,
                "reason": "use preview in dev mode; execution requires the public entrypoint or an explicit non-dev runtime",
            },
            issues=report_issues(
                [
                    ConfigIssue(
                        key="PCLOUD_TOOLS_DEV_EXECUTION",
                        level="error",
                        message="refusing --execute from pcloud-manager-dev",
                    )
                ]
            ),
        )

    if args.execute and _daemon_mode_loaded():
        return CommandReport(
            command=command_name,
            status="error",
            summary="sync execution is refused while daemon mode is loaded",
            details={
                **base_details,
                "reason": "bisync and daemon automation are mutually exclusive; switch to maintenance or pause mode first",
            },
            issues=report_issues(
                [
                    ConfigIssue(
                        key="PCLOUD_TOOLS_MODE_EXCLUSIVE",
                        level="error",
                        message="refusing bisync execution while pushd/diffd daemon services are loaded",
                    )
                ]
            ),
        )

    if lock_state.status == "active":
        issues = sort_issues(
            issues
            + [
                ConfigIssue(
                    key="PCLOUD_TOOLS_SYNC_LOCK",
                    level="error",
                    message=f"sync already running (pid={lock_state.pid})",
                )
            ]
        )
        return CommandReport(
            command=command_name,
            status="error",
            summary="sync command cannot start while another sync is active",
            details=base_details,
            issues=report_issues(issues),
        )

    if lock_state.status in {"stale", "invalid"}:
        issues = sort_issues(
            issues
            + [
                ConfigIssue(
                    key="PCLOUD_TOOLS_SYNC_LOCK",
                    level="error",
                    message=f"sync lock is {lock_state.status}; run `sync clear-stale-lock` before starting sync",
                )
            ]
        )
        return CommandReport(
            command=command_name,
            status="error",
            summary="sync command requires clearing the stale lock first",
            details=base_details,
            issues=report_issues(issues),
        )

    try:
        rclone_bin = _command_path(load_result.config.rclone_bin)
        if not rclone_bin:
            raise SyncExecutionError("rclone command not found")
        listing_recovery = bisync_listing_recovery_state(load_result.config)
        plan = build_sync_plan(
            load_result.config,
            mode,
            scope_info.entries,
            rclone_bin=rclone_bin,
            resync_mode=resync_mode,
        )
    except SyncExecutionError as exc:
        return CommandReport(
            command=command_name,
            status="error",
            summary="sync plan could not be built",
            details={"mode": mode},
            issues=report_issues(
                [ConfigIssue(key="PCLOUD_TOOLS_SYNC_EXEC", level="error", message=str(exc))]
            ),
        )

    details: dict[str, object] = {
        **base_details,
        "scope mode": _readable_sync_scope_mode(plan.scope_mode),
        "command": list(plan.command),
        "rclone log": str(plan.rclone_log),
        "stdout log": str(plan.stdout_log),
        "stderr log": str(plan.stderr_log),
        "listing recovery available": "yes" if listing_recovery.can_recover else "no",
        "path1 list": str(listing_recovery.path1_lst),
        "path2 list": str(listing_recovery.path2_lst),
        "path1 err": str(listing_recovery.path1_err),
        "path2 err": str(listing_recovery.path2_err),
    }
    if plan.filter_file is not None:
        details["filter file"] = str(plan.filter_file)
    if plan.resync_mode is not None:
        details["resync mode"] = plan.resync_mode

    if not args.execute:
        return CommandReport(
            command=command_name,
            status=status_from_issues(issues),
            summary="sync command preview is ready",
            details=details,
            issues=report_issues(issues),
        )

    try:
        result = execute_sync_plan(load_result.config, plan)
    except SyncExecutionError as exc:
        issues = sort_issues(
            issues + [ConfigIssue(key="PCLOUD_TOOLS_SYNC_EXEC", level="error", message=str(exc))]
        )
        return CommandReport(
            command=command_name,
            status="error",
            summary="sync command failed before launch",
            details=details,
            issues=report_issues(issues),
        )
    details["exit code"] = result.exit_code
    details["scope recorded"] = "yes" if result.scope_recorded else "no"
    details["listings recovered"] = "yes" if result.listings_recovered else "no"
    status = "ok" if result.exit_code == 0 else "error"
    return CommandReport(
        command=command_name,
        status=status,
        summary="sync command executed" if result.exit_code == 0 else "sync command failed",
        details=details,
        issues=report_issues(issues),
    )


def cmd_sync_execution(args: argparse.Namespace, paths: RuntimePaths, mode: str) -> int:
    report = _sync_execution_report(args, paths, mode)
    print(render_report(report, as_json=args.json))
    return exit_code_for_report(report)


def _sync_scope_report(args: argparse.Namespace, paths: RuntimePaths) -> CommandReport:
    load_result = load_config(paths)
    config = load_result.config
    info = sync_allowlist_info(config)
    issues = sort_issues(list(load_result.issues) + scope_issues(info))
    details: dict[str, object] = {
        "scope mode": _readable_sync_scope_mode("allowlist"),
        "scope source": str(info.allowlist_file),
        "scope status": info.allowlist_status,
        "scope entries": info.allowlist_count if info.allowlist_status == "loaded" else 0,
        "full-tree override": "pcloud-manager sync full-resync",
        "last resync scope": _readable_baseline(info.baseline.file, info.baseline.mode, info.baseline.status),
        "filter file": str(info.filter_file),
        "entries": list(info.entries),
    }
    if info.allowlist_message != "-":
        details["scope note"] = info.allowlist_message
    if args.filter and info.allowlist_status == "loaded":
        written_filter = write_sync_filter_file(config, info.entries)
        details["filter file"] = str(written_filter)
        details["filter rules"] = list(prepare_sync_filter_rules(config, info.entries))

    return CommandReport(
        command="sync scope",
        status=status_from_issues(issues),
        summary="sync scope is ready" if not issues else "sync scope needs attention",
        details=details,
        issues=report_issues(issues),
    )


def cmd_sync_scope(args: argparse.Namespace, paths: RuntimePaths) -> int:
    report = _sync_scope_report(args, paths)
    print(render_report(report, as_json=args.json))
    return exit_code_for_report(report)


def _sync_check_allowlist_report(args: argparse.Namespace, paths: RuntimePaths) -> CommandReport:
    """Build the sync scope check report; check-allowlist is only a legacy alias."""
    load_result = load_config(paths)
    info = sync_allowlist_info(load_result.config)
    issues = sort_issues(list(load_result.issues) + scope_issues(info))
    details: dict[str, object] = {
        "scope file": str(info.allowlist_file),
        "scope status": info.allowlist_status,
        "scope entries": info.allowlist_count,
    }
    if info.allowlist_message != "-":
        details["reason"] = info.allowlist_message

    summary = (
        f"sync scope loaded ({info.allowlist_count} entries)"
        if info.allowlist_status == "loaded" and not issues
        else f"sync scope {info.allowlist_status}"
    )
    command = "sync check-allowlist" if args.sync_command == "check-allowlist" else "sync check-scope"
    return CommandReport(
        command=command,
        status=status_from_issues(issues),
        summary=summary,
        details=details,
        issues=report_issues(issues),
    )


def cmd_sync_check_allowlist(args: argparse.Namespace, paths: RuntimePaths) -> int:
    """Run the primary check-scope command or its legacy check-allowlist alias."""
    report = _sync_check_allowlist_report(args, paths)
    print(render_report(report, as_json=args.json))
    return exit_code_for_report(report)


def _sync_clear_stale_lock_report(args: argparse.Namespace, paths: RuntimePaths) -> CommandReport:
    load_result = load_config(paths)
    issues = sort_issues(list(load_result.issues))
    lock_state = read_sync_lock_state(load_result.config)
    details: dict[str, object] = {
        "execute": "yes" if args.execute else "no",
        "sync lock status": lock_state.status,
        "sync lock active": "yes" if lock_state.active else "no",
        "sync lock pid": lock_state.pid,
        "sync lock mode": lock_state.mode,
        "sync lock started": lock_state.started_at,
        "sync lock dir": str(paths.state_dir / "bisync.lock"),
    }

    if lock_state.status == "active":
        issues = sort_issues(
            issues
            + [
                ConfigIssue(
                    key="PCLOUD_TOOLS_SYNC_LOCK",
                    level="error",
                    message=f"sync lock is active (pid={lock_state.pid})",
                )
            ]
        )
        return CommandReport(
            command="sync clear-stale-lock",
            status="error",
            summary="active sync lock cannot be cleared",
            details=details,
            issues=report_issues(issues),
        )

    details["planned action"] = "remove lock directory" if lock_state.status in {"stale", "invalid"} else "no-op"

    if not args.execute:
        return CommandReport(
            command="sync clear-stale-lock",
            status=status_from_issues(issues),
            summary="stale lock preview is ready",
            details=details,
            issues=report_issues(issues),
        )

    removed = False
    if lock_state.status in {"stale", "invalid"}:
        removed = clear_sync_lock(load_result.config)

    details["removed"] = "yes" if removed else "no"
    refreshed = read_sync_lock_state(load_result.config)
    details["sync lock status"] = refreshed.status
    details["sync lock active"] = "yes" if refreshed.active else "no"
    details["sync lock pid"] = refreshed.pid
    details["sync lock mode"] = refreshed.mode
    details["sync lock started"] = refreshed.started_at

    return CommandReport(
        command="sync clear-stale-lock",
        status=status_from_issues(issues),
        summary="stale lock cleared" if removed else "no stale lock to clear",
        details=details,
        issues=report_issues(issues),
    )


def cmd_sync_clear_stale_lock(args: argparse.Namespace, paths: RuntimePaths) -> int:
    report = _sync_clear_stale_lock_report(args, paths)
    print(render_report(report, as_json=args.json))
    return exit_code_for_report(report)


def _background_mode(args: argparse.Namespace) -> tuple[str | None, list[ConfigIssue]]:
    issues: list[ConfigIssue] = []
    mode = "normal"
    if args.resync and args.track_renames:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_SYNC_BACKGROUND_MODE",
                level="error",
                message="--resync and --track-renames cannot be used together",
            )
        )
        return None, issues
    if args.resync:
        mode = "resync"
    elif args.track_renames:
        mode = "track-renames"
    return mode, issues


def _background_notify(args: argparse.Namespace) -> tuple[bool, list[ConfigIssue]]:
    issues: list[ConfigIssue] = []
    if args.notify and args.no_notify:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_SYNC_BACKGROUND_NOTIFY",
                level="error",
                message="--notify and --no-notify cannot be used together",
            )
        )
        return False, issues
    if args.no_notify:
        return False, issues
    if args.notify:
        return True, issues
    return True, issues


def _sync_background_report(args: argparse.Namespace, paths: RuntimePaths) -> CommandReport:
    load_result = load_config(paths)
    config = load_result.config
    mode, mode_issues = _background_mode(args)
    notify_on_finish, notify_issues = _background_notify(args)
    issues = sort_issues(list(load_result.issues) + mode_issues + notify_issues)
    lock_state = read_sync_lock_state(config)

    details: dict[str, object] = {
        "execute": "yes" if args.execute else "no",
        "mode": mode or "-",
        "notify on finish": "yes" if notify_on_finish else "no",
        "sync lock status": lock_state.status,
        "sync lock active": "yes" if lock_state.active else "no",
        "sync lock pid": lock_state.pid,
    }

    if issues and has_errors(issues):
        return CommandReport(
            command="sync background",
            status="error",
            summary="background sync options are invalid",
            details=details,
            issues=report_issues(issues),
        )

    if lock_state.status == "active":
        issues = sort_issues(
            issues
            + [
                ConfigIssue(
                    key="PCLOUD_TOOLS_SYNC_LOCK",
                    level="error",
                    message=f"sync already running (pid={lock_state.pid})",
                )
            ]
        )
        return CommandReport(
            command="sync background",
            status="error",
            summary="background sync cannot start while another sync is active",
            details=details,
            issues=report_issues(issues),
        )

    if lock_state.status in {"stale", "invalid"}:
        issues = sort_issues(
            issues
            + [
                ConfigIssue(
                    key="PCLOUD_TOOLS_SYNC_LOCK",
                    level="error",
                    message=(
                        f"sync lock is {lock_state.status}; run `sync clear-stale-lock` before launching background sync"
                    ),
                )
            ]
        )
        return CommandReport(
            command="sync background",
            status="error",
            summary="background sync requires clearing the stale lock first",
            details=details,
            issues=report_issues(issues),
        )

    child_command = (
        sys.executable,
        "-m",
        "pcloud_tools.cli",
        "sync",
        "_run",
        mode,
        "1" if notify_on_finish else "0",
    )
    details["launcher command"] = list(child_command)

    if not args.execute:
        return CommandReport(
            command="sync background",
            status=status_from_issues(issues),
            summary="background sync preview is ready",
            details=details,
            issues=report_issues(issues),
        )

    if paths.dev_mode:
        issues = sort_issues(
            issues
            + [
                ConfigIssue(
                    key="PCLOUD_TOOLS_DEV_BACKGROUND_EXECUTION",
                    level="error",
                    message="refusing --execute for `sync background` from pcloud-manager-dev",
                )
            ]
        )
        details["reason"] = "use preview in dev mode; execution requires the public entrypoint or an explicit non-dev runtime"
        return CommandReport(
            command="sync background",
            status="error",
            summary="dev mode refuses to execute sync background",
            details=details,
            issues=report_issues(issues),
        )

    try:
        launch = launch_background_sync(config, mode, notify_on_finish, child_command)
    except SyncExecutionError as exc:
        issues = sort_issues(
            issues + [ConfigIssue(key="PCLOUD_TOOLS_SYNC_BACKGROUND", level="error", message=str(exc))]
        )
        return CommandReport(
            command="sync background",
            status="error",
            summary="background sync launch failed",
            details=details,
            issues=report_issues(issues),
        )

    details.update(
        {
            "child pid": str(launch.pid),
            "stdout log": str(launch.stdout_log),
            "stderr log": str(launch.stderr_log),
        }
    )
    return CommandReport(
        command="sync background",
        status=status_from_issues(issues),
        summary="background sync launched",
        details=details,
        issues=report_issues(issues),
    )


def cmd_sync_background(args: argparse.Namespace, paths: RuntimePaths) -> int:
    report = _sync_background_report(args, paths)
    print(render_report(report, as_json=args.json))
    return exit_code_for_report(report)


def cmd_sync_internal_run(args: argparse.Namespace, paths: RuntimePaths) -> int:
    load_result = load_config(paths)
    config = load_result.config
    issues = sort_issues(
        list(load_result.issues) + list(enforce_sync_scope_guard(config, args.mode))
    )
    if issues and has_errors(issues):
        report = CommandReport(
            command="sync _run",
            status="error",
            summary="internal sync run cannot start until configuration issues are resolved",
            details={"mode": args.mode},
            issues=report_issues(issues),
        )
        print(render_report(report))
        return 1

    if paths.dev_mode:
        report = CommandReport(
            command="sync _run",
            status="error",
            summary="dev mode refuses internal sync execution",
            details={"mode": args.mode},
            issues=report_issues(
                [
                    ConfigIssue(
                        key="PCLOUD_TOOLS_DEV_SYNC_INTERNAL",
                        level="error",
                        message="refusing internal sync execution from pcloud-manager-dev",
                    )
                ]
            ),
        )
        print(render_report(report))
        return 1

    rclone_bin = _command_path(config.rclone_bin)
    if not rclone_bin:
        report = CommandReport(
            command="sync _run",
            status="error",
            summary="rclone command not found",
            details={"mode": args.mode},
            issues=report_issues(
                [ConfigIssue(key="PCLOUD_TOOLS_SYNC_EXEC", level="error", message="rclone command not found")]
            ),
        )
        print(render_report(report))
        return 1

    scope_info = sync_allowlist_info(config)
    scope_validation = sort_issues(scope_issues(scope_info))
    if scope_validation and has_errors(scope_validation):
        report = CommandReport(
            command="sync _run",
            status="error",
            summary="internal sync run cannot start until scope issues are resolved",
            details={"mode": args.mode},
            issues=report_issues(scope_validation),
        )
        print(render_report(report))
        return 1

    try:
        plan = build_sync_plan(
            config,
            args.mode,
            scope_info.entries,
            rclone_bin=rclone_bin,
            resync_mode=args.resync_mode,
        )
        result = execute_sync_plan(config, plan)
    except SyncExecutionError as exc:
        report = CommandReport(
            command="sync _run",
            status="error",
            summary="internal sync run failed before launch",
            details={"mode": args.mode},
            issues=report_issues(
                [ConfigIssue(key="PCLOUD_TOOLS_SYNC_EXEC", level="error", message=str(exc))]
            ),
        )
        print(render_report(report))
        return 1

    if args.notify_flag == "1":
        send_sync_notification(config, result.exit_code, args.mode)
    return result.exit_code

_REQUIRED_SHADOW_CHECKS = {
    "temporary workspace guard",
    "temporary state dir guard",
    "unsafe state dir guard",
}


def _saved_shadow_report_check(
    report_path: Path | None,
    *,
    issue_key: str = "PCLOUD_TOOLS_AUTOSYNC_SHADOW_REPORT",
) -> tuple[dict[str, object], list[ConfigIssue]]:
    if report_path is None:
        return (
            {
                "name": "saved shadow validation report",
                "status": "pending",
                "detail": "pass --report-path after saving scripts/pcloud-shadow-validation.py --report-path",
            },
            [
                ConfigIssue(
                    key=issue_key,
                    level="warning",
                    message="saved shadow validation report was not provided",
                )
            ],
        )

    path = report_path.expanduser()
    if not path.exists() or not path.is_file():
        return (
            {
                "name": "saved shadow validation report",
                "status": "missing",
                "detail": str(path),
            },
            [
                ConfigIssue(
                    key=issue_key,
                    level="warning",
                    message=f"saved shadow validation report is missing: {path}",
                )
            ],
        )

    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return (
            {
                "name": "saved shadow validation report",
                "status": "invalid",
                "detail": str(exc),
            },
            [
                ConfigIssue(
                    key=issue_key,
                    level="warning",
                    message=f"saved shadow validation report could not be read: {exc}",
                )
            ],
        )

    checks = payload.get("checks")
    check_names = {
        str(check.get("name", ""))
        for check in checks
        if isinstance(check, dict)
    } if isinstance(checks, list) else set()
    failed = [
        str(check.get("name", "unknown"))
        for check in checks
        if isinstance(check, dict) and check.get("status") != "ok"
    ] if isinstance(checks, list) else ["checks missing"]
    missing_required = sorted(_REQUIRED_SHADOW_CHECKS - check_names)
    workspace = str(payload.get("workspace", ""))
    state_dir = str(payload.get("state_dir", ""))
    temp_workspace_ok = "/pcloud-shadow-validation-" in workspace and workspace.endswith("/workspace")
    temp_state_ok = state_dir == f"{workspace}/.dev-state/state" if workspace else False
    report_status = payload.get("status")
    if report_status == "ok" and not failed and not missing_required and temp_workspace_ok and temp_state_ok:
        return (
            {
                "name": "saved shadow validation report",
                "status": "ok",
                "detail": f"{path}; required checks present; temp state guard verified",
            },
            [],
        )

    detail = (
        f"top-level status={report_status!r}; failed checks={failed}; "
        f"missing required checks={missing_required}; temp workspace ok={temp_workspace_ok}; "
        f"temp state dir ok={temp_state_ok}"
    )
    return (
        {
            "name": "saved shadow validation report",
            "status": "not-ok",
            "detail": detail,
        },
        [
            ConfigIssue(
                key=issue_key,
                level="warning",
                message=f"saved shadow validation report is not ok: {detail}",
            )
        ],
    )
