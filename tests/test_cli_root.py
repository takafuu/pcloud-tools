from __future__ import annotations

from conftest import *


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
def test_top_level_status_and_doctor_surface_pushd_missing_local_details(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    existing = workspace / "Documents" / "existing.txt"
    existing.parent.mkdir(parents=True)
    existing.write_text("local content\n")
    pushd_dir = state_dir / "pushd"
    pushd_dir.mkdir(parents=True)
    (pushd_dir / "queue.json").write_text(
        json.dumps(
            [
                {"path": "Documents/missing.txt", "action": "upload", "reason": "fswatch"},
                {"path": "Documents/existing.txt", "action": "upload", "reason": "fswatch"},
            ]
        )
    )

    status = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "status", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    status_human = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "status"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    doctor = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "doctor", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    doctor_human = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "doctor"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    doctor_detail = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "doctor", "--detail"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    status_payload = _payload(status)
    doctor_payload = _payload(doctor)
    issue_keys = [issue["key"] for issue in status_payload["issues"]]

    assert status.returncode == 0
    assert status_payload["status"] == "warning"
    assert status_payload["details"]["push"] == "queued=2; planned=1; vanished-local=1; manual-review=0"
    assert status_payload["details"]["push review"] == "pcloud-manager pushd status"
    assert status_payload["details"]["push cleanup"] == "pcloud-manager action pushd.queue.prune-missing-local"
    assert "pushd warning: missing-local=1" not in status_payload["summary"]
    assert "PCLOUD_TOOLS_PUSHD_QUEUE_MISSING_LOCAL" not in issue_keys
    assert "push: queued=2; planned=1; vanished-local=1; manual-review=0" in status_human.stdout
    assert "push cleanup: pcloud-manager action pushd.queue.prune-missing-local" in status_human.stdout
    assert doctor.returncode == 0
    assert doctor_payload["status"] == "warning"
    assert doctor_payload["details"]["summary"] != "queue warning"
    assert doctor_payload["details"]["suspected cause"] != "pushd queue has missing local upload records"
    assert doctor_payload["details"]["push missing local detail"] == "vanished local candidates=1"
    assert doctor_human.returncode == 0
    assert "next:" in doctor_human.stdout
    assert "checks:" in doctor_human.stdout
    assert "push cleanup: pcloud-manager action pushd.queue.prune-missing-local" not in doctor_human.stdout
    assert "ignore missing local push records: pcloud-manager action pushd.queue.prune-missing-local" in doctor_human.stdout
    assert "config dir:" not in doctor_human.stdout
    assert doctor_detail.returncode == 0
    assert "config dir:" in doctor_detail.stdout
    assert "path1 list:" in doctor_detail.stdout
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
