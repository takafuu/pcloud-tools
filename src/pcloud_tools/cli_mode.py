from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .autosync_runtime import read_autosync_state
from .cli_common import (
    action_command,
    entrypoint_command,
    exit_code_for_report,
    has_errors,
    report_issues,
    shell_command,
    sort_issues,
    status_from_issues,
)
from .config import AppConfig, ConfigIssue, load_config
from .daemon_state import read_daemon_state
from .io_utils import atomic_write_json
from .output import CommandReport, ReportAction, render_report
from .runtime import RuntimePaths
from .service_daemon_plan import build_diffd_plan, build_pushd_plan
from .service_daemon_state import read_service_daemon_state
from .sync_runtime import read_sync_lock_state

_MODE_SWITCH_GATE_ENV = "PCLOUD_TOOLS_MODE_SWITCH_GATE"
_MODE_SWITCH_GATE_VALUE = "operator-approved-mode-switch-v1"
_VALID_MODES = ("daemon", "maintenance", "pause")


def add_mode_parser(subparsers: argparse._SubParsersAction) -> None:
    mode_parser = subparsers.add_parser(
        "mode", help="Inspect or switch exclusive daemon/bisync operation modes."
    )
    mode_subparsers = mode_parser.add_subparsers(dest="mode_command")

    status_parser = mode_subparsers.add_parser("status", help="Show exclusive operation mode status.")
    status_parser.add_argument("--json", action="store_true")
    status_parser.add_argument("--xbar", action="store_true")

    plan_parser = mode_subparsers.add_parser("plan", help="Preview an exclusive mode switch.")
    plan_parser.add_argument("target_mode", choices=_VALID_MODES)
    plan_parser.add_argument("--json", action="store_true")
    plan_parser.add_argument("--xbar", action="store_true")

    switch_parser = mode_subparsers.add_parser("switch", help="Run a gated exclusive mode switch.")
    switch_parser.add_argument("target_mode", choices=_VALID_MODES)
    switch_parser.add_argument("--execute", action="store_true")
    switch_parser.add_argument("--json", action="store_true")
    switch_parser.add_argument("--xbar", action="store_true")
    switch_parser.add_argument("--operator-reviewed-mode-plan", action="store_true")
    switch_parser.add_argument("--reviewer-approved-exclusive-policy", action="store_true")
    switch_parser.add_argument("--reviewer-approved-launchd-policy", action="store_true")
    switch_parser.add_argument("--reviewer-approved-rollback-policy", action="store_true")


def cmd_mode(args: argparse.Namespace, paths: RuntimePaths) -> int | None:
    if args.mode_command == "status":
        report = _mode_status_report(paths)
    elif args.mode_command == "plan":
        report = _mode_plan_report(paths, args.target_mode, execute=False, args=args)
    elif args.mode_command == "switch":
        report = _mode_plan_report(paths, args.target_mode, execute=bool(args.execute), args=args)
    else:
        return None
    output_format = "xbar" if getattr(args, "xbar", False) else "json" if getattr(args, "json", False) else "human"
    print(_render_mode_human(report) if output_format == "human" else render_report(report, output_format=output_format))
    return exit_code_for_report(report)


def _command_v(command_name: str) -> str | None:
    result = subprocess.run(
        ["/bin/sh", "-c", f"command -v {shlex.quote(command_name)}"],
        check=False,
        capture_output=True,
        text=True,
    )
    found = result.stdout.strip()
    return found.splitlines()[0] if found else None


def _service_plist_path(label: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def _daemon_services() -> tuple[dict[str, str], ...]:
    return (
        {"name": "pushd", "label": "com.takafumi.pcloud-pushd"},
        {"name": "pushd-executor", "label": "com.takafumi.pcloud-pushd-executor"},
        {"name": "diffd", "label": "com.takafumi.pcloud-diffd"},
        {"name": "diffd-executor", "label": "com.takafumi.pcloud-diffd-executor"},
    )


def _launchctl_print(launchctl_bin: str | None, label: str) -> dict[str, object]:
    target = f"gui/{os.getuid()}/{label}"
    if not launchctl_bin:
        return {
            "label": label,
            "target": target,
            "loaded": False,
            "state": "launchctl-missing",
            "runs": "-",
            "returncode": None,
        }
    result = subprocess.run(
        [launchctl_bin, "print", target],
        check=False,
        capture_output=True,
        text=True,
    )
    state = "not_loaded"
    runs = "-"
    if result.returncode == 0:
        state = "loaded"
        for raw_line in result.stdout.splitlines():
            line = raw_line.strip()
            if line.startswith("state = "):
                state = line.split("=", 1)[1].strip()
            elif line.startswith("runs = "):
                runs = line.split("=", 1)[1].strip()
    return {
        "label": label,
        "target": target,
        "loaded": result.returncode == 0,
        "state": state,
        "runs": runs,
        "returncode": result.returncode,
    }


def _read_json_list_count(path: Path) -> tuple[int, ConfigIssue | None]:
    if not path.exists():
        return 0, None
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return 0, ConfigIssue(
            key="PCLOUD_TOOLS_MODE_STATE_READ",
            level="warning",
            message=f"cannot read mode dirty-state file {path}: {exc}",
        )
    if not isinstance(payload, list):
        return 0, ConfigIssue(
            key="PCLOUD_TOOLS_MODE_STATE_READ",
            level="warning",
            message=f"mode dirty-state file must be a JSON list: {path}",
        )
    return len(payload), None


def _rclone_cache_dir() -> Path:
    raw = os.environ.get("XDG_CACHE_HOME", "").strip()
    if raw:
        return Path(raw).expanduser() / "rclone"
    return Path.home() / "Library" / "Caches" / "rclone"


def _rclone_bisync_lock_file(config: AppConfig) -> Path:
    def encode(value: str) -> str:
        return value.replace("/", "_").replace(":", "_")

    session = f"local_{encode(str(config.core_dir))}..{encode(config.core_remote)}"
    return _rclone_cache_dir() / "bisync" / f"{session}.lck"


def _process_active(pid: str) -> str:
    if not pid or pid == "-":
        return "unknown"
    try:
        os.kill(int(pid), 0)
    except ValueError:
        return "unknown"
    except ProcessLookupError:
        return "no"
    except PermissionError:
        return "yes"
    return "yes"


def _rclone_bisync_lock_info(config: AppConfig) -> dict[str, object]:
    path = _rclone_bisync_lock_file(config)
    info: dict[str, object] = {
        "path": str(path),
        "status": "present" if path.exists() else "missing",
        "pid": "-",
        "process active": "unknown",
    }
    if not path.exists():
        return info
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return info
    if not isinstance(payload, dict):
        return info
    pid = str(payload.get("PID", "-") or "-")
    info["pid"] = pid
    info["process active"] = _process_active(pid)
    return info


def _dirty_state(
    config: AppConfig,
    service_launchd: dict[str, dict[str, object]],
) -> tuple[dict[str, object], list[ConfigIssue]]:
    issues: list[ConfigIssue] = []
    pushd_state = read_service_daemon_state(config, "pushd")
    diffd_state = read_service_daemon_state(config, "diffd")
    daemon_state = read_daemon_state(config)
    pushd_plan, _scope = build_pushd_plan(config, pushd_state)
    diffd_plan = build_diffd_plan(config, diffd_state, daemon_state)
    sync_lock = read_sync_lock_state(config)
    rclone_lock = _rclone_bisync_lock_info(config)
    issues.extend(pushd_state.issues)
    issues.extend(diffd_state.issues)
    issues.extend(daemon_state.issues)
    issues.extend(pushd_plan.issues)
    issues.extend(diffd_plan.issues)

    active_executor_labels = []
    for name in ("pushd-executor", "diffd-executor"):
        state = service_launchd.get(name, {}).get("state")
        if state == "running":
            active_executor_labels.append(name)

    manual_review = 0
    for path in (
        config.state_dir / "pushd" / "manual-review.json",
        config.state_dir / "diffd" / "manual-review.json",
    ):
        count, issue = _read_json_list_count(path)
        manual_review += count
        if issue:
            issues.append(issue)

    details = {
        "pushd queue records": pushd_state.queue_length,
        "pushd planned uploads": pushd_plan.upload_count,
        "pushd excluded records": pushd_plan.excluded_count,
        "pushd invalid records": pushd_plan.invalid_count,
        "diffd remote changes": diffd_plan.remote_change_count,
        "diffd pending downloads": diffd_plan.pending_download_count,
        "diffd planned downloads": diffd_plan.download_count,
        "diffd skipped downloads": diffd_plan.skipped_count,
        "manual review records": manual_review,
        "sync lock active": "yes" if sync_lock.active else "no",
        "sync lock status": sync_lock.status,
        "sync lock pid": sync_lock.pid,
        "rclone bisync lock status": rclone_lock["status"],
        "rclone bisync lock pid": rclone_lock["pid"],
        "rclone bisync lock process active": rclone_lock["process active"],
        "active executor services": active_executor_labels,
    }
    return details, issues


def _current_mode(daemon_loaded: bool, bisync_loaded: bool) -> str:
    if daemon_loaded and not bisync_loaded:
        return "daemon"
    if not daemon_loaded and not bisync_loaded:
        return "pause-or-maintenance"
    if not daemon_loaded and bisync_loaded:
        return "bisync-active-unmanaged"
    return "mixed-unsafe"


def _mode_snapshot(paths: RuntimePaths) -> tuple[AppConfig, dict[str, object], list[ConfigIssue]]:
    load_result = load_config(paths)
    config = load_result.config
    issues = list(load_result.issues)
    launchctl_bin = _command_v("launchctl")
    if not launchctl_bin:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_MODE_LAUNCHCTL",
                level="warning",
                message="launchctl was not found by command -v; mode status is limited",
            )
        )

    daemon_status: dict[str, dict[str, object]] = {}
    for service in _daemon_services():
        daemon_status[service["name"]] = {
            **_launchctl_print(launchctl_bin, service["label"]),
            "plist": str(_service_plist_path(service["label"])),
        }
    autosync = read_autosync_state(config)
    daemon_loaded = any(bool(item["loaded"]) for item in daemon_status.values())
    all_daemon_loaded = all(bool(item["loaded"]) for item in daemon_status.values())
    dirty, dirty_issues = _dirty_state(config, daemon_status)
    issues.extend(dirty_issues)
    snapshot = {
        "runtime": "development" if paths.dev_mode else "default",
        "current mode": _current_mode(daemon_loaded, autosync.loaded),
        "daemon services loaded": "yes" if all_daemon_loaded else "partial" if daemon_loaded else "no",
        "bisync loaded": "yes" if autosync.loaded else "no",
        "autosync state": autosync.state,
        "autosync label": autosync.label,
        "autosync plist": autosync.plist,
        "launchctl availability": "available" if launchctl_bin else "missing",
        "launchctl binary": launchctl_bin or "-",
        "daemon services": daemon_status,
        "dirty state": dirty,
    }
    return config, snapshot, issues


def _mode_actions(paths: RuntimePaths) -> list[ReportAction]:
    return [
        ReportAction(
            id="mode.status.refresh",
            label="Refresh mode status",
            command=action_command(paths, "mode.status.refresh"),
        ),
        ReportAction(
            id="mode.plan.daemon",
            label="Preview daemon mode switch",
            command=action_command(paths, "mode.plan.daemon"),
            terminal=True,
            refresh=False,
        ),
        ReportAction(
            id="mode.plan.maintenance",
            label="Preview maintenance mode switch",
            command=action_command(paths, "mode.plan.maintenance"),
            terminal=True,
            refresh=False,
        ),
        ReportAction(
            id="mode.plan.pause",
            label="Preview pause mode switch",
            command=action_command(paths, "mode.plan.pause"),
            terminal=True,
            refresh=False,
        ),
    ]


def _mode_status_report(paths: RuntimePaths) -> CommandReport:
    _config, snapshot, issues = _mode_snapshot(paths)
    issues = sort_issues(issues)
    return CommandReport(
        command="mode status",
        status=status_from_issues(issues),
        summary=f"pcloud-manager mode is {snapshot.get('current mode')}",
        details={
            **{key: value for key, value in snapshot.items() if key != "daemon services"},
            "state writes": "none",
        },
        issues=report_issues(issues),
        actions=_mode_actions(paths),
    )


def _planned_commands(target_mode: str, config: AppConfig, launchctl_bin: str) -> list[list[str]]:
    uid = str(os.getuid())
    commands: list[list[str]] = []
    bisync_target = f"gui/{uid}/{config.autosync_label}"
    commands.extend(
        [
            [launchctl_bin, "bootout", bisync_target],
            [launchctl_bin, "disable", bisync_target],
        ]
    )
    for service in _daemon_services():
        label = service["label"]
        target = f"gui/{uid}/{label}"
        plist = _service_plist_path(label)
        commands.append([launchctl_bin, "bootout", target])
        commands.append([launchctl_bin, "disable", target])
        if target_mode == "daemon":
            commands.append([launchctl_bin, "enable", target])
            commands.append([launchctl_bin, "bootstrap", f"gui/{uid}", str(plist)])
    return commands


def _approval_checks(args: argparse.Namespace, gate_open: bool) -> list[dict[str, str]]:
    return [
        {
            "name": "operator mode plan review",
            "status": "ok" if getattr(args, "operator_reviewed_mode_plan", False) else "pending",
            "detail": "operator reviewed current mode, dirty state, and planned launchctl commands",
        },
        {
            "name": "exclusive policy approval",
            "status": "ok" if getattr(args, "reviewer_approved_exclusive_policy", False) else "pending",
            "detail": "reviewer approved bisync and daemon automation remain mutually exclusive",
        },
        {
            "name": "launchd policy approval",
            "status": "ok" if getattr(args, "reviewer_approved_launchd_policy", False) else "pending",
            "detail": "reviewer approved bootout/disable/enable/bootstrap command set",
        },
        {
            "name": "rollback policy approval",
            "status": "ok" if getattr(args, "reviewer_approved_rollback_policy", False) else "pending",
            "detail": "reviewer approved using mode switch pause as rollback stop state",
        },
        {
            "name": "mode switch gate env",
            "status": "ok" if gate_open else "pending",
            "detail": f"{_MODE_SWITCH_GATE_ENV}={_MODE_SWITCH_GATE_VALUE}",
        },
    ]


def _dirty_blockers(dirty: dict[str, object]) -> list[str]:
    blockers: list[str] = []
    for key in (
        "pushd queue records",
        "pushd planned uploads",
        "pushd invalid records",
        "diffd remote changes",
        "diffd pending downloads",
        "diffd planned downloads",
        "manual review records",
    ):
        if int(dirty.get(key, 0) or 0) > 0:
            blockers.append(f"{key}={dirty[key]}")
    if dirty.get("sync lock active") == "yes":
        blockers.append(f"sync lock active pid={dirty.get('sync lock pid')}")
    if (
        dirty.get("rclone bisync lock status") == "present"
        and dirty.get("rclone bisync lock process active") == "yes"
    ):
        blockers.append(f"rclone bisync lock active pid={dirty.get('rclone bisync lock pid')}")
    active = dirty.get("active executor services")
    if isinstance(active, list) and active:
        blockers.append(f"active executor services={', '.join(str(item) for item in active)}")
    return blockers


def _mode_switch_state_file(config: AppConfig) -> Path:
    return config.state_dir / "mode" / "last-switch.json"


def _launchctl_bootout_missing_is_tolerable(command: list[str], result: subprocess.CompletedProcess[str]) -> bool:
    if len(command) < 2 or command[1] != "bootout" or result.returncode == 0:
        return False
    output = f"{result.stdout}\n{result.stderr}"
    return result.returncode == 3 and "No such process" in output


def _run_launchctl_commands(commands: list[list[str]]) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for command in commands:
        result = subprocess.run(command, check=False, capture_output=True, text=True)
        tolerated = _launchctl_bootout_missing_is_tolerable(command, result)
        results.append(
            {
                "command": shell_command(command),
                "argv": command,
                "returncode": result.returncode,
                "stdout": result.stdout[:2000],
                "stderr": result.stderr[:2000],
                "tolerated": tolerated,
                "tolerance reason": "service was not loaded" if tolerated else "-",
            }
        )
        if result.returncode != 0 and not tolerated:
            break
    return results


def _mode_plan_report(
    paths: RuntimePaths,
    target_mode: str,
    *,
    execute: bool,
    args: argparse.Namespace,
) -> CommandReport:
    config, snapshot, issues = _mode_snapshot(paths)
    launchctl_bin = str(snapshot.get("launchctl binary") or "-")
    if launchctl_bin == "-":
        launchctl_bin = "launchctl"
    dirty = snapshot["dirty state"]
    blockers = _dirty_blockers(dirty if isinstance(dirty, dict) else {})
    gate_open = os.environ.get(_MODE_SWITCH_GATE_ENV) == _MODE_SWITCH_GATE_VALUE
    approval_checks = _approval_checks(args, gate_open)
    approval_status = "complete" if all(check["status"] == "ok" for check in approval_checks) else "pending"
    commands = _planned_commands(target_mode, config, launchctl_bin)
    state_file = _mode_switch_state_file(config)

    if blockers:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_MODE_DIRTY_STATE",
                level="error" if execute else "warning",
                message="mode switch requires clean queues/locks: " + "; ".join(blockers),
            )
        )
    if execute and approval_status != "complete":
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_MODE_SWITCH_APPROVAL",
                level="error",
                message="mode switch requires plan review, exclusive policy, launchd policy, rollback policy, and gate env",
            )
        )
    if execute and snapshot.get("launchctl availability") != "available":
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_MODE_LAUNCHCTL",
                level="error",
                message="mode switch execution requires launchctl",
            )
        )

    details: dict[str, object] = {
        "target mode": target_mode,
        "current mode": snapshot.get("current mode"),
        "execute requested": "yes" if execute else "no",
        "mode switch gate": (
            f"open: {_MODE_SWITCH_GATE_VALUE}"
            if gate_open
            else f"closed: requires {_MODE_SWITCH_GATE_ENV}={_MODE_SWITCH_GATE_VALUE}"
        ),
        "mode switch can run": "yes" if approval_status == "complete" and not blockers else "no",
        "approval status": approval_status,
        "state writes": "mode switch state only" if execute else "none",
        "launchctl execution": "yes" if execute else "no",
        "launchctl availability": snapshot.get("launchctl availability"),
        "launchctl binary": snapshot.get("launchctl binary"),
        "planned launchctl commands": commands,
        "dirty blockers": blockers,
        "dirty state": dirty,
        "preflight checks": approval_checks,
        "mode switch state file": str(state_file),
        "checkpoint policy": "diffd checkpoint is never automatic; run diffd api-poll checkpoint separately if needed",
        "maintenance policy": "bisync is not automatically enabled or executed in maintenance mode",
        "blocked operations": [
            "upload/download transfer execution",
            "normal sync/resync",
            "rclone listing cache operations",
            "diffd API checkpoint",
            "autosync bisync enable/bootstrap",
        ],
    }

    if not execute or has_errors(issues):
        if has_errors(issues):
            details["state writes"] = "none"
            details["launchctl execution"] = "no"
        issues = sort_issues(issues)
        return CommandReport(
            command="mode switch" if execute else "mode plan",
            status=status_from_issues(issues),
            summary=(
                f"mode switch to {target_mode} is gated"
                if execute or issues
                else f"mode switch to {target_mode} is ready for review"
            ),
            details=details,
            issues=report_issues(issues),
            actions=_mode_actions(paths),
        )

    results = _run_launchctl_commands(commands)
    details["launchctl results"] = results
    failed = [result for result in results if result.get("returncode") != 0 and not result.get("tolerated")]
    if failed:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_MODE_SWITCH_LAUNCHCTL",
                level="error",
                message=f"mode switch launchctl command failed: {failed[0].get('stderr') or failed[0].get('stdout')}",
            )
        )
    if not has_errors(issues):
        payload = {
            "mode": target_mode,
            "switched_at": datetime.now(timezone.utc).isoformat(),
            "previous_mode": snapshot.get("current mode"),
            "commands": commands,
            "results": results,
        }
        state_file.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(state_file, payload, sort_keys=True)
        details["process result"] = payload
    else:
        details["state writes"] = "none"

    issues = sort_issues(issues)
    return CommandReport(
        command="mode switch",
        status=status_from_issues(issues),
        summary=(
            f"mode switch to {target_mode} completed"
            if not has_errors(issues)
            else f"mode switch to {target_mode} failed"
        ),
        details=details,
        issues=report_issues(issues),
        actions=_mode_actions(paths),
    )


def _render_mode_human(report: CommandReport) -> str:
    details = report.details
    lines = [
        f"{report.command}: {report.status}",
        report.summary,
        f"current mode: {details.get('current mode', '-')}",
        f"target mode: {details.get('target mode', '-')}",
        f"state writes: {details.get('state writes', '-')}",
        f"launchctl execution: {details.get('launchctl execution', '-')}",
        f"launchctl: {details.get('launchctl availability', '-')} ({details.get('launchctl binary', '-')})",
    ]
    if "mode switch gate" in details:
        lines.append(f"mode switch gate: {details.get('mode switch gate')}")
        lines.append(f"mode switch can run: {details.get('mode switch can run')}")
    dirty = details.get("dirty state")
    if isinstance(dirty, dict):
        lines.append("dirty state:")
        for key, value in dirty.items():
            lines.append(f"- {key}: {value}")
    commands = details.get("planned launchctl commands")
    if isinstance(commands, list) and commands:
        lines.append("planned launchctl commands:")
        for command in commands:
            lines.append(f"- {shell_command(command)}")
    results = details.get("launchctl results")
    if isinstance(results, list) and results:
        lines.append("launchctl results:")
        for result in results:
            tolerated = " tolerated" if result.get("tolerated") else ""
            lines.append(f"- {result.get('command')}: rc={result.get('returncode')}{tolerated}")
    checks = details.get("preflight checks")
    if isinstance(checks, list):
        pending = [check for check in checks if isinstance(check, dict) and check.get("status") != "ok"]
        if pending:
            lines.append("blocked checks:")
            for check in pending:
                lines.append(f"- {check.get('name')}: {check.get('status')} - {check.get('detail')}")
    if report.issues:
        lines.append("issues:" if report.status == "error" else "warnings:")
        for issue in report.issues:
            lines.append(f"- {issue.key}: {issue.message}")
    return "\n".join(lines)
