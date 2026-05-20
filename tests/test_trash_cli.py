from __future__ import annotations

from conftest import *


def _install_trash_rclone(env: dict[str, str]) -> Path:
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    bin_dir = workspace / ".dev-state" / "trash-bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    log = workspace / ".dev-state" / "trash-rclone.log"
    rclone = bin_dir / "rclone"
    rclone.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$TRASH_RCLONE_LOG\"\n"
        "case \"$1\" in\n"
        "  size) printf '{\"bytes\":123,\"count\":2}\\n'; exit 0 ;;\n"
        "  moveto) [ \"$TRASH_RCLONE_FAIL_MOVETO\" = \"1\" ] && exit 44; exit 0 ;;\n"
        "  copyto) exit 0 ;;\n"
        "  deletefile) exit 0 ;;\n"
        "  lsjson) exit 0 ;;\n"
        "  lsf) printf '%s\\n' 'objects/2026/05/20/remoteid__通知書.pdf' 'objects/2026/05/20/remoteid__通知書.pdf.json'; exit 0 ;;\n"
        "  cat) printf '%s\\n' '{\"schema_version\":\"pcloud-tools-remote-trash.v1\",\"item_id\":\"remoteid\",\"original_path\":\"受信/通知書.pdf\",\"object_path\":\".pcloud-manager-trash/objects/2026/05/20/remoteid__通知書.pdf\",\"metadata_path\":\".pcloud-manager-trash/objects/2026/05/20/remoteid__通知書.pdf.json\",\"display_name\":\"通知書.pdf\",\"operation\":\"delete\",\"created_at\":\"2026-05-20T00:00:00+00:00\"}'; exit 0 ;;\n"
        "esac\n"
        "exit 0\n"
    )
    rclone.chmod(0o755)
    env["PCLOUD_TOOLS_RCLONE_BIN"] = str(rclone)
    env["TRASH_RCLONE_LOG"] = str(log)
    return log


def _trash_apply_gate_env(env: dict[str, str]) -> dict[str, str]:
    return env | {
        "PCLOUD_TOOLS_PUSHD_TRASH_APPLY_GATE": "operator-approved-pushd-trash-apply-v1",
    }


def _trash_purge_gate_env(env: dict[str, str]) -> dict[str, str]:
    return env | {
        "PCLOUD_TOOLS_PUSHD_TRASH_PURGE_GATE": "operator-approved-pushd-trash-purge-v1",
    }


def test_pushd_trash_apply_preview_selects_delete_and_missing_rename_only(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = _use_default_dev_state_dir(env)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    pushd_dir = state_dir / "pushd"
    pushd_dir.mkdir(parents=True)
    _write_workspace_file(env, "Documents/rename-present.txt", "still here\n")
    (workspace / ".pcloud-sync-allowlist").write_text("/\n")
    (pushd_dir / "queue.json").write_text(
        json.dumps(
            [
                {"path": "Documents/delete-me.txt", "action": "delete", "reason": "fswatch:Removed"},
                {"path": "Documents/rename-old.txt", "action": "rename", "reason": "fswatch:Renamed"},
                {"path": "Documents/rename-present.txt", "action": "rename", "reason": "fswatch:Renamed"},
                {"path": "Documents/missing-upload.txt", "action": "upload", "reason": "fswatch:Created"},
                {"path": ".pcloud-manager-trash/objects/item", "action": "delete", "reason": "test"},
            ]
        )
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "trash",
            "apply",
            "--limit",
            "10",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    candidates = payload["details"]["candidate details"]

    assert result.returncode == 0
    assert payload["details"]["candidate count"] == 2
    assert [item["path"] for item in candidates] == ["Documents/delete-me.txt", "Documents/rename-old.txt"]
    assert {item["trash reason"] for item in candidates} == {"local delete", "rename old path"}
    assert payload["details"]["state writes"] == "none"
    assert not (pushd_dir / "trash-index.sqlite").exists()


def test_pushd_trash_apply_execute_uses_dedicated_gate_and_consumes_exact_record(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = _use_default_dev_state_dir(env)
    log = _install_trash_rclone(env)
    pushd_dir = state_dir / "pushd"
    pushd_dir.mkdir(parents=True)
    queue_file = pushd_dir / "queue.json"
    keep_upload = {"path": "Documents/delete-me.txt", "action": "upload", "reason": "fswatch:Created"}
    keep_delete = {"path": "Documents/delete-me.txt", "action": "delete", "reason": "other-reason"}
    remove_delete = {"path": "Documents/delete-me.txt", "action": "delete", "reason": "fswatch:Removed"}
    queue_file.write_text(json.dumps([remove_delete, keep_upload, keep_delete]))

    refused = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "trash",
            "apply",
            "--path",
            "Documents/delete-me.txt",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env | {"PCLOUD_TOOLS_REAL_TRANSFER_EXECUTION_GATE": "operator-approved-real-transfer-v1"},
    )
    executed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "trash",
            "apply",
            "--path",
            "Documents/delete-me.txt",
            "--execute",
            "--operator-reviewed-trash-candidates",
            "--reviewer-approved-remote-trash-move",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=_trash_apply_gate_env(env),
    )

    refused_payload = _payload(refused)
    executed_payload = _payload(executed)
    queue_payload = json.loads(queue_file.read_text())
    log_lines = log.read_text().splitlines()

    assert refused.returncode == 1
    assert refused_payload["summary"] == "pushd remote trash apply refused"
    assert refused_payload["details"]["real_transfer gates reused"] == "no"
    assert "PCLOUD_TOOLS_PUSHD_TRASH_APPLY_GATE" in [issue["key"] for issue in refused_payload["issues"]]
    assert executed.returncode == 0
    assert executed_payload["summary"] == "pushd remote trash apply completed"
    assert executed_payload["details"]["state writes"] == (
        "remote trash, sidecar metadata, local index, and matching pushd queue record"
    )
    assert queue_payload == [keep_upload, keep_delete]
    assert len(log_lines) == 2
    assert log_lines[0].startswith("moveto pcloud:core/Documents/delete-me.txt ")
    assert log_lines[1].startswith("copyto ")
    assert (state_dir / "pushd" / "trash-index.sqlite").exists()


def test_pushd_trash_apply_failure_retains_queue_record(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = _use_default_dev_state_dir(env)
    _install_trash_rclone(env)
    pushd_dir = state_dir / "pushd"
    pushd_dir.mkdir(parents=True)
    queue_file = pushd_dir / "queue.json"
    queue_payload = [{"path": "Documents/delete-me.txt", "action": "delete", "reason": "fswatch:Removed"}]
    queue_file.write_text(json.dumps(queue_payload))

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "trash",
            "apply",
            "--execute",
            "--operator-reviewed-trash-candidates",
            "--reviewer-approved-remote-trash-move",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=_trash_apply_gate_env(env) | {"TRASH_RCLONE_FAIL_MOVETO": "1"},
    )

    payload = _payload(result)

    assert result.returncode == 1
    assert payload["summary"] == "pushd remote trash apply refused"
    assert payload["details"]["results"][0]["queue records removed"] == 0
    assert json.loads(queue_file.read_text()) == queue_payload


def test_pushd_trash_search_falls_back_to_remote_sidecar_metadata(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    _use_default_dev_state_dir(env)
    _install_trash_rclone(env)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "trash",
            "search",
            "通知書",
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
    assert payload["details"]["state writes"] == "none"
    assert payload["details"]["remote fallback matches"] >= 1
    assert any(match["original_path"] == "受信/通知書.pdf" for match in payload["details"]["matches"])


def test_pushd_trash_status_restore_preview_and_purge_are_read_only_or_dedicated_gated(
    tmp_path: Path,
) -> None:
    env = _base_env(tmp_path)
    state_dir = _use_default_dev_state_dir(env)
    log = _install_trash_rclone(env)
    pushd_dir = state_dir / "pushd"
    pushd_dir.mkdir(parents=True)
    queue_file = pushd_dir / "queue.json"
    queue_file.write_text(json.dumps([{"path": "Documents/delete-me.txt", "action": "delete", "reason": "test"}]))
    apply = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "trash",
            "apply",
            "--execute",
            "--operator-reviewed-trash-candidates",
            "--reviewer-approved-remote-trash-move",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=_trash_apply_gate_env(env),
    )
    item_id = _payload(apply)["details"]["results"][0]["candidate"]["item_id"]

    status = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "trash", "status", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    restore = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "trash",
            "restore-preview",
            item_id,
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    purge_refused = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "trash", "purge", "--execute", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env | {"PCLOUD_TOOLS_REAL_TRANSFER_EXECUTION_GATE": "operator-approved-real-transfer-v1"},
    )
    purge = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "trash",
            "purge",
            "--execute",
            "--operator-reviewed-trash-status",
            "--reviewer-approved-permanent-delete",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=_trash_purge_gate_env(env),
    )

    status_payload = _payload(status)
    restore_payload = _payload(restore)
    purge_refused_payload = _payload(purge_refused)
    purge_payload = _payload(purge)

    assert status.returncode == 0
    assert status_payload["details"]["state writes"] == "none"
    assert status_payload["details"]["trash bytes"] == 123
    assert restore.returncode == 0
    assert restore_payload["details"]["implementation status"] == "restore preview only; no local or remote writes"
    assert purge_refused.returncode == 1
    assert purge_refused_payload["summary"] == "pushd remote trash purge refused"
    assert "PCLOUD_TOOLS_PUSHD_TRASH_PURGE_GATE" in [
        issue["key"] for issue in purge_refused_payload["issues"]
    ]
    assert purge.returncode == 0
    assert purge_payload["summary"] == "pushd remote trash purge completed"
    assert purge_payload["details"]["results"][0]["purged"] is True
    assert any(line.startswith("deletefile pcloud:core/.pcloud-manager-trash/objects/") for line in log.read_text().splitlines())
