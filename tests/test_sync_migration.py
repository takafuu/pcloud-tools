from __future__ import annotations

from conftest import *


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
                    "last resync scope": "scope-file",
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
                    "last resync scope": "scope-file",
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
                    "last resync scope": "scope-file",
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
                    "last resync scope": "scope-file",
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
                    "last resync scope": "scope-file",
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
    assert payload["details"]["scope mode"] == "scope-file"
    assert payload["details"]["state writes"] == "sync logs, lock, status, and migration run state"
    assert "bisync" in log.read_text()
    assert "SUCCESS mode=normal" in status_log
    assert run_state["mode"] == "normal"
    assert run_state["exit_code"] == 0
