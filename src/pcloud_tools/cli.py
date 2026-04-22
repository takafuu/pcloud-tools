from __future__ import annotations

import argparse
from pathlib import Path

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

    subparsers.add_parser("doctor", help="Check runtime scaffold health.")

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


def cmd_status(args: argparse.Namespace, paths: RuntimePaths) -> int:
    if args.detail:
        print("status: scaffold-ready")
        _print_path_block(paths)
        return 0

    mode = "dev" if paths.dev_mode else "default"
    print(f"pcloud-manager-dev: scaffold-ready ({mode})")
    return 0


def _doctor_line(label: str, path: Path) -> str:
    status = "present" if path.exists() else "missing"
    return f"{label}: {status} ({path})"


def cmd_doctor(paths: RuntimePaths) -> int:
    paths.ensure_directories()
    print("doctor: scaffold-ok")
    print(_doctor_line("config dir", paths.config_dir))
    print(_doctor_line("state dir", paths.state_dir))
    print(_doctor_line("log dir", paths.log_dir))
    print(_doctor_line("env file", paths.env_file))
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
        return cmd_doctor(paths)
    if args.command == "sync":
        name = f"sync {args.sync_command}" if args.sync_command else "sync"
        return cmd_placeholder(name)
    if args.command in {"mount", "umount", "index"}:
        return cmd_placeholder(args.command)

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
