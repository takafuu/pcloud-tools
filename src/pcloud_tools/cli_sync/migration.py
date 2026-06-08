from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from ..autosync_runtime import read_autosync_state
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
from ..sync_exec import (
    DEFAULT_RESYNC_MODE,
    RESYNC_MODES,
    SyncExecutionError,
    bisync_listing_recovery_state,
    build_sync_plan,
    enforce_sync_scope_guard,
    execute_sync_plan,
)
from ..sync_runtime import (
    read_sync_lock_state,
    read_sync_state,
    sync_last_error_status,
)
from ..sync_scope import scope_issues, sync_allowlist_info
from .core import (
    _command_path,
    _readable_baseline,
    _readable_sync_scope_mode,
    _saved_shadow_report_check,
    _sync_status_actions,
)

def _rclone_cache_dir() -> Path:
    raw = os.environ.get("XDG_CACHE_HOME", "").strip()
    if raw:
        return Path(raw).expanduser() / "rclone"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "rclone"
    return Path.home() / ".cache" / "rclone"


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


def _allowlist_root(path: str) -> str:
    if not path or path == "-":
        return "-"
    candidate = Path(path).expanduser()
    if candidate.name == ".pcloud-sync-allowlist":
        return str(candidate.parent)
    return "-"


def _rclone_bisync_lock_info(config: AppConfig) -> dict[str, object]:
    path = _rclone_bisync_lock_file(config)
    info: dict[str, object] = {
        "path": str(path),
        "status": "present" if path.exists() else "missing",
        "pid": "-",
        "process active": "unknown",
        "session": "-",
        "time renewed": "-",
        "time expires": "-",
        "delete command": ["rclone", "deletefile", str(path)],
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
    info.update(
        {
            "pid": pid,
            "process active": _process_active(pid),
            "session": str(payload.get("Session", "-") or "-"),
            "time renewed": str(payload.get("TimeRenewed", "-") or "-"),
            "time expires": str(payload.get("TimeExpires", "-") or "-"),
        }
    )
    return info

def _render_migration_gate_human(report: CommandReport) -> str:
    details = report.details
    lines = [
        f"{report.command}: {report.status}",
        report.summary,
        f"migration gate: {details.get('migration gate status', '-')}",
        f"sync/resync can run: {details.get('sync/resync can run', '-')}",
        f"state writes: {details.get('state writes', '-')}",
        f"sync state: {details.get('sync state', '-')}",
        f"last result: {details.get('last result', '-')}",
        f"last error status: {details.get('last error status', '-')}",
        f"target root: {details.get('migration target root status', '-')} ({details.get('configured core dir', '-')})",
        f"sync lock: {details.get('sync lock status', '-')}",
        (
            "rclone bisync lock: "
            f"{details.get('rclone bisync lock status', '-')} "
            f"(pid={details.get('rclone bisync lock pid', '-')}; "
            f"process_active={details.get('rclone bisync lock process active', '-')})"
        ),
        (
            "scope: "
            f"{details.get('scope status', '-')}; "
            f"{details.get('scope baseline', '-')}; "
            f"entries={details.get('scope entries', '-')}"
        ),
        f"rclone: {details.get('rclone availability', '-')} ({details.get('rclone binary', '-')})",
        f"approval status: {details.get('migration approval status', '-')}",
        f"human gate: {details.get('human gate status', '-')}",
        f"next human check trigger: {details.get('next human check trigger', '-')}",
    ]
    commands = [
        ("status command", details.get("status command")),
        ("normal sync preview", details.get("normal sync preview command")),
        ("resync preview", details.get("resync preview command")),
    ]
    for label, command in commands:
        if command:
            lines.append(f"{label}: {shell_command(command)}")
    rclone_lock_delete = details.get("rclone bisync lock delete command")
    if details.get("rclone bisync lock status") == "present" and rclone_lock_delete:
        lines.append(f"rclone lock cleanup candidate: {shell_command(rclone_lock_delete)}")
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


def _sync_migration_run_state_file(config: AppConfig) -> Path:
    return config.state_dir / "sync" / "migration-last-run.json"


def _sync_migration_gate_spec():
    return GATES["sync.migration"]


def _sync_migration_gate_open(config: AppConfig) -> bool:
    spec = _sync_migration_gate_spec()
    return config.sync_migration_gate == spec.expected_value


def _render_migration_run_human(report: CommandReport) -> str:
    details = report.details
    lines = [
        f"{report.command}: {report.status}",
        report.summary,
        f"migration run gate: {details.get('migration run gate status', '-')}",
        f"sync/resync can run: {details.get('sync/resync can run', '-')}",
        f"mode: {details.get('mode', '-')}",
        f"execute requested: {details.get('execute requested', '-')}",
        f"state writes: {details.get('state writes', '-')}",
        f"sync state: {details.get('sync state', '-')}",
        f"last result: {details.get('last result', '-')}",
        f"target root: {details.get('migration target root status', '-')} ({details.get('configured core dir', '-')})",
        f"sync lock: {details.get('sync lock status', '-')}",
        (
            "rclone bisync lock: "
            f"{details.get('rclone bisync lock status', '-')} "
            f"(pid={details.get('rclone bisync lock pid', '-')}; "
            f"process_active={details.get('rclone bisync lock process active', '-')})"
        ),
        f"rclone: {details.get('rclone availability', '-')} ({details.get('rclone binary', '-')})",
        f"approval status: {details.get('migration approval status', '-')}",
    ]
    command = details.get("planned sync command")
    if command:
        lines.append(f"planned sync command: {shell_command(command)}")
    state_file = details.get("migration run state file")
    if state_file:
        lines.append(f"migration run state: {state_file}")
    rclone_lock_delete = details.get("rclone bisync lock delete command")
    if details.get("rclone bisync lock status") == "present" and rclone_lock_delete:
        lines.append(f"rclone lock cleanup candidate: {shell_command(rclone_lock_delete)}")
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


def _saved_sync_status_report(
    path: Path | None,
) -> tuple[dict[str, object] | None, dict[str, str] | None, list[ConfigIssue]]:
    if path is None:
        return None, None, []
    if not path.exists():
        return (
            None,
            {
                "name": "saved sync status report",
                "status": "pending",
                "detail": f"sync status report not found: {path}",
            },
            [
                ConfigIssue(
                    key="PCLOUD_TOOLS_SYNC_STATUS_REPORT",
                    level="warning",
                    message=f"sync status report not found: {path}",
                )
            ],
        )
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        return (
            None,
            {
                "name": "saved sync status report",
                "status": "pending",
                "detail": f"invalid JSON: {exc}",
            },
            [
                ConfigIssue(
                    key="PCLOUD_TOOLS_SYNC_STATUS_REPORT",
                    level="warning",
                    message=f"invalid sync status report JSON: {path}: {exc}",
                )
            ],
        )
    details = payload.get("details")
    if payload.get("command") != "sync status" or not isinstance(details, dict):
        return (
            None,
            {
                "name": "saved sync status report",
                "status": "pending",
                "detail": f"not a sync status report: {path}",
            },
            [
                ConfigIssue(
                    key="PCLOUD_TOOLS_SYNC_STATUS_REPORT",
                    level="warning",
                    message=f"saved report is not a sync status report: {path}",
                )
            ],
        )
    return (
        dict(details),
        {
            "name": "saved sync status report",
            "status": "ok",
            "detail": str(path),
        },
        [],
    )


def _sync_migration_gate_report(args: argparse.Namespace, paths: RuntimePaths) -> CommandReport:
    load_result = load_config(paths)
    config = load_result.config
    migration_gate = validate_gate(
        _sync_migration_gate_spec(),
        args,
        {_sync_migration_gate_spec().env_var: config.sync_migration_gate},
    )
    sync_state = read_sync_state(config)
    lock_state = read_sync_lock_state(config)
    rclone_lock = _rclone_bisync_lock_info(config)
    scope = sync_allowlist_info(config)
    autosync = read_autosync_state(config)
    listing_recovery = bisync_listing_recovery_state(config)
    baseline_label = _readable_baseline(scope.baseline.file, scope.baseline.mode, scope.baseline.status)
    issues = list(load_result.issues) + scope_issues(scope)
    shadow_check, shadow_issues = _saved_shadow_report_check(
        getattr(args, "report_path", None),
        issue_key="PCLOUD_TOOLS_SYNC_MIGRATION_SHADOW_REPORT",
    )
    issues.extend(shadow_issues)
    saved_status, saved_status_check, saved_status_issues = _saved_sync_status_report(
        getattr(args, "sync_status_report_path", None)
    )
    issues.extend(saved_status_issues)
    rclone_bin = _command_path("rclone")
    rclone_check = {
        "name": "rclone binary",
        "status": "ok" if rclone_bin else "pending",
        "detail": rclone_bin or "rclone not found by command -v; verify before sync/resync validation",
    }
    if not rclone_bin:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_SYNC_MIGRATION_RCLONE",
                level="warning",
                message="rclone was not found by command -v; migration gate remains closed",
            )
        )
    status_source = "saved sync status report" if saved_status is not None else "runtime"
    sync_state_value = str(saved_status.get("sync state", sync_state.state)) if saved_status else sync_state.state
    last_result = str(saved_status.get("last result", sync_state.last_sync)) if saved_status else sync_state.last_sync
    last_error = str(saved_status.get("last error", sync_state.last_error)) if saved_status else sync_state.last_error
    last_error_status = (
        str(saved_status.get("last error status", sync_last_error_status(sync_state)))
        if saved_status
        else sync_last_error_status(sync_state)
    )
    sync_lock_status = (
        str(saved_status.get("sync lock status", lock_state.status)) if saved_status else lock_state.status
    )
    sync_lock_active = (
        str(saved_status.get("sync lock active", "yes" if lock_state.active else "no"))
        if saved_status
        else "yes" if lock_state.active else "no"
    )
    sync_lock_pid = str(saved_status.get("sync lock pid", lock_state.pid)) if saved_status else lock_state.pid
    sync_lock_mode = str(saved_status.get("sync lock mode", lock_state.mode)) if saved_status else lock_state.mode
    sync_lock_started = (
        str(saved_status.get("sync lock started", lock_state.started_at)) if saved_status else lock_state.started_at
    )
    scope_status = str(saved_status.get("scope status", scope.allowlist_status)) if saved_status else scope.allowlist_status
    scope_entries = saved_status.get("scope entries", scope.allowlist_count) if saved_status else scope.allowlist_count
    scope_baseline = (
        str(saved_status.get("last resync scope", baseline_label)) if saved_status else baseline_label
    )
    allowlist_path = str(saved_status.get("allowlist", scope.allowlist_file)) if saved_status else str(scope.allowlist_file)
    saved_allowlist_root = _allowlist_root(allowlist_path)
    configured_core_dir = str(config.core_dir)
    target_root_ok = saved_allowlist_root in {"-", configured_core_dir}
    target_root_check = {
        "name": "migration target root",
        "status": "ok" if target_root_ok else "pending",
        "detail": f"configured_core_dir={configured_core_dir}; saved_allowlist_root={saved_allowlist_root}; source={status_source}",
    }
    if not target_root_ok:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_SYNC_MIGRATION_TARGET_ROOT",
                level="warning",
                message=(
                    "saved sync status appears to describe a different core root than this command would sync: "
                    f"{saved_allowlist_root} != {configured_core_dir}"
                ),
            )
        )
    autosync_state = str(saved_status.get("autosync state", autosync.state)) if saved_status else autosync.state
    autosync_runs = str(saved_status.get("autosync runs", autosync.runs)) if saved_status else autosync.runs
    sync_ok = sync_state_value == "synced" and "SUCCESS" in last_result
    sync_state_check = {
        "name": "latest sync status",
        "status": "ok" if sync_ok else "pending",
        "detail": f"{sync_state_value}; last={last_result}; last_error_status={last_error_status}; source={status_source}",
    }
    lock_ok = sync_lock_status == "missing" and sync_lock_active != "yes"
    lock_check = {
        "name": "sync lock",
        "status": "ok" if lock_ok else "pending",
        "detail": f"{sync_lock_status}; active={sync_lock_active}; pid={sync_lock_pid}; source={status_source}",
    }
    rclone_lock_present = rclone_lock["status"] == "present"
    rclone_lock_check = {
        "name": "rclone bisync lock",
        "status": "pending" if rclone_lock_present else "ok",
        "detail": (
            f"{rclone_lock['status']}; pid={rclone_lock['pid']}; "
            f"process_active={rclone_lock['process active']}; path={rclone_lock['path']}"
        ),
    }
    if rclone_lock_present:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_SYNC_MIGRATION_RCLONE_LOCK",
                level="warning",
                message=(
                    "rclone bisync prior lock exists; inspect it before running migration validation: "
                    f"{rclone_lock['path']}"
                ),
            )
        )
    scope_ok = scope_status == "loaded" and (saved_status is not None or scope.baseline.status != "invalid")
    scope_check = {
        "name": "document/media scope",
        "status": "ok" if scope_ok else "pending",
        "detail": f"{scope_baseline}; entries={scope_entries}; allowlist={allowlist_path}; source={status_source}",
    }
    entrypoint = entrypoint_command(paths)
    status_command = [entrypoint, "sync", "status", "--json"]
    normal_preview = [entrypoint, "sync", "--json"]
    resync_preview = [entrypoint, "sync", "resync", "--resync-mode", DEFAULT_RESYNC_MODE, "--json"]
    checks = [
        shadow_check,
        rclone_check,
    ]
    if saved_status_check is not None:
        checks.append(saved_status_check)
    checks.extend([
        sync_state_check,
        lock_check,
        rclone_lock_check,
        target_root_check,
        scope_check,
        {
            "name": "sync preview commands",
            "status": "ok",
            "detail": f"{' '.join(normal_preview)}; {' '.join(resync_preview)}",
        },
        {
            "name": "operator status review",
            "status": "ok" if migration_gate.flag_ok("--operator-reviewed-status") else "pending",
            "detail": "operator reviewed sync status, latest result, lock state, and preview commands",
        },
        {
            "name": "scope approval",
            "status": "ok" if migration_gate.flag_ok("--reviewer-approved-scope") else "pending",
            "detail": "reviewer approved document/media allowlist scope before migration validation",
        },
        {
            "name": "rollback policy approval",
            "status": "ok" if migration_gate.flag_ok("--reviewer-approved-rollback-policy") else "pending",
            "detail": "reviewer approved listing/filter rollback backups and restore commands before validation",
        },
        {
            "name": "stop conditions approval",
            "status": "ok" if migration_gate.flag_ok("--reviewer-approved-stop-conditions") else "pending",
            "detail": "reviewer approved stop conditions for sync errors, locks, broad scope, or unexpected transfer plan",
        },
        {
            "name": "parallel dangerous gates",
            "status": "ok",
            "detail": "fswatch resident start, pCloud API long-poll start, autosync launchd changes, and archive work stay out of scope",
        },
    ])
    approval_status = "complete-read-only" if all(check["status"] == "ok" for check in checks) else "pending"
    issues.append(
        ConfigIssue(
            key=migration_gate.spec.env_var,
            level="warning",
            message="normal sync/resync migration validation remains gated; this command is read-only",
        )
    )
    issues = sort_issues(issues)
    details: dict[str, object] = {
        "planned action": "check normal sync/resync migration validation prerequisites",
        "implementation status": "read-only checklist; sync/resync is not executed",
        "migration gate status": "closed",
        "sync/resync can run": "no",
        "operator verification required": "yes-before-sync-migration-validation",
        "human gate status": "required-before-sync-migration-validation",
        "human gate reason": "normal sync/resync validation would run live rclone bisync against the configured remote",
        "state writes": "none",
        "core remote": config.core_remote,
        "configured core dir": configured_core_dir,
        "saved allowlist root": saved_allowlist_root,
        "migration target root status": "ok" if target_root_ok else "mismatch",
        "sync status source": status_source,
        "sync status report": str(getattr(args, "sync_status_report_path", None) or "-"),
        "sync state": sync_state_value,
        "last result": last_result,
        "last error": last_error,
        "last error status": last_error_status,
        "sync lock status": sync_lock_status,
        "sync lock active": sync_lock_active,
        "sync lock pid": sync_lock_pid,
        "sync lock mode": sync_lock_mode,
        "sync lock started": sync_lock_started,
        "rclone bisync lock status": rclone_lock["status"],
        "rclone bisync lock path": rclone_lock["path"],
        "rclone bisync lock pid": rclone_lock["pid"],
        "rclone bisync lock process active": rclone_lock["process active"],
        "rclone bisync lock time renewed": rclone_lock["time renewed"],
        "rclone bisync lock time expires": rclone_lock["time expires"],
        "rclone bisync lock delete command": rclone_lock["delete command"],
        "autosync state": autosync_state,
        "autosync runs": autosync_runs,
        "scope status": scope_status,
        "scope baseline": scope_baseline,
        "scope entries": scope_entries,
        "allowlist": allowlist_path,
        "rclone availability": "available" if rclone_bin else "missing",
        "rclone binary": rclone_bin or "-",
        "listing recovery available": "yes" if listing_recovery.can_recover else "no",
        "path1 list": str(listing_recovery.path1_lst),
        "path2 list": str(listing_recovery.path2_lst),
        "path1 err": str(listing_recovery.path1_err),
        "path2 err": str(listing_recovery.path2_err),
        "status command": status_command,
        "normal sync preview command": normal_preview,
        "resync preview command": resync_preview,
        "migration approval status": approval_status,
        "preflight checks": checks,
        "success policy": "run only the explicitly approved validation command and re-check sync status/logs afterward",
        "failure policy": "stop on sync error, active/stale lock, broad-scope warning, or unexpected transfer plan",
        "rollback policy": "use saved listing/filter rollback backups; do not delete listing cache automatically",
        "blocked operations": [
            "normal sync execution",
            "resync execution",
            "full-resync execution",
            "track-renames execution",
            "listing cache deletion or movement",
            "autosync launchd changes",
        ],
        "next human check trigger": "explicit normal sync/resync validation command or listing rollback decision",
    }
    return CommandReport(
        command="sync migration-gate",
        status=status_from_issues(issues),
        summary="sync migration validation gate is closed",
        details=details,
        issues=report_issues(issues),
        actions=_sync_status_actions(paths, lock_state.status),
    )


def cmd_sync_migration_gate(args: argparse.Namespace, paths: RuntimePaths) -> int:
    report = _sync_migration_gate_report(args, paths)
    output_format = "xbar" if getattr(args, "xbar", False) else "json" if getattr(args, "json", False) else "human"
    if output_format == "human":
        print(_render_migration_gate_human(report))
    else:
        print(render_report(report, output_format=output_format))
    return exit_code_for_report(report)


def _sync_migration_run_report(args: argparse.Namespace, paths: RuntimePaths) -> CommandReport:
    gate_report = _sync_migration_gate_report(args, paths)
    load_result = load_config(paths)
    config = load_result.config
    mode = str(getattr(args, "mode", "normal"))
    plan_mode = "normal" if mode == "normal" else "resync"
    execute = bool(getattr(args, "execute", False))
    details = dict(gate_report.details)
    issues = [
        ConfigIssue(key=issue.key, level=issue.level, message=issue.message)
        for issue in gate_report.issues
        if issue.key != "PCLOUD_TOOLS_SYNC_MIGRATION_GATE"
    ]
    approval_status = str(details.get("migration approval status", "pending"))
    migration_spec = _sync_migration_gate_spec()
    gate_open = _sync_migration_gate_open(config)
    state_file = _sync_migration_run_state_file(config)
    rclone_bin = _command_path("rclone")
    plan = None

    details.update(
        {
            "planned action": "run sync migration validation" if execute else "preview sync migration run",
            "implementation status": (
                "guarded rclone bisync validation path"
                if execute
                else "sync migration run preview only; rclone bisync is not executed"
            ),
            "mode": mode,
            "migration run gate status": (
                f"open: {migration_spec.expected_value}"
                if gate_open
                else f"closed: requires {migration_spec.env_var}={migration_spec.expected_value}"
            ),
            "migration gate status": (
                f"open: {migration_spec.expected_value}"
                if gate_open
                else f"closed: requires {migration_spec.env_var}={migration_spec.expected_value}"
            ),
            "sync/resync can run": "yes" if gate_open and approval_status == "complete-read-only" else "no",
            "execute requested": "yes" if execute else "no",
            "state writes": "sync logs, lock, status, and migration run state" if execute else "none",
            "migration run state file": str(state_file),
            "future gate env": f"{migration_spec.env_var}={migration_spec.expected_value}",
        }
    )

    if approval_status != "complete-read-only":
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_SYNC_MIGRATION_APPROVAL",
                level="error" if execute else "warning",
                message="sync migration execution requires complete read-only approvals",
            )
        )
    if not gate_open:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_SYNC_MIGRATION_EXECUTION_GATE",
                level="error" if execute else "warning",
                message=(
                    "sync migration execution requires "
                    f"{migration_spec.env_var}={migration_spec.expected_value!r}"
                ),
            )
        )
    if not rclone_bin:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_SYNC_MIGRATION_RCLONE",
                level="error" if execute else "warning",
                message="rclone command not found",
            )
        )

    if rclone_bin and gate_open and approval_status == "complete-read-only":
        try:
            scope_info = sync_allowlist_info(config)
            plan = build_sync_plan(
                config,
                plan_mode,
                scope_info.entries,
                rclone_bin=rclone_bin,
                resync_mode=getattr(args, "resync_mode", DEFAULT_RESYNC_MODE),
            )
            details.update(
                {
                    "scope mode": _readable_sync_scope_mode(plan.scope_mode),
                    "planned sync command": list(plan.command),
                    "rclone log": str(plan.rclone_log),
                    "stdout log": str(plan.stdout_log),
                    "stderr log": str(plan.stderr_log),
                    "filter file": str(plan.filter_file) if plan.filter_file is not None else "-",
                    "resync mode": plan.resync_mode or "-",
                }
            )
        except SyncExecutionError as exc:
            issues.append(
                ConfigIssue(
                    key="PCLOUD_TOOLS_SYNC_MIGRATION_PLAN",
                    level="error",
                    message=str(exc),
                )
            )

    if not execute or has_errors(issues):
        if has_errors(issues):
            details["state writes"] = "none"
        issues = sort_issues(issues)
        return CommandReport(
            command="sync migration-run",
            status=status_from_issues(issues),
            summary=(
                "sync migration execution is gated"
                if has_errors(issues) or not gate_open or approval_status != "complete-read-only"
                else "sync migration run is ready"
            ),
            details=details,
            issues=report_issues(issues),
            actions=_sync_status_actions(paths, "missing"),
        )

    assert plan is not None
    started_at = datetime.now(timezone.utc).isoformat()
    result = None
    try:
        result = execute_sync_plan(config, plan)
    except SyncExecutionError as exc:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_SYNC_MIGRATION_EXEC",
                level="error",
                message=str(exc),
            )
        )
    finished_at = datetime.now(timezone.utc).isoformat()
    exit_code = result.exit_code if result is not None else 1
    run_state = {
        "mode": mode,
        "started_at": started_at,
        "finished_at": finished_at,
        "command": list(plan.command),
        "exit_code": exit_code,
        "scope_recorded": result.scope_recorded if result is not None else False,
        "listings_recovered": result.listings_recovered if result is not None else False,
        "rclone_log": str(plan.rclone_log),
        "stdout_log": str(plan.stdout_log),
        "stderr_log": str(plan.stderr_log),
    }
    if result is not None and result.exit_code != 0:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_SYNC_MIGRATION_EXIT",
                level="error",
                message=f"sync migration validation failed with exit={result.exit_code}",
            )
        )
    if not has_errors(issues):
        state_file.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(state_file, run_state, sort_keys=True)

    details.update(
        {
            "exit code": exit_code,
            "scope recorded": "yes" if run_state["scope_recorded"] else "no",
            "listings recovered": "yes" if run_state["listings_recovered"] else "no",
            "process result": run_state,
            "state writes": "sync logs, lock, status, and migration run state" if not has_errors(issues) else "none",
        }
    )
    issues = sort_issues(issues)
    return CommandReport(
        command="sync migration-run",
        status=status_from_issues(issues),
        summary="sync migration run completed" if not has_errors(issues) else "sync migration run failed",
        details=details,
        issues=report_issues(issues),
        actions=_sync_status_actions(paths, "missing"),
    )


def cmd_sync_migration_run(args: argparse.Namespace, paths: RuntimePaths) -> int:
    report = _sync_migration_run_report(args, paths)
    output_format = "xbar" if getattr(args, "xbar", False) else "json" if getattr(args, "json", False) else "human"
    if output_format == "human":
        print(_render_migration_run_human(report))
    else:
        print(render_report(report, output_format=output_format))
    return exit_code_for_report(report)
