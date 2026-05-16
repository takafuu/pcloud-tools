from __future__ import annotations

import argparse

from ..cli_common import output_format, print_report, shell_command
from ..output import CommandReport


def render_api_long_poll_gate_human(report: CommandReport) -> str:
    details = report.details
    lines = [
        f"{report.command}: {report.status}",
        report.summary,
        f"long-poll gate: {details.get('long-poll gate status', '-')}",
        f"long-poll can start: {details.get('long-poll can start', '-')}",
        f"state writes: {details.get('state writes', '-')}",
        f"remote root: {details.get('remote root', '-')}",
        f"current diffid: {details.get('current diffid', '-')}",
        f"poll interval seconds: {details.get('poll interval seconds', '-')}",
        f"batch limit: {details.get('batch limit', '-')}",
        (
            "scope: "
            f"{details.get('scope status', '-')}; "
            f"{details.get('scope baseline', '-')}; "
            f"entries={details.get('scope entries', '-')}"
        ),
        f"approval status: {details.get('long-poll approval status', '-')}",
        f"human gate: {details.get('human gate status', '-')}",
        f"next human check trigger: {details.get('next human check trigger', '-')}",
    ]
    preview_command = details.get("preview command")
    if preview_command:
        lines.append(f"preview command: {shell_command(preview_command)}")
    request_query = details.get("request query")
    if isinstance(request_query, dict):
        query = ", ".join(f"{key}={value}" for key, value in request_query.items())
        lines.append(
            f"request: {details.get('request method', '-')} {details.get('request path', '-')} ({query})"
        )
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


def print_api_long_poll_gate_report(report: CommandReport, args: argparse.Namespace) -> None:
    if output_format(args) == "human":
        print(render_api_long_poll_gate_human(report))
        return
    print_report(report, args)


def render_api_long_poll_run_human(report: CommandReport) -> str:
    details = report.details
    lines = [
        f"{report.command}: {report.status}",
        report.summary,
        f"long-poll run gate: {details.get('long-poll run gate status', '-')}",
        f"long-poll can start: {details.get('long-poll can start', '-')}",
        f"execute requested: {details.get('execute requested', '-')}",
        f"state writes: {details.get('state writes', '-')}",
        f"live API requested: {details.get('live API requested', '-')}",
        f"API credential source: {details.get('API credential source', '-')}",
        f"API token provided: {details.get('API token provided', '-')}",
        f"API request URL: {details.get('API request URL', '-')}",
        f"fixture: {details.get('fixture file', '-')}",
        f"current diffid: {details.get('current diffid', '-')}",
        f"new diffid: {details.get('new diffid', '-')}",
        f"approval status: {details.get('long-poll approval status', '-')}",
        f"parsed changes: {details.get('parsed diff changes', '-')}",
        f"download records appended: {details.get('download records appended', '-')}",
        f"skipped records: {details.get('skipped download records', '-')}",
        f"invalid changes: {details.get('invalid diff changes', '-')}",
        f"folder cache: {details.get('folder cache entries before', '-')} -> {details.get('folder cache entries after', '-')}",
    ]
    state_file = details.get("long-poll state file")
    if state_file:
        lines.append(f"long-poll state: {state_file}")
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


def print_api_long_poll_run_report(report: CommandReport, args: argparse.Namespace) -> None:
    if output_format(args) == "human":
        print(render_api_long_poll_run_human(report))
        return
    print_report(report, args)


def render_diffd_folder_cache_human(report: CommandReport) -> str:
    details = report.details
    lines = [
        f"{report.command}: {report.status}",
        report.summary,
        f"folder cache file: {details.get('folder cache file', '-')}",
        f"folder cache entries: {details.get('folder cache entries before', '-')} -> {details.get('folder cache entries after', '-')}",
        f"state writes: {details.get('state writes', '-')}",
    ]
    folder_id = details.get("folder id")
    if folder_id:
        lines.append(f"folder id: {folder_id}")
    path = details.get("path")
    if path:
        lines.append(f"path: {path}")
    previous_path = details.get("previous path")
    if previous_path:
        lines.append(f"previous path: {previous_path}")
    removed = details.get("folder cache entries removed")
    if removed is not None:
        lines.append(f"removed entries: {removed}")
    entries = details.get("entries")
    if isinstance(entries, list) and entries:
        lines.append("entries:")
        for entry in entries:
            if isinstance(entry, dict):
                lines.append(f"- {entry.get('folder_id', '-')} -> {entry.get('path', '-')}")
    if report.issues:
        lines.append("issues:")
        for issue in report.issues:
            lines.append(f"- {issue.level}: {issue.key}: {issue.message}")
    return "\n".join(lines)


def print_diffd_folder_cache_report(report: CommandReport, args: argparse.Namespace) -> None:
    if output_format(args) == "human":
        print(render_diffd_folder_cache_human(report))
        return
    print_report(report, args)
