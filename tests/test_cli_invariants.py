from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from pcloud_tools.config import AppConfig
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
    fake_rclone.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$FAKE_RCLONE_LOG\"\n")
    fake_rclone.chmod(0o755)
    env["PCLOUD_TOOLS_TRANSFER_EXECUTION_GATE"] = "dev-fake-rclone"
    env["PCLOUD_TOOLS_RCLONE_BIN"] = str(fake_rclone)
    env["FAKE_RCLONE_LOG"] = str(fake_log)
    return fake_log


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
        default_excludes=(),
        autosync_label="com.takafumi.pcloud-bisync",
        autosync_plist=tmp_path / "com.takafumi.pcloud-bisync.plist",
        indexer_bin=tmp_path / "pcloud-index",
        notify_bin=tmp_path / "pcloud-notify",
        rclone_bin="rclone",
        transfer_execution_gate="",
        transfer_exec_timeout_seconds=5,
        pushd_debounce_seconds=2,
        pushd_queue_limit=100,
        diffd_poll_interval_seconds=30,
        diffd_batch_limit=100,
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
    assert payload["summary"] == "pushd: not recorded; queued: 0; cursor: -"
    assert "PCLOUD_TOOLS_PUSHD_PID" in [issue["key"] for issue in payload["issues"]]


def test_service_daemon_status_summarizes_last_transfer_state(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    pushd_dir = state_dir / "pushd"
    diffd_dir = state_dir / "diffd"
    pushd_dir.mkdir(parents=True)
    diffd_dir.mkdir(parents=True)
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


def test_pushd_preview_builds_allowlisted_queue_plan(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    pushd_dir = state_dir / "pushd"
    pushd_dir.mkdir(parents=True)
    (pushd_dir / "queue.json").write_text(
        json.dumps(
            [
                {"path": "Documents/report.pdf", "action": "upload"},
                {"path": "Documents/.DS_Store", "action": "upload"},
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
    assert payload["details"]["pending queue items"] == 4
    assert payload["details"]["planned uploads"] == 1
    assert payload["details"]["excluded queue items"] == 2
    assert payload["details"]["invalid queue items"] == 1
    assert payload["details"]["plan summary"] == "upload: 1; excluded: 2; invalid: 1"
    assert payload["details"]["planned upload records"][0]["path"] == "Documents/report.pdf"


def test_pushd_fswatch_fixture_preview_is_read_only(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    fixture = tmp_path / "fswatch-events.txt"
    fixture.write_text(
        "\n".join(
            [
                "Documents/report.pdf\tCreated Updated",
                "Documents/.DS_Store\tUpdated",
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
    assert payload["details"]["parsed fswatch events"] == 3
    assert payload["details"]["invalid fswatch events"] == 1
    assert payload["details"]["planned uploads"] == 1
    assert payload["details"]["excluded queue items"] == 2
    assert payload["details"]["invalid queue items"] == 1
    assert payload["details"]["planned upload records"][0]["reason"] == "fswatch:Created,Updated"
    assert not (state_dir / "pushd").exists()


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
    assert payload["details"]["skipped download records"] == 2
    skipped = payload["details"]["skipped download record details"]
    assert skipped[0]["reason"] == "default exclude"
    assert skipped[1]["reason"] == "outside allowlist"


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


def test_transfer_previews_emit_commands_without_state_writes(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    pushd_dir = state_dir / "pushd"
    diffd_dir = state_dir / "diffd"
    pushd_dir.mkdir(parents=True)
    diffd_dir.mkdir(parents=True)
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


def test_transfer_check_is_read_only_and_reports_gate_prerequisites(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    pushd_dir = state_dir / "pushd"
    diffd_dir = state_dir / "diffd"
    pushd_dir.mkdir(parents=True)
    diffd_dir.mkdir(parents=True)
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


def test_transfer_check_accepts_operator_confirmations_without_opening_gate(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    pushd_dir = state_dir / "pushd"
    diffd_dir = state_dir / "diffd"
    pushd_dir.mkdir(parents=True)
    diffd_dir.mkdir(parents=True)
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
    assert payload["details"]["real transfer execution gate status"] == "closed: no accepted value in this build"
    assert payload["details"]["future real gate env var"] == "PCLOUD_TOOLS_REAL_TRANSFER_EXECUTION_GATE"
    assert payload["details"]["future real gate accepted value"] == "-"
    assert payload["details"]["fake-rclone gate reuse"] == "forbidden"
    assert payload["details"]["separate real gate approval status"] == "complete-read-only"
    assert {
        check["status"] for check in payload["details"]["separate real gate approval checks"]
    } == {"ok"}
    assert payload["details"]["operator verification required"] == "not-now"
    assert "actual pCloud/rclone transfer" in payload["details"]["next human check trigger"]
    assert standalone_payload["details"]["operator verification required"] == "no"
    assert payload["details"]["future real-run policy status"] == "documented-read-only"
    assert "pushd queue record" in payload["details"]["future real-run success policy"]
    assert "retain matching pushd queue record" in payload["details"]["future real-run failure policy"]
    assert payload["details"]["future real-run policy state writes"] == "none"
    assert payload["details"]["state writes"] == "none"
    assert "PCLOUD_TOOLS_REAL_TRANSFER_EXECUTION_GATE" in [issue["key"] for issue in payload["issues"]]
    assert not (pushd_dir / "last-transfer.json").exists()


def test_transfer_real_run_is_hard_refusal(tmp_path: Path) -> None:
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
    assert action_result.returncode == 1
    assert payload["command"] == "pushd transfer real-run"
    assert payload["status"] == "error"
    assert payload["summary"] == "pushd real transfer execution is unavailable"
    assert payload["details"]["implementation status"] == "hard refusal; no real rclone/pCloud execution path exists"
    assert payload["details"]["real transfer gate status"] == "closed"
    assert payload["details"]["real transfer execution gate status"] == "closed: no accepted value in this build"
    assert payload["details"]["execute requested"] == "yes"
    assert payload["details"]["real gate env provided"] == "yes"
    assert payload["details"]["real gate env honored"] == "no"
    assert payload["details"]["fake-rclone gate reuse"] == "forbidden"
    assert payload["details"]["fake-rclone gate env provided"] == "yes"
    assert payload["details"]["fake-rclone gate env honored"] == "no"
    assert payload["details"]["state writes"] == "none"
    assert payload["details"]["safe alternative command"][1:4] == ["pushd", "transfer", "real-gate"]
    assert "PCLOUD_TOOLS_REAL_TRANSFER_EXECUTION_GATE" in [issue["key"] for issue in payload["issues"]]
    assert "pushd real transfer execution is unavailable" in action_result.stdout
    assert "safe alternative:" in action_result.stdout
    assert "real gate env provided: no" in action_result.stdout
    assert "fake-rclone gate env honored: no" in action_result.stdout
    assert not (pushd_dir / "last-transfer.json").exists()


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
    assert "does not match planned path" in checks["first real run target"]["detail"]
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
    assert diffd.returncode == 0
    assert "diffd download transfer preview is ready" in diffd.stdout
    assert pushd_check.returncode == 0
    assert "pushd real transfer gate checklist is not open" in pushd_check.stdout
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


def test_transfer_consume_preview_reports_successful_records_without_writes(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = _use_default_dev_state_dir(env)
    _install_fake_rclone(env)
    pushd_dir = state_dir / "pushd"
    pushd_dir.mkdir(parents=True)
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
    assert "state writes: none" in preview.stdout
    assert "successful transfers: 1" in preview.stdout
    assert "planned record removals: 1" in preview.stdout
    assert "first removal: Documents/upload.pdf (upload)" in preview.stdout
    assert structured.returncode == 0
    assert payload["details"]["implementation status"].startswith("read-only consume preview")
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
    assert "actual pCloud/rclone transfer" in pushd_payload["details"]["next human check trigger"]
    assert "actual pCloud/rclone transfer" in diffd_payload["details"]["next human check trigger"]
    assert "fswatch resident daemon" in pushd_payload["details"]["blocked operations"]
    assert "pCloud API long-poll" in diffd_payload["details"]["blocked operations"]
    assert "capture first real upload target with transfer check --final-review" in pushd_payload["details"]["suggested next units"]
    assert "capture first real download target with transfer check --final-review" in diffd_payload["details"]["suggested next units"]
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
