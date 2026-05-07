from __future__ import annotations

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

from pcloud_tools.config import AppConfig
from pcloud_tools.download_suppression import local_fingerprint
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


def test_launchctl_command_runner_retries_bootstrap_input_output_error(tmp_path: Path) -> None:
    from pcloud_tools.cli_service_daemon import _run_launchctl_commands

    fake_launchctl = tmp_path / "launchctl"
    attempts_file = tmp_path / "attempts"
    fake_launchctl.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"bootstrap\" ]; then\n"
        f"  count=$(cat {shlex.quote(str(attempts_file))} 2>/dev/null || printf 0)\n"
        "  count=$((count + 1))\n"
        f"  printf '%s' \"$count\" > {shlex.quote(str(attempts_file))}\n"
        "  if [ \"$count\" = 1 ]; then\n"
        "    printf 'Bootstrap failed: 5: Input/output error\\n' >&2\n"
        "    exit 5\n"
        "  fi\n"
        "fi\n"
        "exit 0\n"
    )
    fake_launchctl.chmod(0o755)

    results = _run_launchctl_commands(
        [[str(fake_launchctl), "bootstrap", "gui/501", "/tmp/example.plist"]],
        retry_bootstrap_io_error=True,
        retry_delay_seconds=0,
    )

    assert [result["returncode"] for result in results] == [5, 0]
    assert results[0]["tolerated"] is True
    assert results[0]["retry"] == "scheduled"
    assert results[1]["retry"] == "attempted"


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


def test_autosync_internal_mode_builds_allowlist_normal_bisync_plan(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    state_dir = tmp_path / "state"
    log_dir = tmp_path / "logs"
    workspace.mkdir()
    state_dir.mkdir()
    log_dir.mkdir()
    allowlist = workspace / ".pcloud-sync-allowlist"
    allowlist.write_text("Documents/\n")
    config = AppConfig(
        env_file=tmp_path / ".env",
        core_dir=workspace,
        remote="pcloud:",
        core_remote="pcloud:core",
        vault_remote="pcloud-vault:",
        crypt_remote="pcloud-crypt:",
        vault_dir=tmp_path / "vault-dir",
        vault_mount_dir=tmp_path / "vault",
        crypt_dir=tmp_path / "crypt-dir",
        crypt_mount_dir=tmp_path / "crypt",
        enable_vault_layer=True,
        enable_crypt_layer=True,
        vault_engine="webdav",
        crypt_engine="webdav",
        vault_port=5566,
        crypt_port=5567,
        state_dir=state_dir,
        log_dir=log_dir,
        allowlist_file=allowlist,
        manager_ignore_file=workspace / ".pcloudmanagerignore",
        default_excludes=(),
        autosync_label="com.takafumi.pcloud-bisync",
        autosync_plist=tmp_path / "com.takafumi.pcloud-bisync.plist",
        indexer_bin=tmp_path / "pcloud-index",
        notify_bin=tmp_path / "pcloud-notify",
        chat_notify_enabled=False,
        chat_notify_cmd=str(tmp_path / "pcloud-notify") + " send --to discord {message}",
        rclone_bin="rclone",
        transfer_execution_gate="",
        pushd_fswatch_resident_gate="",
        diffd_api_long_poll_gate="",
        autosync_launchd_gate="",
        sync_migration_gate="",
        transfer_exec_timeout_seconds=5,
        download_suppression_ttl_seconds=86400,
        pushd_debounce_seconds=2,
        pushd_queue_limit=100,
        diffd_poll_interval_seconds=30,
        diffd_batch_limit=100,
        pcloud_api_base_url="https://api.pcloud.com",
        pcloud_api_auth_param="auth",
        pcloud_api_token="",
        pcloud_api_timeout_seconds=30,
    )

    plan = build_sync_plan(config, "autosync", ("Documents/",), rclone_bin="/usr/local/bin/rclone")

    assert plan.mode == "autosync"
    assert plan.scope_mode == "allowlist"
    assert plan.resync_mode is None
    assert plan.rclone_log.name.startswith("bisync-autosync-")
    assert "--filter-from" in plan.command
    assert "--resync-mode" not in plan.command
    assert "--track-renames" not in plan.command


def test_daemon_auto_download_execute_does_not_write_on_config_error(tmp_path: Path) -> None:
    env = _base_env(tmp_path, {"PCLOUD_TOOLS_VAULT_PORT": "bad"})
    result = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "daemon", "auto-download", "on", "--execute", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    assert result.returncode == 1
    assert payload["status"] == "error"
    assert not (_state_dir(env) / "daemon" / "auto-download").exists()


def test_dev_sync_execute_is_refused_before_remote_execution(tmp_path: Path) -> None:
    result = _run_cli(tmp_path, "sync", "--execute", "--json")

    payload = _payload(result)
    assert result.returncode == 1
    assert payload["status"] == "error"
    assert payload["summary"] == "dev mode refuses to execute bisync against a configured remote"
    assert [issue["key"] for issue in payload["issues"]] == ["PCLOUD_TOOLS_DEV_EXECUTION"]


def test_root_help_uses_runtime_specific_program_name(tmp_path: Path) -> None:
    dev_result = _run_cli(tmp_path, "--help")
    public_result = _run_cli(
        tmp_path / "public",
        "--help",
        extra_env={
            "PCLOUD_TOOLS_DEV": "0",
            "PCLOUD_TOOLS_WORKSPACE_ROOT": str(tmp_path / "live-core"),
        },
    )

    assert dev_result.returncode == 0
    assert "usage: pcloud-manager-dev " in dev_result.stdout
    assert "Development CLI for the pcloud-tools migration." in dev_result.stdout
    assert public_result.returncode == 0
    assert "usage: pcloud-manager " in public_result.stdout
    assert "pcloud-manager-dev" not in public_result.stdout
    assert "CLI for pcloud-tools operations." in public_result.stdout


def test_help_subcommand_and_ai_context_are_read_only(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    human_result = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "help"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    ai_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "help",
            "--ai",
            "inspect pushd launchd status safely",
            "--topic",
            "pushd",
            "--topic",
            "launchd",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(ai_result)
    subcommands = payload["generated_help"]["subcommands"]
    topics = {topic["name"]: topic for topic in payload["topics"]}

    assert human_result.returncode == 0
    assert "Examples:" in human_result.stdout
    assert "help --ai" in human_result.stdout
    assert ai_result.returncode == 0
    assert payload["schema_version"] == "pcloud-tools-help-ai.v1"
    assert payload["context_kind"] == "custom-cli-help-ai"
    assert payload["command_name"] == "pcloud-manager-dev"
    assert payload["runtime_mode"] == "dev"
    assert payload["user_request"] == "inspect pushd launchd status safely"
    assert "usage: pcloud-manager-dev " in payload["generated_help"]["root"]
    assert "pushd" in subcommands
    assert "help" in subcommands
    assert "info" in subcommands
    assert "launchd" in topics
    assert "pushd" in topics
    assert any("does not call an LLM" in item for item in payload["non_goals"])
    assert any("does not execute generated commands" in item for item in payload["non_goals"])
    assert any("Do not bootstrap" in item for item in topics["launchd"]["safety"])
    assert payload["important_paths"]["public_wrapper"] == "/Users/takafumi/bin/pcloud-manager"
    assert not state_dir.exists() or not any(state_dir.iterdir())


def test_public_help_ai_uses_public_command_name(tmp_path: Path) -> None:
    result = _run_cli(
        tmp_path,
        "help",
        "--ai",
        "check config paths",
        "--topic",
        "config",
        extra_env={
            "PCLOUD_TOOLS_DEV": "0",
            "PCLOUD_TOOLS_WORKSPACE_ROOT": str(tmp_path / "live-core"),
        },
    )

    payload = _payload(result)

    assert result.returncode == 0
    assert payload["command_name"] == "pcloud-manager"
    assert payload["runtime_mode"] == "public"
    assert "usage: pcloud-manager " in payload["generated_help"]["root"]
    assert "pcloud-manager-dev" not in payload["generated_help"]["root"]
    assert payload["topics"][0]["name"] == "config"


def test_info_reports_runtime_paths_and_redacted_config(tmp_path: Path) -> None:
    env = _base_env(
        tmp_path,
        {
            "PCLOUD_TOOLS_PCLOUD_API_TOKEN": "secret-token",
            "PCLOUD_TOOLS_CHAT_NOTIFY_CMD": "/tmp/notify send {message}",
        },
    )

    overview = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "info", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    paths = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "info", "paths", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    config = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "info", "config", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    overview_payload = _payload(overview)
    paths_payload = _payload(paths)
    config_payload = _payload(config)

    assert overview.returncode == 0
    assert overview_payload["command"] == "info"
    assert overview_payload["details"]["mode"] == "dev"
    assert overview_payload["details"]["manager ignore"].endswith(".pcloudmanagerignore")
    assert paths.returncode == 0
    assert paths_payload["command"] == "info paths"
    assert any("manager ignore file:" in item for item in paths_payload["details"]["paths"])
    assert config.returncode == 0
    assert config_payload["command"] == "info config"
    assert config_payload["details"]["pCloud API token"] == "set (redacted)"
    assert "secret-token" not in config.stdout
    assert config_payload["details"]["gate env values"] == "redacted from info; use gates/status commands for gate state"


def test_sync_status_marks_old_last_error_as_historical_after_success(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    (workspace / "bisync_status.log").write_text("2026-04-27 23:13:00 SUCCESS mode=autosync\n")
    (workspace / "bisync_error.log").write_text("2026-04-27 22:34:15 ERROR rclone command not found\n")

    sync_result = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "sync", "status", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    status_result = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "status", "--detail", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    sync_payload = _payload(sync_result)
    status_payload = _payload(status_result)

    assert sync_result.returncode == 0
    assert sync_payload["details"]["sync state"] == "synced"
    assert sync_payload["details"]["last error status"] == "historical"
    assert status_result.returncode == 0
    assert status_payload["details"]["last sync error status"] == "historical"


def test_sync_status_marks_last_error_as_current_when_latest_result_failed(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    (workspace / "bisync_status.log").write_text("2026-04-27 22:34:15 ERROR mode=autosync\n")
    (workspace / "bisync_error.log").write_text("2026-04-27 22:34:15 ERROR rclone command not found\n")

    result = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "sync", "status", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)

    assert result.returncode == 0
    assert payload["details"]["sync state"] == "sync_error"
    assert payload["details"]["last error status"] == "current"


def test_resync_preview_exposes_explicit_resync_mode(tmp_path: Path) -> None:
    newer = _run_cli(tmp_path / "newer", "sync", "resync", "--resync-mode", "newer", "--json")
    path1 = _run_cli(tmp_path / "path1", "sync", "resync", "--resync-mode", "path1", "--json")

    newer_payload = _payload(newer)
    path1_payload = _payload(path1)

    assert newer.returncode == 0
    assert newer_payload["details"]["resync mode"] == "newer"
    assert "--resync-mode" in newer_payload["details"]["command"]
    assert "newer" in newer_payload["details"]["command"]
    assert "--resync" not in newer_payload["details"]["command"]

    assert path1.returncode == 0
    assert path1_payload["details"]["resync mode"] == "path1"
    assert "--resync-mode" in path1_payload["details"]["command"]
    assert "path1" in path1_payload["details"]["command"]


def test_full_resync_preview_exposes_explicit_resync_mode(tmp_path: Path) -> None:
    result = _run_cli(tmp_path, "sync", "full-resync", "--resync-mode", "newer", "--json")

    payload = _payload(result)

    assert result.returncode == 0
    assert payload["details"]["scope mode"] == "full"
    assert payload["details"]["resync mode"] == "newer"
    assert "--resync-mode" in payload["details"]["command"]
    assert "newer" in payload["details"]["command"]
    assert "--filter-from" not in payload["details"]["command"]


def test_document_only_scope_policy_warns_on_source_roots(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    allowlist = workspace / ".pcloud-sync-allowlist"
    allowlist.write_text("Documents/\ndev/\nproject/\ntools/\n")

    result = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "sync", "scope", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    issue_keys = [issue["key"] for issue in payload["issues"]]
    assert result.returncode == 0
    assert payload["status"] == "warning"
    assert "PCLOUD_TOOLS_SCOPE_POLICY" in issue_keys
    policy_issue = next(issue for issue in payload["issues"] if issue["key"] == "PCLOUD_TOOLS_SCOPE_POLICY")
    assert "dev/" in policy_issue["message"]
    assert "project/" in policy_issue["message"]
    assert "tools/" in policy_issue["message"]


def test_foreground_sync_preview_rejects_stale_and_invalid_locks(tmp_path: Path) -> None:
    for lock_status in ("stale", "invalid"):
        case_path = tmp_path / lock_status
        env = _base_env(case_path)
        lock_dir = _state_dir(env) / "bisync.lock"
        lock_dir.mkdir(parents=True)
        if lock_status == "stale":
            (lock_dir / "pid").write_text("999999\n")
            (lock_dir / "mode").write_text("normal\n")
            (lock_dir / "started_at").write_text("2026-04-25 00:00:00\n")

        result = subprocess.run(
            [sys.executable, "-m", "pcloud_tools.cli", "sync", "--json"],
            check=False,
            capture_output=True,
            text=True,
            cwd=case_path,
            env=env,
        )

        payload = _payload(result)
        assert result.returncode == 1
        assert payload["status"] == "error"
        assert payload["details"]["sync lock status"] == lock_status
        assert "PCLOUD_TOOLS_SYNC_LOCK" in [issue["key"] for issue in payload["issues"]]


def test_action_dispatch_uses_stable_ids_with_isolated_runtime(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    rclone = bin_dir / "rclone"
    rclone.write_text("#!/bin/sh\nexit 0\n")
    rclone.chmod(0o755)

    env = _base_env(tmp_path, {"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"})
    daemon = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "action", "daemon.status.refresh"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    sync = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "action", "sync.preview"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    autosync_gate = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "action", "sync.autosync.gate"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    migration_gate = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "action", "sync.migration.gate"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    pushd = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "action", "pushd.preview"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    diffd = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "action", "diffd.status.refresh"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    assert daemon.returncode == 0
    assert "pCloud " in daemon.stdout
    assert "Refresh daemon state" in daemon.stdout
    assert sync.returncode == 0
    assert "sync command preview is ready" in sync.stdout
    assert autosync_gate.returncode == 0
    assert "autosync launchd gate is closed" in autosync_gate.stdout
    assert migration_gate.returncode == 0
    assert "sync migration validation gate is closed" in migration_gate.stdout
    assert pushd.returncode == 0
    assert "pushd scaffold preview is ready" in pushd.stdout
    assert diffd.returncode == 0
    assert "pCloud " in diffd.stdout
    assert "Refresh diffd state" in diffd.stdout


def test_pushd_diffd_scaffold_reports_use_isolated_state(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    pushd = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "preview", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    diffd = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "diffd", "status", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    pushd_payload = _payload(pushd)
    diffd_payload = _payload(diffd)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])

    assert pushd.returncode == 0
    assert pushd_payload["status"] in {"ok", "warning"}
    assert pushd_payload["details"]["implementation status"].startswith("scaffold only")
    assert pushd_payload["details"]["state dir"] == str(state_dir / "pushd")
    assert "pushd.preview" in [action["id"] for action in pushd_payload["actions"]]
    assert not (state_dir / "pushd").exists()

    assert diffd.returncode == 0
    assert diffd_payload["status"] in {"ok", "warning"}
    assert diffd_payload["details"]["state dir"] == str(state_dir / "diffd")
    assert "diffd.preview" in [action["id"] for action in diffd_payload["actions"]]


def test_pushd_diffd_root_wrappers_delegate_to_dev_entrypoint(tmp_path: Path) -> None:
    (tmp_path / "src").symlink_to(REPO_ROOT / "src", target_is_directory=True)
    for name in ("pcloud-manager-dev", "pcloud-pushd", "pcloud-diffd"):
        target = tmp_path / name
        target.write_text((REPO_ROOT / name).read_text())
        target.chmod(0o755)
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PCLOUD_TOOLS_") and key != "PYTHONPATH"
    }
    env["HOME"] = str(tmp_path / "home")
    env["XDG_CACHE_HOME"] = str(tmp_path / "cache")
    Path(env["HOME"]).mkdir()
    Path(env["XDG_CACHE_HOME"]).mkdir()
    (tmp_path / ".pcloud-sync-allowlist").write_text("Documents/\n")

    pushd = subprocess.run(
        [str(tmp_path / "pcloud-pushd"), "status", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    diffd = subprocess.run(
        [str(tmp_path / "pcloud-diffd"), "preview", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    pushd_payload = _payload(pushd)
    diffd_payload = _payload(diffd)

    assert os.access(REPO_ROOT / "pcloud-pushd", os.X_OK)
    assert os.access(REPO_ROOT / "pcloud-diffd", os.X_OK)
    assert pushd.returncode == 0
    assert pushd_payload["command"] == "pushd status"
    assert pushd_payload["details"]["state dir"] == str(tmp_path / ".dev-state" / "state" / "pushd")
    assert pushd_payload["details"]["queue file"] == str(tmp_path / ".dev-state" / "state" / "pushd" / "queue.json")
    assert diffd.returncode == 0
    assert diffd_payload["command"] == "diffd preview"
    assert diffd_payload["details"]["state dir"] == str(tmp_path / ".dev-state" / "state" / "diffd")
    assert diffd_payload["details"]["remote changes file"] == str(
        tmp_path / ".dev-state" / "state" / "diffd" / "remote-changes.json"
    )


def test_service_daemon_pid_zero_is_invalid_not_running(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    pushd_dir = state_dir / "pushd"
    pushd_dir.mkdir(parents=True)
    (pushd_dir / "pid").write_text("0\n")

    result = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "status", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    assert result.returncode == 0
    assert payload["status"] == "warning"
    assert payload["details"]["process state"] == "not recorded"
    assert payload["details"]["pid"] == "-"
    assert payload["summary"] == (
        "pushd: not recorded; queued: 0; planned: 0; stale: 0; manual-review: 0; launchd: not_loaded"
    )
    assert "PCLOUD_TOOLS_PUSHD_PID" in [issue["key"] for issue in payload["issues"]]


def test_service_daemon_status_summarizes_last_transfer_state(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    pushd_dir = state_dir / "pushd"
    diffd_dir = state_dir / "diffd"
    pushd_dir.mkdir(parents=True)
    diffd_dir.mkdir(parents=True)
    _write_workspace_file(env, "Documents/upload.pdf", "upload\n")
    pushd_payload = {
        "service": "pushd",
        "mode": "dev-fake-rclone-transfer",
        "results": [
            {
                "path": "Documents/upload.pdf",
                "direction": "upload",
                "returncode": 0,
                "timed_out": False,
            }
        ],
    }
    diffd_payload = {
        "service": "diffd",
        "mode": "dev-fake-rclone-transfer",
        "results": [
            {
                "path": "Documents/download.pdf",
                "direction": "download",
                "returncode": None,
                "timed_out": True,
            }
        ],
    }
    (pushd_dir / "last-transfer.json").write_text(json.dumps(pushd_payload))
    (diffd_dir / "last-transfer.json").write_text(json.dumps(diffd_payload))

    pushd = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "status", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    diffd = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "diffd", "status", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    pushd_report = _payload(pushd)
    diffd_report = _payload(diffd)
    assert pushd.returncode == 0
    assert diffd.returncode == 0
    assert pushd_report["details"]["last transfer file"] == str(pushd_dir / "last-transfer.json")
    assert diffd_report["details"]["last transfer file"] == str(diffd_dir / "last-transfer.json")
    assert pushd_report["details"]["last transfer status"] == "success"
    assert pushd_report["details"]["last transfer summary"] == "success: 1; failed: 0; timeout: 0; total: 1"
    assert diffd_report["details"]["last transfer status"] == "timeout"
    assert diffd_report["details"]["last transfer summary"] == "success: 0; failed: 0; timeout: 1; total: 1"
    assert json.loads((pushd_dir / "last-transfer.json").read_text()) == pushd_payload
    assert json.loads((diffd_dir / "last-transfer.json").read_text()) == diffd_payload


def test_service_daemon_status_warns_on_invalid_last_transfer_json(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    pushd_dir = state_dir / "pushd"
    diffd_dir = state_dir / "diffd"
    pushd_dir.mkdir(parents=True)
    diffd_dir.mkdir(parents=True)
    invalid_payload = "{not-json"
    (pushd_dir / "last-transfer.json").write_text(invalid_payload)
    (diffd_dir / "last-transfer.json").write_text(invalid_payload)

    pushd = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "status", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    diffd = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "diffd", "status", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    pushd_report = _payload(pushd)
    diffd_report = _payload(diffd)
    assert pushd.returncode == 0
    assert diffd.returncode == 0
    assert pushd_report["status"] == "warning"
    assert diffd_report["status"] == "warning"
    assert "PCLOUD_TOOLS_PUSHD_STATE_LAST_TRANSFER.JSON" in [
        issue["key"] for issue in pushd_report["issues"]
    ]
    assert "PCLOUD_TOOLS_DIFFD_STATE_LAST_TRANSFER.JSON" in [
        issue["key"] for issue in diffd_report["issues"]
    ]
    assert pushd_report["details"]["last transfer status"] == "none"
    assert diffd_report["details"]["last transfer status"] == "none"
    assert (pushd_dir / "last-transfer.json").read_text() == invalid_payload
    assert (diffd_dir / "last-transfer.json").read_text() == invalid_payload


def test_service_daemon_status_reports_last_run_and_gate_summaries(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    pushd_dir = state_dir / "pushd"
    diffd_dir = state_dir / "diffd"
    daemon_dir = state_dir / "daemon"
    pushd_dir.mkdir(parents=True)
    diffd_dir.mkdir(parents=True)
    daemon_dir.mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_launchctl = bin_dir / "launchctl"
    fake_launchctl.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"print\" ]; then\n"
        "  printf 'Could not find service \"%s\"\\n' \"$2\" >&2\n"
        "  exit 113\n"
        "fi\n"
        "exit 2\n"
    )
    fake_launchctl.chmod(0o755)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    _write_workspace_file(env, "Documents/upload.txt", "upload\n")
    (pushd_dir / "queue.json").write_text(
        json.dumps(
            [
                {"path": "Documents/upload.txt", "action": "upload"},
                {"path": "Documents/delete.txt", "action": "delete"},
            ]
        )
    )
    (pushd_dir / "fswatch-resident-last-run.json").write_text(
        json.dumps(
            {
                "finished_at": "2026-05-05T00:00:00+00:00",
                "returncode": 0,
                "appended_records": [{"path": "Documents/upload.txt", "action": "upload"}],
                "duplicate_records": [],
                "debounce_records": [],
                "queue_limit_records": [],
                "excluded_records": [],
                "invalid_records": [],
            }
        )
    )
    (diffd_dir / "remote-changes.json").write_text(
        json.dumps(
            [
                {"path": "Documents/download.txt", "action": "download"},
                {"path": "Documents/delete.txt", "action": "delete"},
            ]
        )
    )
    (diffd_dir / "api-long-poll-last-run.json").write_text(
        json.dumps(
            {
                "source": "fixture",
                "live_api": False,
                "finished_at": "2026-05-05T00:01:00+00:00",
                "previous_diffid": "9",
                "written_diffid": "10",
                "parsed_diff_changes": 3,
                "appended_records": [{"path": "Documents/download.txt", "action": "download"}],
                "skipped_records": [{"path": "private/skip.txt", "action": "download"}],
                "invalid_records": [],
            }
        )
    )
    (diffd_dir / "folder-cache.json").write_text(json.dumps({"123": "Documents"}))
    (daemon_dir / "diffid").write_text("10\n")

    pushd = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "status", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    diffd = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "diffd", "status", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    pushd_report = _payload(pushd)
    diffd_report = _payload(diffd)
    assert pushd.returncode == 0
    assert diffd.returncode == 0
    assert pushd_report["details"]["state writes"] == "none"
    assert diffd_report["details"]["state writes"] == "none"
    assert pushd_report["details"]["last resident run status"] == "success"
    assert pushd_report["details"]["last resident run summary"] == (
        "events: 1; appended: 1; duplicate: 0; debounce: 0; queue-limit: 0; excluded: 0; invalid: 0"
    )
    assert pushd_report["details"]["planned uploads"] == 1
    assert pushd_report["details"]["manual review transfer records"] == 1
    assert pushd_report["details"]["gate summary"]["resident gate"] == "closed"
    assert pushd_report["details"]["gate summary"]["launchd registration"] == "not_loaded"
    assert pushd_report["details"]["launchd gate"] == "closed"
    assert pushd_report["details"]["transfer gate"] == "closed"
    assert pushd_report["details"]["next safe actions"] == [
        "pushd preview",
        "pushd launchd status",
        "pushd launchd gate",
        "pushd transfer check",
    ]
    assert "preflight checks" not in pushd_report["details"]
    assert diffd_report["details"]["last api poll run status"] == "success"
    assert diffd_report["details"]["last api poll run summary"] == (
        "parsed: 3; appended: 1; skipped: 1; invalid: 0; diffid: 9 -> 10"
    )
    assert diffd_report["details"]["daemon diffid"] == "10"
    assert diffd_report["details"]["folder cache entries"] == 1
    assert diffd_report["details"]["planned downloads"] == 1
    assert diffd_report["details"]["manual review transfer records"] == 1
    assert diffd_report["details"]["gate summary"]["long-poll gate"] == "closed"
    assert diffd_report["details"]["gate summary"]["launchd registration"] == "not_loaded"
    assert "preflight checks" not in diffd_report["details"]
    assert json.loads((pushd_dir / "queue.json").read_text())[0]["path"] == "Documents/upload.txt"
    assert json.loads((diffd_dir / "remote-changes.json").read_text())[0]["path"] == "Documents/download.txt"


def test_pushd_status_reports_running_resident_heartbeat(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    pushd_dir = state_dir / "pushd"
    pushd_dir.mkdir(parents=True)
    _write_workspace_file(env, "Documents/upload.pdf", "upload\n")
    (pushd_dir / "queue.json").write_text("[]")
    (pushd_dir / "fswatch-resident-last-run.json").write_text(
        json.dumps(
            {
                "status": "running",
                "pid": 12345,
                "started_at": "2026-05-07T10:00:00+00:00",
                "updated_at": "2026-05-07T10:01:00+00:00",
                "last_raw_event": "/tmp/root/Documents/live.txt",
                "last_normalized_event": "Documents/live.txt",
                "appended_records": [{"path": "Documents/live.txt", "action": "upload"}],
                "duplicate_records": [],
                "debounce_records": [],
                "queue_limit_records": [],
                "excluded_records": [],
                "invalid_records": [],
            }
        )
    )

    result = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "status", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    assert result.returncode == 0
    assert payload["details"]["last resident run status"] == "running"
    assert payload["details"]["last resident run pid"] == 12345
    assert payload["details"]["last resident run updated at"] == "2026-05-07T10:01:00+00:00"
    assert payload["details"]["last resident run last normalized event"] == "Documents/live.txt"


def test_service_daemon_status_xbar_is_concise_and_safe(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    pushd_dir = state_dir / "pushd"
    diffd_dir = state_dir / "diffd"
    pushd_dir.mkdir(parents=True)
    diffd_dir.mkdir(parents=True)
    upload_path = workspace / "Documents" / "upload.txt"
    upload_path.parent.mkdir(parents=True, exist_ok=True)
    upload_path.write_text("upload\n")
    (pushd_dir / "queue.json").write_text(json.dumps([{"path": "Documents/upload.txt", "action": "upload"}]))
    (diffd_dir / "remote-changes.json").write_text(json.dumps([{"path": "Documents/download.txt", "action": "download"}]))
    (pushd_dir / "fswatch-resident-last-run.json").write_text(
        json.dumps({"returncode": 0, "appended_records": [{"path": "Documents/upload.txt", "action": "upload"}]})
    )
    (diffd_dir / "api-long-poll-last-run.json").write_text(
        json.dumps(
            {
                "written_diffid": "1",
                "previous_diffid": "0",
                "parsed_diff_changes": 1,
                "appended_records": [{"path": "Documents/download.txt", "action": "download"}],
            }
        )
    )

    pushd = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "status", "--xbar"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    diffd = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "diffd", "status", "--xbar"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    assert pushd.returncode == 0
    assert diffd.returncode == 0
    assert "pCloud " in pushd.stdout
    assert "plan: uploads=1" in pushd.stdout
    assert "notify: off; dedupe=3600s" in pushd.stdout
    assert "last resident: success" in pushd.stdout
    assert "gates: real=closed; resident=closed; transfer=closed" in pushd.stdout
    assert "Preview pushd plan" in pushd.stdout
    assert "Inspect pushd launchd status" in pushd.stdout
    assert "pushd.transfer.check" in pushd.stdout
    assert "last transfer:" not in pushd.stdout
    assert "planned_transfer_commands" not in pushd.stdout
    assert "real-run" not in pushd.stdout
    assert "real-gate" not in pushd.stdout
    assert "validation-matrix" not in pushd.stdout
    assert "consume" not in pushd.stdout
    assert "queue.clear" not in pushd.stdout
    assert "plan: downloads=1" in diffd.stdout
    assert "notify: off; dedupe=3600s" in diffd.stdout
    assert "last api poll: success" in diffd.stdout
    assert "gates: real=closed; long-poll=closed; transfer=closed" in diffd.stdout
    assert "diffd.api-poll.long-poll-gate" in diffd.stdout
    assert "real-run" not in diffd.stdout
    assert "real-gate" not in diffd.stdout
    assert "validation-matrix" not in diffd.stdout
    assert "consume" not in diffd.stdout
    assert "remote-change.clear" not in diffd.stdout


def test_pushd_preview_builds_allowlisted_queue_plan(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    pushd_dir = state_dir / "pushd"
    pushd_dir.mkdir(parents=True)
    documents_dir = workspace / "Documents"
    documents_dir.mkdir(parents=True)
    (documents_dir / "report.pdf").write_text("report\n")
    (pushd_dir / "queue.json").write_text(
        json.dumps(
            [
                {"path": "Documents/report.pdf", "action": "upload"},
                {"path": "Documents/.DS_Store", "action": "upload"},
                {"path": "Documents/.temporary-upload", "action": "upload"},
                {"path": "private/secret.txt", "action": "upload"},
                {"path": "../bad", "action": "upload"},
            ]
        )
    )

    result = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "preview", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    assert result.returncode == 0
    assert payload["status"] in {"ok", "warning"}
    assert payload["details"]["pending queue items"] == 5
    assert payload["details"]["planned uploads"] == 1
    assert payload["details"]["excluded queue items"] == 3
    assert payload["details"]["invalid queue items"] == 1
    assert payload["details"]["plan summary"] == "upload: 1; missing-local: 0; excluded: 3; invalid: 1"
    assert payload["details"]["planned upload records"][0]["path"] == "Documents/report.pdf"
    assert payload["details"]["excluded queue records"][1]["reason"] == "manager ignore rule"


def test_pushd_preview_uses_manager_ignore_exceptions_for_dot_samples(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = _use_default_dev_state_dir(env)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    (workspace / ".pcloudmanagerignore").write_text(
        "# ! lines are exception allow rules.\n"
        ".*\n"
        "**/.*\n"
        "!.env.sample\n"
        "!**/.env.sample\n"
    )
    (workspace / "Documents").mkdir()
    (workspace / "Documents" / ".env").write_text("secret=local\n")
    (workspace / "Documents" / ".env.sample").write_text("secret=\n")
    pushd_dir = state_dir / "pushd"
    pushd_dir.mkdir(parents=True)
    (pushd_dir / "queue.json").write_text(
        json.dumps(
            [
                {"path": "Documents/.env", "action": "upload"},
                {"path": "Documents/.env.sample", "action": "upload"},
            ]
        )
    )

    result = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "preview", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    assert result.returncode == 0
    assert payload["details"]["planned uploads"] == 1
    assert payload["details"]["excluded queue items"] == 1
    assert payload["details"]["planned upload records"][0]["path"] == "Documents/.env.sample"
    assert payload["details"]["excluded queue records"][0]["path"] == "Documents/.env"
    assert payload["details"]["excluded queue records"][0]["reason"] == "manager ignore rule"


def test_pushd_queue_prune_excluded_is_gated_and_scoped(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = _use_default_dev_state_dir(env)
    pushd_dir = state_dir / "pushd"
    pushd_dir.mkdir(parents=True)
    queue_file = pushd_dir / "queue.json"
    queue_file.write_text(
        json.dumps(
            [
                {"path": "Documents/report.pdf", "action": "upload"},
                {"path": "Documents/.temporary-upload", "action": "upload"},
                {"path": "private/secret.txt", "action": "upload"},
            ]
        )
    )

    preview = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "queue", "prune-excluded", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    executed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "queue",
            "prune-excluded",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    preview_payload = _payload(preview)
    executed_payload = _payload(executed)

    assert preview.returncode == 0
    assert preview_payload["summary"] == "pushd queue prune-excluded preview is ready"
    assert preview_payload["details"]["queue items removed"] == 2
    assert preview_payload["details"]["state writes"] == "none"
    assert executed.returncode == 0
    assert executed_payload["summary"] == "pushd queue excluded records pruned"
    assert executed_payload["details"]["queue items removed"] == 2
    assert executed_payload["details"]["state writes"] == "pushd queue only"
    assert json.loads(queue_file.read_text()) == [
        {"path": "Documents/report.pdf", "action": "upload"}
    ]


def test_public_pushd_queue_prune_excluded_requires_gate(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    env["PCLOUD_TOOLS_DEV"] = "0"
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    env["PCLOUD_TOOLS_CORE_DIR"] = str(workspace)
    env["PCLOUD_TOOLS_ALLOWLIST_FILE"] = str(workspace / ".pcloud-sync-allowlist")
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    pushd_dir = state_dir / "pushd"
    pushd_dir.mkdir(parents=True)
    queue_file = pushd_dir / "queue.json"
    queue_file.write_text(
        json.dumps(
            [
                {"path": "Documents/report.pdf", "action": "upload"},
                {"path": "Documents/.temporary-upload", "action": "upload"},
            ]
        )
    )

    refused = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "queue",
            "prune-excluded",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    refused_payload = _payload(refused)

    assert refused.returncode == 1
    assert refused_payload["summary"] == "pushd queue cannot be updated until issues are resolved"
    assert refused_payload["details"]["state writes"] == "none"
    assert json.loads(queue_file.read_text())[1]["path"] == "Documents/.temporary-upload"

    approved = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "queue",
            "prune-excluded",
            "--reviewer-approved-excluded-record-cleanup",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env
        | {
            "PCLOUD_TOOLS_PUSHD_QUEUE_PRUNE_EXCLUDED_GATE": (
                "operator-approved-pushd-queue-prune-excluded-v1"
            )
        },
    )

    approved_payload = _payload(approved)

    assert approved.returncode == 0
    assert approved_payload["summary"] == "pushd queue excluded records pruned"
    assert approved_payload["details"]["prune gate env honored"] == "yes"
    assert json.loads(queue_file.read_text()) == [{"path": "Documents/report.pdf", "action": "upload"}]


def test_pushd_fswatch_fixture_preview_is_read_only(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    _write_workspace_file(env, "Documents/report.pdf", "report\n")
    fixture = tmp_path / "fswatch-events.txt"
    fixture.write_text(
        "\n".join(
            [
                "Documents/report.pdf\tCreated Updated",
                "Documents/.DS_Store\tUpdated",
                "Documents/.temporary-upload\tUpdated",
                "private/secret.txt\tCreated",
                "../bad\tUpdated",
            ]
        )
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "fswatch",
            "preview",
            "--fixture",
            str(fixture),
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    assert result.returncode == 0
    assert payload["summary"] == "pushd fswatch fixture preview is ready"
    assert payload["details"]["implementation status"] == "fixture parser only; fswatch process is not started"
    assert payload["details"]["gate status"] == "closed"
    assert payload["details"]["parsed fswatch events"] == 4
    assert payload["details"]["invalid fswatch events"] == 1
    assert payload["details"]["planned uploads"] == 1
    assert payload["details"]["excluded queue items"] == 3
    assert payload["details"]["invalid queue items"] == 1
    assert payload["details"]["planned upload records"][0]["reason"] == "fswatch:Created,Updated"
    assert not (state_dir / "pushd").exists()


def test_pushd_fswatch_delete_and_rename_events_stay_manual_review_actions(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    fixture = tmp_path / "fswatch-manual-review-events.txt"
    fixture.write_text(
        "\n".join(
            [
                "Documents/removed.pdf\tRemoved",
                "Documents/renamed.pdf\tRenamed",
            ]
        )
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "fswatch",
            "preview",
            "--fixture",
            str(fixture),
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    records = payload["details"]["planned upload records"]

    assert result.returncode == 0
    assert [record["action"] for record in records] == ["delete", "rename"]
    assert [record["reason"] for record in records] == ["fswatch:Removed", "fswatch:Renamed"]


def test_pushd_and_diffd_policy_reports_are_read_only(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])

    pushd_result = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "policy", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    diffd_result = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "diffd", "policy", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    pushd_payload = _payload(pushd_result)
    diffd_payload = _payload(diffd_result)

    assert pushd_result.returncode == 0
    assert diffd_result.returncode == 0
    assert pushd_payload["details"]["daemonization policy status"] == "documented-read-only"
    assert diffd_payload["details"]["daemonization policy status"] == "documented-read-only"
    assert pushd_payload["details"]["state writes"] == "none"
    assert diffd_payload["details"]["state writes"] == "none"
    assert "automatic upload/download transfer execution" in pushd_payload["details"]["blocked operations"]
    assert "automatic upload/download transfer execution" in diffd_payload["details"]["blocked operations"]
    assert "pushd.policy" in [action["id"] for action in pushd_payload["actions"]]
    assert "diffd.policy" in [action["id"] for action in diffd_payload["actions"]]
    assert not (state_dir / "pushd").exists()
    assert not (state_dir / "diffd").exists()


def test_pushd_fswatch_probe_is_preview_only(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])

    result = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "fswatch", "probe", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    assert result.returncode == 0
    assert payload["summary"] == "pushd fswatch one-shot probe preview is ready"
    assert payload["details"]["implementation status"] == "probe preview only; fswatch process is not started"
    assert payload["details"]["gate status"] == "closed"
    assert payload["details"]["state writes"] == "none"
    assert payload["details"]["fswatch command"][-1] == env["PCLOUD_TOOLS_WORKSPACE_ROOT"]
    assert "--one-event" in payload["details"]["fswatch command"]
    assert not (state_dir / "pushd").exists()


def test_pushd_fswatch_resident_gate_is_read_only_checklist(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fswatch = bin_dir / "fswatch"
    fswatch.write_text("#!/bin/sh\nexit 0\n")
    fswatch.chmod(0o755)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    shadow_workspace = tmp_path / "pcloud-shadow-validation-fswatch" / "workspace"
    shadow_report = tmp_path / "shadow-validation-fswatch.json"
    shadow_report.write_text(
        json.dumps(
            {
                "status": "ok",
                "workspace": str(shadow_workspace),
                "state_dir": str(shadow_workspace / ".dev-state" / "state"),
                "checks": [
                    {"name": "temporary workspace guard", "status": "ok"},
                    {"name": "temporary state dir guard", "status": "ok"},
                    {"name": "unsafe state dir guard", "status": "ok"},
                ],
            }
        )
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "fswatch",
            "resident-gate",
            "--report-path",
            str(shadow_report),
            "--operator-reviewed-probe",
            "--reviewer-approved-queue-policy",
            "--reviewer-approved-process-policy",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    checks = {check["name"]: check for check in payload["details"]["preflight checks"]}

    assert result.returncode == 0
    assert payload["status"] == "warning"
    assert payload["summary"] == "pushd fswatch resident gate is closed"
    assert payload["details"]["implementation status"].startswith("read-only checklist")
    assert payload["details"]["resident gate status"] == "closed"
    assert payload["details"]["resident can start"] == "no"
    assert payload["details"]["state writes"] == "none"
    assert payload["details"]["fswatch availability"] == "available"
    assert "--one-event" not in payload["details"]["resident command preview"]
    assert payload["details"]["resident command preview"][-1] == env["PCLOUD_TOOLS_WORKSPACE_ROOT"]
    assert payload["details"]["resident approval status"] == "complete-read-only"
    assert payload["details"]["human gate status"] == "required-before-resident-start"
    assert checks["saved shadow validation report"]["status"] == "ok"
    assert checks["fswatch binary"]["status"] == "ok"
    assert checks["operator probe review"]["status"] == "ok"
    assert checks["queue policy approval"]["status"] == "ok"
    assert checks["process lifecycle approval"]["status"] == "ok"
    assert "pushd.fswatch.resident-gate" in [action["id"] for action in payload["actions"]]
    assert not (state_dir / "pushd").exists()


def test_pushd_fswatch_resident_run_refuses_without_execution_gate(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = _use_default_dev_state_dir(env)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fswatch = bin_dir / "fswatch"
    fswatch.write_text("#!/bin/sh\nprintf 'Documents/from-fswatch.txt\\n'\n")
    fswatch.chmod(0o755)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    shadow_report = tmp_path / "shadow-validation-fswatch-run.json"
    shadow_workspace = tmp_path / "pcloud-shadow-validation-fswatch-run" / "workspace"
    shadow_report.write_text(
        json.dumps(
            {
                "status": "ok",
                "workspace": str(shadow_workspace),
                "state_dir": str(shadow_workspace / ".dev-state" / "state"),
                "checks": [
                    {"name": "temporary workspace guard", "status": "ok"},
                    {"name": "temporary state dir guard", "status": "ok"},
                    {"name": "unsafe state dir guard", "status": "ok"},
                ],
            }
        )
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "fswatch",
            "resident-run",
            "--report-path",
            str(shadow_report),
            "--operator-reviewed-probe",
            "--reviewer-approved-queue-policy",
            "--reviewer-approved-process-policy",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)

    assert result.returncode == 1
    assert payload["status"] == "error"
    assert payload["summary"] == "pushd fswatch resident execution is gated"
    assert payload["details"]["state writes"] == "none"
    assert payload["details"]["resident can start"] == "no"
    assert "PCLOUD_TOOLS_PUSHD_FSWATCH_RESIDENT_EXECUTION_GATE" in [
        issue["key"] for issue in payload["issues"]
    ]
    assert not (state_dir / "pushd" / "queue.json").exists()


def test_pushd_fswatch_resident_run_executes_fake_fswatch_in_dev_state(tmp_path: Path) -> None:
    env = _base_env(
        tmp_path,
        {"PCLOUD_TOOLS_PUSHD_FSWATCH_RESIDENT_GATE": "operator-approved-fswatch-resident-v1"},
    )
    state_dir = _use_default_dev_state_dir(env)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    (workspace / "Documents").mkdir()
    (workspace / "Documents" / "from-fswatch.txt").write_text("sample\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fswatch = bin_dir / "fswatch"
    fswatch.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$PCLOUD_TOOLS_WORKSPACE_ROOT/Documents/from-fswatch.txt\"\n"
    )
    fswatch.chmod(0o755)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    shadow_report = tmp_path / "shadow-validation-fswatch-run-ok.json"
    shadow_workspace = tmp_path / "pcloud-shadow-validation-fswatch-run-ok" / "workspace"
    shadow_report.write_text(
        json.dumps(
            {
                "status": "ok",
                "workspace": str(shadow_workspace),
                "state_dir": str(shadow_workspace / ".dev-state" / "state"),
                "checks": [
                    {"name": "temporary workspace guard", "status": "ok"},
                    {"name": "temporary state dir guard", "status": "ok"},
                    {"name": "unsafe state dir guard", "status": "ok"},
                ],
            }
        )
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "fswatch",
            "resident-run",
            "--report-path",
            str(shadow_report),
            "--operator-reviewed-probe",
            "--reviewer-approved-queue-policy",
            "--reviewer-approved-process-policy",
            "--max-events",
            "1",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    queue_payload = json.loads((state_dir / "pushd" / "queue.json").read_text())
    resident_state = json.loads((state_dir / "pushd" / "fswatch-resident-last-run.json").read_text())

    assert result.returncode == 0
    assert payload["status"] in {"ok", "warning"}
    assert payload["summary"] == "pushd fswatch resident run completed"
    assert payload["details"]["resident can start"] == "yes"
    assert payload["details"]["state writes"] == "pushd queue and resident run state"
    assert payload["details"]["queue records appended"] == 1
    assert queue_payload == [
        {"path": "Documents/from-fswatch.txt", "action": "upload", "reason": "fswatch"}
    ]
    assert resident_state["appended_records"] == queue_payload


def test_pushd_fswatch_resident_run_excludes_hidden_temp_paths(tmp_path: Path) -> None:
    env = _base_env(
        tmp_path,
        {"PCLOUD_TOOLS_PUSHD_FSWATCH_RESIDENT_GATE": "operator-approved-fswatch-resident-v1"},
    )
    state_dir = _use_default_dev_state_dir(env)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    (workspace / "Documents").mkdir()
    (workspace / "Documents" / ".temporary-upload").write_text("temp\n")
    (workspace / "Documents" / "incoming.jpeg.36b4106a.partial").write_text("partial\n")
    (workspace / "Documents" / "final-upload.txt").write_text("final\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fswatch = bin_dir / "fswatch"
    fswatch.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$PCLOUD_TOOLS_WORKSPACE_ROOT/Documents\"\n"
        "printf '%s\\n' \"$PCLOUD_TOOLS_WORKSPACE_ROOT/Documents/.temporary-upload\"\n"
        "printf '%s\\n' \"$PCLOUD_TOOLS_WORKSPACE_ROOT/Documents/incoming.jpeg.36b4106a.partial\"\n"
        "printf '%s\\n' \"$PCLOUD_TOOLS_WORKSPACE_ROOT/Documents/final-upload.txt\"\n"
    )
    fswatch.chmod(0o755)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    shadow_report = tmp_path / "shadow-validation-fswatch-hidden.json"
    shadow_workspace = tmp_path / "pcloud-shadow-validation-fswatch-hidden" / "workspace"
    shadow_report.write_text(
        json.dumps(
            {
                "status": "ok",
                "workspace": str(shadow_workspace),
                "state_dir": str(shadow_workspace / ".dev-state" / "state"),
                "checks": [
                    {"name": "temporary workspace guard", "status": "ok"},
                    {"name": "temporary state dir guard", "status": "ok"},
                    {"name": "unsafe state dir guard", "status": "ok"},
                ],
            }
        )
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "fswatch",
            "resident-run",
            "--report-path",
            str(shadow_report),
            "--operator-reviewed-probe",
            "--reviewer-approved-queue-policy",
            "--reviewer-approved-process-policy",
            "--max-events",
            "4",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    queue_payload = json.loads((state_dir / "pushd" / "queue.json").read_text())

    assert result.returncode == 0
    assert payload["details"]["queue records appended"] == 1
    assert payload["details"]["excluded events"] == 3
    assert [record["reason"] for record in payload["details"]["excluded event details"]] == [
        "directory upload not supported",
        "manager ignore rule",
        "partial transfer file",
    ]
    assert queue_payload == [
        {"path": "Documents/final-upload.txt", "action": "upload", "reason": "fswatch"}
    ]


def test_pushd_fswatch_resident_run_debounces_same_run_upload_events(tmp_path: Path) -> None:
    env = _base_env(
        tmp_path,
        {"PCLOUD_TOOLS_PUSHD_FSWATCH_RESIDENT_GATE": "operator-approved-fswatch-resident-v1"},
    )
    state_dir = _use_default_dev_state_dir(env)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    (workspace / "Documents").mkdir()
    (workspace / "Documents" / "from-fswatch.txt").write_text("sample\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fswatch = bin_dir / "fswatch"
    fswatch.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$PCLOUD_TOOLS_WORKSPACE_ROOT/Documents/from-fswatch.txt\"\n"
        "printf '%s\\n' \"$PCLOUD_TOOLS_WORKSPACE_ROOT/Documents/from-fswatch.txt\"\n"
    )
    fswatch.chmod(0o755)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    shadow_report = tmp_path / "shadow-validation-fswatch-dedupe.json"
    shadow_workspace = tmp_path / "pcloud-shadow-validation-fswatch-dedupe" / "workspace"
    shadow_report.write_text(
        json.dumps(
            {
                "status": "ok",
                "workspace": str(shadow_workspace),
                "state_dir": str(shadow_workspace / ".dev-state" / "state"),
                "checks": [
                    {"name": "temporary workspace guard", "status": "ok"},
                    {"name": "temporary state dir guard", "status": "ok"},
                    {"name": "unsafe state dir guard", "status": "ok"},
                ],
            }
        )
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "fswatch",
            "resident-run",
            "--report-path",
            str(shadow_report),
            "--operator-reviewed-probe",
            "--reviewer-approved-queue-policy",
            "--reviewer-approved-process-policy",
            "--max-events",
            "2",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    queue_payload = json.loads((state_dir / "pushd" / "queue.json").read_text())
    resident_state = json.loads((state_dir / "pushd" / "fswatch-resident-last-run.json").read_text())

    assert result.returncode == 0
    assert payload["details"]["events processed"] == 2
    assert payload["details"]["queue records appended"] == 1
    assert payload["details"]["duplicate events skipped"] == 0
    assert payload["details"]["debounce events skipped"] == 1
    assert payload["details"]["queue limit skips"] == 0
    assert queue_payload == [
        {"path": "Documents/from-fswatch.txt", "action": "upload", "reason": "fswatch"}
    ]
    assert resident_state["debounce_records"] == [
        {"path": "Documents/from-fswatch.txt", "action": "upload", "reason": "recent resident append"}
    ]


def test_pushd_fswatch_resident_run_skips_existing_duplicate_queue_records(tmp_path: Path) -> None:
    env = _base_env(
        tmp_path,
        {"PCLOUD_TOOLS_PUSHD_FSWATCH_RESIDENT_GATE": "operator-approved-fswatch-resident-v1"},
    )
    state_dir = _use_default_dev_state_dir(env)
    queue_file = state_dir / "pushd" / "queue.json"
    queue_file.parent.mkdir(parents=True)
    queue_file.write_text(
        json.dumps([{"path": "Documents/from-fswatch.txt", "action": "upload", "reason": "seed"}])
    )
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    (workspace / "Documents").mkdir()
    (workspace / "Documents" / "from-fswatch.txt").write_text("sample\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fswatch = bin_dir / "fswatch"
    fswatch.write_text("#!/bin/sh\nprintf '%s\\n' \"$PCLOUD_TOOLS_WORKSPACE_ROOT/Documents/from-fswatch.txt\"\n")
    fswatch.chmod(0o755)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    shadow_report = tmp_path / "shadow-validation-fswatch-duplicate-existing.json"
    shadow_workspace = tmp_path / "pcloud-shadow-validation-fswatch-duplicate-existing" / "workspace"
    shadow_report.write_text(
        json.dumps(
            {
                "status": "ok",
                "workspace": str(shadow_workspace),
                "state_dir": str(shadow_workspace / ".dev-state" / "state"),
                "checks": [
                    {"name": "temporary workspace guard", "status": "ok"},
                    {"name": "temporary state dir guard", "status": "ok"},
                    {"name": "unsafe state dir guard", "status": "ok"},
                ],
            }
        )
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "fswatch",
            "resident-run",
            "--report-path",
            str(shadow_report),
            "--operator-reviewed-probe",
            "--reviewer-approved-queue-policy",
            "--reviewer-approved-process-policy",
            "--max-events",
            "1",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    queue_payload = json.loads(queue_file.read_text())

    assert result.returncode == 0
    assert payload["details"]["queue records appended"] == 0
    assert payload["details"]["duplicate events skipped"] == 1
    assert payload["details"]["debounce events skipped"] == 0
    assert queue_payload == [{"path": "Documents/from-fswatch.txt", "action": "upload", "reason": "seed"}]


def test_pushd_fswatch_resident_run_debounces_recent_prior_run(tmp_path: Path) -> None:
    env = _base_env(
        tmp_path,
        {"PCLOUD_TOOLS_PUSHD_FSWATCH_RESIDENT_GATE": "operator-approved-fswatch-resident-v1"},
    )
    state_dir = _use_default_dev_state_dir(env)
    state_file = state_dir / "pushd" / "fswatch-resident-last-run.json"
    state_file.parent.mkdir(parents=True)
    state_file.write_text(
        json.dumps(
            {
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "appended_records": [
                    {"path": "Documents/recent.txt", "action": "upload", "reason": "fswatch"}
                ],
            }
        )
    )
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    (workspace / "Documents").mkdir()
    (workspace / "Documents" / "recent.txt").write_text("sample\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fswatch = bin_dir / "fswatch"
    fswatch.write_text("#!/bin/sh\nprintf '%s\\n' \"$PCLOUD_TOOLS_WORKSPACE_ROOT/Documents/recent.txt\"\n")
    fswatch.chmod(0o755)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    shadow_report = tmp_path / "shadow-validation-fswatch-prior-debounce.json"
    shadow_workspace = tmp_path / "pcloud-shadow-validation-fswatch-prior-debounce" / "workspace"
    shadow_report.write_text(
        json.dumps(
            {
                "status": "ok",
                "workspace": str(shadow_workspace),
                "state_dir": str(shadow_workspace / ".dev-state" / "state"),
                "checks": [
                    {"name": "temporary workspace guard", "status": "ok"},
                    {"name": "temporary state dir guard", "status": "ok"},
                    {"name": "unsafe state dir guard", "status": "ok"},
                ],
            }
        )
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "fswatch",
            "resident-run",
            "--report-path",
            str(shadow_report),
            "--operator-reviewed-probe",
            "--reviewer-approved-queue-policy",
            "--reviewer-approved-process-policy",
            "--max-events",
            "1",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)

    assert result.returncode == 0
    assert payload["details"]["queue records appended"] == 0
    assert payload["details"]["debounce events skipped"] == 1
    assert not (state_dir / "pushd" / "queue.json").exists()


def test_pushd_fswatch_resident_run_respects_queue_limit(tmp_path: Path) -> None:
    env = _base_env(
        tmp_path,
        {
            "PCLOUD_TOOLS_PUSHD_FSWATCH_RESIDENT_GATE": "operator-approved-fswatch-resident-v1",
            "PCLOUD_TOOLS_PUSHD_QUEUE_LIMIT": "1",
        },
    )
    state_dir = _use_default_dev_state_dir(env)
    queue_file = state_dir / "pushd" / "queue.json"
    queue_file.parent.mkdir(parents=True)
    queue_file.write_text(
        json.dumps([{"path": "Documents/existing.txt", "action": "upload", "reason": "seed"}])
    )
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    (workspace / "Documents").mkdir()
    (workspace / "Documents" / "new.txt").write_text("sample\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fswatch = bin_dir / "fswatch"
    fswatch.write_text("#!/bin/sh\nprintf '%s\\n' \"$PCLOUD_TOOLS_WORKSPACE_ROOT/Documents/new.txt\"\n")
    fswatch.chmod(0o755)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    shadow_report = tmp_path / "shadow-validation-fswatch-limit.json"
    shadow_workspace = tmp_path / "pcloud-shadow-validation-fswatch-limit" / "workspace"
    shadow_report.write_text(
        json.dumps(
            {
                "status": "ok",
                "workspace": str(shadow_workspace),
                "state_dir": str(shadow_workspace / ".dev-state" / "state"),
                "checks": [
                    {"name": "temporary workspace guard", "status": "ok"},
                    {"name": "temporary state dir guard", "status": "ok"},
                    {"name": "unsafe state dir guard", "status": "ok"},
                ],
            }
        )
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "fswatch",
            "resident-run",
            "--report-path",
            str(shadow_report),
            "--operator-reviewed-probe",
            "--reviewer-approved-queue-policy",
            "--reviewer-approved-process-policy",
            "--max-events",
            "1",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    queue_payload = json.loads(queue_file.read_text())

    assert result.returncode == 0
    assert payload["status"] == "warning"
    assert payload["details"]["queue records appended"] == 0
    assert payload["details"]["queue limit skips"] == 1
    assert queue_payload == [{"path": "Documents/existing.txt", "action": "upload", "reason": "seed"}]


def test_sync_autosync_gate_is_read_only_checklist(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    launchctl = bin_dir / "launchctl"
    launchctl.write_text("#!/bin/sh\nif [ \"$1\" = \"print\" ]; then exit 1; fi\nexit 0\n")
    launchctl.chmod(0o755)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    plist = workspace / ".dev-state" / "com.example.pcloud-bisync.dev.plist"
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text("<plist><dict/></plist>\n")
    shadow_workspace = tmp_path / "pcloud-shadow-validation-autosync" / "workspace"
    shadow_report = tmp_path / "shadow-validation-autosync.json"
    shadow_report.write_text(
        json.dumps(
            {
                "status": "ok",
                "workspace": str(shadow_workspace),
                "state_dir": str(shadow_workspace / ".dev-state" / "state"),
                "checks": [
                    {"name": "temporary workspace guard", "status": "ok"},
                    {"name": "temporary state dir guard", "status": "ok"},
                    {"name": "unsafe state dir guard", "status": "ok"},
                ],
            }
        )
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "sync",
            "autosync-gate",
            "--report-path",
            str(shadow_report),
            "--operator-reviewed-preview",
            "--reviewer-approved-plist",
            "--reviewer-approved-launchctl-policy",
            "--reviewer-approved-rollback-policy",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    checks = {check["name"]: check for check in payload["details"]["preflight checks"]}

    assert result.returncode == 0
    assert payload["status"] == "warning"
    assert payload["summary"] == "autosync launchd gate is closed"
    assert payload["details"]["implementation status"].startswith("read-only checklist")
    assert payload["details"]["launchd gate status"] == "closed"
    assert payload["details"]["autosync changes can run"] == "no"
    assert payload["details"]["state writes"] == "none"
    assert payload["details"]["autosync state"] == "not_loaded"
    assert payload["details"]["launchctl availability"] == "available"
    assert payload["details"]["autosync plist status"] == "present"
    assert payload["details"]["autosync plist review command"] == ["plutil", "-p", str(plist)]
    assert payload["details"]["autosync approval status"] == "complete-read-only"
    assert payload["details"]["human gate status"] == "required-before-autosync-launchd-change"
    assert checks["saved shadow validation report"]["status"] == "ok"
    assert checks["launchctl binary"]["status"] == "ok"
    assert checks["autosync plist"]["status"] == "ok"
    assert checks["operator preview review"]["status"] == "ok"
    assert checks["plist approval"]["status"] == "ok"
    assert checks["launchctl policy approval"]["status"] == "ok"
    assert checks["rollback policy approval"]["status"] == "ok"
    assert "sync.autosync.gate" in [action["id"] for action in payload["actions"]]
    assert not any(state_dir.iterdir())


def test_sync_autosync_gate_missing_plist_reports_review_command(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "sync",
            "autosync-gate",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    plist = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"]) / ".dev-state" / "com.example.pcloud-bisync.dev.plist"

    assert result.returncode == 0
    assert payload["status"] == "warning"
    assert payload["details"]["autosync plist status"] == "missing"
    assert payload["details"]["autosync plist review command"] == ["plutil", "-p", str(plist)]
    assert "autosync-gate does not write it" in payload["details"]["autosync plist note"]
    assert "PCLOUD_TOOLS_AUTOSYNC_PLIST" in [issue["key"] for issue in payload["issues"]]


def test_sync_autosync_run_refuses_without_execution_gate(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = _use_default_dev_state_dir(env)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "launchctl.log"
    launchctl = bin_dir / "launchctl"
    launchctl.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$LAUNCHCTL_LOG\"\n"
        "if [ \"$1\" = \"print\" ]; then exit 1; fi\n"
        "exit 0\n"
    )
    launchctl.chmod(0o755)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["LAUNCHCTL_LOG"] = str(log)
    plist = workspace / ".dev-state" / "com.example.pcloud-bisync.dev.plist"
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text("<plist><dict/></plist>\n")
    shadow_report = tmp_path / "shadow-validation-autosync-run.json"
    shadow_workspace = tmp_path / "pcloud-shadow-validation-autosync-run" / "workspace"
    shadow_report.write_text(
        json.dumps(
            {
                "status": "ok",
                "workspace": str(shadow_workspace),
                "state_dir": str(shadow_workspace / ".dev-state" / "state"),
                "checks": [
                    {"name": "temporary workspace guard", "status": "ok"},
                    {"name": "temporary state dir guard", "status": "ok"},
                    {"name": "unsafe state dir guard", "status": "ok"},
                ],
            }
        )
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "sync",
            "autosync-run",
            "enable",
            "--report-path",
            str(shadow_report),
            "--operator-reviewed-preview",
            "--reviewer-approved-plist",
            "--reviewer-approved-launchctl-policy",
            "--reviewer-approved-rollback-policy",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)

    assert result.returncode == 1
    assert payload["status"] == "error"
    assert payload["summary"] == "autosync launchd execution is gated"
    assert payload["details"]["state writes"] == "none"
    assert payload["details"]["autosync changes can run"] == "no"
    assert "PCLOUD_TOOLS_AUTOSYNC_LAUNCHD_EXECUTION_GATE" in [issue["key"] for issue in payload["issues"]]
    assert "enable " not in log.read_text()
    assert not (state_dir / "sync" / "autosync-launchd-last-run.json").exists()


def test_sync_autosync_run_executes_fake_launchctl_in_dev_state(tmp_path: Path) -> None:
    env = _base_env(
        tmp_path,
        {"PCLOUD_TOOLS_AUTOSYNC_LAUNCHD_GATE": "operator-approved-autosync-launchd-v1"},
    )
    state_dir = _use_default_dev_state_dir(env)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "launchctl.log"
    launchctl = bin_dir / "launchctl"
    launchctl.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$LAUNCHCTL_LOG\"\n"
        "if [ \"$1\" = \"print\" ]; then exit 1; fi\n"
        "exit 0\n"
    )
    launchctl.chmod(0o755)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["LAUNCHCTL_LOG"] = str(log)
    plist = workspace / ".dev-state" / "com.example.pcloud-bisync.dev.plist"
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text("<plist><dict/></plist>\n")
    shadow_report = tmp_path / "shadow-validation-autosync-run-ok.json"
    shadow_workspace = tmp_path / "pcloud-shadow-validation-autosync-run-ok" / "workspace"
    shadow_report.write_text(
        json.dumps(
            {
                "status": "ok",
                "workspace": str(shadow_workspace),
                "state_dir": str(shadow_workspace / ".dev-state" / "state"),
                "checks": [
                    {"name": "temporary workspace guard", "status": "ok"},
                    {"name": "temporary state dir guard", "status": "ok"},
                    {"name": "unsafe state dir guard", "status": "ok"},
                ],
            }
        )
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "sync",
            "autosync-run",
            "enable",
            "--report-path",
            str(shadow_report),
            "--operator-reviewed-preview",
            "--reviewer-approved-plist",
            "--reviewer-approved-launchctl-policy",
            "--reviewer-approved-rollback-policy",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    launchctl_log = log.read_text()
    run_state = json.loads((state_dir / "sync" / "autosync-launchd-last-run.json").read_text())

    assert result.returncode == 0
    assert payload["status"] in {"ok", "warning"}
    assert payload["summary"] == "autosync launchd run completed"
    assert payload["details"]["autosync changes can run"] == "yes"
    assert payload["details"]["state writes"] == "autosync launchd run state"
    assert "enable gui/" in launchctl_log
    assert "bootstrap gui/" in launchctl_log
    assert run_state["mode"] == "enable"
    assert len(run_state["commands"]) == 2


def test_sync_autosync_plist_preview_and_dev_execute_are_guarded(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    dev_entrypoint = workspace / "pcloud-manager-dev"
    dev_entrypoint.write_text("#!/bin/sh\nexit 0\n")
    dev_entrypoint.chmod(0o755)
    plist = workspace / ".dev-state" / "com.example.pcloud-bisync.dev.plist"

    preview = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "sync", "autosync-plist", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    preview_payload = _payload(preview)

    assert preview.returncode == 0
    assert preview_payload["status"] in {"ok", "warning"}
    assert preview_payload["details"]["state writes"] == "none"
    assert preview_payload["details"]["launchctl execution"] == "no"
    assert not plist.exists()

    written = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "sync", "autosync-plist", "--execute", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    written_payload = _payload(written)
    plist_payload = plistlib.loads(plist.read_bytes())

    assert written.returncode == 0
    assert written_payload["status"] in {"ok", "warning"}
    assert written_payload["summary"] == "autosync plist written"
    assert written_payload["details"]["state writes"] == "autosync plist only"
    assert written_payload["details"]["scheduled sync execution"] == "no"
    assert plist_payload["Label"] == "com.example.pcloud-bisync.dev"
    assert plist_payload["ProgramArguments"] == [
        str(dev_entrypoint),
        "sync",
        "background",
        "--execute",
    ]


def test_sync_autosync_plist_execute_refuses_outside_dev_state(tmp_path: Path) -> None:
    env = _base_env(
        tmp_path,
        {"PCLOUD_TOOLS_AUTOSYNC_PLIST": str(tmp_path / "outside.plist")},
    )
    result = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "sync", "autosync-plist", "--execute", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)

    assert result.returncode == 1
    assert payload["status"] == "error"
    assert payload["details"]["state writes"] == "none"
    assert not (tmp_path / "outside.plist").exists()
    assert "PCLOUD_TOOLS_AUTOSYNC_PLIST_PATH" in [issue["key"] for issue in payload["issues"]]


def test_sync_migration_gate_is_read_only_checklist(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    rclone = bin_dir / "rclone"
    rclone.write_text("#!/bin/sh\nexit 0\n")
    rclone.chmod(0o755)
    env = _base_env(tmp_path, {"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"})
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    (workspace / "bisync_status.log").write_text("2026-05-04 12:00:00 SUCCESS mode=autosync\n")
    shadow_workspace = tmp_path / "pcloud-shadow-validation-migration" / "workspace"
    shadow_report = tmp_path / "shadow-validation-migration.json"
    shadow_report.write_text(
        json.dumps(
            {
                "status": "ok",
                "workspace": str(shadow_workspace),
                "state_dir": str(shadow_workspace / ".dev-state" / "state"),
                "checks": [
                    {"name": "temporary workspace guard", "status": "ok"},
                    {"name": "temporary state dir guard", "status": "ok"},
                    {"name": "unsafe state dir guard", "status": "ok"},
                ],
            }
        )
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "sync",
            "migration-gate",
            "--report-path",
            str(shadow_report),
            "--operator-reviewed-status",
            "--reviewer-approved-scope",
            "--reviewer-approved-rollback-policy",
            "--reviewer-approved-stop-conditions",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    checks = {check["name"]: check for check in payload["details"]["preflight checks"]}

    assert result.returncode == 0
    assert payload["status"] == "warning"
    assert payload["summary"] == "sync migration validation gate is closed"
    assert payload["details"]["implementation status"].startswith("read-only checklist")
    assert payload["details"]["migration gate status"] == "closed"
    assert payload["details"]["sync/resync can run"] == "no"
    assert payload["details"]["state writes"] == "none"
    assert payload["details"]["sync state"] == "synced"
    assert payload["details"]["last error status"] == "none"
    assert payload["details"]["sync lock status"] == "missing"
    assert payload["details"]["scope status"] == "loaded"
    assert payload["details"]["rclone availability"] == "available"
    assert payload["details"]["migration approval status"] == "complete-read-only"
    assert payload["details"]["human gate status"] == "required-before-sync-migration-validation"
    assert checks["saved shadow validation report"]["status"] == "ok"
    assert checks["rclone binary"]["status"] == "ok"
    assert checks["latest sync status"]["status"] == "ok"
    assert checks["sync lock"]["status"] == "ok"
    assert checks["document/media scope"]["status"] == "ok"
    assert checks["operator status review"]["status"] == "ok"
    assert checks["scope approval"]["status"] == "ok"
    assert checks["rollback policy approval"]["status"] == "ok"
    assert checks["stop conditions approval"]["status"] == "ok"
    assert "sync.migration.gate" in [action["id"] for action in payload["actions"]]
    assert not any(state_dir.iterdir())


def test_sync_migration_gate_can_use_saved_sync_status_report(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    rclone = bin_dir / "rclone"
    rclone.write_text("#!/bin/sh\nexit 0\n")
    rclone.chmod(0o755)
    env = _base_env(tmp_path, {"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"})
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    (workspace / "bisync_status.log").write_text("2026-04-24 15:43:23 ERROR mode=resync\n")
    shadow_workspace = tmp_path / "pcloud-shadow-validation-migration-status" / "workspace"
    shadow_report = tmp_path / "shadow-validation-migration-status.json"
    shadow_report.write_text(
        json.dumps(
            {
                "status": "ok",
                "workspace": str(shadow_workspace),
                "state_dir": str(shadow_workspace / ".dev-state" / "state"),
                "checks": [
                    {"name": "temporary workspace guard", "status": "ok"},
                    {"name": "temporary state dir guard", "status": "ok"},
                    {"name": "unsafe state dir guard", "status": "ok"},
                ],
            }
        )
    )
    status_report = tmp_path / "sync-status.json"
    status_report.write_text(
        json.dumps(
            {
                "command": "sync status",
                "status": "ok",
                "details": {
                    "sync state": "synced",
                    "last result": "2026-05-04 12:00:00 SUCCESS mode=autosync",
                    "last error": "2026-04-30 10:54:28 historical failure",
                    "last error status": "historical",
                    "sync lock status": "missing",
                    "sync lock active": "no",
                    "sync lock pid": "-",
                    "sync lock mode": "-",
                    "sync lock started": "-",
                    "scope status": "loaded",
                    "scope entries": 4,
                    "last resync scope": "allowlist",
                    "allowlist": str(workspace / ".pcloud-sync-allowlist"),
                    "autosync state": "active",
                    "autosync runs": "7",
                },
            }
        )
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "sync",
            "migration-gate",
            "--report-path",
            str(shadow_report),
            "--sync-status-report-path",
            str(status_report),
            "--operator-reviewed-status",
            "--reviewer-approved-scope",
            "--reviewer-approved-rollback-policy",
            "--reviewer-approved-stop-conditions",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    checks = {check["name"]: check for check in payload["details"]["preflight checks"]}

    assert result.returncode == 0
    assert payload["details"]["sync status source"] == "saved sync status report"
    assert payload["details"]["sync state"] == "synced"
    assert payload["details"]["sync lock status"] == "missing"
    assert payload["details"]["scope entries"] == 4
    assert payload["details"]["migration target root status"] == "ok"
    assert payload["details"]["migration approval status"] == "complete-read-only"
    assert checks["saved sync status report"]["status"] == "ok"
    assert checks["latest sync status"]["status"] == "ok"
    assert checks["sync lock"]["status"] == "ok"
    assert checks["migration target root"]["status"] == "ok"


def test_sync_migration_gate_blocks_saved_status_target_root_mismatch(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    rclone = bin_dir / "rclone"
    rclone.write_text("#!/bin/sh\nexit 0\n")
    rclone.chmod(0o755)
    env = _base_env(tmp_path, {"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"})
    shadow_workspace = tmp_path / "pcloud-shadow-validation-migration-mismatch" / "workspace"
    shadow_report = tmp_path / "shadow-validation-migration-mismatch.json"
    shadow_report.write_text(
        json.dumps(
            {
                "status": "ok",
                "workspace": str(shadow_workspace),
                "state_dir": str(shadow_workspace / ".dev-state" / "state"),
                "checks": [
                    {"name": "temporary workspace guard", "status": "ok"},
                    {"name": "temporary state dir guard", "status": "ok"},
                    {"name": "unsafe state dir guard", "status": "ok"},
                ],
            }
        )
    )
    status_report = tmp_path / "sync-status-mismatch.json"
    status_report.write_text(
        json.dumps(
            {
                "command": "sync status",
                "status": "ok",
                "details": {
                    "sync state": "synced",
                    "last result": "2026-05-04 12:00:00 SUCCESS mode=autosync",
                    "last error status": "historical",
                    "sync lock status": "missing",
                    "sync lock active": "no",
                    "scope status": "loaded",
                    "scope entries": 4,
                    "last resync scope": "allowlist",
                    "allowlist": "/Users/takafumi/p-core/.pcloud-sync-allowlist",
                },
            }
        )
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "sync",
            "migration-gate",
            "--report-path",
            str(shadow_report),
            "--sync-status-report-path",
            str(status_report),
            "--operator-reviewed-status",
            "--reviewer-approved-scope",
            "--reviewer-approved-rollback-policy",
            "--reviewer-approved-stop-conditions",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    checks = {check["name"]: check for check in payload["details"]["preflight checks"]}

    assert result.returncode == 0
    assert payload["details"]["migration approval status"] == "pending"
    assert payload["details"]["migration target root status"] == "mismatch"
    assert payload["details"]["sync/resync can run"] == "no"
    assert checks["migration target root"]["status"] == "pending"
    assert "PCLOUD_TOOLS_SYNC_MIGRATION_TARGET_ROOT" in [issue["key"] for issue in payload["issues"]]


def test_sync_migration_run_refuses_without_execution_gate(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "rclone.log"
    rclone = bin_dir / "rclone"
    rclone.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$RCLONE_LOG\"\nexit 0\n")
    rclone.chmod(0o755)
    env = _base_env(tmp_path, {"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"})
    env["RCLONE_LOG"] = str(log)
    state_dir = _use_default_dev_state_dir(env)
    shadow_report = tmp_path / "shadow-validation-migration-run.json"
    shadow_workspace = tmp_path / "pcloud-shadow-validation-migration-run" / "workspace"
    shadow_report.write_text(
        json.dumps(
            {
                "status": "ok",
                "workspace": str(shadow_workspace),
                "state_dir": str(shadow_workspace / ".dev-state" / "state"),
                "checks": [
                    {"name": "temporary workspace guard", "status": "ok"},
                    {"name": "temporary state dir guard", "status": "ok"},
                    {"name": "unsafe state dir guard", "status": "ok"},
                ],
            }
        )
    )
    status_report = tmp_path / "sync-status.json"
    status_report.write_text(
        json.dumps(
            {
                "command": "sync status",
                "details": {
                    "sync state": "synced",
                    "last result": "2026-05-04 12:00:00 SUCCESS mode=autosync",
                    "last error status": "historical",
                    "sync lock status": "missing",
                    "sync lock active": "no",
                    "scope status": "loaded",
                    "scope entries": 4,
                    "last resync scope": "allowlist",
                },
            }
        )
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "sync",
            "migration-run",
            "normal",
            "--report-path",
            str(shadow_report),
            "--sync-status-report-path",
            str(status_report),
            "--operator-reviewed-status",
            "--reviewer-approved-scope",
            "--reviewer-approved-rollback-policy",
            "--reviewer-approved-stop-conditions",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)

    assert result.returncode == 1
    assert payload["status"] == "error"
    assert payload["summary"] == "sync migration execution is gated"
    assert payload["details"]["state writes"] == "none"
    assert payload["details"]["sync/resync can run"] == "no"
    assert "PCLOUD_TOOLS_SYNC_MIGRATION_EXECUTION_GATE" in [issue["key"] for issue in payload["issues"]]
    assert not log.exists()
    assert not (state_dir / "sync" / "migration-last-run.json").exists()


def test_sync_migration_run_refuses_with_rclone_bisync_lock(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "rclone.log"
    rclone = bin_dir / "rclone"
    rclone.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$RCLONE_LOG\"\nexit 0\n")
    rclone.chmod(0o755)
    env = _base_env(
        tmp_path,
        {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "PCLOUD_TOOLS_SYNC_MIGRATION_GATE": "operator-approved-sync-migration-v1",
        },
    )
    env["RCLONE_LOG"] = str(log)
    state_dir = _use_default_dev_state_dir(env)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    lock_name = f"local_{str(workspace).replace('/', '_')}..pcloud_core.lck"
    lock_file = Path(env["XDG_CACHE_HOME"]) / "rclone" / "bisync" / lock_name
    lock_file.parent.mkdir(parents=True)
    lock_file.write_text(
        json.dumps(
            {
                "Session": str(lock_file.with_suffix("")),
                "PID": "999999",
                "TimeRenewed": "2026-04-24T15:42:31+09:00",
                "TimeExpires": "2226-03-07T15:42:31+09:00",
            }
        )
    )
    shadow_report = tmp_path / "shadow-validation-migration-run-lock.json"
    shadow_workspace = tmp_path / "pcloud-shadow-validation-migration-run-lock" / "workspace"
    shadow_report.write_text(
        json.dumps(
            {
                "status": "ok",
                "workspace": str(shadow_workspace),
                "state_dir": str(shadow_workspace / ".dev-state" / "state"),
                "checks": [
                    {"name": "temporary workspace guard", "status": "ok"},
                    {"name": "temporary state dir guard", "status": "ok"},
                    {"name": "unsafe state dir guard", "status": "ok"},
                ],
            }
        )
    )
    status_report = tmp_path / "sync-status-lock.json"
    status_report.write_text(
        json.dumps(
            {
                "command": "sync status",
                "details": {
                    "sync state": "synced",
                    "last result": "2026-05-04 12:00:00 SUCCESS mode=autosync",
                    "last error status": "historical",
                    "sync lock status": "missing",
                    "sync lock active": "no",
                    "scope status": "loaded",
                    "scope entries": 4,
                    "last resync scope": "allowlist",
                },
            }
        )
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "sync",
            "migration-run",
            "normal",
            "--report-path",
            str(shadow_report),
            "--sync-status-report-path",
            str(status_report),
            "--operator-reviewed-status",
            "--reviewer-approved-scope",
            "--reviewer-approved-rollback-policy",
            "--reviewer-approved-stop-conditions",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    checks = {check["name"]: check for check in payload["details"]["preflight checks"]}

    assert result.returncode == 1
    assert payload["status"] == "error"
    assert payload["details"]["state writes"] == "none"
    assert payload["details"]["sync/resync can run"] == "no"
    assert payload["details"]["rclone bisync lock status"] == "present"
    assert payload["details"]["rclone bisync lock process active"] == "no"
    assert checks["rclone bisync lock"]["status"] == "pending"
    assert "PCLOUD_TOOLS_SYNC_MIGRATION_RCLONE_LOCK" in [issue["key"] for issue in payload["issues"]]
    assert not log.exists()
    assert not (state_dir / "sync" / "migration-last-run.json").exists()


def test_sync_migration_run_executes_fake_rclone_in_dev_state(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "rclone.log"
    rclone = bin_dir / "rclone"
    rclone.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$RCLONE_LOG\"\nexit 0\n")
    rclone.chmod(0o755)
    env = _base_env(
        tmp_path,
        {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "PCLOUD_TOOLS_SYNC_MIGRATION_GATE": "operator-approved-sync-migration-v1",
        },
    )
    env["RCLONE_LOG"] = str(log)
    state_dir = _use_default_dev_state_dir(env)
    shadow_report = tmp_path / "shadow-validation-migration-run-ok.json"
    shadow_workspace = tmp_path / "pcloud-shadow-validation-migration-run-ok" / "workspace"
    shadow_report.write_text(
        json.dumps(
            {
                "status": "ok",
                "workspace": str(shadow_workspace),
                "state_dir": str(shadow_workspace / ".dev-state" / "state"),
                "checks": [
                    {"name": "temporary workspace guard", "status": "ok"},
                    {"name": "temporary state dir guard", "status": "ok"},
                    {"name": "unsafe state dir guard", "status": "ok"},
                ],
            }
        )
    )
    status_report = tmp_path / "sync-status-ok.json"
    status_report.write_text(
        json.dumps(
            {
                "command": "sync status",
                "details": {
                    "sync state": "synced",
                    "last result": "2026-05-04 12:00:00 SUCCESS mode=autosync",
                    "last error status": "historical",
                    "sync lock status": "missing",
                    "sync lock active": "no",
                    "scope status": "loaded",
                    "scope entries": 4,
                    "last resync scope": "allowlist",
                },
            }
        )
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "sync",
            "migration-run",
            "normal",
            "--report-path",
            str(shadow_report),
            "--sync-status-report-path",
            str(status_report),
            "--operator-reviewed-status",
            "--reviewer-approved-scope",
            "--reviewer-approved-rollback-policy",
            "--reviewer-approved-stop-conditions",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    run_state = json.loads((state_dir / "sync" / "migration-last-run.json").read_text())
    status_log = (Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"]) / "bisync_status.log").read_text()

    assert result.returncode == 0
    assert payload["status"] in {"ok", "warning"}
    assert payload["summary"] == "sync migration run completed"
    assert payload["details"]["sync/resync can run"] == "yes"
    assert payload["details"]["state writes"] == "sync logs, lock, status, and migration run state"
    assert "bisync" in log.read_text()
    assert "SUCCESS mode=normal" in status_log
    assert run_state["mode"] == "normal"
    assert run_state["exit_code"] == 0


def test_archive_old_monolith_gate_is_read_only_checklist(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    backup_dir = workspace / ".dev-state" / "cutover-backups" / "20260426-040551"
    backup_dir.mkdir(parents=True)
    legacy_backup = backup_dir / "pcloud-manager.current"
    legacy_backup.write_text("#!/bin/zsh\nPCLOUD_MANAGER_CONFIG=\"${HOME}/.config/pcloud-manager/config.zsh\"\n")
    (backup_dir / "shadow-validation.json").write_text(json.dumps({"status": "ok", "checks": []}))

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "archive",
            "old-monolith-gate",
            "--backup-dir",
            str(backup_dir),
            "--operator-reviewed-current-wrapper",
            "--reviewer-approved-backup-source",
            "--reviewer-approved-rollback-policy",
            "--reviewer-approved-archive-target",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    checks = {check["name"]: check for check in payload["details"]["preflight checks"]}

    assert result.returncode == 0
    assert payload["status"] == "warning"
    assert payload["summary"] == "old monolith archive gate is closed"
    assert payload["details"]["archive gate status"] == "closed"
    assert payload["details"]["archive can run"] == "no"
    assert payload["details"]["state writes"] == "none"
    assert payload["details"]["legacy backup status"] == "monolith-backup"
    assert payload["details"]["archive approval status"] == "complete-read-only"
    assert payload["details"]["human gate status"] == "required-before-old-monolith-archive"
    assert checks["legacy monolith backup"]["status"] == "ok"
    assert checks["backup source approval"]["status"] == "ok"
    assert "archive.old-monolith.gate" in [action["id"] for action in payload["actions"]]
    assert not (workspace / ".dev-state" / "old-monolith-archive").exists()


def test_archive_old_monolith_gate_missing_backup_stays_pending(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    result = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "archive", "old-monolith-gate", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)

    assert result.returncode == 0
    assert payload["status"] == "warning"
    assert payload["details"]["archive gate status"] == "closed"
    assert payload["details"]["archive can run"] == "no"
    assert payload["details"]["legacy backup status"] == "missing-or-unrecognized"
    assert payload["details"]["archive approval status"] == "pending"
    assert "PCLOUD_TOOLS_ARCHIVE_LEGACY_BACKUP" in [issue["key"] for issue in payload["issues"]]


def test_archive_old_monolith_run_refuses_without_execution_gate(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    backup_dir = workspace / ".dev-state" / "cutover-backups" / "20260426-040551"
    backup_dir.mkdir(parents=True)
    (backup_dir / "pcloud-manager.current").write_text(
        '#!/bin/zsh\nPCLOUD_MANAGER_CONFIG="${HOME}/.config/pcloud-manager/config.zsh"\n'
    )
    (backup_dir / "shadow-validation.json").write_text(json.dumps({"status": "ok", "checks": []}))

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "archive",
            "old-monolith-run",
            "--backup-dir",
            str(backup_dir),
            "--operator-reviewed-current-wrapper",
            "--reviewer-approved-backup-source",
            "--reviewer-approved-rollback-policy",
            "--reviewer-approved-archive-target",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)

    assert result.returncode == 1
    assert payload["status"] == "error"
    assert payload["summary"] == "old monolith archive execution is gated"
    assert payload["details"]["state writes"] == "none"
    assert payload["details"]["archive can run"] == "no"
    assert "PCLOUD_TOOLS_OLD_MONOLITH_ARCHIVE_EXECUTION_GATE" in [
        issue["key"] for issue in payload["issues"]
    ]
    assert not (workspace / ".dev-state" / "old-monolith-archive").exists()


def test_archive_old_monolith_run_copies_backup_to_dev_archive(tmp_path: Path) -> None:
    env = _base_env(
        tmp_path,
        {"PCLOUD_TOOLS_OLD_MONOLITH_ARCHIVE_GATE": "operator-approved-old-monolith-archive-v1"},
    )
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    backup_dir = workspace / ".dev-state" / "cutover-backups" / "20260426-040551"
    backup_dir.mkdir(parents=True)
    legacy_text = '#!/bin/zsh\nPCLOUD_MANAGER_CONFIG="${HOME}/.config/pcloud-manager/config.zsh"\n'
    shadow_payload = {"status": "ok", "checks": [{"name": "sample", "status": "ok"}]}
    legacy_backup = backup_dir / "pcloud-manager.current"
    shadow_backup = backup_dir / "shadow-validation.json"
    legacy_backup.write_text(legacy_text)
    shadow_backup.write_text(json.dumps(shadow_payload))

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "archive",
            "old-monolith-run",
            "--backup-dir",
            str(backup_dir),
            "--operator-reviewed-current-wrapper",
            "--reviewer-approved-backup-source",
            "--reviewer-approved-rollback-policy",
            "--reviewer-approved-archive-target",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    archive_target = workspace / ".dev-state" / "old-monolith-archive" / "20260426-040551"
    archived_legacy = archive_target / "pcloud-manager.current"
    archived_shadow = archive_target / "shadow-validation.json"
    manifest = json.loads((archive_target / "archive-manifest.json").read_text())

    assert result.returncode == 0
    assert payload["status"] in {"ok", "warning"}
    assert payload["summary"] == "old monolith archive run completed"
    assert payload["details"]["archive can run"] == "yes"
    assert payload["details"]["state writes"] == "archive target copy and manifest"
    assert archived_legacy.read_text() == legacy_text
    assert json.loads(archived_shadow.read_text()) == shadow_payload
    assert manifest["public_wrapper_modified"] is False
    assert manifest["source_backup_retained"] is True
    assert legacy_backup.exists()


def test_gates_status_summarizes_remaining_gates_without_writes(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    backup_dir = workspace / ".dev-state" / "cutover-backups" / "20260426-040551"
    backup_dir.mkdir(parents=True)
    (backup_dir / "pcloud-manager.current").write_text(
        '#!/bin/zsh\nPCLOUD_MANAGER_CONFIG="${HOME}/.config/pcloud-manager/config.zsh"\n'
    )
    (backup_dir / "shadow-validation.json").write_text(json.dumps({"status": "ok", "checks": []}))
    (workspace / ".dev-state" / "com.example.pcloud-bisync.dev.plist").write_text("<plist><dict/></plist>\n")
    shadow_workspace = tmp_path / "pcloud-shadow-validation-gates" / "workspace"
    shadow_report = tmp_path / "shadow-validation-gates.json"
    shadow_report.write_text(
        json.dumps(
            {
                "status": "ok",
                "workspace": str(shadow_workspace),
                "state_dir": str(shadow_workspace / ".dev-state" / "state"),
                "checks": [
                    {"name": "temporary workspace guard", "status": "ok"},
                    {"name": "temporary state dir guard", "status": "ok"},
                    {"name": "unsafe state dir guard", "status": "ok"},
                ],
            }
        )
    )
    status_report = tmp_path / "sync-status-gates.json"
    status_report.write_text(
        json.dumps(
            {
                "command": "sync status",
                "status": "ok",
                "details": {
                    "sync state": "synced",
                    "last result": "2026-05-04 12:00:00 SUCCESS mode=autosync",
                    "last error": "(none)",
                    "last error status": "none",
                    "sync lock status": "missing",
                    "sync lock active": "no",
                    "scope status": "loaded",
                    "scope entries": 4,
                    "last resync scope": "allowlist",
                    "allowlist": str(workspace / ".pcloud-sync-allowlist"),
                    "autosync state": "active",
                    "autosync runs": "7",
                },
            }
        )
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "gates",
            "status",
            "--report-path",
            str(shadow_report),
            "--sync-status-report-path",
            str(status_report),
            "--backup-dir",
            str(backup_dir),
            "--assume-read-only-approvals",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    gates = {item["name"]: item for item in payload["details"]["gates"]}

    assert result.returncode == 0
    assert payload["status"] == "warning"
    assert payload["details"]["implementation status"].startswith("read-only aggregate")
    assert payload["details"]["state writes"] == "none"
    assert payload["details"]["assume read-only approvals"] == "yes"
    assert payload["details"]["sync status report"] == str(status_report)
    assert payload["details"]["gate count"] == 5
    assert payload["details"]["complete read-only approvals"] == 5
    assert gates["pushd fswatch resident"]["gate status"] == "closed"
    assert gates["diffd pCloud API long-poll"]["can run"] == "no"
    assert gates["old monolith archive"]["approval status"] == "complete-read-only"
    assert "sync autosync launchd" in gates
    assert gates["sync migration validation"]["approval status"] == "complete-read-only"
    assert gates["pushd fswatch resident"]["guarded run path"] == "available"
    assert gates["diffd pCloud API long-poll"]["run command"] == ["diffd", "api-poll", "long-poll-run"]
    assert gates["sync autosync launchd"]["execution gate env"].startswith("PCLOUD_TOOLS_AUTOSYNC")
    assert "old monolith archive" in payload["details"]["guarded run paths"]
    assert "--execute" not in payload["details"]["read-only command examples"]["pushd fswatch resident"][0]
    assert "sync migration-run normal" in payload["details"]["read-only command examples"]["sync migration validation"][0]
    assert not any(state_dir.iterdir())
    assert not (workspace / ".dev-state" / "old-monolith-archive").exists()


def test_gates_status_human_output_is_concise(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    result = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "gates", "status"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    assert result.returncode == 0
    assert "gates status: warning" in result.stdout
    assert "state writes: none" in result.stdout
    assert "pushd fswatch resident" in result.stdout
    assert "diffd pCloud API long-poll" in result.stdout
    assert "old monolith archive" in result.stdout
    assert "run=archive old-monolith-run" in result.stdout
    assert "read-only command examples:" not in result.stdout


def test_gates_status_human_output_can_show_read_only_command_examples(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "gates",
            "status",
            "--show-command-examples",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    assert result.returncode == 0
    assert "read-only command examples:" in result.stdout
    assert "PCLOUD_TOOLS_PUSHD_FSWATCH_RESIDENT_GATE=operator-approved-fswatch-resident-v1" in result.stdout
    assert "./pcloud-manager-dev pushd fswatch resident-run" in result.stdout
    assert "--execute" not in result.stdout


def test_gates_status_xbar_is_concise_and_safe(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    result = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "gates", "status", "--xbar"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    assert result.returncode == 0
    assert "pCloud WARN" in result.stdout
    assert "gates: complete=" in result.stdout
    assert "state writes: none" in result.stdout
    assert "gate summary:" in result.stdout
    assert "pushd fswatch resident: gate=closed" in result.stdout
    assert "diffd pCloud API long-poll: gate=closed" in result.stdout
    assert "old monolith archive: gate=closed" in result.stdout
    assert "Refresh gates" in result.stdout
    assert "Pushd status" in result.stdout
    assert "Diffd status" in result.stdout
    assert "Sync status" in result.stdout
    assert "read-only command examples" not in result.stdout
    assert "PCLOUD_TOOLS_PUSHD_FSWATCH_RESIDENT_GATE" not in result.stdout
    assert "guarded run paths" not in result.stdout
    assert "--execute" not in result.stdout


def test_diffd_preview_builds_remote_and_pending_download_plan(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    diffd_dir = state_dir / "diffd"
    daemon_dir = state_dir / "daemon"
    diffd_dir.mkdir(parents=True)
    daemon_dir.mkdir(parents=True)
    (diffd_dir / "remote-changes.json").write_text(
        json.dumps(
            [
                {"path": "Documents/remote.pdf", "action": "download"},
                {"path": "", "action": "download"},
            ]
        )
    )
    (daemon_dir / "pending-downloads.json").write_text(
        json.dumps(
            [
                {
                    "path": "Documents/pending.pdf",
                    "diffid": "123",
                    "reason": "remote-change",
                    "recorded_at": "2026-04-25T00:00:00+00:00",
                }
            ]
        )
    )

    result = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "diffd", "preview", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    assert result.returncode == 0
    assert payload["status"] in {"ok", "warning"}
    assert payload["details"]["remote changes"] == 2
    assert payload["details"]["pending downloads"] == 1
    assert payload["details"]["planned downloads"] == 2
    assert payload["details"]["skipped download records"] == 1
    assert (
        payload["details"]["plan summary"]
        == "downloads: 2; remote changes: 2; pending downloads: 1; skipped: 1"
    )
    assert payload["details"]["planned download records"][0]["path"] == "Documents/remote.pdf"
    assert payload["details"]["skipped download record details"][0]["path"] == ""


def test_diffd_preview_applies_allowlist_and_default_excludes(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    diffd_dir = state_dir / "diffd"
    diffd_dir.mkdir(parents=True)
    (diffd_dir / "remote-changes.json").write_text(
        json.dumps(
            [
                {"path": "Documents/remote.pdf", "action": "download"},
                {"path": "Documents/.DS_Store", "action": "download"},
                {"path": "Documents/.temporary-download", "action": "download"},
                {"path": "private/secret.txt", "action": "download"},
            ]
        )
    )

    result = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "diffd", "preview", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    assert result.returncode == 0
    assert payload["details"]["planned downloads"] == 1
    assert payload["details"]["skipped download records"] == 3
    skipped = payload["details"]["skipped download record details"]
    assert skipped[0]["reason"] == "default exclude"
    assert skipped[1]["reason"] == "manager ignore rule"
    assert skipped[2]["reason"] == "outside allowlist"


def test_diffd_diff_fixture_preview_is_read_only(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    fixture = tmp_path / "pcloud-diff.json"
    fixture.write_text(
        json.dumps(
            {
                "diffid": "456",
                "entries": [
                    {"path": "/Documents/remote.pdf", "event": "modified"},
                    {"path": "", "event": "deleted"},
                ],
            }
        )
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "diffd",
            "diff",
            "preview",
            "--fixture",
            str(fixture),
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    assert result.returncode == 0
    assert payload["summary"] == "diffd pCloud diff fixture preview is ready"
    assert payload["details"]["implementation status"] == "fixture parser only; pCloud API is not called"
    assert payload["details"]["gate status"] == "closed"
    assert payload["details"]["fixture diffid"] == "456"
    assert payload["details"]["parsed diff changes"] == 1
    assert payload["details"]["invalid diff changes"] == 1
    assert payload["details"]["remote changes"] == 2
    assert payload["details"]["planned downloads"] == 1
    assert payload["details"]["skipped download records"] == 1
    assert payload["details"]["remote change records"][0]["reason"] == "diff:modified"
    assert not (state_dir / "diffd").exists()


def test_diffd_api_poll_preview_is_request_shape_only(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])

    result = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "diffd", "api-poll", "preview", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    assert result.returncode == 0
    assert payload["summary"] == "diffd pCloud API poll preview is ready"
    assert payload["details"]["implementation status"] == "API poll preview only; pCloud API is not called"
    assert payload["details"]["gate status"] == "closed"
    assert payload["details"]["request method"] == "GET"
    assert payload["details"]["request path"] == "/diff"
    assert payload["details"]["request query"]["diffid"] == "0"
    assert payload["details"]["state writes"] == "none"
    assert not (state_dir / "diffd").exists()


def test_diffd_api_long_poll_gate_is_read_only_checklist(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    shadow_workspace = tmp_path / "pcloud-shadow-validation-api-poll" / "workspace"
    shadow_report = tmp_path / "shadow-validation-api-poll.json"
    shadow_report.write_text(
        json.dumps(
            {
                "status": "ok",
                "workspace": str(shadow_workspace),
                "state_dir": str(shadow_workspace / ".dev-state" / "state"),
                "checks": [
                    {"name": "temporary workspace guard", "status": "ok"},
                    {"name": "temporary state dir guard", "status": "ok"},
                    {"name": "unsafe state dir guard", "status": "ok"},
                ],
            }
        )
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "diffd",
            "api-poll",
            "long-poll-gate",
            "--report-path",
            str(shadow_report),
            "--operator-reviewed-preview",
            "--reviewer-approved-response-policy",
            "--reviewer-approved-credential-policy",
            "--reviewer-approved-process-policy",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    checks = {check["name"]: check for check in payload["details"]["preflight checks"]}

    assert result.returncode == 0
    assert payload["status"] == "warning"
    assert payload["summary"] == "diffd pCloud API long-poll gate is closed"
    assert payload["details"]["implementation status"].startswith("read-only checklist")
    assert payload["details"]["long-poll gate status"] == "closed"
    assert payload["details"]["long-poll can start"] == "no"
    assert payload["details"]["state writes"] == "none"
    assert payload["details"]["human gate status"] == "required-before-api-long-poll"
    assert payload["details"]["request method"] == "GET"
    assert payload["details"]["request path"] == "/diff"
    assert payload["details"]["request query"]["diffid"] == "0"
    assert payload["details"]["poll interval seconds"] == 60
    assert payload["details"]["batch limit"] == 100
    assert payload["details"]["long-poll approval status"] == "complete-read-only"
    assert checks["saved shadow validation report"]["status"] == "ok"
    assert checks["API preview command"]["status"] == "ok"
    assert checks["diff cursor state"]["status"] == "ok"
    assert checks["download scope"]["status"] == "ok"
    assert checks["operator preview review"]["status"] == "ok"
    assert checks["response policy approval"]["status"] == "ok"
    assert checks["credential policy approval"]["status"] == "ok"
    assert checks["process lifecycle approval"]["status"] == "ok"
    assert "diffd.api-poll.long-poll-gate" in [action["id"] for action in payload["actions"]]
    assert not (state_dir / "diffd").exists()


def test_diffd_api_long_poll_run_refuses_without_execution_gate(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = _use_default_dev_state_dir(env)
    fixture = tmp_path / "api-long-poll.json"
    fixture.write_text(
        json.dumps(
            {
                "diffid": "123",
                "entries": [
                    {"path": "Documents/from-api.pdf", "event": "modified"},
                ],
            }
        )
    )
    shadow_report = tmp_path / "shadow-validation-api-long-poll-run.json"
    shadow_workspace = tmp_path / "pcloud-shadow-validation-api-long-poll-run" / "workspace"
    shadow_report.write_text(
        json.dumps(
            {
                "status": "ok",
                "workspace": str(shadow_workspace),
                "state_dir": str(shadow_workspace / ".dev-state" / "state"),
                "checks": [
                    {"name": "temporary workspace guard", "status": "ok"},
                    {"name": "temporary state dir guard", "status": "ok"},
                    {"name": "unsafe state dir guard", "status": "ok"},
                ],
            }
        )
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "diffd",
            "api-poll",
            "long-poll-run",
            "--report-path",
            str(shadow_report),
            "--operator-reviewed-preview",
            "--reviewer-approved-response-policy",
            "--reviewer-approved-credential-policy",
            "--reviewer-approved-process-policy",
            "--fixture",
            str(fixture),
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)

    assert result.returncode == 1
    assert payload["status"] == "error"
    assert payload["summary"] == "diffd pCloud API long-poll execution is gated"
    assert payload["details"]["state writes"] == "none"
    assert payload["details"]["long-poll can start"] == "no"
    assert "PCLOUD_TOOLS_DIFFD_API_LONG_POLL_EXECUTION_GATE" in [
        issue["key"] for issue in payload["issues"]
    ]
    assert not (state_dir / "diffd" / "remote-changes.json").exists()
    assert not (state_dir / "daemon" / "diffid").exists()


def test_diffd_api_long_poll_run_executes_fixture_in_dev_state(tmp_path: Path) -> None:
    env = _base_env(
        tmp_path,
        {"PCLOUD_TOOLS_DIFFD_API_LONG_POLL_GATE": "operator-approved-api-long-poll-v1"},
    )
    state_dir = _use_default_dev_state_dir(env)
    fixture = tmp_path / "api-long-poll-ok.json"
    fixture.write_text(
        json.dumps(
            {
                "diffid": "123",
                "entries": [
                    {"path": "Documents/from-api.pdf", "event": "modified"},
                    {"path": "private/outside.pdf", "event": "modified"},
                ],
            }
        )
    )
    shadow_report = tmp_path / "shadow-validation-api-long-poll-run-ok.json"
    shadow_workspace = tmp_path / "pcloud-shadow-validation-api-long-poll-run-ok" / "workspace"
    shadow_report.write_text(
        json.dumps(
            {
                "status": "ok",
                "workspace": str(shadow_workspace),
                "state_dir": str(shadow_workspace / ".dev-state" / "state"),
                "checks": [
                    {"name": "temporary workspace guard", "status": "ok"},
                    {"name": "temporary state dir guard", "status": "ok"},
                    {"name": "unsafe state dir guard", "status": "ok"},
                ],
            }
        )
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "diffd",
            "api-poll",
            "long-poll-run",
            "--report-path",
            str(shadow_report),
            "--operator-reviewed-preview",
            "--reviewer-approved-response-policy",
            "--reviewer-approved-credential-policy",
            "--reviewer-approved-process-policy",
            "--fixture",
            str(fixture),
            "--max-iterations",
            "1",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    remote_changes = json.loads((state_dir / "diffd" / "remote-changes.json").read_text())
    diffid = (state_dir / "daemon" / "diffid").read_text().strip()
    run_state = json.loads((state_dir / "diffd" / "api-long-poll-last-run.json").read_text())

    assert result.returncode == 0
    assert payload["status"] in {"ok", "warning"}
    assert payload["summary"] == "diffd pCloud API long-poll run completed"
    assert payload["details"]["long-poll can start"] == "yes"
    assert payload["details"]["state writes"] == "diffd remote-change records, diff cursor, and long-poll run state"
    assert payload["details"]["download records appended"] == 1
    assert payload["details"]["skipped download records"] == 1
    assert payload["details"]["written diffid"] == "123"
    assert remote_changes == [{"path": "Documents/from-api.pdf", "action": "download", "reason": "diff:modified"}]
    assert diffid == "123"
    assert run_state["appended_records"] == remote_changes
    assert run_state["written_diffid"] == "123"


def test_diffd_api_long_poll_parses_pcloud_metadata_paths(tmp_path: Path) -> None:
    env = _base_env(
        tmp_path,
        {"PCLOUD_TOOLS_DIFFD_API_LONG_POLL_GATE": "operator-approved-api-long-poll-v1"},
    )
    state_dir = _use_default_dev_state_dir(env)
    fixture = tmp_path / "api-long-poll-metadata.json"
    fixture.write_text(
        json.dumps(
            {
                "diffid": "124",
                "entries": [
                    {"diffid": 0, "event": "reset"},
                    {
                        "diffid": 1,
                        "event": "createfolder",
                        "metadata": {"isfolder": True, "folderid": 10, "parentfolderid": 0, "name": "Documents"},
                    },
                    {
                        "diffid": 2,
                        "event": "createfile",
                        "metadata": {"isfolder": False, "parentfolderid": 10, "name": "from-metadata.pdf"},
                    },
                    {
                        "diffid": 3,
                        "event": "createfolder",
                        "metadata": {"isfolder": True, "folderid": 11, "parentfolderid": 0, "name": "private"},
                    },
                    {
                        "diffid": 4,
                        "event": "createfile",
                        "metadata": {"isfolder": False, "parentfolderid": 11, "name": "skip.pdf"},
                    },
                    {"diffid": 5, "event": "modifyuserinfo"},
                ],
            }
        )
    )
    shadow_report = tmp_path / "shadow-validation-api-long-poll-metadata.json"
    shadow_workspace = tmp_path / "pcloud-shadow-validation-api-long-poll-metadata" / "workspace"
    shadow_report.write_text(
        json.dumps(
            {
                "status": "ok",
                "workspace": str(shadow_workspace),
                "state_dir": str(shadow_workspace / ".dev-state" / "state"),
                "checks": [
                    {"name": "temporary workspace guard", "status": "ok"},
                    {"name": "temporary state dir guard", "status": "ok"},
                    {"name": "unsafe state dir guard", "status": "ok"},
                ],
            }
        )
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "diffd",
            "api-poll",
            "long-poll-run",
            "--report-path",
            str(shadow_report),
            "--operator-reviewed-preview",
            "--reviewer-approved-response-policy",
            "--reviewer-approved-credential-policy",
            "--reviewer-approved-process-policy",
            "--fixture",
            str(fixture),
            "--max-iterations",
            "1",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    remote_changes = json.loads((state_dir / "diffd" / "remote-changes.json").read_text())

    assert result.returncode == 0
    assert payload["details"]["parsed diff changes"] == 2
    assert payload["details"]["invalid diff changes"] == 0
    assert payload["details"]["download records appended"] == 1
    assert payload["details"]["skipped download records"] == 1
    assert remote_changes == [
        {"path": "Documents/from-metadata.pdf", "action": "download", "reason": "diff:createfile"}
    ]


def test_diffd_api_long_poll_reuses_folder_cache_across_runs(tmp_path: Path) -> None:
    env = _base_env(
        tmp_path,
        {"PCLOUD_TOOLS_DIFFD_API_LONG_POLL_GATE": "operator-approved-api-long-poll-v1"},
    )
    state_dir = _use_default_dev_state_dir(env)
    shadow_report = tmp_path / "shadow-validation-api-folder-cache.json"
    shadow_workspace = tmp_path / "pcloud-shadow-validation-api-folder-cache" / "workspace"
    shadow_report.write_text(
        json.dumps(
            {
                "status": "ok",
                "workspace": str(shadow_workspace),
                "state_dir": str(shadow_workspace / ".dev-state" / "state"),
                "checks": [
                    {"name": "temporary workspace guard", "status": "ok"},
                    {"name": "temporary state dir guard", "status": "ok"},
                    {"name": "unsafe state dir guard", "status": "ok"},
                ],
            }
        )
    )
    first_fixture = tmp_path / "api-folder-cache-first.json"
    first_fixture.write_text(
        json.dumps(
            {
                "diffid": "10",
                "entries": [
                    {
                        "diffid": 10,
                        "event": "createfolder",
                        "metadata": {"isfolder": True, "folderid": 42, "parentfolderid": 0, "name": "Documents"},
                    }
                ],
            }
        )
    )
    second_fixture = tmp_path / "api-folder-cache-second.json"
    second_fixture.write_text(
        json.dumps(
            {
                "diffid": "11",
                "entries": [
                    {
                        "diffid": 11,
                        "event": "createfile",
                        "metadata": {"isfolder": False, "parentfolderid": 42, "name": "from-cache.pdf"},
                    }
                ],
            }
        )
    )
    common = [
        sys.executable,
        "-m",
        "pcloud_tools.cli",
        "diffd",
        "api-poll",
        "long-poll-run",
        "--report-path",
        str(shadow_report),
        "--operator-reviewed-preview",
        "--reviewer-approved-response-policy",
        "--reviewer-approved-credential-policy",
        "--reviewer-approved-process-policy",
        "--max-iterations",
        "1",
        "--execute",
        "--json",
    ]

    first = subprocess.run(
        [*common, "--fixture", str(first_fixture)],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    second = subprocess.run(
        [*common, "--fixture", str(second_fixture)],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    first_payload = _payload(first)
    second_payload = _payload(second)
    folder_cache = json.loads((state_dir / "diffd" / "folder-cache.json").read_text())
    remote_changes = json.loads((state_dir / "diffd" / "remote-changes.json").read_text())

    assert first.returncode == 0
    assert second.returncode == 0
    assert first_payload["details"]["folder cache entries before"] == 0
    assert first_payload["details"]["folder cache entries after"] == 1
    assert second_payload["details"]["folder cache entries before"] == 1
    assert second_payload["details"]["parsed diff changes"] == 1
    assert second_payload["details"]["invalid diff changes"] == 0
    assert folder_cache == {"42": "Documents"}
    assert remote_changes == [
        {"path": "Documents/from-cache.pdf", "action": "download", "reason": "diff:createfile"}
    ]


def test_diffd_api_long_poll_resolves_live_parent_folder_metadata(tmp_path: Path) -> None:
    requests: list[tuple[str, dict[str, list[str]]]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            requests.append((parsed.path, query))
            if parsed.path == "/diff":
                body = json.dumps(
                    {
                        "diffid": "2228182",
                        "entries": [
                            {
                                "diffid": 2228181,
                                "event": "createfile",
                                "metadata": {
                                    "isfolder": False,
                                    "parentfolderid": 30754773616,
                                    "name": "IMG_001.jpeg",
                                },
                            }
                        ],
                    }
                ).encode()
            elif parsed.path == "/listfolder" and query.get("folderid") == ["30754773616"]:
                body = json.dumps(
                    {
                        "metadata": {
                            "isfolder": True,
                            "folderid": 30754773616,
                            "parentfolderid": 29925560641,
                            "name": "Documents",
                        }
                    }
                ).encode()
            elif parsed.path == "/listfolder" and query.get("folderid") == ["29925560641"]:
                body = json.dumps(
                    {
                        "metadata": {
                            "isfolder": True,
                            "folderid": 29925560641,
                            "parentfolderid": 0,
                            "name": "core",
                        }
                    }
                ).encode()
            else:
                body = json.dumps({"result": 2000, "error": "not found"}).encode()
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env = _base_env(
            tmp_path,
            {
                "PCLOUD_TOOLS_DIFFD_API_LONG_POLL_GATE": "operator-approved-api-long-poll-v1",
                "PCLOUD_TOOLS_PCLOUD_API_BASE_URL": f"http://127.0.0.1:{server.server_port}",
                "PCLOUD_TOOLS_PCLOUD_API_TOKEN": "topsecret-token",
                "PCLOUD_TOOLS_PCLOUD_API_TIMEOUT_SECONDS": "5",
            },
        )
        state_dir = _use_default_dev_state_dir(env)
        shadow_report = tmp_path / "shadow-validation-api-live-folder-metadata.json"
        shadow_workspace = tmp_path / "pcloud-shadow-validation-api-live-folder-metadata" / "workspace"
        shadow_report.write_text(
            json.dumps(
                {
                    "status": "ok",
                    "workspace": str(shadow_workspace),
                    "state_dir": str(shadow_workspace / ".dev-state" / "state"),
                    "checks": [
                        {"name": "temporary workspace guard", "status": "ok"},
                        {"name": "temporary state dir guard", "status": "ok"},
                        {"name": "unsafe state dir guard", "status": "ok"},
                    ],
                }
            )
        )
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pcloud_tools.cli",
                "diffd",
                "api-poll",
                "long-poll-run",
                "--report-path",
                str(shadow_report),
                "--operator-reviewed-preview",
                "--reviewer-approved-response-policy",
                "--reviewer-approved-credential-policy",
                "--reviewer-approved-process-policy",
                "--live-api",
                "--max-iterations",
                "1",
                "--execute",
                "--json",
            ],
            check=False,
            capture_output=True,
            text=True,
            cwd=tmp_path,
            env=env,
        )
    finally:
        server.shutdown()
        server.server_close()

    payload = _payload(result)
    remote_changes = json.loads((state_dir / "diffd" / "remote-changes.json").read_text())
    folder_cache = json.loads((state_dir / "diffd" / "folder-cache.json").read_text())

    assert result.returncode == 0
    assert payload["details"]["parsed diff changes"] == 1
    assert payload["details"]["invalid diff changes"] == 0
    assert payload["details"]["download records appended"] == 1
    assert payload["details"]["folder metadata requests count"] == 2
    assert remote_changes == [
        {"path": "Documents/IMG_001.jpeg", "action": "download", "reason": "diff:createfile"}
    ]
    assert folder_cache == {"29925560641": "", "30754773616": "Documents"}
    assert [request[0] for request in requests] == ["/diff", "/listfolder", "/listfolder"]
    assert "topsecret-token" not in result.stdout


def test_diffd_folder_cache_add_status_remove_is_dev_state_only(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = _use_default_dev_state_dir(env)
    cache_file = state_dir / "diffd" / "folder-cache.json"

    preview = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "diffd",
            "folder-cache",
            "add",
            "29913863697",
            "bench_test",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    add = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "diffd",
            "folder-cache",
            "add",
            "29913863697",
            "bench_test",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    status = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "diffd",
            "folder-cache",
            "status",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    remove = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "diffd",
            "folder-cache",
            "remove",
            "29913863697",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    preview_payload = _payload(preview)
    add_payload = _payload(add)
    status_payload = _payload(status)
    remove_payload = _payload(remove)

    assert preview.returncode == 0
    assert add.returncode == 0
    assert status.returncode == 0
    assert remove.returncode == 0
    assert preview_payload["details"]["state writes"] == "none"
    assert add_payload["details"]["state writes"] == "diffd folder cache"
    assert status_payload["details"]["entries"] == [
        {"folder_id": "29913863697", "path": "bench_test"}
    ]
    assert json.loads(cache_file.read_text()) == {}
    assert remove_payload["details"]["folder cache entries removed"] == 1


def test_diffd_api_long_poll_run_refuses_live_api_without_token(tmp_path: Path) -> None:
    env = _base_env(
        tmp_path,
        {"PCLOUD_TOOLS_DIFFD_API_LONG_POLL_GATE": "operator-approved-api-long-poll-v1"},
    )
    state_dir = _use_default_dev_state_dir(env)
    shadow_report = tmp_path / "shadow-validation-api-live-no-token.json"
    shadow_workspace = tmp_path / "pcloud-shadow-validation-api-live-no-token" / "workspace"
    shadow_report.write_text(
        json.dumps(
            {
                "status": "ok",
                "workspace": str(shadow_workspace),
                "state_dir": str(shadow_workspace / ".dev-state" / "state"),
                "checks": [
                    {"name": "temporary workspace guard", "status": "ok"},
                    {"name": "temporary state dir guard", "status": "ok"},
                    {"name": "unsafe state dir guard", "status": "ok"},
                ],
            }
        )
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "diffd",
            "api-poll",
            "long-poll-run",
            "--report-path",
            str(shadow_report),
            "--operator-reviewed-preview",
            "--reviewer-approved-response-policy",
            "--reviewer-approved-credential-policy",
            "--reviewer-approved-process-policy",
            "--live-api",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)

    assert result.returncode == 1
    assert payload["status"] == "error"
    assert payload["details"]["live API requested"] == "yes"
    assert payload["details"]["API token provided"] == "no"
    assert payload["details"]["state writes"] == "none"
    assert "PCLOUD_TOOLS_PCLOUD_API_TOKEN" in [issue["key"] for issue in payload["issues"]]
    assert not (state_dir / "diffd" / "remote-changes.json").exists()
    assert not (state_dir / "daemon" / "diffid").exists()


def test_diffd_api_long_poll_run_executes_live_api_against_local_server(tmp_path: Path) -> None:
    requests: list[dict[str, list[str]]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            requests.append(urllib.parse.parse_qs(parsed.query))
            body = json.dumps(
                {
                    "diffid": "456",
                    "entries": [
                        {"path": "Documents/from-live-api.pdf", "event": "modified"},
                        {"path": "private/outside.pdf", "event": "modified"},
                    ],
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env = _base_env(
            tmp_path,
            {
                "PCLOUD_TOOLS_DIFFD_API_LONG_POLL_GATE": "operator-approved-api-long-poll-v1",
                "PCLOUD_TOOLS_PCLOUD_API_BASE_URL": f"http://127.0.0.1:{server.server_port}",
                "PCLOUD_TOOLS_PCLOUD_API_TOKEN": "topsecret-token",
                "PCLOUD_TOOLS_PCLOUD_API_TIMEOUT_SECONDS": "5",
            },
        )
        state_dir = _use_default_dev_state_dir(env)
        shadow_report = tmp_path / "shadow-validation-api-live-ok.json"
        shadow_workspace = tmp_path / "pcloud-shadow-validation-api-live-ok" / "workspace"
        shadow_report.write_text(
            json.dumps(
                {
                    "status": "ok",
                    "workspace": str(shadow_workspace),
                    "state_dir": str(shadow_workspace / ".dev-state" / "state"),
                    "checks": [
                        {"name": "temporary workspace guard", "status": "ok"},
                        {"name": "temporary state dir guard", "status": "ok"},
                        {"name": "unsafe state dir guard", "status": "ok"},
                    ],
                }
            )
        )

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pcloud_tools.cli",
                "diffd",
                "api-poll",
                "long-poll-run",
                "--report-path",
                str(shadow_report),
                "--operator-reviewed-preview",
                "--reviewer-approved-response-policy",
                "--reviewer-approved-credential-policy",
                "--reviewer-approved-process-policy",
                "--live-api",
                "--max-iterations",
                "1",
                "--execute",
                "--json",
            ],
            check=False,
            capture_output=True,
            text=True,
            cwd=tmp_path,
            env=env,
        )
    finally:
        server.shutdown()
        server.server_close()

    payload = _payload(result)
    remote_changes = json.loads((state_dir / "diffd" / "remote-changes.json").read_text())
    diffid = (state_dir / "daemon" / "diffid").read_text().strip()
    run_state = json.loads((state_dir / "diffd" / "api-long-poll-last-run.json").read_text())

    assert result.returncode == 0
    assert payload["status"] in {"ok", "warning"}
    assert payload["details"]["live API requested"] == "yes"
    assert payload["details"]["API token provided"] == "yes"
    assert "topsecret-token" not in result.stdout
    assert payload["details"]["API request URL"].endswith("auth=%3Credacted%3E")
    assert requests[0]["auth"] == ["topsecret-token"]
    assert requests[0]["diffid"] == ["0"]
    assert requests[0]["limit"] == ["100"]
    assert payload["details"]["download records appended"] == 1
    assert payload["details"]["skipped download records"] == 1
    assert remote_changes == [{"path": "Documents/from-live-api.pdf", "action": "download", "reason": "diff:modified"}]
    assert diffid == "456"
    assert run_state["live_api"] is True
    assert run_state["written_diffid"] == "456"


def test_diffd_api_long_poll_live_catchup_requires_separate_gate_and_iterates(
    tmp_path: Path,
) -> None:
    requests: list[dict[str, list[str]]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            requests.append(query)
            diffid = query.get("diffid", ["0"])[0]
            if diffid == "0":
                payload = {
                    "diffid": "100",
                    "entries": [{"path": "private/old.pdf", "event": "modified"}],
                }
            else:
                payload = {
                    "diffid": "200",
                    "entries": [{"path": "Documents/current.pdf", "event": "modified"}],
                }
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env = _base_env(
            tmp_path,
            {
                "PCLOUD_TOOLS_DIFFD_API_LONG_POLL_GATE": "operator-approved-api-long-poll-v1",
                "PCLOUD_TOOLS_DIFFD_API_CATCHUP_GATE": "operator-approved-api-catchup-v1",
                "PCLOUD_TOOLS_PCLOUD_API_BASE_URL": f"http://127.0.0.1:{server.server_port}",
                "PCLOUD_TOOLS_PCLOUD_API_TOKEN": "topsecret-token",
                "PCLOUD_TOOLS_PCLOUD_API_TIMEOUT_SECONDS": "5",
            },
        )
        state_dir = _use_default_dev_state_dir(env)
        shadow_report = tmp_path / "shadow-validation-api-live-catchup.json"
        shadow_workspace = tmp_path / "pcloud-shadow-validation-api-live-catchup" / "workspace"
        shadow_report.write_text(
            json.dumps(
                {
                    "status": "ok",
                    "workspace": str(shadow_workspace),
                    "state_dir": str(shadow_workspace / ".dev-state" / "state"),
                    "checks": [
                        {"name": "temporary workspace guard", "status": "ok"},
                        {"name": "temporary state dir guard", "status": "ok"},
                        {"name": "unsafe state dir guard", "status": "ok"},
                    ],
                }
            )
        )

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pcloud_tools.cli",
                "diffd",
                "api-poll",
                "long-poll-run",
                "--report-path",
                str(shadow_report),
                "--operator-reviewed-preview",
                "--reviewer-approved-response-policy",
                "--reviewer-approved-credential-policy",
                "--reviewer-approved-process-policy",
                "--reviewer-approved-catchup-policy",
                "--live-api",
                "--max-iterations",
                "2",
                "--execute",
                "--json",
            ],
            check=False,
            capture_output=True,
            text=True,
            cwd=tmp_path,
            env=env,
        )
    finally:
        server.shutdown()
        server.server_close()

    payload = _payload(result)
    remote_changes = json.loads((state_dir / "diffd" / "remote-changes.json").read_text())
    diffid = (state_dir / "daemon" / "diffid").read_text().strip()
    run_state = json.loads((state_dir / "diffd" / "api-long-poll-last-run.json").read_text())

    assert result.returncode == 0
    assert payload["details"]["catch-up requested"] == "yes"
    assert payload["details"]["catch-up gate env honored"] == "yes"
    assert payload["details"]["catch-up policy approval"] == "yes"
    assert payload["details"]["iterations processed"] == 2
    assert payload["details"]["download records appended"] == 1
    assert payload["details"]["skipped download records"] == 1
    assert [request["diffid"] for request in requests] == [["0"], ["100"]]
    assert remote_changes == [{"path": "Documents/current.pdf", "action": "download", "reason": "diff:modified"}]
    assert diffid == "200"
    assert run_state["iterations_processed"] == 2
    assert run_state["written_diffid"] == "200"


def test_diffd_api_checkpoint_uses_last_zero_and_writes_cursor_only(tmp_path: Path) -> None:
    requests: list[dict[str, list[str]]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            requests.append(urllib.parse.parse_qs(parsed.query))
            body = json.dumps({"diffid": "987", "entries": []}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env = _base_env(
            tmp_path,
            {
                "PCLOUD_TOOLS_DIFFD_API_CHECKPOINT_GATE": "operator-approved-api-checkpoint-v1",
                "PCLOUD_TOOLS_PCLOUD_API_BASE_URL": f"http://127.0.0.1:{server.server_port}",
                "PCLOUD_TOOLS_PCLOUD_API_TOKEN": "topsecret-token",
                "PCLOUD_TOOLS_PCLOUD_API_TIMEOUT_SECONDS": "5",
            },
        )
        state_dir = _use_default_dev_state_dir(env)
        stale_lock = state_dir / "diffd" / "api-long-poll.lock"
        stale_lock.mkdir(parents=True)
        old_timestamp = time.time() - 600
        os.utime(stale_lock, (old_timestamp, old_timestamp))
        shadow_report = tmp_path / "shadow-validation-api-checkpoint.json"
        shadow_workspace = tmp_path / "pcloud-shadow-validation-api-checkpoint" / "workspace"
        shadow_report.write_text(
            json.dumps(
                {
                    "status": "ok",
                    "workspace": str(shadow_workspace),
                    "state_dir": str(shadow_workspace / ".dev-state" / "state"),
                    "checks": [
                        {"name": "temporary workspace guard", "status": "ok"},
                        {"name": "temporary state dir guard", "status": "ok"},
                        {"name": "unsafe state dir guard", "status": "ok"},
                    ],
                }
            )
        )

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pcloud_tools.cli",
                "diffd",
                "api-poll",
                "checkpoint",
                "--report-path",
                str(shadow_report),
                "--operator-reviewed-checkpoint",
                "--reviewer-approved-checkpoint-policy",
                "--execute",
                "--json",
            ],
            check=False,
            capture_output=True,
            text=True,
            cwd=tmp_path,
            env=env,
        )
    finally:
        server.shutdown()
        server.server_close()

    payload = _payload(result)
    diffid = (state_dir / "daemon" / "diffid").read_text().strip()
    checkpoint_state = json.loads((state_dir / "diffd" / "api-checkpoint-last-run.json").read_text())

    assert result.returncode == 0
    assert payload["summary"] == "diffd API checkpoint completed"
    assert payload["details"]["checkpoint diffid"] == "987"
    assert payload["details"]["state writes"] == "diff cursor and checkpoint state only"
    assert payload["details"]["diffd API lock status"] == "released"
    assert payload["details"]["remote-change records appended"] == 0
    assert "topsecret-token" not in result.stdout
    assert requests == [{"last": ["0"], "auth": ["topsecret-token"]}]
    assert diffid == "987"
    assert checkpoint_state["written_diffid"] == "987"
    assert not (state_dir / "diffd" / "remote-changes.json").exists()
    assert not (state_dir / "diffd" / "api-long-poll.lock").exists()


def test_diffd_api_long_poll_run_records_failure_state_without_cursor_mutation(tmp_path: Path) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = b"temporary failure"
            self.send_response(503)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env = _base_env(
            tmp_path,
            {
                "PCLOUD_TOOLS_DIFFD_API_LONG_POLL_GATE": "operator-approved-api-long-poll-v1",
                "PCLOUD_TOOLS_PCLOUD_API_BASE_URL": f"http://127.0.0.1:{server.server_port}",
                "PCLOUD_TOOLS_PCLOUD_API_TOKEN": "topsecret-token",
                "PCLOUD_TOOLS_PCLOUD_API_TIMEOUT_SECONDS": "5",
            },
        )
        state_dir = _use_default_dev_state_dir(env)
        shadow_report = tmp_path / "shadow-validation-api-live-failure.json"
        shadow_workspace = tmp_path / "pcloud-shadow-validation-api-live-failure" / "workspace"
        shadow_report.write_text(
            json.dumps(
                {
                    "status": "ok",
                    "workspace": str(shadow_workspace),
                    "state_dir": str(shadow_workspace / ".dev-state" / "state"),
                    "checks": [
                        {"name": "temporary workspace guard", "status": "ok"},
                        {"name": "temporary state dir guard", "status": "ok"},
                        {"name": "unsafe state dir guard", "status": "ok"},
                    ],
                }
            )
        )

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pcloud_tools.cli",
                "diffd",
                "api-poll",
                "long-poll-run",
                "--report-path",
                str(shadow_report),
                "--operator-reviewed-preview",
                "--reviewer-approved-response-policy",
                "--reviewer-approved-credential-policy",
                "--reviewer-approved-process-policy",
                "--live-api",
                "--max-iterations",
                "1",
                "--execute",
                "--json",
            ],
            check=False,
            capture_output=True,
            text=True,
            cwd=tmp_path,
            env=env,
        )
    finally:
        server.shutdown()
        server.server_close()

    payload = _payload(result)
    run_state = json.loads((state_dir / "diffd" / "api-long-poll-last-run.json").read_text())

    assert result.returncode == 1
    assert payload["status"] == "error"
    assert payload["details"]["state writes"] == "diffd long-poll failure state"
    assert payload["details"]["failure state written"] == "yes"
    assert payload["details"]["written diffid"] == "-"
    assert run_state["written_diffid"] == "-"
    assert run_state["backoff_seconds"] == 60
    assert run_state["appended_records"] == []
    assert not (state_dir / "daemon" / "diffid").exists()
    assert not (state_dir / "diffd" / "remote-changes.json").exists()


def test_diffd_api_long_poll_run_uses_rclone_config_credentials(tmp_path: Path) -> None:
    requests: list[dict[str, list[str]]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            requests.append(urllib.parse.parse_qs(parsed.query))
            body = json.dumps(
                {
                    "diffid": "789",
                    "entries": [
                        {"path": "Documents/from-rclone-config.pdf", "event": "created"},
                    ],
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env = _base_env(
            tmp_path,
            {
                "PCLOUD_TOOLS_DIFFD_API_LONG_POLL_GATE": "operator-approved-api-long-poll-v1",
                "PCLOUD_TOOLS_CORE_REMOTE": "pcloud:core",
                "PCLOUD_TOOLS_PCLOUD_API_TIMEOUT_SECONDS": "5",
            },
        )
        state_dir = _use_default_dev_state_dir(env)
        rclone_config = tmp_path / "rclone.conf"
        env["RCLONE_CONFIG"] = str(rclone_config)
        rclone_config.write_text(
            "\n".join(
                [
                    "[pcloud]",
                    "type = pcloud",
                    f"hostname = http://127.0.0.1:{server.server_port}",
                    'token = {"access_token":"rclone-secret","token_type":"bearer","expiry":"0001-01-01T00:00:00Z"}',
                    "",
                    "[pcloud-crypt]",
                    "type = crypt",
                    "remote = pcloud:crypt",
                    "password = should-not-be-read",
                    "password2 = should-not-be-read",
                    "",
                ]
            )
        )
        shadow_report = tmp_path / "shadow-validation-api-rclone-ok.json"
        shadow_workspace = tmp_path / "pcloud-shadow-validation-api-rclone-ok" / "workspace"
        shadow_report.write_text(
            json.dumps(
                {
                    "status": "ok",
                    "workspace": str(shadow_workspace),
                    "state_dir": str(shadow_workspace / ".dev-state" / "state"),
                    "checks": [
                        {"name": "temporary workspace guard", "status": "ok"},
                        {"name": "temporary state dir guard", "status": "ok"},
                        {"name": "unsafe state dir guard", "status": "ok"},
                    ],
                }
            )
        )

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pcloud_tools.cli",
                "diffd",
                "api-poll",
                "long-poll-run",
                "--report-path",
                str(shadow_report),
                "--operator-reviewed-preview",
                "--reviewer-approved-response-policy",
                "--reviewer-approved-credential-policy",
                "--reviewer-approved-process-policy",
                "--live-api",
                "--max-iterations",
                "1",
                "--execute",
                "--json",
            ],
            check=False,
            capture_output=True,
            text=True,
            cwd=tmp_path,
            env=env,
        )
    finally:
        server.shutdown()
        server.server_close()

    payload = _payload(result)
    remote_changes = json.loads((state_dir / "diffd" / "remote-changes.json").read_text())

    assert result.returncode == 0
    assert payload["details"]["API credential source"] == "rclone config"
    assert payload["details"]["API auth parameter"] == "access_token"
    assert payload["details"]["API token provided"] == "yes"
    assert "rclone-secret" not in result.stdout
    assert "should-not-be-read" not in result.stdout
    assert requests[0]["access_token"] == ["rclone-secret"]
    assert remote_changes == [
        {"path": "Documents/from-rclone-config.pdf", "action": "download", "reason": "diff:created"}
    ]


def test_transfer_previews_emit_commands_without_state_writes(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    pushd_dir = state_dir / "pushd"
    diffd_dir = state_dir / "diffd"
    pushd_dir.mkdir(parents=True)
    diffd_dir.mkdir(parents=True)
    _write_workspace_file(env, "Documents/final-upload.txt", "upload\n")
    _write_workspace_file(env, "Documents/upload.pdf", "upload\n")
    (pushd_dir / "queue.json").write_text(
        json.dumps([{"path": "Documents/upload.pdf", "action": "upload", "reason": "test"}])
    )
    (diffd_dir / "remote-changes.json").write_text(
        json.dumps([{"path": "Documents/download.pdf", "action": "download", "reason": "test"}])
    )

    pushd_human = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "transfer", "preview"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    pushd = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "transfer", "preview", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    diffd = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "diffd", "transfer", "preview", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    pushd_payload = _payload(pushd)
    diffd_payload = _payload(diffd)
    assert pushd_human.returncode == 0
    assert "pushd transfer preview:" in pushd_human.stdout
    assert "gate: closed" in pushd_human.stdout
    assert "state writes: none" in pushd_human.stdout
    assert "planned transfers: 1" in pushd_human.stdout
    assert "first target: upload Documents/upload.pdf" in pushd_human.stdout
    assert "first command:" in pushd_human.stdout
    assert "planned transfer commands:" not in pushd_human.stdout
    assert "core dir:" not in pushd_human.stdout
    assert pushd.returncode == 0
    assert diffd.returncode == 0
    assert pushd_payload["summary"] == "pushd upload transfer preview is ready"
    assert diffd_payload["summary"] == "diffd download transfer preview is ready"
    assert pushd_payload["details"]["implementation status"] == "transfer command preview only; rclone is not executed"
    assert diffd_payload["details"]["implementation status"] == "transfer command preview only; rclone is not executed"
    assert pushd_payload["details"]["gate status"] == "closed"
    assert diffd_payload["details"]["gate status"] == "closed"
    assert pushd_payload["details"]["state writes"] == "none"
    assert diffd_payload["details"]["state writes"] == "none"
    push_command = pushd_payload["details"]["planned transfer commands"][0]["command"]
    diff_command = diffd_payload["details"]["planned transfer commands"][0]["command"]
    assert push_command[1] == "copyto"
    assert push_command[2].endswith("/Documents/upload.pdf")
    assert push_command[3].endswith("/Documents/upload.pdf")
    assert diff_command[1] == "copyto"
    assert diff_command[2].endswith("/Documents/download.pdf")
    assert diff_command[3].endswith("/Documents/download.pdf")
    assert not (pushd_dir / "last-plan.json").exists()
    assert not (diffd_dir / "last-plan.json").exists()


def test_transfer_preview_routes_conflicts_and_delete_rename_actions_to_manual_review(
    tmp_path: Path,
) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    pushd_dir = state_dir / "pushd"
    diffd_dir = state_dir / "diffd"
    pushd_dir.mkdir(parents=True)
    diffd_dir.mkdir(parents=True)
    _write_workspace_file(env, "Documents/upload.pdf", "upload\n")
    _write_workspace_file(env, "Documents/conflict.pdf", "conflict\n")
    (pushd_dir / "queue.json").write_text(
        json.dumps(
            [
                {"path": "Documents/upload.pdf", "action": "upload", "reason": "ok"},
                {"path": "Documents/conflict.pdf", "action": "upload", "reason": "local"},
                {"path": "Documents/deleted.pdf", "action": "delete", "reason": "local-delete"},
            ]
        )
    )
    (diffd_dir / "remote-changes.json").write_text(
        json.dumps(
            [
                {"path": "Documents/download.pdf", "action": "download", "reason": "ok"},
                {"path": "Documents/conflict.pdf", "action": "download", "reason": "remote"},
                {"path": "Documents/renamed.pdf", "action": "rename", "reason": "remote-rename"},
            ]
        )
    )

    pushd = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "transfer", "preview", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    diffd = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "diffd", "transfer", "preview", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    pushd_payload = _payload(pushd)
    diffd_payload = _payload(diffd)
    pushd_manual = pushd_payload["details"]["manual review transfer record details"]
    diffd_manual = diffd_payload["details"]["manual review transfer record details"]

    assert pushd.returncode == 0
    assert diffd.returncode == 0
    assert pushd_payload["status"] == "warning"
    assert diffd_payload["status"] == "warning"
    assert pushd_payload["details"]["planned uploads"] == 1
    assert diffd_payload["details"]["planned downloads"] == 1
    assert pushd_payload["details"]["real execution can run"] == "no"
    assert diffd_payload["details"]["real execution can run"] == "no"
    assert pushd_payload["details"]["real execution readiness"] == "blocked-preview"
    assert diffd_payload["details"]["real execution readiness"] == "blocked-preview"
    assert pushd_payload["details"]["manual review transfer records"] == 2
    assert diffd_payload["details"]["manual review transfer records"] == 2
    assert [item["path"] for item in pushd_payload["details"]["planned transfer commands"]] == [
        "Documents/upload.pdf"
    ]
    assert [item["path"] for item in diffd_payload["details"]["planned transfer commands"]] == [
        "Documents/download.pdf"
    ]
    assert {item["path"] for item in pushd_manual} == {
        "Documents/conflict.pdf",
        "Documents/deleted.pdf",
    }
    assert {item["path"] for item in diffd_manual} == {
        "Documents/conflict.pdf",
        "Documents/renamed.pdf",
    }
    assert any("opposite-side change" in item["reason"] for item in pushd_manual)
    assert any("delete action" in item["reason"] for item in pushd_manual)
    assert any("opposite-side change" in item["reason"] for item in diffd_manual)
    assert any("rename action" in item["reason"] for item in diffd_manual)
    assert "PCLOUD_TOOLS_PUSHD_TRANSFER_MANUAL_REVIEW" in [
        issue["key"] for issue in pushd_payload["issues"]
    ]
    assert "PCLOUD_TOOLS_DIFFD_TRANSFER_MANUAL_REVIEW" in [
        issue["key"] for issue in diffd_payload["issues"]
    ]


def test_transfer_validation_matrix_is_read_only_and_lists_human_review_cases(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])

    pushd = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "transfer", "validation-matrix", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    pushd_human = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "transfer", "validation-matrix"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    diffd = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "diffd", "transfer", "validation-matrix", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    pushd_payload = _payload(pushd)
    diffd_payload = _payload(diffd)
    pushd_details = pushd_payload["details"]
    diffd_details = diffd_payload["details"]
    pushd_first = pushd_details["cases"][0]
    diffd_ids = {case["id"] for case in diffd_details["cases"]}

    assert pushd.returncode == 0
    assert pushd_human.returncode == 0
    assert diffd.returncode == 0
    assert "pushd transfer validation-matrix:" in pushd_human.stdout
    assert "setup:" in pushd_human.stdout
    assert "check:" in pushd_human.stdout
    assert "state writes: none" in pushd_human.stdout
    assert "{'id':" not in pushd_human.stdout
    assert pushd_payload["command"] == "pushd transfer validation-matrix"
    assert diffd_payload["command"] == "diffd transfer validation-matrix"
    assert pushd_details["implementation status"] == (
        "read-only matrix; no setup, transfer, consume, or cleanup command is executed"
    )
    assert diffd_details["implementation status"] == (
        "read-only matrix; no setup, transfer, consume, or cleanup command is executed"
    )
    assert pushd_details["real execution can run"] == "no"
    assert diffd_details["real execution can run"] == "no"
    assert pushd_details["state writes"] == "none"
    assert diffd_details["state writes"] == "none"
    assert pushd_details["case count"] == 5
    assert diffd_details["case count"] == 6
    assert "remote-only-download" in diffd_ids
    assert pushd_first["commands"]["setup"][1:4] == ["pushd", "queue", "add"]
    assert pushd_first["commands"]["check"][1:4] == ["pushd", "transfer", "check"]
    assert "--final-review" in pushd_first["commands"]["check"]
    assert pushd_first["commands"]["cleanup"][1:4] == ["pushd", "queue", "remove"]
    assert diffd_details["cases"][-1]["commands"]["setup"][1:4] == ["diffd", "remote-change", "add"]
    assert diffd_details["cases"][-1]["commands"]["check"][1:4] == ["diffd", "transfer", "check"]
    assert "--final-review" in diffd_details["cases"][-1]["commands"]["check"]
    assert "running rclone copyto" in pushd_details["blocked operations"]
    assert "running rclone copyto" in diffd_details["blocked operations"]
    assert not (state_dir / "pushd").exists()
    assert not (state_dir / "diffd").exists()


def test_service_launchd_gate_is_read_only_and_does_not_write_plists(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    pushd = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "launchd", "gate", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    pushd_human = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "launchd", "gate"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    diffd = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "diffd",
            "launchd",
            "gate",
            "--operator-reviewed-daemon-command",
            "--reviewer-approved-plist-policy",
            "--reviewer-approved-launchctl-policy",
            "--reviewer-approved-rollback-policy",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    pushd_payload = _payload(pushd)
    diffd_payload = _payload(diffd)
    pushd_details = pushd_payload["details"]
    diffd_details = diffd_payload["details"]

    assert pushd.returncode == 0
    assert pushd_human.returncode == 0
    assert diffd.returncode == 0
    assert "pushd launchd gate:" in pushd_human.stdout
    assert "launchd can register: no" in pushd_human.stdout
    assert "state writes: none" in pushd_human.stdout
    assert "bootstrap command examples:" in pushd_human.stdout
    assert "blocked operations:" in pushd_human.stdout
    assert "plist payload draft:" not in pushd_human.stdout
    assert pushd_payload["command"] == "pushd launchd gate"
    assert diffd_payload["command"] == "diffd launchd gate"
    assert pushd_details["implementation status"] == (
        "read-only launchd gate scaffold; plist is not written and launchctl is not executed"
    )
    assert diffd_details["implementation status"] == (
        "read-only launchd gate scaffold; plist is not written and launchctl is not executed"
    )
    assert pushd_details["launchd gate status"] == "closed"
    assert diffd_details["launchd gate status"] == "closed"
    assert pushd_details["launchd can register"] == "no"
    assert diffd_details["launchd can register"] == "no"
    assert pushd_details["state writes"] == "none"
    assert diffd_details["state writes"] == "none"
    assert pushd_details["service label"] == "com.example.pcloud-pushd.dev"
    assert diffd_details["service label"] == "com.example.pcloud-diffd.dev"
    assert pushd_details["plist status"] == "draft-only; not written by this command"
    assert diffd_details["plist status"] == "draft-only; not written by this command"
    assert pushd_details["plist payload draft"]["ProgramArguments"][1:4] == [
        "pushd",
        "fswatch",
        "resident-run",
    ]
    assert diffd_details["plist payload draft"]["ProgramArguments"][1:4] == [
        "diffd",
        "api-poll",
        "long-poll-run",
    ]
    assert pushd_details["future launchd gate env var"] == "PCLOUD_TOOLS_PUSHD_LAUNCHD_GATE"
    assert diffd_details["future launchd gate env var"] == "PCLOUD_TOOLS_DIFFD_LAUNCHD_GATE"
    assert "launchctl bootstrap" in pushd_details["blocked operations"]
    assert "writing LaunchAgent plist" in diffd_details["blocked operations"]
    assert diffd_details["approval status"] in {"pending", "complete-read-only"}
    assert not (workspace / ".dev-state" / "launchd").exists()
    assert not (state_dir / "pushd").exists()
    assert not (state_dir / "diffd").exists()


def test_service_launchd_status_is_read_only(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_launchctl = bin_dir / "launchctl"
    fake_launchctl.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"print\" ]; then\n"
        "  case \"$2\" in\n"
        "    *pcloud-diffd*) printf 'state = running\\n'; exit 0 ;;\n"
        "    *) printf 'Could not find service \"%s\"\\n' \"$2\" >&2; exit 113 ;;\n"
        "  esac\n"
        "fi\n"
        "exit 2\n"
    )
    fake_launchctl.chmod(0o755)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"

    pushd = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "launchd", "status", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    pushd_human = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "launchd", "status"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    diffd = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "diffd", "launchd", "status", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    pushd_payload = _payload(pushd)
    diffd_payload = _payload(diffd)
    pushd_details = pushd_payload["details"]
    diffd_details = diffd_payload["details"]

    assert pushd.returncode == 0
    assert pushd_human.returncode == 0
    assert diffd.returncode == 0
    assert "pushd launchd status:" in pushd_human.stdout
    assert "registration status: not_loaded" in pushd_human.stdout
    assert "state writes: none" in pushd_human.stdout
    assert pushd_payload["command"] == "pushd launchd status"
    assert diffd_payload["command"] == "diffd launchd status"
    assert pushd_details["implementation status"] == "read-only launchd status surface; launchctl print only"
    assert diffd_details["implementation status"] == "read-only launchd status surface; launchctl print only"
    assert pushd_details["registration status"] == "not_loaded"
    assert diffd_details["registration status"] == "loaded"
    assert pushd_details["launchd loaded"] == "no"
    assert diffd_details["launchd loaded"] == "yes"
    assert pushd_details["state writes"] == "none"
    assert diffd_details["state writes"] == "none"
    assert pushd_details["launchd can register"] == "no"
    assert diffd_details["launchd can register"] == "no"
    assert pushd_details["launchctl print command"][1] == "print"
    assert "launchctl bootstrap" in diffd_details["blocked operations"]
    assert "pushd.launchd.status" in [action["id"] for action in pushd_payload["actions"]]
    assert "diffd.launchd.status" in [action["id"] for action in diffd_payload["actions"]]
    assert not (workspace / ".dev-state" / "launchd").exists()
    assert not (state_dir / "pushd").exists()
    assert not (state_dir / "diffd").exists()


def test_service_launchd_review_is_read_only_and_uses_public_wrapper(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_pcloud_manager = bin_dir / "pcloud-manager"
    fake_pcloud_manager.write_text("#!/bin/sh\nprintf 'fake pcloud-manager\\n'\n")
    fake_pcloud_manager.chmod(0o755)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"

    pushd = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "launchd", "review", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    diffd = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "diffd", "launchd", "review", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    pushd_human = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "launchd", "review"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    pushd_payload = _payload(pushd)
    diffd_payload = _payload(diffd)
    pushd_details = pushd_payload["details"]
    diffd_details = diffd_payload["details"]

    assert pushd.returncode == 0
    assert diffd.returncode == 0
    assert pushd_human.returncode == 0
    assert pushd_payload["status"] == "warning"
    assert diffd_payload["status"] == "warning"
    assert pushd_details["state writes"] == "none"
    assert diffd_details["state writes"] == "none"
    assert pushd_details["launchctl execution"] == "no"
    assert pushd_details["persistent daemon start"] == "no"
    assert pushd_details["service label"] == "com.takafumi.pcloud-pushd"
    assert diffd_details["service label"] == "com.takafumi.pcloud-diffd"
    assert pushd_details["plist payload"]["ProgramArguments"][:4] == [
        str(fake_pcloud_manager),
        "pushd",
        "fswatch",
        "resident-run",
    ]
    assert diffd_details["foreground command preview"][:4] == [
        str(fake_pcloud_manager),
        "diffd",
        "api-poll",
        "long-poll-run",
    ]
    assert "pushd.launchd.review" in [action["id"] for action in pushd_payload["actions"]]
    assert "human review status:" in pushd_human.stdout
    assert "terminal review commands:" in pushd_human.stdout
    assert "launchctl bootstrap" in pushd_human.stdout
    assert not (Path(env["HOME"]) / "Library" / "LaunchAgents" / "com.takafumi.pcloud-pushd.plist").exists()
    assert not (state_dir / "pushd").exists()
    assert not (state_dir / "diffd").exists()


def test_service_launchd_plist_preview_and_dev_write_do_not_run_launchctl(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    launchctl_log = tmp_path / "launchctl.log"
    fake_launchctl = bin_dir / "launchctl"
    fake_launchctl.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$FAKE_LAUNCHCTL_LOG\"\nexit 0\n")
    fake_launchctl.chmod(0o755)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    env["FAKE_LAUNCHCTL_LOG"] = str(launchctl_log)
    pushd_plist = workspace / ".dev-state" / "launchd" / "com.example.pcloud-pushd.dev.plist"
    diffd_plist = workspace / ".dev-state" / "launchd" / "com.example.pcloud-diffd.dev.plist"

    pushd_preview = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "launchd", "plist", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    pushd_write = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "launchd", "plist", "--execute", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    diffd_write = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "diffd", "launchd", "plist", "--execute", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    pushd_human = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "launchd", "plist"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    pushd_preview_payload = _payload(pushd_preview)
    pushd_write_payload = _payload(pushd_write)
    diffd_write_payload = _payload(diffd_write)
    pushd_payload = plistlib.loads(pushd_plist.read_bytes())
    diffd_payload = plistlib.loads(diffd_plist.read_bytes())

    assert pushd_preview.returncode == 0
    assert pushd_write.returncode == 0
    assert diffd_write.returncode == 0
    assert pushd_human.returncode == 0
    assert pushd_preview_payload["details"]["state writes"] == "none"
    assert pushd_preview_payload["details"]["launchctl execution"] == "no"
    assert pushd_preview_payload["details"]["persistent daemon start"] == "no"
    assert pushd_preview_payload["details"]["plist status"] == "missing"
    assert pushd_preview_payload["details"]["plist payload"]["Label"] == "com.example.pcloud-pushd.dev"
    assert pushd_write_payload["summary"] == "pushd launchd plist written"
    assert pushd_write_payload["details"]["state writes"] == "launchd plist only"
    assert pushd_write_payload["details"]["launchctl execution"] == "no"
    assert diffd_write_payload["details"]["state writes"] == "launchd plist only"
    assert pushd_payload["Label"] == "com.example.pcloud-pushd.dev"
    assert diffd_payload["Label"] == "com.example.pcloud-diffd.dev"
    assert pushd_payload["ProgramArguments"][1:4] == ["pushd", "fswatch", "resident-run"]
    assert diffd_payload["ProgramArguments"][1:4] == ["diffd", "api-poll", "long-poll-run"]
    assert pushd_payload["WorkingDirectory"] == str(workspace)
    assert diffd_payload["WorkingDirectory"] == str(workspace)
    assert pushd_payload["StandardOutPath"].endswith("pushd-launchd.out")
    assert diffd_payload["StandardErrorPath"].endswith("diffd-launchd.err")
    assert pushd_payload["RunAtLoad"] is True
    assert pushd_payload["KeepAlive"] is False
    assert "program arguments:" in pushd_human.stdout
    assert "blocked operations:" in pushd_human.stdout
    assert not launchctl_log.exists()
    assert not (state_dir / "pushd").exists()
    assert not (state_dir / "diffd").exists()


def test_service_launchd_plist_execute_refuses_outside_dev_mode(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    env.pop("PCLOUD_TOOLS_DEV", None)

    result = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "launchd", "plist", "--execute", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    assert result.returncode == 1
    assert payload["status"] == "error"
    assert payload["details"]["state writes"] == "none"
    assert payload["details"]["launchctl execution"] == "no"
    assert "PCLOUD_TOOLS_PUSHD_LAUNCHD_PLIST_EXECUTION" in [issue["key"] for issue in payload["issues"]]
    assert not (workspace / ".dev-state" / "launchd" / "com.example.pcloud-pushd.dev.plist").exists()


def test_service_launchd_public_plist_write_requires_gate_and_does_not_bootstrap(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    home = Path(env["HOME"])
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    env.pop("PCLOUD_TOOLS_DEV", None)
    env["PCLOUD_TOOLS_CORE_DIR"] = str(workspace)
    env["PCLOUD_TOOLS_ALLOWLIST_FILE"] = str(workspace / ".pcloud-sync-allowlist")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    launchctl_log = tmp_path / "launchctl.log"
    fake_pcloud_manager = bin_dir / "pcloud-manager"
    fake_pcloud_manager.write_text("#!/bin/sh\nprintf 'fake pcloud-manager\\n'\n")
    fake_pcloud_manager.chmod(0o755)
    fake_launchctl = bin_dir / "launchctl"
    fake_launchctl.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$FAKE_LAUNCHCTL_LOG\"\nexit 0\n")
    fake_launchctl.chmod(0o755)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    env["FAKE_LAUNCHCTL_LOG"] = str(launchctl_log)
    pushd_public_plist = home / "Library" / "LaunchAgents" / "com.takafumi.pcloud-pushd.plist"
    diffd_public_plist = home / "Library" / "LaunchAgents" / "com.takafumi.pcloud-diffd.plist"

    closed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "launchd",
            "plist",
            "--execute",
            "--public-write",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    gated_env = dict(env)
    gated_env["PCLOUD_TOOLS_PUSHD_LAUNCHD_PLIST_GATE"] = "operator-approved-pushd-launchd-plist-v1"
    opened = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "launchd",
            "plist",
            "--execute",
            "--public-write",
            "--operator-reviewed-plist",
            "--reviewer-approved-public-target",
            "--reviewer-approved-no-bootstrap",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=gated_env,
    )

    closed_payload = _payload(closed)
    opened_payload = _payload(opened)
    public_payload = plistlib.loads(pushd_public_plist.read_bytes())

    assert closed.returncode == 1
    assert closed_payload["status"] == "error"
    assert closed_payload["details"]["state writes"] == "none"
    assert closed_payload["details"]["launchctl execution"] == "no"
    assert "PCLOUD_TOOLS_PUSHD_LAUNCHD_PLIST_PUBLIC_APPROVAL" in [
        issue["key"] for issue in closed_payload["issues"]
    ]
    assert opened.returncode == 0
    assert opened_payload["summary"] == "pushd launchd plist written"
    assert opened_payload["details"]["plist target kind"] == "public"
    assert opened_payload["details"]["state writes"] == "public launchd plist only"
    assert opened_payload["details"]["launchctl execution"] == "no"
    assert opened_payload["details"]["persistent daemon start"] == "no"
    assert public_payload["Label"] == "com.takafumi.pcloud-pushd"
    assert public_payload["ProgramArguments"][:4] == [
        str(fake_pcloud_manager),
        "pushd",
        "fswatch",
        "resident-run",
    ]
    assert public_payload["WorkingDirectory"] == str(workspace)
    assert public_payload["RunAtLoad"] is True
    assert public_payload["KeepAlive"] is False
    assert not diffd_public_plist.exists()
    assert not launchctl_log.exists()
    assert not (state_dir / "pushd").exists()
    assert not (state_dir / "diffd").exists()


def test_service_launchd_public_plist_write_refuses_dev_runtime(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    env["PCLOUD_TOOLS_PUSHD_LAUNCHD_PLIST_GATE"] = "operator-approved-pushd-launchd-plist-v1"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "launchd",
            "plist",
            "--execute",
            "--public-write",
            "--operator-reviewed-plist",
            "--reviewer-approved-public-target",
            "--reviewer-approved-no-bootstrap",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    assert result.returncode == 1
    assert payload["status"] == "error"
    assert payload["details"]["state writes"] == "none"
    assert payload["details"]["launchctl execution"] == "no"
    assert "PCLOUD_TOOLS_PUSHD_LAUNCHD_PLIST_PUBLIC_RUNTIME" in [
        issue["key"] for issue in payload["issues"]
    ]
    assert not (workspace / ".dev-state" / "launchd" / "com.example.pcloud-pushd.dev.plist").exists()


def test_service_launchd_register_is_gated_and_uses_fake_launchctl(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    home = Path(env["HOME"])
    env.pop("PCLOUD_TOOLS_DEV", None)
    env["PCLOUD_TOOLS_CORE_DIR"] = str(workspace)
    env["PCLOUD_TOOLS_ALLOWLIST_FILE"] = str(workspace / ".pcloud-sync-allowlist")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    launchctl_log = tmp_path / "launchctl.log"
    fake_launchctl = bin_dir / "launchctl"
    fake_launchctl.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$FAKE_LAUNCHCTL_LOG\"\nexit 0\n")
    fake_launchctl.chmod(0o755)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    env["FAKE_LAUNCHCTL_LOG"] = str(launchctl_log)
    public_plist = home / "Library" / "LaunchAgents" / "com.takafumi.pcloud-pushd.plist"
    public_plist.parent.mkdir(parents=True)
    public_plist.write_bytes(
        plistlib.dumps(
            {
                "Label": "com.takafumi.pcloud-pushd",
                "ProgramArguments": ["/tmp/pcloud-manager", "pushd", "fswatch", "resident-run"],
                "RunAtLoad": True,
                "KeepAlive": False,
                "WorkingDirectory": str(workspace),
                "StandardOutPath": str(tmp_path / "pushd.out"),
                "StandardErrorPath": str(tmp_path / "pushd.err"),
            }
        )
    )
    shadow_report = tmp_path / "shadow-validation.json"
    shadow_workspace = tmp_path / "pcloud-shadow-validation-register" / "workspace"
    shadow_report.write_text(
        json.dumps(
            {
                "status": "ok",
                "workspace": str(shadow_workspace),
                "state_dir": str(shadow_workspace / ".dev-state" / "state"),
                "checks": [
                    {"name": "temporary workspace guard", "status": "ok"},
                    {"name": "temporary state dir guard", "status": "ok"},
                    {"name": "unsafe state dir guard", "status": "ok"},
                    {"name": "launchd register shadow", "status": "ok"},
                ],
            }
        )
    )

    preview = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "launchd", "register", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    closed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "launchd",
            "register",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    gated_env = dict(env)
    gated_env["PCLOUD_TOOLS_PUSHD_LAUNCHD_GATE"] = "operator-approved-pushd-launchd-v1"
    opened = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "launchd",
            "register",
            "--report-path",
            str(shadow_report),
            "--operator-reviewed-daemon-command",
            "--reviewer-approved-plist-policy",
            "--reviewer-approved-launchctl-policy",
            "--reviewer-approved-rollback-policy",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=gated_env,
    )

    preview_payload = _payload(preview)
    closed_payload = _payload(closed)
    opened_payload = _payload(opened)
    launchctl_lines = launchctl_log.read_text().splitlines()

    assert preview.returncode == 0
    assert preview_payload["details"]["launchctl execution"] == "no"
    assert preview_payload["details"]["persistent daemon start"] == "no"
    assert preview_payload["details"]["launchd can register"] == "no"
    assert "pushd.launchd.register.preview" in [action["id"] for action in preview_payload["actions"]]
    assert closed.returncode == 1
    assert closed_payload["status"] == "error"
    assert closed_payload["details"]["launchctl execution"] == "no"
    assert "PCLOUD_TOOLS_PUSHD_LAUNCHD_REGISTER_APPROVAL" in [
        issue["key"] for issue in closed_payload["issues"]
    ]
    assert opened.returncode == 0
    assert opened_payload["summary"] == "pushd launchd registration completed"
    assert opened_payload["details"]["launchctl execution"] == "yes"
    assert opened_payload["details"]["persistent daemon start"] == "yes-if-bootstrap-succeeds"
    assert opened_payload["details"]["state writes"] == "launchctl registration only"
    assert len(opened_payload["details"]["launchctl results"]) == 2
    assert any(line.startswith("enable gui/") for line in launchctl_lines)
    assert any(line.startswith("bootstrap gui/") for line in launchctl_lines)


def test_pushd_launchd_resident_plist_write_is_gated_and_does_not_bootstrap(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    home = Path(env["HOME"])
    env.pop("PCLOUD_TOOLS_DEV", None)
    env["PCLOUD_TOOLS_CORE_DIR"] = str(workspace)
    env["PCLOUD_TOOLS_ALLOWLIST_FILE"] = str(workspace / ".pcloud-sync-allowlist")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    launchctl_log = tmp_path / "launchctl.log"
    fake_pcloud_manager = bin_dir / "pcloud-manager"
    fake_pcloud_manager.write_text("#!/bin/sh\nprintf 'fake pcloud-manager\\n'\n")
    fake_pcloud_manager.chmod(0o755)
    fake_fswatch = bin_dir / "fswatch"
    fake_fswatch.write_text("#!/bin/sh\nprintf 'fake fswatch\\n'\n")
    fake_fswatch.chmod(0o755)
    fake_launchctl = bin_dir / "launchctl"
    fake_launchctl.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$FAKE_LAUNCHCTL_LOG\"\nexit 0\n")
    fake_launchctl.chmod(0o755)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    env["FAKE_LAUNCHCTL_LOG"] = str(launchctl_log)
    public_plist = home / "Library" / "LaunchAgents" / "com.takafumi.pcloud-pushd.plist"
    shadow_report = tmp_path / "shadow-validation.json"
    shadow_workspace = tmp_path / "pcloud-shadow-validation-resident-plist" / "workspace"
    shadow_report.write_text(
        json.dumps(
            {
                "status": "ok",
                "workspace": str(shadow_workspace),
                "state_dir": str(shadow_workspace / ".dev-state" / "state"),
                "checks": [
                    {"name": "temporary workspace guard", "status": "ok"},
                    {"name": "temporary state dir guard", "status": "ok"},
                    {"name": "unsafe state dir guard", "status": "ok"},
                ],
            }
        )
    )

    preview = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "launchd",
            "resident-plist",
            "--report-path",
            str(shadow_report),
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    closed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "launchd",
            "resident-plist",
            "--report-path",
            str(shadow_report),
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    gated_env = dict(env)
    gated_env["PCLOUD_TOOLS_PUSHD_LAUNCHD_RESIDENT_PLIST_GATE"] = (
        "operator-approved-pushd-launchd-resident-plist-v1"
    )
    opened = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "launchd",
            "resident-plist",
            "--report-path",
            str(shadow_report),
            "--operator-reviewed-resident-command",
            "--reviewer-approved-resident-environment",
            "--reviewer-approved-no-bootstrap",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=gated_env,
    )

    preview_payload = _payload(preview)
    closed_payload = _payload(closed)
    opened_payload = _payload(opened)
    plist_payload = plistlib.loads(public_plist.read_bytes())

    assert preview.returncode == 0
    assert preview_payload["details"]["state writes"] == "none"
    assert preview_payload["details"]["launchctl execution"] == "no"
    assert "pushd.launchd.resident-plist.preview" in [
        action["id"] for action in preview_payload["actions"]
    ]
    assert closed.returncode == 1
    assert closed_payload["details"]["state writes"] == "none"
    assert "PCLOUD_TOOLS_PUSHD_LAUNCHD_RESIDENT_PLIST_APPROVAL" in [
        issue["key"] for issue in closed_payload["issues"]
    ]
    assert opened.returncode == 0
    assert opened_payload["summary"] == "pushd launchd resident plist written"
    assert opened_payload["details"]["state writes"] == "public launchd resident plist only"
    assert opened_payload["details"]["launchctl execution"] == "no"
    assert opened_payload["details"]["persistent daemon start"] == "no"
    assert plist_payload["Label"] == "com.takafumi.pcloud-pushd"
    assert plist_payload["ProgramArguments"][:4] == [
        str(fake_pcloud_manager),
        "pushd",
        "fswatch",
        "resident-run",
    ]
    assert "--execute" in plist_payload["ProgramArguments"]
    assert "--operator-reviewed-probe" in plist_payload["ProgramArguments"]
    assert "--reviewer-approved-queue-policy" in plist_payload["ProgramArguments"]
    assert "--reviewer-approved-process-policy" in plist_payload["ProgramArguments"]
    assert str(shadow_report.resolve()) in plist_payload["ProgramArguments"]
    assert plist_payload["EnvironmentVariables"]["PCLOUD_TOOLS_PUSHD_FSWATCH_RESIDENT_GATE"] == (
        "operator-approved-fswatch-resident-v1"
    )
    assert "/opt/homebrew/bin" in plist_payload["EnvironmentVariables"]["PATH"]
    assert not launchctl_log.exists()


def test_pushd_launchd_reload_is_gated_and_uses_fake_launchctl(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    home = Path(env["HOME"])
    env.pop("PCLOUD_TOOLS_DEV", None)
    env["PCLOUD_TOOLS_CORE_DIR"] = str(workspace)
    env["PCLOUD_TOOLS_ALLOWLIST_FILE"] = str(workspace / ".pcloud-sync-allowlist")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    launchctl_log = tmp_path / "launchctl.log"
    fake_launchctl = bin_dir / "launchctl"
    fake_launchctl.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$FAKE_LAUNCHCTL_LOG\"\nexit 0\n")
    fake_launchctl.chmod(0o755)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    env["FAKE_LAUNCHCTL_LOG"] = str(launchctl_log)
    public_plist = home / "Library" / "LaunchAgents" / "com.takafumi.pcloud-pushd.plist"
    public_plist.parent.mkdir(parents=True)
    public_plist.write_bytes(
        plistlib.dumps(
            {
                "Label": "com.takafumi.pcloud-pushd",
                "ProgramArguments": [
                    "/tmp/pcloud-manager",
                    "pushd",
                    "fswatch",
                    "resident-run",
                    "--operator-reviewed-probe",
                    "--reviewer-approved-queue-policy",
                    "--reviewer-approved-process-policy",
                    "--execute",
                    "--report-path",
                    str(tmp_path / "shadow-validation.json"),
                ],
                "EnvironmentVariables": {
                    "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                    "PCLOUD_TOOLS_PUSHD_FSWATCH_RESIDENT_GATE": "operator-approved-fswatch-resident-v1",
                },
                "RunAtLoad": True,
                "KeepAlive": False,
                "WorkingDirectory": str(workspace),
                "StandardOutPath": str(tmp_path / "pushd.out"),
                "StandardErrorPath": str(tmp_path / "pushd.err"),
            }
        )
    )
    shadow_report = tmp_path / "shadow-validation.json"
    shadow_workspace = tmp_path / "pcloud-shadow-validation-reload" / "workspace"
    shadow_report.write_text(
        json.dumps(
            {
                "status": "ok",
                "workspace": str(shadow_workspace),
                "state_dir": str(shadow_workspace / ".dev-state" / "state"),
                "checks": [
                    {"name": "temporary workspace guard", "status": "ok"},
                    {"name": "temporary state dir guard", "status": "ok"},
                    {"name": "unsafe state dir guard", "status": "ok"},
                ],
            }
        )
    )

    preview = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "launchd",
            "reload",
            "--report-path",
            str(shadow_report),
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    closed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "launchd",
            "reload",
            "--report-path",
            str(shadow_report),
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    gated_env = dict(env)
    gated_env["PCLOUD_TOOLS_PUSHD_LAUNCHD_RELOAD_GATE"] = "operator-approved-pushd-launchd-reload-v1"
    opened = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "launchd",
            "reload",
            "--report-path",
            str(shadow_report),
            "--operator-reviewed-resident-plist",
            "--reviewer-approved-bootout-bootstrap",
            "--reviewer-approved-rollback-policy",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=gated_env,
    )

    preview_payload = _payload(preview)
    closed_payload = _payload(closed)
    opened_payload = _payload(opened)
    launchctl_lines = launchctl_log.read_text().splitlines()

    assert preview.returncode == 0
    assert preview_payload["details"]["launchctl execution"] == "no"
    assert preview_payload["details"]["persistent daemon start"] == "no"
    assert preview_payload["details"]["resident plist status"] == "operational"
    assert "pushd.launchd.reload.preview" in [action["id"] for action in preview_payload["actions"]]
    assert closed.returncode == 1
    assert closed_payload["details"]["launchctl execution"] == "no"
    assert "PCLOUD_TOOLS_PUSHD_LAUNCHD_RELOAD_APPROVAL" in [
        issue["key"] for issue in closed_payload["issues"]
    ]
    assert opened.returncode == 0
    assert opened_payload["summary"] == "pushd launchd reload completed"
    assert opened_payload["details"]["launchctl execution"] == "yes"
    assert opened_payload["details"]["persistent daemon start"] == "yes-if-bootstrap-succeeds"
    assert opened_payload["details"]["state writes"] == "launchctl reload only"
    assert any(line.startswith("bootout gui/") for line in launchctl_lines)
    assert any(line.startswith("bootstrap gui/") for line in launchctl_lines)


def test_diffd_launchd_operational_plist_and_reload_are_gated(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    home = Path(env["HOME"])
    env.pop("PCLOUD_TOOLS_DEV", None)
    env["PCLOUD_TOOLS_CORE_DIR"] = str(workspace)
    env["PCLOUD_TOOLS_ALLOWLIST_FILE"] = str(workspace / ".pcloud-sync-allowlist")
    env["PCLOUD_TOOLS_PCLOUD_API_TOKEN"] = "test-token"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    launchctl_log = tmp_path / "launchctl.log"
    fake_pcloud_manager = bin_dir / "pcloud-manager"
    fake_pcloud_manager.write_text("#!/bin/sh\nprintf 'fake pcloud-manager\\n'\n")
    fake_pcloud_manager.chmod(0o755)
    fake_launchctl = bin_dir / "launchctl"
    fake_launchctl.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$FAKE_LAUNCHCTL_LOG\"\nexit 0\n")
    fake_launchctl.chmod(0o755)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    env["FAKE_LAUNCHCTL_LOG"] = str(launchctl_log)
    public_plist = home / "Library" / "LaunchAgents" / "com.takafumi.pcloud-diffd.plist"
    shadow_report = tmp_path / "shadow-validation.json"
    shadow_workspace = tmp_path / "pcloud-shadow-validation-diffd-operational" / "workspace"
    shadow_report.write_text(
        json.dumps(
            {
                "status": "ok",
                "workspace": str(shadow_workspace),
                "state_dir": str(shadow_workspace / ".dev-state" / "state"),
                "checks": [
                    {"name": "temporary workspace guard", "status": "ok"},
                    {"name": "temporary state dir guard", "status": "ok"},
                    {"name": "unsafe state dir guard", "status": "ok"},
                ],
            }
        )
    )

    preview = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "diffd",
            "launchd",
            "resident-plist",
            "--report-path",
            str(shadow_report),
            "--start-interval-seconds",
            "60",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    closed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "diffd",
            "launchd",
            "resident-plist",
            "--report-path",
            str(shadow_report),
            "--start-interval-seconds",
            "60",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    gated_env = dict(env)
    gated_env["PCLOUD_TOOLS_DIFFD_LAUNCHD_LONG_POLL_PLIST_GATE"] = (
        "operator-approved-diffd-launchd-long-poll-plist-v1"
    )
    opened = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "diffd",
            "launchd",
            "resident-plist",
            "--report-path",
            str(shadow_report),
            "--start-interval-seconds",
            "60",
            "--operator-reviewed-resident-command",
            "--reviewer-approved-resident-environment",
            "--reviewer-approved-no-bootstrap",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=gated_env,
    )

    preview_payload = _payload(preview)
    closed_payload = _payload(closed)
    opened_payload = _payload(opened)
    plist_payload = plistlib.loads(public_plist.read_bytes())

    assert preview.returncode == 0
    assert preview_payload["details"]["state writes"] == "none"
    assert preview_payload["details"]["start interval seconds"] == 60
    assert "diffd.launchd.resident-plist.preview" in [
        action["id"] for action in preview_payload["actions"]
    ]
    assert closed.returncode == 1
    assert "PCLOUD_TOOLS_DIFFD_LAUNCHD_RESIDENT_PLIST_APPROVAL" in [
        issue["key"] for issue in closed_payload["issues"]
    ]
    assert opened.returncode == 0
    assert opened_payload["summary"] == "diffd launchd resident plist written"
    assert opened_payload["details"]["launchctl execution"] == "no"
    assert plist_payload["ProgramArguments"][:4] == [
        str(fake_pcloud_manager),
        "diffd",
        "api-poll",
        "long-poll-run",
    ]
    assert "--live-api" in plist_payload["ProgramArguments"]
    assert "--max-iterations" in plist_payload["ProgramArguments"]
    assert "1" in plist_payload["ProgramArguments"]
    assert "--execute" in plist_payload["ProgramArguments"]
    assert plist_payload["StartInterval"] == 60
    assert opened_payload["details"]["start interval seconds"] == 60
    assert plist_payload["EnvironmentVariables"]["PCLOUD_TOOLS_DIFFD_API_LONG_POLL_GATE"] == (
        "operator-approved-api-long-poll-v1"
    )
    assert not launchctl_log.exists()

    reload_preview = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "diffd",
            "launchd",
            "reload",
            "--report-path",
            str(shadow_report),
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    reload_env = dict(env)
    reload_env["PCLOUD_TOOLS_DIFFD_LAUNCHD_RELOAD_GATE"] = "operator-approved-diffd-launchd-reload-v1"
    reload_opened = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "diffd",
            "launchd",
            "reload",
            "--report-path",
            str(shadow_report),
            "--operator-reviewed-resident-plist",
            "--reviewer-approved-bootout-bootstrap",
            "--reviewer-approved-rollback-policy",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=reload_env,
    )

    reload_preview_payload = _payload(reload_preview)
    reload_opened_payload = _payload(reload_opened)
    launchctl_lines = launchctl_log.read_text().splitlines()

    assert reload_preview.returncode == 0
    assert reload_preview_payload["details"]["resident plist status"] == "operational"
    assert "diffd.launchd.reload.preview" in [
        action["id"] for action in reload_preview_payload["actions"]
    ]
    assert reload_opened.returncode == 0
    assert reload_opened_payload["summary"] == "diffd launchd reload completed"
    assert reload_opened_payload["details"]["state writes"] == "launchctl reload only"
    assert any(line.startswith("bootout gui/") for line in launchctl_lines)
    assert any(line.startswith("bootstrap gui/") for line in launchctl_lines)


def test_transfer_check_is_read_only_and_reports_gate_prerequisites(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    pushd_dir = state_dir / "pushd"
    diffd_dir = state_dir / "diffd"
    pushd_dir.mkdir(parents=True)
    diffd_dir.mkdir(parents=True)
    _write_workspace_file(env, "Documents/upload.pdf", "upload\n")
    (pushd_dir / "queue.json").write_text(
        json.dumps([{"path": "Documents/upload.pdf", "action": "upload", "reason": "test"}])
    )
    (diffd_dir / "remote-changes.json").write_text(
        json.dumps([{"path": "Documents/download.pdf", "action": "download", "reason": "test"}])
    )
    shadow_report = tmp_path / "shadow-validation.json"
    shadow_workspace = tmp_path / "pcloud-shadow-validation-case" / "workspace"
    shadow_report.write_text(
        json.dumps(
            {
                "status": "ok",
                "workspace": str(shadow_workspace),
                "state_dir": str(shadow_workspace / ".dev-state" / "state"),
                "checks": [
                    {"name": "temporary workspace guard", "status": "ok"},
                    {"name": "temporary state dir guard", "status": "ok"},
                    {"name": "unsafe state dir guard", "status": "ok"},
                ],
            }
        )
    )

    pushd = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "transfer",
            "check",
            "--report-path",
            str(shadow_report),
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    diffd = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "diffd",
            "transfer",
            "check",
            "--report-path",
            str(shadow_report),
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    pushd_payload = _payload(pushd)
    diffd_payload = _payload(diffd)

    assert pushd.returncode == 0
    assert diffd.returncode == 0
    assert pushd_payload["status"] == "warning"
    assert diffd_payload["status"] == "warning"
    assert pushd_payload["details"]["implementation status"] == "read-only checklist; rclone is not executed"
    assert diffd_payload["details"]["implementation status"] == "read-only checklist; rclone is not executed"
    assert pushd_payload["details"]["real transfer gate status"] == "closed"
    assert diffd_payload["details"]["real transfer gate status"] == "closed"
    assert pushd_payload["details"]["real execution can run"] == "no"
    assert diffd_payload["details"]["real execution can run"] == "no"
    assert pushd_payload["details"]["real execution readiness"] == "blocked-final-review"
    assert diffd_payload["details"]["real execution readiness"] == "blocked-final-review"
    assert pushd_payload["details"]["state writes"] == "none"
    assert diffd_payload["details"]["state writes"] == "none"
    assert pushd_payload["details"]["sample path"] == "Documents/pushd-transfer-gate-sample.txt"
    assert diffd_payload["details"]["sample path"] == "Documents/diffd-transfer-gate-sample.txt"
    assert pushd_payload["details"]["sample path status"] == "ready"
    assert diffd_payload["details"]["sample path status"] == "ready"
    assert pushd_payload["details"]["first planned transfer status"] == "ready"
    assert diffd_payload["details"]["first planned transfer status"] == "ready"
    assert pushd_payload["details"]["expected after sample setup"]["first planned transfer status"] == "ready"
    assert diffd_payload["details"]["expected after sample setup"]["first planned transfer status"] == "ready"
    assert pushd_payload["details"]["preflight checks"][0]["status"] == "ok"
    assert diffd_payload["details"]["preflight checks"][0]["status"] == "ok"
    assert pushd_payload["details"]["first planned transfer"]["path"] == "Documents/upload.pdf"
    assert diffd_payload["details"]["first planned transfer"]["path"] == "Documents/download.pdf"
    assert pushd_payload["details"]["dev-state sample setup command"][1:4] == ["pushd", "queue", "add"]
    assert diffd_payload["details"]["dev-state sample setup command"][1:4] == [
        "diffd",
        "remote-change",
        "add",
    ]
    assert len(pushd_payload["details"]["review command sequence"]) == 4
    assert len(diffd_payload["details"]["review command sequence"]) == 4
    assert pushd_payload["details"]["review command sequence"][2][1:4] == ["pushd", "transfer", "check"]
    assert diffd_payload["details"]["review command sequence"][2][1:4] == ["diffd", "transfer", "check"]
    assert pushd_payload["details"]["review command sequence"][3][1:4] == ["pushd", "queue", "remove"]
    assert diffd_payload["details"]["review command sequence"][3][1:4] == [
        "diffd",
        "remote-change",
        "remove",
    ]
    assert "PCLOUD_TOOLS_REAL_TRANSFER_GATE" in [issue["key"] for issue in pushd_payload["issues"]]
    assert "PCLOUD_TOOLS_REAL_TRANSFER_GATE" in [issue["key"] for issue in diffd_payload["issues"]]
    assert not (pushd_dir / "last-transfer.json").exists()
    assert not (diffd_dir / "last-transfer.json").exists()


def test_transfer_check_rejects_incomplete_shadow_report_without_state_writes(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    pushd_dir = state_dir / "pushd"
    pushd_dir.mkdir(parents=True)
    _write_workspace_file(env, "Documents/upload.pdf", "upload\n")
    (pushd_dir / "queue.json").write_text(
        json.dumps([{"path": "Documents/upload.pdf", "action": "upload", "reason": "test"}])
    )
    shadow_report = tmp_path / "shadow-validation-incomplete.json"
    shadow_report.write_text(
        json.dumps(
            {
                "status": "ok",
                "workspace": str(tmp_path / "workspace"),
                "state_dir": str(tmp_path / "state"),
                "checks": [
                    {"name": "temporary workspace guard", "status": "ok"},
                ],
            }
        )
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "transfer",
            "check",
            "--report-path",
            str(shadow_report),
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    shadow_check = payload["details"]["preflight checks"][0]

    assert result.returncode == 0
    assert payload["status"] == "warning"
    assert payload["details"]["first planned transfer status"] == "ready"
    assert payload["details"]["review command sequence"][2][1:4] == ["pushd", "transfer", "check"]
    assert payload["details"]["review command sequence"][3][1:4] == ["pushd", "queue", "remove"]
    assert shadow_check["status"] == "not-ok"
    assert "missing required checks" in shadow_check["detail"]
    assert "temp workspace ok=False" in shadow_check["detail"]
    assert "PCLOUD_TOOLS_REAL_TRANSFER_SHADOW_REPORT" in [issue["key"] for issue in payload["issues"]]
    assert not (pushd_dir / "last-transfer.json").exists()


def test_transfer_check_default_sample_uses_allowlist_root(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    (workspace / ".pcloud-sync-allowlist").write_text("dev-fixtures/Documents/\n")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "transfer",
            "check",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    payload = _payload(result)

    assert result.returncode == 0
    assert payload["details"]["sample path"] == "dev-fixtures/Documents/pushd-transfer-gate-sample.txt"
    assert payload["details"]["sample path status"] == "ready"
    assert payload["details"]["dev-state sample setup command"][4] == (
        "dev-fixtures/Documents/pushd-transfer-gate-sample.txt"
    )


def test_transfer_check_accepts_operator_confirmations_without_opening_gate(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    pushd_dir = state_dir / "pushd"
    diffd_dir = state_dir / "diffd"
    pushd_dir.mkdir(parents=True)
    diffd_dir.mkdir(parents=True)
    _write_workspace_file(env, "Documents/first-upload.txt", "upload\n")
    (pushd_dir / "queue.json").write_text(
        json.dumps([{"path": "Documents/first-upload.txt", "action": "upload", "reason": "test"}])
    )
    (diffd_dir / "remote-changes.json").write_text(
        json.dumps([{"path": "Documents/first-download.txt", "action": "download", "reason": "test"}])
    )

    pushd = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "transfer",
            "check",
            "--confirm-path",
            "Documents/first-upload.txt",
            "--confirm-direction",
            "upload",
            "--consume-policy",
            "remove-on-success-retain-on-failure",
            "--timeout-policy",
            "reuse-fake-rclone-cleanup",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    diffd = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "diffd",
            "transfer",
            "check",
            "--confirm-path",
            "Documents/first-download.txt",
            "--confirm-direction",
            "download",
            "--consume-policy",
            "remove-on-success-retain-on-failure",
            "--timeout-policy",
            "reuse-fake-rclone-cleanup",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    pushd_payload = _payload(pushd)
    diffd_payload = _payload(diffd)
    pushd_checks = {check["name"]: check for check in pushd_payload["details"]["preflight checks"]}
    diffd_checks = {check["name"]: check for check in diffd_payload["details"]["preflight checks"]}

    assert pushd.returncode == 0
    assert diffd.returncode == 0
    assert pushd_payload["details"]["real transfer gate status"] == "closed"
    assert diffd_payload["details"]["real transfer gate status"] == "closed"
    assert pushd_payload["details"]["state writes"] == "none"
    assert diffd_payload["details"]["state writes"] == "none"
    assert pushd_payload["details"]["operator target confirmation status"] == "ok"
    assert diffd_payload["details"]["operator target confirmation status"] == "ok"
    assert pushd_payload["details"]["consume policy status"] == "ok"
    assert diffd_payload["details"]["consume policy status"] == "ok"
    assert pushd_payload["details"]["timeout policy status"] == "ok"
    assert diffd_payload["details"]["timeout policy status"] == "ok"
    assert pushd_checks["first real run target"]["status"] == "ok"
    assert diffd_checks["first real run target"]["status"] == "ok"
    assert pushd_checks["queue/change consumption policy"]["status"] == "ok"
    assert diffd_checks["queue/change consumption policy"]["status"] == "ok"
    assert pushd_checks["timeout/process cleanup policy"]["status"] == "ok"
    assert diffd_checks["timeout/process cleanup policy"]["status"] == "ok"
    assert "PCLOUD_TOOLS_PUSHD_REAL_TRANSFER_TARGET_CONFIRMATION" not in [
        issue["key"] for issue in pushd_payload["issues"]
    ]
    assert "PCLOUD_TOOLS_DIFFD_REAL_TRANSFER_TARGET_CONFIRMATION" not in [
        issue["key"] for issue in diffd_payload["issues"]
    ]


def test_transfer_check_final_review_shows_dry_run_commands_without_opening_gate(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    pushd_dir = state_dir / "pushd"
    diffd_dir = state_dir / "diffd"
    pushd_dir.mkdir(parents=True)
    diffd_dir.mkdir(parents=True)
    _write_workspace_file(env, "Documents/final-upload.txt", "upload\n")
    (pushd_dir / "queue.json").write_text(
        json.dumps([{"path": "Documents/final-upload.txt", "action": "upload", "reason": "test"}])
    )
    (diffd_dir / "remote-changes.json").write_text(
        json.dumps([{"path": "Documents/final-download.txt", "action": "download", "reason": "test"}])
    )
    shadow_workspace = tmp_path / "pcloud-shadow-validation-final" / "workspace"
    shadow_report = tmp_path / "shadow-validation-final.json"
    shadow_report.write_text(
        json.dumps(
            {
                "status": "ok",
                "workspace": str(shadow_workspace),
                "state_dir": str(shadow_workspace / ".dev-state" / "state"),
                "checks": [
                    {"name": "temporary workspace guard", "status": "ok"},
                    {"name": "temporary state dir guard", "status": "ok"},
                    {"name": "unsafe state dir guard", "status": "ok"},
                ],
            }
        )
    )

    pushd = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "transfer",
            "check",
            "--report-path",
            str(shadow_report),
            "--confirm-path",
            "Documents/final-upload.txt",
            "--confirm-direction",
            "upload",
            "--consume-policy",
            "remove-on-success-retain-on-failure",
            "--timeout-policy",
            "reuse-fake-rclone-cleanup",
            "--final-review",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    diffd = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "diffd",
            "transfer",
            "check",
            "--report-path",
            str(shadow_report),
            "--confirm-path",
            "Documents/final-download.txt",
            "--confirm-direction",
            "download",
            "--consume-policy",
            "remove-on-success-retain-on-failure",
            "--timeout-policy",
            "reuse-fake-rclone-cleanup",
            "--final-review",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    pushd_payload = _payload(pushd)
    diffd_payload = _payload(diffd)

    assert pushd.returncode == 0
    assert diffd.returncode == 0
    assert pushd_payload["details"]["final review requested"] is True
    assert diffd_payload["details"]["final review requested"] is True
    assert pushd_payload["details"]["final review status"] == "ready"
    assert diffd_payload["details"]["final review status"] == "ready"
    assert pushd_payload["details"]["final review blockers"] == []
    assert diffd_payload["details"]["final review blockers"] == []
    assert pushd_payload["details"]["real transfer gate opening status"] == "ready-for-separate-gate"
    assert diffd_payload["details"]["real transfer gate opening status"] == "ready-for-separate-gate"
    assert "real transfer execution is still unavailable" in pushd_payload["details"][
        "real transfer gate opening note"
    ]
    assert any(
        "real execute gate must be added separately" in item
        for item in pushd_payload["details"]["separate real gate next checks"]
    )
    assert pushd_payload["details"]["dry-run transfer command"][-1] == "--dry-run"
    assert diffd_payload["details"]["dry-run transfer command"][-1] == "--dry-run"
    assert pushd_payload["details"]["real transfer command"][-2:] == [
        str(Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"]) / "Documents" / "final-upload.txt"),
        "pcloud:core/Documents/final-upload.txt",
    ]
    assert diffd_payload["details"]["real transfer command"][-2:] == [
        "pcloud:core/Documents/final-download.txt",
        str(Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"]) / "Documents" / "final-download.txt"),
    ]
    assert pushd_payload["details"]["real transfer gate status"] == "closed"
    assert diffd_payload["details"]["real transfer gate status"] == "closed"
    assert pushd_payload["details"]["state writes"] == "none"
    assert diffd_payload["details"]["state writes"] == "none"


def test_transfer_check_final_review_blocked_human_output_is_actionable(tmp_path: Path) -> None:
    env = _base_env(tmp_path)

    human = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "transfer",
            "check",
            "--final-review",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    structured = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "transfer",
            "check",
            "--final-review",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(structured)
    blocker_names = {
        item["name"] for item in payload["details"]["final review blocker details"]
    }

    assert human.returncode == 0
    assert structured.returncode == 0
    assert "final review: blocked" in human.stdout
    assert "blocked checks:" in human.stdout
    assert "- saved shadow validation report: pending" in human.stdout
    assert "- first real run target: pending" in human.stdout
    assert "- queue/change consumption policy: pending" in human.stdout
    assert "- timeout/process cleanup policy: pending" in human.stdout
    assert "- planned transfer count: not-ok" in human.stdout
    assert "dry-run note: blocked; fix the listed checks" in human.stdout
    assert "dry-run command:" not in human.stdout
    assert "real command:" not in human.stdout

    assert payload["details"]["final review status"] == "blocked"
    assert payload["details"]["dry-run display status"] == "blocked"
    assert payload["details"]["real transfer gate opening status"] == "blocked"
    assert payload["details"]["separate real gate next checks"] == []
    assert payload["details"]["dry-run transfer command"] == []
    assert payload["details"]["real transfer command"] == []
    assert "saved shadow validation report" in blocker_names
    assert "first real run target" in blocker_names
    assert "planned transfer count" in blocker_names
    assert payload["details"]["real transfer gate status"] == "closed"
    assert payload["details"]["state writes"] == "none"


def test_transfer_real_gate_is_read_only_scaffold(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    pushd_dir = state_dir / "pushd"
    pushd_dir.mkdir(parents=True)
    _write_workspace_file(env, "Documents/real-gate.txt", "upload\n")
    (pushd_dir / "queue.json").write_text(
        json.dumps([{"path": "Documents/real-gate.txt", "action": "upload", "reason": "test"}])
    )
    shadow_workspace = tmp_path / "pcloud-shadow-validation-real-gate" / "workspace"
    shadow_report = tmp_path / "shadow-validation-real-gate.json"
    shadow_report.write_text(
        json.dumps(
            {
                "status": "ok",
                "workspace": str(shadow_workspace),
                "state_dir": str(shadow_workspace / ".dev-state" / "state"),
                "checks": [
                    {"name": "temporary workspace guard", "status": "ok"},
                    {"name": "temporary state dir guard", "status": "ok"},
                    {"name": "unsafe state dir guard", "status": "ok"},
                ],
            }
        )
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "transfer",
            "real-gate",
            "--report-path",
            str(shadow_report),
            "--confirm-path",
            "Documents/real-gate.txt",
            "--confirm-direction",
            "upload",
            "--consume-policy",
            "remove-on-success-retain-on-failure",
            "--timeout-policy",
            "reuse-fake-rclone-cleanup",
            "--operator-reviewed-dry-run",
            "--reviewer-approved-real-command",
            "--reviewer-approved-consume-policy",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    standalone = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from pcloud_tools.cli_service_daemon import main_pushd; "
                "raise SystemExit(main_pushd(['transfer','real-gate','--json']))"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    standalone_payload = _payload(standalone)

    assert result.returncode == 0
    assert standalone.returncode == 0
    assert payload["command"] == "pushd transfer real-gate"
    assert standalone_payload["command"] == "pushd transfer real-gate"
    assert payload["summary"] == "pushd real transfer execution gate is closed"
    assert payload["details"]["implementation status"].startswith("read-only real execution gate scaffold")
    assert payload["details"]["final review status"] == "ready"
    assert payload["details"]["real transfer gate opening status"] == "ready-for-separate-gate"
    assert payload["details"]["real transfer execution gate status"].startswith(
        "closed: requires PCLOUD_TOOLS_REAL_TRANSFER_EXECUTION_GATE="
    )
    assert payload["details"]["future real gate env var"] == "PCLOUD_TOOLS_REAL_TRANSFER_EXECUTION_GATE"
    assert payload["details"]["future real gate accepted value"] == "operator-approved-real-transfer-v1"
    assert payload["details"]["fake-rclone gate reuse"] == "forbidden"
    assert payload["details"]["separate real gate approval status"] == "complete-read-only"
    assert {
        check["status"] for check in payload["details"]["separate real gate approval checks"]
    } == {"ok"}
    assert payload["details"]["operator verification required"] == "not-now"
    assert "actual pCloud/rclone transfer" in payload["details"]["next human check trigger"]
    assert standalone_payload["details"]["operator verification required"] == "no"
    assert payload["details"]["human gate status"] == "required-before-actual-transfer"
    assert "explicit operator run command" in payload["details"]["human gate reason"]
    assert standalone_payload["details"]["human gate status"] == "not-yet"
    assert payload["details"]["real execution readiness"] == "blocked-execution-gate"
    assert payload["details"]["real execution can run"] == "no"
    assert standalone_payload["details"]["real execution readiness"] == "blocked-final-review"
    assert payload["details"]["future real-run policy status"] == "documented-read-only"
    assert "pushd queue record" in payload["details"]["future real-run success policy"]
    assert "retain matching pushd queue record" in payload["details"]["future real-run failure policy"]
    assert payload["details"]["future real-run policy state writes"] == "none"
    assert payload["details"]["state writes"] == "none"
    assert "PCLOUD_TOOLS_REAL_TRANSFER_EXECUTION_GATE" in [issue["key"] for issue in payload["issues"]]
    assert not (pushd_dir / "last-transfer.json").exists()


def test_transfer_automation_gate_is_read_only_and_blocks_public_executor(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    pushd_dir = state_dir / "pushd"
    diffd_dir = state_dir / "diffd"
    pushd_dir.mkdir(parents=True)
    diffd_dir.mkdir(parents=True)
    _write_workspace_file(env, "Documents/auto-upload.txt", "upload\n")
    (pushd_dir / "queue.json").write_text(
        json.dumps([{"path": "Documents/auto-upload.txt", "action": "upload", "reason": "test"}])
    )
    (diffd_dir / "remote-changes.json").write_text(
        json.dumps(
            [
                {"path": "Documents/auto-download.txt", "action": "download", "reason": "test"},
                {"path": "Documents/auto-download-2.txt", "action": "download", "reason": "test"},
            ]
        )
    )
    shadow_workspace = tmp_path / "pcloud-shadow-validation-automation-gate" / "workspace"
    shadow_report = tmp_path / "shadow-validation-automation-gate.json"
    shadow_report.write_text(
        json.dumps(
            {
                "status": "ok",
                "workspace": str(shadow_workspace),
                "state_dir": str(shadow_workspace / ".dev-state" / "state"),
                "checks": [
                    {"name": "temporary workspace guard", "status": "ok"},
                    {"name": "temporary state dir guard", "status": "ok"},
                    {"name": "unsafe state dir guard", "status": "ok"},
                ],
            }
        )
    )

    pushd = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "transfer",
            "automation-gate",
            "--report-path",
            str(shadow_report),
            "--confirm-path",
            "Documents/auto-upload.txt",
            "--confirm-direction",
            "upload",
            "--consume-policy",
            "remove-on-success-retain-on-failure",
            "--timeout-policy",
            "reuse-fake-rclone-cleanup",
            "--operator-reviewed-dry-run",
            "--reviewer-approved-real-command",
            "--reviewer-approved-consume-policy",
            "--operator-reviewed-real-transfer-gate",
            "--reviewer-approved-automation-command",
            "--reviewer-approved-launchd-policy",
            "--reviewer-approved-rollback-policy",
            "--start-interval-seconds",
            "45",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env
        | {
            "PCLOUD_TOOLS_REAL_TRANSFER_AUTOMATION_GATE": "operator-approved-real-transfer-automation-v1"
        },
    )
    diffd = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "diffd",
            "transfer",
            "automation-gate",
            "--report-path",
            str(shadow_report),
            "--confirm-path",
            "Documents/auto-download.txt",
            "--confirm-direction",
            "download",
            "--consume-policy",
            "remove-on-success-retain-on-failure",
            "--timeout-policy",
            "reuse-fake-rclone-cleanup",
            "--operator-reviewed-dry-run",
            "--reviewer-approved-real-command",
            "--reviewer-approved-consume-policy",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    pushd_payload = _payload(pushd)
    diffd_payload = _payload(diffd)

    assert pushd.returncode == 0
    assert diffd.returncode == 0
    assert pushd_payload["summary"] == "pushd real transfer automation gate is closed"
    assert diffd_payload["summary"] == "diffd real transfer automation gate is closed"
    assert pushd_payload["details"]["automation gate status"] == "closed"
    assert pushd_payload["details"]["automation can run"] == "no"
    assert pushd_payload["details"]["automation command status"] == "implemented-gated"
    assert pushd_payload["details"]["automation gate env provided"] == "yes"
    assert pushd_payload["details"]["automation gate env honored"] == "no"
    assert pushd_payload["details"]["planned public executor service label"] == "com.takafumi.pcloud-pushd-executor"
    assert pushd_payload["details"]["planned public executor StartInterval"] == 45
    assert pushd_payload["details"]["future automation command"][1:4] == [
        "pushd",
        "transfer",
        "automation-run",
    ]
    assert pushd_payload["details"]["state writes"] == "none"
    assert pushd_payload["details"]["launchctl execution"] == "no"
    assert pushd_payload["details"]["public plist writes"] == "no"
    assert pushd_payload["details"]["automatic real transfer execution"] == "no"
    assert pushd_payload["details"]["automation approval status"] == "ready-for-launchd-review"
    assert any(
        check["name"] == "automation command implementation" and check["status"] == "ok"
        for check in pushd_payload["details"]["automation approval checks"]
    )
    assert "pushd.transfer.automation-gate" in [
        action["id"] for action in pushd_payload["actions"]
    ]
    assert diffd_payload["details"]["planned public executor service label"] == "com.takafumi.pcloud-diffd-executor"
    assert diffd_payload["details"]["automation gate env provided"] == "no"
    assert not (pushd_dir / "last-transfer.json").exists()
    assert not (diffd_dir / "last-transfer.json").exists()


def test_transfer_automation_gate_accepts_prior_successful_real_run_when_queue_is_empty(
    tmp_path: Path,
) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    pushd_dir = state_dir / "pushd"
    pushd_dir.mkdir(parents=True)
    _write_workspace_file(env, "Documents/selected-real-run.txt", "selected\n")
    _write_workspace_file(env, "Documents/retained-real-run.txt", "retained\n")
    (pushd_dir / "queue.json").write_text("[]")
    (pushd_dir / "last-transfer.json").write_text(
        json.dumps(
            {
                "service": "pushd",
                "mode": "real-rclone-transfer",
                "generated_at": "2026-05-07T00:00:00+00:00",
                "planned_transfer_commands": [
                    {
                        "command": ["rclone", "copyto", "local", "remote"],
                        "direction": "upload",
                        "path": "Documents/validated-upload.txt",
                        "reason": "test",
                    }
                ],
                "results": [
                    {
                        "command": ["rclone", "copyto", "local", "remote"],
                        "direction": "upload",
                        "path": "Documents/validated-upload.txt",
                        "reason": "test",
                        "returncode": 0,
                        "stdout": "",
                        "stderr": "",
                        "timed_out": False,
                    }
                ],
            }
        )
    )
    shadow_workspace = tmp_path / "pcloud-shadow-validation-prior-real-run" / "workspace"
    shadow_report = tmp_path / "shadow-validation-prior-real-run.json"
    shadow_report.write_text(
        json.dumps(
            {
                "status": "ok",
                "workspace": str(shadow_workspace),
                "state_dir": str(shadow_workspace / ".dev-state" / "state"),
                "checks": [
                    {"name": "temporary workspace guard", "status": "ok"},
                    {"name": "temporary state dir guard", "status": "ok"},
                    {"name": "unsafe state dir guard", "status": "ok"},
                ],
            }
        )
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "transfer",
            "automation-gate",
            "--report-path",
            str(shadow_report),
            "--consume-policy",
            "remove-on-success-retain-on-failure",
            "--timeout-policy",
            "reuse-fake-rclone-cleanup",
            "--reviewer-approved-consume-policy",
            "--operator-reviewed-real-transfer-gate",
            "--reviewer-approved-automation-command",
            "--reviewer-approved-launchd-policy",
            "--reviewer-approved-rollback-policy",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env
        | {
            "PCLOUD_TOOLS_REAL_TRANSFER_AUTOMATION_GATE": "operator-approved-real-transfer-automation-v1"
        },
    )

    payload = _payload(result)

    assert result.returncode == 0
    assert payload["details"]["planned uploads"] == 0
    assert payload["details"]["prior real transfer validation status"] == "ok"
    assert payload["details"]["real transfer approvals source"] == "prior successful real-run"
    assert payload["details"]["automation approval status"] == "ready-for-launchd-review"
    assert any(
        check["name"] == "prior real-transfer validation" and check["status"] == "ok"
        for check in payload["details"]["automation approval checks"]
    )
    assert payload["details"]["state writes"] == "none"
    assert "PCLOUD_TOOLS_REAL_TRANSFER_TARGET" in [issue["key"] for issue in payload["issues"]]


def test_transfer_automation_gate_accepts_prior_successful_automation_run_when_queue_is_empty(
    tmp_path: Path,
) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    diffd_dir = state_dir / "diffd"
    diffd_dir.mkdir(parents=True)
    (diffd_dir / "remote-changes.json").write_text("[]")
    (diffd_dir / "last-transfer.json").write_text(
        json.dumps(
            {
                "service": "diffd",
                "mode": "real-rclone-automation-transfer",
                "generated_at": "2026-05-07T00:00:00+00:00",
                "planned_transfer_commands": [
                    {
                        "command": ["rclone", "copyto", "remote", "local"],
                        "direction": "download",
                        "path": "Documents/validated-download.txt",
                        "reason": "test",
                    }
                ],
                "results": [
                    {
                        "command": ["rclone", "copyto", "remote", "local"],
                        "direction": "download",
                        "path": "Documents/validated-download.txt",
                        "reason": "test",
                        "returncode": 0,
                        "stdout": "",
                        "stderr": "",
                        "timed_out": False,
                    }
                ],
            }
        )
    )
    shadow_workspace = tmp_path / "pcloud-shadow-validation-prior-automation-run" / "workspace"
    shadow_report = tmp_path / "shadow-validation-prior-automation-run.json"
    shadow_report.write_text(
        json.dumps(
            {
                "status": "ok",
                "workspace": str(shadow_workspace),
                "state_dir": str(shadow_workspace / ".dev-state" / "state"),
                "checks": [
                    {"name": "temporary workspace guard", "status": "ok"},
                    {"name": "temporary state dir guard", "status": "ok"},
                    {"name": "unsafe state dir guard", "status": "ok"},
                ],
            }
        )
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "diffd",
            "transfer",
            "automation-gate",
            "--report-path",
            str(shadow_report),
            "--consume-policy",
            "remove-on-success-retain-on-failure",
            "--timeout-policy",
            "reuse-fake-rclone-cleanup",
            "--reviewer-approved-consume-policy",
            "--operator-reviewed-real-transfer-gate",
            "--reviewer-approved-automation-command",
            "--reviewer-approved-launchd-policy",
            "--reviewer-approved-rollback-policy",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env
        | {
            "PCLOUD_TOOLS_REAL_TRANSFER_AUTOMATION_GATE": "operator-approved-real-transfer-automation-v1"
        },
    )

    payload = _payload(result)

    assert result.returncode == 0
    assert payload["details"]["planned downloads"] == 0
    assert payload["details"]["prior real transfer validation status"] == "ok"
    assert payload["details"]["prior real transfer mode"] == "real-rclone-automation-transfer"
    assert payload["details"]["automation approval status"] == "ready-for-launchd-review"
    assert payload["details"]["state writes"] == "none"
    assert "PCLOUD_TOOLS_REAL_TRANSFER_TARGET" in [issue["key"] for issue in payload["issues"]]


def test_transfer_automation_run_is_guarded_and_consumes_successes(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    real_log = _install_real_rclone_stub(env)
    pushd_dir = state_dir / "pushd"
    diffd_dir = state_dir / "diffd"
    pushd_dir.mkdir(parents=True)
    diffd_dir.mkdir(parents=True)
    _write_workspace_file(env, "Documents/upload.pdf", "upload\n")
    (pushd_dir / "queue.json").write_text(
        json.dumps([{"path": "Documents/auto-upload.txt", "action": "upload", "reason": "test"}])
    )
    (diffd_dir / "remote-changes.json").write_text(
        json.dumps(
            [
                {"path": "Documents/auto-download.txt", "action": "download", "reason": "test"},
                {"path": "Documents/auto-download-2.txt", "action": "download", "reason": "test"},
            ]
        )
    )
    opened_env = env | {
        "PCLOUD_TOOLS_REAL_TRANSFER_EXECUTION_GATE": "operator-approved-real-transfer-v1",
        "PCLOUD_TOOLS_REAL_TRANSFER_AUTOMATION_GATE": "operator-approved-real-transfer-automation-v1",
    }
    shadow_workspace = tmp_path / "pcloud-shadow-validation-automation-run" / "workspace"
    shadow_report = tmp_path / "shadow-validation-automation-run.json"
    shadow_report.write_text(
        json.dumps(
            {
                "status": "ok",
                "workspace": str(shadow_workspace),
                "state_dir": str(shadow_workspace / ".dev-state" / "state"),
                "checks": [
                    {"name": "temporary workspace guard", "status": "ok"},
                    {"name": "temporary state dir guard", "status": "ok"},
                    {"name": "unsafe state dir guard", "status": "ok"},
                ],
            }
        )
    )

    refused = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "transfer",
            "automation-run",
            "--execute",
            "--consume-on-success",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=opened_env,
    )
    execute_env = opened_env | {
        "PCLOUD_TOOLS_REAL_TRANSFER_AUTOMATION_RUN_GATE": "operator-approved-real-transfer-automation-run-v1"
    }
    diffd = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "diffd",
            "transfer",
            "automation-run",
            "--report-path",
            str(shadow_report),
            "--execute",
            "--consume-on-success",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=execute_env,
    )

    refused_payload = _payload(refused)
    diffd_payload = _payload(diffd)

    assert refused.returncode == 1
    assert refused_payload["summary"] == "pushd transfer automation-run refused"
    assert refused_payload["details"]["automation command status"] == "implemented-gated"
    assert refused_payload["details"]["automation can run"] == "no"
    assert refused_payload["details"]["real transfer gate env honored"] == "yes"
    assert refused_payload["details"]["automation gate env honored"] == "yes"
    assert refused_payload["details"]["automation run gate env honored"] == "no"
    assert refused_payload["details"]["state writes"] == "none"
    assert refused_payload["details"]["automatic real transfer execution"] == "no"
    assert refused_payload["details"]["automatic queue/change consumption"] == "no"
    assert json.loads((pushd_dir / "queue.json").read_text())[0]["path"] == "Documents/auto-upload.txt"
    assert not (pushd_dir / "last-transfer.json").exists()
    assert diffd.returncode == 0
    assert diffd_payload["summary"] == "diffd transfer automation-run completed"
    assert diffd_payload["details"]["automation can run"] == "yes"
    assert diffd_payload["details"]["automation run gate env honored"] == "yes"
    assert diffd_payload["details"]["automatic real transfer execution"] == "yes"
    assert diffd_payload["details"]["automatic queue/change consumption"] == "yes"
    assert diffd_payload["details"]["automation batch limit"] == 1
    assert diffd_payload["details"]["planned transfer command count"] == 2
    assert diffd_payload["details"]["execution transfer command count"] == 1
    assert diffd_payload["details"]["deferred transfer command count"] == 1
    assert diffd_payload["details"]["records consumed"] == 1
    assert json.loads((diffd_dir / "remote-changes.json").read_text()) == [
        {"path": "Documents/auto-download-2.txt", "action": "download", "reason": "test"}
    ]
    assert (diffd_dir / "last-transfer.json").exists()
    assert len(real_log.read_text().splitlines()) == 1


def test_launchd_automation_plist_and_reload_are_preview_only(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    pushd_dir = state_dir / "pushd"
    diffd_dir = state_dir / "diffd"
    pushd_dir.mkdir(parents=True)
    diffd_dir.mkdir(parents=True)
    _write_workspace_file(env, "Documents/upload.pdf", "upload\n")
    (pushd_dir / "queue.json").write_text(
        json.dumps([{"path": "Documents/auto-upload.txt", "action": "upload", "reason": "test"}])
    )
    (diffd_dir / "remote-changes.json").write_text(
        json.dumps([{"path": "Documents/auto-download.txt", "action": "download", "reason": "test"}])
    )
    shadow_workspace = tmp_path / "pcloud-shadow-validation-launchd-automation" / "workspace"
    shadow_report = tmp_path / "shadow-validation-launchd-automation.json"
    shadow_report.write_text(
        json.dumps(
            {
                "status": "ok",
                "workspace": str(shadow_workspace),
                "state_dir": str(shadow_workspace / ".dev-state" / "state"),
                "checks": [
                    {"name": "temporary workspace guard", "status": "ok"},
                    {"name": "temporary state dir guard", "status": "ok"},
                    {"name": "unsafe state dir guard", "status": "ok"},
                ],
            }
        )
    )

    common = [
        "--report-path",
        str(shadow_report),
        "--consume-policy",
        "remove-on-success-retain-on-failure",
        "--timeout-policy",
        "reuse-fake-rclone-cleanup",
        "--operator-reviewed-dry-run",
        "--reviewer-approved-real-command",
        "--reviewer-approved-consume-policy",
        "--operator-reviewed-real-transfer-gate",
        "--reviewer-approved-automation-command",
        "--reviewer-approved-launchd-policy",
        "--reviewer-approved-rollback-policy",
    ]
    pushd = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "launchd",
            "automation-plist",
            *common,
            "--confirm-path",
            "Documents/auto-upload.txt",
            "--confirm-direction",
            "upload",
            "--start-interval-seconds",
            "45",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env
        | {
            "PCLOUD_TOOLS_REAL_TRANSFER_AUTOMATION_GATE": "operator-approved-real-transfer-automation-v1"
        },
    )
    diffd = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "diffd",
            "launchd",
            "automation-reload",
            *common,
            "--confirm-path",
            "Documents/auto-download.txt",
            "--confirm-direction",
            "download",
            "--operator-reviewed-automation-plist",
            "--reviewer-approved-bootout-bootstrap",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    pushd_payload = _payload(pushd)
    diffd_payload = _payload(diffd)

    assert pushd.returncode == 0
    assert diffd.returncode == 0
    assert pushd_payload["summary"] == "pushd launchd automation plist is gated"
    assert pushd_payload["details"]["automation command status"] == "implemented-gated"
    assert pushd_payload["details"]["public executor plist can write"] == "no"
    assert pushd_payload["details"]["public plist writes"] == "no"
    assert pushd_payload["details"]["launchctl execution"] == "no"
    assert pushd_payload["details"]["automatic real transfer execution"] == "no"
    assert pushd_payload["details"]["service label"] == "com.takafumi.pcloud-pushd-executor"
    assert pushd_payload["details"]["start interval seconds"] == 45
    assert pushd_payload["details"]["program arguments"][1:4] == [
        "pushd",
        "transfer",
        "automation-run",
    ]
    assert pushd_payload["details"]["environment variables"]["PCLOUD_TOOLS_REAL_TRANSFER_AUTOMATION_GATE"] == (
        "operator-approved-real-transfer-automation-v1"
    )
    assert pushd_payload["details"]["environment variables"][
        "PCLOUD_TOOLS_TRANSFER_EXEC_TIMEOUT_SECONDS"
    ] == "3600"
    assert pushd_payload["details"]["transfer timeout seconds"] == 3600
    assert "pushd.launchd.automation-plist.preview" in [
        action["id"] for action in pushd_payload["actions"]
    ]
    assert diffd_payload["summary"] == "diffd launchd automation reload is gated"
    assert diffd_payload["details"]["automation command status"] == "implemented-gated"
    assert diffd_payload["details"]["launchd can reload"] == "no"
    assert diffd_payload["details"]["state writes"] == "none"
    assert diffd_payload["details"]["public plist writes"] == "no"
    assert diffd_payload["details"]["launchctl execution"] == "no"
    assert diffd_payload["details"]["planned launchctl commands"][0][1] == "bootout"
    assert diffd_payload["details"]["planned launchctl commands"][1][1] == "bootstrap"
    assert "diffd.launchd.automation-reload.preview" in [
        action["id"] for action in diffd_payload["actions"]
    ]
    assert not (Path(env["HOME"]) / "Library" / "LaunchAgents" / "com.takafumi.pcloud-pushd-executor.plist").exists()


def test_public_automation_plist_and_reload_execute_only_with_fake_launchctl_gate(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    env["PCLOUD_TOOLS_DEV"] = "0"
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    env["PCLOUD_TOOLS_CORE_DIR"] = str(workspace)
    env["PCLOUD_TOOLS_ALLOWLIST_FILE"] = str(workspace / ".pcloud-sync-allowlist")
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env["HOME"] = str(home)
    public_bin = tmp_path / "public-bin"
    public_bin.mkdir()
    manager = public_bin / "pcloud-manager"
    manager.write_text("#!/bin/sh\nexit 0\n")
    manager.chmod(0o755)
    launchctl_log = tmp_path / "launchctl.log"
    launchctl = public_bin / "launchctl"
    launchctl.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$LAUNCHCTL_LOG\"\n"
        "if [ \"$1\" = \"bootout\" ]; then\n"
        "  printf '%s\\n' 'Boot-out failed: 3: No such process' >&2\n"
        "  exit 3\n"
        "fi\n"
    )
    launchctl.chmod(0o755)
    env["PATH"] = f"{public_bin}:{env['PATH']}"
    env["LAUNCHCTL_LOG"] = str(launchctl_log)
    env["PCLOUD_TOOLS_REAL_TRANSFER_AUTOMATION_GATE"] = "operator-approved-real-transfer-automation-v1"
    env["PCLOUD_TOOLS_PUSHD_LAUNCHD_AUTOMATION_PLIST_GATE"] = (
        "operator-approved-pushd-launchd-automation-plist-v1"
    )
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    pushd_dir = state_dir / "pushd"
    pushd_dir.mkdir(parents=True)
    documents_dir = workspace / "Documents"
    documents_dir.mkdir(parents=True)
    (documents_dir / "auto-upload.txt").write_text("auto upload\n")
    (documents_dir / "auto-upload-2.txt").write_text("auto upload 2\n")
    (pushd_dir / "queue.json").write_text(
        json.dumps(
            [
                {"path": "Documents/auto-upload.txt", "action": "upload", "reason": "test"},
                {"path": "Documents/auto-upload-2.txt", "action": "upload", "reason": "test"},
            ]
        )
    )
    shadow_workspace = tmp_path / "pcloud-shadow-validation-public-automation" / "workspace"
    shadow_report = tmp_path / "shadow-validation-public-automation.json"
    shadow_report.write_text(
        json.dumps(
            {
                "status": "ok",
                "workspace": str(shadow_workspace),
                "state_dir": str(shadow_workspace / ".dev-state" / "state"),
                "checks": [
                    {"name": "temporary workspace guard", "status": "ok"},
                    {"name": "temporary state dir guard", "status": "ok"},
                    {"name": "unsafe state dir guard", "status": "ok"},
                ],
            }
        )
    )
    common = [
        "--report-path",
        str(shadow_report),
        "--confirm-path",
        "Documents/auto-upload.txt",
        "--confirm-direction",
        "upload",
        "--consume-policy",
        "remove-on-success-retain-on-failure",
        "--timeout-policy",
        "reuse-fake-rclone-cleanup",
        "--operator-reviewed-dry-run",
        "--reviewer-approved-real-command",
        "--reviewer-approved-consume-policy",
        "--operator-reviewed-real-transfer-gate",
        "--reviewer-approved-automation-command",
        "--reviewer-approved-launchd-policy",
        "--reviewer-approved-rollback-policy",
    ]

    plist_write = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "launchd",
            "automation-plist",
            *common,
            "--operator-reviewed-automation-command",
            "--reviewer-approved-automation-environment",
            "--reviewer-approved-no-bootstrap",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    reload_env = env | {
        "PCLOUD_TOOLS_PUSHD_LAUNCHD_AUTOMATION_RELOAD_GATE": (
            "operator-approved-pushd-launchd-automation-reload-v1"
        )
    }
    reload = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "launchd",
            "automation-reload",
            *common,
            "--operator-reviewed-automation-plist",
            "--reviewer-approved-bootout-bootstrap",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=reload_env,
    )

    plist_payload = _payload(plist_write)
    reload_payload = _payload(reload)
    plist_path = home / "Library" / "LaunchAgents" / "com.takafumi.pcloud-pushd-executor.plist"
    plist = plistlib.loads(plist_path.read_bytes())

    assert plist_write.returncode == 0
    assert plist_payload["summary"] == "pushd launchd automation plist written"
    assert plist_payload["details"]["public plist writes"] == "yes"
    assert plist_payload["details"]["launchctl execution"] == "no"
    assert plist_payload["details"]["automation gate details"]["automation approval status"] == (
        "ready-for-launchd-review"
    )
    assert plist_payload["details"]["automation gate details"]["real transfer approvals source"] == (
        "current selected bounded automation tick"
    )
    assert plist_payload["details"]["automation gate details"]["automation batch limit"] == 10
    assert plist_payload["details"]["automation gate details"]["deferred transfer command count"] == 0
    assert plist["Label"] == "com.takafumi.pcloud-pushd-executor"
    assert plist["EnvironmentVariables"]["PCLOUD_TOOLS_REAL_TRANSFER_AUTOMATION_RUN_GATE"] == (
        "operator-approved-real-transfer-automation-run-v1"
    )
    assert plist["EnvironmentVariables"]["PCLOUD_TOOLS_TRANSFER_EXEC_TIMEOUT_SECONDS"] == "3600"
    assert "--report-path" in plist["ProgramArguments"]
    assert "--max-records" in plist["ProgramArguments"]
    max_records_index = plist["ProgramArguments"].index("--max-records") + 1
    assert plist["ProgramArguments"][max_records_index] == "10"
    assert reload.returncode == 0
    assert reload_payload["summary"] == "pushd launchd automation reload completed"
    assert reload_payload["details"]["launchctl execution"] == "yes"
    assert reload_payload["details"]["launchctl results"][0]["tolerated"] is True
    assert reload_payload["details"]["launchctl results"][1]["returncode"] == 0
    assert "automatic real transfer execution" in reload_payload["details"]
    assert launchctl_log.read_text().splitlines() == [
        f"bootout gui/{os.getuid()}/com.takafumi.pcloud-pushd-executor",
        f"bootstrap gui/{os.getuid()} {plist_path}",
    ]


def test_transfer_real_run_is_guarded_until_final_review_and_gate(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    pushd_dir = state_dir / "pushd"
    pushd_dir.mkdir(parents=True)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "transfer",
            "real-run",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env
        | {
            "PCLOUD_TOOLS_REAL_TRANSFER_EXECUTION_GATE": "anything",
            "PCLOUD_TOOLS_TRANSFER_EXECUTION_GATE": "dev-fake-rclone",
        },
    )
    action_result = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "action", "pushd.transfer.real-run.preview"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)

    assert result.returncode == 1
    assert action_result.returncode == 0
    assert payload["command"] == "pushd transfer real-run"
    assert payload["status"] == "error"
    assert payload["summary"] == "pushd real transfer execution refused"
    assert payload["details"]["implementation status"] == (
        "guarded real rclone transfer execution path; blocked by gate checks"
    )
    assert payload["details"]["real transfer gate status"] == "closed"
    assert payload["details"]["real transfer execution gate status"].startswith(
        "closed: requires PCLOUD_TOOLS_REAL_TRANSFER_EXECUTION_GATE="
    )
    assert payload["details"]["real execution readiness"] == "blocked-gate"
    assert payload["details"]["real execution can run"] == "no"
    assert payload["details"]["execute requested"] == "yes"
    assert payload["details"]["real gate env provided"] == "yes"
    assert payload["details"]["real gate env honored"] == "no"
    assert payload["details"]["fake-rclone gate reuse"] == "forbidden"
    assert payload["details"]["fake-rclone gate env provided"] == "yes"
    assert payload["details"]["fake-rclone gate env honored"] == "no"
    assert payload["details"]["state writes"] == "none"
    assert payload["details"]["safe alternative command"][1:4] == ["pushd", "transfer", "real-gate"]
    assert "PCLOUD_TOOLS_REAL_TRANSFER_EXECUTION_GATE" in [issue["key"] for issue in payload["issues"]]
    assert "pushd real transfer execution is gated" in action_result.stdout
    assert "safe alternative:" in action_result.stdout
    assert "real execution can run: no" in action_result.stdout
    assert "real gate env provided: no" in action_result.stdout
    assert "fake-rclone gate env honored: no" in action_result.stdout
    assert not (pushd_dir / "last-transfer.json").exists()


def test_transfer_real_run_executes_only_after_explicit_real_gate_with_stub_rclone(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    real_log = _install_real_rclone_stub(env)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    pushd_dir = state_dir / "pushd"
    pushd_dir.mkdir(parents=True)
    _write_workspace_file(env, "Documents/real-run.txt", "upload\n")
    (pushd_dir / "queue.json").write_text(
        json.dumps([{"path": "Documents/real-run.txt", "action": "upload", "reason": "test"}])
    )
    shadow_workspace = tmp_path / "pcloud-shadow-validation-real-run" / "workspace"
    shadow_report = tmp_path / "shadow-validation-real-run.json"
    shadow_report.write_text(
        json.dumps(
            {
                "status": "ok",
                "workspace": str(shadow_workspace),
                "state_dir": str(shadow_workspace / ".dev-state" / "state"),
                "checks": [
                    {"name": "temporary workspace guard", "status": "ok"},
                    {"name": "temporary state dir guard", "status": "ok"},
                    {"name": "unsafe state dir guard", "status": "ok"},
                ],
            }
        )
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "transfer",
            "real-run",
            "--report-path",
            str(shadow_report),
            "--confirm-path",
            "Documents/real-run.txt",
            "--confirm-direction",
            "upload",
            "--consume-policy",
            "remove-on-success-retain-on-failure",
            "--timeout-policy",
            "reuse-fake-rclone-cleanup",
            "--operator-reviewed-dry-run",
            "--reviewer-approved-real-command",
            "--reviewer-approved-consume-policy",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env | {"PCLOUD_TOOLS_REAL_TRANSFER_EXECUTION_GATE": "operator-approved-real-transfer-v1"},
    )

    payload = _payload(result)
    transfer_state = json.loads((pushd_dir / "last-transfer.json").read_text())

    assert result.returncode == 0
    assert payload["summary"] == "pushd real transfer executed"
    assert payload["details"]["real transfer execution gate status"] == (
        "open: operator-approved-real-transfer-v1"
    )
    assert payload["details"]["real execution readiness"] == "executed"
    assert payload["details"]["real execution can run"] == "yes"
    assert payload["details"]["real gate env honored"] == "yes"
    assert payload["details"]["fake-rclone gate env honored"] == "no"
    assert "/pushd/last-transfer.json" in payload["details"]["state writes"]
    assert "/pushd/queue.json" in payload["details"]["state writes"]
    assert payload["details"]["automatic queue/change consumption"] == "yes"
    assert payload["details"]["records consumed"] == 1
    assert real_log.read_text().strip().startswith("copyto ")
    assert "Documents/real-run.txt pcloud:core/Documents/real-run.txt" in real_log.read_text()
    assert transfer_state["mode"] == "real-rclone-transfer"
    assert transfer_state["results"][0]["returncode"] == 0
    assert json.loads((pushd_dir / "queue.json").read_text()) == []


def test_transfer_real_run_can_select_confirmed_target_from_multiple_planned_records(
    tmp_path: Path,
) -> None:
    env = _base_env(tmp_path)
    real_log = _install_real_rclone_stub(env)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    pushd_dir = state_dir / "pushd"
    pushd_dir.mkdir(parents=True)
    _write_workspace_file(env, "Documents/selected-real-run.txt", "selected\n")
    _write_workspace_file(env, "Documents/retained-real-run.txt", "retained\n")
    (pushd_dir / "queue.json").write_text(
        json.dumps(
            [
                {"path": "Documents/selected-real-run.txt", "action": "upload", "reason": "test"},
                {"path": "Documents/retained-real-run.txt", "action": "upload", "reason": "test"},
            ]
        )
    )
    shadow_workspace = tmp_path / "pcloud-shadow-validation-real-run-select" / "workspace"
    shadow_report = tmp_path / "shadow-validation-real-run-select.json"
    shadow_report.write_text(
        json.dumps(
            {
                "status": "ok",
                "workspace": str(shadow_workspace),
                "state_dir": str(shadow_workspace / ".dev-state" / "state"),
                "checks": [
                    {"name": "temporary workspace guard", "status": "ok"},
                    {"name": "temporary state dir guard", "status": "ok"},
                    {"name": "unsafe state dir guard", "status": "ok"},
                ],
            }
        )
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "transfer",
            "real-run",
            "--report-path",
            str(shadow_report),
            "--confirm-path",
            "Documents/selected-real-run.txt",
            "--confirm-direction",
            "upload",
            "--consume-policy",
            "remove-on-success-retain-on-failure",
            "--timeout-policy",
            "reuse-fake-rclone-cleanup",
            "--operator-reviewed-dry-run",
            "--reviewer-approved-real-command",
            "--reviewer-approved-consume-policy",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env | {"PCLOUD_TOOLS_REAL_TRANSFER_EXECUTION_GATE": "operator-approved-real-transfer-v1"},
    )

    payload = _payload(result)
    transfer_state = json.loads((pushd_dir / "last-transfer.json").read_text())
    real_log_text = real_log.read_text()

    assert result.returncode == 0
    assert payload["summary"] == "pushd real transfer executed"
    assert payload["details"]["final review status"] == "ready"
    assert payload["details"]["selected transfer"]["path"] == "Documents/selected-real-run.txt"
    assert len(payload["details"]["all planned transfer commands"]) == 2
    assert len(payload["details"]["planned transfer commands"]) == 1
    assert "Documents/selected-real-run.txt pcloud:core/Documents/selected-real-run.txt" in real_log_text
    assert "Documents/retained-real-run.txt" not in real_log_text
    assert transfer_state["planned_transfer_commands"][0]["path"] == "Documents/selected-real-run.txt"
    assert payload["details"]["automatic queue/change consumption"] == "yes"
    assert payload["details"]["records consumed"] == 1
    assert json.loads((pushd_dir / "queue.json").read_text()) == [
        {"path": "Documents/retained-real-run.txt", "action": "upload", "reason": "test"}
    ]


def test_transfer_check_warns_on_mismatched_operator_confirmation(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    pushd_dir = state_dir / "pushd"
    pushd_dir.mkdir(parents=True)
    (pushd_dir / "queue.json").write_text(
        json.dumps([{"path": "Documents/planned.txt", "action": "upload", "reason": "test"}])
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "transfer",
            "check",
            "--confirm-path",
            "Documents/other.txt",
            "--confirm-direction",
            "upload",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    checks = {check["name"]: check for check in payload["details"]["preflight checks"]}

    assert result.returncode == 0
    assert payload["details"]["real transfer gate status"] == "closed"
    assert payload["details"]["operator target confirmation status"] == "not-ok"
    assert checks["first real run target"]["status"] == "not-ok"
    assert "does not match any planned transfer" in checks["first real run target"]["detail"]
    assert "PCLOUD_TOOLS_PUSHD_REAL_TRANSFER_TARGET_CONFIRMATION" in [
        issue["key"] for issue in payload["issues"]
    ]


def test_transfer_check_custom_sample_path_must_be_allowlisted(tmp_path: Path) -> None:
    env = _base_env(tmp_path)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "transfer",
            "check",
            "--sample-path",
            "../dev/not-allowed.py",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)

    assert result.returncode == 0
    assert payload["status"] == "warning"
    assert payload["details"]["sample path"] == ""
    assert payload["details"]["sample path status"] == "not planned"
    assert payload["details"]["expected after sample setup"]["planned uploads"] == 0
    assert "PCLOUD_TOOLS_REAL_TRANSFER_SAMPLE_PATH" in [issue["key"] for issue in payload["issues"]]
    assert "PCLOUD_TOOLS_REAL_TRANSFER_GATE" in [issue["key"] for issue in payload["issues"]]


def test_transfer_check_human_output_is_concise_but_json_stays_detailed(tmp_path: Path) -> None:
    env = _base_env(tmp_path)

    human = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "transfer",
            "check",
            "--sample-path",
            "Documents/custom-sample.txt",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    structured = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "transfer",
            "check",
            "--sample-path",
            "Documents/custom-sample.txt",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(structured)

    assert human.returncode == 0
    assert "pushd transfer check: warning" in human.stdout
    assert "gate: closed" in human.stdout
    assert "state writes: none" in human.stdout
    assert "sample: Documents/custom-sample.txt (ready)" in human.stdout
    assert "first target: missing" in human.stdout
    assert "shadow report: pending" in human.stdout
    assert "review commands:" in human.stdout
    assert "- setup sample:" in human.stdout
    assert "- preview transfer:" in human.stdout
    assert "- check again:" in human.stdout
    assert "pushd transfer check --sample-path Documents/custom-sample.txt --json" in human.stdout
    assert "- cleanup sample:" in human.stdout
    assert "preflight checks:" not in human.stdout
    assert "planned transfer commands:" not in human.stdout
    assert "review command sequence:" not in human.stdout
    assert "core dir:" not in human.stdout

    assert structured.returncode == 0
    assert payload["details"]["preflight checks"][0]["status"] == "pending"
    assert len(payload["details"]["review command sequence"]) == 4
    assert payload["details"]["review command sequence"][2][4:6] == [
        "--sample-path",
        "Documents/custom-sample.txt",
    ]
    assert payload["details"]["planned transfer commands"] == []


def test_transfer_preview_and_check_action_ids_dispatch(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    pushd = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "action", "pushd.transfer.preview"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    diffd = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "action", "diffd.transfer.preview"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    pushd_check = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "action", "pushd.transfer.check"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    pushd_launchd_gate = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "action", "pushd.launchd.gate"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    diffd_launchd_gate = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "action", "diffd.launchd.gate"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    pushd_matrix = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "action", "pushd.transfer.validation-matrix"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    diffd_matrix = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "action", "diffd.transfer.validation-matrix"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    pushd_fswatch_gate = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "action", "pushd.fswatch.resident-gate"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    diffd_api_poll_gate = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "action", "diffd.api-poll.long-poll-gate"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    pushd_real_gate = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "action", "pushd.transfer.real-gate"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    diffd_real_gate = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "action", "diffd.transfer.real-gate"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    pushd_consume = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "action", "pushd.transfer.consume.preview"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    diffd_consume = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "action", "diffd.transfer.consume.preview"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    diffd_check = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "action", "diffd.transfer.check"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    assert pushd.returncode == 0
    assert "pushd upload transfer preview is ready" in pushd.stdout
    assert "real execution can run: no" in pushd.stdout
    assert diffd.returncode == 0
    assert "diffd download transfer preview is ready" in diffd.stdout
    assert pushd_check.returncode == 0
    assert "pushd real transfer gate checklist is not open" in pushd_check.stdout
    assert pushd_launchd_gate.returncode == 0
    assert "pushd launchd gate is closed" in pushd_launchd_gate.stdout
    assert diffd_launchd_gate.returncode == 0
    assert "diffd launchd gate is closed" in diffd_launchd_gate.stdout
    assert pushd_matrix.returncode == 0
    assert "pushd real transfer validation matrix is ready" in pushd_matrix.stdout
    assert diffd_matrix.returncode == 0
    assert "diffd real transfer validation matrix is ready" in diffd_matrix.stdout
    assert pushd_fswatch_gate.returncode == 0
    assert "pushd fswatch resident gate is closed" in pushd_fswatch_gate.stdout
    assert diffd_api_poll_gate.returncode == 0
    assert "diffd pCloud API long-poll gate is closed" in diffd_api_poll_gate.stdout
    assert diffd_check.returncode == 0
    assert "diffd real transfer gate checklist is not open" in diffd_check.stdout
    assert pushd_real_gate.returncode == 0
    assert "pushd real transfer execution gate is closed" in pushd_real_gate.stdout
    assert diffd_real_gate.returncode == 0
    assert "diffd real transfer execution gate is closed" in diffd_real_gate.stdout
    assert pushd_consume.returncode == 0
    assert "pushd transfer consume policy preview is ready" in pushd_consume.stdout
    assert diffd_consume.returncode == 0
    assert "diffd transfer consume policy preview is ready" in diffd_consume.stdout


def test_transfer_run_without_execute_is_preview_only(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = _use_default_dev_state_dir(env)
    fake_log = _install_fake_rclone(env)
    pushd_dir = state_dir / "pushd"
    pushd_dir.mkdir(parents=True)
    (pushd_dir / "queue.json").write_text(
        json.dumps([{"path": "Documents/upload.pdf", "action": "upload", "reason": "test"}])
    )

    result = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "transfer", "run", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    assert result.returncode == 0
    assert payload["summary"] == "pushd upload transfer run preview is ready"
    assert payload["details"]["implementation status"] == "transfer run preview only; rclone is not executed"
    assert payload["details"]["real execution can run"] == "no"
    assert payload["details"]["real execution readiness"] == "blocked-preview"
    assert payload["details"]["state writes"] == "none"
    assert not fake_log.exists()
    assert not (pushd_dir / "last-transfer.json").exists()


def test_transfer_run_requires_fake_rclone_gate(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = _use_default_dev_state_dir(env)
    fake_log = _install_fake_rclone(env)
    del env["PCLOUD_TOOLS_TRANSFER_EXECUTION_GATE"]
    pushd_dir = state_dir / "pushd"
    pushd_dir.mkdir(parents=True)
    (pushd_dir / "queue.json").write_text(
        json.dumps([{"path": "Documents/upload.pdf", "action": "upload", "reason": "test"}])
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "transfer",
            "run",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    assert result.returncode == 1
    assert payload["status"] == "error"
    assert payload["summary"] == "pushd transfer execution refused"
    assert "PCLOUD_TOOLS_TRANSFER_EXECUTION_GATE" in [issue["key"] for issue in payload["issues"]]
    assert not fake_log.exists()
    assert not (pushd_dir / "last-transfer.json").exists()


def test_transfer_run_uses_fake_rclone_only_in_dev_state(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = _use_default_dev_state_dir(env)
    fake_log = _install_fake_rclone(env)
    pushd_dir = state_dir / "pushd"
    diffd_dir = state_dir / "diffd"
    pushd_dir.mkdir(parents=True)
    diffd_dir.mkdir(parents=True)
    _write_workspace_file(env, "Documents/upload.pdf", "upload\n")
    (pushd_dir / "queue.json").write_text(
        json.dumps([{"path": "Documents/upload.pdf", "action": "upload", "reason": "test"}])
    )
    (diffd_dir / "remote-changes.json").write_text(
        json.dumps([{"path": "Documents/download.pdf", "action": "download", "reason": "test"}])
    )

    pushd = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "transfer",
            "run",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    diffd = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "diffd",
            "transfer",
            "run",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    pushd_payload = _payload(pushd)
    diffd_payload = _payload(diffd)
    assert pushd.returncode == 0
    assert diffd.returncode == 0
    assert pushd_payload["summary"] == "pushd upload transfer executed with fake-rclone"
    assert diffd_payload["summary"] == "diffd download transfer executed with fake-rclone"
    assert pushd_payload["details"]["execution gate"] == "open: dev-fake-rclone"
    assert diffd_payload["details"]["execution gate"] == "open: dev-fake-rclone"
    assert pushd_payload["details"]["real transfer gate status"] == "closed"
    assert diffd_payload["details"]["real transfer gate status"] == "closed"
    fake_calls = fake_log.read_text().splitlines()
    assert len(fake_calls) == 2
    assert fake_calls[0].startswith("copyto ")
    assert "Documents/upload.pdf pcloud:core/Documents/upload.pdf" in fake_calls[0]
    assert fake_calls[1].startswith("copyto ")
    assert "pcloud:core/Documents/download.pdf " in fake_calls[1]
    assert (pushd_dir / "last-transfer.json").exists()
    assert (diffd_dir / "last-transfer.json").exists()
    assert json.loads((pushd_dir / "queue.json").read_text())[0]["path"] == "Documents/upload.pdf"
    assert json.loads((diffd_dir / "remote-changes.json").read_text())[0]["path"] == "Documents/download.pdf"


def test_transfer_executor_run_executes_and_consumes_dev_state_only(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = _use_default_dev_state_dir(env)
    fake_log = _install_fake_rclone(env)
    pushd_dir = state_dir / "pushd"
    diffd_dir = state_dir / "diffd"
    pushd_dir.mkdir(parents=True)
    diffd_dir.mkdir(parents=True)
    _write_workspace_file(env, "Documents/upload.pdf", "upload\n")
    (pushd_dir / "queue.json").write_text(
        json.dumps([{"path": "Documents/upload.pdf", "action": "upload", "reason": "test"}])
    )
    (diffd_dir / "remote-changes.json").write_text(
        json.dumps([{"path": "Documents/download.pdf", "action": "download", "reason": "test"}])
    )

    pushd = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "transfer",
            "executor-run",
            "--execute",
            "--consume-on-success",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    diffd = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "diffd",
            "transfer",
            "executor-run",
            "--execute",
            "--consume-on-success",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    pushd_payload = _payload(pushd)
    diffd_payload = _payload(diffd)
    fake_calls = fake_log.read_text().splitlines()

    assert pushd.returncode == 0
    assert diffd.returncode == 0
    assert pushd_payload["summary"] == "pushd transfer executor tick completed"
    assert diffd_payload["summary"] == "diffd transfer executor tick completed"
    assert pushd_payload["details"]["consume on success requested"] == "yes"
    assert diffd_payload["details"]["consume on success requested"] == "yes"
    assert pushd_payload["details"]["records consumed"] == 1
    assert diffd_payload["details"]["records consumed"] == 1
    assert pushd_payload["details"]["real transfer automation gate status"] == "closed"
    assert diffd_payload["details"]["real transfer automation gate status"] == "closed"
    assert len(fake_calls) == 2
    assert json.loads((pushd_dir / "queue.json").read_text()) == []
    assert json.loads((diffd_dir / "remote-changes.json").read_text()) == []


def test_pushd_plan_suppresses_fresh_completed_download_until_local_file_changes(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = _use_default_dev_state_dir(env)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    target = workspace / "Documents" / "downloaded.txt"
    target.parent.mkdir(parents=True)
    target.write_text("downloaded\n")
    pushd_dir = state_dir / "pushd"
    diffd_dir = state_dir / "diffd"
    pushd_dir.mkdir(parents=True)
    diffd_dir.mkdir(parents=True)
    (pushd_dir / "queue.json").write_text(
        json.dumps([{"path": "Documents/downloaded.txt", "action": "upload", "reason": "fswatch"}])
    )
    fingerprint = local_fingerprint(target).as_dict()
    (diffd_dir / "download-suppression-journal.json").write_text(
        json.dumps(
            {
                "schema_version": "pcloud-tools-download-suppression.v1",
                "records": [
                    {
                        "path": "Documents/downloaded.txt",
                        "state": "completed",
                        "direction": "download",
                        "started_at": "2026-05-07T00:00:00+00:00",
                        "completed_at": "2026-05-07T00:00:01+00:00",
                        "local_fingerprint": fingerprint,
                    }
                ],
            }
        )
    )

    suppressed = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "preview", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    target.write_text("user edit\n")
    allowed = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "preview", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    suppressed_payload = _payload(suppressed)
    allowed_payload = _payload(allowed)
    assert suppressed.returncode == 0
    assert suppressed_payload["details"]["planned uploads"] == 0
    assert suppressed_payload["details"]["excluded queue records"][0]["reason"] == "download suppression journal"
    assert allowed.returncode == 0
    assert allowed_payload["details"]["planned uploads"] == 1


def test_diffd_plan_suppresses_fresh_completed_upload_until_local_file_changes(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = _use_default_dev_state_dir(env)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    target = workspace / "Documents" / "uploaded.txt"
    target.parent.mkdir(parents=True)
    target.write_text("uploaded\n")
    pushd_dir = state_dir / "pushd"
    diffd_dir = state_dir / "diffd"
    pushd_dir.mkdir(parents=True)
    diffd_dir.mkdir(parents=True)
    (diffd_dir / "remote-changes.json").write_text(
        json.dumps([{"path": "Documents/uploaded.txt", "action": "download", "reason": "diff:createfile"}])
    )
    fingerprint = local_fingerprint(target).as_dict()
    (pushd_dir / "upload-origin-journal.json").write_text(
        json.dumps(
            {
                "schema_version": "pcloud-tools-upload-origin-suppression.v1",
                "records": [
                    {
                        "path": "Documents/uploaded.txt",
                        "state": "completed",
                        "direction": "upload",
                        "started_at": "2026-05-07T00:00:00+00:00",
                        "completed_at": "2026-05-07T00:00:01+00:00",
                        "local_fingerprint": fingerprint,
                    }
                ],
            }
        )
    )

    suppressed = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "diffd", "preview", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    target.write_text("user edit after upload\n")
    allowed = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "diffd", "preview", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    suppressed_payload = _payload(suppressed)
    allowed_payload = _payload(allowed)
    assert suppressed.returncode == 0
    assert suppressed_payload["details"]["planned downloads"] == 0
    assert suppressed_payload["details"]["skipped download record details"][0]["reason"] == "upload origin journal"
    assert allowed.returncode == 0
    assert allowed_payload["details"]["planned downloads"] == 1
    (diffd_dir / "remote-changes.json").write_text(
        json.dumps([{"path": "Documents/uploaded.txt", "action": "download", "reason": "diff:modifyfile"}])
    )
    remote_edit = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "diffd", "preview", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    remote_edit_payload = _payload(remote_edit)
    assert remote_edit.returncode == 0
    assert remote_edit_payload["details"]["planned downloads"] == 1
    assert remote_edit_payload["details"]["planned download records"][0]["reason"] == "diff:modifyfile"


def test_pushd_upload_success_records_upload_origin_journal(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    target = workspace / "Documents" / "uploaded.txt"
    target.parent.mkdir(parents=True)
    target.write_text("uploaded content\n")
    _install_real_rclone_stub(env)
    pushd_dir = state_dir / "pushd"
    pushd_dir.mkdir(parents=True)
    (pushd_dir / "queue.json").write_text(
        json.dumps([{"path": "Documents/uploaded.txt", "action": "upload", "reason": "test"}])
    )
    shadow_workspace = tmp_path / "pcloud-shadow-validation-upload-origin" / "workspace"
    shadow_report = tmp_path / "shadow-validation-upload-origin.json"
    shadow_report.write_text(
        json.dumps(
            {
                "status": "ok",
                "workspace": str(shadow_workspace),
                "state_dir": str(shadow_workspace / ".dev-state" / "state"),
                "checks": [
                    {"name": "temporary workspace guard", "status": "ok"},
                    {"name": "temporary state dir guard", "status": "ok"},
                    {"name": "unsafe state dir guard", "status": "ok"},
                ],
            }
        )
    )
    execute_env = env | {
        "PCLOUD_TOOLS_REAL_TRANSFER_EXECUTION_GATE": "operator-approved-real-transfer-v1",
        "PCLOUD_TOOLS_REAL_TRANSFER_AUTOMATION_GATE": "operator-approved-real-transfer-automation-v1",
        "PCLOUD_TOOLS_REAL_TRANSFER_AUTOMATION_RUN_GATE": "operator-approved-real-transfer-automation-run-v1",
    }

    upload = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "transfer",
            "automation-run",
            "--report-path",
            str(shadow_report),
            "--execute",
            "--consume-on-success",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=execute_env,
    )

    upload_payload = _payload(upload)
    transfer_state = json.loads((pushd_dir / "last-transfer.json").read_text())
    result_record = transfer_state["results"][0]
    journal = json.loads((pushd_dir / "upload-origin-journal.json").read_text())
    assert upload.returncode == 0
    assert upload_payload["details"]["records consumed"] == 1
    assert result_record["upload origin journal state"] == "completed"
    assert journal["records"][0]["path"] == "Documents/uploaded.txt"
    assert journal["records"][0]["direction"] == "upload"
    assert journal["records"][0]["state"] == "completed"


def test_diffd_download_conflict_creates_copy_and_retains_remote_change(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = _use_default_dev_state_dir(env)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    target = workspace / "Documents" / "conflict.txt"
    target.parent.mkdir(parents=True)
    target.write_text("before\n")
    fake_log = _install_fake_rclone(env)
    fake_rclone = Path(env["PCLOUD_TOOLS_RCLONE_BIN"])
    fake_rclone.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$FAKE_RCLONE_LOG\"\n"
        "if [ \"$1\" = \"copyto\" ]; then\n"
        f"  printf 'local edit during download\\n' > {shlex.quote(str(target))}\n"
        "  dest=\"$3\"\n"
        "  mkdir -p \"$(dirname \"$dest\")\"\n"
        "  printf 'downloaded remote content\\n' > \"$dest\"\n"
        "fi\n"
    )
    fake_rclone.chmod(0o755)
    diffd_dir = state_dir / "diffd"
    diffd_dir.mkdir(parents=True)
    remote_records = [{"path": "Documents/conflict.txt", "action": "download", "reason": "test"}]
    (diffd_dir / "remote-changes.json").write_text(json.dumps(remote_records))

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "diffd",
            "transfer",
            "executor-run",
            "--execute",
            "--consume-on-success",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    transfer_state = json.loads((diffd_dir / "last-transfer.json").read_text())
    result_record = transfer_state["results"][0]
    conflict_path = Path(result_record["download conflict path"])

    assert result.returncode == 0
    assert payload["status"] == "warning"
    assert payload["details"]["records consumed"] == 0
    assert result_record["conflict"] is True
    assert result_record["manual_review"] is True
    assert target.read_text() == "local edit during download\n"
    assert conflict_path.exists()
    assert conflict_path.read_text() == "downloaded remote content\n"
    assert json.loads((diffd_dir / "remote-changes.json").read_text()) == remote_records
    journal = json.loads((diffd_dir / "download-suppression-journal.json").read_text())
    assert journal["records"][0]["state"] == "conflict"
    assert "copyto pcloud:core/Documents/conflict.txt" in fake_log.read_text()


def test_transfer_executor_refuses_manual_review_records_before_fake_rclone(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = _use_default_dev_state_dir(env)
    fake_log = _install_fake_rclone(env)
    pushd_dir = state_dir / "pushd"
    pushd_dir.mkdir(parents=True)
    (pushd_dir / "queue.json").write_text(
        json.dumps([{"path": "Documents/delete.pdf", "action": "delete", "reason": "test"}])
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "transfer",
            "executor-run",
            "--execute",
            "--consume-on-success",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)

    assert result.returncode == 1
    assert payload["summary"] == "pushd transfer executor tick refused"
    assert payload["details"]["manual review transfer records"] == 1
    assert "PCLOUD_TOOLS_PUSHD_EXECUTOR_MANUAL_REVIEW" in [
        issue["key"] for issue in payload["issues"]
    ]
    assert not fake_log.exists()
    assert json.loads((pushd_dir / "queue.json").read_text())[0]["path"] == "Documents/delete.pdf"


def test_launchd_executor_plist_is_dev_state_fake_rclone_only(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    _use_default_dev_state_dir(env)
    _install_fake_rclone(env)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    dev_entrypoint = workspace / "pcloud-manager-dev"
    dev_entrypoint.write_text("#!/bin/sh\nprintf 'fake dev pcloud-manager\\n'\n")
    dev_entrypoint.chmod(0o755)

    preview = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "launchd",
            "executor-plist",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    opened = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "launchd",
            "executor-plist",
            "--start-interval-seconds",
            "30",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    diffd_opened = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "diffd",
            "launchd",
            "executor-plist",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    preview_payload = _payload(preview)
    opened_payload = _payload(opened)
    diffd_payload = _payload(diffd_opened)
    pushd_plist_path = workspace / ".dev-state" / "launchd" / "com.example.pcloud-pushd-executor.dev.plist"
    diffd_plist_path = workspace / ".dev-state" / "launchd" / "com.example.pcloud-diffd-executor.dev.plist"
    pushd_plist = plistlib.loads(pushd_plist_path.read_bytes())
    diffd_plist = plistlib.loads(diffd_plist_path.read_bytes())

    assert preview.returncode == 0
    assert preview_payload["summary"] == "pushd launchd executor plist preview is ready"
    assert preview_payload["details"]["state writes"] == "none"
    assert preview_payload["details"]["start interval seconds"] == 60
    assert "pushd.launchd.executor-plist.preview" in [
        action["id"] for action in preview_payload["actions"]
    ]
    assert opened.returncode == 0
    assert opened_payload["summary"] == "pushd launchd executor plist written"
    assert opened_payload["details"]["state writes"] == "launchd executor plist only"
    assert opened_payload["details"]["launchctl execution"] == "no"
    assert opened_payload["details"]["public launchd changes"] == "no"
    assert opened_payload["details"]["real transfer automation gate status"] == "closed"
    assert pushd_plist["Label"] == "com.example.pcloud-pushd-executor.dev"
    assert pushd_plist["StartInterval"] == 30
    assert pushd_plist["RunAtLoad"] is False
    assert pushd_plist["KeepAlive"] is False
    assert pushd_plist["ProgramArguments"][:4] == [
        str(dev_entrypoint),
        "pushd",
        "transfer",
        "executor-run",
    ]
    assert "--execute" in pushd_plist["ProgramArguments"]
    assert "--consume-on-success" in pushd_plist["ProgramArguments"]
    assert pushd_plist["EnvironmentVariables"]["PCLOUD_TOOLS_DEV"] == "1"
    assert pushd_plist["EnvironmentVariables"]["PCLOUD_TOOLS_TRANSFER_EXECUTION_GATE"] == "dev-fake-rclone"
    assert pushd_plist["EnvironmentVariables"]["PCLOUD_TOOLS_RCLONE_BIN"].endswith(
        "/.dev-state/bin/fake-rclone"
    )
    assert diffd_opened.returncode == 0
    assert diffd_payload["summary"] == "diffd launchd executor plist written"
    assert diffd_plist["ProgramArguments"][:4] == [
        str(dev_entrypoint),
        "diffd",
        "transfer",
        "executor-run",
    ]
    assert diffd_plist["StartInterval"] == 60


def test_transfer_consume_preview_reports_successful_records_without_writes(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = _use_default_dev_state_dir(env)
    _install_fake_rclone(env)
    pushd_dir = state_dir / "pushd"
    pushd_dir.mkdir(parents=True)
    _write_workspace_file(env, "Documents/upload.pdf", "upload\n")
    queue_payload = [{"path": "Documents/upload.pdf", "action": "upload", "reason": "test"}]
    (pushd_dir / "queue.json").write_text(json.dumps(queue_payload))

    run = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "transfer",
            "run",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    preview = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "transfer", "consume", "preview"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    structured = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "transfer",
            "consume",
            "preview",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(structured)

    assert run.returncode == 0
    assert preview.returncode == 0
    assert "pushd transfer consume preview:" in preview.stdout
    assert "consume gate: preview-only" in preview.stdout
    assert "real execution can run: no" in preview.stdout
    assert "state writes: none" in preview.stdout
    assert "successful transfers: 1" in preview.stdout
    assert "planned record removals: 1" in preview.stdout
    assert "first removal: Documents/upload.pdf (upload)" in preview.stdout
    assert structured.returncode == 0
    assert payload["details"]["implementation status"].startswith("read-only consume preview")
    assert payload["details"]["real execution can run"] == "no"
    assert payload["details"]["real execution readiness"] == "not-transfer-execution"
    assert payload["details"]["state writes"] == "none"
    assert payload["details"]["successful transfer results"] == 1
    assert payload["details"]["planned record removals"] == 1
    assert payload["details"]["planned removal record details"][0]["path"] == "Documents/upload.pdf"
    assert json.loads((pushd_dir / "queue.json").read_text()) == queue_payload


def test_transfer_consume_run_execute_removes_only_successful_matched_records(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = _use_default_dev_state_dir(env)
    _install_fake_rclone(env)
    pushd_dir = state_dir / "pushd"
    pushd_dir.mkdir(parents=True)
    _write_workspace_file(env, "Documents/upload.pdf", "upload\n")
    _write_workspace_file(env, "Documents/keep.pdf", "keep\n")
    initial_queue = [{"path": "Documents/upload.pdf", "action": "upload", "reason": "test"}]
    expanded_queue = [
        *initial_queue,
        {"path": "Documents/keep.pdf", "action": "upload", "reason": "not-transferred"},
    ]
    (pushd_dir / "queue.json").write_text(json.dumps(initial_queue))

    run = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "transfer",
            "run",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    (pushd_dir / "queue.json").write_text(json.dumps(expanded_queue))
    consume_preview = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "transfer", "consume", "run"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    consume_execute = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "transfer",
            "consume",
            "run",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(consume_execute)
    remaining = json.loads((pushd_dir / "queue.json").read_text())

    assert run.returncode == 0
    assert consume_preview.returncode == 0
    assert "consume gate: closed: preview-only" in consume_preview.stdout
    assert consume_execute.returncode == 0
    assert payload["summary"] == "pushd transfer consumed records"
    assert payload["details"]["consume gate status"] == "open: dev-state"
    assert payload["details"]["real execution can run"] == "no"
    assert payload["details"]["real execution readiness"] == "not-transfer-execution"
    assert payload["details"]["records to remove"] == 1
    assert payload["details"]["records before"] == 2
    assert payload["details"]["records after"] == 1
    assert payload["details"]["state writes"].endswith("/pushd/queue.json")
    assert remaining == [{"path": "Documents/keep.pdf", "action": "upload", "reason": "not-transferred"}]


def test_transfer_run_refuses_unsafe_state_dir_before_fake_rclone(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    unsafe_state_dir = tmp_path / "fake-live"
    unsafe_state_dir.mkdir()
    env["PCLOUD_TOOLS_STATE_DIR"] = str(unsafe_state_dir)
    fake_log = _install_fake_rclone(env)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "transfer",
            "run",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    assert result.returncode == 1
    assert payload["status"] == "error"
    assert "PCLOUD_TOOLS_DEV_STATE_DIR" in [issue["key"] for issue in payload["issues"]]
    assert not fake_log.exists()
    assert not (unsafe_state_dir / "pushd" / "last-transfer.json").exists()


def test_transfer_run_times_out_fake_rclone_without_consuming_queue(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = _use_default_dev_state_dir(env)
    fake_log = _install_fake_rclone(env)
    env["PCLOUD_TOOLS_TRANSFER_EXEC_TIMEOUT_SECONDS"] = "1"
    fake_rclone = Path(env["PCLOUD_TOOLS_RCLONE_BIN"])
    child_marker = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"]) / ".dev-state" / "fake-rclone-child-survived"
    fake_rclone.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$FAKE_RCLONE_LOG\"\n"
        f"(sleep 2; printf survived > {shlex.quote(str(child_marker))}) &\n"
        "sleep 5\n"
    )
    fake_rclone.chmod(0o755)
    pushd_dir = state_dir / "pushd"
    pushd_dir.mkdir(parents=True)
    _write_workspace_file(env, "Documents/slow-upload.pdf", "slow\n")
    (pushd_dir / "queue.json").write_text(
        json.dumps([{"path": "Documents/slow-upload.pdf", "action": "upload", "reason": "slow"}])
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "transfer",
            "run",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    assert result.returncode == 1
    assert payload["status"] == "error"
    assert payload["summary"] == "pushd transfer execution failed"
    assert "PCLOUD_TOOLS_TRANSFER_EXEC_TIMEOUT" in [issue["key"] for issue in payload["issues"]]
    assert payload["details"]["transfer timeout seconds"] == 1
    assert payload["details"]["state writes"].endswith("/pushd/last-transfer.json")
    assert fake_log.exists()
    assert json.loads((pushd_dir / "queue.json").read_text())[0]["path"] == "Documents/slow-upload.pdf"
    transfer_state = json.loads((pushd_dir / "last-transfer.json").read_text())
    assert transfer_state["results"][0]["timed_out"] is True
    assert transfer_state["results"][0]["timeout seconds"] == 1
    assert transfer_state["results"][0]["cleanup"]["terminate attempted"] is True
    assert transfer_state["results"][0]["cleanup"]["terminated"] is True
    time.sleep(2.5)
    assert not child_marker.exists()


def test_automation_run_dedupes_repeated_abnormal_chat_notifications(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = _use_default_dev_state_dir(env)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    notify_log = workspace / ".dev-state" / "notify.log"
    notify_cmd = workspace / ".dev-state" / "notify"
    notify_cmd.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> {shlex.quote(str(notify_log))}\n"
    )
    notify_cmd.chmod(0o755)
    real_log = _install_real_rclone_stub(env)
    rclone = Path(env["PCLOUD_TOOLS_RCLONE_BIN"])
    rclone.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$REAL_RCLONE_STUB_LOG\"\n"
        "sleep 5\n"
    )
    rclone.chmod(0o755)
    env.update(
        {
            "PCLOUD_TOOLS_REAL_TRANSFER_EXECUTION_GATE": "operator-approved-real-transfer-v1",
            "PCLOUD_TOOLS_REAL_TRANSFER_AUTOMATION_GATE": "operator-approved-real-transfer-automation-v1",
            "PCLOUD_TOOLS_REAL_TRANSFER_AUTOMATION_RUN_GATE": "operator-approved-real-transfer-automation-run-v1",
            "PCLOUD_TOOLS_TRANSFER_EXEC_TIMEOUT_SECONDS": "1",
            "PCLOUD_TOOLS_CHAT_NOTIFY_ENABLED": "1",
            "PCLOUD_TOOLS_CHAT_NOTIFY_CMD": f"{notify_cmd} {{message}}",
        }
    )
    (workspace / "Documents").mkdir(parents=True)
    (workspace / "Documents/slow-upload.txt").write_text("slow\n")
    pushd_dir = state_dir / "pushd"
    pushd_dir.mkdir(parents=True)
    (pushd_dir / "queue.json").write_text(
        json.dumps([{"path": "Documents/slow-upload.txt", "action": "upload", "reason": "slow"}])
    )
    shadow_workspace = tmp_path / "pcloud-shadow-validation-notify-dedupe" / "workspace"
    shadow_report = tmp_path / "shadow-validation-notify-dedupe.json"
    shadow_report.write_text(
        json.dumps(
            {
                "status": "ok",
                "workspace": str(shadow_workspace),
                "state_dir": str(shadow_workspace / ".dev-state" / "state"),
                "checks": [
                    {"name": "temporary workspace guard", "status": "ok"},
                    {"name": "temporary state dir guard", "status": "ok"},
                    {"name": "unsafe state dir guard", "status": "ok"},
                ],
            }
        )
    )

    command = [
        sys.executable,
        "-m",
        "pcloud_tools.cli",
        "pushd",
        "transfer",
        "automation-run",
        "--report-path",
        str(shadow_report),
        "--execute",
        "--consume-on-success",
        "--json",
    ]
    first = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    second = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    first_payload = _payload(first)
    second_payload = _payload(second)
    assert first.returncode == 1
    assert second.returncode == 1
    assert real_log.exists()
    assert len(real_log.read_text().splitlines()) == 2
    assert notify_log.exists()
    assert len(notify_log.read_text().splitlines()) == 1
    assert first_payload["details"]["chat notify results"][0]["attempted"] is True
    assert first_payload["details"]["chat notify results"][0]["suppressed"] is False
    assert second_payload["details"]["chat notify results"][0]["attempted"] is False
    assert second_payload["details"]["chat notify results"][0]["suppressed"] is True
    journal = json.loads((pushd_dir / "chat-notify-journal.json").read_text())
    assert journal["pushd:timeout:Documents/slow-upload.txt"]["suppressed_count"] == 1


def test_pushd_queue_add_and_clear_are_preview_first_dev_state_writes(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = _use_default_dev_state_dir(env)
    queue_file = state_dir / "pushd" / "queue.json"

    preview = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "queue",
            "add",
            "Documents/manual.pdf",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    assert preview.returncode == 0
    assert not queue_file.exists()
    assert _payload(preview)["details"]["queue items after"] == 1

    add = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "queue",
            "add",
            "Documents/manual.pdf",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    assert add.returncode == 0
    assert json.loads(queue_file.read_text())[0]["path"] == "Documents/manual.pdf"
    assert _payload(add)["summary"] == "pushd queue record appended"

    preview_remove = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "queue",
            "remove",
            "Documents/manual.pdf",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    assert preview_remove.returncode == 0
    assert json.loads(queue_file.read_text())[0]["path"] == "Documents/manual.pdf"
    assert _payload(preview_remove)["details"]["queue items removed"] == 1

    remove = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "queue",
            "remove",
            "Documents/manual.pdf",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    assert remove.returncode == 0
    assert json.loads(queue_file.read_text()) == []
    assert _payload(remove)["summary"] == "pushd queue records removed"

    add_again = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "queue",
            "add",
            "Documents/manual.pdf",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    assert add_again.returncode == 0

    clear = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "queue",
            "clear",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    assert clear.returncode == 0
    assert json.loads(queue_file.read_text()) == []
    assert _payload(clear)["details"]["queue items before"] == 1


def test_pushd_queue_remove_public_requires_explicit_gate(tmp_path: Path) -> None:
    env = _base_env(tmp_path, {"PCLOUD_TOOLS_DEV": "0"})
    public_core_dir = Path(env["HOME"]) / "p-core"
    public_core_dir.mkdir(parents=True)
    (public_core_dir / ".pcloud-sync-allowlist").write_text("Documents/\n")
    state_dir = _state_dir(env)
    pushd_dir = state_dir / "pushd"
    pushd_dir.mkdir(parents=True)
    queue_file = pushd_dir / "queue.json"
    queue_file.write_text(
        json.dumps(
            [
                {"path": "Documents/remote.txt", "action": "upload", "reason": "fswatch"},
                {"path": "Documents/keep.txt", "action": "upload", "reason": "fswatch"},
            ]
        )
    )

    refused = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "queue",
            "remove",
            "Documents/remote.txt",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    gated_env = env | {
        "PCLOUD_TOOLS_PUSHD_QUEUE_REMOVE_GATE": "operator-approved-pushd-queue-remove-v1",
    }
    removed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "queue",
            "remove",
            "Documents/remote.txt",
            "--reviewer-approved-queue-record-removal",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=gated_env,
    )

    refused_payload = _payload(refused)
    removed_payload = _payload(removed)
    assert refused.returncode == 1
    assert refused_payload["details"]["state writes"] == "none"
    assert "PCLOUD_TOOLS_DEV_EXECUTION" not in [issue["key"] for issue in refused_payload["issues"]]
    assert "PCLOUD_TOOLS_PUSHD_QUEUE_REMOVE_GATE" in [issue["key"] for issue in refused_payload["issues"]]
    assert removed.returncode == 0
    assert removed_payload["summary"] == "pushd queue records removed"
    assert removed_payload["details"]["queue remove gate env honored"] == "yes"
    assert removed_payload["details"]["queue record removal approval"] == "yes"
    assert removed_payload["details"]["state writes"] == "pushd queue only"
    assert json.loads(queue_file.read_text()) == [
        {"path": "Documents/keep.txt", "action": "upload", "reason": "fswatch"}
    ]


def test_pushd_missing_local_uploads_are_stale_and_do_not_block_downloads(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = _use_default_dev_state_dir(env)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    existing = workspace / "Documents" / "existing.txt"
    existing.parent.mkdir(parents=True)
    existing.write_text("local content\n")
    pushd_dir = state_dir / "pushd"
    diffd_dir = state_dir / "diffd"
    pushd_dir.mkdir(parents=True)
    diffd_dir.mkdir(parents=True)
    queue_file = pushd_dir / "queue.json"
    queue_file.write_text(
        json.dumps(
            [
                {"path": "Documents/missing.txt", "action": "upload", "reason": "fswatch"},
                {"path": "Documents/existing.txt", "action": "upload", "reason": "fswatch"},
            ]
        )
    )
    (diffd_dir / "remote-changes.json").write_text(
        json.dumps([{"path": "Documents/missing.txt", "action": "download", "reason": "diff:createfile"}])
    )

    pushd_status = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "status", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    pushd_preview = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "transfer", "preview", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    pushd_xbar = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "status", "--xbar"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    diffd_preview = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "diffd", "transfer", "preview", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    prune = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "queue",
            "prune-missing-local",
            "--reviewer-approved-missing-local-cleanup",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    status_payload = _payload(pushd_status)
    pushd_payload = _payload(pushd_preview)
    diffd_payload = _payload(diffd_preview)
    prune_payload = _payload(prune)
    assert pushd_status.returncode == 0
    assert status_payload["status"] == "warning"
    assert status_payload["details"]["missing local upload records"] == 1
    assert "pushd.queue.prune-missing-local" in [action["id"] for action in status_payload["actions"]]
    assert pushd_preview.returncode == 0
    assert pushd_payload["details"]["planned uploads"] == 1
    assert pushd_payload["details"]["missing local upload records"] == 1
    assert pushd_payload["details"]["planned transfer commands"][0]["path"] == "Documents/existing.txt"
    assert pushd_xbar.returncode == 0
    assert "missing local uploads: 1" in pushd_xbar.stdout
    assert "Missing local upload records" in pushd_xbar.stdout
    assert "Documents/missing.txt" in pushd_xbar.stdout
    assert "Ignore missing local upload records" in pushd_xbar.stdout
    assert "terminal=false" in pushd_xbar.stdout
    assert diffd_preview.returncode == 0
    assert diffd_payload["details"]["planned downloads"] == 1
    assert diffd_payload["details"]["manual review transfer records"] == 0
    assert prune.returncode == 0
    assert prune_payload["summary"] == "pushd queue missing local records pruned"
    assert prune_payload["details"]["queue items removed"] == 1
    assert json.loads(queue_file.read_text()) == [
        {"path": "Documents/existing.txt", "action": "upload", "reason": "fswatch"}
    ]


def test_diffd_remote_change_add_and_clear_are_dev_state_writes(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = _use_default_dev_state_dir(env)
    remote_file = state_dir / "diffd" / "remote-changes.json"

    add = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "diffd",
            "remote-change",
            "add",
            "Documents/remote.pdf",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    assert add.returncode == 0
    assert json.loads(remote_file.read_text())[0]["path"] == "Documents/remote.pdf"
    assert _payload(add)["summary"] == "diffd remote-change record appended"

    preview_remove = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "diffd",
            "remote-change",
            "remove",
            "Documents/remote.pdf",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    assert preview_remove.returncode == 0
    assert json.loads(remote_file.read_text())[0]["path"] == "Documents/remote.pdf"
    assert _payload(preview_remove)["details"]["remote changes removed"] == 1

    remove = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "diffd",
            "remote-change",
            "remove",
            "Documents/remote.pdf",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    assert remove.returncode == 0
    assert json.loads(remote_file.read_text()) == []
    assert _payload(remove)["summary"] == "diffd remote-change records removed"

    add_again = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "diffd",
            "remote-change",
            "add",
            "Documents/remote.pdf",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    assert add_again.returncode == 0

    clear = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "diffd",
            "remote-change",
            "clear",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    assert clear.returncode == 0
    assert json.loads(remote_file.read_text()) == []
    assert _payload(clear)["details"]["remote changes before"] == 1


def test_service_daemon_clear_action_ids_dispatch(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    pushd = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "action", "pushd.queue.clear.preview"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    diffd = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "action", "diffd.remote-change.clear.preview"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    assert pushd.returncode == 0
    assert "pushd queue clear preview is ready" in pushd.stdout
    assert diffd.returncode == 0
    assert "diffd remote-change clear preview is ready" in diffd.stdout


def test_notify_cli_toggles_env_and_xbar_actions_are_terminal_free(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    notify_log = tmp_path / "notify.log"
    notify_cmd = tmp_path / "notify"
    notify_cmd.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> {shlex.quote(str(notify_log))}\n"
    )
    notify_cmd.chmod(0o755)
    env["PCLOUD_TOOLS_CHAT_NOTIFY_CMD"] = f"{notify_cmd} {{message}}"

    status = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "notify", "status", "--xbar"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    enable = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "notify", "enable", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    test = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "notify", "test", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    disable = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "notify", "disable", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    enable_payload = _payload(enable)
    test_payload = _payload(test)
    disable_payload = _payload(disable)

    assert status.returncode == 0
    assert "Discord notify: off" in status.stdout
    assert "terminal=false" in status.stdout
    assert "Send Discord notify test" in status.stdout
    assert enable.returncode == 0
    assert enable_payload["details"]["state writes"].endswith(".env")
    assert enable_payload["details"]["chat notify enabled"] == "yes"
    assert test.returncode == 0
    assert test_payload["details"]["chat notify test result"]["attempted"] is True
    assert "pcloud-manager notify test" in notify_log.read_text()
    assert disable.returncode == 0
    assert disable_payload["details"]["chat notify enabled"] == "no"


def test_service_daemon_execute_refuses_state_dir_outside_workspace_dev_state(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    unsafe_state_dir = tmp_path / "fake-live"
    unsafe_state_dir.mkdir()
    env["PCLOUD_TOOLS_STATE_DIR"] = str(unsafe_state_dir)

    pushd = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "queue",
            "add",
            "Documents/manual.pdf",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    diffd = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "diffd",
            "remote-change",
            "add",
            "Documents/remote.pdf",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    pushd_payload = _payload(pushd)
    diffd_payload = _payload(diffd)
    assert pushd.returncode == 1
    assert diffd.returncode == 1
    assert pushd_payload["status"] == "error"
    assert diffd_payload["status"] == "error"
    assert "PCLOUD_TOOLS_DEV_STATE_DIR" in [issue["key"] for issue in pushd_payload["issues"]]
    assert "PCLOUD_TOOLS_DEV_STATE_DIR" in [issue["key"] for issue in diffd_payload["issues"]]
    assert not (unsafe_state_dir / "pushd" / "queue.json").exists()
    assert not (unsafe_state_dir / "diffd" / "remote-changes.json").exists()


def test_service_daemon_run_records_dry_run_state_in_dev_state(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = _use_default_dev_state_dir(env)
    pushd_dir = state_dir / "pushd"
    diffd_dir = state_dir / "diffd"
    pushd_dir.mkdir(parents=True)
    diffd_dir.mkdir(parents=True)
    (pushd_dir / "queue.json").write_text(
        json.dumps([{"path": "Documents/manual.pdf", "action": "upload"}])
    )
    (diffd_dir / "remote-changes.json").write_text(
        json.dumps([{"path": "Documents/remote.pdf", "action": "download"}])
    )

    pushd = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "run", "--execute", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    diffd = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "diffd", "run", "--execute", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    pushd_payload = _payload(pushd)
    diffd_payload = _payload(diffd)
    assert pushd.returncode == 0
    assert diffd.returncode == 0
    assert pushd_payload["summary"] == "pushd dry-run recorded"
    assert diffd_payload["summary"] == "diffd dry-run recorded"
    assert (pushd_dir / "last-plan.json").exists()
    assert (pushd_dir / "last-event.json").exists()
    assert (pushd_dir / "cursor").read_text().startswith("pushd:dry-run:")
    assert json.loads((pushd_dir / "last-plan.json").read_text())["counts"]["planned_uploads"] == 1
    assert json.loads((diffd_dir / "last-plan.json").read_text())["counts"]["planned_downloads"] == 1
    assert (diffd_dir / "cursor").read_text().startswith("diffd:dry-run:")


def test_service_daemon_run_refuses_unsafe_state_dir(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    unsafe_state_dir = tmp_path / "fake-live"
    unsafe_state_dir.mkdir()
    env["PCLOUD_TOOLS_STATE_DIR"] = str(unsafe_state_dir)

    result = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "run", "--execute", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    assert result.returncode == 1
    assert payload["status"] == "error"
    assert "PCLOUD_TOOLS_DEV_STATE_DIR" in [issue["key"] for issue in payload["issues"]]
    assert not (unsafe_state_dir / "pushd" / "last-plan.json").exists()


def test_service_daemon_run_action_ids_dispatch(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    pushd = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "action", "pushd.run.preview"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    diffd = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "action", "diffd.run.preview"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    assert pushd.returncode == 0
    assert "pushd run preview is ready" in pushd.stdout
    assert diffd.returncode == 0
    assert "diffd run preview is ready" in diffd.stdout


def test_service_daemon_real_gates_are_read_only_and_closed(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])

    pushd = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "gate", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    diffd = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "diffd", "gate", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    pushd_payload = _payload(pushd)
    diffd_payload = _payload(diffd)
    assert pushd.returncode == 0
    assert diffd.returncode == 0
    assert pushd_payload["status"] == "warning"
    assert diffd_payload["status"] == "warning"
    assert pushd_payload["summary"] == "pushd real-operation gate is closed"
    assert diffd_payload["summary"] == "diffd real-operation gate is closed"
    assert pushd_payload["details"]["operator verification required"] == "no"
    assert diffd_payload["details"]["operator verification required"] == "no"
    assert pushd_payload["details"]["human gate status"] == "required-before-real-work"
    assert diffd_payload["details"]["human gate status"] == "required-before-real-work"
    assert "real validation" in pushd_payload["details"]["human gate reason"]
    assert "archive decisions" in diffd_payload["details"]["human gate reason"]
    assert "actual pCloud/rclone transfer" in pushd_payload["details"]["next human check trigger"]
    assert "actual pCloud/rclone transfer" in diffd_payload["details"]["next human check trigger"]
    assert "fswatch resident daemon" in pushd_payload["details"]["blocked operations"]
    assert "pCloud API long-poll" in diffd_payload["details"]["blocked operations"]
    assert "capture first real upload target with transfer check --final-review" in pushd_payload["details"]["suggested next units"]
    assert "capture first real download target with transfer check --final-review" in diffd_payload["details"]["suggested next units"]
    assert (
        "hold real-run implementation until the human gate is explicitly confirmed"
        in pushd_payload["details"]["suggested next units"]
    )
    assert (
        "hold real-run implementation until the human gate is explicitly confirmed"
        in diffd_payload["details"]["suggested next units"]
    )
    assert (
        "document real-run queue consumption and rollback behavior before implementation"
        not in pushd_payload["details"]["suggested next units"]
    )
    assert (
        "document real-run remote-change consumption and rollback behavior before implementation"
        not in diffd_payload["details"]["suggested next units"]
    )
    assert "define fswatch event capture schema" not in pushd_payload["details"]["suggested next units"]
    assert "define pCloud diff response fixture schema" not in diffd_payload["details"]["suggested next units"]
    assert "pushd.gate" in [action["id"] for action in pushd_payload["actions"]]
    assert "diffd.gate" in [action["id"] for action in diffd_payload["actions"]]
    assert not (state_dir / "pushd").exists()
    assert not (state_dir / "diffd").exists()


def test_service_daemon_gate_action_ids_dispatch(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    pushd = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "action", "pushd.gate"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    diffd = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "action", "diffd.gate"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    assert pushd.returncode == 0
    assert "pushd real-operation gate is closed" in pushd.stdout
    assert diffd.returncode == 0
    assert "diffd real-operation gate is closed" in diffd.stdout


def test_dev_xbar_actions_use_executable_dev_wrapper(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    env["PCLOUD_TOOLS_WORKSPACE_ROOT"] = str(REPO_ROOT)
    env["PCLOUD_TOOLS_CONFIG_DIR"] = str(tmp_path / "config")
    env["PCLOUD_TOOLS_STATE_DIR"] = str(tmp_path / "state")
    env["PCLOUD_TOOLS_LOG_DIR"] = str(tmp_path / "logs")
    dev_entrypoint = REPO_ROOT / "pcloud-manager-dev"

    commands = [
        ("status", "--xbar"),
        ("sync", "status", "--xbar"),
        ("daemon", "status", "--xbar"),
        ("pushd", "status", "--xbar"),
        ("diffd", "status", "--xbar"),
    ]
    for command in commands:
        result = subprocess.run(
            [sys.executable, "-m", "pcloud_tools.cli", *command],
            check=False,
            capture_output=True,
            text=True,
            cwd=tmp_path,
            env=env,
        )
        bash_values = _xbar_bash_values(result.stdout)
        assert result.returncode == 0
        assert bash_values
        assert set(bash_values) == {str(dev_entrypoint)}
        assert os.access(dev_entrypoint, os.X_OK)


def test_live_xbar_actions_use_public_wrapper_from_path(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    public_entrypoint = bin_dir / "pcloud-manager"
    public_entrypoint.write_text("#!/bin/sh\nexit 0\n")
    public_entrypoint.chmod(0o755)
    allowlist = tmp_path / "allowlist"
    allowlist.write_text("Documents/\n")

    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PCLOUD_TOOLS_")
    }
    env.update(
        {
            "PYTHONPATH": str(REPO_ROOT / "src"),
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "PCLOUD_TOOLS_WORKSPACE_ROOT": str(tmp_path / "workspace"),
            "PCLOUD_TOOLS_CONFIG_DIR": str(tmp_path / "config"),
            "PCLOUD_TOOLS_STATE_DIR": str(tmp_path / "state"),
            "PCLOUD_TOOLS_LOG_DIR": str(tmp_path / "logs"),
            "PCLOUD_TOOLS_ALLOWLIST_FILE": str(allowlist),
            "HOME": str(tmp_path / "home"),
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
        }
    )
    for path in ("workspace", "config", "state", "logs", "home", "cache"):
        (tmp_path / path).mkdir()

    commands = [
        ("status", "--xbar"),
        ("sync", "status", "--xbar"),
        ("daemon", "status", "--xbar"),
        ("pushd", "status", "--xbar"),
        ("diffd", "status", "--xbar"),
    ]
    for command in commands:
        result = subprocess.run(
            [sys.executable, "-m", "pcloud_tools.cli", *command],
            check=False,
            capture_output=True,
            text=True,
            cwd=tmp_path,
            env=env,
        )
        bash_values = _xbar_bash_values(result.stdout)
        assert result.returncode in {0, 1}
        assert bash_values
        assert set(bash_values) == {str(public_entrypoint)}
        assert os.access(public_entrypoint, os.X_OK)


def test_shadow_validation_script_uses_temp_dev_state_only(tmp_path: Path) -> None:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PCLOUD_TOOLS_")
    }
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "pcloud-shadow-validation.py"), "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    assert result.returncode == 0
    assert payload["status"] == "ok"
    check_names = {check["name"] for check in payload["checks"]}
    assert "pushd dry-run state" in check_names
    assert "diffd dry-run state" in check_names
    assert "unsafe state dir guard" in check_names
    assert "temporary workspace guard" in check_names
    assert "temporary state dir guard" in check_names
    workspace = Path(str(payload["workspace"])).resolve()
    state_dir = Path(str(payload["state_dir"])).resolve()
    assert workspace.parent == Path(tempfile.gettempdir()).resolve() / workspace.parent.name
    assert workspace.parent.name.startswith("pcloud-shadow-validation-")
    assert state_dir == workspace / ".dev-state" / "state"


def test_shadow_validation_script_can_write_report_file(tmp_path: Path) -> None:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PCLOUD_TOOLS_")
    }
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    report_path = tmp_path / "reports" / "shadow-validation.json"
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "pcloud-shadow-validation.py"),
            "--report-path",
            str(report_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = json.loads(report_path.read_text())
    assert result.returncode == 0
    assert "report:" in result.stdout
    assert payload["status"] == "ok"
    assert payload["schema_version"] == "pcloud-tools-shadow-validation.v1"
    workspace = Path(str(payload["workspace"])).resolve()
    state_dir = Path(str(payload["state_dir"])).resolve()
    assert workspace.parent.name.startswith("pcloud-shadow-validation-")
    assert state_dir == workspace / ".dev-state" / "state"


def test_shadow_validation_script_summary_output_is_concise(tmp_path: Path) -> None:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PCLOUD_TOOLS_")
    }
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "pcloud-shadow-validation.py"), "--summary"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    assert result.returncode == 0
    assert "shadow validation: ok" in result.stdout
    assert "checks:" in result.stdout
    assert "- ok:" not in result.stdout


def test_shadow_validation_script_summary_can_save_full_report(tmp_path: Path) -> None:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PCLOUD_TOOLS_")
    }
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    report_path = tmp_path / "reports" / "shadow-validation-summary.json"
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "pcloud-shadow-validation.py"),
            "--summary",
            "--report-path",
            str(report_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = json.loads(report_path.read_text())
    assert result.returncode == 0
    assert "shadow validation: ok" in result.stdout
    assert "report:" in result.stdout
    assert "checks:" in result.stdout
    assert "- ok:" not in result.stdout
    assert payload["status"] == "ok"
    assert payload["checks"]
