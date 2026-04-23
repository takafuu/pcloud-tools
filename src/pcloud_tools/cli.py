from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from .config import ConfigIssue, load_config, repair_allowlist_file, repair_env_file
from .mount_ops import (
    MountCommandError,
    MountExecutionError,
    execute_mount,
    execute_umount,
    mount_layer_state,
    preview_mount_operations,
    preview_umount_operations,
    resolve_layers,
)
from .output import CommandReport, ReportIssue, render_report
from .runtime import RuntimePaths, detect_runtime_paths
from .sync_exec import (
    SyncExecutionError,
    build_sync_plan,
    enforce_sync_scope_guard,
    execute_sync_plan,
)
from .sync_runtime import (
    parse_sync_progress,
    read_latest_sync_logs,
    read_sync_lock_state,
    read_sync_state,
)
from .sync_scope import (
    prepare_sync_filter_rules,
    scope_issues,
    sync_allowlist_info,
    sync_filter_file,
    sync_scope_baseline_info,
    write_sync_filter_file,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pcloud-manager-dev",
        description="Development CLI for the pcloud-tools migration.",
    )
    subparsers = parser.add_subparsers(dest="command")

    status_parser = subparsers.add_parser("status", help="Show runtime status.")
    status_parser.add_argument(
        "--detail",
        action="store_true",
        help="Show the development runtime paths as a detailed block.",
    )
    status_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit structured JSON output.",
    )

    doctor_parser = subparsers.add_parser("doctor", help="Check runtime scaffold health.")
    doctor_parser.add_argument(
        "--repair",
        action="store_true",
        help="Create a starter .env file if it is missing.",
    )
    doctor_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit structured JSON output.",
    )

    sync_parser = subparsers.add_parser("sync", help="Sync command surface scaffold.")
    sync_subparsers = sync_parser.add_subparsers(dest="sync_command")
    sync_status_parser = sync_subparsers.add_parser("status")
    sync_status_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit structured JSON output.",
    )
    sync_scope_parser = sync_subparsers.add_parser("scope")
    sync_scope_parser.add_argument(
        "--filter",
        action="store_true",
        help="Include the generated bisync filter rules.",
    )
    sync_scope_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit structured JSON output.",
    )
    sync_check_parser = sync_subparsers.add_parser("check-allowlist")
    sync_check_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit structured JSON output.",
    )
    sync_progress_parser = sync_subparsers.add_parser("progress")
    sync_progress_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit structured JSON output.",
    )
    for name in ("resync", "full-resync", "track-renames"):
        command_parser = sync_subparsers.add_parser(name)
        command_parser.add_argument(
            "--execute",
            action="store_true",
            help="Run the rclone bisync command instead of only previewing it.",
        )
        command_parser.add_argument(
            "--json",
            action="store_true",
            help="Emit structured JSON output.",
        )

    mount_parser = subparsers.add_parser("mount", help="Preview or run mount operations.")
    mount_parser.add_argument("target", choices=("vault", "crypt", "all"))
    mount_parser.add_argument("--engine", help="Override engine for a single target mount.")
    mount_parser.add_argument("--port", type=int, help="Override port for a single target mount.")
    mount_parser.add_argument("--vault-port", type=int, help="Override vault port.")
    mount_parser.add_argument("--crypt-port", type=int, help="Override crypt port.")
    mount_parser.add_argument(
        "--execute",
        action="store_true",
        help="Run the mount operations instead of only previewing them.",
    )
    mount_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit structured JSON output.",
    )

    umount_parser = subparsers.add_parser("umount", help="Preview or run unmount operations.")
    umount_parser.add_argument("target", choices=("vault", "crypt", "all"))
    umount_parser.add_argument("--port", type=int, help="Override port for a single target umount.")
    umount_parser.add_argument("--vault-port", type=int, help="Override vault port.")
    umount_parser.add_argument("--crypt-port", type=int, help="Override crypt port.")
    umount_parser.add_argument(
        "--execute",
        action="store_true",
        help="Run the unmount operations instead of only previewing them.",
    )
    umount_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit structured JSON output.",
    )

    index_parser = subparsers.add_parser("index", help="Run the configured indexer script.")
    index_parser.add_argument("index_args", nargs=argparse.REMAINDER)

    return parser


def _config_summary(paths: RuntimePaths) -> dict[str, str]:
    load_result = load_config(paths)
    config = load_result.config
    return {
        "config source": load_result.source,
        "core dir": str(config.core_dir),
        "state dir": str(config.state_dir),
        "log dir": str(config.log_dir),
        "allowlist": str(config.allowlist_file),
        "core remote": config.core_remote,
        "vault layer": "enabled" if config.enable_vault_layer else "disabled",
        "crypt layer": "enabled" if config.enable_crypt_layer else "disabled",
    }


def _has_errors(issues: list[ConfigIssue]) -> bool:
    return any(issue.level == "error" for issue in issues)


def _has_warnings(issues: list[ConfigIssue]) -> bool:
    return any(issue.level == "warning" for issue in issues)


def _status_from_issues(issues: list[ConfigIssue]) -> str:
    if _has_errors(issues):
        return "error"
    if _has_warnings(issues):
        return "warning"
    return "ok"


def _report_issues(issues: list[ConfigIssue]) -> list[ReportIssue]:
    return [ReportIssue(level=issue.level, key=issue.key, message=issue.message) for issue in issues]


def _exit_code_for_report(report: CommandReport) -> int:
    return 1 if report.status == "error" else 0


def _sort_issues(issues: list[ConfigIssue]) -> list[ConfigIssue]:
    return sorted(issues, key=_issue_sort_key)


def _status_report(args: argparse.Namespace, paths: RuntimePaths) -> CommandReport:
    load_result = load_config(paths)
    issues = _sort_issues(list(load_result.issues))
    mode = "dev" if paths.dev_mode else "default"
    sync_state = read_sync_state(load_result.config)
    lock_state = read_sync_lock_state(load_result.config)
    log_pointers = read_latest_sync_logs(load_result.config)
    scope_info = sync_allowlist_info(load_result.config)
    details = {
        "workspace": str(paths.workspace_root),
        "config dir": str(paths.config_dir),
        "state dir": str(paths.state_dir),
        "log dir": str(paths.log_dir),
        "env file": str(paths.env_file),
    }
    if args.detail:
        details.update(_config_summary(paths))
        details.update(
            {
                "sync state": sync_state.state,
                "sync activity": sync_state.activity,
                "current sync log": sync_state.current_log,
                "last sync result": sync_state.last_sync,
                "last sync error": sync_state.last_error,
                "scope status": scope_info.allowlist_status,
                "scope entries": scope_info.allowlist_count,
                "scope baseline": _readable_baseline(
                    scope_info.baseline.file,
                    scope_info.baseline.mode,
                    scope_info.baseline.status,
                ),
                "filter file": str(sync_filter_file(load_result.config)),
                "sync lock active": "yes" if lock_state.active else "no",
                "sync lock pid": lock_state.pid,
                "sync lock mode": lock_state.mode,
                "sync lock started": lock_state.started_at,
                "latest rclone log": log_pointers.latest_rclone_log,
                "latest stdout log": log_pointers.latest_stdout_log,
                "latest stderr log": log_pointers.latest_stderr_log,
            }
        )
    return CommandReport(
        command="status",
        status=_status_from_issues(issues),
        summary=f"pcloud-manager-dev scaffold is ready ({mode})",
        details=details,
        issues=_report_issues(issues),
    )


def cmd_status(args: argparse.Namespace, paths: RuntimePaths) -> int:
    report = _status_report(args, paths)
    print(render_report(report, as_json=args.json))
    return _exit_code_for_report(report)


def _issue_sort_key(issue: ConfigIssue) -> tuple[int, str]:
    priority = 0 if issue.level == "error" else 1
    return (priority, issue.key)


def _doctor_report(args: argparse.Namespace, paths: RuntimePaths) -> tuple[CommandReport, bool]:
    repaired_items: list[str] = []
    if args.repair:
        env_missing = not paths.env_file.exists()
        repair_env_file(paths)
        if env_missing and paths.env_file.exists():
            repaired_items.append(f"env file: {paths.env_file}")

    load_result = load_config(paths)
    if args.repair:
        allowlist_missing = not load_result.config.allowlist_file.exists()
        repair_allowlist_file(load_result.config, paths)
        if allowlist_missing and load_result.config.allowlist_file.exists():
            repaired_items.append(f"allowlist file: {load_result.config.allowlist_file}")
        load_result = load_config(paths)

    issues = _sort_issues(list(load_result.issues))
    has_errors = _has_errors(issues)
    status = _status_from_issues(issues)
    sync_state = read_sync_state(load_result.config)
    lock_state = read_sync_lock_state(load_result.config)
    log_pointers = read_latest_sync_logs(load_result.config)
    scope_info = sync_allowlist_info(load_result.config)
    progress = parse_sync_progress(load_result.config)
    details = {
        "config dir": f"{'present' if paths.config_dir.exists() else 'missing'} ({paths.config_dir})",
        "state dir": f"{'present' if paths.state_dir.exists() else 'missing'} ({paths.state_dir})",
        "log dir": f"{'present' if paths.log_dir.exists() else 'missing'} ({paths.log_dir})",
        "env file": f"{'present' if paths.env_file.exists() else 'missing'} ({paths.env_file})",
        "config source": load_result.source,
        "core dir": str(load_result.config.core_dir),
        "allowlist": str(load_result.config.allowlist_file),
        "core remote": load_result.config.core_remote,
        "vault layer": "enabled" if load_result.config.enable_vault_layer else "disabled",
        "crypt layer": "enabled" if load_result.config.enable_crypt_layer else "disabled",
        "sync state": sync_state.state,
        "sync activity": sync_state.activity,
        "scope status": scope_info.allowlist_status,
        "scope entries": scope_info.allowlist_count,
        "scope baseline": _readable_baseline(
            scope_info.baseline.file,
            scope_info.baseline.mode,
            scope_info.baseline.status,
        ),
        "filter file": str(sync_filter_file(load_result.config)),
        "sync lock active": "yes" if lock_state.active else "no",
        "sync lock pid": lock_state.pid,
        "sync lock mode": lock_state.mode,
        "sync lock started": lock_state.started_at,
        "latest rclone log": log_pointers.latest_rclone_log,
        "latest stdout log": log_pointers.latest_stdout_log,
        "latest stderr log": log_pointers.latest_stderr_log,
        "progress source": str(progress.log_path) if progress is not None else "-",
    }
    if repaired_items:
        details["repair"] = "; ".join(f"created {item}" for item in repaired_items)

    report = CommandReport(
        command="doctor",
        status=status,
        summary="configuration looks healthy" if not issues else "configuration has review items",
        details=details,
        issues=_report_issues(issues),
    )
    return report, has_errors


def cmd_doctor(args: argparse.Namespace, paths: RuntimePaths) -> int:
    paths.ensure_directories()
    report, has_errors = _doctor_report(args, paths)
    print(render_report(report, as_json=args.json))
    return 1 if has_errors else 0


def _sync_status_report(paths: RuntimePaths) -> CommandReport:
    load_result = load_config(paths)
    issues = _sort_issues(list(load_result.issues))
    sync_state = read_sync_state(load_result.config)
    lock_state = read_sync_lock_state(load_result.config)
    log_pointers = read_latest_sync_logs(load_result.config)
    scope_info = sync_allowlist_info(load_result.config)
    details = {
        "runtime": "development",
        "sync engine": "bisync fallback scaffold",
        "running": "yes" if sync_state.state == "syncing" else "no",
        "config source": load_result.source,
        "state dir": str(load_result.config.state_dir),
        "allowlist": str(load_result.config.allowlist_file),
        "scope status": scope_info.allowlist_status,
        "scope entries": scope_info.allowlist_count,
        "last resync scope": _readable_baseline(
            scope_info.baseline.file,
            scope_info.baseline.mode,
            scope_info.baseline.status,
        ),
        "filter file": str(sync_filter_file(load_result.config)),
        "core remote": load_result.config.core_remote,
        "sync state": sync_state.state,
        "sync activity": sync_state.activity,
        "current log": sync_state.current_log,
        "sync lock active": "yes" if lock_state.active else "no",
        "sync lock pid": lock_state.pid,
        "sync lock mode": lock_state.mode,
        "sync lock started": lock_state.started_at,
        "last result": sync_state.last_sync,
        "last error": sync_state.last_error,
        "last log": log_pointers.latest_rclone_log,
        "stdout log": log_pointers.latest_stdout_log,
        "stderr log": log_pointers.latest_stderr_log,
    }
    return CommandReport(
        command="sync status",
        status=_status_from_issues(issues),
        summary="sync status is available for migration diagnostics",
        details=details,
        issues=_report_issues(issues),
    )


def cmd_sync_status(args: argparse.Namespace, paths: RuntimePaths) -> int:
    report = _sync_status_report(paths)
    print(render_report(report, as_json=args.json))
    return _exit_code_for_report(report)


def _sync_progress_report(paths: RuntimePaths) -> CommandReport:
    load_result = load_config(paths)
    issues = _sort_issues(list(load_result.issues))
    progress = parse_sync_progress(load_result.config)
    sync_state = read_sync_state(load_result.config)

    if progress is None:
        details: dict[str, object] = {
            "sync state": sync_state.state,
            "progress source": "-",
            "reason": "no sync log available for progress",
        }
        warning_issues = issues + [
            ConfigIssue(
                key="PCLOUD_TOOLS_SYNC_PROGRESS",
                level="warning" if not issues else "error" if _has_errors(issues) else "warning",
                message="no sync log available for progress",
            )
        ]
        return CommandReport(
            command="sync progress",
            status=_status_from_issues(_sort_issues(warning_issues)),
            summary="sync progress is not available yet",
            details=details,
            issues=_report_issues(_sort_issues(warning_issues)),
        )

    details = {
        "sync state": sync_state.state,
        "progress source": str(progress.log_path),
        "activity": progress.activity,
        "scanned entries": progress.scanned_entries,
        "compared entries": progress.compared_entries,
        "files transferred": progress.files_transferred,
        "bytes transferred": progress.bytes_transferred,
        "rate": progress.rate,
        "eta": progress.eta,
        "elapsed": progress.elapsed,
        "transferring block": list(progress.trailing_lines),
    }
    return CommandReport(
        command="sync progress",
        status=_status_from_issues(issues),
        summary="sync progress is available",
        details=details,
        issues=_report_issues(issues),
    )


def cmd_sync_progress(args: argparse.Namespace, paths: RuntimePaths) -> int:
    report = _sync_progress_report(paths)
    print(render_report(report, as_json=args.json))
    return _exit_code_for_report(report)


def _sync_execution_report(args: argparse.Namespace, paths: RuntimePaths, mode: str) -> CommandReport:
    load_result = load_config(paths)
    scope_info = sync_allowlist_info(load_result.config)
    issues = _sort_issues(
        list(load_result.issues)
        + scope_issues(scope_info)
        + list(enforce_sync_scope_guard(load_result.config, mode))
        )

    if issues and _has_errors(issues):
        return CommandReport(
            command=f"sync {mode}",
            status="error",
            summary="sync command cannot run until configuration issues are resolved",
            details={
                "mode": mode,
                "execute": "yes" if args.execute else "no",
            },
            issues=_report_issues(issues),
        )

    if args.execute and paths.dev_mode:
        return CommandReport(
            command=f"sync {mode}",
            status="error",
            summary="dev mode refuses to execute bisync against a configured remote",
            details={
                "mode": mode,
                "execute": "yes",
                "core remote": load_result.config.core_remote,
                "reason": "use preview in dev mode; execution requires the public entrypoint or an explicit non-dev runtime",
            },
            issues=_report_issues(
                [
                    ConfigIssue(
                        key="PCLOUD_TOOLS_DEV_EXECUTION",
                        level="error",
                        message="refusing --execute from pcloud-manager-dev",
                    )
                ]
            ),
        )

    try:
        rclone_bin = shutil.which("rclone")
        if not rclone_bin:
            raise SyncExecutionError("rclone command not found")
        plan = build_sync_plan(
            load_result.config,
            mode,
            scope_info.entries,
            rclone_bin=rclone_bin,
        )
    except SyncExecutionError as exc:
        return CommandReport(
            command=f"sync {mode}",
            status="error",
            summary="sync plan could not be built",
            details={"mode": mode},
            issues=_report_issues(
                [ConfigIssue(key="PCLOUD_TOOLS_SYNC_EXEC", level="error", message=str(exc))]
            ),
        )

    details: dict[str, object] = {
        "mode": mode,
        "scope mode": plan.scope_mode,
        "execute": "yes" if args.execute else "no",
        "command": list(plan.command),
        "rclone log": str(plan.rclone_log),
        "stdout log": str(plan.stdout_log),
        "stderr log": str(plan.stderr_log),
    }
    if plan.filter_file is not None:
        details["filter file"] = str(plan.filter_file)

    if not args.execute:
        return CommandReport(
            command=f"sync {mode}",
            status=_status_from_issues(issues),
            summary="sync command preview is ready",
            details=details,
            issues=_report_issues(issues),
        )

    result = execute_sync_plan(load_result.config, plan)
    details["exit code"] = result.exit_code
    details["scope recorded"] = "yes" if result.scope_recorded else "no"
    status = "ok" if result.exit_code == 0 else "error"
    return CommandReport(
        command=f"sync {mode}",
        status=status,
        summary="sync command executed" if result.exit_code == 0 else "sync command failed",
        details=details,
        issues=_report_issues(issues),
    )


def cmd_sync_execution(args: argparse.Namespace, paths: RuntimePaths, mode: str) -> int:
    report = _sync_execution_report(args, paths, mode)
    print(render_report(report, as_json=args.json))
    return _exit_code_for_report(report)


def _readable_baseline(info_file: Path, mode: str, status: str) -> str:
    if status == "defaulted":
        return f"{mode} (default)"
    if status == "invalid":
        return f"invalid ({info_file})"
    return mode


def _sync_scope_report(args: argparse.Namespace, paths: RuntimePaths) -> CommandReport:
    load_result = load_config(paths)
    config = load_result.config
    info = sync_allowlist_info(config)
    issues = _sort_issues(list(load_result.issues) + scope_issues(info))
    details: dict[str, object] = {
        "scope mode": "allowlist",
        "scope source": str(info.allowlist_file),
        "scope status": info.allowlist_status,
        "scope entries": info.allowlist_count if info.allowlist_status == "loaded" else 0,
        "full-tree override": "pcloud-manager sync full-resync",
        "last resync scope": _readable_baseline(
            info.baseline.file,
            info.baseline.mode,
            info.baseline.status,
        ),
        "filter file": str(info.filter_file),
        "entries": list(info.entries),
    }
    if info.allowlist_message != "-":
        details["scope note"] = info.allowlist_message
    if args.filter and info.allowlist_status == "loaded":
        written_filter = write_sync_filter_file(config, info.entries)
        details["filter file"] = str(written_filter)
        details["filter rules"] = list(prepare_sync_filter_rules(config, info.entries))

    return CommandReport(
        command="sync scope",
        status=_status_from_issues(issues),
        summary="allowlist scope is ready" if not issues else "allowlist scope needs attention",
        details=details,
        issues=_report_issues(issues),
    )


def cmd_sync_scope(args: argparse.Namespace, paths: RuntimePaths) -> int:
    report = _sync_scope_report(args, paths)
    print(render_report(report, as_json=args.json))
    return _exit_code_for_report(report)


def _sync_check_allowlist_report(args: argparse.Namespace, paths: RuntimePaths) -> CommandReport:
    load_result = load_config(paths)
    info = sync_allowlist_info(load_result.config)
    issues = _sort_issues(list(load_result.issues) + scope_issues(info))
    details: dict[str, object] = {
        "file": str(info.allowlist_file),
        "status": info.allowlist_status,
        "entries": info.allowlist_count,
    }
    if info.allowlist_message != "-":
        details["reason"] = info.allowlist_message

    summary = (
        f"allowlist loaded ({info.allowlist_count} entries)"
        if info.allowlist_status == "loaded" and not issues
        else f"allowlist {info.allowlist_status}"
    )
    return CommandReport(
        command="sync check-allowlist",
        status=_status_from_issues(issues),
        summary=summary,
        details=details,
        issues=_report_issues(issues),
    )


def cmd_sync_check_allowlist(args: argparse.Namespace, paths: RuntimePaths) -> int:
    report = _sync_check_allowlist_report(args, paths)
    print(render_report(report, as_json=args.json))
    return _exit_code_for_report(report)


def _mount_option_issues(args: argparse.Namespace, target: str) -> list[ConfigIssue]:
    issues: list[ConfigIssue] = []
    single_port = getattr(args, "port", None)
    single_engine = getattr(args, "engine", None)
    if target == "all" and single_port is not None:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_MOUNT_PORT",
                level="error",
                message="--port is only valid with target vault or crypt",
            )
        )
    if target == "all" and single_engine is not None:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_MOUNT_ENGINE",
                level="error",
                message="--engine is only valid with target vault or crypt",
            )
        )
    return issues


def _mount_details(prefix: str, spec, state, operations: list[str]) -> dict[str, object]:
    return {
        f"{prefix} state": state.state,
        f"{prefix} engine": state.engine,
        f"{prefix} port": state.port,
        f"{prefix} remote": state.remote,
        f"{prefix} mount dir": str(state.target),
        f"{prefix} link path": str(spec.link_dir),
        f"{prefix} link target": state.link_target,
        f"{prefix} link state": state.link_state,
        f"{prefix} pid": state.pid,
        f"{prefix} operations": operations,
    }


def _override_mount_spec(spec, args: argparse.Namespace, target: str):
    single_engine = getattr(args, "engine", None)
    single_port = getattr(args, "port", None)
    if target == spec.name:
        engine = single_engine or spec.engine
        port = single_port or spec.port
    else:
        engine = spec.engine
        port = spec.port
    if spec.name == "vault" and args.vault_port is not None:
        port = args.vault_port
    if spec.name == "crypt" and args.crypt_port is not None:
        port = args.crypt_port
    return type(spec)(
        name=spec.name,
        enabled=spec.enabled,
        remote=spec.remote,
        mount_dir=spec.mount_dir,
        link_dir=spec.link_dir,
        engine=engine,
        port=port,
    )


def _mount_report(args: argparse.Namespace, paths: RuntimePaths, action: str) -> CommandReport:
    load_result = load_config(paths)
    issues = _sort_issues(list(load_result.issues) + _mount_option_issues(args, args.target))
    try:
        specs = [
            _override_mount_spec(spec, args, args.target)
            for spec in resolve_layers(load_result.config, args.target)
        ]
    except MountCommandError as exc:
        issues.append(ConfigIssue(key="PCLOUD_TOOLS_MOUNT_TARGET", level="error", message=str(exc)))
        return CommandReport(
            command=action,
            status="error",
            summary=f"{action} target is invalid",
            details={"target": args.target},
            issues=_report_issues(_sort_issues(issues)),
        )

    if any(spec.engine not in {"webdav", "nfs"} for spec in specs):
        for spec in specs:
            if spec.engine not in {"webdav", "nfs"}:
                issues.append(
                    ConfigIssue(
                        key=f"PCLOUD_TOOLS_{spec.name.upper()}_ENGINE",
                        level="error",
                        message=f"unsupported engine: {spec.engine}",
                    )
                )

    if any(spec.port < 1 or spec.port > 65535 for spec in specs):
        for spec in specs:
            if spec.port < 1 or spec.port > 65535:
                issues.append(
                    ConfigIssue(
                        key=f"PCLOUD_TOOLS_{spec.name.upper()}_PORT",
                        level="error",
                        message=f"port out of range: {spec.port}",
                    )
                )

    issues = _sort_issues(issues)
    details: dict[str, object] = {
        "target": args.target,
        "execute": "yes" if args.execute else "no",
    }

    for spec in specs:
        state = mount_layer_state(spec)
        operations = (
            preview_mount_operations(spec, args.execute)
            if action == "mount"
            else preview_umount_operations(spec, args.execute)
        )
        details.update(_mount_details(spec.name, spec, state, operations))

    if issues and _has_errors(issues):
        return CommandReport(
            command=action,
            status="error",
            summary=f"{action} command cannot run until configuration issues are resolved",
            details=details,
            issues=_report_issues(issues),
        )

    if not args.execute:
        return CommandReport(
            command=action,
            status="ok",
            summary=f"{action} preview is ready",
            details=details,
            issues=_report_issues(issues),
        )

    try:
        if action == "mount":
            rclone_bin = shutil.which("rclone")
            if not rclone_bin:
                raise MountExecutionError("rclone command not found")
            for spec in specs:
                execute_mount(spec, rclone_bin)
        else:
            for spec in specs:
                execute_umount(spec)
    except MountExecutionError as exc:
        issues = _sort_issues(
            issues + [ConfigIssue(key="PCLOUD_TOOLS_MOUNT_EXEC", level="error", message=str(exc))]
        )
        return CommandReport(
            command=action,
            status="error",
            summary=f"{action} command failed",
            details=details,
            issues=_report_issues(issues),
        )

    refreshed_details: dict[str, object] = {
        "target": args.target,
        "execute": "yes",
    }
    for spec in specs:
        state = mount_layer_state(spec)
        refreshed_details.update(_mount_details(spec.name, spec, state, []))

    return CommandReport(
        command=action,
        status="ok",
        summary=f"{action} command executed",
        details=refreshed_details,
        issues=_report_issues(issues),
    )


def cmd_mount(args: argparse.Namespace, paths: RuntimePaths) -> int:
    report = _mount_report(args, paths, "mount")
    print(render_report(report, as_json=args.json))
    return _exit_code_for_report(report)


def cmd_umount(args: argparse.Namespace, paths: RuntimePaths) -> int:
    report = _mount_report(args, paths, "umount")
    print(render_report(report, as_json=args.json))
    return _exit_code_for_report(report)


def cmd_index(args: argparse.Namespace, paths: RuntimePaths) -> int:
    load_result = load_config(paths)
    issues = _sort_issues(list(load_result.issues))
    configured_indexer = load_result.config.indexer_bin
    legacy_indexer = Path.home() / ".zsh/functions/pcloud-indexer.py"
    indexer = configured_indexer if configured_indexer.exists() else legacy_indexer
    details: dict[str, object] = {
        "indexer": str(indexer),
        "configured indexer": str(configured_indexer),
        "args": list(args.index_args),
    }
    if indexer != configured_indexer:
        details["fallback"] = "using legacy indexer path because configured script is missing"
    if not indexer.exists():
        issues = _sort_issues(
            issues
            + [
                ConfigIssue(
                    key="PCLOUD_TOOLS_INDEXER_BIN",
                    level="error",
                    message=f"indexer not found: {configured_indexer}",
                )
            ]
        )
        report = CommandReport(
            command="index",
            status="error",
            summary="indexer script is missing",
            details=details,
            issues=_report_issues(issues),
        )
        print(render_report(report))
        return 1

    command = [sys.executable, str(indexer), *args.index_args]
    result = subprocess.run(command, check=False)
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    paths = detect_runtime_paths()

    if args.command == "status":
        return cmd_status(args, paths)
    if args.command == "doctor":
        return cmd_doctor(args, paths)
    if args.command == "sync":
        if args.sync_command == "status":
            return cmd_sync_status(args, paths)
        if args.sync_command == "progress":
            return cmd_sync_progress(args, paths)
        if args.sync_command in {"resync", "full-resync", "track-renames"}:
            return cmd_sync_execution(args, paths, args.sync_command)
        if args.sync_command == "scope":
            return cmd_sync_scope(args, paths)
        if args.sync_command == "check-allowlist":
            return cmd_sync_check_allowlist(args, paths)
        parser.print_help()
        return 1
    if args.command == "mount":
        return cmd_mount(args, paths)
    if args.command == "umount":
        return cmd_umount(args, paths)
    if args.command == "index":
        return cmd_index(args, paths)

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
