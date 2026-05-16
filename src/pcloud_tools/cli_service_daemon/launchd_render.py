from __future__ import annotations

import argparse

from ..cli_common import output_format, print_report, shell_command
from ..output import CommandReport


def render_service_launchd_gate_human(report: CommandReport) -> str:
    details = report.details
    lines = [
        f"{report.command}: {report.status}",
        report.summary,
        f"launchd gate: {details.get('launchd gate status', '-')}",
        f"launchd can register: {details.get('launchd can register', '-')}",
        f"state writes: {details.get('state writes', '-')}",
        f"service label: {details.get('service label', '-')}",
        f"plist path: {details.get('plist path', '-')}",
        f"plist status: {details.get('plist status', '-')}",
        f"launchctl: {details.get('launchctl availability', '-')} ({details.get('launchctl binary', '-')})",
        f"approval status: {details.get('approval status', '-')}",
    ]
    daemon_command = details.get("daemon command preview")
    if daemon_command:
        lines.append(f"daemon command preview: {shell_command(daemon_command)}")
    bootstrap_commands = details.get("bootstrap command examples")
    if isinstance(bootstrap_commands, list) and bootstrap_commands:
        lines.append("bootstrap command examples:")
        for command in bootstrap_commands:
            lines.append(f"- {shell_command(command)}")
    rollback_commands = details.get("rollback command examples")
    if isinstance(rollback_commands, list) and rollback_commands:
        lines.append("rollback command examples:")
        for command in rollback_commands:
            lines.append(f"- {shell_command(command)}")
    lines.append(
        "future gate env: "
        f"{details.get('future launchd gate env var', '-')}="
        f"{details.get('future launchd gate accepted value', '-')}"
    )
    checks = details.get("preflight checks")
    if isinstance(checks, list) and checks:
        lines.append("preflight checks:")
        for check in checks:
            if isinstance(check, dict):
                lines.append(f"- {check.get('name', '-')}: {check.get('status', '-')}")
    blocked = details.get("blocked operations")
    if isinstance(blocked, list) and blocked:
        lines.append("blocked operations:")
        lines.extend(f"- {item}" for item in blocked)
    if report.issues:
        lines.append("warnings:" if report.status != "error" else "issues:")
        for issue in report.issues:
            lines.append(f"- {issue.key}: {issue.message}")
    return "\n".join(lines)


def print_service_launchd_gate_report(report: CommandReport, args: argparse.Namespace) -> None:
    if output_format(args) == "human":
        print(render_service_launchd_gate_human(report))
        return
    print_report(report, args)


def render_service_launchd_status_human(report: CommandReport) -> str:
    details = report.details
    lines = [
        f"{report.command}: {report.status}",
        report.summary,
        f"registration status: {details.get('registration status', '-')}",
        f"loaded: {details.get('launchd loaded', '-')}",
        f"state writes: {details.get('state writes', '-')}",
        f"service label: {details.get('service label', '-')}",
        f"plist path: {details.get('plist path', '-')}",
        f"plist status: {details.get('plist status', '-')}",
        f"launchctl: {details.get('launchctl availability', '-')} ({details.get('launchctl binary', '-')})",
    ]
    print_command = details.get("launchctl print command")
    if print_command:
        lines.append(f"launchctl print command: {shell_command(print_command)}")
    if report.issues:
        lines.append("warnings:" if report.status != "error" else "issues:")
        for issue in report.issues:
            lines.append(f"- {issue.key}: {issue.message}")
    return "\n".join(lines)


def print_service_launchd_status_report(report: CommandReport, args: argparse.Namespace) -> None:
    if output_format(args) == "human":
        print(render_service_launchd_status_human(report))
        return
    print_report(report, args)


def render_service_launchd_review_human(report: CommandReport) -> str:
    details = report.details
    lines = [
        f"{report.command}: {report.status}",
        report.summary,
        f"human review status: {details.get('human review status', '-')}",
        f"state writes: {details.get('state writes', '-')}",
        f"launchctl execution: {details.get('launchctl execution', '-')}",
        f"persistent daemon start: {details.get('persistent daemon start', '-')}",
        f"service label: {details.get('service label', '-')}",
        f"plist path: {details.get('plist path', '-')}",
    ]
    program_arguments = details.get("program arguments")
    if program_arguments:
        lines.append(f"program arguments: {shell_command(program_arguments)}")
    foreground_command = details.get("foreground command preview")
    if foreground_command:
        lines.append(f"foreground command preview: {shell_command(foreground_command)}")
    review_commands = details.get("terminal review commands")
    if isinstance(review_commands, list) and review_commands:
        lines.append("terminal review commands:")
        for command in review_commands:
            lines.append(f"- {shell_command(command)}")
    blocked = details.get("blocked operations")
    if isinstance(blocked, list) and blocked:
        lines.append("blocked operations:")
        lines.extend(f"- {item}" for item in blocked)
    if report.issues:
        lines.append("warnings:" if report.status != "error" else "issues:")
        for issue in report.issues:
            lines.append(f"- {issue.key}: {issue.message}")
    return "\n".join(lines)


def print_service_launchd_review_report(report: CommandReport, args: argparse.Namespace) -> None:
    if output_format(args) == "human":
        print(render_service_launchd_review_human(report))
        return
    print_report(report, args)


def render_service_launchd_register_human(report: CommandReport) -> str:
    details = report.details
    lines = [
        f"{report.command}: {report.status}",
        report.summary,
        f"execute: {details.get('execute', '-')}",
        f"registration gate: {details.get('launchd gate status', '-')}",
        f"launchd can register: {details.get('launchd can register', '-')}",
        f"state writes: {details.get('state writes', '-')}",
        f"launchctl execution: {details.get('launchctl execution', '-')}",
        f"persistent daemon start: {details.get('persistent daemon start', '-')}",
        f"service label: {details.get('service label', '-')}",
        f"plist path: {details.get('plist path', '-')}",
        f"plist status: {details.get('plist status', '-')}",
    ]
    commands = details.get("planned launchctl commands")
    if isinstance(commands, list) and commands:
        lines.append("planned launchctl commands:")
        for command in commands:
            lines.append(f"- {shell_command(command)}")
    results = details.get("launchctl results")
    if isinstance(results, list) and results:
        lines.append("launchctl results:")
        for result in results:
            if isinstance(result, dict):
                lines.append(f"- {result.get('command', '-')}: rc={result.get('returncode', '-')}")
    checks = details.get("preflight checks")
    if isinstance(checks, list) and checks:
        lines.append("preflight checks:")
        for check in checks:
            if isinstance(check, dict):
                lines.append(f"- {check.get('name', '-')}: {check.get('status', '-')}")
    blocked = details.get("blocked operations")
    if isinstance(blocked, list) and blocked:
        lines.append("blocked operations:")
        lines.extend(f"- {item}" for item in blocked)
    if report.issues:
        lines.append("warnings:" if report.status != "error" else "issues:")
        for issue in report.issues:
            lines.append(f"- {issue.key}: {issue.message}")
    return "\n".join(lines)


def print_service_launchd_register_report(report: CommandReport, args: argparse.Namespace) -> None:
    if output_format(args) == "human":
        print(render_service_launchd_register_human(report))
        return
    print_report(report, args)


def render_service_launchd_reload_human(report: CommandReport) -> str:
    details = report.details
    lines = [
        f"{report.command}: {report.status}",
        report.summary,
        f"execute: {details.get('execute', '-')}",
        f"reload gate: {details.get('reload gate status', '-')}",
        f"launchd can reload: {details.get('launchd can reload', '-')}",
        f"state writes: {details.get('state writes', '-')}",
        f"launchctl execution: {details.get('launchctl execution', '-')}",
        f"persistent daemon start: {details.get('persistent daemon start', '-')}",
        f"service label: {details.get('service label', '-')}",
        f"plist path: {details.get('plist path', '-')}",
        f"resident plist status: {details.get('resident plist status', '-')}",
    ]
    commands = details.get("planned launchctl commands")
    if isinstance(commands, list) and commands:
        lines.append("planned launchctl commands:")
        for command in commands:
            lines.append(f"- {shell_command(command)}")
    checks = details.get("preflight checks")
    if isinstance(checks, list) and checks:
        lines.append("preflight checks:")
        for check in checks:
            if isinstance(check, dict):
                lines.append(f"- {check.get('name', '-')}: {check.get('status', '-')}")
    results = details.get("launchctl results")
    if isinstance(results, list) and results:
        lines.append("launchctl results:")
        for result in results:
            if isinstance(result, dict):
                lines.append(f"- {result.get('command', '-')}: rc={result.get('returncode', '-')}")
    blocked = details.get("blocked operations")
    if isinstance(blocked, list) and blocked:
        lines.append("blocked operations:")
        lines.extend(f"- {item}" for item in blocked)
    if report.issues:
        lines.append("warnings:" if report.status != "error" else "issues:")
        for issue in report.issues:
            lines.append(f"- {issue.key}: {issue.message}")
    return "\n".join(lines)


def print_service_launchd_reload_report(report: CommandReport, args: argparse.Namespace) -> None:
    if output_format(args) == "human":
        print(render_service_launchd_reload_human(report))
        return
    print_report(report, args)


def render_service_launchd_resident_plist_human(report: CommandReport) -> str:
    details = report.details
    lines = [
        f"{report.command}: {report.status}",
        report.summary,
        f"execute: {details.get('execute', '-')}",
        f"resident plist gate: {details.get('resident plist gate status', '-')}",
        f"state writes: {details.get('state writes', '-')}",
        f"launchctl execution: {details.get('launchctl execution', '-')}",
        f"persistent daemon start: {details.get('persistent daemon start', '-')}",
        f"service label: {details.get('service label', '-')}",
        f"plist path: {details.get('plist path', '-')}",
        f"plist status: {details.get('plist status', '-')}",
    ]
    command = details.get("resident program arguments")
    if command:
        lines.append(f"resident program arguments: {shell_command(command)}")
    environment = details.get("environment variables")
    if isinstance(environment, dict) and environment:
        lines.append("environment variables:")
        for key, value in environment.items():
            lines.append(f"- {key}={value}")
    checks = details.get("preflight checks")
    if isinstance(checks, list) and checks:
        lines.append("preflight checks:")
        for check in checks:
            if isinstance(check, dict):
                lines.append(f"- {check.get('name', '-')}: {check.get('status', '-')}")
    blocked = details.get("blocked operations")
    if isinstance(blocked, list) and blocked:
        lines.append("blocked operations:")
        lines.extend(f"- {item}" for item in blocked)
    if report.issues:
        lines.append("warnings:" if report.status != "error" else "issues:")
        for issue in report.issues:
            lines.append(f"- {issue.key}: {issue.message}")
    return "\n".join(lines)


def print_service_launchd_resident_plist_report(report: CommandReport, args: argparse.Namespace) -> None:
    if output_format(args) == "human":
        print(render_service_launchd_resident_plist_human(report))
        return
    print_report(report, args)


def render_service_launchd_plist_human(report: CommandReport) -> str:
    details = report.details
    lines = [
        f"{report.command}: {report.status}",
        report.summary,
        f"execute: {details.get('execute', '-')}",
        f"plist path: {details.get('plist path', '-')}",
        f"plist status: {details.get('plist status', '-')}",
        f"state writes: {details.get('state writes', '-')}",
        f"launchctl execution: {details.get('launchctl execution', '-')}",
        f"service label: {details.get('service label', '-')}",
    ]
    program_arguments = details.get("program arguments")
    if program_arguments:
        lines.append(f"program arguments: {shell_command(program_arguments)}")
    if "start interval seconds" in details:
        lines.append(f"start interval seconds: {details.get('start interval seconds', '-')}")
    environment = details.get("environment variables")
    if isinstance(environment, dict) and environment:
        lines.append("environment variables:")
        for key, value in environment.items():
            lines.append(f"- {key}={value}")
    blocked = details.get("blocked operations")
    if isinstance(blocked, list) and blocked:
        lines.append("blocked operations:")
        lines.extend(f"- {item}" for item in blocked)
    if report.issues:
        lines.append("warnings:" if report.status != "error" else "issues:")
        for issue in report.issues:
            lines.append(f"- {issue.key}: {issue.message}")
    return "\n".join(lines)


def print_service_launchd_plist_report(report: CommandReport, args: argparse.Namespace) -> None:
    if output_format(args) == "human":
        print(render_service_launchd_plist_human(report))
        return
    print_report(report, args)
