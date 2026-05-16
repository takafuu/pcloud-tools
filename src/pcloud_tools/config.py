from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .runtime import RuntimePaths

_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")
_VALID_ENGINES = {"webdav", "nfs"}


class ConfigError(ValueError):
    """Raised when a config value cannot be parsed."""


@dataclass(frozen=True)
class ConfigIssue:
    key: str
    level: str
    message: str


@dataclass(frozen=True)
class ConfigLoadResult:
    config: "AppConfig"
    source: str
    issues: tuple[ConfigIssue, ...]


@dataclass(frozen=True)
class AppConfig:
    env_file: Path
    core_dir: Path
    remote: str
    core_remote: str
    vault_remote: str
    crypt_remote: str
    vault_dir: Path
    vault_mount_dir: Path
    crypt_dir: Path
    crypt_mount_dir: Path
    enable_vault_layer: bool
    enable_crypt_layer: bool
    vault_engine: str
    crypt_engine: str
    vault_port: int
    crypt_port: int
    state_dir: Path
    log_dir: Path
    allowlist_file: Path
    manager_ignore_file: Path
    default_excludes: tuple[str, ...]
    autosync_label: str
    autosync_plist: Path
    indexer_bin: Path
    notify_bin: Path
    chat_notify_enabled: bool
    chat_notify_cmd: str
    rclone_bin: str
    transfer_execution_gate: str
    pushd_fswatch_resident_gate: str
    diffd_api_long_poll_gate: str
    autosync_launchd_gate: str
    sync_migration_gate: str
    transfer_exec_timeout_seconds: int
    download_suppression_ttl_seconds: int
    pushd_debounce_seconds: int
    pushd_queue_limit: int
    diffd_poll_interval_seconds: int
    diffd_batch_limit: int
    pcloud_api_base_url: str
    pcloud_api_auth_param: str
    pcloud_api_token: str
    pcloud_api_timeout_seconds: int


ConfigValueKind = Literal["path", "str", "int", "bool", "csv"]


@dataclass(frozen=True)
class FieldSpec:
    """Configuration field metadata.

    Default templates are expanded by _defaults_for_runtime with home,
    workspace_root, base_state_dir, base_log_dir, and env_file. Literal braces
    that must survive expansion should be escaped as {{...}}, for example
    chat_notify_cmd's {{message}} placeholder.
    """

    name: str
    env_var: str
    kind: ConfigValueKind
    default: str
    dev_default: str | None = None


CONFIG_FIELD_SPECS: tuple[FieldSpec, ...] = (
    FieldSpec("env_file", "", "path", "{env_file}"),
    FieldSpec("core_dir", "PCLOUD_TOOLS_CORE_DIR", "path", "{home}/p-core", "{workspace_root}"),
    FieldSpec("remote", "PCLOUD_TOOLS_REMOTE", "str", "pcloud:"),
    FieldSpec("core_remote", "PCLOUD_TOOLS_CORE_REMOTE", "str", "pcloud:core"),
    FieldSpec("vault_remote", "PCLOUD_TOOLS_VAULT_REMOTE", "str", "pcloud:vault"),
    FieldSpec("crypt_remote", "PCLOUD_TOOLS_CRYPT_REMOTE", "str", "pcloud-crypt:"),
    FieldSpec("vault_dir", "PCLOUD_TOOLS_VAULT_DIR", "path", "{home}/p-vault", "{workspace_root}/.dev-state/links/vault"),
    FieldSpec("vault_mount_dir", "PCLOUD_TOOLS_VAULT_MOUNT_DIR", "path", "{base_state_dir}/vault"),
    FieldSpec("crypt_dir", "PCLOUD_TOOLS_CRYPT_DIR", "path", "{home}/p-crypt", "{workspace_root}/.dev-state/links/crypt"),
    FieldSpec("crypt_mount_dir", "PCLOUD_TOOLS_CRYPT_MOUNT_DIR", "path", "{base_state_dir}/crypt"),
    FieldSpec("enable_vault_layer", "PCLOUD_TOOLS_ENABLE_VAULT_LAYER", "bool", "1"),
    FieldSpec("enable_crypt_layer", "PCLOUD_TOOLS_ENABLE_CRYPT_LAYER", "bool", "1"),
    FieldSpec("vault_engine", "PCLOUD_TOOLS_VAULT_ENGINE", "str", "webdav"),
    FieldSpec("crypt_engine", "PCLOUD_TOOLS_CRYPT_ENGINE", "str", "webdav"),
    FieldSpec("vault_port", "PCLOUD_TOOLS_VAULT_PORT", "int", "5566"),
    FieldSpec("crypt_port", "PCLOUD_TOOLS_CRYPT_PORT", "int", "5567"),
    FieldSpec("state_dir", "PCLOUD_TOOLS_STATE_DIR", "path", "{base_state_dir}"),
    FieldSpec("log_dir", "PCLOUD_TOOLS_LOG_DIR", "path", "{base_log_dir}"),
    FieldSpec("allowlist_file", "PCLOUD_TOOLS_ALLOWLIST_FILE", "path", "{home}/p-core/.pcloud-sync-allowlist", "{workspace_root}/.pcloud-sync-allowlist"),
    FieldSpec("manager_ignore_file", "PCLOUD_TOOLS_MANAGER_IGNORE_FILE", "path", "{home}/p-core/.pcloudmanagerignore", "{workspace_root}/.pcloudmanagerignore"),
    FieldSpec("default_excludes", "PCLOUD_TOOLS_DEFAULT_EXCLUDES", "csv", "config/dotfiles/.ssh/agent/**,.DS_Store,**/.DS_Store"),
    FieldSpec("autosync_label", "PCLOUD_TOOLS_AUTOSYNC_LABEL", "str", "com.takafumi.pcloud-bisync", "com.example.pcloud-bisync.dev"),
    FieldSpec("autosync_plist", "PCLOUD_TOOLS_AUTOSYNC_PLIST", "path", "{home}/Library/LaunchAgents/com.takafumi.pcloud-bisync.plist", "{workspace_root}/.dev-state/com.example.pcloud-bisync.dev.plist"),
    FieldSpec("indexer_bin", "PCLOUD_TOOLS_INDEXER_BIN", "path", "{home}/.zsh/functions/pcloud-indexer.py", "{workspace_root}/scripts/pcloud-indexer.py"),
    FieldSpec("notify_bin", "PCLOUD_TOOLS_NOTIFY_BIN", "path", "{home}/bin/notify", "{workspace_root}/scripts/notify"),
    FieldSpec("chat_notify_enabled", "PCLOUD_TOOLS_CHAT_NOTIFY_ENABLED", "bool", "0"),
    FieldSpec("chat_notify_cmd", "PCLOUD_TOOLS_CHAT_NOTIFY_CMD", "str", "{home}/bin/notify send --to discord {{message}}", "{workspace_root}/scripts/notify send --to discord {{message}}"),
    FieldSpec("rclone_bin", "PCLOUD_TOOLS_RCLONE_BIN", "str", "rclone"),
    FieldSpec("transfer_execution_gate", "PCLOUD_TOOLS_TRANSFER_EXECUTION_GATE", "str", ""),
    FieldSpec("pushd_fswatch_resident_gate", "PCLOUD_TOOLS_PUSHD_FSWATCH_RESIDENT_GATE", "str", ""),
    FieldSpec("diffd_api_long_poll_gate", "PCLOUD_TOOLS_DIFFD_API_LONG_POLL_GATE", "str", ""),
    FieldSpec("autosync_launchd_gate", "PCLOUD_TOOLS_AUTOSYNC_LAUNCHD_GATE", "str", ""),
    FieldSpec("sync_migration_gate", "PCLOUD_TOOLS_SYNC_MIGRATION_GATE", "str", ""),
    FieldSpec("transfer_exec_timeout_seconds", "PCLOUD_TOOLS_TRANSFER_EXEC_TIMEOUT_SECONDS", "int", "5"),
    FieldSpec("download_suppression_ttl_seconds", "PCLOUD_TOOLS_DOWNLOAD_SUPPRESSION_TTL_SECONDS", "int", "86400"),
    FieldSpec("pushd_debounce_seconds", "PCLOUD_TOOLS_PUSHD_DEBOUNCE_SECONDS", "int", "3"),
    FieldSpec("pushd_queue_limit", "PCLOUD_TOOLS_PUSHD_QUEUE_LIMIT", "int", "1000"),
    FieldSpec("diffd_poll_interval_seconds", "PCLOUD_TOOLS_DIFFD_POLL_INTERVAL_SECONDS", "int", "60"),
    FieldSpec("diffd_batch_limit", "PCLOUD_TOOLS_DIFFD_BATCH_LIMIT", "int", "100"),
    FieldSpec("pcloud_api_base_url", "PCLOUD_TOOLS_PCLOUD_API_BASE_URL", "str", "https://api.pcloud.com"),
    FieldSpec("pcloud_api_auth_param", "PCLOUD_TOOLS_PCLOUD_API_AUTH_PARAM", "str", "auth"),
    FieldSpec("pcloud_api_token", "PCLOUD_TOOLS_PCLOUD_API_TOKEN", "str", ""),
    FieldSpec("pcloud_api_timeout_seconds", "PCLOUD_TOOLS_PCLOUD_API_TIMEOUT_SECONDS", "int", "30"),
)


def _expand_value(value: str, mapping: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        return mapping.get(key, os.environ.get(key, ""))

    expanded = _VAR_PATTERN.sub(replace, value)
    return os.path.expanduser(expanded)


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for lineno, raw_line in enumerate(path.read_text().splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigError(f"{path}:{lineno}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise ConfigError(f"{path}:{lineno}: empty key")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def _bool_from_value(key: str, value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "off", "disabled"}:
        return False
    raise ConfigError(f"{key}: expected boolean, got {value!r}")


def _int_from_value(key: str, value: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"{key}: expected integer, got {value!r}") from exc


def _path_value(key: str, values: dict[str, str], default: str) -> Path:
    raw = values.get(key, default)
    return Path(_expand_value(raw, values))


def _string_value(key: str, values: dict[str, str], default: str) -> str:
    raw = values.get(key, default)
    return _expand_value(raw, values)


def _csv_value(key: str, values: dict[str, str], default: str) -> tuple[str, ...]:
    raw = _string_value(key, values, default)
    items = [item.strip() for item in raw.split(",")]
    return tuple(item for item in items if item)


def _defaults_for_runtime(paths: RuntimePaths) -> dict[str, str]:
    home = Path.home()
    base_state_dir = paths.state_dir if paths.dev_mode else home / ".pcloud"
    base_log_dir = paths.log_dir if paths.dev_mode else base_state_dir / "logs"

    tokens = {
        "home": str(home),
        "workspace_root": str(paths.workspace_root),
        "base_state_dir": str(base_state_dir),
        "base_log_dir": str(base_log_dir),
        "env_file": str(paths.env_file),
    }
    return {
        spec.env_var: (spec.dev_default if paths.dev_mode and spec.dev_default is not None else spec.default).format(**tokens)
        for spec in CONFIG_FIELD_SPECS
        if spec.env_var
    }


def _value_for_spec(
    spec: FieldSpec,
    *,
    paths: RuntimePaths,
    values: dict[str, str],
    defaults: dict[str, str],
) -> object:
    if not spec.env_var:
        if spec.name == "env_file":
            return paths.env_file
        raise ConfigError(f"{spec.name}: config field has no env var")
    if spec.kind == "path":
        return _path_value(spec.env_var, values, defaults[spec.env_var])
    if spec.kind == "str":
        return _string_value(spec.env_var, values, defaults[spec.env_var])
    if spec.kind == "int":
        return _int_from_value(spec.env_var, values[spec.env_var])
    if spec.kind == "bool":
        return _bool_from_value(spec.env_var, values[spec.env_var])
    if spec.kind == "csv":
        return _csv_value(spec.env_var, values, defaults[spec.env_var])
    raise ConfigError(f"{spec.env_var}: unsupported config kind {spec.kind!r}")


def _build_config_from_values(paths: RuntimePaths, values: dict[str, str], defaults: dict[str, str]) -> AppConfig:
    parsed = {
        spec.name: _value_for_spec(spec, paths=paths, values=values, defaults=defaults)
        for spec in CONFIG_FIELD_SPECS
    }
    return AppConfig(**parsed)


def load_config(paths: RuntimePaths) -> ConfigLoadResult:
    defaults = _defaults_for_runtime(paths)
    issues: list[ConfigIssue] = []
    env_values: dict[str, str] = {}

    try:
        env_values = parse_env_file(paths.env_file)
        values = {**defaults, **env_values}

        for key, value in os.environ.items():
            if key.startswith("PCLOUD_TOOLS_"):
                values[key] = value

        config = _build_config_from_values(paths, values, defaults)
    except ConfigError as exc:
        issues.append(ConfigIssue(key="config", level="error", message=str(exc)))
        fallback = _build_fallback_config(paths, defaults)
        source = "env-error" if paths.env_file.exists() else "defaults"
        return ConfigLoadResult(config=fallback, source=source, issues=tuple(issues))

    issues.extend(validate_config(config))
    source = "env" if paths.env_file.exists() else "defaults"
    return ConfigLoadResult(config=config, source=source, issues=tuple(issues))


def _build_fallback_config(paths: RuntimePaths, defaults: dict[str, str]) -> AppConfig:
    return _build_config_from_values(paths, defaults, defaults)


def validate_config(config: AppConfig) -> list[ConfigIssue]:
    issues: list[ConfigIssue] = []

    if not config.env_file.exists():
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_ENV_FILE",
                level="warning",
                message=f"env file is missing: {config.env_file}",
            )
        )

    if config.vault_engine not in _VALID_ENGINES:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_VAULT_ENGINE",
                level="error",
                message=f"unsupported engine: {config.vault_engine}",
            )
        )
    if config.crypt_engine not in _VALID_ENGINES:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_CRYPT_ENGINE",
                level="error",
                message=f"unsupported engine: {config.crypt_engine}",
            )
        )

    for key, port in (
        ("PCLOUD_TOOLS_VAULT_PORT", config.vault_port),
        ("PCLOUD_TOOLS_CRYPT_PORT", config.crypt_port),
    ):
        if port < 1 or port > 65535:
            issues.append(
                ConfigIssue(key=key, level="error", message=f"port out of range: {port}")
            )

    for key, value in (
        ("PCLOUD_TOOLS_TRANSFER_EXEC_TIMEOUT_SECONDS", config.transfer_exec_timeout_seconds),
        ("PCLOUD_TOOLS_DOWNLOAD_SUPPRESSION_TTL_SECONDS", config.download_suppression_ttl_seconds),
        ("PCLOUD_TOOLS_PUSHD_DEBOUNCE_SECONDS", config.pushd_debounce_seconds),
        ("PCLOUD_TOOLS_DIFFD_POLL_INTERVAL_SECONDS", config.diffd_poll_interval_seconds),
        ("PCLOUD_TOOLS_PCLOUD_API_TIMEOUT_SECONDS", config.pcloud_api_timeout_seconds),
    ):
        if value < 1:
            issues.append(ConfigIssue(key=key, level="error", message=f"value must be >= 1: {value}"))

    for key, value in (
        ("PCLOUD_TOOLS_PUSHD_QUEUE_LIMIT", config.pushd_queue_limit),
        ("PCLOUD_TOOLS_DIFFD_BATCH_LIMIT", config.diffd_batch_limit),
    ):
        if value < 1:
            issues.append(ConfigIssue(key=key, level="error", message=f"value must be >= 1: {value}"))

    if not config.remote.endswith(":"):
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_REMOTE",
                level="error",
                message="remote must end with ':'",
            )
        )
    if ":" not in config.core_remote:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_CORE_REMOTE",
                level="error",
                message="core remote must contain ':'",
            )
        )
    if not (config.pcloud_api_base_url.startswith("https://") or config.pcloud_api_base_url.startswith("http://")):
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_PCLOUD_API_BASE_URL",
                level="error",
                message="pCloud API base URL must start with https:// or http://",
            )
        )
    if config.pcloud_api_auth_param not in {"auth", "access_token"}:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_PCLOUD_API_AUTH_PARAM",
                level="error",
                message="pCloud API auth parameter must be auth or access_token",
            )
        )

    if config.chat_notify_enabled and "{message}" not in config.chat_notify_cmd:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_CHAT_NOTIFY_CMD",
                level="error",
                message="chat notify command must include {message} placeholder when enabled",
            )
        )

    if not config.core_dir.exists():
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_CORE_DIR",
                level="error",
                message=f"core dir does not exist: {config.core_dir}",
            )
        )
    elif not config.core_dir.is_dir():
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_CORE_DIR",
                level="error",
                message=f"core dir is not a directory: {config.core_dir}",
            )
        )

    if not config.state_dir.exists():
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_STATE_DIR",
                level="warning",
                message=f"state dir does not exist yet: {config.state_dir}",
            )
        )
    elif not config.state_dir.is_dir():
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_STATE_DIR",
                level="error",
                message=f"state dir is not a directory: {config.state_dir}",
            )
        )

    if not config.log_dir.exists():
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_LOG_DIR",
                level="warning",
                message=f"log dir does not exist yet: {config.log_dir}",
            )
        )
    elif not config.log_dir.is_dir():
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_LOG_DIR",
                level="error",
                message=f"log dir is not a directory: {config.log_dir}",
            )
        )

    if not config.allowlist_file.exists():
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_ALLOWLIST_FILE",
                level="error",
                message=f"allowlist file does not exist: {config.allowlist_file}",
            )
        )
    elif not config.allowlist_file.is_file():
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_ALLOWLIST_FILE",
                level="error",
                message=f"allowlist path is not a file: {config.allowlist_file}",
            )
        )
    if config.manager_ignore_file.exists() and not config.manager_ignore_file.is_file():
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_MANAGER_IGNORE_FILE",
                level="error",
                message=f"manager ignore path is not a file: {config.manager_ignore_file}",
            )
        )

    return issues


def render_env_template(paths: RuntimePaths) -> str:
    defaults = _defaults_for_runtime(paths)
    lines = [
        "# pcloud-tools runtime configuration",
        "# Generated by `pcloud-manager-dev doctor --repair`.",
        "",
    ]

    ordered_keys = [spec.env_var for spec in CONFIG_FIELD_SPECS if spec.env_var]
    for key in ordered_keys:
        lines.append(f"{key}={defaults[key]}")
    lines.append("")
    return "\n".join(lines)


def repair_env_file(paths: RuntimePaths) -> Path:
    paths.ensure_directories()
    env_file = paths.env_file
    if env_file.exists():
        return env_file
    env_file.write_text(render_env_template(paths))
    return env_file


def render_allowlist_template(paths: RuntimePaths) -> str:
    if paths.dev_mode:
        return (
            "# Starter allowlist for pcloud-tools development.\n"
            "# Keep this path separate from /Users/takafumi/p-core/Documents.\n"
            "dev-fixtures/Documents/\n"
        )
    return "# Starter allowlist for pcloud-tools\nDocuments/\n"


def repair_allowlist_file(config: AppConfig, paths: RuntimePaths) -> Path:
    allowlist_file = config.allowlist_file
    allowlist_file.parent.mkdir(parents=True, exist_ok=True)
    if allowlist_file.exists():
        return allowlist_file
    allowlist_file.write_text(render_allowlist_template(paths))
    return allowlist_file


def render_manager_ignore_template() -> str:
    return (
        "# pcloud-manager ignore rules.\n"
        "# Syntax is gitignore-like. Lines beginning with ! are exception allow rules.\n"
        "# Example: .env is ignored, but !.env.sample allows .env.sample to sync.\n"
        "\n"
        "# macOS and hidden temporary files\n"
        ".DS_Store\n"
        "**/.DS_Store\n"
        "._*\n"
        "**/._*\n"
        ".*\n"
        "**/.*\n"
        "\n"
        "# Secrets/state stay local by default\n"
        ".env\n"
        "**/.env\n"
        "\n"
        "# Transfer and editor temporary files\n"
        "*.download\n"
        "*.tmp\n"
        "*.part\n"
        "*.swp\n"
        "\n"
        "# Hidden/system directories\n"
        "**/.Trashes/**\n"
        "**/.Spotlight-V100/**\n"
        "\n"
        "# Exception allow rules for shareable dot samples/configs\n"
        "!.env.sample\n"
        "!**/.env.sample\n"
        "!.editorconfig\n"
        "!**/.editorconfig\n"
        "!.keep\n"
        "!**/.keep\n"
    )


def repair_manager_ignore_file(config: AppConfig) -> Path:
    ignore_file = config.manager_ignore_file
    ignore_file.parent.mkdir(parents=True, exist_ok=True)
    if ignore_file.exists():
        return ignore_file
    ignore_file.write_text(render_manager_ignore_template())
    return ignore_file
