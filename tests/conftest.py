from __future__ import annotations

import argparse
import json
import os
import plistlib
import shlex
import subprocess
import sys
import tempfile
import time
import threading
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from pcloud_tools.config import AppConfig
from pcloud_tools.download_suppression import (
    LocalFingerprint,
    SuppressionRecord,
    local_fingerprint,
    mark_download_completed,
    mark_upload_completed,
    read_download_suppression_journal,
    read_upload_origin_journal,
    write_download_suppression_journal,
    write_upload_origin_journal,
)
from pcloud_tools.gates import GATES, validate_gate
from pcloud_tools.io_utils import atomic_write_json
from pcloud_tools.sync_exec import build_sync_plan


REPO_ROOT = Path(__file__).resolve().parents[1]


def _base_env(tmp_path: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    workspace = tmp_path / "workspace"
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    log_dir = tmp_path / "logs"
    home_dir = tmp_path / "home"
    cache_dir = tmp_path / "cache"
    workspace.mkdir(parents=True)
    config_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True)
    log_dir.mkdir(parents=True)
    home_dir.mkdir(parents=True)
    cache_dir.mkdir(parents=True)
    (workspace / ".pcloud-sync-allowlist").write_text("Documents/\n")

    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PCLOUD_TOOLS_")
    }
    env.update(
        {
            "PYTHONPATH": str(REPO_ROOT / "src"),
            "PCLOUD_TOOLS_DEV": "1",
            "PCLOUD_TOOLS_WORKSPACE_ROOT": str(workspace),
            "PCLOUD_TOOLS_CONFIG_DIR": str(config_dir),
            "PCLOUD_TOOLS_STATE_DIR": str(state_dir),
            "PCLOUD_TOOLS_LOG_DIR": str(log_dir),
            "HOME": str(home_dir),
            "XDG_CACHE_HOME": str(cache_dir),
        }
    )
    if extra:
        env.update(extra)
    return env
def _run_cli(tmp_path: Path, *args: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", *args],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=_base_env(tmp_path, extra_env),
    )
def _payload(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    return json.loads(result.stdout)
def _minimal_journal_config(tmp_path: Path) -> AppConfig:
    return cast(
        AppConfig,
        SimpleNamespace(
            state_dir=tmp_path / "state",
            core_dir=tmp_path / "workspace",
            download_suppression_ttl_seconds=86400,
        ),
    )
def _state_dir(env: dict[str, str]) -> Path:
    return Path(env["PCLOUD_TOOLS_STATE_DIR"])
def _use_default_dev_state_dir(env: dict[str, str]) -> Path:
    state_dir = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"]) / ".dev-state" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    env["PCLOUD_TOOLS_STATE_DIR"] = str(state_dir)
    return state_dir
def _install_fake_rclone(env: dict[str, str]) -> Path:
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    bin_dir = workspace / ".dev-state" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    fake_log = workspace / ".dev-state" / "fake-rclone.log"
    fake_rclone = bin_dir / "fake-rclone"
    fake_rclone.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$FAKE_RCLONE_LOG\"\n"
        "if [ \"$1\" = \"copyto\" ]; then\n"
        "  dest=\"$3\"\n"
        "  case \"$dest\" in\n"
        "    pcloud:*) ;;\n"
        "    *) mkdir -p \"$(dirname \"$dest\")\" && printf 'fake download\\n' > \"$dest\" ;;\n"
        "  esac\n"
        "fi\n"
    )
    fake_rclone.chmod(0o755)
    env["PCLOUD_TOOLS_TRANSFER_EXECUTION_GATE"] = "dev-fake-rclone"
    env["PCLOUD_TOOLS_RCLONE_BIN"] = str(fake_rclone)
    env["FAKE_RCLONE_LOG"] = str(fake_log)
    return fake_log
def _install_fake_mode_launchctl(tmp_path: Path, extra_script: str = "") -> tuple[Path, Path, dict[str, str]]:
    bin_dir = tmp_path / "mode-bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    log = tmp_path / "mode-launchctl.log"
    fake_launchctl = bin_dir / "launchctl"
    fake_launchctl.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> {shlex.quote(str(log))}\n"
        f"{extra_script}\n"
        "if [ \"$1\" = \"print\" ]; then\n"
        "  case \"$2\" in\n"
        "    *pcloud-bisync*) exit 113 ;;\n"
        "    *) printf 'state = not running\\nruns = 3\\n'; exit 0 ;;\n"
        "  esac\n"
        "fi\n"
        "if [ \"$1\" = \"bootout\" ]; then\n"
        "  case \"$2\" in\n"
        "    *pcloud-bisync*) printf 'Boot-out failed: 3: No such process\\n' >&2; exit 3 ;;\n"
        "  esac\n"
        "fi\n"
        "exit 0\n"
    )
    fake_launchctl.chmod(0o755)
    return fake_launchctl, log, {"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}"}
def _install_real_rclone_stub(env: dict[str, str]) -> Path:
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    bin_dir = workspace / ".dev-state" / "real-bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    real_log = workspace / ".dev-state" / "real-rclone-stub.log"
    rclone = bin_dir / "rclone"
    rclone.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$REAL_RCLONE_STUB_LOG\"\n"
        "if [ \"$1\" = \"copyto\" ]; then\n"
        "  dest=\"$3\"\n"
        "  case \"$dest\" in\n"
        "    pcloud:*) ;;\n"
        "    *) mkdir -p \"$(dirname \"$dest\")\" && printf 'stub download\\n' > \"$dest\" ;;\n"
        "  esac\n"
        "fi\n"
    )
    rclone.chmod(0o755)
    env["PCLOUD_TOOLS_RCLONE_BIN"] = str(rclone)
    env["REAL_RCLONE_STUB_LOG"] = str(real_log)
    return real_log
def _write_workspace_file(env: dict[str, str], relative_path: str, content: str = "test\n") -> Path:
    path = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"]) / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path
def _xbar_bash_values(output: str) -> list[str]:
    values: list[str] = []
    for line in output.splitlines():
        if " | " not in line:
            continue
        fields = shlex.split(line.split(" | ", 1)[1])
        for field in fields:
            if field.startswith("bash="):
                values.append(field.removeprefix("bash="))
    return values

__all__ = [name for name in globals() if not name.startswith("__")]
