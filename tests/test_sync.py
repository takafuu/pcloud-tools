from __future__ import annotations

from conftest import *
from pcloud_tools.config import load_config
from pcloud_tools.runtime import RuntimePaths
from pcloud_tools.sync_scope import prepare_sync_filter_rules, sync_allowlist_info


def test_dev_sync_execute_is_refused_before_remote_execution(tmp_path: Path) -> None:
    result = _run_cli(tmp_path, "sync", "--execute", "--json")

    payload = _payload(result)
    assert result.returncode == 1
    assert payload["status"] == "error"
    assert payload["summary"] == "dev mode refuses to execute bisync against a configured remote"
    assert [issue["key"] for issue in payload["issues"]] == ["PCLOUD_TOOLS_DEV_EXECUTION"]


def test_root_allowlist_builds_safe_bisync_filter_rules(tmp_path: Path, monkeypatch) -> None:
    env = _base_env(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    (workspace / ".pcloud-sync-allowlist").write_text("/\n")

    paths = RuntimePaths(
        workspace_root=workspace,
        config_dir=Path(env["PCLOUD_TOOLS_CONFIG_DIR"]),
        state_dir=Path(env["PCLOUD_TOOLS_STATE_DIR"]),
        log_dir=Path(env["PCLOUD_TOOLS_LOG_DIR"]),
    )
    config = load_config(paths).config
    info = sync_allowlist_info(config)
    rules = prepare_sync_filter_rules(config, info.entries)

    assert info.entries == ("/",)
    assert "+ /**" in rules
    assert "- /**/.git/**" in rules
    assert "- /.pcloud-manager-trash/**" in rules
    assert "- /**/.pcloud-manager-trash/**" in rules
    assert "- /**/.venv/**" in rules
    assert "- /**/node_modules/**" in rules
    assert "- /**/tmp/**" in rules
    assert "- /**/temp/**" in rules
    assert "- /**/.env" in rules
    assert "- /LLM/**" in rules
    assert rules.index("+ /**") < rules.index("- /**")


def test_sync_execute_is_refused_when_daemon_mode_is_loaded(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    launchctl = bin_dir / "launchctl"
    launchctl.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"print\" ]; then\n"
        "  case \"$2\" in\n"
        "    *com.takafumi.pcloud-pushd*) exit 0 ;;\n"
        "  esac\n"
        "fi\n"
        "exit 113\n"
    )
    launchctl.chmod(0o755)
    lock_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"]) / "bisync.lock"
    lock_dir.mkdir(parents=True)
    (lock_dir / "pid").write_text("999999\n")
    (lock_dir / "mode").write_text("normal\n")
    (lock_dir / "started_at").write_text("2026-04-25 00:00:00\n")

    env.update(
        {
            "PCLOUD_TOOLS_DEV": "0",
            "PCLOUD_TOOLS_CORE_DIR": str(workspace),
            "PCLOUD_TOOLS_ALLOWLIST_FILE": str(workspace / ".pcloud-sync-allowlist"),
            "PCLOUD_TOOLS_MANAGER_IGNORE_FILE": str(workspace / ".pcloudmanagerignore"),
            "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
        }
    )

    result = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "sync", "--execute", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    assert result.returncode == 1
    assert payload["summary"] == "sync execution is refused while daemon mode is loaded"
    assert payload["issues"][0]["key"] == "PCLOUD_TOOLS_MODE_EXCLUSIVE"


def test_sync_background_json_emits_baseline_report(tmp_path: Path) -> None:
    result = _run_cli(tmp_path, "sync", "background", "--json")

    payload = _payload(result)
    assert result.returncode == 0
    assert payload["schema_version"] == "pcloud-tools-report.v1"
    assert payload["command"] == "sync background"
    assert payload["status"] in {"ok", "warning"}
    assert isinstance(payload["summary"], str)
    assert payload["summary"]


def test_sync_check_scope_is_primary_name_with_legacy_alias(tmp_path: Path) -> None:
    scope_result = _run_cli(tmp_path / "scope", "sync", "check-scope", "--json")
    legacy_result = _run_cli(tmp_path / "legacy", "sync", "check-allowlist", "--json")

    scope_payload = _payload(scope_result)
    legacy_payload = _payload(legacy_result)

    assert scope_result.returncode == 0
    assert scope_payload["command"] == "sync check-scope"
    assert scope_payload["summary"].startswith("sync scope loaded")
    assert "scope file" in scope_payload["details"]
    assert "allowlist" not in scope_payload["details"]

    assert legacy_result.returncode == 0
    assert legacy_payload["command"] == "sync check-allowlist"
    assert legacy_payload["summary"] == scope_payload["summary"]


def test_sync_help_hides_legacy_check_allowlist_alias(tmp_path: Path) -> None:
    result = _run_cli(tmp_path, "sync", "--help")

    assert result.returncode == 0
    assert "check-scope" in result.stdout
    assert "check-allowlist" not in result.stdout


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


def test_sync_scope_allows_source_roots_when_allowlisted(tmp_path: Path) -> None:
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
    assert payload["status"] in {"ok", "warning"}
    assert "PCLOUD_TOOLS_SCOPE_POLICY" not in issue_keys
    assert payload["details"]["entries"] == ["Documents/", "dev/", "project/", "tools/"]


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
