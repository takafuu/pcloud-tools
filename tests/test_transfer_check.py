from __future__ import annotations

from conftest import *


def test_transfer_previews_emit_commands_without_state_writes(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    pushd_dir = state_dir / "pushd"
    diffd_dir = state_dir / "diffd"
    pushd_dir.mkdir(parents=True)
    diffd_dir.mkdir(parents=True)
    _write_workspace_file(env, "Documents/final-upload.txt", "upload\n")
    _write_workspace_file(env, "Documents/upload.pdf", "upload\n")
    (pushd_dir / "queue.json").write_text(
        json.dumps([{"path": "Documents/upload.pdf", "action": "upload", "reason": "test"}])
    )
    (diffd_dir / "remote-changes.json").write_text(
        json.dumps([{"path": "Documents/download.pdf", "action": "download", "reason": "test"}])
    )

    pushd_human = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "transfer", "preview"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    pushd = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "transfer", "preview", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    diffd = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "diffd", "transfer", "preview", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    pushd_payload = _payload(pushd)
    diffd_payload = _payload(diffd)
    assert pushd_human.returncode == 0
    assert "pushd transfer preview:" in pushd_human.stdout
    assert "gate: closed" in pushd_human.stdout
    assert "state writes: none" in pushd_human.stdout
    assert "planned transfers: 1" in pushd_human.stdout
    assert "first target: upload Documents/upload.pdf" in pushd_human.stdout
    assert "first command:" in pushd_human.stdout
    assert "planned transfer commands:" not in pushd_human.stdout
    assert "core dir:" not in pushd_human.stdout
    assert pushd.returncode == 0
    assert diffd.returncode == 0
    assert pushd_payload["summary"] == "pushd upload transfer preview is ready"
    assert diffd_payload["summary"] == "diffd download transfer preview is ready"
    assert pushd_payload["details"]["implementation status"] == "transfer command preview only; rclone is not executed"
    assert diffd_payload["details"]["implementation status"] == "transfer command preview only; rclone is not executed"
    assert pushd_payload["details"]["gate status"] == "closed"
    assert diffd_payload["details"]["gate status"] == "closed"
    assert pushd_payload["details"]["state writes"] == "none"
    assert diffd_payload["details"]["state writes"] == "none"
    push_command = pushd_payload["details"]["planned transfer commands"][0]["command"]
    diff_command = diffd_payload["details"]["planned transfer commands"][0]["command"]
    assert push_command[1] == "copyto"
    assert push_command[2].endswith("/Documents/upload.pdf")
    assert push_command[3].endswith("/Documents/upload.pdf")
    assert diff_command[1] == "copyto"
    assert diff_command[2].endswith("/Documents/download.pdf")
    assert diff_command[3].endswith("/Documents/download.pdf")
    assert not (pushd_dir / "last-plan.json").exists()
    assert not (diffd_dir / "last-plan.json").exists()
def test_transfer_preview_routes_conflicts_and_delete_rename_actions_to_manual_review(
    tmp_path: Path,
) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    pushd_dir = state_dir / "pushd"
    diffd_dir = state_dir / "diffd"
    pushd_dir.mkdir(parents=True)
    diffd_dir.mkdir(parents=True)
    _write_workspace_file(env, "Documents/upload.pdf", "upload\n")
    _write_workspace_file(env, "Documents/conflict.pdf", "conflict\n")
    (pushd_dir / "queue.json").write_text(
        json.dumps(
            [
                {"path": "Documents/upload.pdf", "action": "upload", "reason": "ok"},
                {"path": "Documents/conflict.pdf", "action": "upload", "reason": "local"},
                {"path": "Documents/deleted.pdf", "action": "delete", "reason": "local-delete"},
            ]
        )
    )
    (diffd_dir / "remote-changes.json").write_text(
        json.dumps(
            [
                {"path": "Documents/download.pdf", "action": "download", "reason": "ok"},
                {"path": "Documents/conflict.pdf", "action": "download", "reason": "remote"},
                {"path": "Documents/renamed.pdf", "action": "rename", "reason": "remote-rename"},
            ]
        )
    )

    pushd = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "transfer", "preview", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    diffd = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "diffd", "transfer", "preview", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    pushd_payload = _payload(pushd)
    diffd_payload = _payload(diffd)
    pushd_manual = pushd_payload["details"]["manual review transfer record details"]
    diffd_manual = diffd_payload["details"]["manual review transfer record details"]

    assert pushd.returncode == 0
    assert diffd.returncode == 0
    assert pushd_payload["status"] == "warning"
    assert diffd_payload["status"] == "warning"
    assert pushd_payload["details"]["planned uploads"] == 1
    assert diffd_payload["details"]["planned downloads"] == 1
    assert pushd_payload["details"]["real execution can run"] == "no"
    assert diffd_payload["details"]["real execution can run"] == "no"
    assert pushd_payload["details"]["real execution readiness"] == "blocked-preview"
    assert diffd_payload["details"]["real execution readiness"] == "blocked-preview"
    assert pushd_payload["details"]["manual review transfer records"] == 2
    assert diffd_payload["details"]["manual review transfer records"] == 2
    assert [item["path"] for item in pushd_payload["details"]["planned transfer commands"]] == [
        "Documents/upload.pdf"
    ]
    assert [item["path"] for item in diffd_payload["details"]["planned transfer commands"]] == [
        "Documents/download.pdf"
    ]
    assert {item["path"] for item in pushd_manual} == {
        "Documents/conflict.pdf",
        "Documents/deleted.pdf",
    }
    assert {item["path"] for item in diffd_manual} == {
        "Documents/conflict.pdf",
        "Documents/renamed.pdf",
    }
    assert any("opposite-side change" in item["reason"] for item in pushd_manual)
    assert any("delete action" in item["reason"] for item in pushd_manual)
    assert any("opposite-side change" in item["reason"] for item in diffd_manual)
    assert any("rename action" in item["reason"] for item in diffd_manual)
    assert "PCLOUD_TOOLS_PUSHD_TRANSFER_MANUAL_REVIEW" in [
        issue["key"] for issue in pushd_payload["issues"]
    ]
    assert "PCLOUD_TOOLS_DIFFD_TRANSFER_MANUAL_REVIEW" in [
        issue["key"] for issue in diffd_payload["issues"]
    ]
def test_transfer_validation_matrix_is_read_only_and_lists_human_review_cases(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])

    pushd = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "transfer", "validation-matrix", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    pushd_human = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "transfer", "validation-matrix"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    diffd = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "diffd", "transfer", "validation-matrix", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    pushd_payload = _payload(pushd)
    diffd_payload = _payload(diffd)
    pushd_details = pushd_payload["details"]
    diffd_details = diffd_payload["details"]
    pushd_first = pushd_details["cases"][0]
    diffd_ids = {case["id"] for case in diffd_details["cases"]}

    assert pushd.returncode == 0
    assert pushd_human.returncode == 0
    assert diffd.returncode == 0
    assert "pushd transfer validation-matrix:" in pushd_human.stdout
    assert "setup:" in pushd_human.stdout
    assert "check:" in pushd_human.stdout
    assert "state writes: none" in pushd_human.stdout
    assert "{'id':" not in pushd_human.stdout
    assert pushd_payload["command"] == "pushd transfer validation-matrix"
    assert diffd_payload["command"] == "diffd transfer validation-matrix"
    assert pushd_details["implementation status"] == (
        "read-only matrix; no setup, transfer, consume, or cleanup command is executed"
    )
    assert diffd_details["implementation status"] == (
        "read-only matrix; no setup, transfer, consume, or cleanup command is executed"
    )
    assert pushd_details["real execution can run"] == "no"
    assert diffd_details["real execution can run"] == "no"
    assert pushd_details["state writes"] == "none"
    assert diffd_details["state writes"] == "none"
    assert pushd_details["case count"] == 5
    assert diffd_details["case count"] == 6
    assert "remote-only-download" in diffd_ids
    assert pushd_first["commands"]["setup"][1:4] == ["pushd", "queue", "add"]
    assert pushd_first["commands"]["check"][1:4] == ["pushd", "transfer", "check"]
    assert "--final-review" in pushd_first["commands"]["check"]
    assert pushd_first["commands"]["cleanup"][1:4] == ["pushd", "queue", "remove"]
    assert diffd_details["cases"][-1]["commands"]["setup"][1:4] == ["diffd", "remote-change", "add"]
    assert diffd_details["cases"][-1]["commands"]["check"][1:4] == ["diffd", "transfer", "check"]
    assert "--final-review" in diffd_details["cases"][-1]["commands"]["check"]
    assert "running rclone copyto" in pushd_details["blocked operations"]
    assert "running rclone copyto" in diffd_details["blocked operations"]
    assert not (state_dir / "pushd").exists()
    assert not (state_dir / "diffd").exists()
def test_transfer_check_is_read_only_and_reports_gate_prerequisites(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
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
    shadow_report = tmp_path / "shadow-validation.json"
    shadow_workspace = tmp_path / "pcloud-shadow-validation-case" / "workspace"
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

    pushd = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "transfer",
            "check",
            "--report-path",
            str(shadow_report),
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
            "check",
            "--report-path",
            str(shadow_report),
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
    assert pushd_payload["status"] == "warning"
    assert diffd_payload["status"] == "warning"
    assert pushd_payload["details"]["implementation status"] == "read-only checklist; rclone is not executed"
    assert diffd_payload["details"]["implementation status"] == "read-only checklist; rclone is not executed"
    assert pushd_payload["details"]["real transfer gate status"] == "closed"
    assert diffd_payload["details"]["real transfer gate status"] == "closed"
    assert pushd_payload["details"]["real execution can run"] == "no"
    assert diffd_payload["details"]["real execution can run"] == "no"
    assert pushd_payload["details"]["real execution readiness"] == "blocked-final-review"
    assert diffd_payload["details"]["real execution readiness"] == "blocked-final-review"
    assert pushd_payload["details"]["state writes"] == "none"
    assert diffd_payload["details"]["state writes"] == "none"
    assert pushd_payload["details"]["sample path"] == "Documents/pushd-transfer-gate-sample.txt"
    assert diffd_payload["details"]["sample path"] == "Documents/diffd-transfer-gate-sample.txt"
    assert pushd_payload["details"]["sample path status"] == "ready"
    assert diffd_payload["details"]["sample path status"] == "ready"
    assert pushd_payload["details"]["first planned transfer status"] == "ready"
    assert diffd_payload["details"]["first planned transfer status"] == "ready"
    assert pushd_payload["details"]["expected after sample setup"]["first planned transfer status"] == "ready"
    assert diffd_payload["details"]["expected after sample setup"]["first planned transfer status"] == "ready"
    assert pushd_payload["details"]["preflight checks"][0]["status"] == "ok"
    assert diffd_payload["details"]["preflight checks"][0]["status"] == "ok"
    assert pushd_payload["details"]["first planned transfer"]["path"] == "Documents/upload.pdf"
    assert diffd_payload["details"]["first planned transfer"]["path"] == "Documents/download.pdf"
    assert pushd_payload["details"]["dev-state sample setup command"][1:4] == ["pushd", "queue", "add"]
    assert diffd_payload["details"]["dev-state sample setup command"][1:4] == [
        "diffd",
        "remote-change",
        "add",
    ]
    assert len(pushd_payload["details"]["review command sequence"]) == 4
    assert len(diffd_payload["details"]["review command sequence"]) == 4
    assert pushd_payload["details"]["review command sequence"][2][1:4] == ["pushd", "transfer", "check"]
    assert diffd_payload["details"]["review command sequence"][2][1:4] == ["diffd", "transfer", "check"]
    assert pushd_payload["details"]["review command sequence"][3][1:4] == ["pushd", "queue", "remove"]
    assert diffd_payload["details"]["review command sequence"][3][1:4] == [
        "diffd",
        "remote-change",
        "remove",
    ]
    assert "PCLOUD_TOOLS_REAL_TRANSFER_GATE" in [issue["key"] for issue in pushd_payload["issues"]]
    assert "PCLOUD_TOOLS_REAL_TRANSFER_GATE" in [issue["key"] for issue in diffd_payload["issues"]]
    assert not (pushd_dir / "last-transfer.json").exists()
    assert not (diffd_dir / "last-transfer.json").exists()
def test_transfer_check_rejects_incomplete_shadow_report_without_state_writes(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    pushd_dir = state_dir / "pushd"
    pushd_dir.mkdir(parents=True)
    _write_workspace_file(env, "Documents/upload.pdf", "upload\n")
    (pushd_dir / "queue.json").write_text(
        json.dumps([{"path": "Documents/upload.pdf", "action": "upload", "reason": "test"}])
    )
    shadow_report = tmp_path / "shadow-validation-incomplete.json"
    shadow_report.write_text(
        json.dumps(
            {
                "status": "ok",
                "workspace": str(tmp_path / "workspace"),
                "state_dir": str(tmp_path / "state"),
                "checks": [
                    {"name": "temporary workspace guard", "status": "ok"},
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
            "check",
            "--report-path",
            str(shadow_report),
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    shadow_check = payload["details"]["preflight checks"][0]

    assert result.returncode == 0
    assert payload["status"] == "warning"
    assert payload["details"]["first planned transfer status"] == "ready"
    assert payload["details"]["review command sequence"][2][1:4] == ["pushd", "transfer", "check"]
    assert payload["details"]["review command sequence"][3][1:4] == ["pushd", "queue", "remove"]
    assert shadow_check["status"] == "not-ok"
    assert "missing required checks" in shadow_check["detail"]
    assert "temp workspace ok=False" in shadow_check["detail"]
    assert "PCLOUD_TOOLS_REAL_TRANSFER_SHADOW_REPORT" in [issue["key"] for issue in payload["issues"]]
    assert not (pushd_dir / "last-transfer.json").exists()
def test_transfer_check_default_sample_uses_allowlist_root(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    (workspace / ".pcloud-sync-allowlist").write_text("dev-fixtures/Documents/\n")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "transfer",
            "check",
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
    assert payload["details"]["sample path"] == "dev-fixtures/Documents/pushd-transfer-gate-sample.txt"
    assert payload["details"]["sample path status"] == "ready"
    assert payload["details"]["dev-state sample setup command"][4] == (
        "dev-fixtures/Documents/pushd-transfer-gate-sample.txt"
    )
def test_transfer_check_accepts_operator_confirmations_without_opening_gate(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    pushd_dir = state_dir / "pushd"
    diffd_dir = state_dir / "diffd"
    pushd_dir.mkdir(parents=True)
    diffd_dir.mkdir(parents=True)
    _write_workspace_file(env, "Documents/first-upload.txt", "upload\n")
    (pushd_dir / "queue.json").write_text(
        json.dumps([{"path": "Documents/first-upload.txt", "action": "upload", "reason": "test"}])
    )
    (diffd_dir / "remote-changes.json").write_text(
        json.dumps([{"path": "Documents/first-download.txt", "action": "download", "reason": "test"}])
    )

    pushd = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "transfer",
            "check",
            "--confirm-path",
            "Documents/first-upload.txt",
            "--confirm-direction",
            "upload",
            "--consume-policy",
            "remove-on-success-retain-on-failure",
            "--timeout-policy",
            "reuse-fake-rclone-cleanup",
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
            "check",
            "--confirm-path",
            "Documents/first-download.txt",
            "--confirm-direction",
            "download",
            "--consume-policy",
            "remove-on-success-retain-on-failure",
            "--timeout-policy",
            "reuse-fake-rclone-cleanup",
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
    pushd_checks = {check["name"]: check for check in pushd_payload["details"]["preflight checks"]}
    diffd_checks = {check["name"]: check for check in diffd_payload["details"]["preflight checks"]}

    assert pushd.returncode == 0
    assert diffd.returncode == 0
    assert pushd_payload["details"]["real transfer gate status"] == "closed"
    assert diffd_payload["details"]["real transfer gate status"] == "closed"
    assert pushd_payload["details"]["state writes"] == "none"
    assert diffd_payload["details"]["state writes"] == "none"
    assert pushd_payload["details"]["operator target confirmation status"] == "ok"
    assert diffd_payload["details"]["operator target confirmation status"] == "ok"
    assert pushd_payload["details"]["consume policy status"] == "ok"
    assert diffd_payload["details"]["consume policy status"] == "ok"
    assert pushd_payload["details"]["timeout policy status"] == "ok"
    assert diffd_payload["details"]["timeout policy status"] == "ok"
    assert pushd_checks["first real run target"]["status"] == "ok"
    assert diffd_checks["first real run target"]["status"] == "ok"
    assert pushd_checks["queue/change consumption policy"]["status"] == "ok"
    assert diffd_checks["queue/change consumption policy"]["status"] == "ok"
    assert pushd_checks["timeout/process cleanup policy"]["status"] == "ok"
    assert diffd_checks["timeout/process cleanup policy"]["status"] == "ok"
    assert "PCLOUD_TOOLS_PUSHD_REAL_TRANSFER_TARGET_CONFIRMATION" not in [
        issue["key"] for issue in pushd_payload["issues"]
    ]
    assert "PCLOUD_TOOLS_DIFFD_REAL_TRANSFER_TARGET_CONFIRMATION" not in [
        issue["key"] for issue in diffd_payload["issues"]
    ]
def test_transfer_check_final_review_shows_dry_run_commands_without_opening_gate(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    pushd_dir = state_dir / "pushd"
    diffd_dir = state_dir / "diffd"
    pushd_dir.mkdir(parents=True)
    diffd_dir.mkdir(parents=True)
    _write_workspace_file(env, "Documents/final-upload.txt", "upload\n")
    (pushd_dir / "queue.json").write_text(
        json.dumps([{"path": "Documents/final-upload.txt", "action": "upload", "reason": "test"}])
    )
    (diffd_dir / "remote-changes.json").write_text(
        json.dumps([{"path": "Documents/final-download.txt", "action": "download", "reason": "test"}])
    )
    shadow_workspace = tmp_path / "pcloud-shadow-validation-final" / "workspace"
    shadow_report = tmp_path / "shadow-validation-final.json"
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

    pushd = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "transfer",
            "check",
            "--report-path",
            str(shadow_report),
            "--confirm-path",
            "Documents/final-upload.txt",
            "--confirm-direction",
            "upload",
            "--consume-policy",
            "remove-on-success-retain-on-failure",
            "--timeout-policy",
            "reuse-fake-rclone-cleanup",
            "--final-review",
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
            "check",
            "--report-path",
            str(shadow_report),
            "--confirm-path",
            "Documents/final-download.txt",
            "--confirm-direction",
            "download",
            "--consume-policy",
            "remove-on-success-retain-on-failure",
            "--timeout-policy",
            "reuse-fake-rclone-cleanup",
            "--final-review",
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
    assert pushd_payload["details"]["final review requested"] is True
    assert diffd_payload["details"]["final review requested"] is True
    assert pushd_payload["details"]["final review status"] == "ready"
    assert diffd_payload["details"]["final review status"] == "ready"
    assert pushd_payload["details"]["final review blockers"] == []
    assert diffd_payload["details"]["final review blockers"] == []
    assert pushd_payload["details"]["real transfer gate opening status"] == "ready-for-separate-gate"
    assert diffd_payload["details"]["real transfer gate opening status"] == "ready-for-separate-gate"
    assert "real transfer execution is still unavailable" in pushd_payload["details"][
        "real transfer gate opening note"
    ]
    assert any(
        "real execute gate must be added separately" in item
        for item in pushd_payload["details"]["separate real gate next checks"]
    )
    assert pushd_payload["details"]["dry-run transfer command"][-1] == "--dry-run"
    assert diffd_payload["details"]["dry-run transfer command"][-1] == "--dry-run"
    assert pushd_payload["details"]["real transfer command"][-2:] == [
        str(Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"]) / "Documents" / "final-upload.txt"),
        "pcloud:core/Documents/final-upload.txt",
    ]
    assert diffd_payload["details"]["real transfer command"][-2:] == [
        "pcloud:core/Documents/final-download.txt",
        str(Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"]) / "Documents" / "final-download.txt"),
    ]
    assert pushd_payload["details"]["real transfer gate status"] == "closed"
    assert diffd_payload["details"]["real transfer gate status"] == "closed"
    assert pushd_payload["details"]["state writes"] == "none"
    assert diffd_payload["details"]["state writes"] == "none"
def test_transfer_check_final_review_blocked_human_output_is_actionable(tmp_path: Path) -> None:
    env = _base_env(tmp_path)

    human = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "transfer",
            "check",
            "--final-review",
        ],
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
            "check",
            "--final-review",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(structured)
    blocker_names = {
        item["name"] for item in payload["details"]["final review blocker details"]
    }

    assert human.returncode == 0
    assert structured.returncode == 0
    assert "final review: blocked" in human.stdout
    assert "blocked checks:" in human.stdout
    assert "- saved shadow validation report: pending" in human.stdout
    assert "- first real run target: pending" in human.stdout
    assert "- queue/change consumption policy: pending" in human.stdout
    assert "- timeout/process cleanup policy: pending" in human.stdout
    assert "- planned transfer count: not-ok" in human.stdout
    assert "dry-run note: blocked; fix the listed checks" in human.stdout
    assert "dry-run command:" not in human.stdout
    assert "real command:" not in human.stdout

    assert payload["details"]["final review status"] == "blocked"
    assert payload["details"]["dry-run display status"] == "blocked"
    assert payload["details"]["real transfer gate opening status"] == "blocked"
    assert payload["details"]["separate real gate next checks"] == []
    assert payload["details"]["dry-run transfer command"] == []
    assert payload["details"]["real transfer command"] == []
    assert "saved shadow validation report" in blocker_names
    assert "first real run target" in blocker_names
    assert "planned transfer count" in blocker_names
    assert payload["details"]["real transfer gate status"] == "closed"
    assert payload["details"]["state writes"] == "none"
def test_transfer_real_gate_is_read_only_scaffold(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    pushd_dir = state_dir / "pushd"
    pushd_dir.mkdir(parents=True)
    _write_workspace_file(env, "Documents/real-gate.txt", "upload\n")
    (pushd_dir / "queue.json").write_text(
        json.dumps([{"path": "Documents/real-gate.txt", "action": "upload", "reason": "test"}])
    )
    shadow_workspace = tmp_path / "pcloud-shadow-validation-real-gate" / "workspace"
    shadow_report = tmp_path / "shadow-validation-real-gate.json"
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
            "real-gate",
            "--report-path",
            str(shadow_report),
            "--confirm-path",
            "Documents/real-gate.txt",
            "--confirm-direction",
            "upload",
            "--consume-policy",
            "remove-on-success-retain-on-failure",
            "--timeout-policy",
            "reuse-fake-rclone-cleanup",
            "--operator-reviewed-dry-run",
            "--reviewer-approved-real-command",
            "--reviewer-approved-consume-policy",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    standalone = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from pcloud_tools.cli_service_daemon import main_pushd; "
                "raise SystemExit(main_pushd(['transfer','real-gate','--json']))"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    standalone_payload = _payload(standalone)

    assert result.returncode == 0
    assert standalone.returncode == 0
    assert payload["command"] == "pushd transfer real-gate"
    assert standalone_payload["command"] == "pushd transfer real-gate"
    assert payload["summary"] == "pushd real transfer execution gate is closed"
    assert payload["details"]["implementation status"].startswith("read-only real execution gate scaffold")
    assert payload["details"]["final review status"] == "ready"
    assert payload["details"]["real transfer gate opening status"] == "ready-for-separate-gate"
    assert payload["details"]["real transfer execution gate status"].startswith(
        "closed: requires PCLOUD_TOOLS_REAL_TRANSFER_EXECUTION_GATE="
    )
    assert payload["details"]["future real gate env var"] == "PCLOUD_TOOLS_REAL_TRANSFER_EXECUTION_GATE"
    assert payload["details"]["future real gate accepted value"] == "operator-approved-real-transfer-v1"
    assert payload["details"]["fake-rclone gate reuse"] == "forbidden"
    assert payload["details"]["separate real gate approval status"] == "complete-read-only"
    assert {
        check["status"] for check in payload["details"]["separate real gate approval checks"]
    } == {"ok"}
    assert payload["details"]["operator verification required"] == "not-now"
    assert "actual pCloud/rclone transfer" in payload["details"]["next human check trigger"]
    assert standalone_payload["details"]["operator verification required"] == "no"
    assert payload["details"]["human gate status"] == "required-before-actual-transfer"
    assert "explicit operator run command" in payload["details"]["human gate reason"]
    assert standalone_payload["details"]["human gate status"] == "not-yet"
    assert payload["details"]["real execution readiness"] == "blocked-execution-gate"
    assert payload["details"]["real execution can run"] == "no"
    assert standalone_payload["details"]["real execution readiness"] == "blocked-final-review"
    assert payload["details"]["future real-run policy status"] == "documented-read-only"
    assert "pushd queue record" in payload["details"]["future real-run success policy"]
    assert "retain matching pushd queue record" in payload["details"]["future real-run failure policy"]
    assert payload["details"]["future real-run policy state writes"] == "none"
    assert payload["details"]["state writes"] == "none"
    assert "PCLOUD_TOOLS_REAL_TRANSFER_EXECUTION_GATE" in [issue["key"] for issue in payload["issues"]]
    assert not (pushd_dir / "last-transfer.json").exists()
def test_transfer_automation_gate_is_read_only_and_blocks_public_executor(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    pushd_dir = state_dir / "pushd"
    diffd_dir = state_dir / "diffd"
    pushd_dir.mkdir(parents=True)
    diffd_dir.mkdir(parents=True)
    _write_workspace_file(env, "Documents/auto-upload.txt", "upload\n")
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
    shadow_workspace = tmp_path / "pcloud-shadow-validation-automation-gate" / "workspace"
    shadow_report = tmp_path / "shadow-validation-automation-gate.json"
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

    pushd = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "transfer",
            "automation-gate",
            "--report-path",
            str(shadow_report),
            "--confirm-path",
            "Documents/auto-upload.txt",
            "--confirm-direction",
            "upload",
            "--consume-policy",
            "remove-on-success-retain-on-failure",
            "--timeout-policy",
            "reuse-fake-rclone-cleanup",
            "--operator-reviewed-dry-run",
            "--reviewer-approved-real-command",
            "--reviewer-approved-consume-policy",
            "--operator-reviewed-real-transfer-gate",
            "--reviewer-approved-automation-command",
            "--reviewer-approved-launchd-policy",
            "--reviewer-approved-rollback-policy",
            "--start-interval-seconds",
            "45",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env
        | {
            "PCLOUD_TOOLS_REAL_TRANSFER_AUTOMATION_GATE": "operator-approved-real-transfer-automation-v1"
        },
    )
    diffd = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "diffd",
            "transfer",
            "automation-gate",
            "--report-path",
            str(shadow_report),
            "--confirm-path",
            "Documents/auto-download.txt",
            "--confirm-direction",
            "download",
            "--consume-policy",
            "remove-on-success-retain-on-failure",
            "--timeout-policy",
            "reuse-fake-rclone-cleanup",
            "--operator-reviewed-dry-run",
            "--reviewer-approved-real-command",
            "--reviewer-approved-consume-policy",
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
    assert pushd_payload["summary"] == "pushd real transfer automation gate is closed"
    assert diffd_payload["summary"] == "diffd real transfer automation gate is closed"
    assert pushd_payload["details"]["automation gate status"] == "closed"
    assert pushd_payload["details"]["automation can run"] == "no"
    assert pushd_payload["details"]["automation command status"] == "implemented-gated"
    assert pushd_payload["details"]["automation gate env provided"] == "yes"
    assert pushd_payload["details"]["automation gate env honored"] == "no"
    assert pushd_payload["details"]["planned public executor service label"] == "com.takafumi.pcloud-pushd-executor"
    assert pushd_payload["details"]["planned public executor StartInterval"] == 45
    assert pushd_payload["details"]["future automation command"][1:4] == [
        "pushd",
        "transfer",
        "automation-run",
    ]
    assert pushd_payload["details"]["state writes"] == "none"
    assert pushd_payload["details"]["launchctl execution"] == "no"
    assert pushd_payload["details"]["public plist writes"] == "no"
    assert pushd_payload["details"]["automatic real transfer execution"] == "no"
    assert pushd_payload["details"]["automation approval status"] == "ready-for-launchd-review"
    assert any(
        check["name"] == "automation command implementation" and check["status"] == "ok"
        for check in pushd_payload["details"]["automation approval checks"]
    )
    assert "pushd.transfer.automation-gate" in [
        action["id"] for action in pushd_payload["actions"]
    ]
    assert diffd_payload["details"]["planned public executor service label"] == "com.takafumi.pcloud-diffd-executor"
    assert diffd_payload["details"]["automation gate env provided"] == "no"
    assert not (pushd_dir / "last-transfer.json").exists()
    assert not (diffd_dir / "last-transfer.json").exists()
def test_transfer_automation_gate_accepts_prior_successful_real_run_when_queue_is_empty(
    tmp_path: Path,
) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    pushd_dir = state_dir / "pushd"
    pushd_dir.mkdir(parents=True)
    _write_workspace_file(env, "Documents/selected-real-run.txt", "selected\n")
    _write_workspace_file(env, "Documents/retained-real-run.txt", "retained\n")
    (pushd_dir / "queue.json").write_text("[]")
    (pushd_dir / "last-transfer.json").write_text(
        json.dumps(
            {
                "service": "pushd",
                "mode": "real-rclone-transfer",
                "generated_at": "2026-05-07T00:00:00+00:00",
                "planned_transfer_commands": [
                    {
                        "command": ["rclone", "copyto", "local", "remote"],
                        "direction": "upload",
                        "path": "Documents/validated-upload.txt",
                        "reason": "test",
                    }
                ],
                "results": [
                    {
                        "command": ["rclone", "copyto", "local", "remote"],
                        "direction": "upload",
                        "path": "Documents/validated-upload.txt",
                        "reason": "test",
                        "returncode": 0,
                        "stdout": "",
                        "stderr": "",
                        "timed_out": False,
                    }
                ],
            }
        )
    )
    shadow_workspace = tmp_path / "pcloud-shadow-validation-prior-real-run" / "workspace"
    shadow_report = tmp_path / "shadow-validation-prior-real-run.json"
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
            "automation-gate",
            "--report-path",
            str(shadow_report),
            "--consume-policy",
            "remove-on-success-retain-on-failure",
            "--timeout-policy",
            "reuse-fake-rclone-cleanup",
            "--reviewer-approved-consume-policy",
            "--operator-reviewed-real-transfer-gate",
            "--reviewer-approved-automation-command",
            "--reviewer-approved-launchd-policy",
            "--reviewer-approved-rollback-policy",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env
        | {
            "PCLOUD_TOOLS_REAL_TRANSFER_AUTOMATION_GATE": "operator-approved-real-transfer-automation-v1"
        },
    )

    payload = _payload(result)

    assert result.returncode == 0
    assert payload["details"]["planned uploads"] == 0
    assert payload["details"]["prior real transfer validation status"] == "ok"
    assert payload["details"]["real transfer approvals source"] == "prior successful real-run"
    assert payload["details"]["automation approval status"] == "ready-for-launchd-review"
    assert any(
        check["name"] == "prior real-transfer validation" and check["status"] == "ok"
        for check in payload["details"]["automation approval checks"]
    )
    assert payload["details"]["state writes"] == "none"
    assert "PCLOUD_TOOLS_REAL_TRANSFER_TARGET" in [issue["key"] for issue in payload["issues"]]
def test_transfer_automation_gate_accepts_prior_successful_automation_run_when_queue_is_empty(
    tmp_path: Path,
) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    diffd_dir = state_dir / "diffd"
    diffd_dir.mkdir(parents=True)
    (diffd_dir / "remote-changes.json").write_text("[]")
    (diffd_dir / "last-transfer.json").write_text(
        json.dumps(
            {
                "service": "diffd",
                "mode": "real-rclone-automation-transfer",
                "generated_at": "2026-05-07T00:00:00+00:00",
                "planned_transfer_commands": [
                    {
                        "command": ["rclone", "copyto", "remote", "local"],
                        "direction": "download",
                        "path": "Documents/validated-download.txt",
                        "reason": "test",
                    }
                ],
                "results": [
                    {
                        "command": ["rclone", "copyto", "remote", "local"],
                        "direction": "download",
                        "path": "Documents/validated-download.txt",
                        "reason": "test",
                        "returncode": 0,
                        "stdout": "",
                        "stderr": "",
                        "timed_out": False,
                    }
                ],
            }
        )
    )
    shadow_workspace = tmp_path / "pcloud-shadow-validation-prior-automation-run" / "workspace"
    shadow_report = tmp_path / "shadow-validation-prior-automation-run.json"
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
            "diffd",
            "transfer",
            "automation-gate",
            "--report-path",
            str(shadow_report),
            "--consume-policy",
            "remove-on-success-retain-on-failure",
            "--timeout-policy",
            "reuse-fake-rclone-cleanup",
            "--reviewer-approved-consume-policy",
            "--operator-reviewed-real-transfer-gate",
            "--reviewer-approved-automation-command",
            "--reviewer-approved-launchd-policy",
            "--reviewer-approved-rollback-policy",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env
        | {
            "PCLOUD_TOOLS_REAL_TRANSFER_AUTOMATION_GATE": "operator-approved-real-transfer-automation-v1"
        },
    )

    payload = _payload(result)

    assert result.returncode == 0
    assert payload["details"]["planned downloads"] == 0
    assert payload["details"]["prior real transfer validation status"] == "ok"
    assert payload["details"]["prior real transfer mode"] == "real-rclone-automation-transfer"
    assert payload["details"]["automation approval status"] == "ready-for-launchd-review"
    assert payload["details"]["state writes"] == "none"
    assert "PCLOUD_TOOLS_REAL_TRANSFER_TARGET" in [issue["key"] for issue in payload["issues"]]
def test_transfer_check_warns_on_mismatched_operator_confirmation(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    pushd_dir = state_dir / "pushd"
    pushd_dir.mkdir(parents=True)
    (pushd_dir / "queue.json").write_text(
        json.dumps([{"path": "Documents/planned.txt", "action": "upload", "reason": "test"}])
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "transfer",
            "check",
            "--confirm-path",
            "Documents/other.txt",
            "--confirm-direction",
            "upload",
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
    assert payload["details"]["real transfer gate status"] == "closed"
    assert payload["details"]["operator target confirmation status"] == "not-ok"
    assert checks["first real run target"]["status"] == "not-ok"
    assert "does not match any planned transfer" in checks["first real run target"]["detail"]
    assert "PCLOUD_TOOLS_PUSHD_REAL_TRANSFER_TARGET_CONFIRMATION" in [
        issue["key"] for issue in payload["issues"]
    ]
def test_transfer_check_custom_sample_path_must_be_allowlisted(tmp_path: Path) -> None:
    env = _base_env(tmp_path)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "transfer",
            "check",
            "--sample-path",
            "../dev/not-allowed.py",
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
    assert payload["status"] == "warning"
    assert payload["details"]["sample path"] == ""
    assert payload["details"]["sample path status"] == "not planned"
    assert payload["details"]["expected after sample setup"]["planned uploads"] == 0
    assert "PCLOUD_TOOLS_REAL_TRANSFER_SAMPLE_PATH" in [issue["key"] for issue in payload["issues"]]
    assert "PCLOUD_TOOLS_REAL_TRANSFER_GATE" in [issue["key"] for issue in payload["issues"]]
def test_transfer_check_human_output_is_concise_but_json_stays_detailed(tmp_path: Path) -> None:
    env = _base_env(tmp_path)

    human = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "transfer",
            "check",
            "--sample-path",
            "Documents/custom-sample.txt",
        ],
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
            "check",
            "--sample-path",
            "Documents/custom-sample.txt",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(structured)

    assert human.returncode == 0
    assert "pushd transfer check: warning" in human.stdout
    assert "gate: closed" in human.stdout
    assert "state writes: none" in human.stdout
    assert "sample: Documents/custom-sample.txt (ready)" in human.stdout
    assert "first target: missing" in human.stdout
    assert "shadow report: pending" in human.stdout
    assert "review commands:" in human.stdout
    assert "- setup sample:" in human.stdout
    assert "- preview transfer:" in human.stdout
    assert "- check again:" in human.stdout
    assert "pushd transfer check --sample-path Documents/custom-sample.txt --json" in human.stdout
    assert "- cleanup sample:" in human.stdout
    assert "preflight checks:" not in human.stdout
    assert "planned transfer commands:" not in human.stdout
    assert "review command sequence:" not in human.stdout
    assert "core dir:" not in human.stdout

    assert structured.returncode == 0
    assert payload["details"]["preflight checks"][0]["status"] == "pending"
    assert len(payload["details"]["review command sequence"]) == 4
    assert payload["details"]["review command sequence"][2][4:6] == [
        "--sample-path",
        "Documents/custom-sample.txt",
    ]
    assert payload["details"]["planned transfer commands"] == []
def test_transfer_preview_and_check_action_ids_dispatch(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    pushd = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "action", "pushd.transfer.preview"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    diffd = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "action", "diffd.transfer.preview"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    pushd_check = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "action", "pushd.transfer.check"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    pushd_launchd_gate = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "action", "pushd.launchd.gate"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    diffd_launchd_gate = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "action", "diffd.launchd.gate"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    pushd_matrix = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "action", "pushd.transfer.validation-matrix"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    diffd_matrix = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "action", "diffd.transfer.validation-matrix"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    pushd_fswatch_gate = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "action", "pushd.fswatch.resident-gate"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    diffd_api_poll_gate = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "action", "diffd.api-poll.long-poll-gate"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    pushd_real_gate = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "action", "pushd.transfer.real-gate"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    diffd_real_gate = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "action", "diffd.transfer.real-gate"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    pushd_consume = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "action", "pushd.transfer.consume.preview"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    diffd_consume = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "action", "diffd.transfer.consume.preview"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    diffd_check = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "action", "diffd.transfer.check"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    assert pushd.returncode == 0
    assert "pushd upload transfer preview is ready" in pushd.stdout
    assert "real execution can run: no" in pushd.stdout
    assert diffd.returncode == 0
    assert "diffd download transfer preview is ready" in diffd.stdout
    assert pushd_check.returncode == 0
    assert "pushd real transfer gate checklist is not open" in pushd_check.stdout
    assert pushd_launchd_gate.returncode == 0
    assert "pushd launchd gate is closed" in pushd_launchd_gate.stdout
    assert diffd_launchd_gate.returncode == 0
    assert "diffd launchd gate is closed" in diffd_launchd_gate.stdout
    assert pushd_matrix.returncode == 0
    assert "pushd real transfer validation matrix is ready" in pushd_matrix.stdout
    assert diffd_matrix.returncode == 0
    assert "diffd real transfer validation matrix is ready" in diffd_matrix.stdout
    assert pushd_fswatch_gate.returncode == 0
    assert "pushd fswatch resident gate is closed" in pushd_fswatch_gate.stdout
    assert diffd_api_poll_gate.returncode == 0
    assert "diffd pCloud API long-poll gate is closed" in diffd_api_poll_gate.stdout
    assert diffd_check.returncode == 0
    assert "diffd real transfer gate checklist is not open" in diffd_check.stdout
    assert pushd_real_gate.returncode == 0
    assert "pushd real transfer execution gate is closed" in pushd_real_gate.stdout
    assert diffd_real_gate.returncode == 0
    assert "diffd real transfer execution gate is closed" in diffd_real_gate.stdout
    assert pushd_consume.returncode == 0
    assert "pushd transfer consume policy preview is ready" in pushd_consume.stdout
    assert diffd_consume.returncode == 0
    assert "diffd transfer consume policy preview is ready" in diffd_consume.stdout
