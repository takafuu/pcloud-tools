from __future__ import annotations

import argparse
import json
import os
import plistlib
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from ..autosync_runtime import disable_autosync, enable_autosync, read_autosync_state
from ..cli_common import (
    entrypoint_command,
    exit_code_for_report,
    has_errors,
    report_issues,
    shell_command,
    sort_issues,
    status_from_issues,
)
from ..config import AppConfig, ConfigIssue, load_config
from ..gates import GATES, validate_gate
from ..io_utils import atomic_write_json
from ..output import CommandReport, render_report
from ..runtime import RuntimePaths
from .core import _command_path, _saved_shadow_report_check, _sync_status_actions

def _sync_autosync_report(args: argparse.Namespace, paths: RuntimePaths, action: str) -> CommandReport:
    load_result = load_config(paths)
    config = load_result.config
    issues = sort_issues(list(load_result.issues))
    autosync = read_autosync_state(config)
    details: dict[str, object] = {
        "execute": "yes" if args.execute else "no",
        "autosync state": autosync.state,
        "autosync runs": autosync.runs,
        "autosync label": autosync.label,
        "autosync plist": autosync.plist,
    }
    if action == "disable-autosync":
        details["assume yes"] = "yes" if args.yes else "no"

    plist_required = action == "enable-autosync"
    if plist_required and not config.autosync_plist.exists():
        issues = sort_issues(
            issues
            + [
                ConfigIssue(
                    key="PCLOUD_TOOLS_AUTOSYNC_PLIST",
                    level="warning" if not args.execute else "error",
                    message=f"autosync plist not found: {config.autosync_plist}",
                )
            ]
        )

    if issues and has_errors(issues):
        return CommandReport(
            command=f"sync {action}",
            status="error",
            summary="autosync command cannot run until configuration issues are resolved",
            details=details,
            issues=report_issues(issues),
        )

    details["planned action"] = (
        f"launchctl enable gui/<uid>/{config.autosync_label} and bootstrap {config.autosync_plist}"
        if action == "enable-autosync"
        else f"launchctl bootout/disable gui/<uid>/{config.autosync_label}"
    )

    if not args.execute:
        return CommandReport(
            command=f"sync {action}",
            status=status_from_issues(issues),
            summary="autosync preview is ready",
            details=details,
            issues=report_issues(issues),
        )

    if paths.dev_mode:
        issues = sort_issues(
            issues
            + [
                ConfigIssue(
                    key="PCLOUD_TOOLS_DEV_AUTOSYNC_EXECUTION",
                    level="error",
                    message=f"refusing --execute for `sync {action}` from pcloud-manager-dev",
                )
            ]
        )
        details["reason"] = (
            "use preview in dev mode; execution requires the public entrypoint or an explicit non-dev runtime"
        )
        return CommandReport(
            command=f"sync {action}",
            status="error",
            summary=f"dev mode refuses to execute sync {action}",
            details=details,
            issues=report_issues(issues),
        )

    if action == "disable-autosync" and not args.yes:
        issues = sort_issues(
            issues
            + [
                ConfigIssue(
                    key="PCLOUD_TOOLS_AUTOSYNC_CONFIRMATION",
                    level="error",
                    message="disable-autosync requires --yes together with --execute",
                )
            ]
        )
        return CommandReport(
            command=f"sync {action}",
            status="error",
            summary="disable-autosync requires confirmation",
            details=details,
            issues=report_issues(issues),
        )

    try:
        if action == "enable-autosync":
            enable_autosync(config)
        else:
            disable_autosync(config)
    except RuntimeError as exc:
        issues = sort_issues(
            issues + [ConfigIssue(key="PCLOUD_TOOLS_AUTOSYNC_EXEC", level="error", message=str(exc))]
        )
        return CommandReport(
            command=f"sync {action}",
            status="error",
            summary="autosync command failed",
            details=details,
            issues=report_issues(issues),
        )

    refreshed = read_autosync_state(config)
    details.update(
        {
            "autosync state": refreshed.state,
            "autosync runs": refreshed.runs,
        }
    )
    return CommandReport(
        command=f"sync {action}",
        status="ok",
        summary="autosync command executed",
        details=details,
        issues=report_issues(issues),
    )


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _autosync_plist_payload(paths: RuntimePaths, config: AppConfig) -> dict[str, object]:
    entrypoint = entrypoint_command(paths)
    return {
        "Label": config.autosync_label,
        "ProgramArguments": [entrypoint, "sync", "background", "--execute"],
        "RunAtLoad": False,
        "StartInterval": 300,
        "StandardOutPath": str(config.log_dir / "autosync-launchd.out"),
        "StandardErrorPath": str(config.log_dir / "autosync-launchd.err"),
    }


def _sync_autosync_plist_report(args: argparse.Namespace, paths: RuntimePaths) -> CommandReport:
    load_result = load_config(paths)
    config = load_result.config
    issues = list(load_result.issues)
    payload = _autosync_plist_payload(paths, config)
    plist_path = config.autosync_plist
    dev_state_root = paths.workspace_root / ".dev-state"
    details: dict[str, object] = {
        "execute": "yes" if args.execute else "no",
        "planned action": "write autosync LaunchAgent plist" if args.execute else "preview autosync LaunchAgent plist",
        "autosync label": config.autosync_label,
        "autosync plist": str(plist_path),
        "autosync plist status": "present" if plist_path.exists() else "missing",
        "plist payload": payload,
        "state writes": "autosync plist only" if args.execute else "none",
        "launchctl execution": "no",
        "scheduled sync execution": "no",
    }
    if args.execute and not paths.dev_mode:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_AUTOSYNC_PLIST_EXECUTION",
                level="error",
                message="autosync-plist --execute is limited to pcloud-manager-dev/dev mode",
            )
        )
    if args.execute and not _is_relative_to(plist_path, dev_state_root):
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_AUTOSYNC_PLIST_PATH",
                level="error",
                message=f"refusing to write autosync plist outside {dev_state_root}: {plist_path}",
            )
        )
    issues = sort_issues(issues)
    if has_errors(issues):
        details["state writes"] = "none"
    if args.execute and not has_errors(issues):
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        plist_path.write_bytes(plistlib.dumps(payload, sort_keys=True))
        details["autosync plist status"] = "written"
    return CommandReport(
        command="sync autosync-plist",
        status=status_from_issues(issues),
        summary="autosync plist written" if args.execute and not has_errors(issues) else "autosync plist preview is ready",
        details=details,
        issues=report_issues(issues),
    )


def cmd_sync_autosync_plist(args: argparse.Namespace, paths: RuntimePaths) -> int:
    report = _sync_autosync_plist_report(args, paths)
    print(render_report(report, as_json=args.json))
    return exit_code_for_report(report)


_REQUIRED_SHADOW_CHECKS = {
    "temporary workspace guard",
    "temporary state dir guard",
    "unsafe state dir guard",
}


def _saved_shadow_report_check(
    report_path: Path | None,
    *,
    issue_key: str = "PCLOUD_TOOLS_AUTOSYNC_SHADOW_REPORT",
) -> tuple[dict[str, object], list[ConfigIssue]]:
    if report_path is None:
        return (
            {
                "name": "saved shadow validation report",
                "status": "pending",
                "detail": "pass --report-path after saving scripts/pcloud-shadow-validation.py --report-path",
            },
            [
                ConfigIssue(
                    key=issue_key,
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
                    key=issue_key,
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
                    key=issue_key,
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
    missing_required = sorted(_REQUIRED_SHADOW_CHECKS - check_names)
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
                key=issue_key,
                level="warning",
                message=f"saved shadow validation report is not ok: {detail}",
            )
        ],
    )


def _render_autosync_gate_human(report: CommandReport) -> str:
    details = report.details
    lines = [
        f"{report.command}: {report.status}",
        report.summary,
        f"launchd gate: {details.get('launchd gate status', '-')}",
        f"autosync changes can run: {details.get('autosync changes can run', '-')}",
        f"state writes: {details.get('state writes', '-')}",
        f"autosync state: {details.get('autosync state', '-')}",
        f"autosync label: {details.get('autosync label', '-')}",
        f"autosync plist: {details.get('autosync plist', '-')}",
        f"autosync plist status: {details.get('autosync plist status', '-')}",
        f"autosync plist note: {details.get('autosync plist note', '-')}",
        f"launchctl: {details.get('launchctl availability', '-')} ({details.get('launchctl binary', '-')})",
        f"approval status: {details.get('autosync approval status', '-')}",
        f"human gate: {details.get('human gate status', '-')}",
        f"next human check trigger: {details.get('next human check trigger', '-')}",
    ]
    commands = [
        ("enable preview", details.get("enable preview command")),
        ("disable preview", details.get("disable preview command")),
        ("plist review", details.get("autosync plist review command")),
    ]
    for label, command in commands:
        if command:
            lines.append(f"{label}: {shell_command(command)}")
    launchctl_commands = [
        ("enable launchctl commands", details.get("enable launchctl commands")),
        ("disable launchctl commands", details.get("disable launchctl commands")),
    ]
    for label, command_group in launchctl_commands:
        if not isinstance(command_group, list) or not command_group:
            continue
        lines.append(f"{label}:")
        for command in command_group:
            lines.append(f"- {shell_command(command)}")
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


def _autosync_launchd_run_state_file(config: AppConfig) -> Path:
    return config.state_dir / "sync" / "autosync-launchd-last-run.json"


def _autosync_launchd_gate_spec():
    return GATES["autosync.launchd"]


def _autosync_launchd_gate_open(config: AppConfig) -> bool:
    spec = _autosync_launchd_gate_spec()
    return config.autosync_launchd_gate == spec.expected_value


def _actual_launchctl_commands(config: AppConfig, launchctl_bin: str, mode: str) -> list[list[str]]:
    uid = str(os.getuid())
    target = f"gui/{uid}/{config.autosync_label}"
    if mode == "enable":
        return [
            [launchctl_bin, "enable", target],
            [launchctl_bin, "bootstrap", f"gui/{uid}", str(config.autosync_plist)],
        ]
    return [
        [launchctl_bin, "bootout", target],
        [launchctl_bin, "disable", target],
    ]


def _render_autosync_run_human(report: CommandReport) -> str:
    details = report.details
    lines = [
        f"{report.command}: {report.status}",
        report.summary,
        f"launchd run gate: {details.get('launchd run gate status', '-')}",
        f"autosync changes can run: {details.get('autosync changes can run', '-')}",
        f"mode: {details.get('mode', '-')}",
        f"execute requested: {details.get('execute requested', '-')}",
        f"state writes: {details.get('state writes', '-')}",
        f"autosync state: {details.get('autosync state', '-')}",
        f"autosync label: {details.get('autosync label', '-')}",
        f"autosync plist: {details.get('autosync plist', '-')}",
        f"autosync plist status: {details.get('autosync plist status', '-')}",
        f"launchctl: {details.get('launchctl availability', '-')} ({details.get('launchctl binary', '-')})",
        f"approval status: {details.get('autosync approval status', '-')}",
    ]
    commands = details.get("planned launchctl commands")
    if isinstance(commands, list) and commands:
        lines.append("planned launchctl commands:")
        for command in commands:
            lines.append(f"- {shell_command(command)}")
    state_file = details.get("launchd run state file")
    if state_file:
        lines.append(f"launchd run state: {state_file}")
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


def _sync_autosync_gate_report(args: argparse.Namespace, paths: RuntimePaths) -> CommandReport:
    load_result = load_config(paths)
    config = load_result.config
    autosync_gate = validate_gate(
        _autosync_launchd_gate_spec(),
        args,
        {_autosync_launchd_gate_spec().env_var: config.autosync_launchd_gate},
    )
    autosync = read_autosync_state(config)
    issues = list(load_result.issues)
    shadow_check, shadow_issues = _saved_shadow_report_check(getattr(args, "report_path", None))
    issues.extend(shadow_issues)
    launchctl_bin = _command_path("launchctl")
    launchctl_check = {
        "name": "launchctl binary",
        "status": "ok" if launchctl_bin else "pending",
        "detail": launchctl_bin or "launchctl not found by command -v; verify before launchd changes",
    }
    if not launchctl_bin:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_AUTOSYNC_LAUNCHCTL",
                level="warning",
                message="launchctl was not found by command -v; autosync gate remains closed",
            )
        )
    plist_check = {
        "name": "autosync plist",
        "status": "ok" if config.autosync_plist.exists() else "pending",
        "detail": str(config.autosync_plist),
    }
    if not config.autosync_plist.exists():
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_AUTOSYNC_PLIST",
                level="warning",
                message=f"autosync plist not found: {config.autosync_plist}",
            )
        )

    entrypoint = entrypoint_command(paths)
    enable_preview = [entrypoint, "sync", "enable-autosync", "--json"]
    disable_preview = [entrypoint, "sync", "disable-autosync", "--yes", "--json"]
    plist_review = ["plutil", "-p", str(config.autosync_plist)]
    target = f"gui/<uid>/{config.autosync_label}"
    enable_launchctl = [
        [launchctl_bin or "launchctl", "enable", target],
        [launchctl_bin or "launchctl", "bootstrap", "gui/<uid>", str(config.autosync_plist)],
    ]
    disable_launchctl = [
        [launchctl_bin or "launchctl", "bootout", target],
        [launchctl_bin or "launchctl", "disable", target],
    ]
    checks = [
        shadow_check,
        launchctl_check,
        plist_check,
        {
            "name": "autosync preview commands",
            "status": "ok",
            "detail": f"{' '.join(enable_preview)}; {' '.join(disable_preview)}",
        },
        {
            "name": "operator preview review",
            "status": "ok" if autosync_gate.flag_ok("--operator-reviewed-preview") else "pending",
            "detail": "operator reviewed enable/disable autosync preview output and launchd label",
        },
        {
            "name": "plist approval",
            "status": "ok" if autosync_gate.flag_ok("--reviewer-approved-plist") else "pending",
            "detail": "reviewer approved plist path, label, and public entrypoint target",
        },
        {
            "name": "launchctl policy approval",
            "status": "ok" if autosync_gate.flag_ok("--reviewer-approved-launchctl-policy") else "pending",
            "detail": "reviewer approved bootstrap/bootout/enable/disable behavior before launchd changes",
        },
        {
            "name": "rollback policy approval",
            "status": "ok" if autosync_gate.flag_ok("--reviewer-approved-rollback-policy") else "pending",
            "detail": "reviewer approved rollback commands and stop conditions before launchd changes",
        },
        {
            "name": "parallel dangerous gates",
            "status": "ok",
            "detail": "fswatch resident start, pCloud API long-poll start, normal sync/resync, and archive work stay out of scope",
        },
    ]
    approval_status = "complete-read-only" if all(check["status"] == "ok" for check in checks) else "pending"
    issues.append(
        ConfigIssue(
            key=autosync_gate.spec.env_var,
            level="warning",
            message="autosync launchd changes remain gated; this command is read-only",
        )
    )
    issues = sort_issues(issues)
    details: dict[str, object] = {
        "planned action": "check autosync launchd change prerequisites",
        "implementation status": "read-only checklist; launchctl is not executed",
        "launchd gate status": "closed",
        "autosync changes can run": "no",
        "operator verification required": "yes-before-autosync-launchd-gate",
        "human gate status": "required-before-autosync-launchd-change",
        "human gate reason": "autosync launchd changes would alter scheduled live sync behavior",
        "state writes": "none",
        "autosync state": autosync.state,
        "autosync runs": autosync.runs,
        "autosync label": autosync.label,
        "autosync plist": autosync.plist,
        "autosync plist status": "present" if config.autosync_plist.exists() else "missing",
        "autosync plist note": "review or create the plist in a separate gate; autosync-gate does not write it",
        "autosync plist review command": plist_review,
        "launchctl availability": "available" if launchctl_bin else "missing",
        "launchctl binary": launchctl_bin or "-",
        "enable preview command": enable_preview,
        "disable preview command": disable_preview,
        "enable launchctl commands": enable_launchctl,
        "disable launchctl commands": disable_launchctl,
        "autosync approval status": approval_status,
        "preflight checks": checks,
        "success policy": "apply launchctl changes only after explicit operator command and verify autosync status afterward",
        "failure policy": "stop on launchctl failure and keep prior autosync state for operator review",
        "rollback policy": "use the displayed inverse launchctl command and saved plist/label information",
        "blocked operations": [
            "launchctl enable",
            "launchctl bootstrap",
            "launchctl bootout",
            "launchctl disable",
            "starting or stopping scheduled sync jobs",
            "normal sync/resync execution",
        ],
        "next human check trigger": "explicit autosync launchd change implementation or scheduled sync behavior change",
    }
    return CommandReport(
        command="sync autosync-gate",
        status=status_from_issues(issues),
        summary="autosync launchd gate is closed",
        details=details,
        issues=report_issues(issues),
        actions=_sync_status_actions(paths, "missing"),
    )


def cmd_sync_autosync_gate(args: argparse.Namespace, paths: RuntimePaths) -> int:
    report = _sync_autosync_gate_report(args, paths)
    output_format = "xbar" if getattr(args, "xbar", False) else "json" if getattr(args, "json", False) else "human"
    if output_format == "human":
        print(_render_autosync_gate_human(report))
    else:
        print(render_report(report, output_format=output_format))
    return exit_code_for_report(report)


def _sync_autosync_run_report(args: argparse.Namespace, paths: RuntimePaths) -> CommandReport:
    gate_report = _sync_autosync_gate_report(args, paths)
    load_result = load_config(paths)
    config = load_result.config
    autosync = read_autosync_state(config)
    mode = str(getattr(args, "mode", "enable"))
    execute = bool(getattr(args, "execute", False))
    launchctl_bin = _command_path("launchctl")
    details = dict(gate_report.details)
    issues = [
        ConfigIssue(key=issue.key, level=issue.level, message=issue.message)
        for issue in gate_report.issues
        if issue.key != "PCLOUD_TOOLS_AUTOSYNC_LAUNCHD_GATE"
    ]
    approval_status = str(details.get("autosync approval status", "pending"))
    autosync_spec = _autosync_launchd_gate_spec()
    gate_open = _autosync_launchd_gate_open(config)
    state_file = _autosync_launchd_run_state_file(config)
    planned_commands = _actual_launchctl_commands(config, launchctl_bin or "launchctl", mode)

    details.update(
        {
            "planned action": "run autosync launchd changes" if execute else "preview autosync launchd run",
            "implementation status": (
                "guarded launchctl execution path"
                if execute
                else "autosync launchd run preview only; launchctl is not executed"
            ),
            "mode": mode,
            "launchd run gate status": (
                f"open: {autosync_spec.expected_value}"
                if gate_open
                else f"closed: requires {autosync_spec.env_var}={autosync_spec.expected_value}"
            ),
            "launchd gate status": (
                f"open: {autosync_spec.expected_value}"
                if gate_open
                else f"closed: requires {autosync_spec.env_var}={autosync_spec.expected_value}"
            ),
            "autosync changes can run": "yes" if gate_open and approval_status == "complete-read-only" else "no",
            "execute requested": "yes" if execute else "no",
            "state writes": "autosync launchd run state" if execute else "none",
            "planned launchctl commands": planned_commands,
            "launchd run state file": str(state_file),
            "future gate env": f"{autosync_spec.env_var}={autosync_spec.expected_value}",
        }
    )

    if approval_status != "complete-read-only":
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_AUTOSYNC_LAUNCHD_APPROVAL",
                level="error" if execute else "warning",
                message="autosync launchd execution requires complete read-only approvals",
            )
        )
    if not gate_open:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_AUTOSYNC_LAUNCHD_EXECUTION_GATE",
                level="error" if execute else "warning",
                message=(
                    "autosync launchd execution requires "
                    f"{autosync_spec.env_var}={autosync_spec.expected_value!r}"
                ),
            )
        )
    if not launchctl_bin:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_AUTOSYNC_LAUNCHCTL",
                level="error" if execute else "warning",
                message="launchctl command not found",
            )
        )
    if mode == "enable" and not config.autosync_plist.exists():
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_AUTOSYNC_PLIST",
                level="error" if execute else "warning",
                message=f"autosync plist not found: {config.autosync_plist}",
            )
        )

    if not execute or has_errors(issues):
        if has_errors(issues):
            details["state writes"] = "none"
        issues = sort_issues(issues)
        return CommandReport(
            command="sync autosync-run",
            status=status_from_issues(issues),
            summary=(
                "autosync launchd execution is gated"
                if has_errors(issues) or not gate_open
                else "autosync launchd run is ready"
            ),
            details=details,
            issues=report_issues(issues),
            actions=_sync_status_actions(paths, "missing"),
        )

    started_at = datetime.now(timezone.utc).isoformat()
    run_state: dict[str, object] = {
        "mode": mode,
        "started_at": started_at,
        "commands": planned_commands,
        "previous_autosync_state": autosync.state,
    }
    try:
        if mode == "enable":
            enable_autosync(config)
        else:
            disable_autosync(config)
    except RuntimeError as exc:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_AUTOSYNC_LAUNCHD_EXEC",
                level="error",
                message=str(exc),
            )
        )
    finished_at = datetime.now(timezone.utc).isoformat()
    refreshed = read_autosync_state(config)
    run_state.update(
        {
            "finished_at": finished_at,
            "autosync_state_after": refreshed.state,
            "autosync_runs_after": refreshed.runs,
        }
    )
    if not has_errors(issues):
        state_file.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(state_file, run_state, sort_keys=True)

    details.update(
        {
            "autosync state after": refreshed.state,
            "autosync runs after": refreshed.runs,
            "process result": run_state,
            "state writes": "autosync launchd run state" if not has_errors(issues) else "none",
        }
    )
    issues = sort_issues(issues)
    return CommandReport(
        command="sync autosync-run",
        status=status_from_issues(issues),
        summary="autosync launchd run completed" if not has_errors(issues) else "autosync launchd run failed",
        details=details,
        issues=report_issues(issues),
        actions=_sync_status_actions(paths, "missing"),
    )


def cmd_sync_autosync_run(args: argparse.Namespace, paths: RuntimePaths) -> int:
    report = _sync_autosync_run_report(args, paths)
    output_format = "xbar" if getattr(args, "xbar", False) else "json" if getattr(args, "json", False) else "human"
    if output_format == "human":
        print(_render_autosync_run_human(report))
    else:
        print(render_report(report, output_format=output_format))
    return exit_code_for_report(report)

def cmd_sync_autosync(args: argparse.Namespace, paths: RuntimePaths, action: str) -> int:
    report = _sync_autosync_report(args, paths, action)
    print(render_report(report, as_json=args.json))
    return exit_code_for_report(report)
