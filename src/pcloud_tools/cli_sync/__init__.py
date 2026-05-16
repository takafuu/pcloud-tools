from __future__ import annotations

import argparse
from pathlib import Path

from ..gates import GATES, add_gate_review_args
from ..runtime import RuntimePaths
from ..sync_exec import DEFAULT_RESYNC_MODE, RESYNC_MODES
from .autosync import (
    _sync_autosync_gate_report,
    cmd_sync_autosync,
    cmd_sync_autosync_gate,
    cmd_sync_autosync_plist,
    cmd_sync_autosync_run,
)
from .core import (
    cmd_sync_background,
    cmd_sync_check_allowlist,
    cmd_sync_clear_stale_lock,
    cmd_sync_execution,
    cmd_sync_internal_run,
    cmd_sync_progress,
    cmd_sync_scope,
    cmd_sync_status,
)
from .migration import (
    _sync_migration_gate_report,
    cmd_sync_migration_gate,
    cmd_sync_migration_run,
)

def add_sync_parser(subparsers: argparse._SubParsersAction) -> None:
    sync_parser = subparsers.add_parser("sync", help="Sync command surface scaffold.")
    sync_parser.add_argument(
        "--execute",
        action="store_true",
        help="Run the normal rclone bisync command instead of only previewing it.",
    )
    sync_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit structured JSON output for the normal sync command.",
    )
    sync_subparsers = sync_parser.add_subparsers(dest="sync_command")
    sync_status_parser = sync_subparsers.add_parser("status")
    sync_status_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    sync_status_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
    sync_scope_parser = sync_subparsers.add_parser("scope")
    sync_scope_parser.add_argument("--filter", action="store_true", help="Include the generated bisync filter rules.")
    sync_scope_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    sync_check_parser = sync_subparsers.add_parser("check-allowlist")
    sync_check_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    sync_progress_parser = sync_subparsers.add_parser("progress")
    sync_progress_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    sync_background_parser = sync_subparsers.add_parser("background")
    sync_background_parser.add_argument("--resync", action="store_true", help="Run background sync in resync mode.")
    sync_background_parser.add_argument(
        "--track-renames",
        action="store_true",
        help="Run background sync in track-renames mode.",
    )
    sync_background_parser.add_argument("--notify", action="store_true", help="Send a completion notification.")
    sync_background_parser.add_argument(
        "--no-notify",
        action="store_true",
        help="Disable the completion notification.",
    )
    sync_background_parser.add_argument(
        "--execute",
        action="store_true",
        help="Launch the background sync instead of only previewing it.",
    )
    sync_background_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    sync_enable_autosync_parser = sync_subparsers.add_parser("enable-autosync")
    sync_enable_autosync_parser.add_argument(
        "--execute",
        action="store_true",
        help="Apply the launchctl changes instead of only previewing them.",
    )
    sync_enable_autosync_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    sync_disable_autosync_parser = sync_subparsers.add_parser("disable-autosync")
    sync_disable_autosync_parser.add_argument(
        "--yes",
        action="store_true",
        help="Acknowledge the disable action without prompting.",
    )
    sync_disable_autosync_parser.add_argument(
        "--execute",
        action="store_true",
        help="Apply the launchctl changes instead of only previewing them.",
    )
    sync_disable_autosync_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    sync_autosync_plist_parser = sync_subparsers.add_parser(
        "autosync-plist", help="Preview or write the dev autosync LaunchAgent plist."
    )
    sync_autosync_plist_parser.add_argument(
        "--execute",
        action="store_true",
        help="Write the plist when running in dev mode with a .dev-state target.",
    )
    sync_autosync_plist_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    sync_autosync_gate_parser = sync_subparsers.add_parser(
        "autosync-gate", help="Read-only checklist before changing autosync launchd registration."
    )
    sync_autosync_gate_parser.add_argument("--report-path", type=Path)
    add_gate_review_args(sync_autosync_gate_parser, GATES["autosync.launchd"])
    sync_autosync_gate_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    sync_autosync_gate_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
    sync_autosync_run_parser = sync_subparsers.add_parser(
        "autosync-run", help="Run guarded autosync launchd changes after the dedicated gate opens."
    )
    sync_autosync_run_parser.add_argument("mode", choices=("enable", "disable"))
    sync_autosync_run_parser.add_argument("--report-path", type=Path)
    add_gate_review_args(sync_autosync_run_parser, GATES["autosync.launchd"])
    sync_autosync_run_parser.add_argument("--execute", action="store_true")
    sync_autosync_run_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    sync_autosync_run_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
    sync_migration_gate_parser = sync_subparsers.add_parser(
        "migration-gate", help="Read-only checklist before running normal sync/resync migration validation."
    )
    sync_migration_gate_parser.add_argument("--report-path", type=Path)
    sync_migration_gate_parser.add_argument("--sync-status-report-path", type=Path)
    add_gate_review_args(sync_migration_gate_parser, GATES["sync.migration"])
    sync_migration_gate_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    sync_migration_gate_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
    sync_migration_run_parser = sync_subparsers.add_parser(
        "migration-run", help="Run guarded normal/resync migration validation after the dedicated gate opens."
    )
    sync_migration_run_parser.add_argument("mode", choices=("normal", "resync"))
    sync_migration_run_parser.add_argument("--resync-mode", choices=RESYNC_MODES, default=DEFAULT_RESYNC_MODE)
    sync_migration_run_parser.add_argument("--report-path", type=Path)
    sync_migration_run_parser.add_argument("--sync-status-report-path", type=Path)
    add_gate_review_args(sync_migration_run_parser, GATES["sync.migration"])
    sync_migration_run_parser.add_argument("--execute", action="store_true")
    sync_migration_run_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    sync_migration_run_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
    sync_clear_stale_lock_parser = sync_subparsers.add_parser("clear-stale-lock")
    sync_clear_stale_lock_parser.add_argument(
        "--execute",
        action="store_true",
        help="Remove the stale lock instead of only previewing it.",
    )
    sync_clear_stale_lock_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    sync_internal_run_parser = sync_subparsers.add_parser("_run", help=argparse.SUPPRESS)
    sync_internal_run_parser.add_argument("mode", choices=("normal", "autosync", "resync", "full-resync", "track-renames"))
    sync_internal_run_parser.add_argument("notify_flag", nargs="?", default="0")
    sync_internal_run_parser.add_argument("--resync-mode", choices=RESYNC_MODES, default=DEFAULT_RESYNC_MODE)
    for name in ("resync", "full-resync"):
        command_parser = sync_subparsers.add_parser(name)
        command_parser.add_argument(
            "--execute",
            action="store_true",
            help="Run the rclone bisync command instead of only previewing it.",
        )
        command_parser.add_argument(
            "--resync-mode",
            choices=RESYNC_MODES,
            default=DEFAULT_RESYNC_MODE,
            help="Choose which side rclone prefers when rebuilding the bisync baseline.",
        )
        command_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    track_renames_parser = sync_subparsers.add_parser("track-renames")
    track_renames_parser.add_argument(
        "--execute",
        action="store_true",
        help="Run the rclone bisync command instead of only previewing it.",
    )
    track_renames_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")


def cmd_sync(args: argparse.Namespace, paths: RuntimePaths) -> int | None:
    if args.sync_command is None:
        return cmd_sync_execution(args, paths, "normal")
    if args.sync_command == "status":
        return cmd_sync_status(args, paths)
    if args.sync_command == "progress":
        return cmd_sync_progress(args, paths)
    if args.sync_command == "background":
        return cmd_sync_background(args, paths)
    if args.sync_command == "clear-stale-lock":
        return cmd_sync_clear_stale_lock(args, paths)
    if args.sync_command == "enable-autosync":
        return cmd_sync_autosync(args, paths, "enable-autosync")
    if args.sync_command == "disable-autosync":
        return cmd_sync_autosync(args, paths, "disable-autosync")
    if args.sync_command == "autosync-plist":
        return cmd_sync_autosync_plist(args, paths)
    if args.sync_command == "autosync-gate":
        return cmd_sync_autosync_gate(args, paths)
    if args.sync_command == "autosync-run":
        return cmd_sync_autosync_run(args, paths)
    if args.sync_command == "migration-gate":
        return cmd_sync_migration_gate(args, paths)
    if args.sync_command == "migration-run":
        return cmd_sync_migration_run(args, paths)
    if args.sync_command == "_run":
        return cmd_sync_internal_run(args, paths)
    if args.sync_command in {"resync", "full-resync", "track-renames"}:
        return cmd_sync_execution(args, paths, args.sync_command)
    if args.sync_command == "scope":
        return cmd_sync_scope(args, paths)
    if args.sync_command == "check-allowlist":
        return cmd_sync_check_allowlist(args, paths)
    return None

def cmd_sync(args: argparse.Namespace, paths: RuntimePaths) -> int | None:
    if args.sync_command is None:
        return cmd_sync_execution(args, paths, "normal")
    if args.sync_command == "status":
        return cmd_sync_status(args, paths)
    if args.sync_command == "progress":
        return cmd_sync_progress(args, paths)
    if args.sync_command == "background":
        return cmd_sync_background(args, paths)
    if args.sync_command == "clear-stale-lock":
        return cmd_sync_clear_stale_lock(args, paths)
    if args.sync_command == "enable-autosync":
        return cmd_sync_autosync(args, paths, "enable-autosync")
    if args.sync_command == "disable-autosync":
        return cmd_sync_autosync(args, paths, "disable-autosync")
    if args.sync_command == "autosync-plist":
        return cmd_sync_autosync_plist(args, paths)
    if args.sync_command == "autosync-gate":
        return cmd_sync_autosync_gate(args, paths)
    if args.sync_command == "autosync-run":
        return cmd_sync_autosync_run(args, paths)
    if args.sync_command == "migration-gate":
        return cmd_sync_migration_gate(args, paths)
    if args.sync_command == "migration-run":
        return cmd_sync_migration_run(args, paths)
    if args.sync_command == "_run":
        return cmd_sync_internal_run(args, paths)
    if args.sync_command in {"resync", "full-resync", "track-renames"}:
        return cmd_sync_execution(args, paths, args.sync_command)
    if args.sync_command == "scope":
        return cmd_sync_scope(args, paths)
    if args.sync_command == "check-allowlist":
        return cmd_sync_check_allowlist(args, paths)
    return None
