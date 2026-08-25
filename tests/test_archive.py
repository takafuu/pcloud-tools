from __future__ import annotations

from conftest import *


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
            "legacy",
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
    assert "legacy.old-monolith.gate" in [action["id"] for action in payload["actions"]]
    assert not (workspace / ".dev-state" / "old-monolith-archive").exists()
def test_archive_old_monolith_gate_missing_backup_stays_pending(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    result = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "legacy", "old-monolith-gate", "--json"],
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
                "legacy",
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
                "legacy",
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


def test_archive_old_monolith_alias_warns_and_routes_to_legacy(tmp_path: Path) -> None:
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
    assert payload["command"] == "archive old-monolith-gate"
    assert "PCLOUD_TOOLS_ARCHIVE_DEPRECATED" in [issue["key"] for issue in payload["issues"]]
    assert "legacy.old-monolith.gate" in [action["id"] for action in payload["actions"]]
