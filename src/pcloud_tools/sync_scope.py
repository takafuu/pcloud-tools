from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import AppConfig, ConfigIssue
from .manager_ignore import load_manager_ignore_rules


class AllowlistError(ValueError):
    """Raised when the sync scope file cannot be normalized."""


@dataclass(frozen=True)
class ScopeBaseline:
    mode: str
    status: str
    file: Path


@dataclass(frozen=True)
class SyncScopeInfo:
    allowlist_file: Path
    allowlist_status: str
    allowlist_count: int
    allowlist_message: str
    entries: tuple[str, ...]
    baseline: ScopeBaseline
    filter_file: Path


def sync_scope_mode_file(config: AppConfig) -> Path:
    return config.state_dir / "last-resync-scope"


def sync_filter_file(config: AppConfig) -> Path:
    return config.state_dir / "bisync.filter"


def _normalize_filter_path(value: object) -> str:
    path = str(value or "").strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    while path.startswith("/"):
        path = path[1:]
    parts = [part for part in path.split("/") if part]
    if any(part in {".", ".."} for part in parts):
        return ""
    return "/".join(parts)


def sync_scope_baseline_info(config: AppConfig) -> ScopeBaseline:
    scope_file = sync_scope_mode_file(config)
    if not scope_file.exists():
        return ScopeBaseline(mode="allowlist", status="defaulted", file=scope_file)

    stored_mode = scope_file.read_text().strip()
    if stored_mode in {"allowlist", "full"}:
        return ScopeBaseline(mode=stored_mode, status="stored", file=scope_file)

    return ScopeBaseline(mode="-", status="invalid", file=scope_file)


def normalize_allowlist_entries(allowlist_file: Path) -> tuple[str, ...]:
    if not allowlist_file.exists():
        raise AllowlistError(f"sync scope file not found: {allowlist_file}")
    if not allowlist_file.is_file():
        raise AllowlistError(f"sync scope path is not a file: {allowlist_file}")

    entries: list[str] = []
    for raw_line in allowlist_file.read_text().splitlines():
        entry = raw_line.strip()
        if not entry or entry.startswith("#"):
            continue

        if entry in {".", "/", "./"}:
            entries.append("/")
            continue

        while entry.startswith("./"):
            entry = entry[2:]
        while entry.startswith("/"):
            entry = entry[1:]

        normalized = entry[:-1] if entry.endswith("/") else entry
        if not normalized:
            raise AllowlistError(f"invalid sync scope entry in {allowlist_file}: {raw_line}")

        for part in normalized.split("/"):
            if part in {".", ".."}:
                raise AllowlistError(f"invalid sync scope entry in {allowlist_file}: {raw_line}")

        if entry.endswith("/"):
            normalized = f"{normalized}/"

        entries.append(normalized)

    if not entries:
        raise AllowlistError(f"sync scope file is empty: {allowlist_file}")
    return tuple(entries)


def sync_allowlist_info(config: AppConfig) -> SyncScopeInfo:
    allowlist_file = config.allowlist_file
    baseline = sync_scope_baseline_info(config)
    filter_file = sync_filter_file(config)

    try:
        entries = normalize_allowlist_entries(allowlist_file)
    except AllowlistError as exc:
        status = "missing" if not allowlist_file.exists() else "invalid"
        return SyncScopeInfo(
            allowlist_file=allowlist_file,
            allowlist_status=status,
            allowlist_count=0,
            allowlist_message=str(exc),
            entries=(),
            baseline=baseline,
            filter_file=filter_file,
        )

    return SyncScopeInfo(
        allowlist_file=allowlist_file,
        allowlist_status="loaded",
        allowlist_count=len(entries),
        allowlist_message="-",
        entries=entries,
        baseline=baseline,
        filter_file=filter_file,
    )


def default_exclude_rules(config: AppConfig) -> tuple[str, ...]:
    rules: list[str] = []
    for pattern in config.default_excludes:
        cleaned = pattern.strip().lstrip("/")
        if cleaned:
            rules.append(f"- /{cleaned}")
    return tuple(rules)


def hard_safety_filter_rules(config: AppConfig) -> tuple[str, ...]:
    root = str(config.remote_trash_root or "").strip().rstrip("/")
    remote = str(config.core_remote or "").strip().rstrip("/")
    relative = ".pcloud-manager-trash"
    if remote and root.startswith(f"{remote}/"):
        relative = _normalize_filter_path(root[len(remote) + 1:]) or relative
    return (f"- /{relative}/**", f"- /**/{relative}/**")


def _filter_rules_for_manager_pattern(pattern: str, *, allow: bool) -> tuple[str, ...]:
    clean = pattern.strip().lstrip("/")
    if not clean:
        return ()

    prefix = "+" if allow else "-"
    if clean.endswith("/**"):
        body = clean[:-3].rstrip("/")
        if not body:
            return ()
        if "/" in body or body.startswith("**/"):
            return (f"{prefix} /{body}/**",)
        return (f"{prefix} /{body}/**", f"{prefix} /**/{body}/**")
    if clean.endswith("/"):
        body = clean.rstrip("/")
        if not body:
            return ()
        if "/" in body or body.startswith("**/"):
            return (f"{prefix} /{body}/**",)
        return (f"{prefix} /{body}/**", f"{prefix} /**/{body}/**")
    if clean.startswith("**/"):
        return (f"{prefix} /{clean}",)
    if "/" in clean:
        return (f"{prefix} /{clean}",)
    return (f"{prefix} /{clean}", f"{prefix} /**/{clean}")


def manager_ignore_filter_rules(config: AppConfig) -> tuple[str, ...]:
    allow_rules: list[str] = []
    deny_rules: list[str] = []
    seen: set[str] = set()
    for rule in load_manager_ignore_rules(config):
        target = allow_rules if rule.allow else deny_rules
        for filter_rule in _filter_rules_for_manager_pattern(rule.pattern, allow=rule.allow):
            if filter_rule not in seen:
                seen.add(filter_rule)
                target.append(filter_rule)
    return tuple([*allow_rules, *deny_rules])


def prepare_sync_filter_rules(config: AppConfig, entries: tuple[str, ...]) -> tuple[str, ...]:
    rules: list[str] = []
    seen: set[str] = set()

    for rule in (*hard_safety_filter_rules(config), *manager_ignore_filter_rules(config), *default_exclude_rules(config)):
        if rule not in seen:
            seen.add(rule)
            rules.append(rule)

    for entry in entries:
        if entry == "/":
            root_rule = "+ /**"
            if root_rule not in seen:
                seen.add(root_rule)
                rules.append(root_rule)
            continue
        clean_entry = entry[:-1] if entry.endswith("/") else entry
        is_dir = entry.endswith("/")
        parts = clean_entry.split("/")

        parent_limit = len(parts) if is_dir else len(parts) - 1
        parent = ""
        for idx in range(parent_limit):
            parent = f"{parent}/{parts[idx]}".strip("/")
            rule = f"+ /{parent}/"
            if rule not in seen:
                seen.add(rule)
                rules.append(rule)

        final_rule = f"+ /{clean_entry}/**" if is_dir else f"+ /{clean_entry}"
        if final_rule not in seen:
            seen.add(final_rule)
            rules.append(final_rule)

    final_deny = "- /**"
    if final_deny not in seen:
        rules.append(final_deny)
    return tuple(rules)


def write_sync_filter_file(config: AppConfig, entries: tuple[str, ...]) -> Path:
    filter_file = sync_filter_file(config)
    filter_file.parent.mkdir(parents=True, exist_ok=True)
    rules = prepare_sync_filter_rules(config, entries)
    filter_file.write_text("".join(f"{rule}\n" for rule in rules))
    return filter_file


def scope_issues(info: SyncScopeInfo) -> list[ConfigIssue]:
    issues: list[ConfigIssue] = []
    if info.allowlist_status == "missing":
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_ALLOWLIST_FILE",
                level="error",
                message=info.allowlist_message,
            )
        )
    elif info.allowlist_status == "invalid":
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_ALLOWLIST_FILE",
                level="error",
                message=info.allowlist_message,
            )
        )

    if info.baseline.status == "invalid":
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_SCOPE_BASELINE",
                level="error",
                message=f"stored sync scope is invalid: {info.baseline.file}",
            )
        )
    return issues
