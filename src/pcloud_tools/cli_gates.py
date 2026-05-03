from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from .cli_archive import _old_monolith_gate_report
from .cli_service_daemon import _diffd_api_long_poll_gate_report, _pushd_fswatch_resident_gate_report
from .cli_sync import _sync_autosync_gate_report, _sync_migration_gate_report
from .output import CommandReport, ReportIssue, render_report
from .runtime import RuntimePaths


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


def _gate_item(name: str, report: CommandReport, gate_keys: tuple[str, ...], can_keys: tuple[str, ...], approval_keys: tuple[str, ...]) -> dict[str, Any]:
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
    }


def _gates_status_report(args: argparse.Namespace, paths: RuntimePaths) -> CommandReport:
    gate_args = _approval_namespace(args)
    reports = [
        _gate_item(
            "pushd fswatch resident",
            _pushd_fswatch_resident_gate_report(gate_args, paths),
            ("resident gate status",),
            ("resident can start",),
            ("resident approval status",),
        ),
        _gate_item(
            "diffd pCloud API long-poll",
            _diffd_api_long_poll_gate_report(gate_args, paths),
            ("long-poll gate status",),
            ("long-poll can start",),
            ("long-poll approval status",),
        ),
        _gate_item(
            "sync autosync launchd",
            _sync_autosync_gate_report(gate_args, paths),
            ("launchd gate status",),
            ("autosync changes can run",),
            ("autosync approval status",),
        ),
        _gate_item(
            "sync migration validation",
            _sync_migration_gate_report(gate_args, paths),
            ("migration gate status",),
            ("sync/resync can run",),
            ("migration approval status",),
        ),
        _gate_item(
            "old monolith archive",
            _old_monolith_gate_report(gate_args, paths),
            ("archive gate status",),
            ("archive can run",),
            ("archive approval status",),
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
        "report path": str(getattr(args, "report_path", None) or "-"),
        "sync status report": str(getattr(args, "sync_status_report_path", None) or "-"),
        "backup dir": str(getattr(args, "backup_dir", None) or "-"),
        "gate count": len(reports),
        "complete read-only approvals": ready_read_only,
        "pending gates": len(reports) - ready_read_only,
        "gates": reports,
        "blockers": blockers,
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
                f"blockers={blocker_text}"
            )
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
