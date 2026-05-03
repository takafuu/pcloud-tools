from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path

from .autosync_runtime import disable_autosync, enable_autosync, read_autosync_state
from .config import ConfigIssue, load_config
from .output import CommandReport, ReportAction, ReportIssue, render_report
from .runtime import RuntimePaths, action_entrypoint_command
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
    sync_autosync_gate_parser = sync_subparsers.add_parser(
        "autosync-gate", help="Read-only checklist before changing autosync launchd registration."
    )
    sync_autosync_gate_parser.add_argument("--report-path", type=Path)
    sync_autosync_gate_parser.add_argument("--operator-reviewed-preview", action="store_true")
    sync_autosync_gate_parser.add_argument("--reviewer-approved-plist", action="store_true")
    sync_autosync_gate_parser.add_argument("--reviewer-approved-launchctl-policy", action="store_true")
    sync_autosync_gate_parser.add_argument("--reviewer-approved-rollback-policy", action="store_true")
    sync_autosync_gate_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    sync_autosync_gate_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
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
    if args.sync_command == "autosync-gate":
        return cmd_sync_autosync_gate(args, paths)
    if args.sync_command == "_run":
        return cmd_sync_internal_run(args, paths)
    if args.sync_command in {"resync", "full-resync", "track-renames"}:
        return cmd_sync_execution(args, paths, args.sync_command)
    if args.sync_command == "scope":
        return cmd_sync_scope(args, paths)
    if args.sync_command == "check-allowlist":
        return cmd_sync_check_allowlist(args, paths)
    return None


def _has_errors(issues: list[ConfigIssue]) -> bool:
    return any(issue.level == "error" for issue in issues)


def _has_warnings(issues: list[ConfigIssue]) -> bool:
    return any(issue.level == "warning" for issue in issues)


def _status_from_issues(issues: list[ConfigIssue]) -> str:
    if _has_errors(issues):
        return "error"
    if _has_warnings(issues):
        return "warning"
    return "ok"


def _report_issues(issues: list[ConfigIssue]) -> list[ReportIssue]:
    return [ReportIssue(level=issue.level, key=issue.key, message=issue.message) for issue in issues]


def _issue_sort_key(issue: ConfigIssue) -> tuple[int, str]:
    priority = 0 if issue.level == "error" else 1
    return (priority, issue.key)


def _sort_issues(issues: list[ConfigIssue]) -> list[ConfigIssue]:
    return sorted(issues, key=_issue_sort_key)


def _exit_code_for_report(report: CommandReport) -> int:
    return 1 if report.status == "error" else 0


def _entrypoint_command(paths: RuntimePaths) -> str:
    return action_entrypoint_command(paths)


def _action_command(paths: RuntimePaths, action_id: str) -> tuple[str, ...]:
    return (_entrypoint_command(paths), "action", action_id)


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
            command=_action_command(paths, "sync.status.refresh"),
        ),
        ReportAction(
            id="sync.preview",
            label="Preview normal sync",
            command=_action_command(paths, "sync.preview"),
            terminal=True,
            refresh=False,
        ),
        ReportAction(
            id="sync.progress",
            label="Show sync progress",
            command=_action_command(paths, "sync.progress"),
            terminal=True,
            refresh=False,
        ),
        ReportAction(
            id="sync.scope",
            label="Show sync scope",
            command=_action_command(paths, "sync.scope"),
            terminal=True,
            refresh=False,
        ),
        ReportAction(
            id="sync.background.preview",
            label="Preview background sync",
            command=_action_command(paths, "sync.background.preview"),
            terminal=True,
            refresh=False,
        ),
        ReportAction(
            id="sync.autosync.gate",
            label="Check autosync launchd gate",
            command=_action_command(paths, "sync.autosync.gate"),
            terminal=True,
            refresh=False,
        ),
    ]
    if lock_status in {"stale", "invalid"}:
        actions.append(
            ReportAction(
                id="sync.clear-stale-lock.preview",
                label="Preview clear stale lock",
                command=_action_command(paths, "sync.clear-stale-lock.preview"),
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
    issues = _sort_issues(
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
        status=_status_from_issues(issues),
        summary="sync status is available for migration diagnostics",
        details=details,
        issues=_report_issues(issues),
        actions=_sync_status_actions(paths, lock_state.status),
    )


def cmd_sync_status(args: argparse.Namespace, paths: RuntimePaths) -> int:
    report = _sync_status_report(paths)
    print(render_report(report, output_format="xbar" if args.xbar else "json" if args.json else "human"))
    return _exit_code_for_report(report)


def _sync_progress_report(paths: RuntimePaths) -> CommandReport:
    load_result = load_config(paths)
    issues = _sort_issues(list(load_result.issues))
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
                level="warning" if not issues else "error" if _has_errors(issues) else "warning",
                message="no sync log available for progress",
            )
        ]
        return CommandReport(
            command="sync progress",
            status=_status_from_issues(_sort_issues(warning_issues)),
            summary="sync progress is not available yet",
            details=details,
            issues=_report_issues(_sort_issues(warning_issues)),
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
        status=_status_from_issues(issues),
        summary="sync progress is available",
        details=details,
        issues=_report_issues(issues),
    )


def cmd_sync_progress(args: argparse.Namespace, paths: RuntimePaths) -> int:
    report = _sync_progress_report(paths)
    print(render_report(report, as_json=args.json))
    return _exit_code_for_report(report)


def _sync_execution_report(args: argparse.Namespace, paths: RuntimePaths, mode: str) -> CommandReport:
    command_name = "sync" if mode == "normal" else f"sync {mode}"
    resync_mode = getattr(args, "resync_mode", DEFAULT_RESYNC_MODE)
    load_result = load_config(paths)
    lock_state = read_sync_lock_state(load_result.config)
    scope_info = sync_allowlist_info(load_result.config)
    issues = _sort_issues(
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

    if issues and _has_errors(issues):
        return CommandReport(
            command=command_name,
            status="error",
            summary="sync command cannot run until configuration issues are resolved",
            details=base_details,
            issues=_report_issues(issues),
        )

    if lock_state.status == "active":
        issues = _sort_issues(
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
            issues=_report_issues(issues),
        )

    if lock_state.status in {"stale", "invalid"}:
        issues = _sort_issues(
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
            issues=_report_issues(issues),
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
            issues=_report_issues(
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
            issues=_report_issues(
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
            status=_status_from_issues(issues),
            summary="sync command preview is ready",
            details=details,
            issues=_report_issues(issues),
        )

    try:
        result = execute_sync_plan(load_result.config, plan)
    except SyncExecutionError as exc:
        issues = _sort_issues(
            issues + [ConfigIssue(key="PCLOUD_TOOLS_SYNC_EXEC", level="error", message=str(exc))]
        )
        return CommandReport(
            command=command_name,
            status="error",
            summary="sync command failed before launch",
            details=details,
            issues=_report_issues(issues),
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
        issues=_report_issues(issues),
    )


def cmd_sync_execution(args: argparse.Namespace, paths: RuntimePaths, mode: str) -> int:
    report = _sync_execution_report(args, paths, mode)
    print(render_report(report, as_json=args.json))
    return _exit_code_for_report(report)


def _sync_scope_report(args: argparse.Namespace, paths: RuntimePaths) -> CommandReport:
    load_result = load_config(paths)
    config = load_result.config
    info = sync_allowlist_info(config)
    issues = _sort_issues(list(load_result.issues) + scope_issues(info))
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
        status=_status_from_issues(issues),
        summary="allowlist scope is ready" if not issues else "allowlist scope needs attention",
        details=details,
        issues=_report_issues(issues),
    )


def cmd_sync_scope(args: argparse.Namespace, paths: RuntimePaths) -> int:
    report = _sync_scope_report(args, paths)
    print(render_report(report, as_json=args.json))
    return _exit_code_for_report(report)


def _sync_check_allowlist_report(args: argparse.Namespace, paths: RuntimePaths) -> CommandReport:
    load_result = load_config(paths)
    info = sync_allowlist_info(load_result.config)
    issues = _sort_issues(list(load_result.issues) + scope_issues(info))
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
        status=_status_from_issues(issues),
        summary=summary,
        details=details,
        issues=_report_issues(issues),
    )


def cmd_sync_check_allowlist(args: argparse.Namespace, paths: RuntimePaths) -> int:
    report = _sync_check_allowlist_report(args, paths)
    print(render_report(report, as_json=args.json))
    return _exit_code_for_report(report)


def _sync_clear_stale_lock_report(args: argparse.Namespace, paths: RuntimePaths) -> CommandReport:
    load_result = load_config(paths)
    issues = _sort_issues(list(load_result.issues))
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
        issues = _sort_issues(
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
            issues=_report_issues(issues),
        )

    details["planned action"] = "remove lock directory" if lock_state.status in {"stale", "invalid"} else "no-op"

    if not args.execute:
        return CommandReport(
            command="sync clear-stale-lock",
            status=_status_from_issues(issues),
            summary="stale lock preview is ready",
            details=details,
            issues=_report_issues(issues),
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
        status=_status_from_issues(issues),
        summary="stale lock cleared" if removed else "no stale lock to clear",
        details=details,
        issues=_report_issues(issues),
    )


def cmd_sync_clear_stale_lock(args: argparse.Namespace, paths: RuntimePaths) -> int:
    report = _sync_clear_stale_lock_report(args, paths)
    print(render_report(report, as_json=args.json))
    return _exit_code_for_report(report)


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
    issues = _sort_issues(list(load_result.issues) + mode_issues + notify_issues)
    lock_state = read_sync_lock_state(config)

    details: dict[str, object] = {
        "execute": "yes" if args.execute else "no",
        "mode": mode or "-",
        "notify on finish": "yes" if notify_on_finish else "no",
        "sync lock status": lock_state.status,
        "sync lock active": "yes" if lock_state.active else "no",
        "sync lock pid": lock_state.pid,
    }

    if issues and _has_errors(issues):
        return CommandReport(
            command="sync background",
            status="error",
            summary="background sync options are invalid",
            details=details,
            issues=_report_issues(issues),
        )

    if lock_state.status == "active":
        issues = _sort_issues(
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
            issues=_report_issues(issues),
        )

    if lock_state.status in {"stale", "invalid"}:
        issues = _sort_issues(
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
            issues=_report_issues(issues),
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
            status=_status_from_issues(issues),
            summary="background sync preview is ready",
            details=details,
            issues=_report_issues(issues),
        )

    if paths.dev_mode:
        issues = _sort_issues(
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
            issues=_report_issues(issues),
        )

    try:
        launch = launch_background_sync(config, mode, notify_on_finish, child_command)
    except SyncExecutionError as exc:
        issues = _sort_issues(
            issues + [ConfigIssue(key="PCLOUD_TOOLS_SYNC_BACKGROUND", level="error", message=str(exc))]
        )
        return CommandReport(
            command="sync background",
            status="error",
            summary="background sync launch failed",
            details=details,
            issues=_report_issues(issues),
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
        status=_status_from_issues(issues),
        summary="background sync launched",
        details=details,
        issues=_report_issues(issues),
    )


def cmd_sync_background(args: argparse.Namespace, paths: RuntimePaths) -> int:
    report = _sync_background_report(args, paths)
    print(render_report(report, as_json=args.json))
    return _exit_code_for_report(report)


def cmd_sync_internal_run(args: argparse.Namespace, paths: RuntimePaths) -> int:
    load_result = load_config(paths)
    config = load_result.config
    issues = _sort_issues(
        list(load_result.issues) + list(enforce_sync_scope_guard(config, args.mode))
    )
    if issues and _has_errors(issues):
        report = CommandReport(
            command="sync _run",
            status="error",
            summary="internal sync run cannot start until configuration issues are resolved",
            details={"mode": args.mode},
            issues=_report_issues(issues),
        )
        print(render_report(report))
        return 1

    if paths.dev_mode:
        report = CommandReport(
            command="sync _run",
            status="error",
            summary="dev mode refuses internal sync execution",
            details={"mode": args.mode},
            issues=_report_issues(
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
            issues=_report_issues(
                [ConfigIssue(key="PCLOUD_TOOLS_SYNC_EXEC", level="error", message="rclone command not found")]
            ),
        )
        print(render_report(report))
        return 1

    scope_info = sync_allowlist_info(config)
    scope_validation = _sort_issues(scope_issues(scope_info))
    if scope_validation and _has_errors(scope_validation):
        report = CommandReport(
            command="sync _run",
            status="error",
            summary="internal sync run cannot start until scope issues are resolved",
            details={"mode": args.mode},
            issues=_report_issues(scope_validation),
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
            issues=_report_issues(
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
    issues = _sort_issues(list(load_result.issues))
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
        issues = _sort_issues(
            issues
            + [
                ConfigIssue(
                    key="PCLOUD_TOOLS_AUTOSYNC_PLIST",
                    level="warning" if not args.execute else "error",
                    message=f"autosync plist not found: {config.autosync_plist}",
                )
            ]
        )

    if issues and _has_errors(issues):
        return CommandReport(
            command=f"sync {action}",
            status="error",
            summary="autosync command cannot run until configuration issues are resolved",
            details=details,
            issues=_report_issues(issues),
        )

    details["planned action"] = (
        f"launchctl enable gui/<uid>/{config.autosync_label} and bootstrap {config.autosync_plist}"
        if action == "enable-autosync"
        else f"launchctl bootout/disable gui/<uid>/{config.autosync_label}"
    )

    if not args.execute:
        return CommandReport(
            command=f"sync {action}",
            status=_status_from_issues(issues),
            summary="autosync preview is ready",
            details=details,
            issues=_report_issues(issues),
        )

    if paths.dev_mode:
        issues = _sort_issues(
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
            issues=_report_issues(issues),
        )

    if action == "disable-autosync" and not args.yes:
        issues = _sort_issues(
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
            issues=_report_issues(issues),
        )

    try:
        if action == "enable-autosync":
            enable_autosync(config)
        else:
            disable_autosync(config)
    except RuntimeError as exc:
        issues = _sort_issues(
            issues + [ConfigIssue(key="PCLOUD_TOOLS_AUTOSYNC_EXEC", level="error", message=str(exc))]
        )
        return CommandReport(
            command=f"sync {action}",
            status="error",
            summary="autosync command failed",
            details=details,
            issues=_report_issues(issues),
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
        issues=_report_issues(issues),
    )


_REQUIRED_SHADOW_CHECKS = {
    "temporary workspace guard",
    "temporary state dir guard",
    "unsafe state dir guard",
}


def _saved_shadow_report_check(report_path: Path | None) -> tuple[dict[str, object], list[ConfigIssue]]:
    if report_path is None:
        return (
            {
                "name": "saved shadow validation report",
                "status": "pending",
                "detail": "pass --report-path after saving scripts/pcloud-shadow-validation.py --report-path",
            },
            [
                ConfigIssue(
                    key="PCLOUD_TOOLS_AUTOSYNC_SHADOW_REPORT",
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
                    key="PCLOUD_TOOLS_AUTOSYNC_SHADOW_REPORT",
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
                    key="PCLOUD_TOOLS_AUTOSYNC_SHADOW_REPORT",
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
                key="PCLOUD_TOOLS_AUTOSYNC_SHADOW_REPORT",
                level="warning",
                message=f"saved shadow validation report is not ok: {detail}",
            )
        ],
    )


def _shell_command(value: object) -> str:
    if isinstance(value, (list, tuple)):
        return shlex.join(str(part) for part in value)
    return str(value)


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
        f"launchctl: {details.get('launchctl availability', '-')} ({details.get('launchctl binary', '-')})",
        f"approval status: {details.get('autosync approval status', '-')}",
        f"human gate: {details.get('human gate status', '-')}",
        f"next human check trigger: {details.get('next human check trigger', '-')}",
    ]
    commands = [
        ("enable preview", details.get("enable preview command")),
        ("disable preview", details.get("disable preview command")),
    ]
    for label, command in commands:
        if command:
            lines.append(f"{label}: {_shell_command(command)}")
    launchctl_commands = [
        ("enable launchctl commands", details.get("enable launchctl commands")),
        ("disable launchctl commands", details.get("disable launchctl commands")),
    ]
    for label, command_group in launchctl_commands:
        if not isinstance(command_group, list) or not command_group:
            continue
        lines.append(f"{label}:")
        for command in command_group:
            lines.append(f"- {_shell_command(command)}")
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


def _sync_autosync_gate_report(args: argparse.Namespace, paths: RuntimePaths) -> CommandReport:
    load_result = load_config(paths)
    config = load_result.config
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

    entrypoint = _entrypoint_command(paths)
    enable_preview = [entrypoint, "sync", "enable-autosync", "--json"]
    disable_preview = [entrypoint, "sync", "disable-autosync", "--yes", "--json"]
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
            "status": "ok" if getattr(args, "operator_reviewed_preview", False) else "pending",
            "detail": "operator reviewed enable/disable autosync preview output and launchd label",
        },
        {
            "name": "plist approval",
            "status": "ok" if getattr(args, "reviewer_approved_plist", False) else "pending",
            "detail": "reviewer approved plist path, label, and public entrypoint target",
        },
        {
            "name": "launchctl policy approval",
            "status": "ok" if getattr(args, "reviewer_approved_launchctl_policy", False) else "pending",
            "detail": "reviewer approved bootstrap/bootout/enable/disable behavior before launchd changes",
        },
        {
            "name": "rollback policy approval",
            "status": "ok" if getattr(args, "reviewer_approved_rollback_policy", False) else "pending",
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
            key="PCLOUD_TOOLS_AUTOSYNC_LAUNCHD_GATE",
            level="warning",
            message="autosync launchd changes remain gated; this command is read-only",
        )
    )
    issues = _sort_issues(issues)
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
        status=_status_from_issues(issues),
        summary="autosync launchd gate is closed",
        details=details,
        issues=_report_issues(issues),
        actions=_sync_status_actions(paths, "missing"),
    )


def cmd_sync_autosync_gate(args: argparse.Namespace, paths: RuntimePaths) -> int:
    report = _sync_autosync_gate_report(args, paths)
    output_format = "xbar" if getattr(args, "xbar", False) else "json" if getattr(args, "json", False) else "human"
    if output_format == "human":
        print(_render_autosync_gate_human(report))
    else:
        print(render_report(report, output_format=output_format))
    return _exit_code_for_report(report)


def cmd_sync_autosync(args: argparse.Namespace, paths: RuntimePaths, action: str) -> int:
    report = _sync_autosync_report(args, paths, action)
    print(render_report(report, as_json=args.json))
    return _exit_code_for_report(report)
