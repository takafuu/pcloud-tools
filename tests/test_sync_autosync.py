from __future__ import annotations

from conftest import *


def test_sync_autosync_gate_is_read_only_checklist(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    launchctl = bin_dir / "launchctl"
    launchctl.write_text("#!/bin/sh\nif [ \"$1\" = \"print\" ]; then exit 1; fi\nexit 0\n")
    launchctl.chmod(0o755)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    plist = workspace / ".dev-state" / "com.example.pcloud-bisync.dev.plist"
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text("<plist><dict/></plist>\n")
    shadow_workspace = tmp_path / "pcloud-shadow-validation-autosync" / "workspace"
    shadow_report = tmp_path / "shadow-validation-autosync.json"
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
            "autosync-gate",
            "--report-path",
            str(shadow_report),
            "--operator-reviewed-preview",
            "--reviewer-approved-plist",
            "--reviewer-approved-launchctl-policy",
            "--reviewer-approved-rollback-policy",
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
    assert payload["summary"] == "autosync launchd gate is closed"
    assert payload["details"]["implementation status"].startswith("read-only checklist")
    assert payload["details"]["launchd gate status"] == "closed"
    assert payload["details"]["autosync changes can run"] == "no"
    assert payload["details"]["state writes"] == "none"
    assert payload["details"]["autosync state"] == "not_loaded"
    assert payload["details"]["launchctl availability"] == "available"
    assert payload["details"]["autosync plist status"] == "present"
    assert payload["details"]["autosync plist review command"] == ["plutil", "-p", str(plist)]
    assert payload["details"]["autosync approval status"] == "complete-read-only"
    assert payload["details"]["human gate status"] == "required-before-autosync-launchd-change"
    assert checks["saved shadow validation report"]["status"] == "ok"
    assert checks["launchctl binary"]["status"] == "ok"
    assert checks["autosync plist"]["status"] == "ok"
    assert checks["operator preview review"]["status"] == "ok"
    assert checks["plist approval"]["status"] == "ok"
    assert checks["launchctl policy approval"]["status"] == "ok"
    assert checks["rollback policy approval"]["status"] == "ok"
    assert "sync.autosync.gate" in [action["id"] for action in payload["actions"]]
    assert not any(state_dir.iterdir())
def test_sync_autosync_gate_missing_plist_reports_review_command(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "sync",
            "autosync-gate",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    plist = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"]) / ".dev-state" / "com.example.pcloud-bisync.dev.plist"

    assert result.returncode == 0
    assert payload["status"] == "warning"
    assert payload["details"]["autosync plist status"] == "missing"
    assert payload["details"]["autosync plist review command"] == ["plutil", "-p", str(plist)]
    assert "autosync-gate does not write it" in payload["details"]["autosync plist note"]
    assert "PCLOUD_TOOLS_AUTOSYNC_PLIST" in [issue["key"] for issue in payload["issues"]]
def test_sync_autosync_run_refuses_without_execution_gate(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = _use_default_dev_state_dir(env)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "launchctl.log"
    launchctl = bin_dir / "launchctl"
    launchctl.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$LAUNCHCTL_LOG\"\n"
        "if [ \"$1\" = \"print\" ]; then exit 1; fi\n"
        "exit 0\n"
    )
    launchctl.chmod(0o755)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["LAUNCHCTL_LOG"] = str(log)
    plist = workspace / ".dev-state" / "com.example.pcloud-bisync.dev.plist"
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text("<plist><dict/></plist>\n")
    shadow_report = tmp_path / "shadow-validation-autosync-run.json"
    shadow_workspace = tmp_path / "pcloud-shadow-validation-autosync-run" / "workspace"
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
            "autosync-run",
            "enable",
            "--report-path",
            str(shadow_report),
            "--operator-reviewed-preview",
            "--reviewer-approved-plist",
            "--reviewer-approved-launchctl-policy",
            "--reviewer-approved-rollback-policy",
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
    assert payload["summary"] == "autosync launchd execution is gated"
    assert payload["details"]["state writes"] == "none"
    assert payload["details"]["autosync changes can run"] == "no"
    assert "PCLOUD_TOOLS_AUTOSYNC_LAUNCHD_EXECUTION_GATE" in [issue["key"] for issue in payload["issues"]]
    assert "enable " not in log.read_text()
    assert not (state_dir / "sync" / "autosync-launchd-last-run.json").exists()
def test_sync_autosync_run_executes_fake_launchctl_in_dev_state(tmp_path: Path) -> None:
    env = _base_env(
        tmp_path,
        {"PCLOUD_TOOLS_AUTOSYNC_LAUNCHD_GATE": "operator-approved-autosync-launchd-v1"},
    )
    state_dir = _use_default_dev_state_dir(env)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "launchctl.log"
    launchctl = bin_dir / "launchctl"
    launchctl.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$LAUNCHCTL_LOG\"\n"
        "if [ \"$1\" = \"print\" ]; then exit 1; fi\n"
        "exit 0\n"
    )
    launchctl.chmod(0o755)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["LAUNCHCTL_LOG"] = str(log)
    plist = workspace / ".dev-state" / "com.example.pcloud-bisync.dev.plist"
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text("<plist><dict/></plist>\n")
    shadow_report = tmp_path / "shadow-validation-autosync-run-ok.json"
    shadow_workspace = tmp_path / "pcloud-shadow-validation-autosync-run-ok" / "workspace"
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
            "autosync-run",
            "enable",
            "--report-path",
            str(shadow_report),
            "--operator-reviewed-preview",
            "--reviewer-approved-plist",
            "--reviewer-approved-launchctl-policy",
            "--reviewer-approved-rollback-policy",
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
    launchctl_log = log.read_text()
    run_state = json.loads((state_dir / "sync" / "autosync-launchd-last-run.json").read_text())

    assert result.returncode == 0
    assert payload["status"] in {"ok", "warning"}
    assert payload["summary"] == "autosync launchd run completed"
    assert payload["details"]["autosync changes can run"] == "yes"
    assert payload["details"]["state writes"] == "autosync launchd run state"
    assert "enable gui/" in launchctl_log
    assert "bootstrap gui/" in launchctl_log
    assert run_state["mode"] == "enable"
    assert len(run_state["commands"]) == 2
def test_sync_autosync_plist_preview_and_dev_execute_are_guarded(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    dev_entrypoint = workspace / "pcloud-manager-dev"
    dev_entrypoint.write_text("#!/bin/sh\nexit 0\n")
    dev_entrypoint.chmod(0o755)
    plist = workspace / ".dev-state" / "com.example.pcloud-bisync.dev.plist"

    preview = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "sync", "autosync-plist", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    preview_payload = _payload(preview)

    assert preview.returncode == 0
    assert preview_payload["status"] in {"ok", "warning"}
    assert preview_payload["details"]["state writes"] == "none"
    assert preview_payload["details"]["launchctl execution"] == "no"
    assert not plist.exists()

    written = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "sync", "autosync-plist", "--execute", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    written_payload = _payload(written)
    plist_payload = plistlib.loads(plist.read_bytes())

    assert written.returncode == 0
    assert written_payload["status"] in {"ok", "warning"}
    assert written_payload["summary"] == "autosync plist written"
    assert written_payload["details"]["state writes"] == "autosync plist only"
    assert written_payload["details"]["scheduled sync execution"] == "no"
    assert plist_payload["Label"] == "com.example.pcloud-bisync.dev"
    assert plist_payload["ProgramArguments"] == [
        str(dev_entrypoint),
        "sync",
        "background",
        "--execute",
    ]
def test_sync_autosync_plist_execute_refuses_outside_dev_state(tmp_path: Path) -> None:
    env = _base_env(
        tmp_path,
        {"PCLOUD_TOOLS_AUTOSYNC_PLIST": str(tmp_path / "outside.plist")},
    )
    result = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "sync", "autosync-plist", "--execute", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)

    assert result.returncode == 1
    assert payload["status"] == "error"
    assert payload["details"]["state writes"] == "none"
    assert not (tmp_path / "outside.plist").exists()
    assert "PCLOUD_TOOLS_AUTOSYNC_PLIST_PATH" in [issue["key"] for issue in payload["issues"]]
