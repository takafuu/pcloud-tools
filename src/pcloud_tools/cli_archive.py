from __future__ import annotations

import argparse
import shutil
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .cli_common import (
    action_command as _action_command,
    entrypoint_command as _entrypoint_command,
    has_errors as _has_errors,
    report_issues as _report_issues,
    sort_issues as _sort_issues,
    status_from_issues as _status_from_issues,
)
from .config import ConfigIssue
from .io_utils import atomic_write_json
from .output import CommandReport, ReportAction, render_report
from .runtime import RuntimePaths

_OLD_MONOLITH_ARCHIVE_GATE_VALUE = "operator-approved-old-monolith-archive-v1"


def add_archive_parser(subparsers: argparse._SubParsersAction) -> None:
    archive_parser = subparsers.add_parser("archive", help="Read-only archive readiness checks.")
    archive_subparsers = archive_parser.add_subparsers(dest="archive_command")
    old_monolith_parser = archive_subparsers.add_parser(
        "old-monolith-gate", help="Read-only checklist before archiving the old pcloud-manager monolith."
    )
    old_monolith_parser.add_argument("--backup-dir", type=Path)
    old_monolith_parser.add_argument("--operator-reviewed-current-wrapper", action="store_true")
    old_monolith_parser.add_argument("--reviewer-approved-backup-source", action="store_true")
    old_monolith_parser.add_argument("--reviewer-approved-rollback-policy", action="store_true")
    old_monolith_parser.add_argument("--reviewer-approved-archive-target", action="store_true")
    old_monolith_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    old_monolith_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
    old_monolith_run_parser = archive_subparsers.add_parser(
        "old-monolith-run", help="Run guarded old pcloud-manager monolith archival after the dedicated gate opens."
    )
    old_monolith_run_parser.add_argument("--backup-dir", type=Path)
    old_monolith_run_parser.add_argument("--operator-reviewed-current-wrapper", action="store_true")
    old_monolith_run_parser.add_argument("--reviewer-approved-backup-source", action="store_true")
    old_monolith_run_parser.add_argument("--reviewer-approved-rollback-policy", action="store_true")
    old_monolith_run_parser.add_argument("--reviewer-approved-archive-target", action="store_true")
    old_monolith_run_parser.add_argument("--execute", action="store_true")
    old_monolith_run_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    old_monolith_run_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")


def cmd_archive(args: argparse.Namespace, paths: RuntimePaths) -> int | None:
    if args.archive_command == "old-monolith-gate":
        return cmd_archive_old_monolith_gate(args, paths)
    if args.archive_command == "old-monolith-run":
        return cmd_archive_old_monolith_run(args, paths)
    return None


def _command_path(command: str) -> str | None:
    try:
        result = subprocess.run(
            ["/bin/sh", "-c", f"command -v {shlex.quote(command)}"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode == 0:
        value = result.stdout.strip().splitlines()
        if value:
            return value[0]
    return None


def _latest_cutover_backup(paths: RuntimePaths) -> Path | None:
    backups_dir = paths.workspace_root / ".dev-state" / "cutover-backups"
    if not backups_dir.exists():
        return None
    candidates = [path for path in backups_dir.iterdir() if path.is_dir()]
    if not candidates:
        return None
    return sorted(candidates)[-1]


def _file_contains(path: Path, text: str) -> bool:
    try:
        return text in path.read_text(errors="replace")
    except OSError:
        return False


def _check(name: str, ok: bool, detail: str) -> dict[str, str]:
    return {"name": name, "status": "ok" if ok else "pending", "detail": detail}


def _render_old_monolith_gate_human(report: CommandReport) -> str:
    details = report.details
    lines = [
        f"{report.command}: {report.status}",
        report.summary,
        f"archive gate: {details.get('archive gate status', '-')}",
        f"archive can run: {details.get('archive can run', '-')}",
        f"state writes: {details.get('state writes', '-')}",
        f"current wrapper: {details.get('current public wrapper', '-')}",
        f"dotfiles wrapper: {details.get('dotfiles wrapper', '-')}",
        f"legacy backup: {details.get('legacy backup file', '-')}",
        f"legacy backup status: {details.get('legacy backup status', '-')}",
        f"archive target preview: {details.get('archive target preview', '-')}",
        f"approval status: {details.get('archive approval status', '-')}",
        f"human gate: {details.get('human gate status', '-')}",
        f"next human check trigger: {details.get('next human check trigger', '-')}",
    ]
    commands = details.get("review commands")
    if isinstance(commands, list) and commands:
        lines.append("review commands:")
        for command in commands:
            lines.append(f"- {' '.join(str(part) for part in command)}")
    checks = details.get("preflight checks")
    if isinstance(checks, list) and checks:
        lines.append("preflight checks:")
        for check in checks:
            if isinstance(check, dict):
                lines.append(f"- {check.get('name', '-')}: {check.get('status', '-')} - {check.get('detail', '-')}")
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


def _render_old_monolith_run_human(report: CommandReport) -> str:
    details = report.details
    lines = [
        f"{report.command}: {report.status}",
        report.summary,
        f"archive run gate: {details.get('archive run gate status', '-')}",
        f"archive can run: {details.get('archive can run', '-')}",
        f"execute requested: {details.get('execute requested', '-')}",
        f"state writes: {details.get('state writes', '-')}",
        f"legacy backup: {details.get('legacy backup file', '-')}",
        f"legacy backup status: {details.get('legacy backup status', '-')}",
        f"archive target: {details.get('archive target', '-')}",
        f"approval status: {details.get('archive approval status', '-')}",
    ]
    manifest = details.get("archive manifest")
    if manifest:
        lines.append(f"archive manifest: {manifest}")
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


def _old_monolith_gate_report(args: argparse.Namespace, paths: RuntimePaths) -> CommandReport:
    issues: list[ConfigIssue] = []
    current_wrapper = Path("/Users/takafumi/p-core/dotfiles/.zsh/functions/pcloud-manager")
    home_wrapper = Path("/Users/takafumi/.zsh/functions/pcloud-manager")
    command_wrapper = _command_path("pcloud-manager")
    backup_dir = getattr(args, "backup_dir", None) or _latest_cutover_backup(paths)
    legacy_backup = backup_dir / "pcloud-manager.current" if backup_dir else None
    shadow_backup = backup_dir / "shadow-validation.json" if backup_dir else None
    archive_target = paths.workspace_root / ".dev-state" / "old-monolith-archive" / (
        backup_dir.name if backup_dir else "YYYYMMDD-HHMMSS"
    )
    current_is_python = current_wrapper.exists() and _file_contains(current_wrapper, "pcloud_tools.cli")
    legacy_is_monolith = bool(legacy_backup and legacy_backup.exists() and _file_contains(legacy_backup, "PCLOUD_MANAGER_CONFIG"))
    checks = [
        _check(
            "current public wrapper",
            bool(command_wrapper) and current_wrapper.exists() and current_is_python,
            f"command-v={command_wrapper or '-'}; wrapper={current_wrapper}; python-wrapper={'yes' if current_is_python else 'no'}",
        ),
        _check(
            "dotfiles wrapper match",
            home_wrapper.exists() and current_wrapper.exists() and home_wrapper.resolve() == current_wrapper.resolve(),
            f"{home_wrapper} -> {home_wrapper.resolve() if home_wrapper.exists() else '-'}; dotfiles={current_wrapper}",
        ),
        _check(
            "cutover backup directory",
            bool(backup_dir and backup_dir.exists()),
            str(backup_dir or "-"),
        ),
        _check(
            "legacy monolith backup",
            legacy_is_monolith,
            str(legacy_backup or "-"),
        ),
        _check(
            "shadow validation backup",
            bool(shadow_backup and shadow_backup.exists()),
            str(shadow_backup or "-"),
        ),
        _check(
            "operator current-wrapper review",
            getattr(args, "operator_reviewed_current_wrapper", False),
            "operator reviewed command -v and current wrapper target",
        ),
        _check(
            "backup source approval",
            getattr(args, "reviewer_approved_backup_source", False),
            "reviewer approved the selected legacy monolith backup as rollback/archive source",
        ),
        _check(
            "rollback policy approval",
            getattr(args, "reviewer_approved_rollback_policy", False),
            "reviewer approved restoring pcloud-manager.current from backup if archive/cutover assumptions fail",
        ),
        _check(
            "archive target approval",
            getattr(args, "reviewer_approved_archive_target", False),
            "reviewer approved archive target and retention policy before moving any files",
        ),
        _check(
            "parallel dangerous gates",
            True,
            "launchd changes, fswatch resident start, pCloud API long-poll, and sync/resync stay out of scope",
        ),
    ]
    if not command_wrapper:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_ARCHIVE_PUBLIC_WRAPPER",
                level="warning",
                message="pcloud-manager was not found with command -v",
            )
        )
    if not legacy_is_monolith:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_ARCHIVE_LEGACY_BACKUP",
                level="warning",
                message="legacy monolith backup is missing or does not look like the old zsh implementation",
            )
        )
    issues.append(
        ConfigIssue(
            key="PCLOUD_TOOLS_OLD_MONOLITH_ARCHIVE_GATE",
            level="warning",
            message="old monolith archive remains gated; this command is read-only",
        )
    )
    approval_status = "complete-read-only" if all(check["status"] == "ok" for check in checks) else "pending"
    review_commands: list[list[str]] = [
        ["command", "-v", "pcloud-manager"],
        ["ls", "-l", "/Users/takafumi/p-core/bin/pcloud-manager", str(home_wrapper), str(current_wrapper)],
    ]
    if legacy_backup:
        review_commands.append(["sed", "-n", "1,80p", str(legacy_backup)])
    if shadow_backup:
        review_commands.append(["python3", "-m", "json.tool", str(shadow_backup)])
    details: dict[str, object] = {
        "planned action": "check old pcloud-manager monolith archive prerequisites",
        "implementation status": "read-only checklist; old monolith files are not moved or deleted",
        "archive gate status": "closed",
        "archive can run": "no",
        "state writes": "none",
        "current public wrapper": command_wrapper or "-",
        "dotfiles wrapper": str(current_wrapper),
        "home wrapper": str(home_wrapper),
        "backup dir": str(backup_dir or "-"),
        "legacy backup file": str(legacy_backup or "-"),
        "legacy backup status": "monolith-backup" if legacy_is_monolith else "missing-or-unrecognized",
        "shadow validation backup": str(shadow_backup or "-"),
        "archive target preview": str(archive_target),
        "archive approval status": approval_status,
        "human gate status": "required-before-old-monolith-archive",
        "human gate reason": "archive would move or retire the only rollback copy of the old zsh implementation",
        "next human check trigger": "explicit old monolith archive command or rollback-source retention decision",
        "review commands": review_commands,
        "preflight checks": checks,
        "success policy": "archive only after the selected backup source and rollback policy are explicitly approved",
        "failure policy": "retain all current and backup files for manual review",
        "rollback policy": "restore pcloud-manager.current from the selected cutover backup; do not touch remotes or sync state",
        "blocked operations": [
            "moving old monolith files",
            "deleting old monolith files",
            "modifying public pcloud-manager wrapper",
            "launchd changes",
            "normal sync/resync execution",
        ],
    }
    return CommandReport(
        command="archive old-monolith-gate",
        status=_status_from_issues(_sort_issues(issues)),
        summary="old monolith archive gate is closed",
        details=details,
        issues=_report_issues(_sort_issues(issues)),
        actions=[
            ReportAction(
                id="archive.old-monolith.gate",
                label="Check old monolith archive gate",
                command=_action_command(paths, "archive.old-monolith.gate"),
                terminal=True,
                refresh=False,
            ),
            ReportAction(
                id="archive.old-monolith-run.preview",
                label="Preview old monolith archive run",
                command=_action_command(paths, "archive.old-monolith-run.preview"),
                terminal=True,
                refresh=False,
            )
        ],
    )


def cmd_archive_old_monolith_gate(args: argparse.Namespace, paths: RuntimePaths) -> int:
    report = _old_monolith_gate_report(args, paths)
    output_format = "xbar" if getattr(args, "xbar", False) else "json" if getattr(args, "json", False) else "human"
    if output_format == "human":
        print(_render_old_monolith_gate_human(report))
    else:
        print(render_report(report, output_format=output_format))
    return 1 if report.status == "error" else 0


def _old_monolith_archive_gate_open() -> bool:
    import os

    return os.environ.get("PCLOUD_TOOLS_OLD_MONOLITH_ARCHIVE_GATE", "") == _OLD_MONOLITH_ARCHIVE_GATE_VALUE


def _old_monolith_run_report(args: argparse.Namespace, paths: RuntimePaths) -> CommandReport:
    gate_report = _old_monolith_gate_report(args, paths)
    execute = bool(getattr(args, "execute", False))
    details = dict(gate_report.details)
    issues = [
        ConfigIssue(key=issue.key, level=issue.level, message=issue.message)
        for issue in gate_report.issues
        if issue.key != "PCLOUD_TOOLS_OLD_MONOLITH_ARCHIVE_GATE"
    ]
    approval_status = str(details.get("archive approval status", "pending"))
    gate_open = _old_monolith_archive_gate_open()
    backup_dir = Path(str(details.get("backup dir", "-")))
    legacy_backup = Path(str(details.get("legacy backup file", "-")))
    shadow_backup = Path(str(details.get("shadow validation backup", "-")))
    archive_target = Path(str(details.get("archive target preview", "-")))
    manifest_path = archive_target / "archive-manifest.json"

    details.update(
        {
            "planned action": "archive old pcloud-manager monolith backup" if execute else "preview old monolith archive run",
            "implementation status": (
                "guarded archive copy; public wrappers and launchd are not modified"
                if execute
                else "old monolith archive run preview only; files are not copied or moved"
            ),
            "archive run gate status": (
                f"open: {_OLD_MONOLITH_ARCHIVE_GATE_VALUE}"
                if gate_open
                else f"closed: requires PCLOUD_TOOLS_OLD_MONOLITH_ARCHIVE_GATE={_OLD_MONOLITH_ARCHIVE_GATE_VALUE}"
            ),
            "archive gate status": (
                f"open: {_OLD_MONOLITH_ARCHIVE_GATE_VALUE}"
                if gate_open
                else f"closed: requires PCLOUD_TOOLS_OLD_MONOLITH_ARCHIVE_GATE={_OLD_MONOLITH_ARCHIVE_GATE_VALUE}"
            ),
            "archive can run": "yes" if gate_open and approval_status == "complete-read-only" else "no",
            "execute requested": "yes" if execute else "no",
            "state writes": "archive target copy and manifest" if execute else "none",
            "archive target": str(archive_target),
            "archive manifest": str(manifest_path),
            "future gate env": f"PCLOUD_TOOLS_OLD_MONOLITH_ARCHIVE_GATE={_OLD_MONOLITH_ARCHIVE_GATE_VALUE}",
        }
    )

    if approval_status != "complete-read-only":
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_OLD_MONOLITH_ARCHIVE_APPROVAL",
                level="error" if execute else "warning",
                message="old monolith archive execution requires complete read-only approvals",
            )
        )
    if not gate_open:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_OLD_MONOLITH_ARCHIVE_EXECUTION_GATE",
                level="error" if execute else "warning",
                message=(
                    "old monolith archive execution requires "
                    f"PCLOUD_TOOLS_OLD_MONOLITH_ARCHIVE_GATE={_OLD_MONOLITH_ARCHIVE_GATE_VALUE!r}"
                ),
            )
        )
    if execute and (not legacy_backup.exists() or not _file_contains(legacy_backup, "PCLOUD_MANAGER_CONFIG")):
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_ARCHIVE_LEGACY_BACKUP",
                level="error",
                message=f"legacy monolith backup is not usable: {legacy_backup}",
            )
        )
    if execute and not shadow_backup.exists():
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_ARCHIVE_SHADOW_BACKUP",
                level="error",
                message=f"shadow validation backup is missing: {shadow_backup}",
            )
        )
    dev_archive_root = paths.workspace_root / ".dev-state" / "old-monolith-archive"
    try:
        archive_target.relative_to(dev_archive_root)
    except ValueError:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_ARCHIVE_TARGET",
                level="error",
                message=f"refusing archive target outside {dev_archive_root}: {archive_target}",
            )
        )

    if not execute or _has_errors(issues):
        if _has_errors(issues):
            details["state writes"] = "none"
        sorted_issues = _sort_issues(issues)
        return CommandReport(
            command="archive old-monolith-run",
            status=_status_from_issues(sorted_issues),
            summary=(
                "old monolith archive execution is gated"
                if _has_errors(sorted_issues) or not gate_open
                else "old monolith archive run is ready"
            ),
            details=details,
            issues=_report_issues(sorted_issues),
            actions=gate_report.actions,
        )

    archive_target.mkdir(parents=True, exist_ok=True)
    archived_legacy = archive_target / "pcloud-manager.current"
    archived_shadow = archive_target / "shadow-validation.json"
    shutil.copy2(legacy_backup, archived_legacy)
    shutil.copy2(shadow_backup, archived_shadow)
    manifest = {
        "archived_at": datetime.now(timezone.utc).isoformat(),
        "backup_dir": str(backup_dir),
        "archive_target": str(archive_target),
        "legacy_backup": str(legacy_backup),
        "shadow_validation_backup": str(shadow_backup),
        "archived_files": [str(archived_legacy), str(archived_shadow)],
        "public_wrapper_modified": False,
        "launchd_modified": False,
        "sync_executed": False,
        "source_backup_retained": legacy_backup.exists(),
    }
    atomic_write_json(manifest_path, manifest, sort_keys=True)
    details.update(
        {
            "archived legacy backup": str(archived_legacy),
            "archived shadow validation": str(archived_shadow),
            "archive manifest payload": manifest,
            "source backup retained": "yes" if legacy_backup.exists() else "no",
            "state writes": "archive target copy and manifest",
        }
    )
    sorted_issues = _sort_issues(issues)
    return CommandReport(
        command="archive old-monolith-run",
        status=_status_from_issues(sorted_issues),
        summary="old monolith archive run completed",
        details=details,
        issues=_report_issues(sorted_issues),
        actions=gate_report.actions,
    )


def cmd_archive_old_monolith_run(args: argparse.Namespace, paths: RuntimePaths) -> int:
    report = _old_monolith_run_report(args, paths)
    output_format = "xbar" if getattr(args, "xbar", False) else "json" if getattr(args, "json", False) else "human"
    if output_format == "human":
        print(_render_old_monolith_run_human(report))
    else:
        print(render_report(report, output_format=output_format))
    return 1 if report.status == "error" else 0
