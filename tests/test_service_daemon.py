from __future__ import annotations

from conftest import *


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
