from __future__ import annotations

import argparse
import json
import os
import plistlib
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from .autosync_runtime import disable_autosync, enable_autosync, read_autosync_state
from .cli_common import (
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
from .config import AppConfig, ConfigIssue, load_config
from .gates import GATES, add_gate_review_args, validate_gate
from .io_utils import atomic_write_json
from .output import CommandReport, ReportAction, render_report
from .runtime import RuntimePaths
from .sync_exec import (
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
from .sync_runtime import (
    clear_sync_lock,
    parse_sync_progress,
    read_latest_sync_logs,
    read_sync_lock_state,
    read_sync_state,
    sync_last_error_status,
)
from .sync_scope import (
    prepare_sync_filter_rules,
    scope_issues,
    sync_allowlist_info,
    sync_filter_file,
    write_sync_filter_file,
)

def _rclone_cache_dir() -> Path:
    raw = os.environ.get("XDG_CACHE_HOME", "").strip()
    if raw:
        return Path(raw).expanduser() / "rclone"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "rclone"
    return Path.home() / ".cache" / "rclone"


def _rclone_bisync_lock_file(config: AppConfig) -> Path:
    def encode(value: str) -> str:
        return value.replace("/", "_").replace(":", "_")

    session = f"local_{encode(str(config.core_dir))}..{encode(config.core_remote)}"
    return _rclone_cache_dir() / "bisync" / f"{session}.lck"


def _process_active(pid: str) -> str:
    if not pid or pid == "-":
        return "unknown"
    try:
        os.kill(int(pid), 0)
    except ValueError:
        return "unknown"
    except ProcessLookupError:
        return "no"
    except PermissionError:
        return "yes"
    return "yes"


def _allowlist_root(path: str) -> str:
    if not path or path == "-":
        return "-"
    candidate = Path(path).expanduser()
    if candidate.name == ".pcloud-sync-allowlist":
        return str(candidate.parent)
    return "-"


def _rclone_bisync_lock_info(config: AppConfig) -> dict[str, object]:
    path = _rclone_bisync_lock_file(config)
    info: dict[str, object] = {
        "path": str(path),
        "status": "present" if path.exists() else "missing",
        "pid": "-",
        "process active": "unknown",
        "session": "-",
        "time renewed": "-",
        "time expires": "-",
        "delete command": ["rclone", "deletefile", str(path)],
    }
    if not path.exists():
        return info
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return info
    if not isinstance(payload, dict):
        return info
    pid = str(payload.get("PID", "-") or "-")
    info.update(
        {
            "pid": pid,
            "process active": _process_active(pid),
            "session": str(payload.get("Session", "-") or "-"),
            "time renewed": str(payload.get("TimeRenewed", "-") or "-"),
            "time expires": str(payload.get("TimeExpires", "-") or "-"),
        }
    )
    return info


def add_sync_parser(subparsers: argparse._SubParsersAction) -> None:
    sync_parser = subparsers.add_parser("sync", help="Sync command surface scaffold.")
    sync_parser.add_argument(
        "--execute",
        action="store_true",
        help="Run the normal rclone bisync command instead of only previewing it.",
    )
    sync_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit structured JSON output for the normal sync command.",
    )
    sync_subparsers = sync_parser.add_subparsers(dest="sync_command")
    sync_status_parser = sync_subparsers.add_parser("status")
    sync_status_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    sync_status_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
    sync_scope_parser = sync_subparsers.add_parser("scope")
    sync_scope_parser.add_argument("--filter", action="store_true", help="Include the generated bisync filter rules.")
    sync_scope_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    sync_check_parser = sync_subparsers.add_parser("check-allowlist")
    sync_check_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    sync_progress_parser = sync_subparsers.add_parser("progress")
    sync_progress_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    sync_background_parser = sync_subparsers.add_parser("background")
    sync_background_parser.add_argument("--resync", action="store_true", help="Run background sync in resync mode.")
    sync_background_parser.add_argument(
        "--track-renames",
        action="store_true",
        help="Run background sync in track-renames mode.",
    )
    sync_background_parser.add_argument("--notify", action="store_true", help="Send a completion notification.")
    sync_background_parser.add_argument(
        "--no-notify",
        action="store_true",
        help="Disable the completion notification.",
    )
    sync_background_parser.add_argument(
        "--execute",
        action="store_true",
        help="Launch the background sync instead of only previewing it.",
    )
    sync_background_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    sync_enable_autosync_parser = sync_subparsers.add_parser("enable-autosync")
    sync_enable_autosync_parser.add_argument(
        "--execute",
        action="store_true",
        help="Apply the launchctl changes instead of only previewing them.",
    )
    sync_enable_autosync_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    sync_disable_autosync_parser = sync_subparsers.add_parser("disable-autosync")
    sync_disable_autosync_parser.add_argument(
        "--yes",
        action="store_true",
        help="Acknowledge the disable action without prompting.",
    )
    sync_disable_autosync_parser.add_argument(
        "--execute",
        action="store_true",
        help="Apply the launchctl changes instead of only previewing them.",
    )
    sync_disable_autosync_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    sync_autosync_plist_parser = sync_subparsers.add_parser(
        "autosync-plist", help="Preview or write the dev autosync LaunchAgent plist."
    )
    sync_autosync_plist_parser.add_argument(
        "--execute",
        action="store_true",
        help="Write the plist when running in dev mode with a .dev-state target.",
    )
    sync_autosync_plist_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    sync_autosync_gate_parser = sync_subparsers.add_parser(
        "autosync-gate", help="Read-only checklist before changing autosync launchd registration."
    )
    sync_autosync_gate_parser.add_argument("--report-path", type=Path)
    add_gate_review_args(sync_autosync_gate_parser, GATES["autosync.launchd"])
    sync_autosync_gate_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    sync_autosync_gate_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
    sync_autosync_run_parser = sync_subparsers.add_parser(
        "autosync-run", help="Run guarded autosync launchd changes after the dedicated gate opens."
    )
    sync_autosync_run_parser.add_argument("mode", choices=("enable", "disable"))
    sync_autosync_run_parser.add_argument("--report-path", type=Path)
    add_gate_review_args(sync_autosync_run_parser, GATES["autosync.launchd"])
    sync_autosync_run_parser.add_argument("--execute", action="store_true")
    sync_autosync_run_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    sync_autosync_run_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
    sync_migration_gate_parser = sync_subparsers.add_parser(
        "migration-gate", help="Read-only checklist before running normal sync/resync migration validation."
    )
    sync_migration_gate_parser.add_argument("--report-path", type=Path)
    sync_migration_gate_parser.add_argument("--sync-status-report-path", type=Path)
    add_gate_review_args(sync_migration_gate_parser, GATES["sync.migration"])
    sync_migration_gate_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    sync_migration_gate_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
    sync_migration_run_parser = sync_subparsers.add_parser(
        "migration-run", help="Run guarded normal/resync migration validation after the dedicated gate opens."
    )
    sync_migration_run_parser.add_argument("mode", choices=("normal", "resync"))
    sync_migration_run_parser.add_argument("--resync-mode", choices=RESYNC_MODES, default=DEFAULT_RESYNC_MODE)
    sync_migration_run_parser.add_argument("--report-path", type=Path)
    sync_migration_run_parser.add_argument("--sync-status-report-path", type=Path)
    add_gate_review_args(sync_migration_run_parser, GATES["sync.migration"])
    sync_migration_run_parser.add_argument("--execute", action="store_true")
    sync_migration_run_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    sync_migration_run_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
    sync_clear_stale_lock_parser = sync_subparsers.add_parser("clear-stale-lock")
    sync_clear_stale_lock_parser.add_argument(
        "--execute",
        action="store_true",
        help="Remove the stale lock instead of only previewing it.",
    )
    sync_clear_stale_lock_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    sync_internal_run_parser = sync_subparsers.add_parser("_run", help=argparse.SUPPRESS)
    sync_internal_run_parser.add_argument("mode", choices=("normal", "autosync", "resync", "full-resync", "track-renames"))
    sync_internal_run_parser.add_argument("notify_flag", nargs="?", default="0")
    sync_internal_run_parser.add_argument("--resync-mode", choices=RESYNC_MODES, default=DEFAULT_RESYNC_MODE)
    for name in ("resync", "full-resync"):
        command_parser = sync_subparsers.add_parser(name)
        command_parser.add_argument(
            "--execute",
            action="store_true",
            help="Run the rclone bisync command instead of only previewing it.",
        )
        command_parser.add_argument(
            "--resync-mode",
            choices=RESYNC_MODES,
            default=DEFAULT_RESYNC_MODE,
            help="Choose which side rclone prefers when rebuilding the bisync baseline.",
        )
        command_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    track_renames_parser = sync_subparsers.add_parser("track-renames")
    track_renames_parser.add_argument(
        "--execute",
        action="store_true",
        help="Run the rclone bisync command instead of only previewing it.",
    )
    track_renames_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")


def cmd_sync(args: argparse.Namespace, paths: RuntimePaths) -> int | None:
    if args.sync_command is None:
        return cmd_sync_execution(args, paths, "normal")
    if args.sync_command == "status":
        return cmd_sync_status(args, paths)
    if args.sync_command == "progress":
        return cmd_sync_progress(args, paths)
    if args.sync_command == "background":
        return cmd_sync_background(args, paths)
    if args.sync_command == "clear-stale-lock":
        return cmd_sync_clear_stale_lock(args, paths)
    if args.sync_command == "enable-autosync":
        return cmd_sync_autosync(args, paths, "enable-autosync")
    if args.sync_command == "disable-autosync":
        return cmd_sync_autosync(args, paths, "disable-autosync")
    if args.sync_command == "autosync-plist":
        return cmd_sync_autosync_plist(args, paths)
    if args.sync_command == "autosync-gate":
        return cmd_sync_autosync_gate(args, paths)
    if args.sync_command == "autosync-run":
        return cmd_sync_autosync_run(args, paths)
    if args.sync_command == "migration-gate":
        return cmd_sync_migration_gate(args, paths)
    if args.sync_command == "migration-run":
        return cmd_sync_migration_run(args, paths)
    if args.sync_command == "_run":
        return cmd_sync_internal_run(args, paths)
    if args.sync_command in {"resync", "full-resync", "track-renames"}:
        return cmd_sync_execution(args, paths, args.sync_command)
    if args.sync_command == "scope":
        return cmd_sync_scope(args, paths)
    if args.sync_command == "check-allowlist":
        return cmd_sync_check_allowlist(args, paths)
    return None


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


def _readable_baseline(info_file: Path, mode: str, status: str) -> str:
    if status == "defaulted":
        return f"{mode} (default)"
    if status == "invalid":
        return f"invalid ({info_file})"
    return mode


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
        "allowlist": str(load_result.config.allowlist_file),
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

    try:
        rclone_bin = _command_path("rclone")
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
        "scope mode": plan.scope_mode,
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
        "scope mode": "allowlist",
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
        summary="allowlist scope is ready" if not issues else "allowlist scope needs attention",
        details=details,
        issues=report_issues(issues),
    )


def cmd_sync_scope(args: argparse.Namespace, paths: RuntimePaths) -> int:
    report = _sync_scope_report(args, paths)
    print(render_report(report, as_json=args.json))
    return exit_code_for_report(report)


def _sync_check_allowlist_report(args: argparse.Namespace, paths: RuntimePaths) -> CommandReport:
    load_result = load_config(paths)
    info = sync_allowlist_info(load_result.config)
    issues = sort_issues(list(load_result.issues) + scope_issues(info))
    details: dict[str, object] = {
        "file": str(info.allowlist_file),
        "status": info.allowlist_status,
        "entries": info.allowlist_count,
    }
    if info.allowlist_message != "-":
        details["reason"] = info.allowlist_message

    summary = (
        f"allowlist loaded ({info.allowlist_count} entries)"
        if info.allowlist_status == "loaded" and not issues
        else f"allowlist {info.allowlist_status}"
    )
    return CommandReport(
        command="sync check-allowlist",
        status=status_from_issues(issues),
        summary=summary,
        details=details,
        issues=report_issues(issues),
    )


def cmd_sync_check_allowlist(args: argparse.Namespace, paths: RuntimePaths) -> int:
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

    rclone_bin = _command_path("rclone")
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


def _sync_autosync_report(args: argparse.Namespace, paths: RuntimePaths, action: str) -> CommandReport:
    load_result = load_config(paths)
    config = load_result.config
    issues = sort_issues(list(load_result.issues))
    autosync = read_autosync_state(config)
    details: dict[str, object] = {
        "execute": "yes" if args.execute else "no",
        "autosync state": autosync.state,
        "autosync runs": autosync.runs,
        "autosync label": autosync.label,
        "autosync plist": autosync.plist,
    }
    if action == "disable-autosync":
        details["assume yes"] = "yes" if args.yes else "no"

    plist_required = action == "enable-autosync"
    if plist_required and not config.autosync_plist.exists():
        issues = sort_issues(
            issues
            + [
                ConfigIssue(
                    key="PCLOUD_TOOLS_AUTOSYNC_PLIST",
                    level="warning" if not args.execute else "error",
                    message=f"autosync plist not found: {config.autosync_plist}",
                )
            ]
        )

    if issues and has_errors(issues):
        return CommandReport(
            command=f"sync {action}",
            status="error",
            summary="autosync command cannot run until configuration issues are resolved",
            details=details,
            issues=report_issues(issues),
        )

    details["planned action"] = (
        f"launchctl enable gui/<uid>/{config.autosync_label} and bootstrap {config.autosync_plist}"
        if action == "enable-autosync"
        else f"launchctl bootout/disable gui/<uid>/{config.autosync_label}"
    )

    if not args.execute:
        return CommandReport(
            command=f"sync {action}",
            status=status_from_issues(issues),
            summary="autosync preview is ready",
            details=details,
            issues=report_issues(issues),
        )

    if paths.dev_mode:
        issues = sort_issues(
            issues
            + [
                ConfigIssue(
                    key="PCLOUD_TOOLS_DEV_AUTOSYNC_EXECUTION",
                    level="error",
                    message=f"refusing --execute for `sync {action}` from pcloud-manager-dev",
                )
            ]
        )
        details["reason"] = (
            "use preview in dev mode; execution requires the public entrypoint or an explicit non-dev runtime"
        )
        return CommandReport(
            command=f"sync {action}",
            status="error",
            summary=f"dev mode refuses to execute sync {action}",
            details=details,
            issues=report_issues(issues),
        )

    if action == "disable-autosync" and not args.yes:
        issues = sort_issues(
            issues
            + [
                ConfigIssue(
                    key="PCLOUD_TOOLS_AUTOSYNC_CONFIRMATION",
                    level="error",
                    message="disable-autosync requires --yes together with --execute",
                )
            ]
        )
        return CommandReport(
            command=f"sync {action}",
            status="error",
            summary="disable-autosync requires confirmation",
            details=details,
            issues=report_issues(issues),
        )

    try:
        if action == "enable-autosync":
            enable_autosync(config)
        else:
            disable_autosync(config)
    except RuntimeError as exc:
        issues = sort_issues(
            issues + [ConfigIssue(key="PCLOUD_TOOLS_AUTOSYNC_EXEC", level="error", message=str(exc))]
        )
        return CommandReport(
            command=f"sync {action}",
            status="error",
            summary="autosync command failed",
            details=details,
            issues=report_issues(issues),
        )

    refreshed = read_autosync_state(config)
    details.update(
        {
            "autosync state": refreshed.state,
            "autosync runs": refreshed.runs,
        }
    )
    return CommandReport(
        command=f"sync {action}",
        status="ok",
        summary="autosync command executed",
        details=details,
        issues=report_issues(issues),
    )


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _autosync_plist_payload(paths: RuntimePaths, config: AppConfig) -> dict[str, object]:
    entrypoint = entrypoint_command(paths)
    return {
        "Label": config.autosync_label,
        "ProgramArguments": [entrypoint, "sync", "background", "--execute"],
        "RunAtLoad": False,
        "StartInterval": 300,
        "StandardOutPath": str(config.log_dir / "autosync-launchd.out"),
        "StandardErrorPath": str(config.log_dir / "autosync-launchd.err"),
    }


def _sync_autosync_plist_report(args: argparse.Namespace, paths: RuntimePaths) -> CommandReport:
    load_result = load_config(paths)
    config = load_result.config
    issues = list(load_result.issues)
    payload = _autosync_plist_payload(paths, config)
    plist_path = config.autosync_plist
    dev_state_root = paths.workspace_root / ".dev-state"
    details: dict[str, object] = {
        "execute": "yes" if args.execute else "no",
        "planned action": "write autosync LaunchAgent plist" if args.execute else "preview autosync LaunchAgent plist",
        "autosync label": config.autosync_label,
        "autosync plist": str(plist_path),
        "autosync plist status": "present" if plist_path.exists() else "missing",
        "plist payload": payload,
        "state writes": "autosync plist only" if args.execute else "none",
        "launchctl execution": "no",
        "scheduled sync execution": "no",
    }
    if args.execute and not paths.dev_mode:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_AUTOSYNC_PLIST_EXECUTION",
                level="error",
                message="autosync-plist --execute is limited to pcloud-manager-dev/dev mode",
            )
        )
    if args.execute and not _is_relative_to(plist_path, dev_state_root):
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_AUTOSYNC_PLIST_PATH",
                level="error",
                message=f"refusing to write autosync plist outside {dev_state_root}: {plist_path}",
            )
        )
    issues = sort_issues(issues)
    if has_errors(issues):
        details["state writes"] = "none"
    if args.execute and not has_errors(issues):
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        plist_path.write_bytes(plistlib.dumps(payload, sort_keys=True))
        details["autosync plist status"] = "written"
    return CommandReport(
        command="sync autosync-plist",
        status=status_from_issues(issues),
        summary="autosync plist written" if args.execute and not has_errors(issues) else "autosync plist preview is ready",
        details=details,
        issues=report_issues(issues),
    )


def cmd_sync_autosync_plist(args: argparse.Namespace, paths: RuntimePaths) -> int:
    report = _sync_autosync_plist_report(args, paths)
    print(render_report(report, as_json=args.json))
    return exit_code_for_report(report)


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


def _render_autosync_gate_human(report: CommandReport) -> str:
    details = report.details
    lines = [
        f"{report.command}: {report.status}",
        report.summary,
        f"launchd gate: {details.get('launchd gate status', '-')}",
        f"autosync changes can run: {details.get('autosync changes can run', '-')}",
        f"state writes: {details.get('state writes', '-')}",
        f"autosync state: {details.get('autosync state', '-')}",
        f"autosync label: {details.get('autosync label', '-')}",
        f"autosync plist: {details.get('autosync plist', '-')}",
        f"autosync plist status: {details.get('autosync plist status', '-')}",
        f"autosync plist note: {details.get('autosync plist note', '-')}",
        f"launchctl: {details.get('launchctl availability', '-')} ({details.get('launchctl binary', '-')})",
        f"approval status: {details.get('autosync approval status', '-')}",
        f"human gate: {details.get('human gate status', '-')}",
        f"next human check trigger: {details.get('next human check trigger', '-')}",
    ]
    commands = [
        ("enable preview", details.get("enable preview command")),
        ("disable preview", details.get("disable preview command")),
        ("plist review", details.get("autosync plist review command")),
    ]
    for label, command in commands:
        if command:
            lines.append(f"{label}: {shell_command(command)}")
    launchctl_commands = [
        ("enable launchctl commands", details.get("enable launchctl commands")),
        ("disable launchctl commands", details.get("disable launchctl commands")),
    ]
    for label, command_group in launchctl_commands:
        if not isinstance(command_group, list) or not command_group:
            continue
        lines.append(f"{label}:")
        for command in command_group:
            lines.append(f"- {shell_command(command)}")
    checks = details.get("preflight checks")
    if isinstance(checks, list) and checks:
        lines.append("preflight checks:")
        for check in checks:
            if not isinstance(check, dict):
                continue
            lines.append(
                "- "
                f"{check.get('name', '-')}: "
                f"{check.get('status', '-')} - "
                f"{check.get('detail', '-')}"
            )
    blocked = details.get("blocked operations")
    if isinstance(blocked, list) and blocked:
        lines.append("blocked operations:")
        for item in blocked:
            lines.append(f"- {item}")
    if report.issues:
        lines.append("warnings:")
        for issue in report.issues:
            if issue.level == "warning":
                lines.append(f"- {issue.key}: {issue.message}")
    return "\n".join(lines)


def _autosync_launchd_run_state_file(config: AppConfig) -> Path:
    return config.state_dir / "sync" / "autosync-launchd-last-run.json"


def _autosync_launchd_gate_spec():
    return GATES["autosync.launchd"]


def _autosync_launchd_gate_open(config: AppConfig) -> bool:
    spec = _autosync_launchd_gate_spec()
    return config.autosync_launchd_gate == spec.expected_value


def _actual_launchctl_commands(config: AppConfig, launchctl_bin: str, mode: str) -> list[list[str]]:
    uid = str(os.getuid())
    target = f"gui/{uid}/{config.autosync_label}"
    if mode == "enable":
        return [
            [launchctl_bin, "enable", target],
            [launchctl_bin, "bootstrap", f"gui/{uid}", str(config.autosync_plist)],
        ]
    return [
        [launchctl_bin, "bootout", target],
        [launchctl_bin, "disable", target],
    ]


def _render_autosync_run_human(report: CommandReport) -> str:
    details = report.details
    lines = [
        f"{report.command}: {report.status}",
        report.summary,
        f"launchd run gate: {details.get('launchd run gate status', '-')}",
        f"autosync changes can run: {details.get('autosync changes can run', '-')}",
        f"mode: {details.get('mode', '-')}",
        f"execute requested: {details.get('execute requested', '-')}",
        f"state writes: {details.get('state writes', '-')}",
        f"autosync state: {details.get('autosync state', '-')}",
        f"autosync label: {details.get('autosync label', '-')}",
        f"autosync plist: {details.get('autosync plist', '-')}",
        f"autosync plist status: {details.get('autosync plist status', '-')}",
        f"launchctl: {details.get('launchctl availability', '-')} ({details.get('launchctl binary', '-')})",
        f"approval status: {details.get('autosync approval status', '-')}",
    ]
    commands = details.get("planned launchctl commands")
    if isinstance(commands, list) and commands:
        lines.append("planned launchctl commands:")
        for command in commands:
            lines.append(f"- {shell_command(command)}")
    state_file = details.get("launchd run state file")
    if state_file:
        lines.append(f"launchd run state: {state_file}")
    checks = details.get("preflight checks")
    blocked_checks: list[str] = []
    if isinstance(checks, list):
        for check in checks:
            if isinstance(check, dict) and check.get("status") != "ok":
                blocked_checks.append(
                    f"{check.get('name', '-')}: {check.get('status', '-')} - {check.get('detail', '-')}"
                )
    if blocked_checks:
        lines.append("blocked checks:")
        for check in blocked_checks:
            lines.append(f"- {check}")
    if report.issues:
        lines.append("warnings:" if report.status != "error" else "issues:")
        for issue in report.issues:
            lines.append(f"- {issue.key}: {issue.message}")
    return "\n".join(lines)


def _sync_autosync_gate_report(args: argparse.Namespace, paths: RuntimePaths) -> CommandReport:
    load_result = load_config(paths)
    config = load_result.config
    autosync_gate = validate_gate(
        _autosync_launchd_gate_spec(),
        args,
        {_autosync_launchd_gate_spec().env_var: config.autosync_launchd_gate},
    )
    autosync = read_autosync_state(config)
    issues = list(load_result.issues)
    shadow_check, shadow_issues = _saved_shadow_report_check(getattr(args, "report_path", None))
    issues.extend(shadow_issues)
    launchctl_bin = _command_path("launchctl")
    launchctl_check = {
        "name": "launchctl binary",
        "status": "ok" if launchctl_bin else "pending",
        "detail": launchctl_bin or "launchctl not found by command -v; verify before launchd changes",
    }
    if not launchctl_bin:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_AUTOSYNC_LAUNCHCTL",
                level="warning",
                message="launchctl was not found by command -v; autosync gate remains closed",
            )
        )
    plist_check = {
        "name": "autosync plist",
        "status": "ok" if config.autosync_plist.exists() else "pending",
        "detail": str(config.autosync_plist),
    }
    if not config.autosync_plist.exists():
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_AUTOSYNC_PLIST",
                level="warning",
                message=f"autosync plist not found: {config.autosync_plist}",
            )
        )

    entrypoint = entrypoint_command(paths)
    enable_preview = [entrypoint, "sync", "enable-autosync", "--json"]
    disable_preview = [entrypoint, "sync", "disable-autosync", "--yes", "--json"]
    plist_review = ["plutil", "-p", str(config.autosync_plist)]
    target = f"gui/<uid>/{config.autosync_label}"
    enable_launchctl = [
        [launchctl_bin or "launchctl", "enable", target],
        [launchctl_bin or "launchctl", "bootstrap", "gui/<uid>", str(config.autosync_plist)],
    ]
    disable_launchctl = [
        [launchctl_bin or "launchctl", "bootout", target],
        [launchctl_bin or "launchctl", "disable", target],
    ]
    checks = [
        shadow_check,
        launchctl_check,
        plist_check,
        {
            "name": "autosync preview commands",
            "status": "ok",
            "detail": f"{' '.join(enable_preview)}; {' '.join(disable_preview)}",
        },
        {
            "name": "operator preview review",
            "status": "ok" if autosync_gate.flag_ok("--operator-reviewed-preview") else "pending",
            "detail": "operator reviewed enable/disable autosync preview output and launchd label",
        },
        {
            "name": "plist approval",
            "status": "ok" if autosync_gate.flag_ok("--reviewer-approved-plist") else "pending",
            "detail": "reviewer approved plist path, label, and public entrypoint target",
        },
        {
            "name": "launchctl policy approval",
            "status": "ok" if autosync_gate.flag_ok("--reviewer-approved-launchctl-policy") else "pending",
            "detail": "reviewer approved bootstrap/bootout/enable/disable behavior before launchd changes",
        },
        {
            "name": "rollback policy approval",
            "status": "ok" if autosync_gate.flag_ok("--reviewer-approved-rollback-policy") else "pending",
            "detail": "reviewer approved rollback commands and stop conditions before launchd changes",
        },
        {
            "name": "parallel dangerous gates",
            "status": "ok",
            "detail": "fswatch resident start, pCloud API long-poll start, normal sync/resync, and archive work stay out of scope",
        },
    ]
    approval_status = "complete-read-only" if all(check["status"] == "ok" for check in checks) else "pending"
    issues.append(
        ConfigIssue(
            key=autosync_gate.spec.env_var,
            level="warning",
            message="autosync launchd changes remain gated; this command is read-only",
        )
    )
    issues = sort_issues(issues)
    details: dict[str, object] = {
        "planned action": "check autosync launchd change prerequisites",
        "implementation status": "read-only checklist; launchctl is not executed",
        "launchd gate status": "closed",
        "autosync changes can run": "no",
        "operator verification required": "yes-before-autosync-launchd-gate",
        "human gate status": "required-before-autosync-launchd-change",
        "human gate reason": "autosync launchd changes would alter scheduled live sync behavior",
        "state writes": "none",
        "autosync state": autosync.state,
        "autosync runs": autosync.runs,
        "autosync label": autosync.label,
        "autosync plist": autosync.plist,
        "autosync plist status": "present" if config.autosync_plist.exists() else "missing",
        "autosync plist note": "review or create the plist in a separate gate; autosync-gate does not write it",
        "autosync plist review command": plist_review,
        "launchctl availability": "available" if launchctl_bin else "missing",
        "launchctl binary": launchctl_bin or "-",
        "enable preview command": enable_preview,
        "disable preview command": disable_preview,
        "enable launchctl commands": enable_launchctl,
        "disable launchctl commands": disable_launchctl,
        "autosync approval status": approval_status,
        "preflight checks": checks,
        "success policy": "apply launchctl changes only after explicit operator command and verify autosync status afterward",
        "failure policy": "stop on launchctl failure and keep prior autosync state for operator review",
        "rollback policy": "use the displayed inverse launchctl command and saved plist/label information",
        "blocked operations": [
            "launchctl enable",
            "launchctl bootstrap",
            "launchctl bootout",
            "launchctl disable",
            "starting or stopping scheduled sync jobs",
            "normal sync/resync execution",
        ],
        "next human check trigger": "explicit autosync launchd change implementation or scheduled sync behavior change",
    }
    return CommandReport(
        command="sync autosync-gate",
        status=status_from_issues(issues),
        summary="autosync launchd gate is closed",
        details=details,
        issues=report_issues(issues),
        actions=_sync_status_actions(paths, "missing"),
    )


def cmd_sync_autosync_gate(args: argparse.Namespace, paths: RuntimePaths) -> int:
    report = _sync_autosync_gate_report(args, paths)
    output_format = "xbar" if getattr(args, "xbar", False) else "json" if getattr(args, "json", False) else "human"
    if output_format == "human":
        print(_render_autosync_gate_human(report))
    else:
        print(render_report(report, output_format=output_format))
    return exit_code_for_report(report)


def _sync_autosync_run_report(args: argparse.Namespace, paths: RuntimePaths) -> CommandReport:
    gate_report = _sync_autosync_gate_report(args, paths)
    load_result = load_config(paths)
    config = load_result.config
    autosync = read_autosync_state(config)
    mode = str(getattr(args, "mode", "enable"))
    execute = bool(getattr(args, "execute", False))
    launchctl_bin = _command_path("launchctl")
    details = dict(gate_report.details)
    issues = [
        ConfigIssue(key=issue.key, level=issue.level, message=issue.message)
        for issue in gate_report.issues
        if issue.key != "PCLOUD_TOOLS_AUTOSYNC_LAUNCHD_GATE"
    ]
    approval_status = str(details.get("autosync approval status", "pending"))
    autosync_spec = _autosync_launchd_gate_spec()
    gate_open = _autosync_launchd_gate_open(config)
    state_file = _autosync_launchd_run_state_file(config)
    planned_commands = _actual_launchctl_commands(config, launchctl_bin or "launchctl", mode)

    details.update(
        {
            "planned action": "run autosync launchd changes" if execute else "preview autosync launchd run",
            "implementation status": (
                "guarded launchctl execution path"
                if execute
                else "autosync launchd run preview only; launchctl is not executed"
            ),
            "mode": mode,
            "launchd run gate status": (
                f"open: {autosync_spec.expected_value}"
                if gate_open
                else f"closed: requires {autosync_spec.env_var}={autosync_spec.expected_value}"
            ),
            "launchd gate status": (
                f"open: {autosync_spec.expected_value}"
                if gate_open
                else f"closed: requires {autosync_spec.env_var}={autosync_spec.expected_value}"
            ),
            "autosync changes can run": "yes" if gate_open and approval_status == "complete-read-only" else "no",
            "execute requested": "yes" if execute else "no",
            "state writes": "autosync launchd run state" if execute else "none",
            "planned launchctl commands": planned_commands,
            "launchd run state file": str(state_file),
            "future gate env": f"{autosync_spec.env_var}={autosync_spec.expected_value}",
        }
    )

    if approval_status != "complete-read-only":
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_AUTOSYNC_LAUNCHD_APPROVAL",
                level="error" if execute else "warning",
                message="autosync launchd execution requires complete read-only approvals",
            )
        )
    if not gate_open:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_AUTOSYNC_LAUNCHD_EXECUTION_GATE",
                level="error" if execute else "warning",
                message=(
                    "autosync launchd execution requires "
                    f"{autosync_spec.env_var}={autosync_spec.expected_value!r}"
                ),
            )
        )
    if not launchctl_bin:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_AUTOSYNC_LAUNCHCTL",
                level="error" if execute else "warning",
                message="launchctl command not found",
            )
        )
    if mode == "enable" and not config.autosync_plist.exists():
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_AUTOSYNC_PLIST",
                level="error" if execute else "warning",
                message=f"autosync plist not found: {config.autosync_plist}",
            )
        )

    if not execute or has_errors(issues):
        if has_errors(issues):
            details["state writes"] = "none"
        issues = sort_issues(issues)
        return CommandReport(
            command="sync autosync-run",
            status=status_from_issues(issues),
            summary=(
                "autosync launchd execution is gated"
                if has_errors(issues) or not gate_open
                else "autosync launchd run is ready"
            ),
            details=details,
            issues=report_issues(issues),
            actions=_sync_status_actions(paths, "missing"),
        )

    started_at = datetime.now(timezone.utc).isoformat()
    run_state: dict[str, object] = {
        "mode": mode,
        "started_at": started_at,
        "commands": planned_commands,
        "previous_autosync_state": autosync.state,
    }
    try:
        if mode == "enable":
            enable_autosync(config)
        else:
            disable_autosync(config)
    except RuntimeError as exc:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_AUTOSYNC_LAUNCHD_EXEC",
                level="error",
                message=str(exc),
            )
        )
    finished_at = datetime.now(timezone.utc).isoformat()
    refreshed = read_autosync_state(config)
    run_state.update(
        {
            "finished_at": finished_at,
            "autosync_state_after": refreshed.state,
            "autosync_runs_after": refreshed.runs,
        }
    )
    if not has_errors(issues):
        state_file.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(state_file, run_state, sort_keys=True)

    details.update(
        {
            "autosync state after": refreshed.state,
            "autosync runs after": refreshed.runs,
            "process result": run_state,
            "state writes": "autosync launchd run state" if not has_errors(issues) else "none",
        }
    )
    issues = sort_issues(issues)
    return CommandReport(
        command="sync autosync-run",
        status=status_from_issues(issues),
        summary="autosync launchd run completed" if not has_errors(issues) else "autosync launchd run failed",
        details=details,
        issues=report_issues(issues),
        actions=_sync_status_actions(paths, "missing"),
    )


def cmd_sync_autosync_run(args: argparse.Namespace, paths: RuntimePaths) -> int:
    report = _sync_autosync_run_report(args, paths)
    output_format = "xbar" if getattr(args, "xbar", False) else "json" if getattr(args, "json", False) else "human"
    if output_format == "human":
        print(_render_autosync_run_human(report))
    else:
        print(render_report(report, output_format=output_format))
    return exit_code_for_report(report)


def _render_migration_gate_human(report: CommandReport) -> str:
    details = report.details
    lines = [
        f"{report.command}: {report.status}",
        report.summary,
        f"migration gate: {details.get('migration gate status', '-')}",
        f"sync/resync can run: {details.get('sync/resync can run', '-')}",
        f"state writes: {details.get('state writes', '-')}",
        f"sync state: {details.get('sync state', '-')}",
        f"last result: {details.get('last result', '-')}",
        f"last error status: {details.get('last error status', '-')}",
        f"target root: {details.get('migration target root status', '-')} ({details.get('configured core dir', '-')})",
        f"sync lock: {details.get('sync lock status', '-')}",
        (
            "rclone bisync lock: "
            f"{details.get('rclone bisync lock status', '-')} "
            f"(pid={details.get('rclone bisync lock pid', '-')}; "
            f"process_active={details.get('rclone bisync lock process active', '-')})"
        ),
        (
            "scope: "
            f"{details.get('scope status', '-')}; "
            f"{details.get('scope baseline', '-')}; "
            f"entries={details.get('scope entries', '-')}"
        ),
        f"rclone: {details.get('rclone availability', '-')} ({details.get('rclone binary', '-')})",
        f"approval status: {details.get('migration approval status', '-')}",
        f"human gate: {details.get('human gate status', '-')}",
        f"next human check trigger: {details.get('next human check trigger', '-')}",
    ]
    commands = [
        ("status command", details.get("status command")),
        ("normal sync preview", details.get("normal sync preview command")),
        ("resync preview", details.get("resync preview command")),
    ]
    for label, command in commands:
        if command:
            lines.append(f"{label}: {shell_command(command)}")
    rclone_lock_delete = details.get("rclone bisync lock delete command")
    if details.get("rclone bisync lock status") == "present" and rclone_lock_delete:
        lines.append(f"rclone lock cleanup candidate: {shell_command(rclone_lock_delete)}")
    checks = details.get("preflight checks")
    if isinstance(checks, list) and checks:
        lines.append("preflight checks:")
        for check in checks:
            if not isinstance(check, dict):
                continue
            lines.append(
                "- "
                f"{check.get('name', '-')}: "
                f"{check.get('status', '-')} - "
                f"{check.get('detail', '-')}"
            )
    blocked = details.get("blocked operations")
    if isinstance(blocked, list) and blocked:
        lines.append("blocked operations:")
        for item in blocked:
            lines.append(f"- {item}")
    if report.issues:
        lines.append("warnings:")
        for issue in report.issues:
            if issue.level == "warning":
                lines.append(f"- {issue.key}: {issue.message}")
    return "\n".join(lines)


def _sync_migration_run_state_file(config: AppConfig) -> Path:
    return config.state_dir / "sync" / "migration-last-run.json"


def _sync_migration_gate_spec():
    return GATES["sync.migration"]


def _sync_migration_gate_open(config: AppConfig) -> bool:
    spec = _sync_migration_gate_spec()
    return config.sync_migration_gate == spec.expected_value


def _render_migration_run_human(report: CommandReport) -> str:
    details = report.details
    lines = [
        f"{report.command}: {report.status}",
        report.summary,
        f"migration run gate: {details.get('migration run gate status', '-')}",
        f"sync/resync can run: {details.get('sync/resync can run', '-')}",
        f"mode: {details.get('mode', '-')}",
        f"execute requested: {details.get('execute requested', '-')}",
        f"state writes: {details.get('state writes', '-')}",
        f"sync state: {details.get('sync state', '-')}",
        f"last result: {details.get('last result', '-')}",
        f"target root: {details.get('migration target root status', '-')} ({details.get('configured core dir', '-')})",
        f"sync lock: {details.get('sync lock status', '-')}",
        (
            "rclone bisync lock: "
            f"{details.get('rclone bisync lock status', '-')} "
            f"(pid={details.get('rclone bisync lock pid', '-')}; "
            f"process_active={details.get('rclone bisync lock process active', '-')})"
        ),
        f"rclone: {details.get('rclone availability', '-')} ({details.get('rclone binary', '-')})",
        f"approval status: {details.get('migration approval status', '-')}",
    ]
    command = details.get("planned sync command")
    if command:
        lines.append(f"planned sync command: {shell_command(command)}")
    state_file = details.get("migration run state file")
    if state_file:
        lines.append(f"migration run state: {state_file}")
    rclone_lock_delete = details.get("rclone bisync lock delete command")
    if details.get("rclone bisync lock status") == "present" and rclone_lock_delete:
        lines.append(f"rclone lock cleanup candidate: {shell_command(rclone_lock_delete)}")
    checks = details.get("preflight checks")
    blocked_checks: list[str] = []
    if isinstance(checks, list):
        for check in checks:
            if isinstance(check, dict) and check.get("status") != "ok":
                blocked_checks.append(
                    f"{check.get('name', '-')}: {check.get('status', '-')} - {check.get('detail', '-')}"
                )
    if blocked_checks:
        lines.append("blocked checks:")
        for check in blocked_checks:
            lines.append(f"- {check}")
    if report.issues:
        lines.append("warnings:" if report.status != "error" else "issues:")
        for issue in report.issues:
            lines.append(f"- {issue.key}: {issue.message}")
    return "\n".join(lines)


def _saved_sync_status_report(
    path: Path | None,
) -> tuple[dict[str, object] | None, dict[str, str] | None, list[ConfigIssue]]:
    if path is None:
        return None, None, []
    if not path.exists():
        return (
            None,
            {
                "name": "saved sync status report",
                "status": "pending",
                "detail": f"sync status report not found: {path}",
            },
            [
                ConfigIssue(
                    key="PCLOUD_TOOLS_SYNC_STATUS_REPORT",
                    level="warning",
                    message=f"sync status report not found: {path}",
                )
            ],
        )
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        return (
            None,
            {
                "name": "saved sync status report",
                "status": "pending",
                "detail": f"invalid JSON: {exc}",
            },
            [
                ConfigIssue(
                    key="PCLOUD_TOOLS_SYNC_STATUS_REPORT",
                    level="warning",
                    message=f"invalid sync status report JSON: {path}: {exc}",
                )
            ],
        )
    details = payload.get("details")
    if payload.get("command") != "sync status" or not isinstance(details, dict):
        return (
            None,
            {
                "name": "saved sync status report",
                "status": "pending",
                "detail": f"not a sync status report: {path}",
            },
            [
                ConfigIssue(
                    key="PCLOUD_TOOLS_SYNC_STATUS_REPORT",
                    level="warning",
                    message=f"saved report is not a sync status report: {path}",
                )
            ],
        )
    return (
        dict(details),
        {
            "name": "saved sync status report",
            "status": "ok",
            "detail": str(path),
        },
        [],
    )


def _sync_migration_gate_report(args: argparse.Namespace, paths: RuntimePaths) -> CommandReport:
    load_result = load_config(paths)
    config = load_result.config
    migration_gate = validate_gate(
        _sync_migration_gate_spec(),
        args,
        {_sync_migration_gate_spec().env_var: config.sync_migration_gate},
    )
    sync_state = read_sync_state(config)
    lock_state = read_sync_lock_state(config)
    rclone_lock = _rclone_bisync_lock_info(config)
    scope = sync_allowlist_info(config)
    autosync = read_autosync_state(config)
    listing_recovery = bisync_listing_recovery_state(config)
    baseline_label = _readable_baseline(scope.baseline.file, scope.baseline.mode, scope.baseline.status)
    issues = list(load_result.issues) + scope_issues(scope)
    shadow_check, shadow_issues = _saved_shadow_report_check(
        getattr(args, "report_path", None),
        issue_key="PCLOUD_TOOLS_SYNC_MIGRATION_SHADOW_REPORT",
    )
    issues.extend(shadow_issues)
    saved_status, saved_status_check, saved_status_issues = _saved_sync_status_report(
        getattr(args, "sync_status_report_path", None)
    )
    issues.extend(saved_status_issues)
    rclone_bin = _command_path("rclone")
    rclone_check = {
        "name": "rclone binary",
        "status": "ok" if rclone_bin else "pending",
        "detail": rclone_bin or "rclone not found by command -v; verify before sync/resync validation",
    }
    if not rclone_bin:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_SYNC_MIGRATION_RCLONE",
                level="warning",
                message="rclone was not found by command -v; migration gate remains closed",
            )
        )
    status_source = "saved sync status report" if saved_status is not None else "runtime"
    sync_state_value = str(saved_status.get("sync state", sync_state.state)) if saved_status else sync_state.state
    last_result = str(saved_status.get("last result", sync_state.last_sync)) if saved_status else sync_state.last_sync
    last_error = str(saved_status.get("last error", sync_state.last_error)) if saved_status else sync_state.last_error
    last_error_status = (
        str(saved_status.get("last error status", sync_last_error_status(sync_state)))
        if saved_status
        else sync_last_error_status(sync_state)
    )
    sync_lock_status = (
        str(saved_status.get("sync lock status", lock_state.status)) if saved_status else lock_state.status
    )
    sync_lock_active = (
        str(saved_status.get("sync lock active", "yes" if lock_state.active else "no"))
        if saved_status
        else "yes" if lock_state.active else "no"
    )
    sync_lock_pid = str(saved_status.get("sync lock pid", lock_state.pid)) if saved_status else lock_state.pid
    sync_lock_mode = str(saved_status.get("sync lock mode", lock_state.mode)) if saved_status else lock_state.mode
    sync_lock_started = (
        str(saved_status.get("sync lock started", lock_state.started_at)) if saved_status else lock_state.started_at
    )
    scope_status = str(saved_status.get("scope status", scope.allowlist_status)) if saved_status else scope.allowlist_status
    scope_entries = saved_status.get("scope entries", scope.allowlist_count) if saved_status else scope.allowlist_count
    scope_baseline = (
        str(saved_status.get("last resync scope", baseline_label)) if saved_status else baseline_label
    )
    allowlist_path = str(saved_status.get("allowlist", scope.allowlist_file)) if saved_status else str(scope.allowlist_file)
    saved_allowlist_root = _allowlist_root(allowlist_path)
    configured_core_dir = str(config.core_dir)
    target_root_ok = saved_allowlist_root in {"-", configured_core_dir}
    target_root_check = {
        "name": "migration target root",
        "status": "ok" if target_root_ok else "pending",
        "detail": f"configured_core_dir={configured_core_dir}; saved_allowlist_root={saved_allowlist_root}; source={status_source}",
    }
    if not target_root_ok:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_SYNC_MIGRATION_TARGET_ROOT",
                level="warning",
                message=(
                    "saved sync status appears to describe a different core root than this command would sync: "
                    f"{saved_allowlist_root} != {configured_core_dir}"
                ),
            )
        )
    autosync_state = str(saved_status.get("autosync state", autosync.state)) if saved_status else autosync.state
    autosync_runs = str(saved_status.get("autosync runs", autosync.runs)) if saved_status else autosync.runs
    sync_ok = sync_state_value == "synced" and "SUCCESS" in last_result
    sync_state_check = {
        "name": "latest sync status",
        "status": "ok" if sync_ok else "pending",
        "detail": f"{sync_state_value}; last={last_result}; last_error_status={last_error_status}; source={status_source}",
    }
    lock_ok = sync_lock_status == "missing" and sync_lock_active != "yes"
    lock_check = {
        "name": "sync lock",
        "status": "ok" if lock_ok else "pending",
        "detail": f"{sync_lock_status}; active={sync_lock_active}; pid={sync_lock_pid}; source={status_source}",
    }
    rclone_lock_present = rclone_lock["status"] == "present"
    rclone_lock_check = {
        "name": "rclone bisync lock",
        "status": "pending" if rclone_lock_present else "ok",
        "detail": (
            f"{rclone_lock['status']}; pid={rclone_lock['pid']}; "
            f"process_active={rclone_lock['process active']}; path={rclone_lock['path']}"
        ),
    }
    if rclone_lock_present:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_SYNC_MIGRATION_RCLONE_LOCK",
                level="warning",
                message=(
                    "rclone bisync prior lock exists; inspect it before running migration validation: "
                    f"{rclone_lock['path']}"
                ),
            )
        )
    scope_ok = scope_status == "loaded" and (saved_status is not None or scope.baseline.status != "invalid")
    scope_check = {
        "name": "document/media scope",
        "status": "ok" if scope_ok else "pending",
        "detail": f"{scope_baseline}; entries={scope_entries}; allowlist={allowlist_path}; source={status_source}",
    }
    entrypoint = entrypoint_command(paths)
    status_command = [entrypoint, "sync", "status", "--json"]
    normal_preview = [entrypoint, "sync", "--json"]
    resync_preview = [entrypoint, "sync", "resync", "--resync-mode", DEFAULT_RESYNC_MODE, "--json"]
    checks = [
        shadow_check,
        rclone_check,
    ]
    if saved_status_check is not None:
        checks.append(saved_status_check)
    checks.extend([
        sync_state_check,
        lock_check,
        rclone_lock_check,
        target_root_check,
        scope_check,
        {
            "name": "sync preview commands",
            "status": "ok",
            "detail": f"{' '.join(normal_preview)}; {' '.join(resync_preview)}",
        },
        {
            "name": "operator status review",
            "status": "ok" if migration_gate.flag_ok("--operator-reviewed-status") else "pending",
            "detail": "operator reviewed sync status, latest result, lock state, and preview commands",
        },
        {
            "name": "scope approval",
            "status": "ok" if migration_gate.flag_ok("--reviewer-approved-scope") else "pending",
            "detail": "reviewer approved document/media allowlist scope before migration validation",
        },
        {
            "name": "rollback policy approval",
            "status": "ok" if migration_gate.flag_ok("--reviewer-approved-rollback-policy") else "pending",
            "detail": "reviewer approved listing/filter rollback backups and restore commands before validation",
        },
        {
            "name": "stop conditions approval",
            "status": "ok" if migration_gate.flag_ok("--reviewer-approved-stop-conditions") else "pending",
            "detail": "reviewer approved stop conditions for sync errors, locks, broad scope, or unexpected transfer plan",
        },
        {
            "name": "parallel dangerous gates",
            "status": "ok",
            "detail": "fswatch resident start, pCloud API long-poll start, autosync launchd changes, and archive work stay out of scope",
        },
    ])
    approval_status = "complete-read-only" if all(check["status"] == "ok" for check in checks) else "pending"
    issues.append(
        ConfigIssue(
            key=migration_gate.spec.env_var,
            level="warning",
            message="normal sync/resync migration validation remains gated; this command is read-only",
        )
    )
    issues = sort_issues(issues)
    details: dict[str, object] = {
        "planned action": "check normal sync/resync migration validation prerequisites",
        "implementation status": "read-only checklist; sync/resync is not executed",
        "migration gate status": "closed",
        "sync/resync can run": "no",
        "operator verification required": "yes-before-sync-migration-validation",
        "human gate status": "required-before-sync-migration-validation",
        "human gate reason": "normal sync/resync validation would run live rclone bisync against the configured remote",
        "state writes": "none",
        "core remote": config.core_remote,
        "configured core dir": configured_core_dir,
        "saved allowlist root": saved_allowlist_root,
        "migration target root status": "ok" if target_root_ok else "mismatch",
        "sync status source": status_source,
        "sync status report": str(getattr(args, "sync_status_report_path", None) or "-"),
        "sync state": sync_state_value,
        "last result": last_result,
        "last error": last_error,
        "last error status": last_error_status,
        "sync lock status": sync_lock_status,
        "sync lock active": sync_lock_active,
        "sync lock pid": sync_lock_pid,
        "sync lock mode": sync_lock_mode,
        "sync lock started": sync_lock_started,
        "rclone bisync lock status": rclone_lock["status"],
        "rclone bisync lock path": rclone_lock["path"],
        "rclone bisync lock pid": rclone_lock["pid"],
        "rclone bisync lock process active": rclone_lock["process active"],
        "rclone bisync lock time renewed": rclone_lock["time renewed"],
        "rclone bisync lock time expires": rclone_lock["time expires"],
        "rclone bisync lock delete command": rclone_lock["delete command"],
        "autosync state": autosync_state,
        "autosync runs": autosync_runs,
        "scope status": scope_status,
        "scope baseline": scope_baseline,
        "scope entries": scope_entries,
        "allowlist": allowlist_path,
        "rclone availability": "available" if rclone_bin else "missing",
        "rclone binary": rclone_bin or "-",
        "listing recovery available": "yes" if listing_recovery.can_recover else "no",
        "path1 list": str(listing_recovery.path1_lst),
        "path2 list": str(listing_recovery.path2_lst),
        "path1 err": str(listing_recovery.path1_err),
        "path2 err": str(listing_recovery.path2_err),
        "status command": status_command,
        "normal sync preview command": normal_preview,
        "resync preview command": resync_preview,
        "migration approval status": approval_status,
        "preflight checks": checks,
        "success policy": "run only the explicitly approved validation command and re-check sync status/logs afterward",
        "failure policy": "stop on sync error, active/stale lock, broad-scope warning, or unexpected transfer plan",
        "rollback policy": "use saved listing/filter rollback backups; do not delete listing cache automatically",
        "blocked operations": [
            "normal sync execution",
            "resync execution",
            "full-resync execution",
            "track-renames execution",
            "listing cache deletion or movement",
            "autosync launchd changes",
        ],
        "next human check trigger": "explicit normal sync/resync validation command or listing rollback decision",
    }
    return CommandReport(
        command="sync migration-gate",
        status=status_from_issues(issues),
        summary="sync migration validation gate is closed",
        details=details,
        issues=report_issues(issues),
        actions=_sync_status_actions(paths, lock_state.status),
    )


def cmd_sync_migration_gate(args: argparse.Namespace, paths: RuntimePaths) -> int:
    report = _sync_migration_gate_report(args, paths)
    output_format = "xbar" if getattr(args, "xbar", False) else "json" if getattr(args, "json", False) else "human"
    if output_format == "human":
        print(_render_migration_gate_human(report))
    else:
        print(render_report(report, output_format=output_format))
    return exit_code_for_report(report)


def _sync_migration_run_report(args: argparse.Namespace, paths: RuntimePaths) -> CommandReport:
    gate_report = _sync_migration_gate_report(args, paths)
    load_result = load_config(paths)
    config = load_result.config
    mode = str(getattr(args, "mode", "normal"))
    plan_mode = "normal" if mode == "normal" else "resync"
    execute = bool(getattr(args, "execute", False))
    details = dict(gate_report.details)
    issues = [
        ConfigIssue(key=issue.key, level=issue.level, message=issue.message)
        for issue in gate_report.issues
        if issue.key != "PCLOUD_TOOLS_SYNC_MIGRATION_GATE"
    ]
    approval_status = str(details.get("migration approval status", "pending"))
    migration_spec = _sync_migration_gate_spec()
    gate_open = _sync_migration_gate_open(config)
    state_file = _sync_migration_run_state_file(config)
    rclone_bin = _command_path("rclone")
    plan = None

    details.update(
        {
            "planned action": "run sync migration validation" if execute else "preview sync migration run",
            "implementation status": (
                "guarded rclone bisync validation path"
                if execute
                else "sync migration run preview only; rclone bisync is not executed"
            ),
            "mode": mode,
            "migration run gate status": (
                f"open: {migration_spec.expected_value}"
                if gate_open
                else f"closed: requires {migration_spec.env_var}={migration_spec.expected_value}"
            ),
            "migration gate status": (
                f"open: {migration_spec.expected_value}"
                if gate_open
                else f"closed: requires {migration_spec.env_var}={migration_spec.expected_value}"
            ),
            "sync/resync can run": "yes" if gate_open and approval_status == "complete-read-only" else "no",
            "execute requested": "yes" if execute else "no",
            "state writes": "sync logs, lock, status, and migration run state" if execute else "none",
            "migration run state file": str(state_file),
            "future gate env": f"{migration_spec.env_var}={migration_spec.expected_value}",
        }
    )

    if approval_status != "complete-read-only":
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_SYNC_MIGRATION_APPROVAL",
                level="error" if execute else "warning",
                message="sync migration execution requires complete read-only approvals",
            )
        )
    if not gate_open:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_SYNC_MIGRATION_EXECUTION_GATE",
                level="error" if execute else "warning",
                message=(
                    "sync migration execution requires "
                    f"{migration_spec.env_var}={migration_spec.expected_value!r}"
                ),
            )
        )
    if not rclone_bin:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_SYNC_MIGRATION_RCLONE",
                level="error" if execute else "warning",
                message="rclone command not found",
            )
        )

    if rclone_bin and gate_open and approval_status == "complete-read-only":
        try:
            scope_info = sync_allowlist_info(config)
            plan = build_sync_plan(
                config,
                plan_mode,
                scope_info.entries,
                rclone_bin=rclone_bin,
                resync_mode=getattr(args, "resync_mode", DEFAULT_RESYNC_MODE),
            )
            details.update(
                {
                    "scope mode": plan.scope_mode,
                    "planned sync command": list(plan.command),
                    "rclone log": str(plan.rclone_log),
                    "stdout log": str(plan.stdout_log),
                    "stderr log": str(plan.stderr_log),
                    "filter file": str(plan.filter_file) if plan.filter_file is not None else "-",
                    "resync mode": plan.resync_mode or "-",
                }
            )
        except SyncExecutionError as exc:
            issues.append(
                ConfigIssue(
                    key="PCLOUD_TOOLS_SYNC_MIGRATION_PLAN",
                    level="error",
                    message=str(exc),
                )
            )

    if not execute or has_errors(issues):
        if has_errors(issues):
            details["state writes"] = "none"
        issues = sort_issues(issues)
        return CommandReport(
            command="sync migration-run",
            status=status_from_issues(issues),
            summary=(
                "sync migration execution is gated"
                if has_errors(issues) or not gate_open or approval_status != "complete-read-only"
                else "sync migration run is ready"
            ),
            details=details,
            issues=report_issues(issues),
            actions=_sync_status_actions(paths, "missing"),
        )

    assert plan is not None
    started_at = datetime.now(timezone.utc).isoformat()
    result = None
    try:
        result = execute_sync_plan(config, plan)
    except SyncExecutionError as exc:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_SYNC_MIGRATION_EXEC",
                level="error",
                message=str(exc),
            )
        )
    finished_at = datetime.now(timezone.utc).isoformat()
    exit_code = result.exit_code if result is not None else 1
    run_state = {
        "mode": mode,
        "started_at": started_at,
        "finished_at": finished_at,
        "command": list(plan.command),
        "exit_code": exit_code,
        "scope_recorded": result.scope_recorded if result is not None else False,
        "listings_recovered": result.listings_recovered if result is not None else False,
        "rclone_log": str(plan.rclone_log),
        "stdout_log": str(plan.stdout_log),
        "stderr_log": str(plan.stderr_log),
    }
    if result is not None and result.exit_code != 0:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_SYNC_MIGRATION_EXIT",
                level="error",
                message=f"sync migration validation failed with exit={result.exit_code}",
            )
        )
    if not has_errors(issues):
        state_file.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(state_file, run_state, sort_keys=True)

    details.update(
        {
            "exit code": exit_code,
            "scope recorded": "yes" if run_state["scope_recorded"] else "no",
            "listings recovered": "yes" if run_state["listings_recovered"] else "no",
            "process result": run_state,
            "state writes": "sync logs, lock, status, and migration run state" if not has_errors(issues) else "none",
        }
    )
    issues = sort_issues(issues)
    return CommandReport(
        command="sync migration-run",
        status=status_from_issues(issues),
        summary="sync migration run completed" if not has_errors(issues) else "sync migration run failed",
        details=details,
        issues=report_issues(issues),
        actions=_sync_status_actions(paths, "missing"),
    )


def cmd_sync_migration_run(args: argparse.Namespace, paths: RuntimePaths) -> int:
    report = _sync_migration_run_report(args, paths)
    output_format = "xbar" if getattr(args, "xbar", False) else "json" if getattr(args, "json", False) else "human"
    if output_format == "human":
        print(_render_migration_run_human(report))
    else:
        print(render_report(report, output_format=output_format))
    return exit_code_for_report(report)


def cmd_sync_autosync(args: argparse.Namespace, paths: RuntimePaths, action: str) -> int:
    report = _sync_autosync_report(args, paths, action)
    print(render_report(report, as_json=args.json))
    return exit_code_for_report(report)
