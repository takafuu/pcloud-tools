from __future__ import annotations

import argparse

from ..cli_common import exit_code_for_report, print_report, report_issues, sort_issues, status_from_issues
from ..config import AppConfig, ConfigIssue, load_config
from ..output import CommandReport, ReportAction
from ..runtime import RuntimePaths


def gate_details(paths: RuntimePaths, config: AppConfig, service_name: str) -> dict[str, object]:
    shared_requirements = [
        "saved shadow validation report with status ok",
        "reviewer approval recorded in report handoff",
        "explicit operator gate for this real operation",
    ]
    if service_name == "pushd":
        blocked = [
            "fswatch resident daemon",
            "launchd registration",
            "real upload execution",
            "queue consumption against live state",
        ]
        next_units = [
            "capture first real upload target with transfer check --final-review",
            "complete read-only real-gate approvals without opening execution",
            "hold real-run implementation until the human gate is explicitly confirmed",
        ]
    else:
        blocked = [
            "pCloud API long-poll",
            "launchd registration",
            "real download execution",
            "diff cursor mutation against live state",
        ]
        next_units = [
            "capture first real download target with transfer check --final-review",
            "complete read-only real-gate approvals without opening execution",
            "hold real-run implementation until the human gate is explicitly confirmed",
        ]
    return {
        "gate status": "closed",
        "allowed work": "dev-state preview/status/plan/report/test only",
        "operator verification required": "no",
        "operator verification scope": "read-only gate diagnostics; automated validation is enough",
        "human gate status": "required-before-real-work",
        "human gate reason": (
            "remaining work includes real rclone/pCloud transfer, real validation, or archive decisions"
        ),
        "next human check trigger": (
            "first real target review, real execution gate implementation, or actual pCloud/rclone transfer"
        ),
        "dev mode": "on" if paths.dev_mode else "off",
        "state dir": str(config.state_dir / service_name),
        "workspace root": str(paths.workspace_root),
        "shadow validation command": "python3 scripts/pcloud-shadow-validation.py --json",
        "blocked operations": blocked,
        "required before opening": shared_requirements,
        "suggested next units": next_units,
    }


def service_gate_report(
    paths: RuntimePaths,
    service_name: str,
    actions: list[ReportAction],
) -> CommandReport:
    load_result = load_config(paths)
    issues = list(load_result.issues)
    issues.append(
        ConfigIssue(
            key=f"PCLOUD_TOOLS_{service_name.upper()}_REAL_GATE",
            level="warning",
            message=(
                f"{service_name} real operations remain gated; "
                "use preview/dev-state paths until the dedicated gate is explicitly opened"
            ),
        )
    )
    issues = sort_issues(issues)
    return CommandReport(
        command=f"{service_name} gate",
        status=status_from_issues(issues),
        summary=f"{service_name} real-operation gate is closed",
        details=gate_details(paths, load_result.config, service_name),
        issues=report_issues(issues),
        actions=actions,
    )


def print_service_gate_report(report: CommandReport, args: argparse.Namespace) -> int:
    print_report(report, args)
    return exit_code_for_report(report)
