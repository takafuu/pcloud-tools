from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    if not value:
        return default
    return Path(value).expanduser()


@dataclass(frozen=True)
class RuntimePaths:
    workspace_root: Path
    config_dir: Path
    state_dir: Path
    log_dir: Path

    @property
    def env_file(self) -> Path:
        return self.config_dir / ".env"

    @property
    def dev_mode(self) -> bool:
        return os.environ.get("PCLOUD_TOOLS_DEV", "").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    def ensure_directories(self) -> None:
        for path in (self.config_dir, self.state_dir, self.log_dir):
            path.mkdir(parents=True, exist_ok=True)


def detect_runtime_paths() -> RuntimePaths:
    dev_mode = os.environ.get("PCLOUD_TOOLS_DEV", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    home = Path.home()
    workspace_root = Path(
        os.environ.get(
            "PCLOUD_TOOLS_WORKSPACE_ROOT",
            Path.cwd() if dev_mode else home / "p-core",
        )
    ).expanduser()
    dev_root = workspace_root / ".dev-state"

    if dev_mode:
        default_config_dir = dev_root / "config"
        default_state_dir = dev_root / "state"
        default_log_dir = dev_root / "logs"
    else:
        default_config_dir = home / ".config" / "pcloud-tools"
        default_state_dir = home / ".pcloud"
        default_log_dir = default_state_dir / "logs"

    return RuntimePaths(
        workspace_root=workspace_root,
        config_dir=_env_path("PCLOUD_TOOLS_CONFIG_DIR", default_config_dir),
        state_dir=_env_path("PCLOUD_TOOLS_STATE_DIR", default_state_dir),
        log_dir=_env_path("PCLOUD_TOOLS_LOG_DIR", default_log_dir),
    )


def action_entrypoint_command(paths: RuntimePaths) -> str:
    configured_entrypoint = os.environ.get("PCLOUD_TOOLS_PUBLIC_ENTRYPOINT")
    if configured_entrypoint:
        configured_path = Path(configured_entrypoint).expanduser()
        if configured_path.exists() and os.access(configured_path, os.X_OK):
            return str(configured_path)

    if paths.dev_mode:
        dev_entrypoint = paths.workspace_root / "pcloud-manager-dev"
        if dev_entrypoint.exists() and os.access(dev_entrypoint, os.X_OK):
            return str(dev_entrypoint)
        resolved_dev = shutil.which("pcloud-manager-dev")
        if resolved_dev:
            return resolved_dev

    resolved_public = _public_entrypoint_from_path(skip_prefix=paths.dev_mode)
    if resolved_public:
        return resolved_public

    argv0 = Path(sys.argv[0])
    if argv0.exists() and os.access(argv0, os.X_OK):
        return str(argv0)
    return sys.executable


def _public_entrypoint_from_path(*, skip_prefix: bool) -> str | None:
    if not skip_prefix:
        return shutil.which("pcloud-manager")

    active_prefix = Path(sys.prefix).resolve()
    for directory in os.environ.get("PATH", os.defpath).split(os.pathsep):
        if not directory:
            directory = os.curdir
        candidate = Path(directory).expanduser() / "pcloud-manager"
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            continue
        try:
            candidate.resolve().relative_to(active_prefix)
        except ValueError:
            return str(candidate)
    return None
