from __future__ import annotations

from conftest import *


def test_pushd_preview_builds_allowlisted_queue_plan(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    pushd_dir = state_dir / "pushd"
    pushd_dir.mkdir(parents=True)
    documents_dir = workspace / "Documents"
    documents_dir.mkdir(parents=True)
    (documents_dir / "report.pdf").write_text("report\n")
    (pushd_dir / "queue.json").write_text(
        json.dumps(
            [
                {"path": "Documents/report.pdf", "action": "upload"},
                {"path": "Documents/.DS_Store", "action": "upload"},
                {"path": "Documents/.temporary-upload", "action": "upload"},
                {"path": "private/secret.txt", "action": "upload"},
                {"path": "../bad", "action": "upload"},
            ]
        )
    )

    result = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "preview", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    assert result.returncode == 0
    assert payload["status"] in {"ok", "warning"}
    assert payload["details"]["pending queue items"] == 5
    assert payload["details"]["planned uploads"] == 1
    assert payload["details"]["excluded queue items"] == 3
    assert payload["details"]["invalid queue items"] == 1
    assert payload["details"]["plan summary"] == "upload: 1; missing-local: 0; excluded: 3; invalid: 1"
    assert payload["details"]["planned upload records"][0]["path"] == "Documents/report.pdf"
    assert payload["details"]["excluded queue records"][1]["reason"] == "manager ignore rule"
def test_pushd_preview_uses_manager_ignore_exceptions_for_dot_samples(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = _use_default_dev_state_dir(env)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    (workspace / ".pcloudmanagerignore").write_text(
        "# ! lines are exception allow rules.\n"
        ".*\n"
        "**/.*\n"
        "!.env.sample\n"
        "!**/.env.sample\n"
    )
    (workspace / "Documents").mkdir()
    (workspace / "Documents" / ".env").write_text("secret=local\n")
    (workspace / "Documents" / ".env.sample").write_text("secret=\n")
    pushd_dir = state_dir / "pushd"
    pushd_dir.mkdir(parents=True)
    (pushd_dir / "queue.json").write_text(
        json.dumps(
            [
                {"path": "Documents/.env", "action": "upload"},
                {"path": "Documents/.env.sample", "action": "upload"},
            ]
        )
    )

    result = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "preview", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    assert result.returncode == 0
    assert payload["details"]["planned uploads"] == 1
    assert payload["details"]["excluded queue items"] == 1
    assert payload["details"]["planned upload records"][0]["path"] == "Documents/.env.sample"
    assert payload["details"]["excluded queue records"][0]["path"] == "Documents/.env"
    assert payload["details"]["excluded queue records"][0]["reason"] == "manager ignore rule"
def test_pushd_queue_prune_excluded_is_gated_and_scoped(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = _use_default_dev_state_dir(env)
    pushd_dir = state_dir / "pushd"
    pushd_dir.mkdir(parents=True)
    queue_file = pushd_dir / "queue.json"
    queue_file.write_text(
        json.dumps(
            [
                {"path": "Documents/report.pdf", "action": "upload"},
                {"path": "Documents/.temporary-upload", "action": "upload"},
                {"path": "private/secret.txt", "action": "upload"},
            ]
        )
    )

    preview = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "queue", "prune-excluded", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    executed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "queue",
            "prune-excluded",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    preview_payload = _payload(preview)
    executed_payload = _payload(executed)

    assert preview.returncode == 0
    assert preview_payload["summary"] == "pushd queue prune-excluded preview is ready"
    assert preview_payload["details"]["queue items removed"] == 2
    assert preview_payload["details"]["state writes"] == "none"
    assert executed.returncode == 0
    assert executed_payload["summary"] == "pushd queue excluded records pruned"
    assert executed_payload["details"]["queue items removed"] == 2
    assert executed_payload["details"]["state writes"] == "pushd queue only"
    assert json.loads(queue_file.read_text()) == [
        {"path": "Documents/report.pdf", "action": "upload"}
    ]
def test_public_pushd_queue_prune_excluded_requires_gate(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    env["PCLOUD_TOOLS_DEV"] = "0"
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    env["PCLOUD_TOOLS_CORE_DIR"] = str(workspace)
    env["PCLOUD_TOOLS_ALLOWLIST_FILE"] = str(workspace / ".pcloud-sync-allowlist")
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    pushd_dir = state_dir / "pushd"
    pushd_dir.mkdir(parents=True)
    queue_file = pushd_dir / "queue.json"
    queue_file.write_text(
        json.dumps(
            [
                {"path": "Documents/report.pdf", "action": "upload"},
                {"path": "Documents/.temporary-upload", "action": "upload"},
            ]
        )
    )

    refused = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "queue",
            "prune-excluded",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    refused_payload = _payload(refused)

    assert refused.returncode == 1
    assert refused_payload["summary"] == "pushd queue cannot be updated until issues are resolved"
    assert refused_payload["details"]["state writes"] == "none"
    assert json.loads(queue_file.read_text())[1]["path"] == "Documents/.temporary-upload"

    approved = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "queue",
            "prune-excluded",
            "--reviewer-approved-excluded-record-cleanup",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env
        | {
            "PCLOUD_TOOLS_PUSHD_QUEUE_PRUNE_EXCLUDED_GATE": (
                "operator-approved-pushd-queue-prune-excluded-v1"
            )
        },
    )

    approved_payload = _payload(approved)

    assert approved.returncode == 0
    assert approved_payload["summary"] == "pushd queue excluded records pruned"
    assert approved_payload["details"]["prune gate env honored"] == "yes"
    assert json.loads(queue_file.read_text()) == [{"path": "Documents/report.pdf", "action": "upload"}]
def test_pushd_fswatch_fixture_preview_is_read_only(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    _write_workspace_file(env, "Documents/report.pdf", "report\n")
    fixture = tmp_path / "fswatch-events.txt"
    fixture.write_text(
        "\n".join(
            [
                "Documents/report.pdf\tCreated Updated",
                "Documents/.DS_Store\tUpdated",
                "Documents/.temporary-upload\tUpdated",
                "private/secret.txt\tCreated",
                "../bad\tUpdated",
            ]
        )
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "fswatch",
            "preview",
            "--fixture",
            str(fixture),
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
    assert payload["summary"] == "pushd fswatch fixture preview is ready"
    assert payload["details"]["implementation status"] == "fixture parser only; fswatch process is not started"
    assert payload["details"]["gate status"] == "closed"
    assert payload["details"]["parsed fswatch events"] == 4
    assert payload["details"]["invalid fswatch events"] == 1
    assert payload["details"]["planned uploads"] == 1
    assert payload["details"]["excluded queue items"] == 3
    assert payload["details"]["invalid queue items"] == 1
    assert payload["details"]["planned upload records"][0]["reason"] == "fswatch:Created,Updated"
    assert not (state_dir / "pushd").exists()
def test_pushd_fswatch_delete_and_rename_events_stay_manual_review_actions(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    fixture = tmp_path / "fswatch-manual-review-events.txt"
    fixture.write_text(
        "\n".join(
            [
                "Documents/removed.pdf\tRemoved",
                "Documents/renamed.pdf\tRenamed",
            ]
        )
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "fswatch",
            "preview",
            "--fixture",
            str(fixture),
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    records = payload["details"]["planned upload records"]

    assert result.returncode == 0
    assert [record["action"] for record in records] == ["delete", "rename"]
    assert [record["reason"] for record in records] == ["fswatch:Removed", "fswatch:Renamed"]
def test_pushd_and_diffd_policy_reports_are_read_only(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])

    pushd_result = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "policy", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    diffd_result = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "diffd", "policy", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    pushd_payload = _payload(pushd_result)
    diffd_payload = _payload(diffd_result)

    assert pushd_result.returncode == 0
    assert diffd_result.returncode == 0
    assert pushd_payload["details"]["daemonization policy status"] == "documented-read-only"
    assert diffd_payload["details"]["daemonization policy status"] == "documented-read-only"
    assert pushd_payload["details"]["state writes"] == "none"
    assert diffd_payload["details"]["state writes"] == "none"
    assert "automatic upload/download transfer execution" in pushd_payload["details"]["blocked operations"]
    assert "automatic upload/download transfer execution" in diffd_payload["details"]["blocked operations"]
    assert "pushd.policy" in [action["id"] for action in pushd_payload["actions"]]
    assert "diffd.policy" in [action["id"] for action in diffd_payload["actions"]]
    assert not (state_dir / "pushd").exists()
    assert not (state_dir / "diffd").exists()
def test_pushd_fswatch_probe_is_preview_only(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])

    result = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "fswatch", "probe", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    assert result.returncode == 0
    assert payload["summary"] == "pushd fswatch one-shot probe preview is ready"
    assert payload["details"]["implementation status"] == "probe preview only; fswatch process is not started"
    assert payload["details"]["gate status"] == "closed"
    assert payload["details"]["state writes"] == "none"
    assert payload["details"]["fswatch command"][-1] == env["PCLOUD_TOOLS_WORKSPACE_ROOT"]
    assert "--one-event" in payload["details"]["fswatch command"]
    assert not (state_dir / "pushd").exists()
def test_pushd_fswatch_resident_gate_is_read_only_checklist(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fswatch = bin_dir / "fswatch"
    fswatch.write_text("#!/bin/sh\nexit 0\n")
    fswatch.chmod(0o755)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    shadow_workspace = tmp_path / "pcloud-shadow-validation-fswatch" / "workspace"
    shadow_report = tmp_path / "shadow-validation-fswatch.json"
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
            "fswatch",
            "resident-gate",
            "--report-path",
            str(shadow_report),
            "--operator-reviewed-probe",
            "--reviewer-approved-queue-policy",
            "--reviewer-approved-process-policy",
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
    assert payload["summary"] == "pushd fswatch resident gate is closed"
    assert payload["details"]["implementation status"].startswith("read-only checklist")
    assert payload["details"]["resident gate status"] == "closed"
    assert payload["details"]["resident can start"] == "no"
    assert payload["details"]["state writes"] == "none"
    assert payload["details"]["fswatch availability"] == "available"
    assert "--one-event" not in payload["details"]["resident command preview"]
    assert payload["details"]["resident command preview"][-1] == env["PCLOUD_TOOLS_WORKSPACE_ROOT"]
    assert payload["details"]["resident approval status"] == "complete-read-only"
    assert payload["details"]["human gate status"] == "required-before-resident-start"
    assert checks["saved shadow validation report"]["status"] == "ok"
    assert checks["fswatch binary"]["status"] == "ok"
    assert checks["operator probe review"]["status"] == "ok"
    assert checks["queue policy approval"]["status"] == "ok"
    assert checks["process lifecycle approval"]["status"] == "ok"
    assert "pushd.fswatch.resident-gate" in [action["id"] for action in payload["actions"]]
    assert not (state_dir / "pushd").exists()
def test_pushd_fswatch_resident_run_refuses_without_execution_gate(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = _use_default_dev_state_dir(env)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fswatch = bin_dir / "fswatch"
    fswatch.write_text("#!/bin/sh\nprintf 'Documents/from-fswatch.txt\\n'\n")
    fswatch.chmod(0o755)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    shadow_report = tmp_path / "shadow-validation-fswatch-run.json"
    shadow_workspace = tmp_path / "pcloud-shadow-validation-fswatch-run" / "workspace"
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
            "fswatch",
            "resident-run",
            "--report-path",
            str(shadow_report),
            "--operator-reviewed-probe",
            "--reviewer-approved-queue-policy",
            "--reviewer-approved-process-policy",
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
    assert payload["summary"] == "pushd fswatch resident execution is gated"
    assert payload["details"]["state writes"] == "none"
    assert payload["details"]["resident can start"] == "no"
    assert "PCLOUD_TOOLS_PUSHD_FSWATCH_RESIDENT_EXECUTION_GATE" in [
        issue["key"] for issue in payload["issues"]
    ]
    assert not (state_dir / "pushd" / "queue.json").exists()
def test_pushd_fswatch_resident_run_executes_fake_fswatch_in_dev_state(tmp_path: Path) -> None:
    env = _base_env(
        tmp_path,
        {"PCLOUD_TOOLS_PUSHD_FSWATCH_RESIDENT_GATE": "operator-approved-fswatch-resident-v1"},
    )
    state_dir = _use_default_dev_state_dir(env)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    (workspace / "Documents").mkdir()
    (workspace / "Documents" / "from-fswatch.txt").write_text("sample\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fswatch = bin_dir / "fswatch"
    fswatch.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$PCLOUD_TOOLS_WORKSPACE_ROOT/Documents/from-fswatch.txt\"\n"
    )
    fswatch.chmod(0o755)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    shadow_report = tmp_path / "shadow-validation-fswatch-run-ok.json"
    shadow_workspace = tmp_path / "pcloud-shadow-validation-fswatch-run-ok" / "workspace"
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
            "fswatch",
            "resident-run",
            "--report-path",
            str(shadow_report),
            "--operator-reviewed-probe",
            "--reviewer-approved-queue-policy",
            "--reviewer-approved-process-policy",
            "--max-events",
            "1",
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
    queue_payload = json.loads((state_dir / "pushd" / "queue.json").read_text())
    resident_state = json.loads((state_dir / "pushd" / "fswatch-resident-last-run.json").read_text())

    assert result.returncode == 0
    assert payload["status"] in {"ok", "warning"}
    assert payload["summary"] == "pushd fswatch resident run completed"
    assert payload["details"]["resident can start"] == "yes"
    assert payload["details"]["state writes"] == "pushd queue and resident run state"
    assert payload["details"]["queue records appended"] == 1
    assert queue_payload == [
        {"path": "Documents/from-fswatch.txt", "action": "upload", "reason": "fswatch"}
    ]
    assert resident_state["appended_records"] == queue_payload
def test_pushd_fswatch_resident_run_excludes_hidden_temp_paths(tmp_path: Path) -> None:
    env = _base_env(
        tmp_path,
        {"PCLOUD_TOOLS_PUSHD_FSWATCH_RESIDENT_GATE": "operator-approved-fswatch-resident-v1"},
    )
    state_dir = _use_default_dev_state_dir(env)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    (workspace / "Documents").mkdir()
    (workspace / "Documents" / ".temporary-upload").write_text("temp\n")
    (workspace / "Documents" / "incoming.jpeg.36b4106a.partial").write_text("partial\n")
    (workspace / "Documents" / "final-upload.txt").write_text("final\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fswatch = bin_dir / "fswatch"
    fswatch.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$PCLOUD_TOOLS_WORKSPACE_ROOT/Documents\"\n"
        "printf '%s\\n' \"$PCLOUD_TOOLS_WORKSPACE_ROOT/Documents/.temporary-upload\"\n"
        "printf '%s\\n' \"$PCLOUD_TOOLS_WORKSPACE_ROOT/Documents/incoming.jpeg.36b4106a.partial\"\n"
        "printf '%s\\n' \"$PCLOUD_TOOLS_WORKSPACE_ROOT/Documents/final-upload.txt\"\n"
    )
    fswatch.chmod(0o755)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    shadow_report = tmp_path / "shadow-validation-fswatch-hidden.json"
    shadow_workspace = tmp_path / "pcloud-shadow-validation-fswatch-hidden" / "workspace"
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
            "fswatch",
            "resident-run",
            "--report-path",
            str(shadow_report),
            "--operator-reviewed-probe",
            "--reviewer-approved-queue-policy",
            "--reviewer-approved-process-policy",
            "--max-events",
            "4",
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
    queue_payload = json.loads((state_dir / "pushd" / "queue.json").read_text())

    assert result.returncode == 0
    assert payload["details"]["queue records appended"] == 1
    assert payload["details"]["excluded events"] == 3
    assert [record["reason"] for record in payload["details"]["excluded event details"]] == [
        "directory upload not supported",
        "manager ignore rule",
        "partial transfer file",
    ]
    assert queue_payload == [
        {"path": "Documents/final-upload.txt", "action": "upload", "reason": "fswatch"}
    ]
def test_pushd_fswatch_resident_run_debounces_same_run_upload_events(tmp_path: Path) -> None:
    env = _base_env(
        tmp_path,
        {"PCLOUD_TOOLS_PUSHD_FSWATCH_RESIDENT_GATE": "operator-approved-fswatch-resident-v1"},
    )
    state_dir = _use_default_dev_state_dir(env)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    (workspace / "Documents").mkdir()
    (workspace / "Documents" / "from-fswatch.txt").write_text("sample\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fswatch = bin_dir / "fswatch"
    fswatch.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$PCLOUD_TOOLS_WORKSPACE_ROOT/Documents/from-fswatch.txt\"\n"
        "printf '%s\\n' \"$PCLOUD_TOOLS_WORKSPACE_ROOT/Documents/from-fswatch.txt\"\n"
    )
    fswatch.chmod(0o755)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    shadow_report = tmp_path / "shadow-validation-fswatch-dedupe.json"
    shadow_workspace = tmp_path / "pcloud-shadow-validation-fswatch-dedupe" / "workspace"
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
            "fswatch",
            "resident-run",
            "--report-path",
            str(shadow_report),
            "--operator-reviewed-probe",
            "--reviewer-approved-queue-policy",
            "--reviewer-approved-process-policy",
            "--max-events",
            "2",
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
    queue_payload = json.loads((state_dir / "pushd" / "queue.json").read_text())
    resident_state = json.loads((state_dir / "pushd" / "fswatch-resident-last-run.json").read_text())

    assert result.returncode == 0
    assert payload["details"]["events processed"] == 2
    assert payload["details"]["queue records appended"] == 1
    assert payload["details"]["duplicate events skipped"] == 0
    assert payload["details"]["debounce events skipped"] == 1
    assert payload["details"]["queue limit skips"] == 0
    assert queue_payload == [
        {"path": "Documents/from-fswatch.txt", "action": "upload", "reason": "fswatch"}
    ]
    assert resident_state["debounce_records"] == [
        {"path": "Documents/from-fswatch.txt", "action": "upload", "reason": "recent resident append"}
    ]
def test_pushd_fswatch_resident_run_skips_existing_duplicate_queue_records(tmp_path: Path) -> None:
    env = _base_env(
        tmp_path,
        {"PCLOUD_TOOLS_PUSHD_FSWATCH_RESIDENT_GATE": "operator-approved-fswatch-resident-v1"},
    )
    state_dir = _use_default_dev_state_dir(env)
    queue_file = state_dir / "pushd" / "queue.json"
    queue_file.parent.mkdir(parents=True)
    queue_file.write_text(
        json.dumps([{"path": "Documents/from-fswatch.txt", "action": "upload", "reason": "seed"}])
    )
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    (workspace / "Documents").mkdir()
    (workspace / "Documents" / "from-fswatch.txt").write_text("sample\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fswatch = bin_dir / "fswatch"
    fswatch.write_text("#!/bin/sh\nprintf '%s\\n' \"$PCLOUD_TOOLS_WORKSPACE_ROOT/Documents/from-fswatch.txt\"\n")
    fswatch.chmod(0o755)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    shadow_report = tmp_path / "shadow-validation-fswatch-duplicate-existing.json"
    shadow_workspace = tmp_path / "pcloud-shadow-validation-fswatch-duplicate-existing" / "workspace"
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
            "fswatch",
            "resident-run",
            "--report-path",
            str(shadow_report),
            "--operator-reviewed-probe",
            "--reviewer-approved-queue-policy",
            "--reviewer-approved-process-policy",
            "--max-events",
            "1",
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
    queue_payload = json.loads(queue_file.read_text())

    assert result.returncode == 0
    assert payload["details"]["queue records appended"] == 0
    assert payload["details"]["duplicate events skipped"] == 1
    assert payload["details"]["debounce events skipped"] == 0
    assert queue_payload == [{"path": "Documents/from-fswatch.txt", "action": "upload", "reason": "seed"}]
def test_pushd_fswatch_resident_run_debounces_recent_prior_run(tmp_path: Path) -> None:
    env = _base_env(
        tmp_path,
        {"PCLOUD_TOOLS_PUSHD_FSWATCH_RESIDENT_GATE": "operator-approved-fswatch-resident-v1"},
    )
    state_dir = _use_default_dev_state_dir(env)
    state_file = state_dir / "pushd" / "fswatch-resident-last-run.json"
    state_file.parent.mkdir(parents=True)
    state_file.write_text(
        json.dumps(
            {
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "appended_records": [
                    {"path": "Documents/recent.txt", "action": "upload", "reason": "fswatch"}
                ],
            }
        )
    )
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    (workspace / "Documents").mkdir()
    (workspace / "Documents" / "recent.txt").write_text("sample\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fswatch = bin_dir / "fswatch"
    fswatch.write_text("#!/bin/sh\nprintf '%s\\n' \"$PCLOUD_TOOLS_WORKSPACE_ROOT/Documents/recent.txt\"\n")
    fswatch.chmod(0o755)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    shadow_report = tmp_path / "shadow-validation-fswatch-prior-debounce.json"
    shadow_workspace = tmp_path / "pcloud-shadow-validation-fswatch-prior-debounce" / "workspace"
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
            "fswatch",
            "resident-run",
            "--report-path",
            str(shadow_report),
            "--operator-reviewed-probe",
            "--reviewer-approved-queue-policy",
            "--reviewer-approved-process-policy",
            "--max-events",
            "1",
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

    assert result.returncode == 0
    assert payload["details"]["queue records appended"] == 0
    assert payload["details"]["debounce events skipped"] == 1
    assert not (state_dir / "pushd" / "queue.json").exists()
def test_pushd_fswatch_resident_run_respects_queue_limit(tmp_path: Path) -> None:
    env = _base_env(
        tmp_path,
        {
            "PCLOUD_TOOLS_PUSHD_FSWATCH_RESIDENT_GATE": "operator-approved-fswatch-resident-v1",
            "PCLOUD_TOOLS_PUSHD_QUEUE_LIMIT": "1",
        },
    )
    state_dir = _use_default_dev_state_dir(env)
    queue_file = state_dir / "pushd" / "queue.json"
    queue_file.parent.mkdir(parents=True)
    queue_file.write_text(
        json.dumps([{"path": "Documents/existing.txt", "action": "upload", "reason": "seed"}])
    )
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    (workspace / "Documents").mkdir()
    (workspace / "Documents" / "new.txt").write_text("sample\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fswatch = bin_dir / "fswatch"
    fswatch.write_text("#!/bin/sh\nprintf '%s\\n' \"$PCLOUD_TOOLS_WORKSPACE_ROOT/Documents/new.txt\"\n")
    fswatch.chmod(0o755)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    shadow_report = tmp_path / "shadow-validation-fswatch-limit.json"
    shadow_workspace = tmp_path / "pcloud-shadow-validation-fswatch-limit" / "workspace"
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
            "fswatch",
            "resident-run",
            "--report-path",
            str(shadow_report),
            "--operator-reviewed-probe",
            "--reviewer-approved-queue-policy",
            "--reviewer-approved-process-policy",
            "--max-events",
            "1",
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
    queue_payload = json.loads(queue_file.read_text())

    assert result.returncode == 0
    assert payload["status"] == "warning"
    assert payload["details"]["queue records appended"] == 0
    assert payload["details"]["queue limit skips"] == 1
    assert queue_payload == [{"path": "Documents/existing.txt", "action": "upload", "reason": "seed"}]
def test_pushd_plan_suppresses_fresh_completed_download_until_local_file_changes(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = _use_default_dev_state_dir(env)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    target = workspace / "Documents" / "downloaded.txt"
    target.parent.mkdir(parents=True)
    target.write_text("downloaded\n")
    pushd_dir = state_dir / "pushd"
    diffd_dir = state_dir / "diffd"
    pushd_dir.mkdir(parents=True)
    diffd_dir.mkdir(parents=True)
    (pushd_dir / "queue.json").write_text(
        json.dumps([{"path": "Documents/downloaded.txt", "action": "upload", "reason": "fswatch"}])
    )
    fingerprint = local_fingerprint(target).as_dict()
    completed_at = datetime.now(timezone.utc).isoformat()
    (diffd_dir / "download-suppression-journal.json").write_text(
        json.dumps(
            {
                "schema_version": "pcloud-tools-download-suppression.v1",
                "records": [
                    {
                        "path": "Documents/downloaded.txt",
                        "state": "completed",
                        "direction": "download",
                        "started_at": completed_at,
                        "completed_at": completed_at,
                        "local_fingerprint": fingerprint,
                    }
                ],
            }
        )
    )

    suppressed = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "preview", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    target.write_text("user edit\n")
    allowed = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "preview", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    suppressed_payload = _payload(suppressed)
    allowed_payload = _payload(allowed)
    assert suppressed.returncode == 0
    assert suppressed_payload["details"]["planned uploads"] == 0
    assert suppressed_payload["details"]["excluded queue records"][0]["reason"] == "download suppression journal"
    assert allowed.returncode == 0
    assert allowed_payload["details"]["planned uploads"] == 1
def test_pushd_queue_add_and_clear_are_preview_first_dev_state_writes(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = _use_default_dev_state_dir(env)
    queue_file = state_dir / "pushd" / "queue.json"

    preview = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "queue",
            "add",
            "Documents/manual.pdf",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    assert preview.returncode == 0
    assert not queue_file.exists()
    assert _payload(preview)["details"]["queue items after"] == 1

    add = subprocess.run(
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
    assert add.returncode == 0
    assert json.loads(queue_file.read_text())[0]["path"] == "Documents/manual.pdf"
    assert _payload(add)["summary"] == "pushd queue record appended"

    preview_remove = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "queue",
            "remove",
            "Documents/manual.pdf",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    assert preview_remove.returncode == 0
    assert json.loads(queue_file.read_text())[0]["path"] == "Documents/manual.pdf"
    assert _payload(preview_remove)["details"]["queue items removed"] == 1

    remove = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "queue",
            "remove",
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
    assert remove.returncode == 0
    assert json.loads(queue_file.read_text()) == []
    assert _payload(remove)["summary"] == "pushd queue records removed"

    add_again = subprocess.run(
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
    assert add_again.returncode == 0

    clear = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "queue",
            "clear",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    assert clear.returncode == 0
    assert json.loads(queue_file.read_text()) == []
    assert _payload(clear)["details"]["queue items before"] == 1
def test_pushd_queue_remove_public_requires_explicit_gate(tmp_path: Path) -> None:
    env = _base_env(tmp_path, {"PCLOUD_TOOLS_DEV": "0"})
    public_core_dir = Path(env["HOME"]) / "p-core"
    public_core_dir.mkdir(parents=True)
    (public_core_dir / ".pcloud-sync-allowlist").write_text("Documents/\n")
    state_dir = _state_dir(env)
    pushd_dir = state_dir / "pushd"
    pushd_dir.mkdir(parents=True)
    queue_file = pushd_dir / "queue.json"
    queue_file.write_text(
        json.dumps(
            [
                {"path": "Documents/remote.txt", "action": "upload", "reason": "fswatch"},
                {"path": "Documents/keep.txt", "action": "upload", "reason": "fswatch"},
            ]
        )
    )

    refused = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "queue",
            "remove",
            "Documents/remote.txt",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    gated_env = env | {
        "PCLOUD_TOOLS_PUSHD_QUEUE_REMOVE_GATE": "operator-approved-pushd-queue-remove-v1",
    }
    removed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "queue",
            "remove",
            "Documents/remote.txt",
            "--reviewer-approved-queue-record-removal",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=gated_env,
    )

    refused_payload = _payload(refused)
    removed_payload = _payload(removed)
    assert refused.returncode == 1
    assert refused_payload["details"]["state writes"] == "none"
    assert "PCLOUD_TOOLS_DEV_EXECUTION" not in [issue["key"] for issue in refused_payload["issues"]]
    assert "PCLOUD_TOOLS_PUSHD_QUEUE_REMOVE_GATE" in [issue["key"] for issue in refused_payload["issues"]]
    assert removed.returncode == 0
    assert removed_payload["summary"] == "pushd queue records removed"
    assert removed_payload["details"]["queue remove gate env honored"] == "yes"
    assert removed_payload["details"]["queue record removal approval"] == "yes"
    assert removed_payload["details"]["state writes"] == "pushd queue only"
    assert json.loads(queue_file.read_text()) == [
        {"path": "Documents/keep.txt", "action": "upload", "reason": "fswatch"}
    ]
def test_pushd_missing_local_uploads_are_stale_and_do_not_block_downloads(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = _use_default_dev_state_dir(env)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    existing = workspace / "Documents" / "existing.txt"
    existing.parent.mkdir(parents=True)
    existing.write_text("local content\n")
    pushd_dir = state_dir / "pushd"
    diffd_dir = state_dir / "diffd"
    pushd_dir.mkdir(parents=True)
    diffd_dir.mkdir(parents=True)
    queue_file = pushd_dir / "queue.json"
    queue_file.write_text(
        json.dumps(
            [
                {"path": "Documents/missing.txt", "action": "upload", "reason": "fswatch"},
                {"path": "Documents/existing.txt", "action": "upload", "reason": "fswatch"},
            ]
        )
    )
    (diffd_dir / "remote-changes.json").write_text(
        json.dumps([{"path": "Documents/missing.txt", "action": "download", "reason": "diff:createfile"}])
    )

    pushd_status = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "status", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    pushd_preview = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "transfer", "preview", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    pushd_xbar = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "status", "--xbar"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    diffd_preview = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "diffd", "transfer", "preview", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    prune = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "queue",
            "prune-missing-local",
            "--reviewer-approved-missing-local-cleanup",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    status_payload = _payload(pushd_status)
    pushd_payload = _payload(pushd_preview)
    diffd_payload = _payload(diffd_preview)
    prune_payload = _payload(prune)
    assert pushd_status.returncode == 0
    assert status_payload["status"] == "warning"
    assert status_payload["details"]["missing local upload records"] == 1
    assert "pushd.queue.prune-missing-local" in [action["id"] for action in status_payload["actions"]]
    assert pushd_preview.returncode == 0
    assert pushd_payload["details"]["planned uploads"] == 1
    assert pushd_payload["details"]["missing local upload records"] == 1
    assert pushd_payload["details"]["planned transfer commands"][0]["path"] == "Documents/existing.txt"
    assert pushd_xbar.returncode == 0
    assert "missing local uploads: 1" in pushd_xbar.stdout
    assert "Missing local upload records" in pushd_xbar.stdout
    assert "Documents/missing.txt" in pushd_xbar.stdout
    assert "Ignore missing local upload records" in pushd_xbar.stdout
    assert "terminal=false" in pushd_xbar.stdout
    assert diffd_preview.returncode == 0
    assert diffd_payload["details"]["planned downloads"] == 1
    assert diffd_payload["details"]["manual review transfer records"] == 0
    assert prune.returncode == 0
    assert prune_payload["summary"] == "pushd queue missing local records pruned"
    assert prune_payload["details"]["queue items removed"] == 1
    assert json.loads(queue_file.read_text()) == [
        {"path": "Documents/existing.txt", "action": "upload", "reason": "fswatch"}
    ]
