from __future__ import annotations

import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = REPO_ROOT / "install.sh"


def _fake_uv(tmp_path: Path) -> tuple[Path, Path]:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    log = tmp_path / "uv.log"
    uv = fake_bin / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "printf '%s\\n' \"$*\" > \"$FAKE_UV_LOG\"\n"
        "mkdir -p \"$UV_TOOL_BIN_DIR\"\n"
        "for command_name in pcloud-manager pcloud-tools pcloud-archive pcloud-pushd pcloud-diffd; do\n"
        "  printf '#!/bin/sh\\necho \"%s 0.1.0\"\\n' \"$command_name\" > \"$UV_TOOL_BIN_DIR/$command_name\"\n"
        "  chmod 755 \"$UV_TOOL_BIN_DIR/$command_name\"\n"
        "done\n"
    )
    uv.chmod(0o755)
    return fake_bin, log


def test_installer_uses_isolated_uv_runtime_and_thin_wrappers(tmp_path: Path) -> None:
    fake_bin, log = _fake_uv(tmp_path)
    runtime_dir = tmp_path / "runtime"
    bin_dir = tmp_path / "public-bin"
    wheel = tmp_path / "pcloud_tools-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"test wheel placeholder")
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "FAKE_UV_LOG": str(log),
        }
    )

    result = subprocess.run(
        [
            str(INSTALLER),
            "--wheel",
            str(wheel),
            "--runtime-dir",
            str(runtime_dir),
            "--bin-dir",
            str(bin_dir),
            "--no-bootstrap-uv",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "tool install --force --python 3.11 --no-config" in log.read_text()
    for command_name in (
        "pcloud-manager",
        "pcloud-tools",
        "pcloud-archive",
        "pcloud-pushd",
        "pcloud-diffd",
    ):
        wrapper = bin_dir / command_name
        assert wrapper.is_file()
        text = wrapper.read_text()
        assert str(REPO_ROOT) not in text
        assert str(runtime_dir) in text
        assert f"PCLOUD_TOOLS_PUBLIC_ENTRYPOINT='{bin_dir / 'pcloud-manager'}'" in text
        executed = subprocess.run(
            [str(wrapper)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert executed.returncode == 0
        assert executed.stdout.strip() == f"{command_name} 0.1.0"


def test_installer_dry_run_shows_github_release_without_writes(tmp_path: Path) -> None:
    fake_bin, _log = _fake_uv(tmp_path)
    runtime_dir = tmp_path / "runtime"
    bin_dir = tmp_path / "public-bin"
    env = os.environ.copy()
    env.update({"PATH": f"{fake_bin}:/usr/bin:/bin"})

    result = subprocess.run(
        [
            str(INSTALLER),
            "--dry-run",
            "--runtime-dir",
            str(runtime_dir),
            "--bin-dir",
            str(bin_dir),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "https://github.com/takafuu/pcloud-tools/releases/latest/download" in result.stdout
    assert "would run uv tool install" in result.stdout
    assert not runtime_dir.exists()
    assert not bin_dir.exists()


def test_installer_refuses_missing_uv_when_bootstrap_is_disabled(tmp_path: Path) -> None:
    wheel = tmp_path / "pcloud_tools-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"test wheel placeholder")
    env = os.environ.copy()
    env["PATH"] = "/usr/bin:/bin"

    result = subprocess.run(
        [str(INSTALLER), "--wheel", str(wheel), "--no-bootstrap-uv"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "uv was not found" in result.stderr
