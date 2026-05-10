from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


AI_HELP_SCHEMA_VERSION = "pcloud-tools-help-ai.v1"


def add_help_parser(subparsers: argparse._SubParsersAction) -> None:
    help_parser = subparsers.add_parser(
        "help",
        help="Show command help or emit AI helper context.",
        description="Show command help or emit AI helper context.",
    )
    help_parser.add_argument(
        "topic_arg",
        nargs="?",
        help="Optional help topic or subcommand name for human help.",
    )
    help_parser.add_argument(
        "--ai",
        metavar="REQUEST",
        help="Emit machine-readable JSON context for an external AI/helper request.",
    )
    help_parser.add_argument(
        "--topic",
        action="append",
        default=[],
        help="Include a task-oriented AI help topic. Can be passed multiple times.",
    )


def cmd_help(args: argparse.Namespace, parser: argparse.ArgumentParser, *, dev_mode: bool) -> int:
    if args.ai is not None:
        print(render_ai_help(parser, request=args.ai, topics=_requested_topics(args), dev_mode=dev_mode))
        return 0

    topic = getattr(args, "topic_arg", None)
    if topic:
        print(render_topic_help(parser, topic))
        return 0

    print(parser.format_help().rstrip())
    print()
    print("Examples:")
    print(f"  {parser.prog} info")
    print(f"  {parser.prog} info paths")
    print(f"  {parser.prog} status --json")
    print(f"  {parser.prog} doctor --json")
    print(f"  {parser.prog} pushd status --xbar")
    print(f"  {parser.prog} diffd status --xbar")
    print(f"  {parser.prog} help --ai \"inspect pushd launchd status\" --topic pushd")
    return 0


def _requested_topics(args: argparse.Namespace) -> list[str]:
    topics: list[str] = []
    topic_arg = getattr(args, "topic_arg", None)
    if topic_arg:
        topics.append(topic_arg)
    topics.extend(getattr(args, "topic", []))
    return topics


def render_topic_help(parser: argparse.ArgumentParser, topic: str) -> str:
    subparser = _subparser_for_topic(parser, topic)
    if subparser is not None:
        return subparser.format_help().rstrip()

    topic_payload = _topic_payload(topic)
    if topic_payload is None:
        available = ", ".join(sorted(_available_topics(parser)))
        return f"Unknown help topic: {topic}\nAvailable topics: {available}"

    lines = [f"{parser.prog} help topic: {topic}", ""]
    lines.extend(topic_payload["summary"])
    if topic_payload["commands"]:
        lines.append("")
        lines.append("Commands:")
        lines.extend(f"  {command}" for command in topic_payload["commands"])
    if topic_payload["safety"]:
        lines.append("")
        lines.append("Safety:")
        lines.extend(f"  - {rule}" for rule in topic_payload["safety"])
    return "\n".join(lines)


def render_ai_help(
    parser: argparse.ArgumentParser,
    *,
    request: str,
    topics: list[str],
    dev_mode: bool,
) -> str:
    selected_topics = topics or _default_ai_topics()
    payload = {
        "schema_version": AI_HELP_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "context_kind": "custom-cli-help-ai",
        "command_name": parser.prog,
        "runtime_mode": "dev" if dev_mode else "public",
        "user_request": request,
        "generated_help": {
            "root": parser.format_help(),
            "subcommands": _subcommand_help(parser, selected_topics),
        },
        "topics": [_topic_payload(topic, include_name=True) for topic in selected_topics],
        "safety_rules": _safety_rules(),
        "important_paths": _important_paths(),
        "non_goals": [
            "This command does not call an LLM.",
            "This command does not execute generated commands.",
            "This command does not mutate runtime state.",
            "This command does not read private or large content to build context.",
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)


def _subcommand_help(parser: argparse.ArgumentParser, topics: list[str]) -> dict[str, str]:
    requested = set(_important_subcommands_for_topics(topics))
    subcommands = _subparser_choices(parser)
    return {
        name: subcommands[name].format_help()
        for name in sorted(requested)
        if name in subcommands
    }


def _subparser_for_topic(parser: argparse.ArgumentParser, topic: str) -> argparse.ArgumentParser | None:
    return _subparser_choices(parser).get(topic)


def _subparser_choices(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def _available_topics(parser: argparse.ArgumentParser) -> set[str]:
    return set(_subparser_choices(parser)) | set(_TOPICS)


def _default_ai_topics() -> list[str]:
    return ["overview", "safety", "mode", "pushd", "diffd", "sync"]


def _important_subcommands_for_topics(topics: list[str]) -> list[str]:
    selected: set[str] = {"help", "info", "status", "doctor", "gates"}
    for topic in topics:
        normalized = topic.lower()
        if normalized in {"pushd", "launchd", "transfer"}:
            selected.add("pushd")
        if normalized in {"diffd", "launchd", "transfer"}:
            selected.add("diffd")
        if normalized in {"sync", "config"}:
            selected.add("sync")
        if normalized in {"mode", "launchd", "sync"}:
            selected.add("mode")
        if normalized in {"overview", "config"}:
            selected.update({"status", "doctor"})
    return sorted(selected)


def _topic_payload(topic: str, *, include_name: bool = False) -> dict[str, Any] | None:
    normalized = topic.lower()
    data = _TOPICS.get(normalized)
    if data is None:
        if include_name:
            return {
                "name": topic,
                "summary": [f"Unknown topic: {topic}"],
                "commands": [],
                "safety": ["Do not infer missing behavior from an unknown topic."],
            }
        return None
    payload = dict(data)
    if include_name:
        payload = {"name": normalized, **payload}
    return payload


def _safety_rules() -> list[str]:
    return [
        "Treat help --ai output as context only; do not execute commands from it automatically.",
        "Do not run automatic upload/download transfer execution without an explicit human gate.",
        "Do not run normal sync/resync, listing cache operations, or autosync launchd changes from daemon validation flow.",
        "Keep bisync/autosync and pushd/diffd daemon automation mutually exclusive; use mode status/plan before switching.",
        "Do not print secrets, OAuth tokens, rclone config token values, or private file contents.",
        "Prefer read-only status/preview/gate/check commands before any gated --execute path.",
        "For launchd, use review/status/gate first; bootout/bootstrap/reload/register require explicit human approval and service-specific gate env.",
        "For pCloud API paths, live API execution must be bounded and gated; no automatic retry loop or download transfer follows from help context.",
    ]


def _important_paths() -> dict[str, str]:
    root = Path("/Users/takafumi/p-core/dev/pcloud-tools")
    return {
        "implementation_root": str(root),
        "public_wrapper": "/Users/takafumi/bin/pcloud-manager",
        "dotfiles_wrapper": "/Users/takafumi/p-core/dotfiles/.zsh/functions/pcloud-manager",
        "dev_wrapper": str(root / "pcloud-manager-dev"),
        "public_config": "/Users/takafumi/.config/pcloud-tools/.env",
        "dev_config": str(root / ".dev-state/config/.env"),
        "public_state": "/Users/takafumi/.pcloud",
        "public_logs": "/Users/takafumi/.pcloud/logs",
        "spec_ai_overview": "/Users/takafumi/p-core/dev/#仕様書/pcloud-manager/AI向け概要.md",
        "spec_technical": "/Users/takafumi/p-core/dev/#仕様書/pcloud-manager/技術仕様.md",
        "spec_usage": "/Users/takafumi/p-core/dev/#仕様書/pcloud-manager/利用ガイド.md",
    }


_TOPICS: dict[str, dict[str, Any]] = {
    "overview": {
        "summary": [
            "pcloud-manager is the public Python CLI for local pCloud/rclone operations.",
            "The dev wrapper is ./pcloud-manager-dev and keeps config/state/logs under .dev-state/.",
            "Use info/status/doctor discovery before operational changes.",
        ],
        "commands": [
            "pcloud-manager info",
            "pcloud-manager info paths",
            "pcloud-manager status --json",
            "pcloud-manager doctor --json",
            "pcloud-manager help --ai \"request\" --topic overview",
        ],
        "safety": [
            "Read-only discovery commands are preferred.",
            "Public and dev runtime modes must not be confused.",
        ],
    },
    "safety": {
        "summary": [
            "The command controls sync, launchd, pCloud API polling, and transfer gates.",
            "Dangerous work is separated into preview/review/gate/execute phases.",
        ],
        "commands": [
            "pcloud-manager gates status",
            "pcloud-manager pushd launchd status",
            "pcloud-manager diffd launchd status",
        ],
        "safety": _safety_rules(),
    },
    "mode": {
        "summary": [
            "mode is the exclusive operation switch for daemon, maintenance, and pause states.",
            "daemon mode uses pushd/diffd residents and executors while bisync remains disabled.",
            "maintenance mode stops daemon automation but does not enable or run bisync automatically.",
        ],
        "commands": [
            "pcloud-manager mode status --json",
            "pcloud-manager mode plan daemon",
            "pcloud-manager mode plan maintenance",
            "pcloud-manager mode plan pause",
        ],
        "safety": [
            "Do not run sync/resync, transfers, listing cache work, or diffd checkpoint from mode switch.",
            "Dirty queue/change/manual-review state blocks mode switch execution.",
            "Run diffd api-poll checkpoint separately when returning from maintenance if the operator decides it is needed.",
        ],
    },
    "pushd": {
        "summary": [
            "pushd tracks local filesystem events and appends upload queue records.",
            "Current live launchd state is queue-only; automatic upload transfer remains closed.",
        ],
        "commands": [
            "pcloud-manager pushd status --xbar",
            "pcloud-manager pushd preview --json",
            "pcloud-manager pushd launchd status --json",
            "pcloud-manager pushd transfer check",
        ],
        "safety": [
            "Do not execute upload transfers automatically from queued records.",
            "Delete/rename events go to manual review instead of automatic upload work.",
        ],
    },
    "diffd": {
        "summary": [
            "diffd polls pCloud /diff and appends allowlisted remote-change records.",
            "Current live launchd state is bounded API one-shot; automatic download transfer remains closed.",
        ],
        "commands": [
            "pcloud-manager diffd status --xbar",
            "pcloud-manager diffd preview --json",
            "pcloud-manager diffd launchd status --json",
            "pcloud-manager diffd transfer check",
        ],
        "safety": [
            "Do not execute download transfers automatically from remote-change records.",
            "Live API use stays bounded and human-gated.",
        ],
    },
    "launchd": {
        "summary": [
            "launchd surfaces expose review/status/plist/register/reload phases.",
            "Status/review/gate are read-only; write/reload/register paths are service-specific gated operations.",
        ],
        "commands": [
            "pcloud-manager pushd launchd status --json",
            "pcloud-manager diffd launchd status --json",
            "pcloud-manager pushd launchd gate",
            "pcloud-manager diffd launchd gate",
        ],
        "safety": [
            "Do not bootstrap/bootout/reload/register without explicit human approval.",
            "Keep autosync launchd changes separate from pushd/diffd launchd changes.",
        ],
    },
    "transfer": {
        "summary": [
            "Transfer surfaces preview rclone copyto commands and real-transfer gates.",
            "Real transfer execution is separate from queue/diff daemon operation.",
        ],
        "commands": [
            "pcloud-manager pushd transfer preview",
            "pcloud-manager diffd transfer preview",
            "pcloud-manager pushd transfer check",
            "pcloud-manager diffd transfer check",
        ],
        "safety": [
            "Do not consume queue/change records unless the approved consume policy says so.",
            "Do not reuse fake-rclone dev gates for real pCloud/rclone transfer.",
        ],
    },
    "sync": {
        "summary": [
            "sync handles rclone bisync, autosync state, allowlist scope, and lock diagnostics.",
            "Daemon validation must not trigger normal sync/resync or listing cache operations.",
        ],
        "commands": [
            "pcloud-manager sync status --json",
            "pcloud-manager sync scope",
            "pcloud-manager sync migration-gate",
        ],
        "safety": [
            "Do not run normal sync/resync from help context.",
            "Do not delete or move listing cache files without a dedicated gate.",
        ],
    },
    "config": {
        "summary": [
            "Public config is /Users/takafumi/.config/pcloud-tools/.env.",
            "Dev config is .dev-state/config/.env under the implementation root.",
        ],
        "commands": [
            "pcloud-manager doctor --json",
            "pcloud-manager doctor --repair --json",
        ],
        "safety": [
            "Do not print secrets or token values.",
            "doctor --repair may create starter config but must not invent secret values.",
        ],
    },
}
