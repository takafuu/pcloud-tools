from __future__ import annotations

from conftest import *


def test_mode_status_infers_daemon_with_bisync_unloaded(tmp_path: Path) -> None:
    _fake_launchctl, log, extra_env = _install_fake_mode_launchctl(tmp_path)
    result = _run_cli(tmp_path, "mode", "status", "--json", extra_env=extra_env)

    assert result.returncode == 0, result.stderr
    payload = _payload(result)
    assert payload["status"] in {"ok", "warning"}
    assert payload["summary"] == "pcloud-manager mode is daemon"
    details = payload["details"]
    assert details["current mode"] == "daemon"
    assert details["bisync loaded"] == "no"
    assert details["daemon services loaded"] == "yes"
    assert details["state writes"] == "none"
    assert "print gui/" in log.read_text()
def test_mode_switch_refuses_dirty_pushd_queue(tmp_path: Path) -> None:
    _fake_launchctl, log, extra_env = _install_fake_mode_launchctl(tmp_path)
    env = _base_env(tmp_path, extra_env)
    queue = _state_dir(env) / "pushd" / "queue.json"
    queue.parent.mkdir(parents=True, exist_ok=True)
    queue.write_text(json.dumps([{"path": "Documents/dirty.txt", "action": "upload"}]))

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "mode",
            "switch",
            "maintenance",
            "--execute",
            "--operator-reviewed-mode-plan",
            "--reviewer-approved-exclusive-policy",
            "--reviewer-approved-launchd-policy",
            "--reviewer-approved-rollback-policy",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={**env, "PCLOUD_TOOLS_MODE_SWITCH_GATE": "operator-approved-mode-switch-v1"},
    )

    assert result.returncode == 1
    payload = _payload(result)
    assert payload["status"] == "error"
    assert payload["details"]["state writes"] == "none"
    assert payload["details"]["launchctl execution"] == "no"
    assert any(issue["key"] == "PCLOUD_TOOLS_MODE_DIRTY_STATE" for issue in payload["issues"])
    assert "disable gui/" not in log.read_text()
def test_mode_switch_refuses_active_rclone_bisync_lock(tmp_path: Path) -> None:
    _fake_launchctl, log, extra_env = _install_fake_mode_launchctl(tmp_path)
    env = _base_env(tmp_path, extra_env)

    def encode(value: str) -> str:
        return value.replace("/", "_").replace(":", "_")

    lock_dir = Path(env["XDG_CACHE_HOME"]) / "rclone" / "bisync"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock = lock_dir / f"local_{encode(env['PCLOUD_TOOLS_WORKSPACE_ROOT'])}..{encode('pcloud:core')}.lck"
    lock.write_text(json.dumps({"PID": os.getpid()}))

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "mode",
            "switch",
            "maintenance",
            "--execute",
            "--operator-reviewed-mode-plan",
            "--reviewer-approved-exclusive-policy",
            "--reviewer-approved-launchd-policy",
            "--reviewer-approved-rollback-policy",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={**env, "PCLOUD_TOOLS_MODE_SWITCH_GATE": "operator-approved-mode-switch-v1"},
    )

    assert result.returncode == 1
    payload = _payload(result)
    dirty = payload["details"]["dirty state"]
    assert dirty["rclone bisync lock status"] == "present"
    assert dirty["rclone bisync lock process active"] == "yes"
    assert any(issue["key"] == "PCLOUD_TOOLS_MODE_DIRTY_STATE" for issue in payload["issues"])
    assert "disable gui/" not in log.read_text()
def test_mode_switch_refuses_closed_gate(tmp_path: Path) -> None:
    _fake_launchctl, log, extra_env = _install_fake_mode_launchctl(tmp_path)
    result = _run_cli(
        tmp_path,
        "mode",
        "switch",
        "maintenance",
        "--execute",
        "--operator-reviewed-mode-plan",
        "--reviewer-approved-exclusive-policy",
        "--reviewer-approved-launchd-policy",
        "--reviewer-approved-rollback-policy",
        "--json",
        extra_env=extra_env,
    )

    assert result.returncode == 1
    payload = _payload(result)
    assert payload["status"] == "error"
    assert payload["details"]["state writes"] == "none"
    assert payload["details"]["launchctl execution"] == "no"
    assert any(issue["key"] == "PCLOUD_TOOLS_MODE_SWITCH_APPROVAL" for issue in payload["issues"])
    assert "print gui/" in log.read_text()
    assert "disable gui/" not in log.read_text()
def test_mode_switch_executes_fake_launchctl_and_records_state(tmp_path: Path) -> None:
    _fake_launchctl, log, extra_env = _install_fake_mode_launchctl(tmp_path)
    result = _run_cli(
        tmp_path,
        "mode",
        "switch",
        "maintenance",
        "--execute",
        "--operator-reviewed-mode-plan",
        "--reviewer-approved-exclusive-policy",
        "--reviewer-approved-launchd-policy",
        "--reviewer-approved-rollback-policy",
        "--json",
        extra_env={
            **extra_env,
            "PCLOUD_TOOLS_MODE_SWITCH_GATE": "operator-approved-mode-switch-v1",
        },
    )

    assert result.returncode == 0, result.stderr
    payload = _payload(result)
    assert payload["status"] in {"ok", "warning"}
    assert payload["summary"] == "mode switch to maintenance completed"
    details = payload["details"]
    assert details["state writes"] == "mode switch state only"
    assert details["launchctl execution"] == "yes"
    assert any(result["tolerated"] for result in details["launchctl results"])
    launchctl_log = log.read_text()
    assert "disable gui/" in launchctl_log
    assert "bootstrap gui/" not in launchctl_log
    state_file = tmp_path / "state" / "mode" / "last-switch.json"
    assert json.loads(state_file.read_text())["mode"] == "maintenance"
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
        remote_trash_root="pcloud:core/.pcloud-manager-trash",
        remote_trash_index_file=state_dir / "pushd" / "trash-index.sqlite",
        remote_trash_warn_bytes=5368709120,
        pushd_missing_local_prune_ttl_seconds=600,
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
