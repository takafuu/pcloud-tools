from __future__ import annotations

import argparse
import sys
from importlib import metadata
from pathlib import Path

from .autosync_runtime import read_autosync_state
from .config import (
    ConfigIssue,
    load_config,
    repair_allowlist_file,
    repair_env_file,
    repair_manager_ignore_file,
)
from .mount_ops import mount_layer_state, resolve_layers
from .output import CommandReport, ReportAction, ReportIssue, render_report
from .runtime import RuntimePaths, action_entrypoint_command
from .sync_exec import bisync_listing_recovery_state
from .sync_runtime import (
    parse_sync_progress,
    read_latest_sync_logs,
    read_sync_lock_state,
    read_sync_state,
    sync_last_error_status,
)
from .sync_scope import scope_issues, sync_allowlist_info, sync_filter_file


def add_status_doctor_parsers(subparsers: argparse._SubParsersAction) -> None:
    info_parser = subparsers.add_parser("info", help="Show installed/runtime paths and scope.")
    info_parser.add_argument(
        "info_command",
        nargs="?",
        choices=("overview", "paths", "config"),
        default="overview",
        help="Info view to show.",
    )
    info_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit structured JSON output.",
    )

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
    status_parser.add_argument(
        "--xbar",
        action="store_true",
        help="Emit xbar menu output.",
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


def _config_summary(paths: RuntimePaths) -> dict[str, str]:
    load_result = load_config(paths)
    config = load_result.config
    return {
        "config source": load_result.source,
        "core dir": str(config.core_dir),
        "state dir": str(config.state_dir),
        "log dir": str(config.log_dir),
        "allowlist": str(config.allowlist_file),
        "manager ignore": str(config.manager_ignore_file),
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


def _output_format(args: argparse.Namespace) -> str:
    if getattr(args, "xbar", False):
        return "xbar"
    return "json" if getattr(args, "json", False) else "human"


def _print_report(report: CommandReport, args: argparse.Namespace) -> None:
    print(render_report(report, output_format=_output_format(args)))


def _entrypoint_command(paths: RuntimePaths) -> str:
    return action_entrypoint_command(paths)


def _action_command(paths: RuntimePaths, action_id: str) -> tuple[str, ...]:
    return (_entrypoint_command(paths), "action", action_id)


def _status_actions(paths: RuntimePaths) -> list[ReportAction]:
    return [
        ReportAction(id="status.refresh", label="Refresh status", command=_action_command(paths, "status.refresh")),
        ReportAction(
            id="status.detail",
            label="Open detailed status",
            command=_action_command(paths, "status.detail"),
            terminal=True,
            refresh=False,
        ),
        ReportAction(
            id="sync.status.refresh",
            label="Sync status",
            command=_action_command(paths, "sync.status.refresh"),
        ),
        ReportAction(
            id="doctor",
            label="Run doctor",
            command=_action_command(paths, "doctor"),
            terminal=True,
            refresh=False,
        ),
        ReportAction(
            id="daemon.status.refresh",
            label="Daemon state",
            command=_action_command(paths, "daemon.status.refresh"),
        ),
        ReportAction(
            id="notify.chat.status",
            label="Discord notify",
            command=_action_command(paths, "notify.chat.status"),
        ),
    ]


def _package_version() -> str:
    try:
        return metadata.version("pcloud-tools")
    except metadata.PackageNotFoundError:
        return "unknown"


def _path_entry(path: Path | str, purpose: str) -> str:
    return f"{purpose}: {path}"


def _redacted_state(value: str) -> str:
    return "set (redacted)" if value else "unset"


def _info_actions(paths: RuntimePaths) -> list[ReportAction]:
    return [
        ReportAction(
            id="info.paths",
            label="Show paths",
            command=(_entrypoint_command(paths), "info", "paths"),
            terminal=True,
            refresh=False,
        ),
        ReportAction(
            id="status.detail",
            label="Open detailed status",
            command=_action_command(paths, "status.detail"),
            terminal=True,
            refresh=False,
        ),
        ReportAction(
            id="doctor",
            label="Run doctor",
            command=_action_command(paths, "doctor"),
            terminal=True,
            refresh=False,
        ),
    ]


def _info_report(args: argparse.Namespace, paths: RuntimePaths) -> CommandReport:
    load_result = load_config(paths)
    config = load_result.config
    scope_info = sync_allowlist_info(config)
    autosync = read_autosync_state(config)
    issues = _sort_issues(list(load_result.issues) + scope_issues(scope_info))
    command = f"info {args.info_command}" if args.info_command != "overview" else "info"
    mode = "dev" if paths.dev_mode else "default"

    if args.info_command == "paths":
        details = {
            "paths": [
                _path_entry(_entrypoint_command(paths), "entrypoint"),
                _path_entry(Path(__file__).resolve().parents[1], "implementation package"),
                _path_entry(paths.workspace_root, "workspace root"),
                _path_entry(paths.config_dir, "config directory"),
                _path_entry(paths.env_file, "env file"),
                _path_entry(config.core_dir, "local sync root"),
                _path_entry(config.allowlist_file, "allowlist file"),
                _path_entry(config.manager_ignore_file, "manager ignore file"),
                _path_entry(config.state_dir, "runtime state directory"),
                _path_entry(config.log_dir, "runtime log directory"),
                _path_entry(sync_filter_file(config), "generated rclone filter file"),
                _path_entry(config.autosync_plist, "autosync LaunchAgent plist"),
                _path_entry(config.vault_mount_dir, "vault mount directory"),
                _path_entry(config.crypt_mount_dir, "crypt mount directory"),
            ],
            "state policy": "runtime state stays local under the configured state directory",
            "content policy": "document/media allowlist only; source/tool roots are out of scope",
        }
    elif args.info_command == "config":
        details = {
            "config source": load_result.source,
            "env file": str(paths.env_file),
            "core dir": str(config.core_dir),
            "core remote": config.core_remote,
            "remote": config.remote,
            "vault remote": config.vault_remote,
            "crypt remote": config.crypt_remote,
            "allowlist file": str(config.allowlist_file),
            "allowlist status": scope_info.allowlist_status,
            "allowlist entries": list(scope_info.entries),
            "manager ignore file": str(config.manager_ignore_file),
            "default excludes": list(config.default_excludes),
            "state dir": str(config.state_dir),
            "log dir": str(config.log_dir),
            "rclone bin": config.rclone_bin,
            "autosync label": config.autosync_label,
            "autosync plist": str(config.autosync_plist),
            "pushd debounce seconds": config.pushd_debounce_seconds,
            "pushd queue limit": config.pushd_queue_limit,
            "diffd poll interval seconds": config.diffd_poll_interval_seconds,
            "diffd batch limit": config.diffd_batch_limit,
            "transfer exec timeout seconds": config.transfer_exec_timeout_seconds,
            "download suppression ttl seconds": config.download_suppression_ttl_seconds,
            "pCloud API base URL": config.pcloud_api_base_url,
            "pCloud API auth parameter": config.pcloud_api_auth_param,
            "pCloud API token": _redacted_state(config.pcloud_api_token),
            "chat notify enabled": "yes" if config.chat_notify_enabled else "no",
            "chat notify command": config.chat_notify_cmd,
            "gate env values": "redacted from info; use gates/status commands for gate state",
        }
    else:
        details = {
            "version": _package_version(),
            "mode": mode,
            "python": sys.executable,
            "entrypoint": _entrypoint_command(paths),
            "implementation package": str(Path(__file__).resolve().parents[1]),
            "workspace": str(paths.workspace_root),
            "config source": load_result.source,
            "config dir": str(paths.config_dir),
            "env file": str(paths.env_file),
            "state dir": str(config.state_dir),
            "log dir": str(config.log_dir),
            "core dir": str(config.core_dir),
            "core remote": config.core_remote,
            "allowlist": str(config.allowlist_file),
            "allowlist status": scope_info.allowlist_status,
            "allowlist entries": scope_info.allowlist_count,
            "manager ignore": str(config.manager_ignore_file),
            "filter file": str(sync_filter_file(config)),
            "autosync state": autosync.state,
            "autosync label": autosync.label,
            "autosync plist": autosync.plist,
            "pushd state dir": str(config.state_dir / "pushd"),
            "diffd state dir": str(config.state_dir / "diffd"),
            "daemon state dir": str(config.state_dir / "daemon"),
            "log policy": "logs stay local; reports redact pCloud API tokens",
            "sensitive data policy": "secrets are read on demand and redacted in info output",
            "content policy": "document/media allowlist only; normal sync/resync remains separately gated",
        }

    return CommandReport(
        command=command,
        status=_status_from_issues(issues),
        summary=f"pcloud-manager runtime info ({mode})",
        details=details,
        issues=_report_issues(issues),
        actions=_info_actions(paths),
    )


def _exit_code_for_report(report: CommandReport) -> int:
    return 1 if report.status == "error" else 0


def _issue_sort_key(issue: ConfigIssue) -> tuple[int, str]:
    priority = 0 if issue.level == "error" else 1
    return (priority, issue.key)


def _sort_issues(issues: list[ConfigIssue]) -> list[ConfigIssue]:
    return sorted(issues, key=_issue_sort_key)


def _sync_state_issues(sync_state) -> list[ConfigIssue]:
    if sync_state.state != "sync_error":
        return []
    message = sync_state.reason if sync_state.reason else sync_state.last_sync
    return [
        ConfigIssue(
            key="PCLOUD_TOOLS_SYNC_STATE",
            level="warning",
            message=f"last sync failed: {message}",
        )
    ]


def _mount_state_issues(layer_states: dict[str, object]) -> list[ConfigIssue]:
    issues: list[ConfigIssue] = []
    for name, state in layer_states.items():
        if not state.enabled or state.state != "error":
            continue
        issues.append(
            ConfigIssue(
                key=f"PCLOUD_TOOLS_{name.upper()}_MOUNT",
                level="warning",
                message=f"{name} mount serve process is running but mount is not active (pid={state.pid})",
            )
        )
    return issues


def _core_status_label(sync_state) -> str:
    if sync_state.state == "syncing":
        if sync_state.activity != "-":
            return f"syncing ({sync_state.activity})"
        return "syncing"
    if sync_state.state == "sync_error":
        return "sync_error"
    return sync_state.state


def _mount_status_label(state) -> str:
    if not state.enabled:
        return "disabled"
    if state.state == "mounted":
        return f"mounted ({state.engine}:{state.port})"
    if state.state == "error":
        return f"error (serve pid={state.pid}, not mounted)"
    return state.state


def _doctor_summary(sync_state, layer_states: dict[str, object]) -> str:
    if sync_state.state == "syncing":
        return "syncing"
    if sync_state.state == "sync_error":
        return "sync error"
    if any(state.state == "error" for state in layer_states.values()):
        return "mount warning"
    return "ok"


def _doctor_mount_suspected_cause(layer_states: dict[str, object]) -> str:
    for name, state in layer_states.items():
        if state.enabled and state.state == "error":
            return f"{name} mount serve running but not mounted"
    return "-"


def _doctor_suspected_cause(
    scope_info,
    lock_state,
    listing_recovery,
    layer_states: dict[str, object],
    sync_state,
    progress,
) -> str:
    if scope_info.allowlist_status != "loaded":
        return f"allowlist {scope_info.allowlist_status}"
    if lock_state.status in {"stale", "invalid"}:
        return f"sync lock is {lock_state.status}"
    if listing_recovery.can_recover:
        return "missing bisync listings; only -err files present"
    if sync_state.state == "sync_error" and sync_state.reason not in {"", "(none)"}:
        return "unclear; see last error"
    mount_cause = _doctor_mount_suspected_cause(layer_states)
    if mount_cause != "-":
        return mount_cause
    if progress is None:
        return "current log unavailable"
    return "-"


def _readable_baseline(info_file: Path, mode: str, status: str) -> str:
    if status == "defaulted":
        return f"{mode} (default)"
    if status == "invalid":
        return f"invalid ({info_file})"
    return mode


def _status_report(args: argparse.Namespace, paths: RuntimePaths) -> CommandReport:
    load_result = load_config(paths)
    config = load_result.config
    mode = "dev" if paths.dev_mode else "default"
    sync_state = read_sync_state(config)
    lock_state = read_sync_lock_state(config)
    log_pointers = read_latest_sync_logs(config)
    scope_info = sync_allowlist_info(config)
    autosync = read_autosync_state(config)
    layer_states = {
        spec.name: mount_layer_state(spec)
        for spec in resolve_layers(config, "all")
    }
    issues = _sort_issues(
        list(load_result.issues)
        + _sync_state_issues(sync_state)
        + scope_issues(scope_info)
        + _mount_state_issues(layer_states)
    )
    details = {
        "core": _core_status_label(sync_state),
        "vault": _mount_status_label(layer_states["vault"]),
        "crypt": _mount_status_label(layer_states["crypt"]),
    }
    if args.detail:
        details.update(
            {
                "workspace": str(paths.workspace_root),
                "config dir": str(paths.config_dir),
                "state dir": str(paths.state_dir),
                "log dir": str(paths.log_dir),
                "env file": str(paths.env_file),
                **_config_summary(paths),
                "sync state": sync_state.state,
                "sync activity": sync_state.activity,
                "current sync log": sync_state.current_log,
                "last sync result": sync_state.last_sync,
                "last sync error": sync_state.last_error,
                "last sync error status": sync_last_error_status(sync_state),
                "scope status": scope_info.allowlist_status,
                "scope entries": scope_info.allowlist_count,
                "scope baseline": _readable_baseline(
                    scope_info.baseline.file,
                    scope_info.baseline.mode,
                    scope_info.baseline.status,
                ),
                "filter file": str(sync_filter_file(config)),
                "sync lock active": "yes" if lock_state.active else "no",
                "sync lock status": lock_state.status,
                "sync lock pid": lock_state.pid,
                "sync lock mode": lock_state.mode,
                "sync lock started": lock_state.started_at,
                "latest rclone log": log_pointers.latest_rclone_log,
                "latest stdout log": log_pointers.latest_stdout_log,
                "latest stderr log": log_pointers.latest_stderr_log,
                "autosync state": autosync.state,
                "autosync runs": autosync.runs,
                "autosync label": autosync.label,
                "autosync plist": autosync.plist,
            }
        )
    return CommandReport(
        command="status",
        status=_status_from_issues(issues),
        summary=(
            f"core: {_core_status_label(sync_state)}; "
            f"vault: {_mount_status_label(layer_states['vault'])}; "
            f"crypt: {_mount_status_label(layer_states['crypt'])} ({mode})"
        ),
        details=details,
        issues=_report_issues(issues),
        actions=_status_actions(paths),
    )


def cmd_status(args: argparse.Namespace, paths: RuntimePaths) -> int:
    report = _status_report(args, paths)
    _print_report(report, args)
    return _exit_code_for_report(report)


def cmd_info(args: argparse.Namespace, paths: RuntimePaths) -> int:
    report = _info_report(args, paths)
    print(render_report(report, as_json=args.json))
    return _exit_code_for_report(report)


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
        ignore_missing = not load_result.config.manager_ignore_file.exists()
        repair_manager_ignore_file(load_result.config)
        if ignore_missing and load_result.config.manager_ignore_file.exists():
            repaired_items.append(f"manager ignore file: {load_result.config.manager_ignore_file}")
        load_result = load_config(paths)

    config = load_result.config
    sync_state = read_sync_state(config)
    lock_state = read_sync_lock_state(config)
    log_pointers = read_latest_sync_logs(config)
    scope_info = sync_allowlist_info(config)
    progress = parse_sync_progress(config)
    autosync = read_autosync_state(config)
    listing_recovery = bisync_listing_recovery_state(config)
    layer_states = {
        spec.name: mount_layer_state(spec)
        for spec in resolve_layers(config, "all")
    }
    issues = _sort_issues(
        list(load_result.issues)
        + _sync_state_issues(sync_state)
        + scope_issues(scope_info)
        + _mount_state_issues(layer_states)
    )
    has_errors = _has_errors(issues)
    status = _status_from_issues(issues)
    doctor_summary = _doctor_summary(sync_state, layer_states)
    suspected_cause = _doctor_suspected_cause(
        scope_info,
        lock_state,
        listing_recovery,
        layer_states,
        sync_state,
        progress,
    )
    details = {
        "summary": doctor_summary,
        "suspected cause": suspected_cause,
        "config dir": f"{'present' if paths.config_dir.exists() else 'missing'} ({paths.config_dir})",
        "state dir": f"{'present' if paths.state_dir.exists() else 'missing'} ({paths.state_dir})",
        "log dir": f"{'present' if paths.log_dir.exists() else 'missing'} ({paths.log_dir})",
        "env file": f"{'present' if paths.env_file.exists() else 'missing'} ({paths.env_file})",
        "config source": load_result.source,
        "core dir": str(config.core_dir),
        "allowlist": str(config.allowlist_file),
        "manager ignore": str(config.manager_ignore_file),
        "core remote": config.core_remote,
        "vault layer": "enabled" if config.enable_vault_layer else "disabled",
        "crypt layer": "enabled" if config.enable_crypt_layer else "disabled",
        "vault state": _mount_status_label(layer_states["vault"]),
        "crypt state": _mount_status_label(layer_states["crypt"]),
        "sync state": sync_state.state,
        "sync activity": sync_state.activity,
        "scope status": scope_info.allowlist_status,
        "scope entries": scope_info.allowlist_count,
        "scope baseline": _readable_baseline(
            scope_info.baseline.file,
            scope_info.baseline.mode,
            scope_info.baseline.status,
        ),
        "filter file": str(sync_filter_file(config)),
        "sync lock active": "yes" if lock_state.active else "no",
        "sync lock status": lock_state.status,
        "sync lock pid": lock_state.pid,
        "sync lock mode": lock_state.mode,
        "sync lock started": lock_state.started_at,
        "latest rclone log": log_pointers.latest_rclone_log,
        "latest stdout log": log_pointers.latest_stdout_log,
        "latest stderr log": log_pointers.latest_stderr_log,
        "progress source": str(progress.log_path) if progress is not None else "-",
        "path1 list": str(listing_recovery.path1_lst),
        "path2 list": str(listing_recovery.path2_lst),
        "path1 err": str(listing_recovery.path1_err),
        "path2 err": str(listing_recovery.path2_err),
        "listing recovery available": "yes" if listing_recovery.can_recover else "no",
        "autosync state": autosync.state,
        "autosync runs": autosync.runs,
        "autosync label": autosync.label,
        "autosync plist": autosync.plist,
    }
    if repaired_items:
        details["repair"] = "; ".join(f"created {item}" for item in repaired_items)

    report = CommandReport(
        command="doctor",
        status=status,
        summary=doctor_summary if suspected_cause == "-" else f"{doctor_summary}; suspected cause: {suspected_cause}",
        details=details,
        issues=_report_issues(issues),
    )
    return report, has_errors


def cmd_doctor(args: argparse.Namespace, paths: RuntimePaths) -> int:
    paths.ensure_directories()
    report, has_errors = _doctor_report(args, paths)
    print(render_report(report, as_json=args.json))
    return 1 if has_errors else 0
