from __future__ import annotations

import argparse

from .config import ConfigIssue, load_config, repair_allowlist_file, repair_env_file
from .output import CommandReport, ReportIssue, render_report
from .runtime import RuntimePaths, detect_runtime_paths


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
    for name in ("progress", "scope", "check-allowlist", "resync", "full-resync", "track-renames"):
        sync_subparsers.add_parser(name)

    for name in ("mount", "umount", "index"):
        subparsers.add_parser(name, help=f"Placeholder for `{name}` migration.")

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


def _status_report(args: argparse.Namespace, paths: RuntimePaths) -> CommandReport:
    load_result = load_config(paths)
    issues = sorted(load_result.issues, key=_issue_sort_key)
    mode = "dev" if paths.dev_mode else "default"
    details = {
        "workspace": str(paths.workspace_root),
        "config dir": str(paths.config_dir),
        "state dir": str(paths.state_dir),
        "log dir": str(paths.log_dir),
        "env file": str(paths.env_file),
    }
    if args.detail:
        details.update(_config_summary(paths))
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
    return 1 if report.status == "error" else 0


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

    issues = sorted(load_result.issues, key=_issue_sort_key)
    has_errors = _has_errors(issues)
    status = _status_from_issues(issues)
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
    issues = sorted(load_result.issues, key=_issue_sort_key)
    details = {
        "runtime": "development",
        "sync engine": "bisync fallback scaffold",
        "running": "no",
        "config source": load_result.source,
        "state dir": str(load_result.config.state_dir),
        "allowlist": str(load_result.config.allowlist_file),
        "core remote": load_result.config.core_remote,
        "next milestone": "port sync status/progress/scope compatibility",
    }
    return CommandReport(
        command="sync status",
        status=_status_from_issues(issues),
        summary="sync command surface is scaffolded and ready for migration work",
        details=details,
        issues=_report_issues(issues),
    )


def cmd_sync_status(args: argparse.Namespace, paths: RuntimePaths) -> int:
    report = _sync_status_report(paths)
    print(render_report(report, as_json=args.json))
    return 1 if report.status == "error" else 0


def cmd_placeholder(command: str) -> int:
    print(f"{command}: not implemented yet")
    return 0


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
        name = f"sync {args.sync_command}" if args.sync_command else "sync"
        return cmd_placeholder(name)
    if args.command in {"mount", "umount", "index"}:
        return cmd_placeholder(args.command)

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
