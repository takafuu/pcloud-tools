from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import AppConfig, ConfigIssue, load_config
from .daemon_state import DaemonState, read_daemon_state
from .diffd_events import diff_changes_to_records, parse_diff_response_fixture
from .output import CommandReport, ReportAction, ReportIssue, render_report
from .pushd_events import fswatch_events_to_records, parse_fswatch_fixture
from .runtime import RuntimePaths, action_entrypoint_command, detect_runtime_paths
from .service_daemon_plan import (
    DiffdPlan,
    PlanRecord,
    PushdPlan,
    append_plan_record,
    build_diffd_plan,
    build_diffd_plan_from_records,
    build_pushd_plan,
    build_pushd_plan_from_records,
    clear_plan_records,
    normalize_plan_path,
    record_dry_run_state,
    record_payloads,
    remove_plan_records,
)
from .service_daemon_state import ServiceDaemonState, read_service_daemon_state
from .sync_scope import ScopeBaseline, SyncScopeInfo, scope_issues, sync_allowlist_info


@dataclass(frozen=True)
class ServiceDefinition:
    name: str
    summary_name: str
    status_help: str
    preview_help: str


_SERVICES = {
    "pushd": ServiceDefinition(
        name="pushd",
        summary_name="local push daemon",
        status_help="Inspect pcloud-pushd scaffold state.",
        preview_help="Preview the pcloud-pushd scaffold plan.",
    ),
    "diffd": ServiceDefinition(
        name="diffd",
        summary_name="remote diff daemon",
        status_help="Inspect pcloud-diffd scaffold state.",
        preview_help="Preview the pcloud-diffd scaffold plan.",
    ),
}

_SIMPLE_TRANSFER_ACTIONS = {
    "change",
    "create",
    "created",
    "download",
    "modify",
    "modified",
    "sync",
    "update",
    "updated",
    "upload",
}
_MANUAL_REVIEW_ACTION_TOKENS = ("delete", "remove", "rename", "move")
_CONSUME_POLICIES = (
    "remove-on-success-retain-on-failure",
    "retain-all",
    "manual-review",
)
_TIMEOUT_POLICIES = (
    "reuse-fake-rclone-cleanup",
    "manual-review",
)


def add_service_daemon_parsers(subparsers: argparse._SubParsersAction) -> None:
    for service in _SERVICES.values():
        _add_service_parser(subparsers, service)


def _add_service_parser(
    subparsers: argparse._SubParsersAction, service: ServiceDefinition
) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(service.name, help=f"Inspect {service.summary_name} scaffold.")
    parser.set_defaults(service_name=service.name)
    service_subparsers = parser.add_subparsers(dest="service_command")

    status_parser = service_subparsers.add_parser("status", help=service.status_help)
    status_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    status_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")

    preview_parser = service_subparsers.add_parser("preview", help=service.preview_help)
    preview_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    preview_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")

    run_parser = service_subparsers.add_parser("run", help=f"Preview a {service.name} one-shot dry run.")
    run_parser.add_argument(
        "--execute",
        action="store_true",
        help="Record the dry-run result under the dev state dir instead of only previewing it.",
    )
    run_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    run_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")

    gate_parser = service_subparsers.add_parser(
        "gate", help=f"Check the read-only gate before real {service.name} implementation work."
    )
    gate_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    gate_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")

    if service.name == "pushd":
        fswatch_parser = service_subparsers.add_parser(
            "fswatch", help="Preview pushd fswatch fixture events without starting fswatch."
        )
        fswatch_subparsers = fswatch_parser.add_subparsers(dest="fswatch_command")
        fswatch_preview_parser = fswatch_subparsers.add_parser(
            "preview", help="Parse a fixture and preview the resulting upload plan."
        )
        fswatch_preview_parser.add_argument("--fixture", required=True, type=Path)
        fswatch_preview_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        fswatch_preview_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
        fswatch_probe_parser = fswatch_subparsers.add_parser(
            "probe", help="Preview the one-shot fswatch probe command without running it."
        )
        fswatch_probe_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        fswatch_probe_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")

        transfer_parser = service_subparsers.add_parser(
            "transfer", help="Preview upload executor commands without running transfers."
        )
        transfer_subparsers = transfer_parser.add_subparsers(dest="transfer_command")
        transfer_preview_parser = transfer_subparsers.add_parser(
            "preview", help="Emit planned upload commands from the current pushd plan."
        )
        transfer_preview_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        transfer_preview_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
        transfer_check_parser = transfer_subparsers.add_parser(
            "check", help="Read-only checklist for the real upload transfer gate."
        )
        transfer_check_parser.add_argument(
            "--report-path",
            type=Path,
            help="Optional saved shadow validation report to inspect without running validation.",
        )
        transfer_check_parser.add_argument(
            "--sample-path",
            help="Relative allowlisted path to use in the displayed dev-state sample setup command.",
        )
        transfer_check_parser.add_argument(
            "--confirm-path",
            help="Operator-confirmed relative path for the first real upload review.",
        )
        transfer_check_parser.add_argument(
            "--confirm-direction",
            choices=("upload", "download"),
            help="Operator-confirmed direction for the first real transfer review.",
        )
        transfer_check_parser.add_argument(
            "--consume-policy",
            choices=_CONSUME_POLICIES,
            help="Reviewer-approved record consumption policy for the first real transfer review.",
        )
        transfer_check_parser.add_argument(
            "--timeout-policy",
            choices=_TIMEOUT_POLICIES,
            help="Reviewer-approved timeout/process cleanup policy for the first real transfer review.",
        )
        transfer_check_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        transfer_check_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
        transfer_run_parser = transfer_subparsers.add_parser(
            "run", help="Run dev-mode fake-rclone upload executor commands behind the transfer gate."
        )
        transfer_run_parser.add_argument(
            "--execute",
            action="store_true",
            help="Execute only when the dev fake-rclone transfer gate and dev state guard are open.",
        )
        transfer_run_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        transfer_run_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
        transfer_consume_parser = transfer_subparsers.add_parser(
            "consume", help="Preview queue consumption after successful transfer records."
        )
        transfer_consume_subparsers = transfer_consume_parser.add_subparsers(dest="consume_command")
        transfer_consume_preview_parser = transfer_consume_subparsers.add_parser(
            "preview", help="Read-only preview of queue records that would be removed."
        )
        transfer_consume_preview_parser.add_argument(
            "--json", action="store_true", help="Emit structured JSON output."
        )
        transfer_consume_preview_parser.add_argument(
            "--xbar", action="store_true", help="Emit xbar menu output."
        )
        transfer_consume_run_parser = transfer_consume_subparsers.add_parser(
            "run", help="Remove matched queue records only when --execute is provided."
        )
        transfer_consume_run_parser.add_argument(
            "--execute",
            action="store_true",
            help="Remove matched queue records under the dev state dir.",
        )
        transfer_consume_run_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        transfer_consume_run_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")

        queue_parser = service_subparsers.add_parser("queue", help="Preview or update pushd queue state.")
        queue_subparsers = queue_parser.add_subparsers(dest="queue_command")
        queue_add_parser = queue_subparsers.add_parser("add")
        queue_add_parser.add_argument("path")
        queue_add_parser.add_argument("--action", default="upload")
        queue_add_parser.add_argument("--reason", default="manual")
        queue_add_parser.add_argument(
            "--execute",
            action="store_true",
            help="Append the queue record under the dev state dir instead of only previewing it.",
        )
        queue_add_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        queue_clear_parser = queue_subparsers.add_parser("clear")
        queue_clear_parser.add_argument(
            "--execute",
            action="store_true",
            help="Clear the queue file under the dev state dir instead of only previewing it.",
        )
        queue_clear_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        queue_remove_parser = queue_subparsers.add_parser("remove")
        queue_remove_parser.add_argument("path")
        queue_remove_parser.add_argument(
            "--execute",
            action="store_true",
            help="Remove matching queue records under the dev state dir instead of only previewing it.",
        )
        queue_remove_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")

    if service.name == "diffd":
        diff_parser = service_subparsers.add_parser(
            "diff", help="Preview diffd pCloud diff fixture responses without calling the API."
        )
        diff_subparsers = diff_parser.add_subparsers(dest="diff_command")
        diff_preview_parser = diff_subparsers.add_parser(
            "preview", help="Parse a fixture and preview the resulting download plan."
        )
        diff_preview_parser.add_argument("--fixture", required=True, type=Path)
        diff_preview_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        diff_preview_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")

        api_poll_parser = service_subparsers.add_parser(
            "api-poll", help="Preview a one-shot pCloud API poll without calling the API."
        )
        api_poll_subparsers = api_poll_parser.add_subparsers(dest="api_poll_command")
        api_poll_preview_parser = api_poll_subparsers.add_parser(
            "preview", help="Report the intended one-shot API poll request shape."
        )
        api_poll_preview_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        api_poll_preview_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")

        transfer_parser = service_subparsers.add_parser(
            "transfer", help="Preview download executor commands without running transfers."
        )
        transfer_subparsers = transfer_parser.add_subparsers(dest="transfer_command")
        transfer_preview_parser = transfer_subparsers.add_parser(
            "preview", help="Emit planned download commands from the current diffd plan."
        )
        transfer_preview_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        transfer_preview_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
        transfer_check_parser = transfer_subparsers.add_parser(
            "check", help="Read-only checklist for the real download transfer gate."
        )
        transfer_check_parser.add_argument(
            "--report-path",
            type=Path,
            help="Optional saved shadow validation report to inspect without running validation.",
        )
        transfer_check_parser.add_argument(
            "--sample-path",
            help="Relative allowlisted path to use in the displayed dev-state sample setup command.",
        )
        transfer_check_parser.add_argument(
            "--confirm-path",
            help="Operator-confirmed relative path for the first real download review.",
        )
        transfer_check_parser.add_argument(
            "--confirm-direction",
            choices=("upload", "download"),
            help="Operator-confirmed direction for the first real transfer review.",
        )
        transfer_check_parser.add_argument(
            "--consume-policy",
            choices=_CONSUME_POLICIES,
            help="Reviewer-approved record consumption policy for the first real transfer review.",
        )
        transfer_check_parser.add_argument(
            "--timeout-policy",
            choices=_TIMEOUT_POLICIES,
            help="Reviewer-approved timeout/process cleanup policy for the first real transfer review.",
        )
        transfer_check_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        transfer_check_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
        transfer_run_parser = transfer_subparsers.add_parser(
            "run", help="Run dev-mode fake-rclone download executor commands behind the transfer gate."
        )
        transfer_run_parser.add_argument(
            "--execute",
            action="store_true",
            help="Execute only when the dev fake-rclone transfer gate and dev state guard are open.",
        )
        transfer_run_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        transfer_run_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
        transfer_consume_parser = transfer_subparsers.add_parser(
            "consume", help="Preview remote-change consumption after successful transfer records."
        )
        transfer_consume_subparsers = transfer_consume_parser.add_subparsers(dest="consume_command")
        transfer_consume_preview_parser = transfer_consume_subparsers.add_parser(
            "preview", help="Read-only preview of remote-change records that would be removed."
        )
        transfer_consume_preview_parser.add_argument(
            "--json", action="store_true", help="Emit structured JSON output."
        )
        transfer_consume_preview_parser.add_argument(
            "--xbar", action="store_true", help="Emit xbar menu output."
        )
        transfer_consume_run_parser = transfer_consume_subparsers.add_parser(
            "run", help="Remove matched remote-change records only when --execute is provided."
        )
        transfer_consume_run_parser.add_argument(
            "--execute",
            action="store_true",
            help="Remove matched remote-change records under the dev state dir.",
        )
        transfer_consume_run_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        transfer_consume_run_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")

        remote_parser = service_subparsers.add_parser(
            "remote-change", help="Preview or update diffd remote change state."
        )
        remote_subparsers = remote_parser.add_subparsers(dest="remote_change_command")
        remote_add_parser = remote_subparsers.add_parser("add")
        remote_add_parser.add_argument("path")
        remote_add_parser.add_argument("--action", default="download")
        remote_add_parser.add_argument("--reason", default="manual")
        remote_add_parser.add_argument(
            "--execute",
            action="store_true",
            help="Append the remote-change record under the dev state dir instead of only previewing it.",
        )
        remote_add_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        remote_clear_parser = remote_subparsers.add_parser("clear")
        remote_clear_parser.add_argument(
            "--execute",
            action="store_true",
            help="Clear the remote-change file under the dev state dir instead of only previewing it.",
        )
        remote_clear_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        remote_remove_parser = remote_subparsers.add_parser("remove")
        remote_remove_parser.add_argument("path")
        remote_remove_parser.add_argument(
            "--execute",
            action="store_true",
            help="Remove matching remote-change records under the dev state dir instead of only previewing it.",
        )
        remote_remove_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    return parser


def cmd_service_daemon(args: argparse.Namespace, paths: RuntimePaths) -> int | None:
    service = _SERVICES[getattr(args, "service_name")]
    if args.service_command == "status":
        return cmd_service_status(args, paths, service)
    if args.service_command == "preview":
        return cmd_service_preview(args, paths, service)
    if args.service_command == "run":
        return cmd_service_run(args, paths, service)
    if args.service_command == "gate":
        return cmd_service_gate(args, paths, service)
    if service.name == "pushd" and args.service_command == "fswatch":
        return cmd_pushd_fswatch(args, paths)
    if service.name == "diffd" and args.service_command == "diff":
        return cmd_diffd_diff(args, paths)
    if service.name == "diffd" and args.service_command == "api-poll":
        return cmd_diffd_api_poll(args, paths)
    if service.name in {"pushd", "diffd"} and args.service_command == "transfer":
        return cmd_service_transfer(args, paths, service)
    if service.name == "pushd" and args.service_command == "queue":
        return cmd_pushd_queue(args, paths)
    if service.name == "diffd" and args.service_command == "remote-change":
        return cmd_diffd_remote_change(args, paths)
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


def _output_format(args: argparse.Namespace) -> str:
    if getattr(args, "xbar", False):
        return "xbar"
    return "json" if getattr(args, "json", False) else "human"


def _print_report(report: CommandReport, args: argparse.Namespace) -> None:
    print(render_report(report, output_format=_output_format(args)))


def _shell_command(value: object) -> str:
    if isinstance(value, (list, tuple)):
        return shlex.join(str(part) for part in value)
    return str(value)


def _render_transfer_check_human(report: CommandReport) -> str:
    details = report.details
    lines = [
        f"{report.command}: {report.status}",
        report.summary,
        f"gate: {details.get('real transfer gate status', '-')}",
        f"state writes: {details.get('state writes', '-')}",
        f"plan: {details.get('plan summary', '-')}",
        (
            f"sample: {details.get('sample path', '-')} "
            f"({details.get('sample path status', '-')})"
        ),
        f"sample detail: {details.get('sample path detail', '-')}",
        f"first target: {details.get('first planned transfer status', '-')}",
    ]
    if "operator target confirmation status" in details:
        lines.append(
            "target confirmation: "
            f"{details.get('operator target confirmation status', '-')}"
        )
    if "consume policy status" in details:
        lines.append(
            "consume policy: "
            f"{details.get('consume policy', '-')} "
            f"({details.get('consume policy status', '-')})"
        )
    if "timeout policy status" in details:
        lines.append(
            "timeout policy: "
            f"{details.get('timeout policy', '-')} "
            f"({details.get('timeout policy status', '-')})"
        )
    checks = details.get("preflight checks")
    if isinstance(checks, list) and checks:
        shadow_check = checks[0]
        if isinstance(shadow_check, dict):
            lines.append(f"shadow report: {shadow_check.get('status', '-')}")

    commands = [
        ("setup sample", details.get("dev-state sample setup command")),
        ("preview transfer", details.get("preview command")),
        ("check again", details.get("check command")),
        ("cleanup sample", details.get("dev-state sample cleanup command")),
    ]
    lines.append("review commands:")
    for label, command in commands:
        if command:
            lines.append(f"- {label}: {_shell_command(command)}")

    if report.issues:
        lines.append("warnings:")
        for issue in report.issues:
            if issue.level == "warning":
                lines.append(f"- {issue.key}: {issue.message}")
    return "\n".join(lines)


def _print_transfer_check_report(report: CommandReport, args: argparse.Namespace) -> None:
    if _output_format(args) == "human":
        print(_render_transfer_check_human(report))
        return
    _print_report(report, args)


def _render_transfer_preview_human(report: CommandReport) -> str:
    details = report.details
    commands = details.get("planned transfer commands")
    command_count = len(commands) if isinstance(commands, list) else 0
    lines = [
        f"{report.command}: {report.status}",
        report.summary,
        f"gate: {details.get('real transfer gate status', '-')}",
        f"state writes: {details.get('state writes', '-')}",
        f"planned transfers: {command_count}",
    ]
    count_keys = (
        "planned uploads",
        "excluded queue items",
        "invalid queue items",
        "planned downloads",
        "remote changes",
        "pending downloads",
        "skipped download records",
        "manual review transfer records",
    )
    count_parts = [f"{key}: {details[key]}" for key in count_keys if key in details]
    if count_parts:
        lines.append(f"plan: {'; '.join(count_parts)}")

    if isinstance(commands, list) and commands:
        first = commands[0]
        if isinstance(first, dict):
            lines.append(
                "first target: "
                f"{first.get('direction', '-')} {first.get('path', '-')}"
            )
            command = first.get("command")
            if command:
                lines.append(f"first command: {_shell_command(command)}")

    if report.issues:
        lines.append("warnings:")
        for issue in report.issues:
            if issue.level == "warning":
                lines.append(f"- {issue.key}: {issue.message}")
    return "\n".join(lines)


def _print_transfer_preview_report(report: CommandReport, args: argparse.Namespace) -> None:
    if _output_format(args) == "human":
        print(_render_transfer_preview_human(report))
        return
    _print_report(report, args)


def _render_transfer_consume_human(report: CommandReport) -> str:
    details = report.details
    lines = [
        f"{report.command}: {report.status}",
        report.summary,
        f"consume gate: {details.get('consume gate status', '-')}",
        f"state writes: {details.get('state writes', '-')}",
        f"successful transfers: {details.get('successful transfer results', '-')}",
        f"planned record removals: {details.get('planned record removals', '-')}",
        f"unmatched successes: {details.get('unmatched successful transfers', '-')}",
    ]
    if "records to remove" in details:
        lines.append(f"records to remove: {details.get('records to remove', '-')}")
    if "records after" in details:
        lines.append(f"records after: {details.get('records after', '-')}")
    removals = details.get("planned removal record details")
    if isinstance(removals, list) and removals:
        first = removals[0]
        if isinstance(first, dict):
            lines.append(f"first removal: {first.get('path', '-')} ({first.get('action', '-')})")
    if report.issues:
        lines.append("warnings:")
        for issue in report.issues:
            if issue.level == "warning":
                lines.append(f"- {issue.key}: {issue.message}")
    return "\n".join(lines)


def _print_transfer_consume_report(report: CommandReport, args: argparse.Namespace) -> None:
    if _output_format(args) == "human":
        print(_render_transfer_consume_human(report))
        return
    _print_report(report, args)


def _exit_code_for_report(report: CommandReport) -> int:
    return 1 if report.status == "error" else 0


def _entrypoint_command(paths: RuntimePaths) -> str:
    return action_entrypoint_command(paths)


def _action_command(paths: RuntimePaths, action_id: str) -> tuple[str, ...]:
    return (_entrypoint_command(paths), "action", action_id)


def _service_actions(paths: RuntimePaths, service: ServiceDefinition) -> list[ReportAction]:
    actions = [
        ReportAction(
            id=f"{service.name}.status.refresh",
            label=f"Refresh {service.name} state",
            command=_action_command(paths, f"{service.name}.status.refresh"),
        ),
        ReportAction(
            id=f"{service.name}.preview",
            label=f"Preview {service.name} plan",
            command=_action_command(paths, f"{service.name}.preview"),
            terminal=True,
            refresh=False,
        ),
        ReportAction(
            id=f"{service.name}.run.preview",
            label=f"Preview {service.name} dry run",
            command=_action_command(paths, f"{service.name}.run.preview"),
            terminal=True,
            refresh=False,
        ),
        ReportAction(
            id=f"{service.name}.gate",
            label=f"Check {service.name} real gate",
            command=_action_command(paths, f"{service.name}.gate"),
            terminal=True,
            refresh=False,
        ),
        ReportAction(
            id=f"{service.name}.transfer.preview",
            label=f"Preview {service.name} transfer commands",
            command=_action_command(paths, f"{service.name}.transfer.preview"),
            terminal=True,
            refresh=False,
        ),
        ReportAction(
            id=f"{service.name}.transfer.check",
            label=f"Check {service.name} transfer gate",
            command=_action_command(paths, f"{service.name}.transfer.check"),
            terminal=True,
            refresh=False,
        ),
    ]
    if service.name == "pushd":
        actions.append(
            ReportAction(
                id="pushd.queue.clear.preview",
                label="Preview clear pushd queue",
                command=_action_command(paths, "pushd.queue.clear.preview"),
                terminal=True,
                refresh=False,
            )
        )
    if service.name == "diffd":
        actions.append(
            ReportAction(
                id="diffd.remote-change.clear.preview",
                label="Preview clear diffd remote changes",
                command=_action_command(paths, "diffd.remote-change.clear.preview"),
                terminal=True,
                refresh=False,
            )
        )
    return actions


def _state_details(state: ServiceDaemonState) -> dict[str, object]:
    if state.pid is None:
        process_state = "not recorded"
    elif state.pid_running:
        process_state = "running"
    else:
        process_state = "stale"
    transfer_summary, transfer_status = _last_transfer_summary(state.last_transfer)

    return {
        "state dir": str(state.state_dir),
        "pid": state.pid if state.pid is not None else "-",
        "process state": process_state,
        "pid file": str(state.pid_file),
        "queue length": state.queue_length,
        "queue file": str(state.queue_file),
        "cursor": state.cursor,
        "cursor file": str(state.cursor_file),
        "last event": state.last_event or {},
        "last event file": str(state.last_event_file),
        "last plan": state.last_plan or {},
        "last plan file": str(state.last_plan_file),
        "last transfer": state.last_transfer or {},
        "last transfer file": str(state.last_transfer_file),
        "last transfer summary": transfer_summary,
        "last transfer status": transfer_status,
    }


def _last_transfer_summary(payload: dict[str, object] | None) -> tuple[str, str]:
    if not payload:
        return "-", "none"
    results = payload.get("results")
    if not isinstance(results, list) or not results:
        return "results: 0", "unknown"

    timed_out = 0
    failed = 0
    succeeded = 0
    for result in results:
        if not isinstance(result, dict):
            failed += 1
            continue
        if result.get("timed_out") is True:
            timed_out += 1
            continue
        if result.get("returncode") == 0:
            succeeded += 1
            continue
        failed += 1

    if timed_out:
        status = "timeout"
    elif failed:
        status = "failed"
    else:
        status = "success"
    return (
        f"success: {succeeded}; failed: {failed}; timeout: {timed_out}; total: {len(results)}",
        status,
    )


def _service_status_report(paths: RuntimePaths, service: ServiceDefinition) -> CommandReport:
    load_result = load_config(paths)
    state = read_service_daemon_state(load_result.config, service.name)
    issues = _sort_issues(list(load_result.issues) + list(state.issues))
    process_state = _state_details(state)["process state"]
    return CommandReport(
        command=f"{service.name} status",
        status=_status_from_issues(issues),
        summary=(
            f"{service.name}: {process_state}; queued: {state.queue_length}; "
            f"cursor: {state.cursor}"
        ),
        details=_state_details(state),
        issues=_report_issues(issues),
        actions=_service_actions(paths, service),
    )


def cmd_service_status(
    args: argparse.Namespace, paths: RuntimePaths, service: ServiceDefinition
) -> int:
    report = _service_status_report(paths, service)
    _print_report(report, args)
    return _exit_code_for_report(report)


def _plan_records(records) -> list[dict[str, str]]:
    return [
        {"path": record.path, "action": record.action, "reason": record.reason}
        for record in records
    ]


def _scope_details(scope: SyncScopeInfo) -> dict[str, object]:
    baseline: ScopeBaseline = scope.baseline
    return {
        "allowlist status": scope.allowlist_status,
        "allowlist entries": scope.allowlist_count,
        "allowlist message": scope.allowlist_message,
        "scope baseline": f"{baseline.mode} ({baseline.status})",
    }


def _pushd_plan_details(plan: PushdPlan, scope: SyncScopeInfo) -> dict[str, object]:
    return {
        "plan source": str(plan.queue_file),
        "plan summary": (
            f"upload: {plan.upload_count}; excluded: {plan.excluded_count}; "
            f"invalid: {plan.invalid_count}"
        ),
        "pending queue items": plan.total,
        "planned uploads": plan.upload_count,
        "excluded queue items": plan.excluded_count,
        "invalid queue items": plan.invalid_count,
        "planned upload records": _plan_records(plan.upload_records),
        "excluded queue records": _plan_records(plan.excluded_records),
        "invalid queue records": _plan_records(plan.invalid_records),
        **_scope_details(scope),
    }


def _diffd_plan_details(plan: DiffdPlan) -> dict[str, object]:
    return {
        "remote changes file": str(plan.remote_changes_file),
        "pending downloads file": str(plan.pending_downloads_file),
        "plan summary": (
            f"downloads: {plan.download_count}; remote changes: {plan.remote_change_count}; "
            f"pending downloads: {plan.pending_download_count}; skipped: {plan.skipped_count}"
        ),
        "remote changes": plan.remote_change_count,
        "pending downloads": plan.pending_download_count,
        "planned downloads": plan.download_count,
        "skipped download records": plan.skipped_count,
        "remote change records": _plan_records(plan.remote_change_records),
        "pending download records": _plan_records(plan.pending_download_records),
        "planned download records": _plan_records(plan.download_records),
        "skipped download record details": _plan_records(plan.skipped_records),
    }


def _pushd_plan_summary(plan: PushdPlan) -> str:
    return f"upload: {plan.upload_count}; excluded: {plan.excluded_count}; invalid: {plan.invalid_count}"


def _diffd_plan_summary(plan: DiffdPlan) -> str:
    return (
        f"downloads: {plan.download_count}; remote changes: {plan.remote_change_count}; "
        f"pending downloads: {plan.pending_download_count}; skipped: {plan.skipped_count}"
    )


def _transfer_manual_review_reason(record: PlanRecord, opposite_paths: set[str]) -> str:
    action = record.action.strip().lower().replace("_", "-")
    if any(token in action for token in _MANUAL_REVIEW_ACTION_TOKENS):
        return f"{record.action} action requires manual review"
    if action not in _SIMPLE_TRANSFER_ACTIONS:
        return f"{record.action} action is not a simple create/update transfer"
    if record.path in opposite_paths:
        return "same path also has an opposite-side change"
    return ""


def _filter_manual_review_transfers(
    records: tuple[PlanRecord, ...],
    opposite_records: tuple[PlanRecord, ...],
) -> tuple[tuple[PlanRecord, ...], tuple[PlanRecord, ...]]:
    opposite_paths = {record.path for record in opposite_records if record.path}
    transfer_records: list[PlanRecord] = []
    manual_review_records: list[PlanRecord] = []
    for record in records:
        reason = _transfer_manual_review_reason(record, opposite_paths)
        if reason:
            manual_review_records.append(PlanRecord(record.path, record.action, reason))
        else:
            transfer_records.append(record)
    return tuple(transfer_records), tuple(manual_review_records)


def _opposite_transfer_candidates(config: AppConfig, service: ServiceDefinition) -> tuple[PlanRecord, ...]:
    if service.name == "pushd":
        diffd_state = read_service_daemon_state(config, "diffd")
        daemon_state = read_daemon_state(config)
        diffd_plan = build_diffd_plan(config, diffd_state, daemon_state)
        return diffd_plan.download_records

    pushd_state = read_service_daemon_state(config, "pushd")
    pushd_plan, _scope = build_pushd_plan(config, pushd_state)
    return pushd_plan.upload_records


def _manual_review_issue(service: ServiceDefinition, count: int) -> ConfigIssue | None:
    if count == 0:
        return None
    return ConfigIssue(
        key=f"PCLOUD_TOOLS_{service.name.upper()}_TRANSFER_MANUAL_REVIEW",
        level="warning",
        message=(
            f"{count} {service.name} transfer record(s) require manual review and were "
            "excluded from planned transfer commands"
        ),
    )


def _transfer_plan_summary(service: ServiceDefinition, counts: dict[str, int]) -> str:
    if service.name == "pushd":
        return (
            f"upload: {counts['planned uploads']}; "
            f"manual review: {counts['manual review transfer records']}; "
            f"excluded: {counts['excluded queue items']}; invalid: {counts['invalid queue items']}"
        )
    return (
        f"downloads: {counts['planned downloads']}; "
        f"manual review: {counts['manual review transfer records']}; "
        f"remote changes: {counts['remote changes']}; pending downloads: {counts['pending downloads']}; "
        f"skipped: {counts['skipped download records']}"
    )


def _real_transfer_target_confirmation(
    args: argparse.Namespace,
    service: ServiceDefinition,
    commands: list[dict[str, object]],
) -> tuple[dict[str, object], list[ConfigIssue]]:
    confirmed_path_raw = getattr(args, "confirm_path", None)
    confirmed_direction = getattr(args, "confirm_direction", None)
    confirmed_path = normalize_plan_path(confirmed_path_raw) if confirmed_path_raw else ""
    expected = commands[0] if commands else {}
    expected_path = str(expected.get("path", "")) if isinstance(expected, dict) else ""
    expected_direction = str(expected.get("direction", "")) if isinstance(expected, dict) else ""

    details: dict[str, object] = {
        "name": "first real run target",
        "confirmed path": confirmed_path or "-",
        "confirmed direction": confirmed_direction or "-",
        "expected path": expected_path or "-",
        "expected direction": expected_direction or "-",
    }
    if not confirmed_path_raw and not confirmed_direction:
        details.update(
            {
                "status": "pending",
                "detail": "operator must confirm exact path and direction before opening the real gate",
            }
        )
        return details, []

    problems: list[str] = []
    if len(commands) != 1:
        problems.append(f"expected exactly one planned transfer, found {len(commands)}")
    if not confirmed_path:
        problems.append("missing confirmed path")
    elif confirmed_path != expected_path:
        problems.append(f"confirmed path {confirmed_path!r} does not match planned path {expected_path!r}")
    if not confirmed_direction:
        problems.append("missing confirmed direction")
    elif confirmed_direction != expected_direction:
        problems.append(
            f"confirmed direction {confirmed_direction!r} does not match planned direction {expected_direction!r}"
        )

    if problems:
        detail = "; ".join(problems)
        details.update({"status": "not-ok", "detail": detail})
        return details, [
            ConfigIssue(
                key=f"PCLOUD_TOOLS_{service.name.upper()}_REAL_TRANSFER_TARGET_CONFIRMATION",
                level="warning",
                message=f"first real transfer target confirmation failed: {detail}",
            )
        ]

    details.update(
        {
            "status": "ok",
            "detail": f"confirmed {confirmed_direction} target {confirmed_path}",
        }
    )
    return details, []


def _real_transfer_policy_check(
    args: argparse.Namespace,
    option_name: str,
    checklist_name: str,
    pending_detail: str,
) -> dict[str, object]:
    value = getattr(args, option_name, None)
    if not value:
        return {
            "name": checklist_name,
            "status": "pending",
            "detail": pending_detail,
        }
    return {
        "name": checklist_name,
        "status": "ok",
        "detail": value,
    }


def _pushd_preview_details(
    paths: RuntimePaths, config: AppConfig, plan: PushdPlan, scope: SyncScopeInfo
) -> dict[str, object]:
    return {
        "planned action": "preview pcloud-pushd scaffold",
        "implementation status": "scaffold only; fswatch and upload execution are disabled",
        "dev mode": "on" if paths.dev_mode else "off",
        "watch root": str(config.core_dir),
        "target remote": config.core_remote,
        "allowlist file": str(config.allowlist_file),
        "default excludes": list(config.default_excludes),
        "debounce seconds": config.pushd_debounce_seconds,
        "queue limit": config.pushd_queue_limit,
        "state dir": str(config.state_dir / "pushd"),
        **_pushd_plan_details(plan, scope),
    }


def _diffd_preview_details(
    paths: RuntimePaths, config: AppConfig, daemon_state: DaemonState, plan: DiffdPlan
) -> dict[str, object]:
    return {
        "planned action": "preview pcloud-diffd scaffold",
        "implementation status": "scaffold only; pCloud API long-poll and downloads are disabled",
        "dev mode": "on" if paths.dev_mode else "off",
        "remote root": config.core_remote,
        "poll interval seconds": config.diffd_poll_interval_seconds,
        "batch limit": config.diffd_batch_limit,
        "state dir": str(config.state_dir / "diffd"),
        "daemon diffid": daemon_state.diffid,
        "auto-download": "on" if daemon_state.auto_download_enabled else "off",
        **_diffd_plan_details(plan),
    }


def _service_preview_report(paths: RuntimePaths, service: ServiceDefinition) -> CommandReport:
    load_result = load_config(paths)
    state = read_service_daemon_state(load_result.config, service.name)
    issues = list(load_result.issues) + list(state.issues)
    if service.name == "pushd":
        plan, scope = build_pushd_plan(load_result.config, state)
        issues.extend(scope_issues(scope))
        issues.extend(plan.issues)
        details = _pushd_preview_details(paths, load_result.config, plan, scope)
    else:
        daemon_state = read_daemon_state(load_result.config)
        issues.extend(daemon_state.issues)
        plan = build_diffd_plan(load_result.config, state, daemon_state)
        issues.extend(plan.issues)
        details = _diffd_preview_details(paths, load_result.config, daemon_state, plan)
    issues = _sort_issues(issues)
    details.update(
        {
            "pid file": str(state.pid_file),
            "queue file": str(state.queue_file),
            "cursor file": str(state.cursor_file),
            "last plan file": str(state.last_plan_file),
        }
    )

    return CommandReport(
        command=f"{service.name} preview",
        status=_status_from_issues(issues),
        summary=f"{service.name} scaffold preview is ready",
        details=details,
        issues=_report_issues(issues),
        actions=_service_actions(paths, service),
    )


def cmd_service_preview(
    args: argparse.Namespace, paths: RuntimePaths, service: ServiceDefinition
) -> int:
    report = _service_preview_report(paths, service)
    _print_report(report, args)
    return _exit_code_for_report(report)


def _gate_details(paths: RuntimePaths, config: AppConfig, service: ServiceDefinition) -> dict[str, object]:
    shared_requirements = [
        "saved shadow validation report with status ok",
        "reviewer approval recorded in report handoff",
        "explicit operator gate for this real operation",
    ]
    if service.name == "pushd":
        blocked = [
            "fswatch resident daemon",
            "launchd registration",
            "real upload execution",
            "queue consumption against live state",
        ]
        next_units = [
            "define fswatch event capture schema",
            "add one-shot read-only fswatch probe",
            "add upload executor preview that emits commands without running them",
        ]
    else:
        blocked = [
            "pCloud API long-poll",
            "launchd registration",
            "real download execution",
            "diff cursor mutation against live state",
        ]
        next_units = [
            "define pCloud diff response fixture schema",
            "add one-shot read-only diff response parser",
            "add download executor preview that emits commands without running them",
        ]
    return {
        "gate status": "closed",
        "allowed work": "dev-state preview/status/plan/report/test only",
        "dev mode": "on" if paths.dev_mode else "off",
        "state dir": str(config.state_dir / service.name),
        "workspace root": str(paths.workspace_root),
        "shadow validation command": "python3 scripts/pcloud-shadow-validation.py --json",
        "blocked operations": blocked,
        "required before opening": shared_requirements,
        "suggested next units": next_units,
    }


def _service_gate_report(paths: RuntimePaths, service: ServiceDefinition) -> CommandReport:
    load_result = load_config(paths)
    issues = list(load_result.issues)
    issues.append(
        ConfigIssue(
            key=f"PCLOUD_TOOLS_{service.name.upper()}_REAL_GATE",
            level="warning",
            message=(
                f"{service.name} real operations remain gated; "
                "use preview/dev-state paths until the dedicated gate is explicitly opened"
            ),
        )
    )
    issues = _sort_issues(issues)
    return CommandReport(
        command=f"{service.name} gate",
        status=_status_from_issues(issues),
        summary=f"{service.name} real-operation gate is closed",
        details=_gate_details(paths, load_result.config, service),
        issues=_report_issues(issues),
        actions=_service_actions(paths, service),
    )


def cmd_service_gate(
    args: argparse.Namespace, paths: RuntimePaths, service: ServiceDefinition
) -> int:
    report = _service_gate_report(paths, service)
    _print_report(report, args)
    return _exit_code_for_report(report)


def _pushd_run_report(args: argparse.Namespace, paths: RuntimePaths) -> CommandReport:
    load_result = load_config(paths)
    state = read_service_daemon_state(load_result.config, "pushd")
    plan, scope = build_pushd_plan(load_result.config, state)
    execute = getattr(args, "execute", False)
    issues = list(load_result.issues) + list(state.issues) + list(plan.issues) + scope_issues(scope)
    details: dict[str, object] = {
        "planned action": "record pushd dry-run state" if execute else "preview pushd dry run",
        "run mode": "dry-run",
        "plan summary": _pushd_plan_summary(plan),
        "planned uploads": plan.upload_count,
        "excluded queue items": plan.excluded_count,
        "invalid queue items": plan.invalid_count,
        "last plan file": str(state.last_plan_file),
        "last event file": str(state.last_event_file),
        "cursor file": str(state.cursor_file),
    }

    if execute:
        dev_issue = _dev_execute_issue(paths, load_result.config, "pushd run")
        if dev_issue:
            issues.append(dev_issue)
        if not _has_errors(issues):
            result = record_dry_run_state(
                state=state,
                service_name="pushd",
                plan_summary=_pushd_plan_summary(plan),
                counts={
                    "planned_uploads": plan.upload_count,
                    "excluded_queue_items": plan.excluded_count,
                    "invalid_queue_items": plan.invalid_count,
                },
                records={
                    "planned_uploads": record_payloads(plan.upload_records),
                    "excluded_queue": record_payloads(plan.excluded_records),
                    "invalid_queue": record_payloads(plan.invalid_records),
                },
            )
            details["recorded cursor"] = result.cursor

    summary = "pushd dry-run recorded" if execute and not _has_errors(issues) else "pushd run preview is ready"
    if _has_errors(issues):
        summary = "pushd run cannot be recorded until issues are resolved"
    issues = _sort_issues(issues)
    return CommandReport(
        command="pushd run",
        status=_status_from_issues(issues),
        summary=summary,
        details=details,
        issues=_report_issues(issues),
        actions=_service_actions(paths, _SERVICES["pushd"]),
    )


def _diffd_run_report(args: argparse.Namespace, paths: RuntimePaths) -> CommandReport:
    load_result = load_config(paths)
    state = read_service_daemon_state(load_result.config, "diffd")
    daemon_state = read_daemon_state(load_result.config)
    plan = build_diffd_plan(load_result.config, state, daemon_state)
    execute = getattr(args, "execute", False)
    issues = list(load_result.issues) + list(state.issues) + list(daemon_state.issues) + list(plan.issues)
    details: dict[str, object] = {
        "planned action": "record diffd dry-run state" if execute else "preview diffd dry run",
        "run mode": "dry-run",
        "plan summary": _diffd_plan_summary(plan),
        "remote changes": plan.remote_change_count,
        "pending downloads": plan.pending_download_count,
        "planned downloads": plan.download_count,
        "skipped download records": plan.skipped_count,
        "daemon diffid": daemon_state.diffid,
        "last plan file": str(state.last_plan_file),
        "last event file": str(state.last_event_file),
        "cursor file": str(state.cursor_file),
    }

    if execute:
        dev_issue = _dev_execute_issue(paths, load_result.config, "diffd run")
        if dev_issue:
            issues.append(dev_issue)
        if not _has_errors(issues):
            result = record_dry_run_state(
                state=state,
                service_name="diffd",
                plan_summary=_diffd_plan_summary(plan),
                counts={
                    "remote_changes": plan.remote_change_count,
                    "pending_downloads": plan.pending_download_count,
                    "planned_downloads": plan.download_count,
                    "skipped_download_records": plan.skipped_count,
                },
                records={
                    "remote_changes": record_payloads(plan.remote_change_records),
                    "pending_downloads": record_payloads(plan.pending_download_records),
                },
            )
            details["recorded cursor"] = result.cursor

    summary = "diffd dry-run recorded" if execute and not _has_errors(issues) else "diffd run preview is ready"
    if _has_errors(issues):
        summary = "diffd run cannot be recorded until issues are resolved"
    issues = _sort_issues(issues)
    return CommandReport(
        command="diffd run",
        status=_status_from_issues(issues),
        summary=summary,
        details=details,
        issues=_report_issues(issues),
        actions=_service_actions(paths, _SERVICES["diffd"]),
    )


def cmd_service_run(
    args: argparse.Namespace, paths: RuntimePaths, service: ServiceDefinition
) -> int:
    report = _pushd_run_report(args, paths) if service.name == "pushd" else _diffd_run_report(args, paths)
    _print_report(report, args)
    return _exit_code_for_report(report)


def _invalid_fswatch_records(invalid_events) -> tuple[PlanRecord, ...]:
    return tuple(
        PlanRecord(path="", action="upload", reason=f"fswatch fixture: {event.reason}")
        for event in invalid_events
        if event.reason != "blank or comment"
    )


def _invalid_fswatch_details(invalid_events) -> list[dict[str, str]]:
    return [
        {"raw": event.raw, "reason": event.reason}
        for event in invalid_events
        if event.reason != "blank or comment"
    ]


def _pushd_fswatch_report(args: argparse.Namespace, paths: RuntimePaths) -> CommandReport:
    load_result = load_config(paths)
    fixture = Path(args.fixture)
    scope = sync_allowlist_info(load_result.config)
    issues = list(load_result.issues) + scope_issues(scope)
    details: dict[str, object] = {
        "planned action": "preview pushd fswatch fixture",
        "implementation status": "fixture parser only; fswatch process is not started",
        "fixture file": str(fixture),
        "gate status": "closed",
    }

    try:
        parsed = parse_fswatch_fixture(fixture)
    except OSError as exc:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_PUSHD_FSWATCH_FIXTURE",
                level="error",
                message=f"cannot read fswatch fixture {fixture}: {exc}",
            )
        )
        issues = _sort_issues(issues)
        return CommandReport(
            command="pushd fswatch preview",
            status=_status_from_issues(issues),
            summary="pushd fswatch fixture cannot be previewed",
            details=details,
            issues=_report_issues(issues),
            actions=_service_actions(paths, _SERVICES["pushd"]),
        )

    event_records = fswatch_events_to_records(parsed.events)
    invalid_records = _invalid_fswatch_records(parsed.invalid)
    plan = build_pushd_plan_from_records(
        load_result.config,
        parsed.source,
        (*event_records, *invalid_records),
        total=len(parsed.events) + len(invalid_records),
    )
    issues.extend(plan.issues)
    issues = _sort_issues(issues)
    details.update(
        {
            "parsed fswatch events": len(parsed.events),
            "invalid fswatch events": len(invalid_records),
            "invalid fswatch records": _invalid_fswatch_details(parsed.invalid),
            **_pushd_plan_details(plan, scope),
        }
    )
    return CommandReport(
        command="pushd fswatch preview",
        status=_status_from_issues(issues),
        summary="pushd fswatch fixture preview is ready",
        details=details,
        issues=_report_issues(issues),
        actions=_service_actions(paths, _SERVICES["pushd"]),
    )


def _pushd_fswatch_probe_command(config: AppConfig, fswatch_bin: str) -> tuple[str, ...]:
    return (
        fswatch_bin,
        "--one-event",
        "--recursive",
        "--event-flag-separator",
        ",",
        str(config.core_dir),
    )


def _pushd_fswatch_probe_report(paths: RuntimePaths) -> CommandReport:
    load_result = load_config(paths)
    issues = list(load_result.issues)
    fswatch_bin = shutil.which("fswatch")
    if fswatch_bin:
        command = _pushd_fswatch_probe_command(load_result.config, fswatch_bin)
        availability = "available"
    else:
        command = _pushd_fswatch_probe_command(load_result.config, "fswatch")
        availability = "missing"
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_PUSHD_FSWATCH_BIN",
                level="warning",
                message="fswatch was not found on PATH; probe remains preview-only",
            )
        )

    details: dict[str, object] = {
        "planned action": "preview pushd one-shot fswatch probe",
        "implementation status": "probe preview only; fswatch process is not started",
        "gate status": "closed",
        "allowed work": "command preview only",
        "watch root": str(load_result.config.core_dir),
        "fswatch availability": availability,
        "fswatch command": list(command),
        "state writes": "none",
    }
    issues = _sort_issues(issues)
    return CommandReport(
        command="pushd fswatch probe",
        status=_status_from_issues(issues),
        summary="pushd fswatch one-shot probe preview is ready",
        details=details,
        issues=_report_issues(issues),
        actions=_service_actions(paths, _SERVICES["pushd"]),
    )


def cmd_pushd_fswatch(args: argparse.Namespace, paths: RuntimePaths) -> int | None:
    if args.fswatch_command == "preview":
        report = _pushd_fswatch_report(args, paths)
        _print_report(report, args)
        return _exit_code_for_report(report)
    if args.fswatch_command == "probe":
        report = _pushd_fswatch_probe_report(paths)
        _print_report(report, args)
        return _exit_code_for_report(report)
    return None


def _invalid_diff_records(invalid_changes) -> tuple[PlanRecord, ...]:
    return tuple(
        PlanRecord(path="", action="download", reason=f"diff fixture: {change.reason}")
        for change in invalid_changes
    )


def _invalid_diff_details(invalid_changes) -> list[dict[str, str]]:
    return [{"raw": change.raw, "reason": change.reason} for change in invalid_changes]


def _diffd_diff_report(args: argparse.Namespace, paths: RuntimePaths) -> CommandReport:
    load_result = load_config(paths)
    fixture = Path(args.fixture)
    issues = list(load_result.issues)
    details: dict[str, object] = {
        "planned action": "preview diffd pCloud diff fixture",
        "implementation status": "fixture parser only; pCloud API is not called",
        "fixture file": str(fixture),
        "gate status": "closed",
    }

    try:
        parsed = parse_diff_response_fixture(fixture)
    except OSError as exc:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_DIFFD_DIFF_FIXTURE",
                level="error",
                message=f"cannot read pCloud diff fixture {fixture}: {exc}",
            )
        )
        issues = _sort_issues(issues)
        return CommandReport(
            command="diffd diff preview",
            status=_status_from_issues(issues),
            summary="diffd pCloud diff fixture cannot be previewed",
            details=details,
            issues=_report_issues(issues),
            actions=_service_actions(paths, _SERVICES["diffd"]),
        )

    remote_records = diff_changes_to_records(parsed.changes)
    invalid_records = _invalid_diff_records(parsed.invalid)
    plan = build_diffd_plan_from_records(
        config=load_result.config,
        remote_changes_file=parsed.source,
        pending_downloads_file=load_result.config.state_dir / "daemon" / "pending-downloads.json",
        remote_records=(*remote_records, *invalid_records),
    )
    issues.extend(plan.issues)
    issues = _sort_issues(issues)
    details.update(
        {
            "fixture diffid": parsed.diffid,
            "parsed diff changes": len(parsed.changes),
            "invalid diff changes": len(parsed.invalid),
            "invalid diff records": _invalid_diff_details(parsed.invalid),
            **_diffd_plan_details(plan),
        }
    )
    return CommandReport(
        command="diffd diff preview",
        status=_status_from_issues(issues),
        summary="diffd pCloud diff fixture preview is ready",
        details=details,
        issues=_report_issues(issues),
        actions=_service_actions(paths, _SERVICES["diffd"]),
    )


def cmd_diffd_diff(args: argparse.Namespace, paths: RuntimePaths) -> int | None:
    if args.diff_command == "preview":
        report = _diffd_diff_report(args, paths)
        _print_report(report, args)
        return _exit_code_for_report(report)
    return None


def _diffd_api_poll_report(paths: RuntimePaths) -> CommandReport:
    load_result = load_config(paths)
    daemon_state = read_daemon_state(load_result.config)
    issues = _sort_issues(list(load_result.issues) + list(daemon_state.issues))
    details: dict[str, object] = {
        "planned action": "preview diffd one-shot pCloud API poll",
        "implementation status": "API poll preview only; pCloud API is not called",
        "gate status": "closed",
        "allowed work": "request-shape preview only",
        "remote root": load_result.config.core_remote,
        "current diffid": daemon_state.diffid,
        "request method": "GET",
        "request path": "/diff",
        "request query": {
            "diffid": daemon_state.diffid,
            "limit": load_result.config.diffd_batch_limit,
        },
        "required before execution": [
            "explicit operator/reviewer API gate",
            "configured pCloud API base URL",
            "configured least-privilege pCloud API credential",
            "fixture coverage for expected response shapes",
        ],
        "state writes": "none",
    }
    return CommandReport(
        command="diffd api-poll preview",
        status=_status_from_issues(issues),
        summary="diffd pCloud API poll preview is ready",
        details=details,
        issues=_report_issues(issues),
        actions=_service_actions(paths, _SERVICES["diffd"]),
    )


def cmd_diffd_api_poll(args: argparse.Namespace, paths: RuntimePaths) -> int | None:
    if args.api_poll_command == "preview":
        report = _diffd_api_poll_report(paths)
        _print_report(report, args)
        return _exit_code_for_report(report)
    return None


def _remote_path(remote: str, path: str) -> str:
    return f"{remote.rstrip('/')}/{path.lstrip('/')}"


_TRANSFER_EXECUTION_GATE_VALUE = "dev-fake-rclone"
_TRANSFER_CLEANUP_WAIT_SECONDS = 1
_REAL_TRANSFER_REQUIRED_SHADOW_CHECKS = {
    "temporary workspace guard",
    "temporary state dir guard",
    "unsafe state dir guard",
}


def _preview_rclone_bin(config: AppConfig) -> str:
    configured = config.rclone_bin.strip()
    if configured and configured != "rclone":
        return configured
    return shutil.which("rclone") or "rclone"


def _transfer_command_records(
    config: AppConfig,
    service: ServiceDefinition,
    records: tuple[PlanRecord, ...],
    *,
    rclone_bin: str | None = None,
) -> list[dict[str, object]]:
    command_bin = rclone_bin or _preview_rclone_bin(config)
    planned: list[dict[str, object]] = []
    for record in records:
        local_path = str(config.core_dir / record.path)
        remote_path = _remote_path(config.core_remote, record.path)
        if service.name == "pushd":
            command = [command_bin, "copyto", local_path, remote_path]
            direction = "upload"
        else:
            command = [command_bin, "copyto", remote_path, local_path]
            direction = "download"
        planned.append(
            {
                "path": record.path,
                "direction": direction,
                "reason": record.reason,
                "command": command,
            }
        )
    return planned


def _transfer_fake_rclone_issue(paths: RuntimePaths, config: AppConfig, command: str) -> ConfigIssue | None:
    dev_issue = _dev_execute_issue(paths, config, command)
    if dev_issue:
        return dev_issue
    if config.transfer_execution_gate != _TRANSFER_EXECUTION_GATE_VALUE:
        return ConfigIssue(
            key="PCLOUD_TOOLS_TRANSFER_EXECUTION_GATE",
            level="error",
            message=(
                "refusing transfer execution until PCLOUD_TOOLS_TRANSFER_EXECUTION_GATE="
                f"{_TRANSFER_EXECUTION_GATE_VALUE!r}"
            ),
        )
    raw_bin = config.rclone_bin.strip()
    if not raw_bin:
        return ConfigIssue(
            key="PCLOUD_TOOLS_RCLONE_BIN",
            level="error",
            message="PCLOUD_TOOLS_RCLONE_BIN must point to a fake-rclone executable for dev transfer execution",
        )
    configured = Path(raw_bin).expanduser()
    if not configured.is_absolute():
        return ConfigIssue(
            key="PCLOUD_TOOLS_RCLONE_BIN",
            level="error",
            message="PCLOUD_TOOLS_RCLONE_BIN must be an absolute fake-rclone path for dev transfer execution",
        )
    try:
        resolved = configured.resolve(strict=True)
    except OSError as exc:
        return ConfigIssue(
            key="PCLOUD_TOOLS_RCLONE_BIN",
            level="error",
            message=f"cannot resolve fake-rclone executable {configured}: {exc}",
        )
    fake_root = (paths.workspace_root / ".dev-state").resolve()
    if resolved.name != "fake-rclone" or not resolved.is_relative_to(fake_root):
        return ConfigIssue(
            key="PCLOUD_TOOLS_RCLONE_BIN",
            level="error",
            message=(
                "refusing transfer execution unless PCLOUD_TOOLS_RCLONE_BIN resolves to "
                f"a fake-rclone executable under {fake_root}"
            ),
        )
    if not resolved.is_file() or not resolved.stat().st_mode & 0o111:
        return ConfigIssue(
            key="PCLOUD_TOOLS_RCLONE_BIN",
            level="error",
            message=f"fake-rclone path is not executable: {resolved}",
        )
    return None


def _subprocess_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace").strip()
    return value.strip()


def _cleanup_transfer_process_group(process: subprocess.Popen[str]) -> dict[str, object]:
    cleanup: dict[str, object] = {
        "process group cleanup": "attempted",
        "terminate attempted": False,
        "kill attempted": False,
        "terminated": False,
    }
    try:
        pgid = os.getpgid(process.pid)
        cleanup["process group id"] = pgid
    except ProcessLookupError:
        cleanup["process group cleanup"] = "already-exited"
        cleanup["terminated"] = True
        return cleanup
    except OSError as exc:
        cleanup["process group cleanup"] = "pgid-unavailable"
        cleanup["cleanup error"] = str(exc)
        try:
            process.terminate()
            cleanup["terminate attempted"] = True
        except OSError as terminate_exc:
            cleanup["cleanup error"] = f"{cleanup['cleanup error']}; terminate failed: {terminate_exc}"
        pgid = None

    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGTERM)
            cleanup["terminate attempted"] = True
        except ProcessLookupError:
            cleanup["process group cleanup"] = "already-exited"
            cleanup["terminated"] = True
            return cleanup
        except OSError as exc:
            cleanup["process group cleanup"] = "terminate-failed"
            cleanup["cleanup error"] = str(exc)

    try:
        process.wait(timeout=_TRANSFER_CLEANUP_WAIT_SECONDS)
        cleanup["terminated"] = True
        return cleanup
    except subprocess.TimeoutExpired:
        cleanup["process group cleanup"] = "terminate-timeout"

    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGKILL)
            cleanup["kill attempted"] = True
        except ProcessLookupError:
            cleanup["terminated"] = True
            return cleanup
        except OSError as exc:
            cleanup["process group cleanup"] = "kill-failed"
            cleanup["cleanup error"] = str(exc)
    else:
        try:
            process.kill()
            cleanup["kill attempted"] = True
        except OSError as exc:
            cleanup["process group cleanup"] = "kill-failed"
            cleanup["cleanup error"] = str(exc)

    try:
        process.wait(timeout=_TRANSFER_CLEANUP_WAIT_SECONDS)
        cleanup["terminated"] = True
        if cleanup.get("process group cleanup") in {"terminate-timeout", "kill-failed"}:
            cleanup["process group cleanup"] = "killed"
    except subprocess.TimeoutExpired:
        cleanup["process group cleanup"] = "kill-timeout"
    return cleanup


def _execute_transfer_commands(
    commands: list[dict[str, object]], *, timeout_seconds: int
) -> tuple[list[dict[str, object]], list[ConfigIssue]]:
    results: list[dict[str, object]] = []
    issues: list[ConfigIssue] = []
    for item in commands:
        command = [str(part) for part in item["command"]]  # command records are built internally.
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            stdout, stderr = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            cleanup = _cleanup_transfer_process_group(process)
            try:
                stdout, stderr = process.communicate(timeout=_TRANSFER_CLEANUP_WAIT_SECONDS)
            except subprocess.TimeoutExpired as exc:
                stdout = _subprocess_output(exc.stdout)
                stderr = _subprocess_output(exc.stderr)
                cleanup["communicate timeout"] = True
            results.append(
                {
                    **item,
                    "returncode": None,
                    "timed_out": True,
                    "timeout seconds": timeout_seconds,
                    "cleanup": cleanup,
                    "stdout": _subprocess_output(stdout),
                    "stderr": _subprocess_output(stderr),
                }
            )
            issues.append(
                ConfigIssue(
                    key="PCLOUD_TOOLS_TRANSFER_EXEC_TIMEOUT",
                    level="error",
                    message=f"transfer command timed out for {item['path']} after {timeout_seconds}s",
                )
            )
            continue
        except OSError as exc:
            results.append(
                {**item, "returncode": None, "timed_out": False, "stdout": "", "stderr": str(exc)}
            )
            issues.append(
                ConfigIssue(
                    key="PCLOUD_TOOLS_TRANSFER_EXEC",
                    level="error",
                    message=f"transfer command could not start for {item['path']}: {exc}",
                )
            )
            continue
        results.append(
            {
                **item,
                "returncode": process.returncode,
                "timed_out": False,
                "stdout": stdout.strip(),
                "stderr": stderr.strip(),
            }
        )
        if process.returncode != 0:
            issues.append(
                ConfigIssue(
                    key="PCLOUD_TOOLS_TRANSFER_EXEC",
                    level="error",
                    message=f"transfer command failed for {item['path']} with exit {process.returncode}",
                )
            )
    return results, issues


def _record_transfer_execution_state(
    state: ServiceDaemonState,
    service: ServiceDefinition,
    commands: list[dict[str, object]],
    results: list[dict[str, object]],
) -> Path:
    generated_at = datetime.now(timezone.utc).isoformat()
    payload = {
        "service": service.name,
        "mode": "dev-fake-rclone-transfer",
        "generated_at": generated_at,
        "planned_transfer_commands": commands,
        "results": results,
    }
    path = state.state_dir / "last-transfer.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return path


def _successful_transfer_results(
    payload: dict[str, object] | None,
    direction: str,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        return [], []
    successful: list[dict[str, object]] = []
    retained: list[dict[str, object]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        item_direction = str(item.get("direction", ""))
        normalized = normalize_plan_path(item.get("path", ""))
        result = {**item, "path": normalized}
        if (
            normalized
            and item_direction == direction
            and item.get("returncode") == 0
            and not item.get("timed_out")
        ):
            successful.append(result)
        else:
            retained.append(result)
    return successful, retained


def _consume_source_records(
    config: AppConfig,
    state: ServiceDaemonState,
    service: ServiceDefinition,
) -> tuple[Path, tuple[PlanRecord, ...], list[ConfigIssue]]:
    if service.name == "pushd":
        plan, scope = build_pushd_plan(config, state)
        issues = list(plan.issues) + scope_issues(scope)
        return (
            state.queue_file,
            (*plan.upload_records, *plan.excluded_records, *plan.invalid_records),
            issues,
        )

    daemon_state = read_daemon_state(config)
    plan = build_diffd_plan(config, state, daemon_state)
    return state.state_dir / "remote-changes.json", plan.remote_change_records, [
        *daemon_state.issues,
        *plan.issues,
    ]


def _consume_preview_report(paths: RuntimePaths, service: ServiceDefinition) -> CommandReport:
    load_result = load_config(paths)
    state = read_service_daemon_state(load_result.config, service.name)
    issues = list(load_result.issues) + list(state.issues)
    source_file, source_records, source_issues = _consume_source_records(
        load_result.config, state, service
    )
    issues.extend(source_issues)
    direction = "upload" if service.name == "pushd" else "download"
    successful, retained = _successful_transfer_results(state.last_transfer, direction)
    success_paths = {str(item.get("path", "")) for item in successful if item.get("path")}
    planned_removals = tuple(record for record in source_records if record.path in success_paths)
    matched_paths = {record.path for record in planned_removals}
    unmatched_successes = [
        item for item in successful
        if str(item.get("path", "")) not in matched_paths
    ]
    details: dict[str, object] = {
        "planned action": f"preview {service.name} transfer consume policy",
        "implementation status": "read-only consume preview; queue/change records are not removed",
        "consume gate status": "preview-only",
        "state writes": "none",
        "source file": str(source_file),
        "last transfer file": str(state.last_transfer_file),
        "last transfer status": "available" if state.last_transfer else "missing",
        "successful transfer results": len(successful),
        "retained transfer results": len(retained),
        "planned record removals": len(planned_removals),
        "unmatched successful transfers": len(unmatched_successes),
        "planned removal record details": _plan_records(planned_removals),
        "unmatched successful transfer details": unmatched_successes,
        "retained transfer result details": retained,
    }
    issues = _sort_issues(issues)
    return CommandReport(
        command=f"{service.name} transfer consume preview",
        status=_status_from_issues(issues),
        summary=f"{service.name} transfer consume policy preview is ready",
        details=details,
        issues=_report_issues(issues),
        actions=_service_actions(paths, service),
    )


def _consume_run_report(
    args: argparse.Namespace,
    paths: RuntimePaths,
    service: ServiceDefinition,
) -> CommandReport:
    execute = getattr(args, "execute", False)
    report = _consume_preview_report(paths, service)
    details = dict(report.details)
    issues = [
        ConfigIssue(level=issue.level, key=issue.key, message=issue.message)
        for issue in report.issues
    ]
    source_file = Path(str(details["source file"]))
    removals = details.get("planned removal record details")
    removal_paths = [
        str(item.get("path", ""))
        for item in removals
        if isinstance(removals, list) and isinstance(item, dict) and item.get("path")
    ]

    details["planned action"] = (
        f"remove {service.name} consumed transfer records"
        if execute
        else f"preview {service.name} transfer consume run"
    )
    details["consume gate status"] = "open: dev-state" if execute else "closed: preview-only"
    details["records to remove"] = len(removal_paths)

    if execute:
        load_result = load_config(paths)
        dev_issue = _dev_execute_issue(paths, load_result.config, f"{service.name} transfer consume run")
        if dev_issue:
            issues.append(dev_issue)
        if not _has_errors(issues):
            before_count: int | None = None
            after_count: int | None = None
            for path in removal_paths:
                result = remove_plan_records(
                    source_file,
                    f"PCLOUD_TOOLS_{service.name.upper()}_TRANSFER_CONSUME",
                    path,
                    write=True,
                )
                if result.issue:
                    issues.append(result.issue)
                    break
                if before_count is None:
                    before_count = result.before_count
                after_count = result.after_count
            details["records before"] = before_count if before_count is not None else 0
            details["records after"] = after_count if after_count is not None else 0
            details["state writes"] = str(source_file) if removal_paths else "none"
        else:
            details["state writes"] = "none"
    else:
        details["state writes"] = "none"

    if _has_errors(issues):
        summary = f"{service.name} transfer consume cannot run until issues are resolved"
    elif execute:
        summary = f"{service.name} transfer consumed records"
    else:
        summary = f"{service.name} transfer consume run preview is ready"
    issues = _sort_issues(issues)
    return CommandReport(
        command=f"{service.name} transfer consume run",
        status=_status_from_issues(issues),
        summary=summary,
        details=details,
        issues=_report_issues(issues),
        actions=_service_actions(paths, service),
    )


def _shadow_report_check(report_path: Path | None) -> tuple[dict[str, object], list[ConfigIssue]]:
    if report_path is None:
        return (
            {
                "name": "saved shadow validation report",
                "status": "pending",
                "detail": "pass --report-path after saving scripts/pcloud-shadow-validation.py --report-path",
            },
            [
                ConfigIssue(
                    key="PCLOUD_TOOLS_REAL_TRANSFER_SHADOW_REPORT",
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
                    key="PCLOUD_TOOLS_REAL_TRANSFER_SHADOW_REPORT",
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
                    key="PCLOUD_TOOLS_REAL_TRANSFER_SHADOW_REPORT",
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
    missing_required = sorted(_REAL_TRANSFER_REQUIRED_SHADOW_CHECKS - check_names)
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
                key="PCLOUD_TOOLS_REAL_TRANSFER_SHADOW_REPORT",
                level="warning",
                message=f"saved shadow validation report is not ok: {detail}",
            )
        ],
    )


def _real_transfer_check_report(
    args: argparse.Namespace,
    paths: RuntimePaths,
    service: ServiceDefinition,
) -> CommandReport:
    load_result = load_config(paths)
    state = read_service_daemon_state(load_result.config, service.name)
    issues = list(load_result.issues) + list(state.issues)
    if service.name == "pushd":
        plan, scope = build_pushd_plan(load_result.config, state)
        issues.extend(plan.issues)
        issues.extend(scope_issues(scope))
        records, manual_review_records = _filter_manual_review_transfers(
            plan.upload_records,
            _opposite_transfer_candidates(load_result.config, service),
        )
        counts = {
            "planned uploads": len(records),
            "manual review transfer records": len(manual_review_records),
            "excluded queue items": plan.excluded_count,
            "invalid queue items": plan.invalid_count,
        }
        direction = "upload"
    else:
        daemon_state = read_daemon_state(load_result.config)
        issues.extend(daemon_state.issues)
        plan = build_diffd_plan(load_result.config, state, daemon_state)
        issues.extend(plan.issues)
        records, manual_review_records = _filter_manual_review_transfers(
            plan.download_records,
            _opposite_transfer_candidates(load_result.config, service),
        )
        counts = {
            "planned downloads": len(records),
            "manual review transfer records": len(manual_review_records),
            "remote changes": plan.remote_change_count,
            "pending downloads": plan.pending_download_count,
            "skipped download records": plan.skipped_count,
        }
        direction = "download"
    plan_summary = _transfer_plan_summary(service, counts)
    manual_review_issue = _manual_review_issue(service, len(manual_review_records))
    if manual_review_issue:
        issues.append(manual_review_issue)

    commands = _transfer_command_records(load_result.config, service, records)
    shadow_check, shadow_issues = _shadow_report_check(getattr(args, "report_path", None))
    issues.extend(shadow_issues)
    issues.append(
        ConfigIssue(
            key="PCLOUD_TOOLS_REAL_TRANSFER_GATE",
            level="warning",
            message="real rclone/pCloud transfer gate remains closed; this command is a read-only checklist",
        )
    )
    if not commands:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_REAL_TRANSFER_TARGET",
                level="warning",
                message=f"no planned {direction} transfer is available for first-run review",
            )
        )

    entrypoint = action_entrypoint_command(paths)
    preview_command = [entrypoint, service.name, "transfer", "preview", "--json"]
    default_sample_path = f"Documents/{service.name}-transfer-gate-sample.txt"
    sample_path = normalize_plan_path(getattr(args, "sample_path", None) or default_sample_path)
    check_command = [entrypoint, service.name, "transfer", "check", "--sample-path", sample_path]
    report_path = getattr(args, "report_path", None)
    if report_path is not None:
        check_command.extend(["--report-path", str(report_path)])
    confirmed_path_raw = getattr(args, "confirm_path", None)
    if confirmed_path_raw:
        check_command.extend(["--confirm-path", normalize_plan_path(confirmed_path_raw)])
    confirmed_direction = getattr(args, "confirm_direction", None)
    if confirmed_direction:
        check_command.extend(["--confirm-direction", confirmed_direction])
    consume_policy = getattr(args, "consume_policy", None)
    if consume_policy:
        check_command.extend(["--consume-policy", consume_policy])
    timeout_policy = getattr(args, "timeout_policy", None)
    if timeout_policy:
        check_command.extend(["--timeout-policy", timeout_policy])
    check_command.append("--json")
    sample_record = PlanRecord(sample_path, direction, "real-transfer-gate-sample")
    if service.name == "pushd":
        sample_plan = build_pushd_plan_from_records(
            load_result.config,
            state.queue_file,
            (sample_record,),
        )
        sample_ready = sample_plan.upload_count == 1
        sample_skip_detail = (
            sample_plan.excluded_records[0].reason
            if sample_plan.excluded_records
            else "invalid path" if sample_plan.invalid_records else ""
        )
    else:
        sample_plan = build_diffd_plan_from_records(
            config=load_result.config,
            remote_changes_file=state.state_dir / "remote-changes.json",
            pending_downloads_file=load_result.config.state_dir / "daemon" / "pending-downloads.json",
            remote_records=(sample_record,),
        )
        sample_ready = sample_plan.download_count == 1
        sample_skip_detail = sample_plan.skipped_records[0].reason if sample_plan.skipped_records else ""
    if not sample_ready:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_REAL_TRANSFER_SAMPLE_PATH",
                level="warning",
                message=(
                    "sample path will not become a planned transfer; "
                    f"use a relative allowlisted path: {sample_path or '(empty)'}"
                ),
            )
        )
    if service.name == "pushd":
        setup_command = [
            entrypoint,
            "pushd",
            "queue",
            "add",
            sample_path,
            "--reason",
            "real-transfer-gate-sample",
            "--execute",
            "--json",
        ]
        cleanup_command = [
            entrypoint,
            "pushd",
            "queue",
            "remove",
            sample_path,
            "--execute",
            "--json",
        ]
    else:
        setup_command = [
            entrypoint,
            "diffd",
            "remote-change",
            "add",
            sample_path,
            "--reason",
            "real-transfer-gate-sample",
            "--execute",
            "--json",
        ]
        cleanup_command = [
            entrypoint,
            "diffd",
            "remote-change",
            "remove",
            sample_path,
            "--execute",
            "--json",
        ]
    first_command = commands[0] if commands else {}
    first_target_status = "ready" if commands else "missing"
    target_check, target_issues = _real_transfer_target_confirmation(args, service, commands)
    issues.extend(target_issues)
    consume_check = _real_transfer_policy_check(
        args,
        "consume_policy",
        "queue/change consumption policy",
        "reviewer must approve whether records are consumed, retained, or rolled back on failure",
    )
    timeout_check = _real_transfer_policy_check(
        args,
        "timeout_policy",
        "timeout/process cleanup policy",
        "fake-rclone timeout cleanup exists; real transfer behavior still needs explicit approval",
    )
    checklist = [
        shadow_check,
        {
            "name": "real transfer preview command",
            "status": "ok" if commands else "pending",
            "detail": " ".join(preview_command),
        },
        target_check,
        consume_check,
        timeout_check,
        {
            "name": "parallel dangerous gates",
            "status": "ok",
            "detail": "fswatch resident mode, pCloud API long-poll, launchd changes, and archive work stay out of scope",
        },
    ]
    details: dict[str, object] = {
        "planned action": f"check {service.name} real {direction} transfer gate prerequisites",
        "implementation status": "read-only checklist; rclone is not executed",
        "real transfer gate status": "closed",
        "state writes": "none",
        "dev mode": "on" if paths.dev_mode else "off",
        "plan summary": plan_summary,
        "core dir": str(load_result.config.core_dir),
        "core remote": load_result.config.core_remote,
        "preview command": preview_command,
        "check command": check_command,
        "sample path": sample_path,
        "sample path status": "ready" if sample_ready else "not planned",
        "sample path detail": sample_skip_detail or "will be planned after setup",
        "dev-state sample setup command": setup_command,
        "dev-state sample cleanup command": cleanup_command,
        "review command sequence": [setup_command, preview_command, check_command, cleanup_command],
        "expected after sample setup": {
            "first planned transfer status": "ready" if sample_ready else "missing",
            f"planned {direction}s": 1 if sample_ready else 0,
            "real transfer gate status": "closed",
            "state writes": "none",
        },
        "first planned transfer status": first_target_status,
        "first planned transfer": first_command,
        "operator confirmed path": target_check.get("confirmed path", "-"),
        "operator confirmed direction": target_check.get("confirmed direction", "-"),
        "operator target confirmation status": target_check.get("status", "-"),
        "consume policy": consume_policy or "-",
        "consume policy status": consume_check.get("status", "-"),
        "timeout policy": timeout_policy or "-",
        "timeout policy status": timeout_check.get("status", "-"),
        "planned transfer commands": commands,
        "manual review transfer record details": _plan_records(manual_review_records),
        "preflight checks": checklist,
        **counts,
    }
    issues = _sort_issues(issues)
    return CommandReport(
        command=f"{service.name} transfer check",
        status=_status_from_issues(issues),
        summary=f"{service.name} real transfer gate checklist is not open",
        details=details,
        issues=_report_issues(issues),
        actions=_service_actions(paths, service),
    )


def _service_transfer_report(
    paths: RuntimePaths,
    service: ServiceDefinition,
    *,
    transfer_command: str,
    execute: bool = False,
) -> CommandReport:
    load_result = load_config(paths)
    state = read_service_daemon_state(load_result.config, service.name)
    issues = list(load_result.issues) + list(state.issues)
    if service.name == "pushd":
        plan, scope = build_pushd_plan(load_result.config, state)
        issues.extend(plan.issues)
        issues.extend(scope_issues(scope))
        records, manual_review_records = _filter_manual_review_transfers(
            plan.upload_records,
            _opposite_transfer_candidates(load_result.config, service),
        )
        counts = {
            "planned uploads": len(records),
            "manual review transfer records": len(manual_review_records),
            "excluded queue items": plan.excluded_count,
            "invalid queue items": plan.invalid_count,
        }
        preview_summary = "pushd upload transfer preview is ready"
        run_preview_summary = "pushd upload transfer run preview is ready"
        executed_summary = "pushd upload transfer executed with fake-rclone"
        planned_action = "preview pushd upload executor commands"
    else:
        daemon_state = read_daemon_state(load_result.config)
        issues.extend(daemon_state.issues)
        plan = build_diffd_plan(load_result.config, state, daemon_state)
        issues.extend(plan.issues)
        records, manual_review_records = _filter_manual_review_transfers(
            plan.download_records,
            _opposite_transfer_candidates(load_result.config, service),
        )
        counts = {
            "planned downloads": len(records),
            "manual review transfer records": len(manual_review_records),
            "remote changes": plan.remote_change_count,
            "pending downloads": plan.pending_download_count,
            "skipped download records": plan.skipped_count,
        }
        preview_summary = "diffd download transfer preview is ready"
        run_preview_summary = "diffd download transfer run preview is ready"
        executed_summary = "diffd download transfer executed with fake-rclone"
        planned_action = "preview diffd download executor commands"

    manual_review_issue = _manual_review_issue(service, len(manual_review_records))
    if manual_review_issue:
        issues.append(manual_review_issue)

    execution_issue: ConfigIssue | None = None
    rclone_bin: str | None = None
    transfer_results: list[dict[str, object]] = []
    transfer_state_file: Path | None = None
    if execute:
        execution_issue = _transfer_fake_rclone_issue(
            paths, load_result.config, f"{service.name} transfer run"
        )
        if execution_issue:
            issues.append(execution_issue)
        else:
            rclone_bin = str(Path(load_result.config.rclone_bin).expanduser().resolve(strict=True))

    commands = _transfer_command_records(load_result.config, service, records, rclone_bin=rclone_bin)
    if execute and not execution_issue and not _has_errors(issues):
        transfer_results, execution_issues = _execute_transfer_commands(
            commands,
            timeout_seconds=load_result.config.transfer_exec_timeout_seconds,
        )
        issues.extend(execution_issues)
        transfer_state_file = _record_transfer_execution_state(state, service, commands, transfer_results)

    if transfer_command == "preview":
        implementation_status = "transfer command preview only; rclone is not executed"
        summary = preview_summary
    elif execute and not _has_errors(issues):
        implementation_status = (
            "dev-mode fake-rclone transfer execution only; real rclone and pCloud transfer are not permitted"
        )
        summary = executed_summary
    elif execute and transfer_results:
        implementation_status = (
            "dev-mode fake-rclone transfer execution failed; real rclone and pCloud transfer are not permitted"
        )
        summary = f"{service.name} transfer execution failed"
    elif execute:
        implementation_status = "transfer execution refused before rclone start"
        summary = f"{service.name} transfer execution refused"
    else:
        implementation_status = "transfer run preview only; rclone is not executed"
        summary = run_preview_summary

    if transfer_state_file:
        state_writes: object = str(transfer_state_file)
    else:
        state_writes = "none"

    details: dict[str, object] = {
        "planned action": planned_action,
        "implementation status": implementation_status,
        "real transfer gate status": "closed",
        "execution gate": (
            "open: dev-fake-rclone"
            if execute and not execution_issue
            else "closed: requires PCLOUD_TOOLS_TRANSFER_EXECUTION_GATE=dev-fake-rclone"
        ),
        "state writes": state_writes,
        "transfer timeout seconds": load_result.config.transfer_exec_timeout_seconds,
        "core dir": str(load_result.config.core_dir),
        "core remote": load_result.config.core_remote,
        "planned transfer commands": commands,
        "manual review transfer record details": _plan_records(manual_review_records),
        **counts,
    }
    if transfer_command == "preview":
        details["gate status"] = "closed"
    if execute:
        details["transfer results"] = transfer_results
    issues = _sort_issues(issues)
    return CommandReport(
        command=f"{service.name} transfer {transfer_command}",
        status=_status_from_issues(issues),
        summary=summary,
        details=details,
        issues=_report_issues(issues),
        actions=_service_actions(paths, service),
    )


def cmd_service_transfer(
    args: argparse.Namespace, paths: RuntimePaths, service: ServiceDefinition
) -> int | None:
    if args.transfer_command == "preview":
        report = _service_transfer_report(paths, service, transfer_command="preview")
        _print_transfer_preview_report(report, args)
        return _exit_code_for_report(report)
    if args.transfer_command == "check":
        report = _real_transfer_check_report(args, paths, service)
        _print_transfer_check_report(report, args)
        return _exit_code_for_report(report)
    if args.transfer_command == "run":
        report = _service_transfer_report(
            paths,
            service,
            transfer_command="run",
            execute=getattr(args, "execute", False),
        )
        _print_report(report, args)
        return _exit_code_for_report(report)
    if args.transfer_command == "consume" and getattr(args, "consume_command", None) == "preview":
        report = _consume_preview_report(paths, service)
        _print_transfer_consume_report(report, args)
        return _exit_code_for_report(report)
    if args.transfer_command == "consume" and getattr(args, "consume_command", None) == "run":
        report = _consume_run_report(args, paths, service)
        _print_transfer_consume_report(report, args)
        return _exit_code_for_report(report)
    return None


def _dev_execute_issue(paths: RuntimePaths, config: AppConfig, command: str) -> ConfigIssue | None:
    if not paths.dev_mode:
        return ConfigIssue(
            key="PCLOUD_TOOLS_DEV_EXECUTION",
            level="error",
            message=f"refusing --execute for `{command}` outside pcloud-tools dev mode",
        )
    expected_state_root = (paths.workspace_root / ".dev-state" / "state").resolve()
    actual_state_dir = config.state_dir.resolve()
    if actual_state_dir != expected_state_root and not actual_state_dir.is_relative_to(
        expected_state_root
    ):
        return ConfigIssue(
            key="PCLOUD_TOOLS_DEV_STATE_DIR",
            level="error",
            message=(
                f"refusing --execute for `{command}` outside dev state dir: "
                f"{actual_state_dir} is not under {expected_state_root}"
            ),
        )
    return None


def _state_update_issue(issue: ConfigIssue | None) -> ConfigIssue | None:
    if not issue:
        return None
    return ConfigIssue(key=issue.key, level="error", message=issue.message)


def _plan_record_from_args(args: argparse.Namespace, default_action: str, key: str) -> tuple[PlanRecord, list[ConfigIssue]]:
    issues: list[ConfigIssue] = []
    path = normalize_plan_path(getattr(args, "path", ""))
    action = str(getattr(args, "action", default_action) or default_action).strip()
    reason = str(getattr(args, "reason", "manual") or "manual").strip()
    if not path:
        issues.append(ConfigIssue(key=key, level="error", message="path must be a relative path"))
    if not action:
        issues.append(ConfigIssue(key=f"{key}_ACTION", level="error", message="action is required"))
    if not reason:
        reason = "manual"
    return PlanRecord(path=path, action=action or default_action, reason=reason), issues


def _pushd_queue_report(args: argparse.Namespace, paths: RuntimePaths) -> CommandReport:
    load_result = load_config(paths)
    state = read_service_daemon_state(load_result.config, "pushd")
    plan, scope = build_pushd_plan(load_result.config, state)
    execute = getattr(args, "execute", False)
    issues = list(load_result.issues) + list(state.issues) + list(plan.issues) + scope_issues(scope)

    if args.queue_command == "add":
        record, record_issues = _plan_record_from_args(args, "upload", "PCLOUD_TOOLS_PUSHD_QUEUE_PATH")
        issues.extend(record_issues)
        planned_action = "append pushd queue record" if execute else "preview append pushd queue record"
        after_count = plan.total + 1
        details: dict[str, object] = {
            "planned action": planned_action,
            "queue file": str(state.queue_file),
            "queue items before": plan.total,
            "queue items after": after_count,
            "path": record.path,
            "action": record.action,
            "reason": record.reason,
        }
        if execute:
            dev_issue = _dev_execute_issue(paths, load_result.config, "pushd queue add")
            if dev_issue:
                issues.append(dev_issue)
            if not _has_errors(issues):
                result = append_plan_record(state.queue_file, "PCLOUD_TOOLS_PUSHD_QUEUE", record)
                update_issue = _state_update_issue(result.issue)
                if update_issue:
                    issues.append(update_issue)
                details["queue items before"] = result.before_count
                details["queue items after"] = result.after_count
        summary = "pushd queue record appended" if execute and not _has_errors(issues) else "pushd queue add preview is ready"
    elif args.queue_command == "clear":
        planned_action = "clear pushd queue" if execute else "preview clear pushd queue"
        details = {
            "planned action": planned_action,
            "queue file": str(state.queue_file),
            "queue items before": plan.total,
            "queue items after": 0,
        }
        if execute:
            dev_issue = _dev_execute_issue(paths, load_result.config, "pushd queue clear")
            if dev_issue:
                issues.append(dev_issue)
            if not _has_errors(issues):
                result = clear_plan_records(state.queue_file, "PCLOUD_TOOLS_PUSHD_QUEUE")
                update_issue = _state_update_issue(result.issue)
                if update_issue:
                    issues.append(update_issue)
                details["queue items before"] = result.before_count
                details["queue items after"] = result.after_count
        summary = "pushd queue cleared" if execute and not _has_errors(issues) else "pushd queue clear preview is ready"
    elif args.queue_command == "remove":
        record, record_issues = _plan_record_from_args(args, "upload", "PCLOUD_TOOLS_PUSHD_QUEUE_PATH")
        issues.extend(record_issues)
        planned_action = "remove pushd queue records" if execute else "preview remove pushd queue records"
        result = (
            remove_plan_records(state.queue_file, "PCLOUD_TOOLS_PUSHD_QUEUE", record.path, write=False)
            if not record_issues
            else None
        )
        if result and result.issue:
            update_issue = _state_update_issue(result.issue)
            if update_issue:
                issues.append(update_issue)
        details = {
            "planned action": planned_action,
            "queue file": str(state.queue_file),
            "queue items before": result.before_count if result else plan.total,
            "queue items after": result.after_count if result else plan.total,
            "queue items removed": (result.before_count - result.after_count) if result else 0,
            "path": record.path,
        }
        if execute:
            dev_issue = _dev_execute_issue(paths, load_result.config, "pushd queue remove")
            if dev_issue:
                issues.append(dev_issue)
            if not _has_errors(issues):
                result = remove_plan_records(state.queue_file, "PCLOUD_TOOLS_PUSHD_QUEUE", record.path)
                update_issue = _state_update_issue(result.issue)
                if update_issue:
                    issues.append(update_issue)
                details["queue items before"] = result.before_count
                details["queue items after"] = result.after_count
                details["queue items removed"] = result.before_count - result.after_count
        summary = (
            "pushd queue records removed"
            if execute and not _has_errors(issues)
            else "pushd queue remove preview is ready"
        )
    else:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_PUSHD_QUEUE_COMMAND",
                level="error",
                message="queue command must be add, clear, or remove",
            )
        )
        details = {"planned action": "none", "queue file": str(state.queue_file)}
        summary = "pushd queue command is invalid"

    if _has_errors(issues):
        summary = "pushd queue cannot be updated until issues are resolved"
    issues = _sort_issues(issues)
    return CommandReport(
        command=f"pushd queue {args.queue_command or ''}".strip(),
        status=_status_from_issues(issues),
        summary=summary,
        details=details,
        issues=_report_issues(issues),
        actions=_service_actions(paths, _SERVICES["pushd"]),
    )


def cmd_pushd_queue(args: argparse.Namespace, paths: RuntimePaths) -> int:
    report = _pushd_queue_report(args, paths)
    _print_report(report, args)
    return _exit_code_for_report(report)


def _diffd_remote_change_report(args: argparse.Namespace, paths: RuntimePaths) -> CommandReport:
    load_result = load_config(paths)
    state = read_service_daemon_state(load_result.config, "diffd")
    daemon_state = read_daemon_state(load_result.config)
    plan = build_diffd_plan(load_result.config, state, daemon_state)
    execute = getattr(args, "execute", False)
    issues = list(load_result.issues) + list(state.issues) + list(daemon_state.issues) + list(plan.issues)

    if args.remote_change_command == "add":
        record, record_issues = _plan_record_from_args(
            args, "download", "PCLOUD_TOOLS_DIFFD_REMOTE_CHANGE_PATH"
        )
        issues.extend(record_issues)
        planned_action = (
            "append diffd remote-change record" if execute else "preview append diffd remote-change record"
        )
        details: dict[str, object] = {
            "planned action": planned_action,
            "remote changes file": str(plan.remote_changes_file),
            "remote changes before": plan.remote_change_count,
            "remote changes after": plan.remote_change_count + 1,
            "path": record.path,
            "action": record.action,
            "reason": record.reason,
        }
        if execute:
            dev_issue = _dev_execute_issue(paths, load_result.config, "diffd remote-change add")
            if dev_issue:
                issues.append(dev_issue)
            if not _has_errors(issues):
                result = append_plan_record(
                    plan.remote_changes_file, "PCLOUD_TOOLS_DIFFD_REMOTE_CHANGES", record
                )
                update_issue = _state_update_issue(result.issue)
                if update_issue:
                    issues.append(update_issue)
                details["remote changes before"] = result.before_count
                details["remote changes after"] = result.after_count
        summary = (
            "diffd remote-change record appended"
            if execute and not _has_errors(issues)
            else "diffd remote-change add preview is ready"
        )
    elif args.remote_change_command == "clear":
        planned_action = "clear diffd remote changes" if execute else "preview clear diffd remote changes"
        details = {
            "planned action": planned_action,
            "remote changes file": str(plan.remote_changes_file),
            "remote changes before": plan.remote_change_count,
            "remote changes after": 0,
        }
        if execute:
            dev_issue = _dev_execute_issue(paths, load_result.config, "diffd remote-change clear")
            if dev_issue:
                issues.append(dev_issue)
            if not _has_errors(issues):
                result = clear_plan_records(plan.remote_changes_file, "PCLOUD_TOOLS_DIFFD_REMOTE_CHANGES")
                update_issue = _state_update_issue(result.issue)
                if update_issue:
                    issues.append(update_issue)
                details["remote changes before"] = result.before_count
                details["remote changes after"] = result.after_count
        summary = (
            "diffd remote changes cleared"
            if execute and not _has_errors(issues)
            else "diffd remote-change clear preview is ready"
        )
    elif args.remote_change_command == "remove":
        record, record_issues = _plan_record_from_args(
            args, "download", "PCLOUD_TOOLS_DIFFD_REMOTE_CHANGE_PATH"
        )
        issues.extend(record_issues)
        planned_action = (
            "remove diffd remote-change records"
            if execute
            else "preview remove diffd remote-change records"
        )
        result = (
            remove_plan_records(
                plan.remote_changes_file,
                "PCLOUD_TOOLS_DIFFD_REMOTE_CHANGES",
                record.path,
                write=False,
            )
            if not record_issues
            else None
        )
        if result and result.issue:
            update_issue = _state_update_issue(result.issue)
            if update_issue:
                issues.append(update_issue)
        details = {
            "planned action": planned_action,
            "remote changes file": str(plan.remote_changes_file),
            "remote changes before": result.before_count if result else plan.remote_change_count,
            "remote changes after": result.after_count if result else plan.remote_change_count,
            "remote changes removed": (result.before_count - result.after_count) if result else 0,
            "path": record.path,
        }
        if execute:
            dev_issue = _dev_execute_issue(paths, load_result.config, "diffd remote-change remove")
            if dev_issue:
                issues.append(dev_issue)
            if not _has_errors(issues):
                result = remove_plan_records(
                    plan.remote_changes_file,
                    "PCLOUD_TOOLS_DIFFD_REMOTE_CHANGES",
                    record.path,
                )
                update_issue = _state_update_issue(result.issue)
                if update_issue:
                    issues.append(update_issue)
                details["remote changes before"] = result.before_count
                details["remote changes after"] = result.after_count
                details["remote changes removed"] = result.before_count - result.after_count
        summary = (
            "diffd remote-change records removed"
            if execute and not _has_errors(issues)
            else "diffd remote-change remove preview is ready"
        )
    else:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_DIFFD_REMOTE_CHANGE_COMMAND",
                level="error",
                message="remote-change command must be add, clear, or remove",
            )
        )
        details = {"planned action": "none", "remote changes file": str(plan.remote_changes_file)}
        summary = "diffd remote-change command is invalid"

    if _has_errors(issues):
        summary = "diffd remote changes cannot be updated until issues are resolved"
    issues = _sort_issues(issues)
    return CommandReport(
        command=f"diffd remote-change {args.remote_change_command or ''}".strip(),
        status=_status_from_issues(issues),
        summary=summary,
        details=details,
        issues=_report_issues(issues),
        actions=_service_actions(paths, _SERVICES["diffd"]),
    )


def cmd_diffd_remote_change(args: argparse.Namespace, paths: RuntimePaths) -> int:
    report = _diffd_remote_change_report(args, paths)
    _print_report(report, args)
    return _exit_code_for_report(report)


def _standalone_main(service_name: str, argv: list[str] | None = None) -> int:
    service = _SERVICES[service_name]
    parser = argparse.ArgumentParser(
        prog=f"pcloud-{service_name}",
        description=f"Development scaffold for {service.summary_name}.",
    )
    subparsers = parser.add_subparsers(dest="service_command")

    status_parser = subparsers.add_parser("status", help=service.status_help)
    status_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    status_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")

    preview_parser = subparsers.add_parser("preview", help=service.preview_help)
    preview_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    preview_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")

    run_parser = subparsers.add_parser("run", help=f"Preview a {service.name} one-shot dry run.")
    run_parser.add_argument("--execute", action="store_true")
    run_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    run_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")

    gate_parser = subparsers.add_parser(
        "gate", help=f"Check the read-only gate before real {service.name} implementation work."
    )
    gate_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    gate_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")

    if service.name == "pushd":
        fswatch_parser = subparsers.add_parser(
            "fswatch", help="Preview pushd fswatch fixture events without starting fswatch."
        )
        fswatch_subparsers = fswatch_parser.add_subparsers(dest="fswatch_command")
        fswatch_preview_parser = fswatch_subparsers.add_parser(
            "preview", help="Parse a fixture and preview the resulting upload plan."
        )
        fswatch_preview_parser.add_argument("--fixture", required=True, type=Path)
        fswatch_preview_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        fswatch_preview_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
        fswatch_probe_parser = fswatch_subparsers.add_parser(
            "probe", help="Preview the one-shot fswatch probe command without running it."
        )
        fswatch_probe_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        fswatch_probe_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")

        transfer_parser = subparsers.add_parser(
            "transfer", help="Preview upload executor commands without running transfers."
        )
        transfer_subparsers = transfer_parser.add_subparsers(dest="transfer_command")
        transfer_preview_parser = transfer_subparsers.add_parser(
            "preview", help="Emit planned upload commands from the current pushd plan."
        )
        transfer_preview_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        transfer_preview_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
        transfer_check_parser = transfer_subparsers.add_parser(
            "check", help="Read-only checklist for the real upload transfer gate."
        )
        transfer_check_parser.add_argument("--report-path", type=Path)
        transfer_check_parser.add_argument("--sample-path")
        transfer_check_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        transfer_check_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
        transfer_run_parser = transfer_subparsers.add_parser(
            "run", help="Run dev-mode fake-rclone upload executor commands behind the transfer gate."
        )
        transfer_run_parser.add_argument("--execute", action="store_true")
        transfer_run_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        transfer_run_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
        transfer_consume_parser = transfer_subparsers.add_parser(
            "consume", help="Preview queue consumption after successful transfer records."
        )
        transfer_consume_subparsers = transfer_consume_parser.add_subparsers(dest="consume_command")
        transfer_consume_preview_parser = transfer_consume_subparsers.add_parser("preview")
        transfer_consume_preview_parser.add_argument(
            "--json", action="store_true", help="Emit structured JSON output."
        )
        transfer_consume_preview_parser.add_argument(
            "--xbar", action="store_true", help="Emit xbar menu output."
        )

        queue_parser = subparsers.add_parser("queue", help="Preview or update pushd queue state.")
        queue_subparsers = queue_parser.add_subparsers(dest="queue_command")
        queue_add_parser = queue_subparsers.add_parser("add")
        queue_add_parser.add_argument("path")
        queue_add_parser.add_argument("--action", default="upload")
        queue_add_parser.add_argument("--reason", default="manual")
        queue_add_parser.add_argument("--execute", action="store_true")
        queue_add_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        queue_clear_parser = queue_subparsers.add_parser("clear")
        queue_clear_parser.add_argument("--execute", action="store_true")
        queue_clear_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        queue_remove_parser = queue_subparsers.add_parser("remove")
        queue_remove_parser.add_argument("path")
        queue_remove_parser.add_argument("--execute", action="store_true")
        queue_remove_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")

    if service.name == "diffd":
        diff_parser = subparsers.add_parser(
            "diff", help="Preview diffd pCloud diff fixture responses without calling the API."
        )
        diff_subparsers = diff_parser.add_subparsers(dest="diff_command")
        diff_preview_parser = diff_subparsers.add_parser(
            "preview", help="Parse a fixture and preview the resulting download plan."
        )
        diff_preview_parser.add_argument("--fixture", required=True, type=Path)
        diff_preview_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        diff_preview_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")

        api_poll_parser = subparsers.add_parser(
            "api-poll", help="Preview a one-shot pCloud API poll without calling the API."
        )
        api_poll_subparsers = api_poll_parser.add_subparsers(dest="api_poll_command")
        api_poll_preview_parser = api_poll_subparsers.add_parser(
            "preview", help="Report the intended one-shot API poll request shape."
        )
        api_poll_preview_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        api_poll_preview_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")

        transfer_parser = subparsers.add_parser(
            "transfer", help="Preview download executor commands without running transfers."
        )
        transfer_subparsers = transfer_parser.add_subparsers(dest="transfer_command")
        transfer_preview_parser = transfer_subparsers.add_parser(
            "preview", help="Emit planned download commands from the current diffd plan."
        )
        transfer_preview_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        transfer_preview_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
        transfer_check_parser = transfer_subparsers.add_parser(
            "check", help="Read-only checklist for the real download transfer gate."
        )
        transfer_check_parser.add_argument("--report-path", type=Path)
        transfer_check_parser.add_argument("--sample-path")
        transfer_check_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        transfer_check_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
        transfer_run_parser = transfer_subparsers.add_parser(
            "run", help="Run dev-mode fake-rclone download executor commands behind the transfer gate."
        )
        transfer_run_parser.add_argument("--execute", action="store_true")
        transfer_run_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        transfer_run_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
        transfer_consume_parser = transfer_subparsers.add_parser(
            "consume", help="Preview remote-change consumption after successful transfer records."
        )
        transfer_consume_subparsers = transfer_consume_parser.add_subparsers(dest="consume_command")
        transfer_consume_preview_parser = transfer_consume_subparsers.add_parser("preview")
        transfer_consume_preview_parser.add_argument(
            "--json", action="store_true", help="Emit structured JSON output."
        )
        transfer_consume_preview_parser.add_argument(
            "--xbar", action="store_true", help="Emit xbar menu output."
        )
        transfer_consume_run_parser = transfer_consume_subparsers.add_parser("run")
        transfer_consume_run_parser.add_argument("--execute", action="store_true")
        transfer_consume_run_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        transfer_consume_run_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")

        remote_parser = subparsers.add_parser(
            "remote-change", help="Preview or update diffd remote change state."
        )
        remote_subparsers = remote_parser.add_subparsers(dest="remote_change_command")
        remote_add_parser = remote_subparsers.add_parser("add")
        remote_add_parser.add_argument("path")
        remote_add_parser.add_argument("--action", default="download")
        remote_add_parser.add_argument("--reason", default="manual")
        remote_add_parser.add_argument("--execute", action="store_true")
        remote_add_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        remote_clear_parser = remote_subparsers.add_parser("clear")
        remote_clear_parser.add_argument("--execute", action="store_true")
        remote_clear_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        remote_remove_parser = remote_subparsers.add_parser("remove")
        remote_remove_parser.add_argument("path")
        remote_remove_parser.add_argument("--execute", action="store_true")
        remote_remove_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")

    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    args.command = service.name
    args.service_name = service.name
    paths = detect_runtime_paths()
    result = cmd_service_daemon(args, paths)
    if result is not None:
        return result
    parser.print_help()
    return 1


def main_pushd(argv: list[str] | None = None) -> int:
    return _standalone_main("pushd", argv)


def main_diffd(argv: list[str] | None = None) -> int:
    return _standalone_main("diffd", argv)
