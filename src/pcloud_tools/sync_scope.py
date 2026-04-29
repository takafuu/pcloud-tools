from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import AppConfig, ConfigIssue


class AllowlistError(ValueError):
    """Raised when the allowlist file cannot be normalized."""


_UNSAFE_ROOT_ENTRIES = {
    "apps",
    "bin",
    "codex",
    "config",
    "dev",
    "dotfiles",
    "project",
    "tools",
}


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
        raise AllowlistError(f"sync allowlist not found: {allowlist_file}")
    if not allowlist_file.is_file():
        raise AllowlistError(f"sync allowlist is not a file: {allowlist_file}")

    entries: list[str] = []
    for raw_line in allowlist_file.read_text().splitlines():
        entry = raw_line.strip()
        if not entry or entry.startswith("#"):
            continue

        while entry.startswith("./"):
            entry = entry[2:]
        while entry.startswith("/"):
            entry = entry[1:]

        normalized = entry[:-1] if entry.endswith("/") else entry
        if not normalized:
            raise AllowlistError(f"invalid allowlist entry in {allowlist_file}: {raw_line}")

        for part in normalized.split("/"):
            if part in {".", ".."}:
                raise AllowlistError(f"invalid allowlist entry in {allowlist_file}: {raw_line}")

        if entry.endswith("/"):
            normalized = f"{normalized}/"

        entries.append(normalized)

    if not entries:
        raise AllowlistError(f"sync allowlist is empty: {allowlist_file}")
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


def prepare_sync_filter_rules(config: AppConfig, entries: tuple[str, ...]) -> tuple[str, ...]:
    rules: list[str] = []
    seen: set[str] = set()

    for rule in default_exclude_rules(config):
        if rule not in seen:
            seen.add(rule)
            rules.append(rule)

    for entry in entries:
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


def unsafe_allowlist_entries(entries: tuple[str, ...]) -> tuple[str, ...]:
    unsafe: list[str] = []
    for entry in entries:
        root = entry.strip("/").split("/", 1)[0]
        if root in _UNSAFE_ROOT_ENTRIES:
            unsafe.append(entry)
    return tuple(unsafe)


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

    if info.allowlist_status == "loaded":
        unsafe_entries = unsafe_allowlist_entries(info.entries)
        if unsafe_entries:
            issues.append(
                ConfigIssue(
                    key="PCLOUD_TOOLS_SCOPE_POLICY",
                    level="warning",
                    message=(
                        "allowlist includes source/tool roots outside document-only sync policy: "
                        + ", ".join(unsafe_entries)
                    ),
                )
            )
    return issues
