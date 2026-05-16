from __future__ import annotations

import argparse

from ..cli_common import output_format, print_report, shell_command
from ..output import CommandReport


def render_fswatch_resident_gate_human(report: CommandReport) -> str:
    details = report.details
    lines = [
        f"{report.command}: {report.status}",
        report.summary,
        f"resident gate: {details.get('resident gate status', '-')}",
        f"resident can start: {details.get('resident can start', '-')}",
        f"state writes: {details.get('state writes', '-')}",
        f"watch root: {details.get('watch root', '-')}",
        (
            "scope: "
            f"{details.get('scope status', '-')}; "
            f"{details.get('scope baseline', '-')}; "
            f"entries={details.get('scope entries', '-')}"
        ),
        (
            "fswatch: "
            f"{details.get('fswatch availability', '-')} "
            f"({details.get('fswatch binary', '-')})"
        ),
        f"approval status: {details.get('resident approval status', '-')}",
        f"human gate: {details.get('human gate status', '-')}",
        f"next human check trigger: {details.get('next human check trigger', '-')}",
    ]
    resident_command = details.get("resident command preview")
    if resident_command:
        lines.append(f"resident command preview: {shell_command(resident_command)}")
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


def print_fswatch_resident_gate_report(report: CommandReport, args: argparse.Namespace) -> None:
    if output_format(args) == "human":
        print(render_fswatch_resident_gate_human(report))
        return
    print_report(report, args)


def render_fswatch_resident_run_human(report: CommandReport) -> str:
    details = report.details
    lines = [
        f"{report.command}: {report.status}",
        report.summary,
        f"resident run gate: {details.get('resident run gate status', '-')}",
        f"resident can start: {details.get('resident can start', '-')}",
        f"execute requested: {details.get('execute requested', '-')}",
        f"state writes: {details.get('state writes', '-')}",
        f"watch root: {details.get('watch root', '-')}",
        (
            "fswatch: "
            f"{details.get('fswatch availability', '-')} "
            f"({details.get('fswatch binary', '-')})"
        ),
        f"approval status: {details.get('resident approval status', '-')}",
        f"events processed: {details.get('events processed', '-')}",
        f"queue records appended: {details.get('queue records appended', '-')}",
        f"duplicate events skipped: {details.get('duplicate events skipped', '-')}",
        f"debounce events skipped: {details.get('debounce events skipped', '-')}",
        f"queue limit skips: {details.get('queue limit skips', '-')}",
        f"excluded events: {details.get('excluded events', '-')}",
        f"invalid events: {details.get('invalid events', '-')}",
    ]
    command = details.get("resident command preview")
    if command:
        lines.append(f"resident command: {shell_command(command)}")
    if details.get("resident state file"):
        lines.append(f"resident state: {details.get('resident state file')}")
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


def print_fswatch_resident_run_report(report: CommandReport, args: argparse.Namespace) -> None:
    if output_format(args) == "human":
        print(render_fswatch_resident_run_human(report))
        return
    print_report(report, args)
