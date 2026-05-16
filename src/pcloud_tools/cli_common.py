from __future__ import annotations

import argparse
import shlex

from .config import ConfigIssue
from .output import CommandReport, ReportIssue, render_report
from .runtime import RuntimePaths, action_entrypoint_command


def has_errors(issues: list[ConfigIssue]) -> bool:
    return any(issue.level == "error" for issue in issues)


def has_warnings(issues: list[ConfigIssue]) -> bool:
    return any(issue.level == "warning" for issue in issues)


def status_from_issues(issues: list[ConfigIssue]) -> str:
    if has_errors(issues):
        return "error"
    if has_warnings(issues):
        return "warning"
    return "ok"


def report_issues(issues: list[ConfigIssue]) -> list[ReportIssue]:
    return [ReportIssue(level=issue.level, key=issue.key, message=issue.message) for issue in issues]


def issue_sort_key(issue: ConfigIssue) -> tuple[int, str]:
    priority = 0 if issue.level == "error" else 1
    return (priority, issue.key)


def sort_issues(issues: list[ConfigIssue]) -> list[ConfigIssue]:
    return sorted(issues, key=issue_sort_key)


def exit_code_for_report(report: CommandReport) -> int:
    return 1 if report.status == "error" else 0


def entrypoint_command(paths: RuntimePaths) -> str:
    return action_entrypoint_command(paths)


def action_command(paths: RuntimePaths, action_id: str) -> tuple[str, ...]:
    return (entrypoint_command(paths), "action", action_id)


def shell_command(value: object) -> str:
    if isinstance(value, (list, tuple)):
        return shlex.join(str(part) for part in value)
    return str(value)


def output_format(args: argparse.Namespace) -> str:
    if getattr(args, "xbar", False):
        return "xbar"
    return "json" if getattr(args, "json", False) else "human"


def print_report(report: CommandReport, args: argparse.Namespace) -> None:
    print(render_report(report, output_format=output_format(args)))
