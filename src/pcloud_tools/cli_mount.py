from __future__ import annotations

import argparse
import shlex
import subprocess

from .config import ConfigIssue, load_config
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
from .runtime import RuntimePaths


def add_mount_parsers(subparsers: argparse._SubParsersAction) -> None:
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


def _has_errors(issues: list[ConfigIssue]) -> bool:
    return any(issue.level == "error" for issue in issues)


def _report_issues(issues: list[ConfigIssue]) -> list[ReportIssue]:
    return [ReportIssue(level=issue.level, key=issue.key, message=issue.message) for issue in issues]


def _command_path(name: str) -> str | None:
    try:
        result = subprocess.run(
            ["/bin/sh", "-c", f"command -v {shlex.quote(name)}"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    path = result.stdout.strip().splitlines()
    return path[0] if path else None


def _exit_code_for_report(report: CommandReport) -> int:
    return 1 if report.status == "error" else 0


def _issue_sort_key(issue: ConfigIssue) -> tuple[int, str]:
    priority = 0 if issue.level == "error" else 1
    return (priority, issue.key)


def _sort_issues(issues: list[ConfigIssue]) -> list[ConfigIssue]:
    return sorted(issues, key=_issue_sort_key)


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

    if paths.dev_mode:
        issues = _sort_issues(
            issues
            + [
                ConfigIssue(
                    key="PCLOUD_TOOLS_DEV_MOUNT_EXECUTION",
                    level="error",
                    message=f"refusing --execute for `{action}` from pcloud-manager-dev",
                )
            ]
        )
        details["reason"] = "use preview in dev mode; execution requires the public entrypoint or an explicit non-dev runtime"
        return CommandReport(
            command=action,
            status="error",
            summary=f"dev mode refuses to execute {action}",
            details=details,
            issues=_report_issues(issues),
        )

    try:
        if action == "mount":
            rclone_bin = _command_path("rclone")
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
