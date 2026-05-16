from __future__ import annotations

from conftest import *


def test_validate_gate_accepts_matching_env_and_flags() -> None:
    spec = GATES["pushd.launchd.reload"]
    args = argparse.Namespace(
        reviewer_approved_bootout_bootstrap=True,
        reviewer_approved_rollback_policy=True,
    )

    result = validate_gate(spec, args, {spec.env_var: spec.expected_value})

    assert result.env_ok is True
    assert result.flags_ok is True
    assert result.complete is True
    assert result.missing_flags == ()
def test_validate_gate_rejects_env_mismatch() -> None:
    spec = GATES["pushd.launchd.reload"]
    args = argparse.Namespace(
        reviewer_approved_bootout_bootstrap=True,
        reviewer_approved_rollback_policy=True,
    )

    result = validate_gate(spec, args, {spec.env_var: "wrong"})

    assert result.env_ok is False
    assert result.flags_ok is True
    assert result.complete is False
    assert result.env_value == "wrong"
def test_validate_gate_reports_missing_flags() -> None:
    spec = GATES["pushd.launchd.reload"]
    args = argparse.Namespace(
        reviewer_approved_bootout_bootstrap=True,
        reviewer_approved_rollback_policy=False,
    )

    result = validate_gate(spec, args, {spec.env_var: spec.expected_value})

    assert result.env_ok is True
    assert result.flags_ok is False
    assert result.complete is False
    assert result.missing_flags == ("--reviewer-approved-rollback-policy",)


def test_launchd_reload_gate_specs_cover_pushd_and_diffd() -> None:
    assert GATES["pushd.launchd.reload"].env_var == "PCLOUD_TOOLS_PUSHD_LAUNCHD_RELOAD_GATE"
    assert GATES["pushd.launchd.reload"].expected_value == "operator-approved-pushd-launchd-reload-v1"
    assert GATES["diffd.launchd.reload"].env_var == "PCLOUD_TOOLS_DIFFD_LAUNCHD_RELOAD_GATE"
    assert GATES["diffd.launchd.reload"].expected_value == "operator-approved-diffd-launchd-reload-v1"


def test_resident_and_api_gate_specs_match_existing_public_contracts() -> None:
    assert GATES["pushd.fswatch.resident"].env_var == "PCLOUD_TOOLS_PUSHD_FSWATCH_RESIDENT_GATE"
    assert GATES["pushd.fswatch.resident"].expected_value == "operator-approved-fswatch-resident-v1"
    assert GATES["pushd.fswatch.resident"].approval_flags == (
        "--operator-reviewed-probe",
        "--reviewer-approved-queue-policy",
        "--reviewer-approved-process-policy",
    )
    assert GATES["diffd.api.long-poll"].env_var == "PCLOUD_TOOLS_DIFFD_API_LONG_POLL_GATE"
    assert GATES["diffd.api.long-poll"].expected_value == "operator-approved-api-long-poll-v1"
    assert GATES["diffd.api.long-poll"].approval_flags == (
        "--operator-reviewed-preview",
        "--reviewer-approved-response-policy",
        "--reviewer-approved-credential-policy",
        "--reviewer-approved-process-policy",
    )


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
