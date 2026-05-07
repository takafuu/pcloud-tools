from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path

from .config import AppConfig, render_manager_ignore_template


@dataclass(frozen=True)
class ManagerIgnoreRule:
    pattern: str
    allow: bool


@dataclass(frozen=True)
class ManagerIgnoreMatch:
    ignored: bool
    pattern: str
    reason: str


def _rule_lines(config: AppConfig) -> tuple[str, ...]:
    if config.manager_ignore_file.exists() and config.manager_ignore_file.is_file():
        return tuple(config.manager_ignore_file.read_text().splitlines())
    return tuple(render_manager_ignore_template().splitlines())


def load_manager_ignore_rules(config: AppConfig) -> tuple[ManagerIgnoreRule, ...]:
    rules: list[ManagerIgnoreRule] = []
    for raw_line in _rule_lines(config):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        allow = line.startswith("!")
        pattern = line[1:].strip() if allow else line
        pattern = pattern.lstrip("/")
        if pattern:
            rules.append(ManagerIgnoreRule(pattern=pattern, allow=allow))
    return tuple(rules)


def _matches_pattern(path: str, pattern: str) -> bool:
    clean_path = path.strip("/")
    clean_pattern = pattern.strip().lstrip("/")
    if not clean_path or not clean_pattern:
        return False

    if clean_pattern.endswith("/**"):
        prefix = clean_pattern[:-3].rstrip("/")
        return clean_path == prefix or clean_path.startswith(f"{prefix}/")
    if clean_pattern.endswith("/"):
        prefix = clean_pattern.rstrip("/")
        return clean_path == prefix or clean_path.startswith(f"{prefix}/")

    if fnmatch.fnmatch(clean_path, clean_pattern) or fnmatch.fnmatch(f"/{clean_path}", f"/{clean_pattern}"):
        return True
    if "/" not in clean_pattern and fnmatch.fnmatch(Path(clean_path).name, clean_pattern):
        return True
    return False


def manager_ignore_match(config: AppConfig, path: str) -> ManagerIgnoreMatch | None:
    matched: ManagerIgnoreRule | None = None
    for rule in load_manager_ignore_rules(config):
        if _matches_pattern(path, rule.pattern):
            matched = rule
    if matched is None:
        return None
    if matched.allow:
        return ManagerIgnoreMatch(ignored=False, pattern=matched.pattern, reason="manager ignore exception")
    return ManagerIgnoreMatch(ignored=True, pattern=matched.pattern, reason="manager ignore rule")
