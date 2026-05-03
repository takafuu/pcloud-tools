from __future__ import annotations

import argparse

from .cli_action import add_action_parser, cmd_action
from .cli_archive import add_archive_parser, cmd_archive
from .cli_daemon import add_daemon_parser, cmd_daemon
from .cli_index import add_index_parser, cmd_index
from .cli_mount import add_mount_parsers, cmd_mount, cmd_umount
from .cli_service_daemon import add_service_daemon_parsers, cmd_service_daemon
from .cli_status import add_status_doctor_parsers, cmd_doctor, cmd_status
from .cli_sync import add_sync_parser, cmd_sync
from .runtime import detect_runtime_paths


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pcloud-manager-dev",
        description="Development CLI for the pcloud-tools migration.",
    )
    subparsers = parser.add_subparsers(dest="command")

    add_status_doctor_parsers(subparsers)

    add_sync_parser(subparsers)

    add_mount_parsers(subparsers)

    add_index_parser(subparsers)

    add_daemon_parser(subparsers)

    add_service_daemon_parsers(subparsers)

    add_archive_parser(subparsers)

    add_action_parser(subparsers)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    paths = detect_runtime_paths()

    if args.command == "status":
        return cmd_status(args, paths)
    if args.command == "doctor":
        return cmd_doctor(args, paths)
    if args.command == "sync":
        result = cmd_sync(args, paths)
        if result is not None:
            return result
        parser.print_help()
        return 1
    if args.command == "mount":
        return cmd_mount(args, paths)
    if args.command == "umount":
        return cmd_umount(args, paths)
    if args.command == "index":
        return cmd_index(args, paths)
    if args.command == "daemon":
        result = cmd_daemon(args, paths)
        if result is not None:
            return result
        parser.print_help()
        return 1
    if args.command in {"pushd", "diffd"}:
        result = cmd_service_daemon(args, paths)
        if result is not None:
            return result
        parser.print_help()
        return 1
    if args.command == "archive":
        result = cmd_archive(args, paths)
        if result is not None:
            return result
        parser.print_help()
        return 1
    if args.command == "action":
        return cmd_action(args, paths, main)

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
