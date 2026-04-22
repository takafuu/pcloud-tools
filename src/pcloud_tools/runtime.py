from __future__ import annotations

import os
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
    workspace_root = Path(
        os.environ.get("PCLOUD_TOOLS_WORKSPACE_ROOT", Path.cwd())
    ).expanduser()
    dev_root = workspace_root / ".dev-state"

    return RuntimePaths(
        workspace_root=workspace_root,
        config_dir=_env_path("PCLOUD_TOOLS_CONFIG_DIR", dev_root / "config"),
        state_dir=_env_path("PCLOUD_TOOLS_STATE_DIR", dev_root / "state"),
        log_dir=_env_path("PCLOUD_TOOLS_LOG_DIR", dev_root / "logs"),
    )
