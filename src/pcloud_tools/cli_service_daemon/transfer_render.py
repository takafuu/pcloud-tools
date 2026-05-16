from __future__ import annotations

import argparse

from ..cli_common import output_format, print_report, shell_command
from ..output import CommandReport


def render_transfer_check_human(report: CommandReport) -> str:
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
    if "real transfer execution gate status" in details:
        lines.append(f"execution gate: {details.get('real transfer execution gate status', '-')}")
    if "real execution can run" in details:
        lines.append(f"real execution can run: {details.get('real execution can run', '-')}")
    if "future real gate env var" in details:
        lines.append(
            "future gate env: "
            f"{details.get('future real gate env var', '-')}="
            f"{details.get('future real gate accepted value', '-')}"
        )
    if "fake-rclone gate reuse" in details:
        lines.append(f"fake-rclone gate reuse: {details.get('fake-rclone gate reuse', '-')}")
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
    if details.get("final review requested"):
        lines.append(f"final review: {details.get('final review status', '-')}")
        if "real transfer gate opening status" in details:
            lines.append(f"gate opening: {details.get('real transfer gate opening status', '-')}")
        if "real transfer gate opening note" in details:
            lines.append(f"gate note: {details.get('real transfer gate opening note', '-')}")
        if "separate real gate approval status" in details:
            lines.append(f"approval status: {details.get('separate real gate approval status', '-')}")
        if "operator verification required" in details:
            lines.append(f"operator verification required: {details.get('operator verification required', '-')}")
        if "human gate status" in details:
            lines.append(f"human gate: {details.get('human gate status', '-')}")
        if "human gate reason" in details:
            lines.append(f"human gate reason: {details.get('human gate reason', '-')}")
        if "next human check trigger" in details:
            lines.append(f"next human check trigger: {details.get('next human check trigger', '-')}")
        if "real execution readiness" in details:
            lines.append(f"real execution readiness: {details.get('real execution readiness', '-')}")
        if "real execution blocked reason" in details:
            lines.append(f"real execution blocked reason: {details.get('real execution blocked reason', '-')}")
        approval_checks = details.get("separate real gate approval checks")
        if isinstance(approval_checks, list) and approval_checks:
            lines.append("approval checks:")
            for check in approval_checks:
                if not isinstance(check, dict):
                    continue
                lines.append(
                    "- "
                    f"{check.get('name', '-')}: "
                    f"{check.get('status', '-')} - "
                    f"{check.get('detail', '-')}"
                )
        if "future real-run policy status" in details:
            lines.append(f"future run policy: {details.get('future real-run policy status', '-')}")
        if "future real-run success policy" in details:
            lines.append(f"success policy: {details.get('future real-run success policy', '-')}")
        if "future real-run failure policy" in details:
            lines.append(f"failure policy: {details.get('future real-run failure policy', '-')}")
        if "future real-run rollback policy" in details:
            lines.append(f"rollback policy: {details.get('future real-run rollback policy', '-')}")
        blocker_details = details.get("final review blocker details")
        if isinstance(blocker_details, list) and blocker_details:
            lines.append("blocked checks:")
            for blocker in blocker_details:
                if not isinstance(blocker, dict):
                    continue
                lines.append(
                    "- "
                    f"{blocker.get('name', '-')}: "
                    f"{blocker.get('status', '-')} - "
                    f"{blocker.get('detail', '-')}"
                )
        elif isinstance(details.get("final review blockers"), list) and details["final review blockers"]:
            lines.append(f"final blockers: {', '.join(str(item) for item in details['final review blockers'])}")
        note = details.get("dry-run display note")
        if note:
            lines.append(f"dry-run note: {note}")
        dry_run_command = details.get("dry-run transfer command")
        if dry_run_command:
            lines.append(f"dry-run command: {shell_command(dry_run_command)}")
        real_command = details.get("real transfer command")
        if real_command:
            lines.append(f"real command: {shell_command(real_command)}")
        next_checks = details.get("separate real gate next checks")
        if isinstance(next_checks, list) and next_checks:
            lines.append("next gate checks:")
            for check in next_checks:
                lines.append(f"- {check}")
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
            lines.append(f"- {label}: {shell_command(command)}")

    if report.issues:
        lines.append("warnings:")
        for issue in report.issues:
            if issue.level == "warning":
                lines.append(f"- {issue.key}: {issue.message}")
    return "\n".join(lines)


def print_transfer_check_report(report: CommandReport, args: argparse.Namespace) -> None:
    if output_format(args) == "human":
        print(render_transfer_check_human(report))
        return
    print_report(report, args)


def render_real_transfer_run_human(report: CommandReport) -> str:
    details = report.details
    lines = [
        f"{report.command}: {report.status}",
        report.summary,
        f"execution gate: {details.get('real transfer execution gate status', '-')}",
        f"real execution readiness: {details.get('real execution readiness', '-')}",
        f"real execution can run: {details.get('real execution can run', '-')}",
        f"execute requested: {details.get('execute requested', '-')}",
        f"state writes: {details.get('state writes', '-')}",
        f"real gate env provided: {details.get('real gate env provided', '-')}",
        f"real gate env honored: {details.get('real gate env honored', '-')}",
        f"fake-rclone gate reuse: {details.get('fake-rclone gate reuse', '-')}",
        f"fake-rclone gate env provided: {details.get('fake-rclone gate env provided', '-')}",
        f"fake-rclone gate env honored: {details.get('fake-rclone gate env honored', '-')}",
    ]
    safe_alternative = details.get("safe alternative command")
    if safe_alternative:
        lines.append(f"safe alternative: {shell_command(safe_alternative)}")
    if report.issues:
        lines.append("issues:")
        for issue in report.issues:
            lines.append(f"- {issue.level}: {issue.key}: {issue.message}")
    return "\n".join(lines)


def print_real_transfer_run_report(report: CommandReport, args: argparse.Namespace) -> None:
    if output_format(args) == "human":
        print(render_real_transfer_run_human(report))
        return
    print_report(report, args)


def render_transfer_preview_human(report: CommandReport) -> str:
    details = report.details
    commands = details.get("planned transfer commands")
    command_count = len(commands) if isinstance(commands, list) else 0
    lines = [
        f"{report.command}: {report.status}",
        report.summary,
        f"gate: {details.get('real transfer gate status', '-')}",
        f"real execution can run: {details.get('real execution can run', '-')}",
        f"state writes: {details.get('state writes', '-')}",
        f"planned transfers: {command_count}",
    ]
    count_keys = (
        "planned uploads",
        "missing local upload records",
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
                lines.append(f"first command: {shell_command(command)}")

    if report.issues:
        lines.append("warnings:")
        for issue in report.issues:
            if issue.level == "warning":
                lines.append(f"- {issue.key}: {issue.message}")
    return "\n".join(lines)


def print_transfer_preview_report(report: CommandReport, args: argparse.Namespace) -> None:
    if output_format(args) == "human":
        print(render_transfer_preview_human(report))
        return
    print_report(report, args)


def render_transfer_consume_human(report: CommandReport) -> str:
    details = report.details
    lines = [
        f"{report.command}: {report.status}",
        report.summary,
        f"consume gate: {details.get('consume gate status', '-')}",
        f"real execution can run: {details.get('real execution can run', '-')}",
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


def print_transfer_consume_report(report: CommandReport, args: argparse.Namespace) -> None:
    if output_format(args) == "human":
        print(render_transfer_consume_human(report))
        return
    print_report(report, args)


def render_validation_matrix_human(report: CommandReport) -> str:
    details = report.details
    lines = [
        f"{report.command}: {report.status}",
        report.summary,
        f"real execution can run: {details.get('real execution can run', '-')}",
        f"state writes: {details.get('state writes', '-')}",
        f"case count: {details.get('case count', '-')}",
    ]
    cases = details.get("cases")
    if isinstance(cases, list) and cases:
        lines.append("cases:")
        for case in cases:
            if not isinstance(case, dict):
                continue
            lines.append(
                f"- {case.get('id', '-')}: {case.get('path', '-')} "
                f"({case.get('direction', '-')}; {case.get('purpose', '-')})"
            )
            commands = case.get("commands")
            if isinstance(commands, dict):
                for label in ("setup", "preview", "check", "cleanup"):
                    command = commands.get(label)
                    if command:
                        lines.append(f"  {label}: {shell_command(command)}")
    blocked = details.get("blocked operations")
    if isinstance(blocked, list) and blocked:
        lines.append("blocked operations:")
        lines.extend(f"- {item}" for item in blocked)
    confirmations = details.get("human confirmation required")
    if isinstance(confirmations, list) and confirmations:
        lines.append("human confirmation required:")
        lines.extend(f"- {item}" for item in confirmations)
    return "\n".join(lines)


def print_validation_matrix_report(report: CommandReport, args: argparse.Namespace) -> None:
    if output_format(args) == "human":
        print(render_validation_matrix_human(report))
        return
    print_report(report, args)
