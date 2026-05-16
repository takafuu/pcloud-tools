from __future__ import annotations

from conftest import *


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
