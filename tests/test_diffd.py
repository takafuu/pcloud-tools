from __future__ import annotations

from conftest import *


def test_diffd_preview_builds_remote_and_pending_download_plan(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    diffd_dir = state_dir / "diffd"
    daemon_dir = state_dir / "daemon"
    diffd_dir.mkdir(parents=True)
    daemon_dir.mkdir(parents=True)
    (diffd_dir / "remote-changes.json").write_text(
        json.dumps(
            [
                {"path": "Documents/remote.pdf", "action": "download"},
                {"path": "", "action": "download"},
            ]
        )
    )
    (daemon_dir / "pending-downloads.json").write_text(
        json.dumps(
            [
                {
                    "path": "Documents/pending.pdf",
                    "diffid": "123",
                    "reason": "remote-change",
                    "recorded_at": "2026-04-25T00:00:00+00:00",
                }
            ]
        )
    )

    result = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "diffd", "preview", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    assert result.returncode == 0
    assert payload["status"] in {"ok", "warning"}
    assert payload["details"]["remote changes"] == 2
    assert payload["details"]["pending downloads"] == 1
    assert payload["details"]["planned downloads"] == 2
    assert payload["details"]["skipped download records"] == 1
    assert (
        payload["details"]["plan summary"]
        == "downloads: 2; remote changes: 2; pending downloads: 1; skipped: 1"
    )
    assert payload["details"]["planned download records"][0]["path"] == "Documents/remote.pdf"
    assert payload["details"]["skipped download record details"][0]["path"] == ""


def test_diffd_preview_root_allowlist_plans_p_core_wide_files(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    diffd_dir = state_dir / "diffd"
    diffd_dir.mkdir(parents=True)
    (workspace / ".pcloud-sync-allowlist").write_text("/\n")
    (diffd_dir / "remote-changes.json").write_text(
        json.dumps(
            [
                {"path": "dev/foo.txt", "action": "download"},
                {"path": "project/foo.txt", "action": "download"},
            ]
        )
    )

    result = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "diffd", "preview", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    assert result.returncode == 0
    assert payload["details"]["planned downloads"] == 2
    assert payload["details"]["skipped download records"] == 0
    assert [record["path"] for record in payload["details"]["planned download records"]] == [
        "dev/foo.txt",
        "project/foo.txt",
    ]


def test_diffd_preview_root_allowlist_still_skips_dangerous_paths(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    diffd_dir = state_dir / "diffd"
    diffd_dir.mkdir(parents=True)
    (workspace / ".pcloud-sync-allowlist").write_text("/\n")
    dangerous_paths = [
        ".venv/secret.txt",
        "node_modules/pkg/index.js",
        "dev/pcloud_tools/__pycache__/core.pyc",
        ".git/config",
        ".env",
        "dev/.hidden",
        "LLM/private.txt",
    ]
    (diffd_dir / "remote-changes.json").write_text(
        json.dumps([{"path": path, "action": "download"} for path in dangerous_paths])
    )

    result = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "diffd", "preview", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    skipped = payload["details"]["skipped download record details"]
    assert result.returncode == 0
    assert payload["details"]["planned downloads"] == 0
    assert payload["details"]["skipped download records"] == len(dangerous_paths)
    assert [record["path"] for record in skipped] == dangerous_paths
    assert {record["reason"] for record in skipped} == {"manager ignore rule"}


def test_diffd_preview_applies_allowlist_and_default_excludes(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    diffd_dir = state_dir / "diffd"
    diffd_dir.mkdir(parents=True)
    (diffd_dir / "remote-changes.json").write_text(
        json.dumps(
            [
                {"path": "Documents/remote.pdf", "action": "download"},
                {"path": "Documents/.DS_Store", "action": "download"},
                {"path": "Documents/.temporary-download", "action": "download"},
                {"path": "private/secret.txt", "action": "download"},
            ]
        )
    )

    result = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "diffd", "preview", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    assert result.returncode == 0
    assert payload["details"]["planned downloads"] == 1
    assert payload["details"]["skipped download records"] == 3
    skipped = payload["details"]["skipped download record details"]
    assert skipped[0]["reason"] == "default exclude"
    assert skipped[1]["reason"] == "manager ignore rule"
    assert skipped[2]["reason"] == "outside sync scope"


def test_diffd_delete_events_do_not_become_download_transfer_commands(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    diffd_dir = state_dir / "diffd"
    diffd_dir.mkdir(parents=True)
    (diffd_dir / "remote-changes.json").write_text(
        json.dumps([{"path": "Documents/deleted-remote.txt", "action": "delete", "reason": "diff:deletefile"}])
    )

    result = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "diffd", "transfer", "preview", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)

    assert result.returncode == 0
    assert payload["status"] == "warning"
    assert payload["details"]["planned downloads"] == 0
    assert payload["details"]["planned transfer commands"] == []
    assert payload["details"]["manual review transfer records"] == 1
    assert payload["details"]["manual review transfer record details"][0]["path"] == "Documents/deleted-remote.txt"
    assert "delete action" in payload["details"]["manual review transfer record details"][0]["reason"]


def test_diffd_diff_fixture_preview_is_read_only(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    fixture = tmp_path / "pcloud-diff.json"
    fixture.write_text(
        json.dumps(
            {
                "diffid": "456",
                "entries": [
                    {"path": "/Documents/remote.pdf", "event": "modified"},
                    {"path": "", "event": "deleted"},
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
            "diff",
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
    assert payload["summary"] == "diffd pCloud diff fixture preview is ready"
    assert payload["details"]["implementation status"] == "fixture parser only; pCloud API is not called"
    assert payload["details"]["gate status"] == "closed"
    assert payload["details"]["fixture diffid"] == "456"
    assert payload["details"]["parsed diff changes"] == 1
    assert payload["details"]["invalid diff changes"] == 1
    assert payload["details"]["remote changes"] == 2
    assert payload["details"]["planned downloads"] == 1
    assert payload["details"]["skipped download records"] == 1
    assert payload["details"]["remote change records"][0]["reason"] == "diff:modified"
    assert not (state_dir / "diffd").exists()
def test_diffd_api_poll_preview_is_request_shape_only(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])

    result = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "diffd", "api-poll", "preview", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    assert result.returncode == 0
    assert payload["summary"] == "diffd pCloud API poll preview is ready"
    assert payload["details"]["implementation status"] == "API poll preview only; pCloud API is not called"
    assert payload["details"]["gate status"] == "closed"
    assert payload["details"]["request method"] == "GET"
    assert payload["details"]["request path"] == "/diff"
    assert payload["details"]["request query"]["diffid"] == "0"
    assert payload["details"]["state writes"] == "none"
    assert not (state_dir / "diffd").exists()
def test_diffd_api_long_poll_gate_is_read_only_checklist(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    shadow_workspace = tmp_path / "pcloud-shadow-validation-api-poll" / "workspace"
    shadow_report = tmp_path / "shadow-validation-api-poll.json"
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
            "api-poll",
            "long-poll-gate",
            "--report-path",
            str(shadow_report),
            "--operator-reviewed-preview",
            "--reviewer-approved-response-policy",
            "--reviewer-approved-credential-policy",
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
    assert payload["summary"] == "diffd pCloud API long-poll gate is closed"
    assert payload["details"]["implementation status"].startswith("read-only checklist")
    assert payload["details"]["long-poll gate status"] == "closed"
    assert payload["details"]["long-poll can start"] == "no"
    assert payload["details"]["state writes"] == "none"
    assert payload["details"]["human gate status"] == "required-before-api-long-poll"
    assert payload["details"]["request method"] == "GET"
    assert payload["details"]["request path"] == "/diff"
    assert payload["details"]["request query"]["diffid"] == "0"
    assert payload["details"]["poll interval seconds"] == 60
    assert payload["details"]["batch limit"] == 100
    assert payload["details"]["long-poll approval status"] == "complete-read-only"
    assert checks["saved shadow validation report"]["status"] == "ok"
    assert checks["API preview command"]["status"] == "ok"
    assert checks["diff cursor state"]["status"] == "ok"
    assert checks["download scope"]["status"] == "ok"
    assert checks["operator preview review"]["status"] == "ok"
    assert checks["response policy approval"]["status"] == "ok"
    assert checks["credential policy approval"]["status"] == "ok"
    assert checks["process lifecycle approval"]["status"] == "ok"
    assert "diffd.api-poll.long-poll-gate" in [action["id"] for action in payload["actions"]]
    assert not (state_dir / "diffd").exists()
def test_diffd_api_long_poll_run_refuses_without_execution_gate(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = _use_default_dev_state_dir(env)
    fixture = tmp_path / "api-long-poll.json"
    fixture.write_text(
        json.dumps(
            {
                "diffid": "123",
                "entries": [
                    {"path": "Documents/from-api.pdf", "event": "modified"},
                ],
            }
        )
    )
    shadow_report = tmp_path / "shadow-validation-api-long-poll-run.json"
    shadow_workspace = tmp_path / "pcloud-shadow-validation-api-long-poll-run" / "workspace"
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
            "api-poll",
            "long-poll-run",
            "--report-path",
            str(shadow_report),
            "--operator-reviewed-preview",
            "--reviewer-approved-response-policy",
            "--reviewer-approved-credential-policy",
            "--reviewer-approved-process-policy",
            "--fixture",
            str(fixture),
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
    assert payload["summary"] == "diffd pCloud API long-poll execution is gated"
    assert payload["details"]["state writes"] == "none"
    assert payload["details"]["long-poll can start"] == "no"
    assert "PCLOUD_TOOLS_DIFFD_API_LONG_POLL_EXECUTION_GATE" in [
        issue["key"] for issue in payload["issues"]
    ]
    assert not (state_dir / "diffd" / "remote-changes.json").exists()
    assert not (state_dir / "daemon" / "diffid").exists()
def test_diffd_api_long_poll_run_executes_fixture_in_dev_state(tmp_path: Path) -> None:
    env = _base_env(
        tmp_path,
        {"PCLOUD_TOOLS_DIFFD_API_LONG_POLL_GATE": "operator-approved-api-long-poll-v1"},
    )
    state_dir = _use_default_dev_state_dir(env)
    fixture = tmp_path / "api-long-poll-ok.json"
    fixture.write_text(
        json.dumps(
            {
                "diffid": "123",
                "entries": [
                    {"path": "Documents/from-api.pdf", "event": "modified"},
                    {"path": "private/outside.pdf", "event": "modified"},
                ],
            }
        )
    )
    shadow_report = tmp_path / "shadow-validation-api-long-poll-run-ok.json"
    shadow_workspace = tmp_path / "pcloud-shadow-validation-api-long-poll-run-ok" / "workspace"
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
            "api-poll",
            "long-poll-run",
            "--report-path",
            str(shadow_report),
            "--operator-reviewed-preview",
            "--reviewer-approved-response-policy",
            "--reviewer-approved-credential-policy",
            "--reviewer-approved-process-policy",
            "--fixture",
            str(fixture),
            "--max-iterations",
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
    remote_changes = json.loads((state_dir / "diffd" / "remote-changes.json").read_text())
    diffid = (state_dir / "daemon" / "diffid").read_text().strip()
    run_state = json.loads((state_dir / "diffd" / "api-long-poll-last-run.json").read_text())

    assert result.returncode == 0
    assert payload["status"] in {"ok", "warning"}
    assert payload["summary"] == "diffd pCloud API long-poll run completed"
    assert payload["details"]["long-poll can start"] == "yes"
    assert payload["details"]["state writes"] == "diffd remote-change records, diff cursor, and long-poll run state"
    assert payload["details"]["download records appended"] == 1
    assert payload["details"]["skipped download records"] == 1
    assert payload["details"]["written diffid"] == "123"
    assert remote_changes == [{"path": "Documents/from-api.pdf", "action": "download", "reason": "diff:modified"}]
    assert diffid == "123"
    assert run_state["appended_records"] == remote_changes
    assert run_state["written_diffid"] == "123"
def test_diffd_api_long_poll_parses_pcloud_metadata_paths(tmp_path: Path) -> None:
    env = _base_env(
        tmp_path,
        {"PCLOUD_TOOLS_DIFFD_API_LONG_POLL_GATE": "operator-approved-api-long-poll-v1"},
    )
    state_dir = _use_default_dev_state_dir(env)
    fixture = tmp_path / "api-long-poll-metadata.json"
    fixture.write_text(
        json.dumps(
            {
                "diffid": "124",
                "entries": [
                    {"diffid": 0, "event": "reset"},
                    {
                        "diffid": 1,
                        "event": "createfolder",
                        "metadata": {"isfolder": True, "folderid": 10, "parentfolderid": 0, "name": "Documents"},
                    },
                    {
                        "diffid": 2,
                        "event": "createfile",
                        "metadata": {"isfolder": False, "parentfolderid": 10, "name": "from-metadata.pdf"},
                    },
                    {
                        "diffid": 3,
                        "event": "createfolder",
                        "metadata": {"isfolder": True, "folderid": 11, "parentfolderid": 0, "name": "private"},
                    },
                    {
                        "diffid": 4,
                        "event": "createfile",
                        "metadata": {"isfolder": False, "parentfolderid": 11, "name": "skip.pdf"},
                    },
                    {"diffid": 5, "event": "modifyuserinfo"},
                ],
            }
        )
    )
    shadow_report = tmp_path / "shadow-validation-api-long-poll-metadata.json"
    shadow_workspace = tmp_path / "pcloud-shadow-validation-api-long-poll-metadata" / "workspace"
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
            "api-poll",
            "long-poll-run",
            "--report-path",
            str(shadow_report),
            "--operator-reviewed-preview",
            "--reviewer-approved-response-policy",
            "--reviewer-approved-credential-policy",
            "--reviewer-approved-process-policy",
            "--fixture",
            str(fixture),
            "--max-iterations",
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
    remote_changes = json.loads((state_dir / "diffd" / "remote-changes.json").read_text())

    assert result.returncode == 0
    assert payload["details"]["parsed diff changes"] == 2
    assert payload["details"]["invalid diff changes"] == 0
    assert payload["details"]["download records appended"] == 1
    assert payload["details"]["skipped download records"] == 1
    assert remote_changes == [
        {"path": "Documents/from-metadata.pdf", "action": "download", "reason": "diff:createfile"}
    ]
def test_diffd_api_long_poll_reuses_folder_cache_across_runs(tmp_path: Path) -> None:
    env = _base_env(
        tmp_path,
        {"PCLOUD_TOOLS_DIFFD_API_LONG_POLL_GATE": "operator-approved-api-long-poll-v1"},
    )
    state_dir = _use_default_dev_state_dir(env)
    shadow_report = tmp_path / "shadow-validation-api-folder-cache.json"
    shadow_workspace = tmp_path / "pcloud-shadow-validation-api-folder-cache" / "workspace"
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
    first_fixture = tmp_path / "api-folder-cache-first.json"
    first_fixture.write_text(
        json.dumps(
            {
                "diffid": "10",
                "entries": [
                    {
                        "diffid": 10,
                        "event": "createfolder",
                        "metadata": {"isfolder": True, "folderid": 42, "parentfolderid": 0, "name": "Documents"},
                    }
                ],
            }
        )
    )
    second_fixture = tmp_path / "api-folder-cache-second.json"
    second_fixture.write_text(
        json.dumps(
            {
                "diffid": "11",
                "entries": [
                    {
                        "diffid": 11,
                        "event": "createfile",
                        "metadata": {"isfolder": False, "parentfolderid": 42, "name": "from-cache.pdf"},
                    }
                ],
            }
        )
    )
    common = [
        sys.executable,
        "-m",
        "pcloud_tools.cli",
        "diffd",
        "api-poll",
        "long-poll-run",
        "--report-path",
        str(shadow_report),
        "--operator-reviewed-preview",
        "--reviewer-approved-response-policy",
        "--reviewer-approved-credential-policy",
        "--reviewer-approved-process-policy",
        "--max-iterations",
        "1",
        "--execute",
        "--json",
    ]

    first = subprocess.run(
        [*common, "--fixture", str(first_fixture)],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    second = subprocess.run(
        [*common, "--fixture", str(second_fixture)],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    first_payload = _payload(first)
    second_payload = _payload(second)
    folder_cache = json.loads((state_dir / "diffd" / "folder-cache.json").read_text())
    remote_changes = json.loads((state_dir / "diffd" / "remote-changes.json").read_text())

    assert first.returncode == 0
    assert second.returncode == 0
    assert first_payload["details"]["folder cache entries before"] == 0
    assert first_payload["details"]["folder cache entries after"] == 1
    assert second_payload["details"]["folder cache entries before"] == 1
    assert second_payload["details"]["parsed diff changes"] == 1
    assert second_payload["details"]["invalid diff changes"] == 0
    assert folder_cache == {"42": "Documents"}
    assert remote_changes == [
        {"path": "Documents/from-cache.pdf", "action": "download", "reason": "diff:createfile"}
    ]
def test_diffd_api_long_poll_resolves_live_parent_folder_metadata(tmp_path: Path) -> None:
    requests: list[tuple[str, dict[str, list[str]]]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            requests.append((parsed.path, query))
            if parsed.path == "/diff":
                body = json.dumps(
                    {
                        "diffid": "2228182",
                        "entries": [
                            {
                                "diffid": 2228181,
                                "event": "createfile",
                                "metadata": {
                                    "isfolder": False,
                                    "parentfolderid": 30754773616,
                                    "name": "IMG_001.jpeg",
                                },
                            }
                        ],
                    }
                ).encode()
            elif parsed.path == "/listfolder" and query.get("folderid") == ["30754773616"]:
                body = json.dumps(
                    {
                        "metadata": {
                            "isfolder": True,
                            "folderid": 30754773616,
                            "parentfolderid": 29925560641,
                            "name": "Documents",
                        }
                    }
                ).encode()
            elif parsed.path == "/listfolder" and query.get("folderid") == ["29925560641"]:
                body = json.dumps(
                    {
                        "metadata": {
                            "isfolder": True,
                            "folderid": 29925560641,
                            "parentfolderid": 0,
                            "name": "core",
                        }
                    }
                ).encode()
            else:
                body = json.dumps({"result": 2000, "error": "not found"}).encode()
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env = _base_env(
            tmp_path,
            {
                "PCLOUD_TOOLS_DIFFD_API_LONG_POLL_GATE": "operator-approved-api-long-poll-v1",
                "PCLOUD_TOOLS_PCLOUD_API_BASE_URL": f"http://127.0.0.1:{server.server_port}",
                "PCLOUD_TOOLS_PCLOUD_API_TOKEN": "topsecret-token",
                "PCLOUD_TOOLS_PCLOUD_API_TIMEOUT_SECONDS": "5",
            },
        )
        state_dir = _use_default_dev_state_dir(env)
        shadow_report = tmp_path / "shadow-validation-api-live-folder-metadata.json"
        shadow_workspace = tmp_path / "pcloud-shadow-validation-api-live-folder-metadata" / "workspace"
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
                "api-poll",
                "long-poll-run",
                "--report-path",
                str(shadow_report),
                "--operator-reviewed-preview",
                "--reviewer-approved-response-policy",
                "--reviewer-approved-credential-policy",
                "--reviewer-approved-process-policy",
                "--live-api",
                "--max-iterations",
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
    finally:
        server.shutdown()
        server.server_close()

    payload = _payload(result)
    remote_changes = json.loads((state_dir / "diffd" / "remote-changes.json").read_text())
    folder_cache = json.loads((state_dir / "diffd" / "folder-cache.json").read_text())

    assert result.returncode == 0
    assert payload["details"]["parsed diff changes"] == 1
    assert payload["details"]["invalid diff changes"] == 0
    assert payload["details"]["download records appended"] == 1
    assert payload["details"]["folder metadata requests count"] == 2
    assert remote_changes == [
        {"path": "Documents/IMG_001.jpeg", "action": "download", "reason": "diff:createfile"}
    ]
    assert folder_cache == {"29925560641": "", "30754773616": "Documents"}
    assert [request[0] for request in requests] == ["/diff", "/listfolder", "/listfolder"]
    assert "topsecret-token" not in result.stdout
def test_diffd_folder_cache_add_status_remove_is_dev_state_only(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = _use_default_dev_state_dir(env)
    cache_file = state_dir / "diffd" / "folder-cache.json"

    preview = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "diffd",
            "folder-cache",
            "add",
            "29913863697",
            "bench_test",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    add = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "diffd",
            "folder-cache",
            "add",
            "29913863697",
            "bench_test",
            "--execute",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    status = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "diffd",
            "folder-cache",
            "status",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    remove = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "diffd",
            "folder-cache",
            "remove",
            "29913863697",
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
    add_payload = _payload(add)
    status_payload = _payload(status)
    remove_payload = _payload(remove)

    assert preview.returncode == 0
    assert add.returncode == 0
    assert status.returncode == 0
    assert remove.returncode == 0
    assert preview_payload["details"]["state writes"] == "none"
    assert add_payload["details"]["state writes"] == "diffd folder cache"
    assert status_payload["details"]["entries"] == [
        {"folder_id": "29913863697", "path": "bench_test"}
    ]
    assert json.loads(cache_file.read_text()) == {}
    assert remove_payload["details"]["folder cache entries removed"] == 1
def test_diffd_api_long_poll_run_refuses_live_api_without_token(tmp_path: Path) -> None:
    env = _base_env(
        tmp_path,
        {"PCLOUD_TOOLS_DIFFD_API_LONG_POLL_GATE": "operator-approved-api-long-poll-v1"},
    )
    state_dir = _use_default_dev_state_dir(env)
    shadow_report = tmp_path / "shadow-validation-api-live-no-token.json"
    shadow_workspace = tmp_path / "pcloud-shadow-validation-api-live-no-token" / "workspace"
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
            "api-poll",
            "long-poll-run",
            "--report-path",
            str(shadow_report),
            "--operator-reviewed-preview",
            "--reviewer-approved-response-policy",
            "--reviewer-approved-credential-policy",
            "--reviewer-approved-process-policy",
            "--live-api",
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
    assert payload["details"]["live API requested"] == "yes"
    assert payload["details"]["API token provided"] == "no"
    assert payload["details"]["state writes"] == "none"
    assert "PCLOUD_TOOLS_PCLOUD_API_TOKEN" in [issue["key"] for issue in payload["issues"]]
    assert not (state_dir / "diffd" / "remote-changes.json").exists()
    assert not (state_dir / "daemon" / "diffid").exists()
def test_diffd_api_long_poll_run_executes_live_api_against_local_server(tmp_path: Path) -> None:
    requests: list[dict[str, list[str]]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            requests.append(urllib.parse.parse_qs(parsed.query))
            body = json.dumps(
                {
                    "diffid": "456",
                    "entries": [
                        {"path": "Documents/from-live-api.pdf", "event": "modified"},
                        {"path": "private/outside.pdf", "event": "modified"},
                    ],
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env = _base_env(
            tmp_path,
            {
                "PCLOUD_TOOLS_DIFFD_API_LONG_POLL_GATE": "operator-approved-api-long-poll-v1",
                "PCLOUD_TOOLS_PCLOUD_API_BASE_URL": f"http://127.0.0.1:{server.server_port}",
                "PCLOUD_TOOLS_PCLOUD_API_TOKEN": "topsecret-token",
                "PCLOUD_TOOLS_PCLOUD_API_TIMEOUT_SECONDS": "5",
            },
        )
        state_dir = _use_default_dev_state_dir(env)
        shadow_report = tmp_path / "shadow-validation-api-live-ok.json"
        shadow_workspace = tmp_path / "pcloud-shadow-validation-api-live-ok" / "workspace"
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
                "api-poll",
                "long-poll-run",
                "--report-path",
                str(shadow_report),
                "--operator-reviewed-preview",
                "--reviewer-approved-response-policy",
                "--reviewer-approved-credential-policy",
                "--reviewer-approved-process-policy",
                "--live-api",
                "--max-iterations",
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
    finally:
        server.shutdown()
        server.server_close()

    payload = _payload(result)
    remote_changes = json.loads((state_dir / "diffd" / "remote-changes.json").read_text())
    diffid = (state_dir / "daemon" / "diffid").read_text().strip()
    run_state = json.loads((state_dir / "diffd" / "api-long-poll-last-run.json").read_text())

    assert result.returncode == 0
    assert payload["status"] in {"ok", "warning"}
    assert payload["details"]["live API requested"] == "yes"
    assert payload["details"]["API token provided"] == "yes"
    assert "topsecret-token" not in result.stdout
    assert payload["details"]["API request URL"].endswith("auth=%3Credacted%3E")
    assert requests[0]["auth"] == ["topsecret-token"]
    assert requests[0]["diffid"] == ["0"]
    assert requests[0]["limit"] == ["100"]
    assert payload["details"]["download records appended"] == 1
    assert payload["details"]["skipped download records"] == 1
    assert remote_changes == [{"path": "Documents/from-live-api.pdf", "action": "download", "reason": "diff:modified"}]
    assert diffid == "456"
    assert run_state["live_api"] is True
    assert run_state["written_diffid"] == "456"
def test_diffd_api_long_poll_live_catchup_requires_separate_gate_and_iterates(
    tmp_path: Path,
) -> None:
    requests: list[dict[str, list[str]]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            requests.append(query)
            diffid = query.get("diffid", ["0"])[0]
            if diffid == "0":
                payload = {
                    "diffid": "100",
                    "entries": [{"path": "private/old.pdf", "event": "modified"}],
                }
            else:
                payload = {
                    "diffid": "200",
                    "entries": [{"path": "Documents/current.pdf", "event": "modified"}],
                }
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env = _base_env(
            tmp_path,
            {
                "PCLOUD_TOOLS_DIFFD_API_LONG_POLL_GATE": "operator-approved-api-long-poll-v1",
                "PCLOUD_TOOLS_DIFFD_API_CATCHUP_GATE": "operator-approved-api-catchup-v1",
                "PCLOUD_TOOLS_PCLOUD_API_BASE_URL": f"http://127.0.0.1:{server.server_port}",
                "PCLOUD_TOOLS_PCLOUD_API_TOKEN": "topsecret-token",
                "PCLOUD_TOOLS_PCLOUD_API_TIMEOUT_SECONDS": "5",
            },
        )
        state_dir = _use_default_dev_state_dir(env)
        shadow_report = tmp_path / "shadow-validation-api-live-catchup.json"
        shadow_workspace = tmp_path / "pcloud-shadow-validation-api-live-catchup" / "workspace"
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
                "api-poll",
                "long-poll-run",
                "--report-path",
                str(shadow_report),
                "--operator-reviewed-preview",
                "--reviewer-approved-response-policy",
                "--reviewer-approved-credential-policy",
                "--reviewer-approved-process-policy",
                "--reviewer-approved-catchup-policy",
                "--live-api",
                "--max-iterations",
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
    finally:
        server.shutdown()
        server.server_close()

    payload = _payload(result)
    remote_changes = json.loads((state_dir / "diffd" / "remote-changes.json").read_text())
    diffid = (state_dir / "daemon" / "diffid").read_text().strip()
    run_state = json.loads((state_dir / "diffd" / "api-long-poll-last-run.json").read_text())

    assert result.returncode == 0
    assert payload["details"]["catch-up requested"] == "yes"
    assert payload["details"]["catch-up gate env honored"] == "yes"
    assert payload["details"]["catch-up policy approval"] == "yes"
    assert payload["details"]["iterations processed"] == 2
    assert payload["details"]["download records appended"] == 1
    assert payload["details"]["skipped download records"] == 1
    assert [request["diffid"] for request in requests] == [["0"], ["100"]]
    assert remote_changes == [{"path": "Documents/current.pdf", "action": "download", "reason": "diff:modified"}]
    assert diffid == "200"
    assert run_state["iterations_processed"] == 2
    assert run_state["written_diffid"] == "200"
def test_diffd_api_checkpoint_uses_last_zero_and_writes_cursor_only(tmp_path: Path) -> None:
    requests: list[dict[str, list[str]]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            requests.append(urllib.parse.parse_qs(parsed.query))
            body = json.dumps({"diffid": "987", "entries": []}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env = _base_env(
            tmp_path,
            {
                "PCLOUD_TOOLS_DIFFD_API_CHECKPOINT_GATE": "operator-approved-api-checkpoint-v1",
                "PCLOUD_TOOLS_PCLOUD_API_BASE_URL": f"http://127.0.0.1:{server.server_port}",
                "PCLOUD_TOOLS_PCLOUD_API_TOKEN": "topsecret-token",
                "PCLOUD_TOOLS_PCLOUD_API_TIMEOUT_SECONDS": "5",
            },
        )
        state_dir = _use_default_dev_state_dir(env)
        stale_lock = state_dir / "diffd" / "api-long-poll.lock"
        stale_lock.mkdir(parents=True)
        old_timestamp = time.time() - 600
        os.utime(stale_lock, (old_timestamp, old_timestamp))
        shadow_report = tmp_path / "shadow-validation-api-checkpoint.json"
        shadow_workspace = tmp_path / "pcloud-shadow-validation-api-checkpoint" / "workspace"
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
                "api-poll",
                "checkpoint",
                "--report-path",
                str(shadow_report),
                "--operator-reviewed-checkpoint",
                "--reviewer-approved-checkpoint-policy",
                "--execute",
                "--json",
            ],
            check=False,
            capture_output=True,
            text=True,
            cwd=tmp_path,
            env=env,
        )
    finally:
        server.shutdown()
        server.server_close()

    payload = _payload(result)
    diffid = (state_dir / "daemon" / "diffid").read_text().strip()
    checkpoint_state = json.loads((state_dir / "diffd" / "api-checkpoint-last-run.json").read_text())

    assert result.returncode == 0
    assert payload["summary"] == "diffd API checkpoint completed"
    assert payload["details"]["checkpoint diffid"] == "987"
    assert payload["details"]["state writes"] == "diff cursor and checkpoint state only"
    assert payload["details"]["diffd API lock status"] == "released"
    assert payload["details"]["remote-change records appended"] == 0
    assert "topsecret-token" not in result.stdout
    assert requests == [{"last": ["0"], "auth": ["topsecret-token"]}]
    assert diffid == "987"
    assert checkpoint_state["written_diffid"] == "987"
    assert not (state_dir / "diffd" / "remote-changes.json").exists()
    assert not (state_dir / "diffd" / "api-long-poll.lock").exists()
def test_diffd_api_long_poll_run_records_failure_state_without_cursor_mutation(tmp_path: Path) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = b"temporary failure"
            self.send_response(503)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env = _base_env(
            tmp_path,
            {
                "PCLOUD_TOOLS_DIFFD_API_LONG_POLL_GATE": "operator-approved-api-long-poll-v1",
                "PCLOUD_TOOLS_PCLOUD_API_BASE_URL": f"http://127.0.0.1:{server.server_port}",
                "PCLOUD_TOOLS_PCLOUD_API_TOKEN": "topsecret-token",
                "PCLOUD_TOOLS_PCLOUD_API_TIMEOUT_SECONDS": "5",
            },
        )
        state_dir = _use_default_dev_state_dir(env)
        shadow_report = tmp_path / "shadow-validation-api-live-failure.json"
        shadow_workspace = tmp_path / "pcloud-shadow-validation-api-live-failure" / "workspace"
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
                "api-poll",
                "long-poll-run",
                "--report-path",
                str(shadow_report),
                "--operator-reviewed-preview",
                "--reviewer-approved-response-policy",
                "--reviewer-approved-credential-policy",
                "--reviewer-approved-process-policy",
                "--live-api",
                "--max-iterations",
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
    finally:
        server.shutdown()
        server.server_close()

    payload = _payload(result)
    run_state = json.loads((state_dir / "diffd" / "api-long-poll-last-run.json").read_text())

    assert result.returncode == 1
    assert payload["status"] == "error"
    assert payload["details"]["state writes"] == "diffd long-poll failure state"
    assert payload["details"]["failure state written"] == "yes"
    assert payload["details"]["written diffid"] == "-"
    assert run_state["written_diffid"] == "-"
    assert run_state["backoff_seconds"] == 60
    assert run_state["appended_records"] == []
    assert not (state_dir / "daemon" / "diffid").exists()
    assert not (state_dir / "diffd" / "remote-changes.json").exists()
def test_diffd_api_long_poll_run_uses_rclone_config_credentials(tmp_path: Path) -> None:
    requests: list[dict[str, list[str]]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            requests.append(urllib.parse.parse_qs(parsed.query))
            body = json.dumps(
                {
                    "diffid": "789",
                    "entries": [
                        {"path": "Documents/from-rclone-config.pdf", "event": "created"},
                    ],
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env = _base_env(
            tmp_path,
            {
                "PCLOUD_TOOLS_DIFFD_API_LONG_POLL_GATE": "operator-approved-api-long-poll-v1",
                "PCLOUD_TOOLS_CORE_REMOTE": "pcloud:core",
                "PCLOUD_TOOLS_PCLOUD_API_TIMEOUT_SECONDS": "5",
            },
        )
        state_dir = _use_default_dev_state_dir(env)
        rclone_config = tmp_path / "rclone.conf"
        env["RCLONE_CONFIG"] = str(rclone_config)
        rclone_config.write_text(
            "\n".join(
                [
                    "[pcloud]",
                    "type = pcloud",
                    f"hostname = http://127.0.0.1:{server.server_port}",
                    'token = {"access_token":"rclone-secret","token_type":"bearer","expiry":"0001-01-01T00:00:00Z"}',
                    "",
                    "[pcloud-crypt]",
                    "type = crypt",
                    "remote = pcloud:crypt",
                    "password = should-not-be-read",
                    "password2 = should-not-be-read",
                    "",
                ]
            )
        )
        shadow_report = tmp_path / "shadow-validation-api-rclone-ok.json"
        shadow_workspace = tmp_path / "pcloud-shadow-validation-api-rclone-ok" / "workspace"
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
                "api-poll",
                "long-poll-run",
                "--report-path",
                str(shadow_report),
                "--operator-reviewed-preview",
                "--reviewer-approved-response-policy",
                "--reviewer-approved-credential-policy",
                "--reviewer-approved-process-policy",
                "--live-api",
                "--max-iterations",
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
    finally:
        server.shutdown()
        server.server_close()

    payload = _payload(result)
    remote_changes = json.loads((state_dir / "diffd" / "remote-changes.json").read_text())

    assert result.returncode == 0
    assert payload["details"]["API credential source"] == "rclone config"
    assert payload["details"]["API auth parameter"] == "access_token"
    assert payload["details"]["API token provided"] == "yes"
    assert "rclone-secret" not in result.stdout
    assert "should-not-be-read" not in result.stdout
    assert requests[0]["access_token"] == ["rclone-secret"]
    assert remote_changes == [
        {"path": "Documents/from-rclone-config.pdf", "action": "download", "reason": "diff:created"}
    ]
def test_diffd_plan_suppresses_fresh_completed_upload_until_local_file_changes(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = _use_default_dev_state_dir(env)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    target = workspace / "Documents" / "uploaded.txt"
    target.parent.mkdir(parents=True)
    target.write_text("uploaded\n")
    pushd_dir = state_dir / "pushd"
    diffd_dir = state_dir / "diffd"
    pushd_dir.mkdir(parents=True)
    diffd_dir.mkdir(parents=True)
    (diffd_dir / "remote-changes.json").write_text(
        json.dumps([{"path": "Documents/uploaded.txt", "action": "download", "reason": "diff:createfile"}])
    )
    fingerprint = local_fingerprint(target).as_dict()
    completed_at = datetime.now(timezone.utc).isoformat()
    (pushd_dir / "upload-origin-journal.json").write_text(
        json.dumps(
            {
                "schema_version": "pcloud-tools-upload-origin-suppression.v1",
                "records": [
                    {
                        "path": "Documents/uploaded.txt",
                        "state": "completed",
                        "direction": "upload",
                        "started_at": completed_at,
                        "completed_at": completed_at,
                        "local_fingerprint": fingerprint,
                    }
                ],
            }
        )
    )

    suppressed = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "diffd", "preview", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    target.write_text("user edit after upload\n")
    allowed = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "diffd", "preview", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    suppressed_payload = _payload(suppressed)
    allowed_payload = _payload(allowed)
    assert suppressed.returncode == 0
    assert suppressed_payload["details"]["planned downloads"] == 0
    assert suppressed_payload["details"]["skipped download record details"][0]["reason"] == "upload origin journal"
    assert allowed.returncode == 0
    assert allowed_payload["details"]["planned downloads"] == 1
    (diffd_dir / "remote-changes.json").write_text(
        json.dumps([{"path": "Documents/uploaded.txt", "action": "download", "reason": "diff:modifyfile"}])
    )
    remote_edit = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "diffd", "preview", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    remote_edit_payload = _payload(remote_edit)
    assert remote_edit.returncode == 0
    assert remote_edit_payload["details"]["planned downloads"] == 1
    assert remote_edit_payload["details"]["planned download records"][0]["reason"] == "diff:modifyfile"
def test_diffd_remote_change_add_and_clear_are_dev_state_writes(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    state_dir = _use_default_dev_state_dir(env)
    remote_file = state_dir / "diffd" / "remote-changes.json"

    add = subprocess.run(
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
    assert add.returncode == 0
    assert json.loads(remote_file.read_text())[0]["path"] == "Documents/remote.pdf"
    assert _payload(add)["summary"] == "diffd remote-change record appended"

    preview_remove = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "diffd",
            "remote-change",
            "remove",
            "Documents/remote.pdf",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    assert preview_remove.returncode == 0
    assert json.loads(remote_file.read_text())[0]["path"] == "Documents/remote.pdf"
    assert _payload(preview_remove)["details"]["remote changes removed"] == 1

    remove = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "diffd",
            "remote-change",
            "remove",
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
    assert remove.returncode == 0
    assert json.loads(remote_file.read_text()) == []
    assert _payload(remove)["summary"] == "diffd remote-change records removed"

    add_again = subprocess.run(
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
    assert add_again.returncode == 0

    clear = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "diffd",
            "remote-change",
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
    assert json.loads(remote_file.read_text()) == []
    assert _payload(clear)["details"]["remote changes before"] == 1
