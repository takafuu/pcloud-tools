from __future__ import annotations

from conftest import *


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
