from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_dev_entrypoint_does_not_inherit_production_credentials_or_actions(tmp_path: Path) -> None:
    """Exercise the real launcher and credential resolver with a usable production fixture."""
    workspace = tmp_path / "workspace"
    package = workspace / "src" / "pcloud_tools"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        f"__path__.append({str(REPO_ROOT / 'src' / 'pcloud_tools')!r})\n"
    )
    (package / "cli.py").write_text(
        "import json, os\n"
        "from pcloud_tools.config import load_config\n"
        "from pcloud_tools.runtime import detect_runtime_paths, action_entrypoint_command\n"
        "from pcloud_tools.rclone_config import rclone_config_path, load_rclone_pcloud_credentials\n"
        "p = detect_runtime_paths(); c = load_config(p).config\n"
        "print(json.dumps({'credentials': load_rclone_pcloud_credentials(c) is not None, "
        "'token': bool(c.pcloud_api_token), 'rclone_config': str(rclone_config_path()), "
        "'cache': os.environ['RCLONE_CACHE_DIR'], 'state': str(c.state_dir), "
        "'action': action_entrypoint_command(p)}))\n"
    )
    launcher = workspace / "pcloud-manager-dev"
    launcher.write_bytes((REPO_ROOT / "pcloud-manager-dev").read_bytes())
    launcher.chmod(0o755)
    python = workspace / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    production = tmp_path / "production-rclone.conf"
    production.write_text('[pcloud]\ntype = pcloud\ntoken = {"access_token":"fixture-only"}\n')
    public = tmp_path / "pcloud-manager"
    public.write_text("#!/bin/sh\nexit 0\n")
    public.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith("PCLOUD_TOOLS_")}
    env.update({
        "RCLONE_CONFIG": str(production),
        "RCLONE_CACHE_DIR": str(tmp_path / "production-cache"),
        "PCLOUD_TOOLS_PCLOUD_API_TOKEN": "fixture-only",
        "PCLOUD_TOOLS_PUBLIC_ENTRYPOINT": str(public),
    })
    result = subprocess.run([str(launcher)], env=env, capture_output=True, text=True, check=True)
    data = json.loads(result.stdout)
    assert data["credentials"] is False
    assert data["token"] is False
    assert data["rclone_config"] == str(workspace / ".dev-state/config/rclone.conf")
    assert data["cache"] == str(workspace / ".dev-state/cache/rclone")
    assert data["state"] == str(workspace / ".dev-state/state")
    assert data["action"] == str(launcher)
    assert not (workspace / ".dev-state").exists()
