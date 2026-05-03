from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

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
    default_excludes: tuple[str, ...]
    autosync_label: str
    autosync_plist: Path
    indexer_bin: Path
    notify_bin: Path
    rclone_bin: str
    transfer_execution_gate: str
    pushd_fswatch_resident_gate: str
    diffd_api_long_poll_gate: str
    autosync_launchd_gate: str
    transfer_exec_timeout_seconds: int
    pushd_debounce_seconds: int
    pushd_queue_limit: int
    diffd_poll_interval_seconds: int
    diffd_batch_limit: int


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

    defaults = {
        "PCLOUD_TOOLS_CORE_DIR": str(home / "p-core"),
        "PCLOUD_TOOLS_REMOTE": "pcloud:",
        "PCLOUD_TOOLS_CORE_REMOTE": "pcloud:core",
        "PCLOUD_TOOLS_VAULT_REMOTE": "pcloud:vault",
        "PCLOUD_TOOLS_CRYPT_REMOTE": "pcloud-crypt:",
        "PCLOUD_TOOLS_VAULT_DIR": str(home / "p-vault"),
        "PCLOUD_TOOLS_VAULT_MOUNT_DIR": str(base_state_dir / "vault"),
        "PCLOUD_TOOLS_CRYPT_DIR": str(home / "p-crypt"),
        "PCLOUD_TOOLS_CRYPT_MOUNT_DIR": str(base_state_dir / "crypt"),
        "PCLOUD_TOOLS_ENABLE_VAULT_LAYER": "1",
        "PCLOUD_TOOLS_ENABLE_CRYPT_LAYER": "1",
        "PCLOUD_TOOLS_VAULT_ENGINE": "webdav",
        "PCLOUD_TOOLS_CRYPT_ENGINE": "webdav",
        "PCLOUD_TOOLS_VAULT_PORT": "5566",
        "PCLOUD_TOOLS_CRYPT_PORT": "5567",
        "PCLOUD_TOOLS_STATE_DIR": str(base_state_dir),
        "PCLOUD_TOOLS_LOG_DIR": str(base_log_dir),
        "PCLOUD_TOOLS_ALLOWLIST_FILE": str(home / "p-core" / ".pcloud-sync-allowlist"),
        "PCLOUD_TOOLS_DEFAULT_EXCLUDES": "config/dotfiles/.ssh/agent/**,.DS_Store,**/.DS_Store",
        "PCLOUD_TOOLS_AUTOSYNC_LABEL": "com.takafumi.pcloud-bisync",
        "PCLOUD_TOOLS_AUTOSYNC_PLIST": str(
            home / "Library/LaunchAgents/com.takafumi.pcloud-bisync.plist"
        ),
        "PCLOUD_TOOLS_INDEXER_BIN": str(home / ".zsh/functions/pcloud-indexer.py"),
        "PCLOUD_TOOLS_NOTIFY_BIN": str(home / "bin/notify"),
        "PCLOUD_TOOLS_RCLONE_BIN": "rclone",
        "PCLOUD_TOOLS_TRANSFER_EXECUTION_GATE": "",
        "PCLOUD_TOOLS_PUSHD_FSWATCH_RESIDENT_GATE": "",
        "PCLOUD_TOOLS_DIFFD_API_LONG_POLL_GATE": "",
        "PCLOUD_TOOLS_AUTOSYNC_LAUNCHD_GATE": "",
        "PCLOUD_TOOLS_TRANSFER_EXEC_TIMEOUT_SECONDS": "5",
        "PCLOUD_TOOLS_PUSHD_DEBOUNCE_SECONDS": "3",
        "PCLOUD_TOOLS_PUSHD_QUEUE_LIMIT": "1000",
        "PCLOUD_TOOLS_DIFFD_POLL_INTERVAL_SECONDS": "60",
        "PCLOUD_TOOLS_DIFFD_BATCH_LIMIT": "100",
    }

    if paths.dev_mode:
        defaults["PCLOUD_TOOLS_CORE_DIR"] = str(paths.workspace_root)
        defaults["PCLOUD_TOOLS_ALLOWLIST_FILE"] = str(paths.workspace_root / ".pcloud-sync-allowlist")
        defaults["PCLOUD_TOOLS_VAULT_DIR"] = str(paths.workspace_root / ".dev-state/links/vault")
        defaults["PCLOUD_TOOLS_CRYPT_DIR"] = str(paths.workspace_root / ".dev-state/links/crypt")
        defaults["PCLOUD_TOOLS_AUTOSYNC_LABEL"] = "com.example.pcloud-bisync.dev"
        defaults["PCLOUD_TOOLS_AUTOSYNC_PLIST"] = str(
            paths.workspace_root / ".dev-state/com.example.pcloud-bisync.dev.plist"
        )
        defaults["PCLOUD_TOOLS_INDEXER_BIN"] = str(paths.workspace_root / "scripts/pcloud-indexer.py")
        defaults["PCLOUD_TOOLS_NOTIFY_BIN"] = str(paths.workspace_root / "scripts/notify")

    return defaults


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

        config = AppConfig(
            env_file=paths.env_file,
            core_dir=_path_value("PCLOUD_TOOLS_CORE_DIR", values, defaults["PCLOUD_TOOLS_CORE_DIR"]),
            remote=_string_value("PCLOUD_TOOLS_REMOTE", values, defaults["PCLOUD_TOOLS_REMOTE"]),
            core_remote=_string_value(
                "PCLOUD_TOOLS_CORE_REMOTE", values, defaults["PCLOUD_TOOLS_CORE_REMOTE"]
            ),
            vault_remote=_string_value(
                "PCLOUD_TOOLS_VAULT_REMOTE", values, defaults["PCLOUD_TOOLS_VAULT_REMOTE"]
            ),
            crypt_remote=_string_value(
                "PCLOUD_TOOLS_CRYPT_REMOTE", values, defaults["PCLOUD_TOOLS_CRYPT_REMOTE"]
            ),
            vault_dir=_path_value("PCLOUD_TOOLS_VAULT_DIR", values, defaults["PCLOUD_TOOLS_VAULT_DIR"]),
            vault_mount_dir=_path_value(
                "PCLOUD_TOOLS_VAULT_MOUNT_DIR", values, defaults["PCLOUD_TOOLS_VAULT_MOUNT_DIR"]
            ),
            crypt_dir=_path_value("PCLOUD_TOOLS_CRYPT_DIR", values, defaults["PCLOUD_TOOLS_CRYPT_DIR"]),
            crypt_mount_dir=_path_value(
                "PCLOUD_TOOLS_CRYPT_MOUNT_DIR", values, defaults["PCLOUD_TOOLS_CRYPT_MOUNT_DIR"]
            ),
            enable_vault_layer=_bool_from_value(
                "PCLOUD_TOOLS_ENABLE_VAULT_LAYER",
                values["PCLOUD_TOOLS_ENABLE_VAULT_LAYER"],
            ),
            enable_crypt_layer=_bool_from_value(
                "PCLOUD_TOOLS_ENABLE_CRYPT_LAYER",
                values["PCLOUD_TOOLS_ENABLE_CRYPT_LAYER"],
            ),
            vault_engine=_string_value(
                "PCLOUD_TOOLS_VAULT_ENGINE", values, defaults["PCLOUD_TOOLS_VAULT_ENGINE"]
            ),
            crypt_engine=_string_value(
                "PCLOUD_TOOLS_CRYPT_ENGINE", values, defaults["PCLOUD_TOOLS_CRYPT_ENGINE"]
            ),
            vault_port=_int_from_value(
                "PCLOUD_TOOLS_VAULT_PORT",
                values["PCLOUD_TOOLS_VAULT_PORT"],
            ),
            crypt_port=_int_from_value(
                "PCLOUD_TOOLS_CRYPT_PORT",
                values["PCLOUD_TOOLS_CRYPT_PORT"],
            ),
            state_dir=_path_value("PCLOUD_TOOLS_STATE_DIR", values, defaults["PCLOUD_TOOLS_STATE_DIR"]),
            log_dir=_path_value("PCLOUD_TOOLS_LOG_DIR", values, defaults["PCLOUD_TOOLS_LOG_DIR"]),
            allowlist_file=_path_value(
                "PCLOUD_TOOLS_ALLOWLIST_FILE", values, defaults["PCLOUD_TOOLS_ALLOWLIST_FILE"]
            ),
            default_excludes=_csv_value(
                "PCLOUD_TOOLS_DEFAULT_EXCLUDES",
                values,
                defaults["PCLOUD_TOOLS_DEFAULT_EXCLUDES"],
            ),
            autosync_label=_string_value(
                "PCLOUD_TOOLS_AUTOSYNC_LABEL", values, defaults["PCLOUD_TOOLS_AUTOSYNC_LABEL"]
            ),
            autosync_plist=_path_value(
                "PCLOUD_TOOLS_AUTOSYNC_PLIST", values, defaults["PCLOUD_TOOLS_AUTOSYNC_PLIST"]
            ),
            indexer_bin=_path_value(
                "PCLOUD_TOOLS_INDEXER_BIN", values, defaults["PCLOUD_TOOLS_INDEXER_BIN"]
            ),
            notify_bin=_path_value(
                "PCLOUD_TOOLS_NOTIFY_BIN", values, defaults["PCLOUD_TOOLS_NOTIFY_BIN"]
            ),
            rclone_bin=_string_value("PCLOUD_TOOLS_RCLONE_BIN", values, defaults["PCLOUD_TOOLS_RCLONE_BIN"]),
            transfer_execution_gate=_string_value(
                "PCLOUD_TOOLS_TRANSFER_EXECUTION_GATE",
                values,
                defaults["PCLOUD_TOOLS_TRANSFER_EXECUTION_GATE"],
            ),
            pushd_fswatch_resident_gate=_string_value(
                "PCLOUD_TOOLS_PUSHD_FSWATCH_RESIDENT_GATE",
                values,
                defaults["PCLOUD_TOOLS_PUSHD_FSWATCH_RESIDENT_GATE"],
            ),
            diffd_api_long_poll_gate=_string_value(
                "PCLOUD_TOOLS_DIFFD_API_LONG_POLL_GATE",
                values,
                defaults["PCLOUD_TOOLS_DIFFD_API_LONG_POLL_GATE"],
            ),
            autosync_launchd_gate=_string_value(
                "PCLOUD_TOOLS_AUTOSYNC_LAUNCHD_GATE",
                values,
                defaults["PCLOUD_TOOLS_AUTOSYNC_LAUNCHD_GATE"],
            ),
            transfer_exec_timeout_seconds=_int_from_value(
                "PCLOUD_TOOLS_TRANSFER_EXEC_TIMEOUT_SECONDS",
                values["PCLOUD_TOOLS_TRANSFER_EXEC_TIMEOUT_SECONDS"],
            ),
            pushd_debounce_seconds=_int_from_value(
                "PCLOUD_TOOLS_PUSHD_DEBOUNCE_SECONDS",
                values["PCLOUD_TOOLS_PUSHD_DEBOUNCE_SECONDS"],
            ),
            pushd_queue_limit=_int_from_value(
                "PCLOUD_TOOLS_PUSHD_QUEUE_LIMIT",
                values["PCLOUD_TOOLS_PUSHD_QUEUE_LIMIT"],
            ),
            diffd_poll_interval_seconds=_int_from_value(
                "PCLOUD_TOOLS_DIFFD_POLL_INTERVAL_SECONDS",
                values["PCLOUD_TOOLS_DIFFD_POLL_INTERVAL_SECONDS"],
            ),
            diffd_batch_limit=_int_from_value(
                "PCLOUD_TOOLS_DIFFD_BATCH_LIMIT",
                values["PCLOUD_TOOLS_DIFFD_BATCH_LIMIT"],
            ),
        )
    except ConfigError as exc:
        issues.append(ConfigIssue(key="config", level="error", message=str(exc)))
        fallback = _build_fallback_config(paths, defaults)
        source = "env-error" if paths.env_file.exists() else "defaults"
        return ConfigLoadResult(config=fallback, source=source, issues=tuple(issues))

    issues.extend(validate_config(config))
    source = "env" if paths.env_file.exists() else "defaults"
    return ConfigLoadResult(config=config, source=source, issues=tuple(issues))


def _build_fallback_config(paths: RuntimePaths, defaults: dict[str, str]) -> AppConfig:
    return AppConfig(
        env_file=paths.env_file,
        core_dir=Path(defaults["PCLOUD_TOOLS_CORE_DIR"]).expanduser(),
        remote=defaults["PCLOUD_TOOLS_REMOTE"],
        core_remote=defaults["PCLOUD_TOOLS_CORE_REMOTE"],
        vault_remote=defaults["PCLOUD_TOOLS_VAULT_REMOTE"],
        crypt_remote=defaults["PCLOUD_TOOLS_CRYPT_REMOTE"],
        vault_dir=Path(defaults["PCLOUD_TOOLS_VAULT_DIR"]).expanduser(),
        vault_mount_dir=Path(defaults["PCLOUD_TOOLS_VAULT_MOUNT_DIR"]).expanduser(),
        crypt_dir=Path(defaults["PCLOUD_TOOLS_CRYPT_DIR"]).expanduser(),
        crypt_mount_dir=Path(defaults["PCLOUD_TOOLS_CRYPT_MOUNT_DIR"]).expanduser(),
        enable_vault_layer=True,
        enable_crypt_layer=True,
        vault_engine=defaults["PCLOUD_TOOLS_VAULT_ENGINE"],
        crypt_engine=defaults["PCLOUD_TOOLS_CRYPT_ENGINE"],
        vault_port=int(defaults["PCLOUD_TOOLS_VAULT_PORT"]),
        crypt_port=int(defaults["PCLOUD_TOOLS_CRYPT_PORT"]),
        state_dir=Path(defaults["PCLOUD_TOOLS_STATE_DIR"]).expanduser(),
        log_dir=Path(defaults["PCLOUD_TOOLS_LOG_DIR"]).expanduser(),
        allowlist_file=Path(defaults["PCLOUD_TOOLS_ALLOWLIST_FILE"]).expanduser(),
        default_excludes=tuple(defaults["PCLOUD_TOOLS_DEFAULT_EXCLUDES"].split(",")),
        autosync_label=defaults["PCLOUD_TOOLS_AUTOSYNC_LABEL"],
        autosync_plist=Path(defaults["PCLOUD_TOOLS_AUTOSYNC_PLIST"]).expanduser(),
        indexer_bin=Path(defaults["PCLOUD_TOOLS_INDEXER_BIN"]).expanduser(),
        notify_bin=Path(defaults["PCLOUD_TOOLS_NOTIFY_BIN"]).expanduser(),
        rclone_bin=defaults["PCLOUD_TOOLS_RCLONE_BIN"],
        transfer_execution_gate=defaults["PCLOUD_TOOLS_TRANSFER_EXECUTION_GATE"],
        pushd_fswatch_resident_gate=defaults["PCLOUD_TOOLS_PUSHD_FSWATCH_RESIDENT_GATE"],
        diffd_api_long_poll_gate=defaults["PCLOUD_TOOLS_DIFFD_API_LONG_POLL_GATE"],
        autosync_launchd_gate=defaults["PCLOUD_TOOLS_AUTOSYNC_LAUNCHD_GATE"],
        transfer_exec_timeout_seconds=int(defaults["PCLOUD_TOOLS_TRANSFER_EXEC_TIMEOUT_SECONDS"]),
        pushd_debounce_seconds=int(defaults["PCLOUD_TOOLS_PUSHD_DEBOUNCE_SECONDS"]),
        pushd_queue_limit=int(defaults["PCLOUD_TOOLS_PUSHD_QUEUE_LIMIT"]),
        diffd_poll_interval_seconds=int(defaults["PCLOUD_TOOLS_DIFFD_POLL_INTERVAL_SECONDS"]),
        diffd_batch_limit=int(defaults["PCLOUD_TOOLS_DIFFD_BATCH_LIMIT"]),
    )


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
        ("PCLOUD_TOOLS_PUSHD_DEBOUNCE_SECONDS", config.pushd_debounce_seconds),
        ("PCLOUD_TOOLS_DIFFD_POLL_INTERVAL_SECONDS", config.diffd_poll_interval_seconds),
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

    return issues


def render_env_template(paths: RuntimePaths) -> str:
    defaults = _defaults_for_runtime(paths)
    lines = [
        "# pcloud-tools runtime configuration",
        "# Generated by `pcloud-manager-dev doctor --repair`.",
        "",
    ]

    ordered_keys = [
        "PCLOUD_TOOLS_CORE_DIR",
        "PCLOUD_TOOLS_REMOTE",
        "PCLOUD_TOOLS_CORE_REMOTE",
        "PCLOUD_TOOLS_VAULT_REMOTE",
        "PCLOUD_TOOLS_CRYPT_REMOTE",
        "PCLOUD_TOOLS_VAULT_DIR",
        "PCLOUD_TOOLS_VAULT_MOUNT_DIR",
        "PCLOUD_TOOLS_CRYPT_DIR",
        "PCLOUD_TOOLS_CRYPT_MOUNT_DIR",
        "PCLOUD_TOOLS_ENABLE_VAULT_LAYER",
        "PCLOUD_TOOLS_ENABLE_CRYPT_LAYER",
        "PCLOUD_TOOLS_VAULT_ENGINE",
        "PCLOUD_TOOLS_CRYPT_ENGINE",
        "PCLOUD_TOOLS_VAULT_PORT",
        "PCLOUD_TOOLS_CRYPT_PORT",
        "PCLOUD_TOOLS_AUTOSYNC_LABEL",
        "PCLOUD_TOOLS_AUTOSYNC_PLIST",
        "PCLOUD_TOOLS_ALLOWLIST_FILE",
        "PCLOUD_TOOLS_DEFAULT_EXCLUDES",
        "PCLOUD_TOOLS_INDEXER_BIN",
        "PCLOUD_TOOLS_NOTIFY_BIN",
        "PCLOUD_TOOLS_RCLONE_BIN",
        "PCLOUD_TOOLS_TRANSFER_EXECUTION_GATE",
        "PCLOUD_TOOLS_TRANSFER_EXEC_TIMEOUT_SECONDS",
        "PCLOUD_TOOLS_PUSHD_DEBOUNCE_SECONDS",
        "PCLOUD_TOOLS_PUSHD_QUEUE_LIMIT",
        "PCLOUD_TOOLS_DIFFD_POLL_INTERVAL_SECONDS",
        "PCLOUD_TOOLS_DIFFD_BATCH_LIMIT",
    ]
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
        return "# Starter allowlist for pcloud-tools development\nDocuments/\n"
    return "# Starter allowlist for pcloud-tools\nDocuments/\n"


def repair_allowlist_file(config: AppConfig, paths: RuntimePaths) -> Path:
    allowlist_file = config.allowlist_file
    allowlist_file.parent.mkdir(parents=True, exist_ok=True)
    if allowlist_file.exists():
        return allowlist_file
    allowlist_file.write_text(render_allowlist_template(paths))
    return allowlist_file
