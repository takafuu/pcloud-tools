from __future__ import annotations

import argparse
import fnmatch
import json
import os
import shlex
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .cli_common import has_errors, report_issues, sort_issues, status_from_issues
from .config import ConfigIssue
from .io_utils import atomic_write_json
from .output import CommandReport, render_report


ARCHIVE_HELP_AI_SCHEMA_VERSION = "pcloud-archive-help-ai.v1"
ARCHIVE_REPORT_SCHEMA_VERSION = "pcloud-archive-report.v1"


@dataclass(frozen=True)
class ArchiveProfile:
    name: str
    config_file: Path
    config_source: str
    source_root: Path
    remote_root: str
    state_dir: Path
    log_dir: Path
    rclone_bin: str
    transfers: int
    checkers: int
    bwlimit: str
    tpslimit: int
    retries: int
    low_level_retries: int
    ignore_patterns: tuple[str, ...]

    @property
    def manifest_file(self) -> Path:
        return self.state_dir / "manifest.json"

    @property
    def tombstone_file(self) -> Path:
        return self.state_dir / "tombstones.json"

    @property
    def last_run_file(self) -> Path:
        return self.state_dir / "last-run.json"


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if args.command == "help":
        return _cmd_help(args, parser)
    report = _dispatch_report(args, parser)
    print(render_report(report, output_format="json" if getattr(args, "json", False) else "human"))
    return 1 if report.status == "error" else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pcloud-archive",
        description="Promote NAS/Mac staging data into pcloud-crypt canonical archive storage.",
    )
    parser.add_argument("--config", type=Path, help="Override config file path.")
    subparsers = parser.add_subparsers(dest="command")

    help_parser = subparsers.add_parser("help", help="Show command help or emit AI helper context.")
    help_parser.add_argument("topic_arg", nargs="?")
    help_parser.add_argument("--ai", metavar="REQUEST")
    help_parser.add_argument("--topic", action="append", default=[])

    for name in ("info", "doctor", "status", "diff"):
        sub = subparsers.add_parser(name, help=f"{name} pcloud-archive state.")
        _add_profile_args(sub)
        if name == "info":
            sub.add_argument("view", choices=("overview", "paths", "config"), nargs="?", default="overview")
        sub.add_argument("--json", action="store_true", help="Emit structured JSON output.")

    promote = subparsers.add_parser("promote", help="Preview or execute rclone copy into the archive remote.")
    _add_profile_args(promote)
    promote.add_argument("path", help="Path under source_root, or an absolute path inside source_root.")
    promote.add_argument("--dry-run", action="store_true", help="Preview the rclone copy command.")
    promote.add_argument("--execute", action="store_true", help="Run the planned rclone copy command.")
    promote.add_argument("--bwlimit", help="Override rclone --bwlimit for this run.")
    promote.add_argument("--json", action="store_true", help="Emit structured JSON output.")

    check = subparsers.add_parser("check", help="Run rclone check and update the manifest on success.")
    _add_profile_args(check)
    check.add_argument("path", help="Path under source_root, or an absolute path inside source_root.")
    check.add_argument("--execute", action="store_true", help="Run rclone check. Default is preview only.")
    check.add_argument("--json", action="store_true", help="Emit structured JSON output.")

    delete = subparsers.add_parser("delete-canonical", help="Preview or execute canonical remote deletion.")
    _add_profile_args(delete)
    delete.add_argument("remote_path", help="Path under remote_root, or a full remote path.")
    delete.add_argument("--execute", action="store_true", help="Run rclone deletefile and record a tombstone.")
    delete.add_argument("--json", action="store_true", help="Emit structured JSON output.")

    drop = subparsers.add_parser("drop-cache", help="Preview local cache deletion without touching pCloud.")
    _add_profile_args(drop)
    drop.add_argument("local_path", help="Local cache path to preview.")
    drop.add_argument("--json", action="store_true", help="Emit structured JSON output.")

    return parser


def _add_profile_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--profile", help="Profile name. Defaults to config defaults.profile or nas-dev.")


def _dispatch_report(args: argparse.Namespace, parser: argparse.ArgumentParser) -> CommandReport:
    if args.command == "info":
        return _info_report(args)
    if args.command == "doctor":
        return _doctor_report(args)
    if args.command == "status":
        return _status_report(args)
    if args.command == "diff":
        return _diff_report(args)
    if args.command == "promote":
        return _promote_report(args)
    if args.command == "check":
        return _check_report(args)
    if args.command == "delete-canonical":
        return _delete_canonical_report(args)
    if args.command == "drop-cache":
        return _drop_cache_report(args)
    return CommandReport(
        "pcloud-archive",
        "error",
        "pcloud-archive command is required",
        {"help": parser.format_help()},
        schema_version=ARCHIVE_REPORT_SCHEMA_VERSION,
    )


def _cmd_help(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.ai is not None:
        topics = [getattr(args, "topic_arg", None) or "", *getattr(args, "topic", [])]
        print(_render_help_ai(parser, args.ai, [topic for topic in topics if topic]))
        return 0
    topic = getattr(args, "topic_arg", None)
    if topic:
        print(_topic_help(topic))
        return 0
    print(parser.format_help().rstrip())
    print()
    print("Examples:")
    print("  pcloud-archive doctor --profile nas-dev")
    print("  pcloud-archive diff --profile nas-dev")
    print("  pcloud-archive promote --profile nas-dev Photos/2026-07 --dry-run")
    print("  pcloud-archive check --profile nas-dev Photos/2026-07 --execute")
    print("  pcloud-archive help --ai \"inspect archive safety\" --topic safety")
    return 0


def _render_help_ai(parser: argparse.ArgumentParser, request: str, topics: list[str]) -> str:
    selected = topics or ["overview", "safety", "config", "workflow"]
    payload = {
        "schema_version": ARCHIVE_HELP_AI_SCHEMA_VERSION,
        "generated_at": _now(),
        "context_kind": "custom-cli-help-ai",
        "command_name": "pcloud-archive",
        "user_request": request,
        "generated_help": {
            "root": parser.format_help(),
            "subcommands": _subcommand_help(parser),
        },
        "topics": [_topic_payload(topic, include_name=True) for topic in selected],
        "important_paths": {
            "public_wrapper": "/Users/takafumi/p-core/bin/pcloud-archive",
            "implementation": "/Users/takafumi/p-core/dev/pcloud-tools/src/pcloud_tools/pcloud_archive.py",
            "default_config": "~/.config/pcloud-archive/config.toml",
            "default_state": "~/.local/state/pcloud-archive/<profile>",
        },
        "safety_rules": _safety_rules(),
        "non_goals": [
            "This command does not call an LLM.",
            "This command does not execute generated commands from help context.",
            "This command does not sync or delete NAS data automatically.",
            "This command does not write through a mounted crypt folder as a fallback.",
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)


def _subcommand_help(parser: argparse.ArgumentParser) -> dict[str, str]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return {name: sub.format_help() for name, sub in sorted(action.choices.items())}
    return {}


def _topic_help(topic: str) -> str:
    payload = _topic_payload(topic)
    lines = [f"pcloud-archive help topic: {topic}", ""]
    lines.extend(payload["summary"])
    if payload["commands"]:
        lines.append("")
        lines.append("Commands:")
        lines.extend(f"  {command}" for command in payload["commands"])
    if payload["safety"]:
        lines.append("")
        lines.append("Safety:")
        lines.extend(f"  - {rule}" for rule in payload["safety"])
    return "\n".join(lines)


def _topic_payload(topic: str, *, include_name: bool = False) -> dict[str, Any]:
    normalized = topic.lower()
    data = _TOPICS.get(
        normalized,
        {
            "summary": [f"Unknown topic: {topic}"],
            "commands": [],
            "safety": ["Do not infer missing behavior from an unknown topic."],
        },
    )
    return {"name": normalized, **data} if include_name else data


def _safety_rules() -> list[str]:
    return [
        "Use rclone copy/check against pcloud-crypt:, never cp into a crypt mount fallback.",
        "Refuse promote/check when source_root is missing.",
        "Refuse promote/check/delete when the pcloud-crypt remote is unavailable.",
        "Treat delete-canonical as the only canonical delete path and record tombstones.",
        "Block promotion of tombstoned local paths.",
    ]


_TOPICS: dict[str, dict[str, Any]] = {
    "overview": {
        "summary": [
            "pcloud-archive promotes NAS/Mac staging data into pcloud-crypt canonical storage.",
            "The transfer path is rclone remote-to-remote/local-to-remote, not mounted-folder cp.",
        ],
        "commands": ["pcloud-archive info", "pcloud-archive status", "pcloud-archive doctor"],
        "safety": ["Start with sandbox remote roots such as pcloud-crypt:_pcloud-archive-dev."],
    },
    "safety": {
        "summary": [
            "The command separates diff, promote, check, cache drop, and canonical delete.",
            "NAS deletion never propagates to pCloud.",
        ],
        "commands": ["pcloud-archive diff", "pcloud-archive promote --dry-run", "pcloud-archive check"],
        "safety": _safety_rules(),
    },
    "config": {
        "summary": ["Config is read from CLI flags, environment, config.toml, then safe defaults."],
        "commands": ["pcloud-archive info config", "pcloud-archive doctor"],
        "safety": ["Secrets are not needed for config; rclone owns pCloud credentials."],
    },
    "workflow": {
        "summary": ["Manual v1 workflow is diff -> promote dry-run -> promote execute -> check."],
        "commands": [
            "pcloud-archive diff --profile nas-dev",
            "pcloud-archive promote --profile nas-dev <path> --dry-run",
            "pcloud-archive promote --profile nas-dev <path> --execute",
            "pcloud-archive check --profile nas-dev <path> --execute",
        ],
        "safety": ["Launchd should only call the same CLI paths after manual verification."],
    },
}

def _config_file(args: argparse.Namespace) -> Path:
    value = getattr(args, "config", None) or os.environ.get("PCLOUD_ARCHIVE_CONFIG_FILE")
    if value:
        return Path(value).expanduser()
    return Path("~/.config/pcloud-archive/config.toml").expanduser()


def _load_profile(args: argparse.Namespace) -> tuple[ArchiveProfile, list[ConfigIssue]]:
    config_file = _config_file(args)
    issues: list[ConfigIssue] = []
    payload: dict[str, Any] = {}
    config_source = "defaults"
    if config_file.exists():
        try:
            payload = tomllib.loads(config_file.read_text())
            config_source = str(config_file)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            issues.append(ConfigIssue("PCLOUD_ARCHIVE_CONFIG", "error", f"cannot read config: {exc}"))
    else:
        issues.append(ConfigIssue("PCLOUD_ARCHIVE_CONFIG", "warning", f"config file is missing: {config_file}"))

    defaults = payload.get("defaults", {}) if isinstance(payload.get("defaults", {}), dict) else {}
    profile_name = (
        getattr(args, "profile", None)
        or os.environ.get("PCLOUD_ARCHIVE_PROFILE")
        or str(defaults.get("profile") or "nas-dev")
    )
    profiles = payload.get("profiles", {}) if isinstance(payload.get("profiles", {}), dict) else {}
    profile_payload = profiles.get(profile_name, {}) if isinstance(profiles.get(profile_name, {}), dict) else {}
    if payload and profile_name not in profiles:
        issues.append(ConfigIssue("PCLOUD_ARCHIVE_PROFILE", "error", f"profile is not defined: {profile_name}"))

    home = Path.home()
    default_state = home / ".local" / "state" / "pcloud-archive" / profile_name
    default_log = home / ".local" / "state" / "pcloud-archive" / "logs"
    transfer = profile_payload.get("transfer", {}) if isinstance(profile_payload.get("transfer", {}), dict) else {}
    ignore = profile_payload.get("ignore", {}) if isinstance(profile_payload.get("ignore", {}), dict) else {}

    source_root = _path_setting(
        args,
        "source_root",
        "PCLOUD_ARCHIVE_SOURCE_ROOT",
        profile_payload,
        "/Volumes/NAS/archive-inbox",
    )
    state_dir = _path_setting(args, "state_dir", "PCLOUD_ARCHIVE_STATE_DIR", profile_payload, str(default_state))
    log_dir = _path_setting(args, "log_dir", "PCLOUD_ARCHIVE_LOG_DIR", profile_payload, str(default_log))
    remote_root = _str_setting(
        args,
        "remote_root",
        "PCLOUD_ARCHIVE_REMOTE_ROOT",
        profile_payload,
        "pcloud-crypt:_pcloud-archive-dev",
    ).rstrip("/")
    rclone_bin = _str_setting(args, "rclone_bin", "PCLOUD_ARCHIVE_RCLONE_BIN", profile_payload, "rclone")
    ignore_patterns = _ignore_patterns(ignore)
    env_ignore = os.environ.get("PCLOUD_ARCHIVE_IGNORE_PATTERNS")
    if env_ignore:
        ignore_patterns = tuple(item.strip() for item in env_ignore.split(",") if item.strip())

    return (
        ArchiveProfile(
            name=profile_name,
            config_file=config_file,
            config_source=config_source,
            source_root=source_root,
            remote_root=remote_root,
            state_dir=state_dir,
            log_dir=log_dir,
            rclone_bin=rclone_bin,
            transfers=_int_setting("transfers", "PCLOUD_ARCHIVE_TRANSFERS", transfer, 3),
            checkers=_int_setting("checkers", "PCLOUD_ARCHIVE_CHECKERS", transfer, 8),
            bwlimit=os.environ.get("PCLOUD_ARCHIVE_BWLIMIT") or str(transfer.get("bwlimit") or "8M"),
            tpslimit=_int_setting("tpslimit", "PCLOUD_ARCHIVE_TPSLIMIT", transfer, 4),
            retries=_int_setting("retries", "PCLOUD_ARCHIVE_RETRIES", transfer, 5),
            low_level_retries=_int_setting("low_level_retries", "PCLOUD_ARCHIVE_LOW_LEVEL_RETRIES", transfer, 10),
            ignore_patterns=ignore_patterns,
        ),
        issues,
    )


def _path_setting(args: argparse.Namespace, key: str, env_key: str, profile: dict[str, Any], default: str) -> Path:
    del args
    return Path(os.environ.get(env_key) or str(profile.get(key) or default)).expanduser()


def _str_setting(args: argparse.Namespace, key: str, env_key: str, profile: dict[str, Any], default: str) -> str:
    del args
    return os.environ.get(env_key) or str(profile.get(key) or default)


def _int_setting(key: str, env_key: str, values: dict[str, Any], default: int) -> int:
    raw = os.environ.get(env_key) or values.get(key) or default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _ignore_patterns(ignore: dict[str, Any]) -> tuple[str, ...]:
    patterns = ignore.get("patterns")
    if isinstance(patterns, list):
        return tuple(str(pattern) for pattern in patterns)
    return (".DS_Store", "**/.DS_Store", "@eaDir/**", "**/@eaDir/**", "*.tmp", "*.part")


def _command_v(command: str) -> str | None:
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
        lines = result.stdout.strip().splitlines()
        if lines:
            return lines[0]
    return None


def _remote_available(profile: ArchiveProfile) -> tuple[bool, str]:
    result = _run_rclone(profile, ["lsd", _remote_base(profile.remote_root)], check=False)
    if result.returncode == 0:
        return True, "ok"
    detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
    return False, detail


def _remote_base(remote: str) -> str:
    if ":" not in remote:
        return remote
    return remote.split(":", 1)[0] + ":"


def _run_rclone(profile: ArchiveProfile, args: list[str], *, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [profile.rclone_bin, *args],
        check=check,
        capture_output=True,
        text=True,
    )


def _base_details(profile: ArchiveProfile) -> dict[str, Any]:
    return {
        "profile": profile.name,
        "config file": str(profile.config_file),
        "config source": profile.config_source,
        "source root": str(profile.source_root),
        "remote root": profile.remote_root,
        "state dir": str(profile.state_dir),
        "log dir": str(profile.log_dir),
        "rclone": profile.rclone_bin,
        "transfers": profile.transfers,
        "checkers": profile.checkers,
        "bwlimit": profile.bwlimit,
        "tpslimit": profile.tpslimit,
    }


def _info_report(args: argparse.Namespace) -> CommandReport:
    profile, issues = _load_profile(args)
    view = getattr(args, "view", "overview")
    details = _base_details(profile)
    if view == "paths":
        details = {
            "paths": [
                f"config file: {profile.config_file}",
                f"source root: {profile.source_root}",
                f"remote root: {profile.remote_root}",
                f"state dir: {profile.state_dir}",
                f"log dir: {profile.log_dir}",
                f"manifest: {profile.manifest_file}",
                f"tombstones: {profile.tombstone_file}",
                f"last run: {profile.last_run_file}",
            ]
        }
    elif view == "config":
        details["ignore patterns"] = list(profile.ignore_patterns)
        details["config resolution"] = "CLI flags > PCLOUD_ARCHIVE_* env > config.toml > built-in defaults"
    return CommandReport(
        f"info {view}" if view != "overview" else "info",
        status_from_issues(issues),
        "pcloud-archive info is ready",
        details,
        report_issues(sort_issues(issues)),
        schema_version=ARCHIVE_REPORT_SCHEMA_VERSION,
    )


def _doctor_report(args: argparse.Namespace) -> CommandReport:
    profile, issues = _load_profile(args)
    rclone_path = _command_v(profile.rclone_bin)
    if not rclone_path:
        issues.append(ConfigIssue("PCLOUD_ARCHIVE_RCLONE_BIN", "error", f"rclone command not found: {profile.rclone_bin}"))
    if not profile.source_root.exists():
        issues.append(ConfigIssue("PCLOUD_ARCHIVE_SOURCE_ROOT", "error", f"source root is missing: {profile.source_root}"))
    if rclone_path:
        remote_ok, remote_detail = _remote_available(profile)
        if not remote_ok:
            issues.append(ConfigIssue("PCLOUD_ARCHIVE_REMOTE_ROOT", "error", f"remote unavailable: {remote_detail}"))
    details = _base_details(profile)
    details.update(
        {
            "rclone path": rclone_path or "-",
            "source root status": "ok" if profile.source_root.exists() else "missing",
            "remote status": "checked" if rclone_path else "not checked",
            "state writes": "none",
        }
    )
    status = status_from_issues(issues)
    return CommandReport(
        "doctor",
        status,
        "pcloud-archive doctor passed" if status == "ok" else "pcloud-archive doctor found issues",
        details,
        report_issues(sort_issues(issues)),
        schema_version=ARCHIVE_REPORT_SCHEMA_VERSION,
    )


def _status_report(args: argparse.Namespace) -> CommandReport:
    profile, issues = _load_profile(args)
    manifest = _read_json(profile.manifest_file, {"records": {}})
    tombstones = _read_json(profile.tombstone_file, {"records": {}})
    last_run = _read_json(profile.last_run_file, {})
    details = _base_details(profile)
    details.update(
        {
            "manifest records": len(manifest.get("records", {})) if isinstance(manifest, dict) else 0,
            "tombstones": len(tombstones.get("records", {})) if isinstance(tombstones, dict) else 0,
            "last run": last_run.get("command", "-") if isinstance(last_run, dict) else "-",
            "last run status": last_run.get("status", "-") if isinstance(last_run, dict) else "-",
            "state writes": "none",
        }
    )
    return CommandReport(
        "status",
        status_from_issues(issues),
        "pcloud-archive status is ready",
        details,
        report_issues(sort_issues(issues)),
        schema_version=ARCHIVE_REPORT_SCHEMA_VERSION,
    )


def _diff_report(args: argparse.Namespace) -> CommandReport:
    profile, issues = _load_profile(args)
    if not profile.source_root.exists():
        issues.append(ConfigIssue("PCLOUD_ARCHIVE_SOURCE_ROOT", "error", f"source root is missing: {profile.source_root}"))
    local = {} if has_errors(issues) else _local_inventory(profile)
    remote: dict[str, dict[str, Any]] = {}
    if not has_errors(issues):
        remote, remote_issue = _remote_inventory(profile)
        if remote_issue:
            issues.append(remote_issue)
    tombstones = _read_json(profile.tombstone_file, {"records": {}}).get("records", {})
    classified = _classify(local, remote, tombstones if isinstance(tombstones, dict) else {})
    details = _base_details(profile)
    details.update(
        {
            "state writes": "none",
            "nas only": len(classified["NAS only"]),
            "same": len(classified["same"]),
            "different": len(classified["different"]),
            "pCloud only": len(classified["pCloud only"]),
            "tombstoned-local": len(classified["tombstoned-local"]),
            "samples": {key: values[:10] for key, values in classified.items() if values},
        }
    )
    return CommandReport(
        "diff",
        status_from_issues(issues),
        "pcloud-archive diff is ready" if not has_errors(issues) else "pcloud-archive diff cannot run",
        details,
        report_issues(sort_issues(issues)),
        schema_version=ARCHIVE_REPORT_SCHEMA_VERSION,
    )


def _promote_report(args: argparse.Namespace) -> CommandReport:
    profile, issues = _load_profile(args)
    relative, local_path, path_issue = _resolve_local_arg(profile, args.path)
    if path_issue:
        issues.append(path_issue)
    if relative and _is_tombstoned(profile, relative):
        issues.append(ConfigIssue("PCLOUD_ARCHIVE_TOMBSTONE", "error", f"path is tombstoned and cannot be promoted: {relative}"))
    remote_ok = False
    if not has_errors(issues):
        remote_ok, remote_detail = _remote_available(profile)
        if not remote_ok:
            issues.append(ConfigIssue("PCLOUD_ARCHIVE_REMOTE_ROOT", "error", f"remote unavailable: {remote_detail}"))
    bwlimit = getattr(args, "bwlimit", None) or profile.bwlimit
    command = _copy_command(profile, local_path, relative, bwlimit) if relative and local_path else []
    execute = bool(getattr(args, "execute", False))
    results: dict[str, Any] = {}
    if execute and not has_errors(issues):
        profile.log_dir.mkdir(parents=True, exist_ok=True)
        result = _run_rclone(profile, command[1:], check=False)
        results = _result_payload(result)
        _write_last_run(profile, "promote", "ok" if result.returncode == 0 else "error", {"path": relative, "result": results})
        if result.returncode != 0:
            issues.append(ConfigIssue("PCLOUD_ARCHIVE_PROMOTE", "error", f"rclone copy failed with exit {result.returncode}"))
    details = _base_details(profile)
    details.update(
        {
            "path": str(local_path) if local_path else "-",
            "relative path": relative or "-",
            "remote target": _remote_target(profile, relative, local_path) if relative and local_path else "-",
            "execute requested": "yes" if execute else "no",
            "planned command": shlex.join(command) if command else "-",
            "state writes": "last-run only" if execute and not has_errors(issues) else "none",
            "result": results or "-",
        }
    )
    return CommandReport(
        "promote",
        status_from_issues(issues),
        "pcloud-archive promote completed" if execute and not has_errors(issues) else "pcloud-archive promote preview is ready",
        details,
        report_issues(sort_issues(issues)),
        schema_version=ARCHIVE_REPORT_SCHEMA_VERSION,
    )


def _check_report(args: argparse.Namespace) -> CommandReport:
    profile, issues = _load_profile(args)
    relative, local_path, path_issue = _resolve_local_arg(profile, args.path)
    if path_issue:
        issues.append(path_issue)
    if not has_errors(issues):
        remote_ok, remote_detail = _remote_available(profile)
        if not remote_ok:
            issues.append(ConfigIssue("PCLOUD_ARCHIVE_REMOTE_ROOT", "error", f"remote unavailable: {remote_detail}"))
    command = _check_command(profile, local_path, relative) if relative and local_path else []
    execute = bool(getattr(args, "execute", False))
    results: dict[str, Any] = {}
    if execute and not has_errors(issues):
        result = _run_rclone(profile, command[1:], check=False)
        results = _result_payload(result)
        status = "ok" if result.returncode == 0 else "error"
        if result.returncode == 0:
            _record_manifest(profile, relative, local_path)
        _write_last_run(profile, "check", status, {"path": relative, "result": results})
        if result.returncode != 0:
            issues.append(ConfigIssue("PCLOUD_ARCHIVE_CHECK", "error", f"rclone check failed with exit {result.returncode}"))
    details = _base_details(profile)
    details.update(
        {
            "relative path": relative or "-",
            "remote target": _remote_target(profile, relative, local_path) if relative and local_path else "-",
            "execute requested": "yes" if execute else "no",
            "planned command": shlex.join(command) if command else "-",
            "state writes": "manifest and last-run" if execute and not has_errors(issues) else "none",
            "result": results or "-",
        }
    )
    return CommandReport(
        "check",
        status_from_issues(issues),
        "pcloud-archive check completed" if execute and not has_errors(issues) else "pcloud-archive check preview is ready",
        details,
        report_issues(sort_issues(issues)),
        schema_version=ARCHIVE_REPORT_SCHEMA_VERSION,
    )


def _delete_canonical_report(args: argparse.Namespace) -> CommandReport:
    profile, issues = _load_profile(args)
    remote_path = _remote_arg(profile, args.remote_path)
    relative = _relative_remote_arg(profile, args.remote_path)
    if not has_errors(issues):
        remote_ok, remote_detail = _remote_available(profile)
        if not remote_ok:
            issues.append(ConfigIssue("PCLOUD_ARCHIVE_REMOTE_ROOT", "error", f"remote unavailable: {remote_detail}"))
    command = [profile.rclone_bin, "deletefile", remote_path]
    execute = bool(getattr(args, "execute", False))
    results: dict[str, Any] = {}
    if execute and not has_errors(issues):
        result = _run_rclone(profile, command[1:], check=False)
        results = _result_payload(result)
        status = "ok" if result.returncode == 0 else "error"
        if result.returncode == 0:
            _record_tombstone(profile, relative, remote_path)
        _write_last_run(profile, "delete-canonical", status, {"path": relative, "result": results})
        if result.returncode != 0:
            issues.append(ConfigIssue("PCLOUD_ARCHIVE_DELETE_CANONICAL", "error", f"rclone deletefile failed with exit {result.returncode}"))
    details = _base_details(profile)
    details.update(
        {
            "remote path": remote_path,
            "relative path": relative,
            "execute requested": "yes" if execute else "no",
            "planned command": shlex.join(command),
            "state writes": "tombstone and last-run" if execute and not has_errors(issues) else "none",
            "result": results or "-",
        }
    )
    return CommandReport(
        "delete-canonical",
        status_from_issues(issues),
        "pcloud-archive canonical delete completed" if execute and not has_errors(issues) else "pcloud-archive canonical delete preview is ready",
        details,
        report_issues(sort_issues(issues)),
        schema_version=ARCHIVE_REPORT_SCHEMA_VERSION,
    )


def _drop_cache_report(args: argparse.Namespace) -> CommandReport:
    profile, issues = _load_profile(args)
    local_path = Path(args.local_path).expanduser()
    details = _base_details(profile)
    details.update(
        {
            "local path": str(local_path),
            "local path exists": "yes" if local_path.exists() else "no",
            "planned action": "preview local cache deletion only",
            "state writes": "none",
            "pCloud writes": "none",
        }
    )
    return CommandReport(
        "drop-cache",
        status_from_issues(issues),
        "pcloud-archive drop-cache is preview-only in v1",
        details,
        report_issues(sort_issues(issues)),
        schema_version=ARCHIVE_REPORT_SCHEMA_VERSION,
    )


def _resolve_local_arg(profile: ArchiveProfile, value: str) -> tuple[str | None, Path | None, ConfigIssue | None]:
    raw = Path(value).expanduser()
    local_path = raw if raw.is_absolute() else profile.source_root / raw
    try:
        resolved_source = profile.source_root.resolve()
        resolved_local = local_path.resolve()
    except OSError as exc:
        return None, local_path, ConfigIssue("PCLOUD_ARCHIVE_LOCAL_PATH", "error", f"cannot resolve local path: {exc}")
    if not profile.source_root.exists():
        return None, local_path, ConfigIssue("PCLOUD_ARCHIVE_SOURCE_ROOT", "error", f"source root is missing: {profile.source_root}")
    if not local_path.exists():
        return None, local_path, ConfigIssue("PCLOUD_ARCHIVE_LOCAL_PATH", "error", f"local path is missing: {local_path}")
    try:
        relative = resolved_local.relative_to(resolved_source).as_posix()
    except ValueError:
        return None, local_path, ConfigIssue("PCLOUD_ARCHIVE_LOCAL_PATH", "error", f"path is outside source root: {local_path}")
    if _ignored(relative, profile.ignore_patterns):
        return None, local_path, ConfigIssue("PCLOUD_ARCHIVE_IGNORE", "error", f"path is ignored by profile policy: {relative}")
    return relative or ".", local_path, None


def _copy_command(profile: ArchiveProfile, local_path: Path, relative: str, bwlimit: str) -> list[str]:
    command = [
        profile.rclone_bin,
        "copy",
        str(local_path),
        _remote_target(profile, relative, local_path),
        "--create-empty-src-dirs",
        "--transfers",
        str(profile.transfers),
        "--checkers",
        str(profile.checkers),
        "--tpslimit",
        str(profile.tpslimit),
        "--retries",
        str(profile.retries),
        "--low-level-retries",
        str(profile.low_level_retries),
    ]
    if bwlimit and bwlimit.lower() != "off":
        command.extend(["--bwlimit", bwlimit])
    return command


def _check_command(profile: ArchiveProfile, local_path: Path, relative: str) -> list[str]:
    return [
        profile.rclone_bin,
        "check",
        str(local_path),
        _remote_target(profile, relative, local_path),
        "--one-way",
        "--checkers",
        str(profile.checkers),
    ]


def _remote_target(profile: ArchiveProfile, relative: str, local_path: Path) -> str:
    rel = "" if relative == "." else relative.strip("/")
    if local_path.is_file():
        parent = str(Path(rel).parent).replace(".", "").strip("/")
        return f"{profile.remote_root}/{parent}".rstrip("/")
    return f"{profile.remote_root}/{rel}".rstrip("/")


def _remote_arg(profile: ArchiveProfile, value: str) -> str:
    if ":" in value:
        return value.rstrip("/")
    return f"{profile.remote_root}/{value.strip('/')}".rstrip("/")


def _relative_remote_arg(profile: ArchiveProfile, value: str) -> str:
    remote = _remote_arg(profile, value)
    prefix = f"{profile.remote_root}/"
    if remote == profile.remote_root:
        return "."
    if remote.startswith(prefix):
        return remote[len(prefix):]
    return value.strip("/")


def _local_inventory(profile: ArchiveProfile) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for path in profile.source_root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(profile.source_root).as_posix()
        if _ignored(rel, profile.ignore_patterns):
            continue
        stat = path.stat()
        records[rel] = {"size": stat.st_size, "mtime": int(stat.st_mtime)}
    return records


def _remote_inventory(profile: ArchiveProfile) -> tuple[dict[str, dict[str, Any]], ConfigIssue | None]:
    result = _run_rclone(profile, ["lsjson", "--recursive", profile.remote_root], check=False)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        lowered = detail.lower()
        if "not found" in lowered or "directory not found" in lowered or "object not found" in lowered:
            return {}, None
        return {}, ConfigIssue("PCLOUD_ARCHIVE_REMOTE_ROOT", "error", f"remote inventory failed: {detail}")
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        return {}, ConfigIssue("PCLOUD_ARCHIVE_REMOTE_JSON", "error", f"remote inventory was not JSON: {exc}")
    records: dict[str, dict[str, Any]] = {}
    if isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict) or item.get("IsDir"):
                continue
            path = str(item.get("Path") or item.get("Name") or "").strip("/")
            if not path:
                continue
            records[path] = {"size": int(item.get("Size") or 0)}
    return records, None


def _classify(
    local: dict[str, dict[str, Any]],
    remote: dict[str, dict[str, Any]],
    tombstones: dict[str, Any],
) -> dict[str, list[str]]:
    result = {"NAS only": [], "same": [], "different": [], "pCloud only": [], "tombstoned-local": []}
    for path, local_meta in sorted(local.items()):
        if path in tombstones:
            result["tombstoned-local"].append(path)
        elif path not in remote:
            result["NAS only"].append(path)
        elif int(local_meta.get("size") or -1) == int(remote[path].get("size") or -2):
            result["same"].append(path)
        else:
            result["different"].append(path)
    for path in sorted(set(remote) - set(local)):
        result["pCloud only"].append(path)
    return result


def _ignored(relative: str, patterns: tuple[str, ...]) -> bool:
    parts = relative.split("/")
    for pattern in patterns:
        if fnmatch.fnmatch(relative, pattern) or any(fnmatch.fnmatch(part, pattern) for part in parts):
            return True
    return False


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def _record_manifest(profile: ArchiveProfile, relative: str, local_path: Path) -> None:
    manifest = _read_json(profile.manifest_file, {"schema_version": "pcloud-archive-manifest.v1", "records": {}})
    if not isinstance(manifest, dict):
        manifest = {"schema_version": "pcloud-archive-manifest.v1", "records": {}}
    records = manifest.setdefault("records", {})
    stat = local_path.stat()
    records[relative] = {
        "checked_at": _now(),
        "source": str(local_path),
        "remote_root": profile.remote_root,
        "size": stat.st_size,
        "mtime": int(stat.st_mtime),
    }
    atomic_write_json(profile.manifest_file, manifest, sort_keys=True)


def _record_tombstone(profile: ArchiveProfile, relative: str, remote_path: str) -> None:
    tombstones = _read_json(profile.tombstone_file, {"schema_version": "pcloud-archive-tombstones.v1", "records": {}})
    if not isinstance(tombstones, dict):
        tombstones = {"schema_version": "pcloud-archive-tombstones.v1", "records": {}}
    records = tombstones.setdefault("records", {})
    records[relative] = {"deleted_at": _now(), "remote_path": remote_path}
    atomic_write_json(profile.tombstone_file, tombstones, sort_keys=True)


def _is_tombstoned(profile: ArchiveProfile, relative: str) -> bool:
    payload = _read_json(profile.tombstone_file, {"records": {}})
    records = payload.get("records", {}) if isinstance(payload, dict) else {}
    return isinstance(records, dict) and relative in records


def _write_last_run(profile: ArchiveProfile, command: str, status: str, detail: dict[str, Any]) -> None:
    payload = {"schema_version": "pcloud-archive-last-run.v1", "command": command, "status": status, "at": _now(), **detail}
    atomic_write_json(profile.last_run_file, payload, sort_keys=True)


def _result_payload(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return {
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    raise SystemExit(main())
