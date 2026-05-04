from __future__ import annotations

import argparse
import shlex
from pathlib import Path
from typing import Any

from .cli_archive import _old_monolith_gate_report
from .cli_service_daemon import _diffd_api_long_poll_gate_report, _pushd_fswatch_resident_gate_report
from .cli_sync import _sync_autosync_gate_report, _sync_migration_gate_report
from .output import CommandReport, ReportIssue, render_report
from .runtime import RuntimePaths


def _shell_join(value: object) -> str:
    if isinstance(value, (list, tuple)):
        return " ".join(str(part) for part in value)
    return str(value)


def _command_example(env_assignment: str, parts: list[str]) -> str:
    return f"{env_assignment} {shlex.join(parts)}"


def _arg_path(args: argparse.Namespace, name: str, default: str) -> str:
    value = getattr(args, name, None)
    return str(value) if value else default


def _read_only_command_examples(args: argparse.Namespace) -> dict[str, list[str]]:
    report_path = _arg_path(args, "report_path", ".dev-state/reports/shadow-validation.json")
    sync_status_report_path = _arg_path(args, "sync_status_report_path", ".dev-state/reports/sync-status.json")
    backup_dir = _arg_path(args, "backup_dir", ".dev-state/cutover-backups/<timestamp>")
    fixture = ".dev-state/fixtures/pcloud-diff-response.json"
    manager = "./pcloud-manager-dev"
    return {
        "pushd fswatch resident": [
            _command_example(
                "PCLOUD_TOOLS_PUSHD_FSWATCH_RESIDENT_GATE=operator-approved-fswatch-resident-v1",
                [
                    manager,
                    "pushd",
                    "fswatch",
                    "resident-run",
                    "--report-path",
                    report_path,
                    "--operator-reviewed-probe",
                    "--reviewer-approved-queue-policy",
                    "--reviewer-approved-process-policy",
                ],
            )
        ],
        "diffd pCloud API long-poll": [
            _command_example(
                "PCLOUD_TOOLS_DIFFD_API_LONG_POLL_GATE=operator-approved-api-long-poll-v1",
                [
                    manager,
                    "diffd",
                    "api-poll",
                    "long-poll-run",
                    "--report-path",
                    report_path,
                    "--fixture",
                    fixture,
                    "--operator-reviewed-preview",
                    "--reviewer-approved-response-policy",
                    "--reviewer-approved-credential-policy",
                    "--reviewer-approved-process-policy",
                ],
            ),
            _command_example(
                "PCLOUD_TOOLS_DIFFD_API_LONG_POLL_GATE=operator-approved-api-long-poll-v1",
                [
                    manager,
                    "diffd",
                    "api-poll",
                    "long-poll-run",
                    "--report-path",
                    report_path,
                    "--live-api",
                    "--max-iterations",
                    "1",
                    "--operator-reviewed-preview",
                    "--reviewer-approved-response-policy",
                    "--reviewer-approved-credential-policy",
                    "--reviewer-approved-process-policy",
                ],
            ),
        ],
        "sync autosync launchd": [
            _command_example(
                "PCLOUD_TOOLS_AUTOSYNC_LAUNCHD_GATE=operator-approved-autosync-launchd-v1",
                [
                    manager,
                    "sync",
                    "autosync-run",
                    mode,
                    "--report-path",
                    report_path,
                    "--operator-reviewed-preview",
                    "--reviewer-approved-plist",
                    "--reviewer-approved-launchctl-policy",
                    "--reviewer-approved-rollback-policy",
                ],
            )
            for mode in ("enable", "disable")
        ],
        "sync migration validation": [
            _command_example(
                "PCLOUD_TOOLS_SYNC_MIGRATION_GATE=operator-approved-sync-migration-v1",
                [
                    manager,
                    "sync",
                    "migration-run",
                    mode,
                    "--report-path",
                    report_path,
                    "--sync-status-report-path",
                    sync_status_report_path,
                    "--operator-reviewed-status",
                    "--reviewer-approved-scope",
                    "--reviewer-approved-rollback-policy",
                    "--reviewer-approved-stop-conditions",
                ],
            )
            for mode in ("normal", "resync")
        ],
        "old monolith archive": [
            _command_example(
                "PCLOUD_TOOLS_OLD_MONOLITH_ARCHIVE_GATE=operator-approved-old-monolith-archive-v1",
                [
                    manager,
                    "archive",
                    "old-monolith-run",
                    "--backup-dir",
                    backup_dir,
                    "--operator-reviewed-current-wrapper",
                    "--reviewer-approved-backup-source",
                    "--reviewer-approved-rollback-policy",
                    "--reviewer-approved-archive-target",
                ],
            )
        ],
    }


def add_gates_parser(subparsers: argparse._SubParsersAction) -> None:
    gates_parser = subparsers.add_parser("gates", help="Summarize remaining human gates without executing them.")
    gates_subparsers = gates_parser.add_subparsers(dest="gates_command")
    status_parser = gates_subparsers.add_parser("status", help="Show a concise read-only gate summary.")
    status_parser.add_argument("--report-path", type=Path)
    status_parser.add_argument("--sync-status-report-path", type=Path)
    status_parser.add_argument("--backup-dir", type=Path)
    status_parser.add_argument(
        "--assume-read-only-approvals",
        action="store_true",
        help="Summarize as if the existing read-only reviewer/operator approval flags were provided.",
    )
    status_parser.add_argument(
        "--show-command-examples",
        action="store_true",
        help="Show read-only guarded run review commands without --execute.",
    )
    status_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    status_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")


def cmd_gates(args: argparse.Namespace, paths: RuntimePaths) -> int | None:
    if args.gates_command == "status":
        return cmd_gates_status(args, paths)
    return None


def _approval_namespace(args: argparse.Namespace) -> argparse.Namespace:
    approved = bool(getattr(args, "assume_read_only_approvals", False))
    return argparse.Namespace(
        report_path=getattr(args, "report_path", None),
        sync_status_report_path=getattr(args, "sync_status_report_path", None),
        backup_dir=getattr(args, "backup_dir", None),
        operator_reviewed_probe=approved,
        reviewer_approved_queue_policy=approved,
        reviewer_approved_process_policy=approved,
        operator_reviewed_preview=approved,
        reviewer_approved_response_policy=approved,
        reviewer_approved_credential_policy=approved,
        reviewer_approved_plist=approved,
        reviewer_approved_launchctl_policy=approved,
        reviewer_approved_rollback_policy=approved,
        operator_reviewed_status=approved,
        reviewer_approved_scope=approved,
        reviewer_approved_stop_conditions=approved,
        operator_reviewed_current_wrapper=approved,
        reviewer_approved_backup_source=approved,
        reviewer_approved_archive_target=approved,
    )


def _check_blockers(report: CommandReport) -> list[str]:
    checks = report.details.get("preflight checks")
    if not isinstance(checks, list):
        return []
    blockers: list[str] = []
    for check in checks:
        if not isinstance(check, dict):
            continue
        if check.get("status") != "ok":
            blockers.append(str(check.get("name", "-")))
    return blockers


def _first_detail(details: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = details.get(key)
        if value is not None:
            return str(value)
    return "-"


def _gate_item(
    name: str,
    report: CommandReport,
    gate_keys: tuple[str, ...],
    can_keys: tuple[str, ...],
    approval_keys: tuple[str, ...],
    *,
    run_command: tuple[str, ...],
    execution_gate_env: str,
    run_scope: str,
    read_only_review_commands: list[str],
) -> dict[str, Any]:
    details = report.details
    blockers = _check_blockers(report)
    return {
        "name": name,
        "summary": report.summary,
        "report status": report.status,
        "gate status": _first_detail(details, gate_keys),
        "can run": _first_detail(details, can_keys),
        "approval status": _first_detail(details, approval_keys),
        "human gate": str(details.get("human gate status", "-")),
        "next human check trigger": str(details.get("next human check trigger", "-")),
        "state writes": str(details.get("state writes", "-")),
        "blockers": blockers,
        "guarded run path": "available",
        "run command": list(run_command),
        "execution gate env": execution_gate_env,
        "run scope": run_scope,
        "read-only review commands": read_only_review_commands,
    }


def _gates_status_report(args: argparse.Namespace, paths: RuntimePaths) -> CommandReport:
    gate_args = _approval_namespace(args)
    command_examples = _read_only_command_examples(args)
    reports = [
        _gate_item(
            "pushd fswatch resident",
            _pushd_fswatch_resident_gate_report(gate_args, paths),
            ("resident gate status",),
            ("resident can start",),
            ("resident approval status",),
            run_command=("pushd", "fswatch", "resident-run"),
            execution_gate_env="PCLOUD_TOOLS_PUSHD_FSWATCH_RESIDENT_GATE=operator-approved-fswatch-resident-v1",
            run_scope="foreground fswatch event-to-queue loop; no upload transfer",
            read_only_review_commands=command_examples["pushd fswatch resident"],
        ),
        _gate_item(
            "diffd pCloud API long-poll",
            _diffd_api_long_poll_gate_report(gate_args, paths),
            ("long-poll gate status",),
            ("long-poll can start",),
            ("long-poll approval status",),
            run_command=("diffd", "api-poll", "long-poll-run"),
            execution_gate_env="PCLOUD_TOOLS_DIFFD_API_LONG_POLL_GATE=operator-approved-api-long-poll-v1",
            run_scope="fixture-backed diff response processing; live API still requires separate approval",
            read_only_review_commands=command_examples["diffd pCloud API long-poll"],
        ),
        _gate_item(
            "sync autosync launchd",
            _sync_autosync_gate_report(gate_args, paths),
            ("launchd gate status",),
            ("autosync changes can run",),
            ("autosync approval status",),
            run_command=("sync", "autosync-run", "enable|disable"),
            execution_gate_env="PCLOUD_TOOLS_AUTOSYNC_LAUNCHD_GATE=operator-approved-autosync-launchd-v1",
            run_scope="launchctl enable/bootstrap or bootout/disable only; no direct sync run",
            read_only_review_commands=command_examples["sync autosync launchd"],
        ),
        _gate_item(
            "sync migration validation",
            _sync_migration_gate_report(gate_args, paths),
            ("migration gate status",),
            ("sync/resync can run",),
            ("migration approval status",),
            run_command=("sync", "migration-run", "normal|resync"),
            execution_gate_env="PCLOUD_TOOLS_SYNC_MIGRATION_GATE=operator-approved-sync-migration-v1",
            run_scope="approved rclone bisync validation command only; no launchd/listing-cache mutation",
            read_only_review_commands=command_examples["sync migration validation"],
        ),
        _gate_item(
            "old monolith archive",
            _old_monolith_gate_report(gate_args, paths),
            ("archive gate status",),
            ("archive can run",),
            ("archive approval status",),
            run_command=("archive", "old-monolith-run"),
            execution_gate_env="PCLOUD_TOOLS_OLD_MONOLITH_ARCHIVE_GATE=operator-approved-old-monolith-archive-v1",
            run_scope="copy selected backup into dev archive and write manifest; no wrapper modification",
            read_only_review_commands=command_examples["old monolith archive"],
        ),
    ]
    ready_read_only = sum(1 for item in reports if item["approval status"] == "complete-read-only")
    blockers = {
        item["name"]: item["blockers"]
        for item in reports
        if item["blockers"]
    }
    details: dict[str, Any] = {
        "planned action": "summarize remaining human gates",
        "implementation status": "read-only aggregate; no gated operation is executed",
        "state writes": "none",
        "assume read-only approvals": "yes" if getattr(args, "assume_read_only_approvals", False) else "no",
        "show command examples": "yes" if getattr(args, "show_command_examples", False) else "no",
        "report path": str(getattr(args, "report_path", None) or "-"),
        "sync status report": str(getattr(args, "sync_status_report_path", None) or "-"),
        "backup dir": str(getattr(args, "backup_dir", None) or "-"),
        "gate count": len(reports),
        "complete read-only approvals": ready_read_only,
        "pending gates": len(reports) - ready_read_only,
        "gates": reports,
        "blockers": blockers,
        "guarded run paths": {
            item["name"]: {
                "command": item["run command"],
                "execution gate env": item["execution gate env"],
                "scope": item["run scope"],
                "read-only review commands": item["read-only review commands"],
            }
            for item in reports
        },
        "read-only command examples": command_examples,
    }
    issues = [
        ReportIssue(
            level="warning",
            key="PCLOUD_TOOLS_GATES_REMAIN_CLOSED",
            message="remaining real-work gates are summarized only; no execution gate is open",
        )
    ]
    return CommandReport(
        command="gates status",
        status="warning",
        summary=f"{ready_read_only}/{len(reports)} gates complete-read-only; all execution gates closed",
        details=details,
        issues=issues,
    )


def _render_gates_human(report: CommandReport) -> str:
    details = report.details
    lines = [
        f"{report.command}: {report.status}",
        report.summary,
        f"state writes: {details.get('state writes', '-')}",
        f"assume read-only approvals: {details.get('assume read-only approvals', '-')}",
        "gates:",
    ]
    gates = details.get("gates")
    if isinstance(gates, list):
        for item in gates:
            if not isinstance(item, dict):
                continue
            blockers = item.get("blockers")
            blocker_text = ", ".join(blockers) if isinstance(blockers, list) and blockers else "-"
            lines.append(
                "- "
                f"{item.get('name', '-')}: "
                f"gate={item.get('gate status', '-')}; "
                f"can-run={item.get('can run', '-')}; "
                f"approval={item.get('approval status', '-')}; "
                f"human={item.get('human gate', '-')}; "
                f"blockers={blocker_text}; "
                f"run={_shell_join(item.get('run command', '-'))}"
            )
    if details.get("show command examples") == "yes":
        examples = details.get("read-only command examples")
        if isinstance(examples, dict) and examples:
            lines.append("read-only command examples:")
            for name, commands in examples.items():
                if not isinstance(commands, list):
                    continue
                for command in commands:
                    lines.append(f"- {name}: {command}")
    if report.issues:
        lines.append("warnings:")
        for issue in report.issues:
            lines.append(f"- {issue.key}: {issue.message}")
    return "\n".join(lines)


def cmd_gates_status(args: argparse.Namespace, paths: RuntimePaths) -> int:
    report = _gates_status_report(args, paths)
    output_format = "xbar" if getattr(args, "xbar", False) else "json" if getattr(args, "json", False) else "human"
    if output_format == "human":
        print(_render_gates_human(report))
    else:
        print(render_report(report, output_format=output_format))
    return 0
