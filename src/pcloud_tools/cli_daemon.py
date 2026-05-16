from __future__ import annotations

import argparse

from .cli_common import (
    action_command,
    entrypoint_command,
    exit_code_for_report,
    has_errors,
    has_warnings,
    issue_sort_key,
    output_format,
    print_report,
    report_issues,
    sort_issues,
    status_from_issues,
)
from .config import ConfigIssue, load_config
from .daemon_state import (
    add_pending_download,
    clear_pending_downloads,
    normalize_diffid,
    read_daemon_state,
    record_notification,
    set_auto_download,
    write_diffid,
)
from .output import CommandReport, ReportAction
from .runtime import RuntimePaths


def add_daemon_parser(subparsers: argparse._SubParsersAction) -> None:
    daemon_parser = subparsers.add_parser("daemon", help="Inspect and update daemon state.")
    daemon_subparsers = daemon_parser.add_subparsers(dest="daemon_command")
    daemon_status_parser = daemon_subparsers.add_parser("status")
    daemon_status_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    daemon_status_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")

    daemon_set_diffid_parser = daemon_subparsers.add_parser("set-diffid")
    daemon_set_diffid_parser.add_argument("diffid")
    daemon_set_diffid_parser.add_argument(
        "--execute",
        action="store_true",
        help="Write the diffid instead of only previewing it.",
    )
    daemon_set_diffid_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")

    daemon_auto_download_parser = daemon_subparsers.add_parser("auto-download")
    daemon_auto_download_parser.add_argument("state", choices=("on", "off"))
    daemon_auto_download_parser.add_argument(
        "--execute",
        action="store_true",
        help="Write the auto-download state instead of only previewing it.",
    )
    daemon_auto_download_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")

    daemon_pending_parser = daemon_subparsers.add_parser("pending-download")
    daemon_pending_subparsers = daemon_pending_parser.add_subparsers(dest="pending_command")
    daemon_pending_add_parser = daemon_pending_subparsers.add_parser("add")
    daemon_pending_add_parser.add_argument("path")
    daemon_pending_add_parser.add_argument("--diffid", default="-")
    daemon_pending_add_parser.add_argument("--reason", default="remote-change")
    daemon_pending_add_parser.add_argument(
        "--execute",
        action="store_true",
        help="Record the pending download instead of only previewing it.",
    )
    daemon_pending_add_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    daemon_pending_clear_parser = daemon_pending_subparsers.add_parser("clear")
    daemon_pending_clear_parser.add_argument(
        "--execute",
        action="store_true",
        help="Clear pending downloads instead of only previewing the action.",
    )
    daemon_pending_clear_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")

    daemon_notification_parser = daemon_subparsers.add_parser("notification")
    daemon_notification_subparsers = daemon_notification_parser.add_subparsers(
        dest="notification_command"
    )
    daemon_notification_record_parser = daemon_notification_subparsers.add_parser("record")
    daemon_notification_record_parser.add_argument("message")
    daemon_notification_record_parser.add_argument("--level", default="info")
    daemon_notification_record_parser.add_argument(
        "--execute",
        action="store_true",
        help="Record notification state instead of only previewing it.",
    )
    daemon_notification_record_parser.add_argument(
        "--json", action="store_true", help="Emit structured JSON output."
    )


def cmd_daemon(args: argparse.Namespace, paths: RuntimePaths) -> int | None:
    if args.daemon_command == "status":
        return cmd_daemon_status(args, paths)
    if args.daemon_command == "set-diffid":
        return cmd_daemon_set_diffid(args, paths)
    if args.daemon_command == "auto-download":
        return cmd_daemon_auto_download(args, paths)
    if args.daemon_command == "pending-download":
        return cmd_daemon_pending_download(args, paths)
    if args.daemon_command == "notification":
        return cmd_daemon_notification(args, paths)
    return None


def _daemon_actions(paths: RuntimePaths, auto_download_enabled: bool) -> list[ReportAction]:
    toggle_id = (
        "daemon.auto-download.off.preview"
        if auto_download_enabled
        else "daemon.auto-download.on.preview"
    )
    toggle_label = "Preview disable auto-download" if auto_download_enabled else "Preview enable auto-download"
    return [
        ReportAction(
            id="daemon.status.refresh",
            label="Refresh daemon state",
            command=action_command(paths, "daemon.status.refresh"),
        ),
        ReportAction(
            id=toggle_id,
            label=toggle_label,
            command=action_command(paths, toggle_id),
            terminal=True,
            refresh=False,
        ),
    ]


def _daemon_state_details(state) -> dict[str, object]:
    pending_rows = [
        {
            "path": item.path,
            "diffid": item.diffid,
            "reason": item.reason,
            "recorded_at": item.recorded_at,
        }
        for item in state.pending_downloads
    ]
    return {
        "state dir": str(state.state_dir),
        "diffid": state.diffid,
        "diffid file": str(state.diffid_file),
        "auto-download": "on" if state.auto_download_enabled else "off",
        "auto-download file": str(state.auto_download_file),
        "pending downloads": len(state.pending_downloads),
        "pending downloads file": str(state.pending_downloads_file),
        "pending download records": pending_rows,
        "last notification": state.last_notification.message if state.last_notification else "-",
        "last notification level": state.last_notification.level if state.last_notification else "-",
        "last notification at": state.last_notification.recorded_at if state.last_notification else "-",
        "last notification file": str(state.notification_file),
    }


def _daemon_status_report(paths: RuntimePaths) -> CommandReport:
    load_result = load_config(paths)
    state = read_daemon_state(load_result.config)
    issues = sort_issues(list(load_result.issues) + list(state.issues))
    return CommandReport(
        command="daemon status",
        status=status_from_issues(issues),
        summary=(
            f"diffid: {state.diffid}; auto-download: "
            f"{'on' if state.auto_download_enabled else 'off'}; "
            f"pending downloads: {len(state.pending_downloads)}"
        ),
        details=_daemon_state_details(state),
        issues=report_issues(issues),
        actions=_daemon_actions(paths, state.auto_download_enabled),
    )


def cmd_daemon_status(args: argparse.Namespace, paths: RuntimePaths) -> int:
    report = _daemon_status_report(paths)
    print_report(report, args)
    return exit_code_for_report(report)


def _daemon_set_diffid_report(args: argparse.Namespace, paths: RuntimePaths) -> CommandReport:
    load_result = load_config(paths)
    issues = list(load_result.issues)
    details: dict[str, object] = {
        "requested diffid": args.diffid,
        "planned action": "write diffid" if args.execute else "preview write diffid",
    }
    try:
        normalized = normalize_diffid(args.diffid)
    except ValueError as exc:
        issues.append(ConfigIssue("PCLOUD_TOOLS_DAEMON_DIFFID", "error", str(exc)))
        normalized = args.diffid
    details["normalized diffid"] = normalized

    if has_errors(issues):
        return CommandReport(
            command="daemon set-diffid",
            status="error",
            summary="daemon diffid cannot be updated until issues are resolved",
            details=details,
            issues=report_issues(sort_issues(issues)),
        )

    if args.execute:
        written = write_diffid(load_result.config, normalized)
        details["written diffid"] = written

    return CommandReport(
        command="daemon set-diffid",
        status="ok",
        summary="daemon diffid updated" if args.execute else "daemon diffid update preview is ready",
        details=details,
        issues=report_issues(sort_issues(issues)),
    )


def cmd_daemon_set_diffid(args: argparse.Namespace, paths: RuntimePaths) -> int:
    report = _daemon_set_diffid_report(args, paths)
    print_report(report, args)
    return exit_code_for_report(report)


def _daemon_auto_download_report(args: argparse.Namespace, paths: RuntimePaths) -> CommandReport:
    load_result = load_config(paths)
    enabled = args.state == "on"
    state_before = read_daemon_state(load_result.config)
    issues = sort_issues(list(load_result.issues) + list(state_before.issues))
    details = {
        "requested auto-download": args.state,
        "planned action": "write auto-download state" if args.execute else "preview auto-download state",
        "current auto-download": "on" if state_before.auto_download_enabled else "off",
        "auto-download file": str(state_before.auto_download_file),
    }

    if has_errors(issues):
        return CommandReport(
            command="daemon auto-download",
            status="error",
            summary="daemon auto-download cannot be updated until issues are resolved",
            details=details,
            issues=report_issues(issues),
        )

    if args.execute:
        set_auto_download(load_result.config, enabled)
        state_after = read_daemon_state(load_result.config)
        issues = sort_issues(list(load_result.issues) + list(state_after.issues))
        details["current auto-download"] = "on" if state_after.auto_download_enabled else "off"

    return CommandReport(
        command="daemon auto-download",
        status=status_from_issues(issues),
        summary=(
            "daemon auto-download state updated"
            if args.execute
            else "daemon auto-download update preview is ready"
        ),
        details=details,
        issues=report_issues(issues),
    )


def cmd_daemon_auto_download(args: argparse.Namespace, paths: RuntimePaths) -> int:
    report = _daemon_auto_download_report(args, paths)
    print_report(report, args)
    return exit_code_for_report(report)


def _daemon_pending_download_report(args: argparse.Namespace, paths: RuntimePaths) -> CommandReport:
    load_result = load_config(paths)
    state_before = read_daemon_state(load_result.config)
    issues = list(load_result.issues) + list(state_before.issues)
    execute = getattr(args, "execute", False)
    details: dict[str, object] = {
        "pending downloads before": len(state_before.pending_downloads),
        "planned action": (
            "record pending download"
            if args.pending_command == "add" and execute
            else "clear pending downloads"
            if args.pending_command == "clear" and execute
            else f"preview pending-download {args.pending_command}"
        ),
    }

    if args.pending_command == "add":
        path = args.path.strip()
        if not path:
            issues.append(ConfigIssue("PCLOUD_TOOLS_DAEMON_PENDING_PATH", "error", "path is required"))
        if args.diffid != "-":
            try:
                normalize_diffid(args.diffid)
            except ValueError as exc:
                issues.append(ConfigIssue("PCLOUD_TOOLS_DAEMON_DIFFID", "error", str(exc)))
        details.update({"path": path, "diffid": args.diffid, "reason": args.reason})
        if not has_errors(issues) and execute:
            item = add_pending_download(load_result.config, path, args.diffid, args.reason)
            details["recorded at"] = item.recorded_at
    elif args.pending_command == "clear":
        if execute:
            details["cleared downloads"] = clear_pending_downloads(load_result.config)
    else:
        issues.append(
            ConfigIssue(
                "PCLOUD_TOOLS_DAEMON_PENDING_COMMAND",
                "error",
                "pending-download command must be add or clear",
            )
        )

    state_after = read_daemon_state(load_result.config)
    issues.extend(state_after.issues)
    details["pending downloads after"] = len(state_after.pending_downloads)

    return CommandReport(
        command=f"daemon pending-download {args.pending_command or ''}".strip(),
        status=status_from_issues(sort_issues(issues)),
        summary=(
            f"daemon pending-download {args.pending_command} executed"
            if execute and not has_errors(issues)
            else f"daemon pending-download {args.pending_command} preview is ready"
        ),
        details=details,
        issues=report_issues(sort_issues(issues)),
    )


def cmd_daemon_pending_download(args: argparse.Namespace, paths: RuntimePaths) -> int:
    report = _daemon_pending_download_report(args, paths)
    print_report(report, args)
    return exit_code_for_report(report)


def _daemon_notification_report(args: argparse.Namespace, paths: RuntimePaths) -> CommandReport:
    load_result = load_config(paths)
    issues = list(load_result.issues)
    execute = getattr(args, "execute", False)
    details = {
        "message": getattr(args, "message", ""),
        "level": getattr(args, "level", "info"),
        "planned action": "record notification" if execute else "preview record notification",
    }
    if args.notification_command != "record":
        issues.append(
            ConfigIssue(
                "PCLOUD_TOOLS_DAEMON_NOTIFICATION_COMMAND",
                "error",
                "notification command must be record",
            )
        )
    if not getattr(args, "message", "").strip():
        issues.append(ConfigIssue("PCLOUD_TOOLS_DAEMON_NOTIFICATION", "error", "message is required"))
    if not has_errors(issues) and execute:
        record = record_notification(load_result.config, args.message, args.level)
        details["recorded at"] = record.recorded_at

    state = read_daemon_state(load_result.config)
    issues.extend(state.issues)
    details["last notification file"] = str(state.notification_file)
    return CommandReport(
        command=f"daemon notification {args.notification_command or ''}".strip(),
        status=status_from_issues(sort_issues(issues)),
        summary=(
            "daemon notification state updated"
            if execute and not has_errors(issues)
            else "daemon notification update preview is ready"
        ),
        details=details,
        issues=report_issues(sort_issues(issues)),
    )


def cmd_daemon_notification(args: argparse.Namespace, paths: RuntimePaths) -> int:
    report = _daemon_notification_report(args, paths)
    print_report(report, args)
    return exit_code_for_report(report)
