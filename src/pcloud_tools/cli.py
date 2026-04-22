from __future__ import annotations

import argparse
from pathlib import Path

from .config import ConfigIssue, load_config, repair_env_file
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

    doctor_parser = subparsers.add_parser("doctor", help="Check runtime scaffold health.")
    doctor_parser.add_argument(
        "--repair",
        action="store_true",
        help="Create a starter .env file if it is missing.",
    )

    sync_parser = subparsers.add_parser("sync", help="Sync command surface scaffold.")
    sync_subparsers = sync_parser.add_subparsers(dest="sync_command")
    for name in (
        "status",
        "progress",
        "scope",
        "check-allowlist",
        "resync",
        "full-resync",
        "track-renames",
    ):
        sync_subparsers.add_parser(name)

    for name in ("mount", "umount", "index"):
        subparsers.add_parser(name, help=f"Placeholder for `{name}` migration.")

    return parser


def _print_path_block(paths: RuntimePaths) -> None:
    print(f"workspace: {paths.workspace_root}")
    print(f"config dir: {paths.config_dir}")
    print(f"state dir: {paths.state_dir}")
    print(f"log dir: {paths.log_dir}")
    print(f"env file: {paths.env_file}")


def _print_config_summary(paths: RuntimePaths) -> None:
    load_result = load_config(paths)
    config = load_result.config
    print(f"config source: {load_result.source}")
    print(f"core dir: {config.core_dir}")
    print(f"state dir: {config.state_dir}")
    print(f"log dir: {config.log_dir}")
    print(f"allowlist: {config.allowlist_file}")
    print(f"core remote: {config.core_remote}")
    print(f"vault layer: {'enabled' if config.enable_vault_layer else 'disabled'}")
    print(f"crypt layer: {'enabled' if config.enable_crypt_layer else 'disabled'}")


def _has_errors(issues: list[ConfigIssue]) -> bool:
    return any(issue.level == "error" for issue in issues)


def cmd_status(args: argparse.Namespace, paths: RuntimePaths) -> int:
    if args.detail:
        print("status: scaffold-ready")
        _print_path_block(paths)
        _print_config_summary(paths)
        return 0

    mode = "dev" if paths.dev_mode else "default"
    print(f"pcloud-manager-dev: scaffold-ready ({mode})")
    return 0


def _doctor_line(label: str, path: Path) -> str:
    status = "present" if path.exists() else "missing"
    return f"{label}: {status} ({path})"


def _issue_sort_key(issue: ConfigIssue) -> tuple[int, str]:
    priority = 0 if issue.level == "error" else 1
    return (priority, issue.key)


def cmd_doctor(args: argparse.Namespace, paths: RuntimePaths) -> int:
    paths.ensure_directories()
    repaired = False
    if args.repair:
        env_missing = not paths.env_file.exists()
        repair_env_file(paths)
        repaired = env_missing and paths.env_file.exists()

    load_result = load_config(paths)
    issues = sorted(load_result.issues, key=_issue_sort_key)
    status = "ok" if not _has_errors(issues) else "needs-attention"

    print(f"doctor: {status}")
    print(_doctor_line("config dir", paths.config_dir))
    print(_doctor_line("state dir", paths.state_dir))
    print(_doctor_line("log dir", paths.log_dir))
    print(_doctor_line("env file", paths.env_file))
    print(f"config source: {load_result.source}")
    if repaired:
        print(f"repair: wrote starter env file to {paths.env_file}")

    print("config")
    print(f"core dir: {load_result.config.core_dir}")
    print(f"state dir: {load_result.config.state_dir}")
    print(f"log dir: {load_result.config.log_dir}")
    print(f"allowlist: {load_result.config.allowlist_file}")
    print(f"core remote: {load_result.config.core_remote}")
    print(
        f"vault layer: {'enabled' if load_result.config.enable_vault_layer else 'disabled'}"
    )
    print(
        f"crypt layer: {'enabled' if load_result.config.enable_crypt_layer else 'disabled'}"
    )

    if issues:
        print("issues")
        for issue in issues:
            print(f"- {issue.level}: {issue.key}: {issue.message}")
        if _has_errors(issues):
            return 1

    return 0


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
        name = f"sync {args.sync_command}" if args.sync_command else "sync"
        return cmd_placeholder(name)
    if args.command in {"mount", "umount", "index"}:
        return cmd_placeholder(args.command)

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
