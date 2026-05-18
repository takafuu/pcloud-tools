from __future__ import annotations

from conftest import *


def test_pushd_backfill_preview_classifies_existing_files_without_queue_writes(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    (workspace / ".pcloud-sync-allowlist").write_text("/\n")

    _write_workspace_file(env, "dev/foo.txt", "upload\n")
    _write_workspace_file(env, ".venv/package.py", "venv\n")
    _write_workspace_file(env, ".git/config", "git\n")
    _write_workspace_file(env, ".env", "SECRET=local\n")
    _write_workspace_file(env, ".hidden", "hidden\n")
    _write_workspace_file(env, "LLM/prompt.md", "local prompt\n")
    queue_file = state_dir / "pushd" / "queue.json"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "backfill",
            "preview",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    details = payload["details"]
    planned_paths = {record["path"] for record in details["planned upload records"]}
    excluded_paths = {record["path"] for record in details["excluded file records"]}
    pruned_paths = {record["path"] for record in details["pruned directory records"]}

    assert result.returncode == 0
    assert payload["command"] == "pushd backfill preview"
    assert payload["summary"] == "pushd backfill preview is ready"
    assert details["state writes"] == "none"
    assert details["candidate files"] == 4
    assert details["candidate paths"] == 7
    assert details["pruned directories"] == 3
    assert details["planned uploads"] == 1
    assert details["excluded files"] == 3
    assert details["invalid files"] == 0
    assert planned_paths == {"dev/foo.txt"}
    assert {".env", ".hidden", ".pcloud-sync-allowlist"}.issubset(excluded_paths)
    assert {".venv/", ".git/", "LLM/"}.issubset(pruned_paths)
    assert not queue_file.exists()


def test_pushd_backfill_preview_does_not_modify_existing_queue(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
    (workspace / ".pcloud-sync-allowlist").write_text("/\n")
    _write_workspace_file(env, "dev/foo.txt", "upload\n")

    pushd_dir = state_dir / "pushd"
    pushd_dir.mkdir(parents=True)
    queue_file = pushd_dir / "queue.json"
    original_queue = [{"path": "Documents/existing.txt", "action": "upload", "reason": "test"}]
    queue_file.write_text(json.dumps(original_queue))

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "backfill",
            "preview",
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
    assert json.loads(queue_file.read_text()) == original_queue
