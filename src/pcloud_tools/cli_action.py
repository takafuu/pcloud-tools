from __future__ import annotations

import argparse
from collections.abc import Callable

from .runtime import RuntimePaths

_ACTION_DISPATCH: dict[str, tuple[str, ...]] = {
    "status.refresh": ("status", "--xbar"),
    "status.detail": ("status", "--detail"),
    "doctor": ("doctor",),
    "sync.status.refresh": ("sync", "status", "--xbar"),
    "sync.preview": ("sync",),
    "sync.progress": ("sync", "progress"),
    "sync.scope": ("sync", "scope"),
    "sync.background.preview": ("sync", "background"),
    "sync.autosync-plist.preview": ("sync", "autosync-plist"),
    "sync.autosync.gate": ("sync", "autosync-gate"),
    "sync.autosync-run.preview": ("sync", "autosync-run", "enable"),
    "sync.migration.gate": ("sync", "migration-gate"),
    "sync.migration-run.preview": ("sync", "migration-run", "normal"),
    "sync.clear-stale-lock.preview": ("sync", "clear-stale-lock"),
    "gates.status": ("gates", "status"),
    "notify.chat.status": ("notify", "status", "--xbar"),
    "notify.chat.enable": ("notify", "enable", "--xbar"),
    "notify.chat.disable": ("notify", "disable", "--xbar"),
    "notify.chat.test": ("notify", "test", "--xbar"),
    "mode.status.refresh": ("mode", "status", "--xbar"),
    "mode.plan.daemon": ("mode", "plan", "daemon"),
    "mode.plan.maintenance": ("mode", "plan", "maintenance"),
    "mode.plan.pause": ("mode", "plan", "pause"),
    "daemon.status.refresh": ("daemon", "status", "--xbar"),
    "daemon.auto-download.on.preview": ("daemon", "auto-download", "on"),
    "daemon.auto-download.off.preview": ("daemon", "auto-download", "off"),
    "pushd.status.refresh": ("pushd", "status", "--xbar"),
    "pushd.preview": ("pushd", "preview"),
    "pushd.policy": ("pushd", "policy"),
    "pushd.run.preview": ("pushd", "run"),
    "pushd.backfill.preview": ("pushd", "backfill", "preview"),
    "pushd.gate": ("pushd", "gate"),
    "pushd.launchd.gate": ("pushd", "launchd", "gate"),
    "pushd.launchd.status": ("pushd", "launchd", "status"),
    "pushd.launchd.review": ("pushd", "launchd", "review"),
    "pushd.launchd.register.preview": ("pushd", "launchd", "register"),
    "pushd.launchd.reload.preview": ("pushd", "launchd", "reload"),
    "pushd.launchd.resident-plist.preview": ("pushd", "launchd", "resident-plist"),
    "pushd.launchd.executor-plist.preview": ("pushd", "launchd", "executor-plist"),
    "pushd.launchd.automation-plist.preview": ("pushd", "launchd", "automation-plist"),
    "pushd.launchd.automation-reload.preview": ("pushd", "launchd", "automation-reload"),
    "pushd.launchd.plist.preview": ("pushd", "launchd", "plist"),
    "pushd.fswatch.resident-gate": ("pushd", "fswatch", "resident-gate"),
    "pushd.fswatch.resident-run.preview": ("pushd", "fswatch", "resident-run"),
    "pushd.transfer.preview": ("pushd", "transfer", "preview"),
    "pushd.transfer.validation-matrix": ("pushd", "transfer", "validation-matrix"),
    "pushd.transfer.check": ("pushd", "transfer", "check"),
    "pushd.transfer.real-gate": ("pushd", "transfer", "real-gate"),
    "pushd.transfer.automation-gate": ("pushd", "transfer", "automation-gate"),
    "pushd.transfer.real-run.preview": ("pushd", "transfer", "real-run"),
    "pushd.transfer.executor-run.preview": ("pushd", "transfer", "executor-run"),
    "pushd.transfer.consume.preview": ("pushd", "transfer", "consume", "preview"),
    "pushd.queue.clear.preview": ("pushd", "queue", "clear"),
    "pushd.queue.prune-missing-local": (
        "pushd",
        "queue",
        "prune-missing-local",
        "--reviewer-approved-missing-local-cleanup",
        "--execute",
        "--xbar",
    ),
    "diffd.status.refresh": ("diffd", "status", "--xbar"),
    "diffd.preview": ("diffd", "preview"),
    "diffd.policy": ("diffd", "policy"),
    "diffd.run.preview": ("diffd", "run"),
    "diffd.gate": ("diffd", "gate"),
    "diffd.launchd.gate": ("diffd", "launchd", "gate"),
    "diffd.launchd.status": ("diffd", "launchd", "status"),
    "diffd.launchd.review": ("diffd", "launchd", "review"),
    "diffd.launchd.register.preview": ("diffd", "launchd", "register"),
    "diffd.launchd.reload.preview": ("diffd", "launchd", "reload"),
    "diffd.launchd.resident-plist.preview": ("diffd", "launchd", "resident-plist"),
    "diffd.launchd.executor-plist.preview": ("diffd", "launchd", "executor-plist"),
    "diffd.launchd.automation-plist.preview": ("diffd", "launchd", "automation-plist"),
    "diffd.launchd.automation-reload.preview": ("diffd", "launchd", "automation-reload"),
    "diffd.launchd.plist.preview": ("diffd", "launchd", "plist"),
    "diffd.api-poll.long-poll-gate": ("diffd", "api-poll", "long-poll-gate"),
    "diffd.api-poll.long-poll-run.preview": ("diffd", "api-poll", "long-poll-run"),
    "diffd.transfer.preview": ("diffd", "transfer", "preview"),
    "diffd.transfer.validation-matrix": ("diffd", "transfer", "validation-matrix"),
    "diffd.transfer.check": ("diffd", "transfer", "check"),
    "diffd.transfer.real-gate": ("diffd", "transfer", "real-gate"),
    "diffd.transfer.automation-gate": ("diffd", "transfer", "automation-gate"),
    "diffd.transfer.real-run.preview": ("diffd", "transfer", "real-run"),
    "diffd.transfer.executor-run.preview": ("diffd", "transfer", "executor-run"),
    "diffd.transfer.consume.preview": ("diffd", "transfer", "consume", "preview"),
    "diffd.remote-change.clear.preview": ("diffd", "remote-change", "clear"),
    "archive.old-monolith.gate": ("archive", "old-monolith-gate"),
    "archive.old-monolith-run.preview": ("archive", "old-monolith-run"),
}


def add_action_parser(subparsers: argparse._SubParsersAction) -> None:
    action_parser = subparsers.add_parser("action", help="Dispatch a stable action id for xbar or wrappers.")
    action_parser.add_argument("action_id", choices=sorted(_ACTION_DISPATCH))


def cmd_action(
    args: argparse.Namespace,
    paths: RuntimePaths,
    dispatch: Callable[[list[str]], int],
) -> int:
    del paths
    dispatch_args = _ACTION_DISPATCH[args.action_id]
    return dispatch(list(dispatch_args))
