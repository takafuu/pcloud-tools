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
    "sync.autosync.gate": ("sync", "autosync-gate"),
    "sync.clear-stale-lock.preview": ("sync", "clear-stale-lock"),
    "daemon.status.refresh": ("daemon", "status", "--xbar"),
    "daemon.auto-download.on.preview": ("daemon", "auto-download", "on"),
    "daemon.auto-download.off.preview": ("daemon", "auto-download", "off"),
    "pushd.status.refresh": ("pushd", "status", "--xbar"),
    "pushd.preview": ("pushd", "preview"),
    "pushd.run.preview": ("pushd", "run"),
    "pushd.gate": ("pushd", "gate"),
    "pushd.fswatch.resident-gate": ("pushd", "fswatch", "resident-gate"),
    "pushd.transfer.preview": ("pushd", "transfer", "preview"),
    "pushd.transfer.check": ("pushd", "transfer", "check"),
    "pushd.transfer.real-gate": ("pushd", "transfer", "real-gate"),
    "pushd.transfer.real-run.preview": ("pushd", "transfer", "real-run"),
    "pushd.transfer.consume.preview": ("pushd", "transfer", "consume", "preview"),
    "pushd.queue.clear.preview": ("pushd", "queue", "clear"),
    "diffd.status.refresh": ("diffd", "status", "--xbar"),
    "diffd.preview": ("diffd", "preview"),
    "diffd.run.preview": ("diffd", "run"),
    "diffd.gate": ("diffd", "gate"),
    "diffd.api-poll.long-poll-gate": ("diffd", "api-poll", "long-poll-gate"),
    "diffd.transfer.preview": ("diffd", "transfer", "preview"),
    "diffd.transfer.check": ("diffd", "transfer", "check"),
    "diffd.transfer.real-gate": ("diffd", "transfer", "real-gate"),
    "diffd.transfer.real-run.preview": ("diffd", "transfer", "real-run"),
    "diffd.transfer.consume.preview": ("diffd", "transfer", "consume", "preview"),
    "diffd.remote-change.clear.preview": ("diffd", "remote-change", "clear"),
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
