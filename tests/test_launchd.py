from __future__ import annotations

from conftest import *


def test_service_launchd_gate_is_read_only_and_does_not_write_plists(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    pushd = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "launchd", "gate", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    pushd_human = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "launchd", "gate"],
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
            "launchd",
            "gate",
            "--operator-reviewed-daemon-command",
            "--reviewer-approved-plist-policy",
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

    pushd_payload = _payload(pushd)
    diffd_payload = _payload(diffd)
    pushd_details = pushd_payload["details"]
    diffd_details = diffd_payload["details"]

    assert pushd.returncode == 0
    assert pushd_human.returncode == 0
    assert diffd.returncode == 0
    assert "pushd launchd gate:" in pushd_human.stdout
    assert "launchd can register: no" in pushd_human.stdout
    assert "state writes: none" in pushd_human.stdout
    assert "bootstrap command examples:" in pushd_human.stdout
    assert "blocked operations:" in pushd_human.stdout
    assert "plist payload draft:" not in pushd_human.stdout
    assert pushd_payload["command"] == "pushd launchd gate"
    assert diffd_payload["command"] == "diffd launchd gate"
    assert pushd_details["implementation status"] == (
        "read-only launchd gate scaffold; plist is not written and launchctl is not executed"
    )
    assert diffd_details["implementation status"] == (
        "read-only launchd gate scaffold; plist is not written and launchctl is not executed"
    )
    assert pushd_details["launchd gate status"] == "closed"
    assert diffd_details["launchd gate status"] == "closed"
    assert pushd_details["launchd can register"] == "no"
    assert diffd_details["launchd can register"] == "no"
    assert pushd_details["state writes"] == "none"
    assert diffd_details["state writes"] == "none"
    assert pushd_details["service label"] == "com.example.pcloud-pushd.dev"
    assert diffd_details["service label"] == "com.example.pcloud-diffd.dev"
    assert pushd_details["plist status"] == "draft-only; not written by this command"
    assert diffd_details["plist status"] == "draft-only; not written by this command"
    assert pushd_details["plist payload draft"]["ProgramArguments"][1:4] == [
        "pushd",
        "fswatch",
        "resident-run",
    ]
    assert diffd_details["plist payload draft"]["ProgramArguments"][1:4] == [
        "diffd",
        "api-poll",
        "long-poll-run",
    ]
    assert pushd_details["future launchd gate env var"] == "PCLOUD_TOOLS_PUSHD_LAUNCHD_GATE"
    assert diffd_details["future launchd gate env var"] == "PCLOUD_TOOLS_DIFFD_LAUNCHD_GATE"
    assert "launchctl bootstrap" in pushd_details["blocked operations"]
    assert "writing LaunchAgent plist" in diffd_details["blocked operations"]
    assert diffd_details["approval status"] in {"pending", "complete-read-only"}
    assert not (workspace / ".dev-state" / "launchd").exists()
    assert not (state_dir / "pushd").exists()
    assert not (state_dir / "diffd").exists()
def test_service_launchd_status_is_read_only(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_launchctl = bin_dir / "launchctl"
    fake_launchctl.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"print\" ]; then\n"
        "  case \"$2\" in\n"
        "    *pcloud-diffd*) printf 'state = running\\n'; exit 0 ;;\n"
        "    *) printf 'Could not find service \"%s\"\\n' \"$2\" >&2; exit 113 ;;\n"
        "  esac\n"
        "fi\n"
        "exit 2\n"
    )
    fake_launchctl.chmod(0o755)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"

    pushd = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "launchd", "status", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    pushd_human = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "launchd", "status"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    diffd = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "diffd", "launchd", "status", "--json"],
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

    assert pushd.returncode == 0
    assert pushd_human.returncode == 0
    assert diffd.returncode == 0
    assert "pushd launchd status:" in pushd_human.stdout
    assert "registration status: not_loaded" in pushd_human.stdout
    assert "state writes: none" in pushd_human.stdout
    assert pushd_payload["command"] == "pushd launchd status"
    assert diffd_payload["command"] == "diffd launchd status"
    assert pushd_details["implementation status"] == "read-only launchd status surface; launchctl print only"
    assert diffd_details["implementation status"] == "read-only launchd status surface; launchctl print only"
    assert pushd_details["registration status"] == "not_loaded"
    assert diffd_details["registration status"] == "loaded"
    assert pushd_details["launchd loaded"] == "no"
    assert diffd_details["launchd loaded"] == "yes"
    assert pushd_details["state writes"] == "none"
    assert diffd_details["state writes"] == "none"
    assert pushd_details["launchd can register"] == "no"
    assert diffd_details["launchd can register"] == "no"
    assert pushd_details["launchctl print command"][1] == "print"
    assert "launchctl bootstrap" in diffd_details["blocked operations"]
    assert "pushd.launchd.status" in [action["id"] for action in pushd_payload["actions"]]
    assert "diffd.launchd.status" in [action["id"] for action in diffd_payload["actions"]]
    assert not (workspace / ".dev-state" / "launchd").exists()
    assert not (state_dir / "pushd").exists()
    assert not (state_dir / "diffd").exists()
def test_service_launchd_review_is_read_only_and_uses_public_wrapper(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_pcloud_manager = bin_dir / "pcloud-manager"
    fake_pcloud_manager.write_text("#!/bin/sh\nprintf 'fake pcloud-manager\\n'\n")
    fake_pcloud_manager.chmod(0o755)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"

    pushd = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "launchd", "review", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    diffd = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "diffd", "launchd", "review", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    pushd_human = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "launchd", "review"],
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

    assert pushd.returncode == 0
    assert diffd.returncode == 0
    assert pushd_human.returncode == 0
    assert pushd_payload["status"] == "warning"
    assert diffd_payload["status"] == "warning"
    assert pushd_details["state writes"] == "none"
    assert diffd_details["state writes"] == "none"
    assert pushd_details["launchctl execution"] == "no"
    assert pushd_details["persistent daemon start"] == "no"
    assert pushd_details["service label"] == "com.takafumi.pcloud-pushd"
    assert diffd_details["service label"] == "com.takafumi.pcloud-diffd"
    assert pushd_details["plist payload"]["ProgramArguments"][:4] == [
        str(fake_pcloud_manager),
        "pushd",
        "fswatch",
        "resident-run",
    ]
    assert diffd_details["foreground command preview"][:4] == [
        str(fake_pcloud_manager),
        "diffd",
        "api-poll",
        "long-poll-run",
    ]
    assert "pushd.launchd.review" in [action["id"] for action in pushd_payload["actions"]]
    assert "human review status:" in pushd_human.stdout
    assert "terminal review commands:" in pushd_human.stdout
    assert "launchctl bootstrap" in pushd_human.stdout
    assert not (Path(env["HOME"]) / "Library" / "LaunchAgents" / "com.takafumi.pcloud-pushd.plist").exists()
    assert not (state_dir / "pushd").exists()
    assert not (state_dir / "diffd").exists()
def test_service_launchd_plist_preview_and_dev_write_do_not_run_launchctl(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    launchctl_log = tmp_path / "launchctl.log"
    fake_launchctl = bin_dir / "launchctl"
    fake_launchctl.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$FAKE_LAUNCHCTL_LOG\"\nexit 0\n")
    fake_launchctl.chmod(0o755)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    env["FAKE_LAUNCHCTL_LOG"] = str(launchctl_log)
    pushd_plist = workspace / ".dev-state" / "launchd" / "com.example.pcloud-pushd.dev.plist"
    diffd_plist = workspace / ".dev-state" / "launchd" / "com.example.pcloud-diffd.dev.plist"

    pushd_preview = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "launchd", "plist", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    pushd_write = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "launchd", "plist", "--execute", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    diffd_write = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "diffd", "launchd", "plist", "--execute", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    pushd_human = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "launchd", "plist"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    pushd_preview_payload = _payload(pushd_preview)
    pushd_write_payload = _payload(pushd_write)
    diffd_write_payload = _payload(diffd_write)
    pushd_payload = plistlib.loads(pushd_plist.read_bytes())
    diffd_payload = plistlib.loads(diffd_plist.read_bytes())

    assert pushd_preview.returncode == 0
    assert pushd_write.returncode == 0
    assert diffd_write.returncode == 0
    assert pushd_human.returncode == 0
    assert pushd_preview_payload["details"]["state writes"] == "none"
    assert pushd_preview_payload["details"]["launchctl execution"] == "no"
    assert pushd_preview_payload["details"]["persistent daemon start"] == "no"
    assert pushd_preview_payload["details"]["plist status"] == "missing"
    assert pushd_preview_payload["details"]["plist payload"]["Label"] == "com.example.pcloud-pushd.dev"
    assert pushd_write_payload["summary"] == "pushd launchd plist written"
    assert pushd_write_payload["details"]["state writes"] == "launchd plist only"
    assert pushd_write_payload["details"]["launchctl execution"] == "no"
    assert diffd_write_payload["details"]["state writes"] == "launchd plist only"
    assert pushd_payload["Label"] == "com.example.pcloud-pushd.dev"
    assert diffd_payload["Label"] == "com.example.pcloud-diffd.dev"
    assert pushd_payload["ProgramArguments"][1:4] == ["pushd", "fswatch", "resident-run"]
    assert diffd_payload["ProgramArguments"][1:4] == ["diffd", "api-poll", "long-poll-run"]
    assert pushd_payload["WorkingDirectory"] == str(workspace)
    assert diffd_payload["WorkingDirectory"] == str(workspace)
    assert pushd_payload["StandardOutPath"].endswith("pushd-launchd.out")
    assert diffd_payload["StandardErrorPath"].endswith("diffd-launchd.err")
    assert pushd_payload["RunAtLoad"] is True
    assert pushd_payload["KeepAlive"] is False
    assert "program arguments:" in pushd_human.stdout
    assert "blocked operations:" in pushd_human.stdout
    assert not launchctl_log.exists()
    assert not (state_dir / "pushd").exists()
    assert not (state_dir / "diffd").exists()
def test_service_launchd_plist_execute_refuses_outside_dev_mode(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    env.pop("PCLOUD_TOOLS_DEV", None)

    result = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "launchd", "plist", "--execute", "--json"],
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
    assert payload["details"]["launchctl execution"] == "no"
    assert "PCLOUD_TOOLS_PUSHD_LAUNCHD_PLIST_EXECUTION" in [issue["key"] for issue in payload["issues"]]
    assert not (workspace / ".dev-state" / "launchd" / "com.example.pcloud-pushd.dev.plist").exists()
def test_service_launchd_public_plist_write_requires_gate_and_does_not_bootstrap(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    home = Path(env["HOME"])
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    env.pop("PCLOUD_TOOLS_DEV", None)
    env["PCLOUD_TOOLS_CORE_DIR"] = str(workspace)
    env["PCLOUD_TOOLS_ALLOWLIST_FILE"] = str(workspace / ".pcloud-sync-allowlist")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    launchctl_log = tmp_path / "launchctl.log"
    fake_pcloud_manager = bin_dir / "pcloud-manager"
    fake_pcloud_manager.write_text("#!/bin/sh\nprintf 'fake pcloud-manager\\n'\n")
    fake_pcloud_manager.chmod(0o755)
    fake_launchctl = bin_dir / "launchctl"
    fake_launchctl.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$FAKE_LAUNCHCTL_LOG\"\nexit 0\n")
    fake_launchctl.chmod(0o755)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    env["FAKE_LAUNCHCTL_LOG"] = str(launchctl_log)
    pushd_public_plist = home / "Library" / "LaunchAgents" / "com.takafumi.pcloud-pushd.plist"
    diffd_public_plist = home / "Library" / "LaunchAgents" / "com.takafumi.pcloud-diffd.plist"

    closed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "launchd",
            "plist",
            "--execute",
            "--public-write",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    gated_env = dict(env)
    gated_env["PCLOUD_TOOLS_PUSHD_LAUNCHD_PLIST_GATE"] = "operator-approved-pushd-launchd-plist-v1"
    opened = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "launchd",
            "plist",
            "--execute",
            "--public-write",
            "--operator-reviewed-plist",
            "--reviewer-approved-public-target",
            "--reviewer-approved-no-bootstrap",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=gated_env,
    )

    closed_payload = _payload(closed)
    opened_payload = _payload(opened)
    public_payload = plistlib.loads(pushd_public_plist.read_bytes())

    assert closed.returncode == 1
    assert closed_payload["status"] == "error"
    assert closed_payload["details"]["state writes"] == "none"
    assert closed_payload["details"]["launchctl execution"] == "no"
    assert "PCLOUD_TOOLS_PUSHD_LAUNCHD_PLIST_PUBLIC_APPROVAL" in [
        issue["key"] for issue in closed_payload["issues"]
    ]
    assert opened.returncode == 0
    assert opened_payload["summary"] == "pushd launchd plist written"
    assert opened_payload["details"]["plist target kind"] == "public"
    assert opened_payload["details"]["state writes"] == "public launchd plist only"
    assert opened_payload["details"]["launchctl execution"] == "no"
    assert opened_payload["details"]["persistent daemon start"] == "no"
    assert public_payload["Label"] == "com.takafumi.pcloud-pushd"
    assert public_payload["ProgramArguments"][:4] == [
        str(fake_pcloud_manager),
        "pushd",
        "fswatch",
        "resident-run",
    ]
    assert public_payload["WorkingDirectory"] == str(workspace)
    assert public_payload["RunAtLoad"] is True
    assert public_payload["KeepAlive"] is False
    assert not diffd_public_plist.exists()
    assert not launchctl_log.exists()
    assert not (state_dir / "pushd").exists()
    assert not (state_dir / "diffd").exists()
def test_service_launchd_public_plist_write_refuses_dev_runtime(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    env["PCLOUD_TOOLS_PUSHD_LAUNCHD_PLIST_GATE"] = "operator-approved-pushd-launchd-plist-v1"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "launchd",
            "plist",
            "--execute",
            "--public-write",
            "--operator-reviewed-plist",
            "--reviewer-approved-public-target",
            "--reviewer-approved-no-bootstrap",
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
    assert payload["details"]["state writes"] == "none"
    assert payload["details"]["launchctl execution"] == "no"
    assert "PCLOUD_TOOLS_PUSHD_LAUNCHD_PLIST_PUBLIC_RUNTIME" in [
        issue["key"] for issue in payload["issues"]
    ]
    assert not (workspace / ".dev-state" / "launchd" / "com.example.pcloud-pushd.dev.plist").exists()
def test_service_launchd_register_is_gated_and_uses_fake_launchctl(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    home = Path(env["HOME"])
    env.pop("PCLOUD_TOOLS_DEV", None)
    env["PCLOUD_TOOLS_CORE_DIR"] = str(workspace)
    env["PCLOUD_TOOLS_ALLOWLIST_FILE"] = str(workspace / ".pcloud-sync-allowlist")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    launchctl_log = tmp_path / "launchctl.log"
    fake_launchctl = bin_dir / "launchctl"
    fake_launchctl.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$FAKE_LAUNCHCTL_LOG\"\nexit 0\n")
    fake_launchctl.chmod(0o755)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    env["FAKE_LAUNCHCTL_LOG"] = str(launchctl_log)
    public_plist = home / "Library" / "LaunchAgents" / "com.takafumi.pcloud-pushd.plist"
    public_plist.parent.mkdir(parents=True)
    public_plist.write_bytes(
        plistlib.dumps(
            {
                "Label": "com.takafumi.pcloud-pushd",
                "ProgramArguments": ["/tmp/pcloud-manager", "pushd", "fswatch", "resident-run"],
                "RunAtLoad": True,
                "KeepAlive": False,
                "WorkingDirectory": str(workspace),
                "StandardOutPath": str(tmp_path / "pushd.out"),
                "StandardErrorPath": str(tmp_path / "pushd.err"),
            }
        )
    )
    shadow_report = tmp_path / "shadow-validation.json"
    shadow_workspace = tmp_path / "pcloud-shadow-validation-register" / "workspace"
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
                    {"name": "launchd register shadow", "status": "ok"},
                ],
            }
        )
    )

    preview = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "pushd", "launchd", "register", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    closed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "launchd",
            "register",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    gated_env = dict(env)
    gated_env["PCLOUD_TOOLS_PUSHD_LAUNCHD_GATE"] = "operator-approved-pushd-launchd-v1"
    opened = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "launchd",
            "register",
            "--report-path",
            str(shadow_report),
            "--operator-reviewed-daemon-command",
            "--reviewer-approved-plist-policy",
            "--reviewer-approved-launchctl-policy",
            "--reviewer-approved-rollback-policy",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=gated_env,
    )

    preview_payload = _payload(preview)
    closed_payload = _payload(closed)
    opened_payload = _payload(opened)
    launchctl_lines = launchctl_log.read_text().splitlines()

    assert preview.returncode == 0
    assert preview_payload["details"]["launchctl execution"] == "no"
    assert preview_payload["details"]["persistent daemon start"] == "no"
    assert preview_payload["details"]["launchd can register"] == "no"
    assert "pushd.launchd.register.preview" in [action["id"] for action in preview_payload["actions"]]
    assert closed.returncode == 1
    assert closed_payload["status"] == "error"
    assert closed_payload["details"]["launchctl execution"] == "no"
    assert "PCLOUD_TOOLS_PUSHD_LAUNCHD_REGISTER_APPROVAL" in [
        issue["key"] for issue in closed_payload["issues"]
    ]
    assert opened.returncode == 0
    assert opened_payload["summary"] == "pushd launchd registration completed"
    assert opened_payload["details"]["launchctl execution"] == "yes"
    assert opened_payload["details"]["persistent daemon start"] == "yes-if-bootstrap-succeeds"
    assert opened_payload["details"]["state writes"] == "launchctl registration only"
    assert len(opened_payload["details"]["launchctl results"]) == 2
    assert any(line.startswith("enable gui/") for line in launchctl_lines)
    assert any(line.startswith("bootstrap gui/") for line in launchctl_lines)
def test_pushd_launchd_resident_plist_write_is_gated_and_does_not_bootstrap(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    home = Path(env["HOME"])
    env.pop("PCLOUD_TOOLS_DEV", None)
    env["PCLOUD_TOOLS_CORE_DIR"] = str(workspace)
    env["PCLOUD_TOOLS_ALLOWLIST_FILE"] = str(workspace / ".pcloud-sync-allowlist")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    launchctl_log = tmp_path / "launchctl.log"
    fake_pcloud_manager = bin_dir / "pcloud-manager"
    fake_pcloud_manager.write_text("#!/bin/sh\nprintf 'fake pcloud-manager\\n'\n")
    fake_pcloud_manager.chmod(0o755)
    fake_fswatch = bin_dir / "fswatch"
    fake_fswatch.write_text("#!/bin/sh\nprintf 'fake fswatch\\n'\n")
    fake_fswatch.chmod(0o755)
    fake_launchctl = bin_dir / "launchctl"
    fake_launchctl.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$FAKE_LAUNCHCTL_LOG\"\nexit 0\n")
    fake_launchctl.chmod(0o755)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    env["FAKE_LAUNCHCTL_LOG"] = str(launchctl_log)
    public_plist = home / "Library" / "LaunchAgents" / "com.takafumi.pcloud-pushd.plist"
    shadow_report = tmp_path / "shadow-validation.json"
    shadow_workspace = tmp_path / "pcloud-shadow-validation-resident-plist" / "workspace"
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

    preview = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "launchd",
            "resident-plist",
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
    closed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "launchd",
            "resident-plist",
            "--report-path",
            str(shadow_report),
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    gated_env = dict(env)
    gated_env["PCLOUD_TOOLS_PUSHD_LAUNCHD_RESIDENT_PLIST_GATE"] = (
        "operator-approved-pushd-launchd-resident-plist-v1"
    )
    opened = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "launchd",
            "resident-plist",
            "--report-path",
            str(shadow_report),
            "--operator-reviewed-resident-command",
            "--reviewer-approved-resident-environment",
            "--reviewer-approved-no-bootstrap",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=gated_env,
    )

    preview_payload = _payload(preview)
    closed_payload = _payload(closed)
    opened_payload = _payload(opened)
    plist_payload = plistlib.loads(public_plist.read_bytes())

    assert preview.returncode == 0
    assert preview_payload["details"]["state writes"] == "none"
    assert preview_payload["details"]["launchctl execution"] == "no"
    assert "pushd.launchd.resident-plist.preview" in [
        action["id"] for action in preview_payload["actions"]
    ]
    assert closed.returncode == 1
    assert closed_payload["details"]["state writes"] == "none"
    assert "PCLOUD_TOOLS_PUSHD_LAUNCHD_RESIDENT_PLIST_APPROVAL" in [
        issue["key"] for issue in closed_payload["issues"]
    ]
    assert opened.returncode == 0
    assert opened_payload["summary"] == "pushd launchd resident plist written"
    assert opened_payload["details"]["state writes"] == "public launchd resident plist only"
    assert opened_payload["details"]["launchctl execution"] == "no"
    assert opened_payload["details"]["persistent daemon start"] == "no"
    assert plist_payload["Label"] == "com.takafumi.pcloud-pushd"
    assert plist_payload["ProgramArguments"][:4] == [
        str(fake_pcloud_manager),
        "pushd",
        "fswatch",
        "resident-run",
    ]
    assert "--execute" in plist_payload["ProgramArguments"]
    assert "--operator-reviewed-probe" in plist_payload["ProgramArguments"]
    assert "--reviewer-approved-queue-policy" in plist_payload["ProgramArguments"]
    assert "--reviewer-approved-process-policy" in plist_payload["ProgramArguments"]
    assert str(shadow_report.resolve()) in plist_payload["ProgramArguments"]
    assert plist_payload["EnvironmentVariables"]["PCLOUD_TOOLS_PUSHD_FSWATCH_RESIDENT_GATE"] == (
        "operator-approved-fswatch-resident-v1"
    )
    assert "/opt/homebrew/bin" in plist_payload["EnvironmentVariables"]["PATH"]
    assert plist_payload["KeepAlive"] is True
    assert not launchctl_log.exists()
def test_pushd_launchd_reload_is_gated_and_uses_fake_launchctl(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    home = Path(env["HOME"])
    env.pop("PCLOUD_TOOLS_DEV", None)
    env["PCLOUD_TOOLS_CORE_DIR"] = str(workspace)
    env["PCLOUD_TOOLS_ALLOWLIST_FILE"] = str(workspace / ".pcloud-sync-allowlist")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    launchctl_log = tmp_path / "launchctl.log"
    fake_launchctl = bin_dir / "launchctl"
    fake_launchctl.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$FAKE_LAUNCHCTL_LOG\"\nexit 0\n")
    fake_launchctl.chmod(0o755)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    env["FAKE_LAUNCHCTL_LOG"] = str(launchctl_log)
    public_plist = home / "Library" / "LaunchAgents" / "com.takafumi.pcloud-pushd.plist"
    public_plist.parent.mkdir(parents=True)
    public_plist.write_bytes(
        plistlib.dumps(
            {
                "Label": "com.takafumi.pcloud-pushd",
                "ProgramArguments": [
                    "/tmp/pcloud-manager",
                    "pushd",
                    "fswatch",
                    "resident-run",
                    "--operator-reviewed-probe",
                    "--reviewer-approved-queue-policy",
                    "--reviewer-approved-process-policy",
                    "--execute",
                    "--report-path",
                    str(tmp_path / "shadow-validation.json"),
                ],
                "EnvironmentVariables": {
                    "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                    "PCLOUD_TOOLS_PUSHD_FSWATCH_RESIDENT_GATE": "operator-approved-fswatch-resident-v1",
                },
                "RunAtLoad": True,
                "KeepAlive": False,
                "WorkingDirectory": str(workspace),
                "StandardOutPath": str(tmp_path / "pushd.out"),
                "StandardErrorPath": str(tmp_path / "pushd.err"),
            }
        )
    )
    shadow_report = tmp_path / "shadow-validation.json"
    shadow_workspace = tmp_path / "pcloud-shadow-validation-reload" / "workspace"
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

    preview = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "launchd",
            "reload",
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
    closed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "launchd",
            "reload",
            "--report-path",
            str(shadow_report),
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    gated_env = dict(env)
    gated_env["PCLOUD_TOOLS_PUSHD_LAUNCHD_RELOAD_GATE"] = "operator-approved-pushd-launchd-reload-v1"
    opened = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "launchd",
            "reload",
            "--report-path",
            str(shadow_report),
            "--operator-reviewed-resident-plist",
            "--reviewer-approved-bootout-bootstrap",
            "--reviewer-approved-rollback-policy",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=gated_env,
    )

    preview_payload = _payload(preview)
    closed_payload = _payload(closed)
    opened_payload = _payload(opened)
    launchctl_lines = launchctl_log.read_text().splitlines()

    assert preview.returncode == 0
    assert preview_payload["details"]["launchctl execution"] == "no"
    assert preview_payload["details"]["persistent daemon start"] == "no"
    assert preview_payload["details"]["resident plist status"] == "operational"
    assert "pushd.launchd.reload.preview" in [action["id"] for action in preview_payload["actions"]]
    assert closed.returncode == 1
    assert closed_payload["details"]["launchctl execution"] == "no"
    assert "PCLOUD_TOOLS_PUSHD_LAUNCHD_RELOAD_APPROVAL" in [
        issue["key"] for issue in closed_payload["issues"]
    ]
    assert opened.returncode == 0
    assert opened_payload["summary"] == "pushd launchd reload completed"
    assert opened_payload["details"]["launchctl execution"] == "yes"
    assert opened_payload["details"]["persistent daemon start"] == "yes-if-bootstrap-succeeds"
    assert opened_payload["details"]["state writes"] == "launchctl reload only"
    assert any(line.startswith("bootout gui/") for line in launchctl_lines)
    assert any(line.startswith("bootstrap gui/") for line in launchctl_lines)


def test_launchd_reload_help_describes_reviewer_approval_flags(tmp_path: Path) -> None:
    result = _run_cli(tmp_path, "pushd", "launchd", "reload", "--help")

    assert result.returncode == 0
    assert "--reviewer-approved-bootout-bootstrap" in result.stdout
    assert "Reviewer approval for launchd bootout/bootstrap reload" in result.stdout
    assert "--reviewer-approved-rollback-policy" in result.stdout


def test_diffd_launchd_operational_plist_and_reload_are_gated(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    home = Path(env["HOME"])
    env.pop("PCLOUD_TOOLS_DEV", None)
    env["PCLOUD_TOOLS_CORE_DIR"] = str(workspace)
    env["PCLOUD_TOOLS_ALLOWLIST_FILE"] = str(workspace / ".pcloud-sync-allowlist")
    env["PCLOUD_TOOLS_PCLOUD_API_TOKEN"] = "test-token"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    launchctl_log = tmp_path / "launchctl.log"
    fake_pcloud_manager = bin_dir / "pcloud-manager"
    fake_pcloud_manager.write_text("#!/bin/sh\nprintf 'fake pcloud-manager\\n'\n")
    fake_pcloud_manager.chmod(0o755)
    fake_launchctl = bin_dir / "launchctl"
    fake_launchctl.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$FAKE_LAUNCHCTL_LOG\"\nexit 0\n")
    fake_launchctl.chmod(0o755)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    env["FAKE_LAUNCHCTL_LOG"] = str(launchctl_log)
    public_plist = home / "Library" / "LaunchAgents" / "com.takafumi.pcloud-diffd.plist"
    shadow_report = tmp_path / "shadow-validation.json"
    shadow_workspace = tmp_path / "pcloud-shadow-validation-diffd-operational" / "workspace"
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

    preview = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "diffd",
            "launchd",
            "resident-plist",
            "--report-path",
            str(shadow_report),
            "--start-interval-seconds",
            "60",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    closed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "diffd",
            "launchd",
            "resident-plist",
            "--report-path",
            str(shadow_report),
            "--start-interval-seconds",
            "60",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    gated_env = dict(env)
    gated_env["PCLOUD_TOOLS_DIFFD_LAUNCHD_LONG_POLL_PLIST_GATE"] = (
        "operator-approved-diffd-launchd-long-poll-plist-v1"
    )
    opened = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "diffd",
            "launchd",
            "resident-plist",
            "--report-path",
            str(shadow_report),
            "--start-interval-seconds",
            "60",
            "--operator-reviewed-resident-command",
            "--reviewer-approved-resident-environment",
            "--reviewer-approved-no-bootstrap",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=gated_env,
    )

    preview_payload = _payload(preview)
    closed_payload = _payload(closed)
    opened_payload = _payload(opened)
    plist_payload = plistlib.loads(public_plist.read_bytes())

    assert preview.returncode == 0
    assert preview_payload["details"]["state writes"] == "none"
    assert preview_payload["details"]["start interval seconds"] == 60
    assert "diffd.launchd.resident-plist.preview" in [
        action["id"] for action in preview_payload["actions"]
    ]
    assert closed.returncode == 1
    assert "PCLOUD_TOOLS_DIFFD_LAUNCHD_RESIDENT_PLIST_APPROVAL" in [
        issue["key"] for issue in closed_payload["issues"]
    ]
    assert opened.returncode == 0
    assert opened_payload["summary"] == "diffd launchd resident plist written"
    assert opened_payload["details"]["launchctl execution"] == "no"
    assert plist_payload["ProgramArguments"][:4] == [
        str(fake_pcloud_manager),
        "diffd",
        "api-poll",
        "long-poll-run",
    ]
    assert "--live-api" in plist_payload["ProgramArguments"]
    assert "--max-iterations" in plist_payload["ProgramArguments"]
    assert "1" in plist_payload["ProgramArguments"]
    assert "--execute" in plist_payload["ProgramArguments"]
    assert plist_payload["StartInterval"] == 60
    assert plist_payload["KeepAlive"] is False
    assert opened_payload["details"]["start interval seconds"] == 60
    assert plist_payload["EnvironmentVariables"]["PCLOUD_TOOLS_DIFFD_API_LONG_POLL_GATE"] == (
        "operator-approved-api-long-poll-v1"
    )
    assert not launchctl_log.exists()

    reload_preview = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "diffd",
            "launchd",
            "reload",
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
    reload_env = dict(env)
    reload_env["PCLOUD_TOOLS_DIFFD_LAUNCHD_RELOAD_GATE"] = "operator-approved-diffd-launchd-reload-v1"
    reload_opened = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "diffd",
            "launchd",
            "reload",
            "--report-path",
            str(shadow_report),
            "--operator-reviewed-resident-plist",
            "--reviewer-approved-bootout-bootstrap",
            "--reviewer-approved-rollback-policy",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=reload_env,
    )

    reload_preview_payload = _payload(reload_preview)
    reload_opened_payload = _payload(reload_opened)
    launchctl_lines = launchctl_log.read_text().splitlines()

    assert reload_preview.returncode == 0
    assert reload_preview_payload["details"]["resident plist status"] == "operational"
    assert "diffd.launchd.reload.preview" in [
        action["id"] for action in reload_preview_payload["actions"]
    ]
    assert reload_opened.returncode == 0
    assert reload_opened_payload["summary"] == "diffd launchd reload completed"
    assert reload_opened_payload["details"]["state writes"] == "launchctl reload only"
    assert any(line.startswith("bootout gui/") for line in launchctl_lines)
    assert any(line.startswith("bootstrap gui/") for line in launchctl_lines)
def test_launchd_automation_plist_and_reload_are_preview_only(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    pushd_dir = state_dir / "pushd"
    diffd_dir = state_dir / "diffd"
    pushd_dir.mkdir(parents=True)
    diffd_dir.mkdir(parents=True)
    _write_workspace_file(env, "Documents/upload.pdf", "upload\n")
    (pushd_dir / "queue.json").write_text(
        json.dumps([{"path": "Documents/auto-upload.txt", "action": "upload", "reason": "test"}])
    )
    (diffd_dir / "remote-changes.json").write_text(
        json.dumps([{"path": "Documents/auto-download.txt", "action": "download", "reason": "test"}])
    )
    shadow_workspace = tmp_path / "pcloud-shadow-validation-launchd-automation" / "workspace"
    shadow_report = tmp_path / "shadow-validation-launchd-automation.json"
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

    common = [
        "--report-path",
        str(shadow_report),
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
    ]
    pushd = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "launchd",
            "automation-plist",
            *common,
            "--confirm-path",
            "Documents/auto-upload.txt",
            "--confirm-direction",
            "upload",
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
            "launchd",
            "automation-reload",
            *common,
            "--confirm-path",
            "Documents/auto-download.txt",
            "--confirm-direction",
            "download",
            "--operator-reviewed-automation-plist",
            "--reviewer-approved-bootout-bootstrap",
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
    assert pushd_payload["summary"] == "pushd launchd automation plist is gated"
    assert pushd_payload["details"]["automation command status"] == "implemented-gated"
    assert pushd_payload["details"]["public executor plist can write"] == "no"
    assert pushd_payload["details"]["public plist writes"] == "no"
    assert pushd_payload["details"]["launchctl execution"] == "no"
    assert pushd_payload["details"]["automatic real transfer execution"] == "no"
    assert pushd_payload["details"]["service label"] == "com.takafumi.pcloud-pushd-executor"
    assert pushd_payload["details"]["start interval seconds"] == 45
    assert pushd_payload["details"]["program arguments"][1:4] == [
        "pushd",
        "transfer",
        "automation-run",
    ]
    assert pushd_payload["details"]["environment variables"]["PCLOUD_TOOLS_REAL_TRANSFER_AUTOMATION_GATE"] == (
        "operator-approved-real-transfer-automation-v1"
    )
    assert pushd_payload["details"]["environment variables"][
        "PCLOUD_TOOLS_TRANSFER_EXEC_TIMEOUT_SECONDS"
    ] == "3600"
    assert pushd_payload["details"]["transfer timeout seconds"] == 3600
    assert "pushd.launchd.automation-plist.preview" in [
        action["id"] for action in pushd_payload["actions"]
    ]
    assert diffd_payload["summary"] == "diffd launchd automation reload is gated"
    assert diffd_payload["details"]["automation command status"] == "implemented-gated"
    assert diffd_payload["details"]["launchd can reload"] == "no"
    assert diffd_payload["details"]["state writes"] == "none"
    assert diffd_payload["details"]["public plist writes"] == "no"
    assert diffd_payload["details"]["launchctl execution"] == "no"
    assert diffd_payload["details"]["planned launchctl commands"][0][1] == "bootout"
    assert diffd_payload["details"]["planned launchctl commands"][1][1] == "bootstrap"
    assert "diffd.launchd.automation-reload.preview" in [
        action["id"] for action in diffd_payload["actions"]
    ]
    assert not (Path(env["HOME"]) / "Library" / "LaunchAgents" / "com.takafumi.pcloud-pushd-executor.plist").exists()
def test_public_automation_plist_and_reload_execute_only_with_fake_launchctl_gate(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    env["PCLOUD_TOOLS_DEV"] = "0"
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    env["PCLOUD_TOOLS_CORE_DIR"] = str(workspace)
    env["PCLOUD_TOOLS_ALLOWLIST_FILE"] = str(workspace / ".pcloud-sync-allowlist")
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env["HOME"] = str(home)
    public_bin = tmp_path / "public-bin"
    public_bin.mkdir()
    manager = public_bin / "pcloud-manager"
    manager.write_text("#!/bin/sh\nexit 0\n")
    manager.chmod(0o755)
    launchctl_log = tmp_path / "launchctl.log"
    launchctl = public_bin / "launchctl"
    launchctl.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$LAUNCHCTL_LOG\"\n"
        "if [ \"$1\" = \"bootout\" ]; then\n"
        "  printf '%s\\n' 'Boot-out failed: 3: No such process' >&2\n"
        "  exit 3\n"
        "fi\n"
    )
    launchctl.chmod(0o755)
    env["PATH"] = f"{public_bin}:{env['PATH']}"
    env["LAUNCHCTL_LOG"] = str(launchctl_log)
    env["PCLOUD_TOOLS_REAL_TRANSFER_AUTOMATION_GATE"] = "operator-approved-real-transfer-automation-v1"
    env["PCLOUD_TOOLS_PUSHD_UPLOAD_SETTLE_SECONDS"] = "0"
    env["PCLOUD_TOOLS_PUSHD_LAUNCHD_AUTOMATION_PLIST_GATE"] = (
        "operator-approved-pushd-launchd-automation-plist-v1"
    )
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    pushd_dir = state_dir / "pushd"
    pushd_dir.mkdir(parents=True)
    documents_dir = workspace / "Documents"
    documents_dir.mkdir(parents=True)
    (documents_dir / "auto-upload.txt").write_text("auto upload\n")
    (documents_dir / "auto-upload-2.txt").write_text("auto upload 2\n")
    (pushd_dir / "queue.json").write_text(
        json.dumps(
            [
                {"path": "Documents/auto-upload.txt", "action": "upload", "reason": "test"},
                {"path": "Documents/auto-upload-2.txt", "action": "upload", "reason": "test"},
            ]
        )
    )
    shadow_workspace = tmp_path / "pcloud-shadow-validation-public-automation" / "workspace"
    shadow_report = tmp_path / "shadow-validation-public-automation.json"
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
    common = [
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
    ]

    plist_write = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "launchd",
            "automation-plist",
            *common,
            "--operator-reviewed-automation-command",
            "--reviewer-approved-automation-environment",
            "--reviewer-approved-no-bootstrap",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    reload_env = env | {
        "PCLOUD_TOOLS_PUSHD_LAUNCHD_AUTOMATION_RELOAD_GATE": (
            "operator-approved-pushd-launchd-automation-reload-v1"
        )
    }
    reload = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "launchd",
            "automation-reload",
            *common,
            "--operator-reviewed-automation-plist",
            "--reviewer-approved-bootout-bootstrap",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=reload_env,
    )

    plist_payload = _payload(plist_write)
    reload_payload = _payload(reload)
    plist_path = home / "Library" / "LaunchAgents" / "com.takafumi.pcloud-pushd-executor.plist"
    plist = plistlib.loads(plist_path.read_bytes())

    assert plist_write.returncode == 0
    assert plist_payload["summary"] == "pushd launchd automation plist written"
    assert plist_payload["details"]["public plist writes"] == "yes"
    assert plist_payload["details"]["launchctl execution"] == "no"
    assert plist_payload["details"]["automation gate details"]["automation approval status"] == (
        "ready-for-launchd-review"
    )
    assert plist_payload["details"]["automation gate details"]["real transfer approvals source"] == (
        "current selected bounded automation tick"
    )
    assert plist_payload["details"]["automation gate details"]["automation batch limit"] == 10
    assert plist_payload["details"]["automation gate details"]["deferred transfer command count"] == 0
    assert plist["Label"] == "com.takafumi.pcloud-pushd-executor"
    assert plist["EnvironmentVariables"]["PCLOUD_TOOLS_REAL_TRANSFER_AUTOMATION_RUN_GATE"] == (
        "operator-approved-real-transfer-automation-run-v1"
    )
    assert plist["EnvironmentVariables"]["PCLOUD_TOOLS_TRANSFER_EXEC_TIMEOUT_SECONDS"] == "3600"
    assert "--report-path" in plist["ProgramArguments"]
    assert "--max-records" in plist["ProgramArguments"]
    max_records_index = plist["ProgramArguments"].index("--max-records") + 1
    assert plist["ProgramArguments"][max_records_index] == "10"
    assert reload.returncode == 0
    assert reload_payload["summary"] == "pushd launchd automation reload completed"
    assert reload_payload["details"]["launchctl execution"] == "yes"
    assert reload_payload["details"]["launchctl results"][0]["tolerated"] is True
    assert reload_payload["details"]["launchctl results"][1]["returncode"] == 0
    assert "automatic real transfer execution" in reload_payload["details"]
    assert launchctl_log.read_text().splitlines() == [
        f"bootout gui/{os.getuid()}/com.takafumi.pcloud-pushd-executor",
        f"bootstrap gui/{os.getuid()} {plist_path}",
    ]
def test_launchd_executor_plist_is_dev_state_fake_rclone_only(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    _use_default_dev_state_dir(env)
    _install_fake_rclone(env)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    dev_entrypoint = workspace / "pcloud-manager-dev"
    dev_entrypoint.write_text("#!/bin/sh\nprintf 'fake dev pcloud-manager\\n'\n")
    dev_entrypoint.chmod(0o755)

    preview = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "launchd",
            "executor-plist",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    opened = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "launchd",
            "executor-plist",
            "--start-interval-seconds",
            "30",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    diffd_opened = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "diffd",
            "launchd",
            "executor-plist",
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
    opened_payload = _payload(opened)
    diffd_payload = _payload(diffd_opened)
    pushd_plist_path = workspace / ".dev-state" / "launchd" / "com.example.pcloud-pushd-executor.dev.plist"
    diffd_plist_path = workspace / ".dev-state" / "launchd" / "com.example.pcloud-diffd-executor.dev.plist"
    pushd_plist = plistlib.loads(pushd_plist_path.read_bytes())
    diffd_plist = plistlib.loads(diffd_plist_path.read_bytes())

    assert preview.returncode == 0
    assert preview_payload["summary"] == "pushd launchd executor plist preview is ready"
    assert preview_payload["details"]["state writes"] == "none"
    assert preview_payload["details"]["start interval seconds"] == 60
    assert "pushd.launchd.executor-plist.preview" in [
        action["id"] for action in preview_payload["actions"]
    ]
    assert opened.returncode == 0
    assert opened_payload["summary"] == "pushd launchd executor plist written"
    assert opened_payload["details"]["state writes"] == "launchd executor plist only"
    assert opened_payload["details"]["launchctl execution"] == "no"
    assert opened_payload["details"]["public launchd changes"] == "no"
    assert opened_payload["details"]["real transfer automation gate status"] == "closed"
    assert pushd_plist["Label"] == "com.example.pcloud-pushd-executor.dev"
    assert pushd_plist["StartInterval"] == 30
    assert pushd_plist["RunAtLoad"] is False
    assert pushd_plist["KeepAlive"] is False
    assert pushd_plist["ProgramArguments"][:4] == [
        str(dev_entrypoint),
        "pushd",
        "transfer",
        "executor-run",
    ]
    assert "--execute" in pushd_plist["ProgramArguments"]
    assert "--consume-on-success" in pushd_plist["ProgramArguments"]
    assert pushd_plist["EnvironmentVariables"]["PCLOUD_TOOLS_DEV"] == "1"
    assert pushd_plist["EnvironmentVariables"]["PCLOUD_TOOLS_TRANSFER_EXECUTION_GATE"] == "dev-fake-rclone"
    assert pushd_plist["EnvironmentVariables"]["PCLOUD_TOOLS_RCLONE_BIN"].endswith(
        "/.dev-state/bin/fake-rclone"
    )
    assert diffd_opened.returncode == 0
    assert diffd_payload["summary"] == "diffd launchd executor plist written"
    assert diffd_plist["ProgramArguments"][:4] == [
        str(dev_entrypoint),
        "diffd",
        "transfer",
        "executor-run",
    ]
    assert diffd_plist["StartInterval"] == 60
