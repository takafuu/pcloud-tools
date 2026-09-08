from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import _base_env, _install_real_rclone_stub, _use_default_dev_state_dir
from pcloud_tools.download_review import mark_missing_source, missing_remote_source, review_reason, discard_missing_source, discard_legacy_missing_reviews


@pytest.mark.parametrize(
    "direction,code,stderr,expected",
    [
        ("download", 3, "error reading source root directory: directory not found", True),
        ("download", 4, "failed to open source object: object not found", True),
        ("download", 3, "error reading destination directory: directory not found", False),
        ("download", 4, "object not found", False),
        ("download", 5, "error reading source root directory: directory not found", False),
        ("upload", 3, "error reading source root directory: directory not found", False),
        ("download", 3, "permission denied", False),
    ],
)
def test_missing_source_requires_source_diagnostic(direction, code, stderr, expected):
    result = {"direction": direction, "returncode": code, "stderr": stderr}
    assert missing_remote_source(result) is expected
    assert not missing_remote_source({**result, "requires_child_exit_confirmation": True})


def test_old_failure_cannot_mark_a_new_queue_generation(tmp_path: Path):
    queue = tmp_path / "remote-changes.json"
    queue.write_text(json.dumps([{"path": "Documents/a.txt", "event_id": "new", "action": "download"}]))
    before = queue.read_bytes()
    assert mark_missing_source(queue, {"path": "Documents/a.txt", "event_id": "old", "returncode": 3}) == 0
    assert queue.read_bytes() == before
    assert review_reason("new", {"download_review": {"event_id": "old", "reason": "remote-source-missing"}}) == ""


def test_missing_source_retires_only_old_request_without_local_mutation(tmp_path: Path):
    env = _base_env(tmp_path)
    state = _use_default_dev_state_dir(env)
    _install_real_rclone_stub(env)
    stub = Path(env["PCLOUD_TOOLS_RCLONE_BIN"])
    stub.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$REAL_RCLONE_STUB_LOG\"\n"
        "case \"$2\" in\n"
        "  *missing.txt) printf 'error reading source root directory: directory not found\\n' >&2; exit 3 ;;\n"
        "esac\n"
        "mkdir -p \"$(dirname \"$3\")\"\n"
        "printf 'downloaded\\n' > \"$3\"\n"
    )
    env.update({
        "PCLOUD_TOOLS_REAL_TRANSFER_EXECUTION_GATE": "operator-approved-real-transfer-v1",
        "PCLOUD_TOOLS_REAL_TRANSFER_AUTOMATION_GATE": "operator-approved-real-transfer-automation-v1",
        "PCLOUD_TOOLS_REAL_TRANSFER_AUTOMATION_RUN_GATE": "operator-approved-real-transfer-automation-run-v1",
        "PCLOUD_TOOLS_CHAT_NOTIFY_ENABLED": "0",
    })
    directory = state / "diffd"
    directory.mkdir(parents=True)
    queue = directory / "remote-changes.json"
    original = {"path": "Documents/missing.txt", "event_id": "missing-event", "action": "download", "reason": "fixture", "unknown": "preserved"}
    queue.write_text(json.dumps([original, {"path": "Documents/present.txt", "event_id": "present-event", "action": "download", "reason": "fixture"}]))
    local = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"]) / "Documents/missing.txt"
    local.parent.mkdir(parents=True)
    local.write_text("local content must remain\n")
    fingerprint = (local.stat().st_size, local.stat().st_mtime_ns)
    report = tmp_path / "shadow-validation-review.json"
    workspace = tmp_path / "pcloud-shadow-validation-review/workspace"
    report.write_text(json.dumps({
        "status": "ok", "workspace": str(workspace), "state_dir": str(workspace / ".dev-state/state"),
        "checks": [{"name": name, "status": "ok"} for name in ("temporary workspace guard", "temporary state dir guard", "unsafe state dir guard")],
    }))

    def run(*args):
        result = subprocess.run([sys.executable, "-m", "pcloud_tools.cli", "diffd", "transfer", *args, "--json"], env=env, cwd=tmp_path, capture_output=True, text=True)
        return result.returncode, json.loads(result.stdout)

    command = ("automation-run", "--report-path", str(report), "--max-records", "1", "--execute", "--consume-on-success")
    code, first = run(*command)
    assert code == 0, first
    assert not any(issue["key"] in {"PCLOUD_TOOLS_TRANSFER_EXEC", "PCLOUD_TOOLS_DIFFD_TRANSFER_MANUAL_REVIEW"} for issue in first["issues"])
    assert first["details"]["performance"]["succeeded"] == 0
    assert first["details"]["performance"]["failed"] == 0
    assert first["details"]["performance"]["obsolete"] == 1
    assert first["details"]["performance"]["conflict"] == 0
    assert first["details"]["chat notify results"] == []
    assert [r["event_id"] for r in json.loads(queue.read_text())] == ["present-event"]
    assert local.read_text() == "local content must remain\n"
    assert (local.stat().st_size, local.stat().st_mtime_ns) == fingerprint

    code, second = run(*command)
    assert code == 0, second
    assert second["details"]["performance"]["succeeded"] == 1
    assert json.loads(queue.read_text()) == []
    assert (local.parent / "present.txt").read_text() == "downloaded\n"

    # Old confirmed-missing holds migrate on an admitted automatic tick.
    legacy = {**original, "download_review": {"event_id": "missing-event", "reason": "remote-source-missing", "returncode": 3}}
    queue.write_text(json.dumps([legacy]))
    before = queue.read_bytes()
    code, preview = run("automation-run", "--report-path", str(report), "--max-records", "1", "--consume-on-success")
    assert queue.read_bytes() == before
    log = Path(env["REAL_RCLONE_STUB_LOG"]).read_bytes()
    code, migrated = run(*command)
    assert code == 0, migrated
    assert migrated["details"]["obsolete download cleanup"]["discarded"] == 1
    assert json.loads(queue.read_text()) == []
    assert Path(env["REAL_RCLONE_STUB_LOG"]).read_bytes() == log
    assert local.read_text() == "local content must remain\n"


def test_obsolete_discard_preserves_new_generation_and_unrelated_records(tmp_path):
    queue = tmp_path / "remote-changes.json"
    new = {"path": "a.txt", "action": "download", "event_id": "new", "custom": 42}
    queue.write_text(json.dumps([new]))
    before = queue.read_bytes()
    result = {"path": "a.txt", "event_id": "old", "direction": "download", "returncode": 3,
              "stderr": "error reading source root directory: directory not found"}
    assert discard_missing_source(queue, result) == 0
    assert queue.read_bytes() == before
    for failure in [{"returncode": 5}, {"stderr": "permission denied"}, {"timed_out": True}, {"requires_child_exit_confirmation": True}]:
        assert discard_missing_source(queue, {**result, "event_id": "new", **failure}) == 0
        assert queue.read_bytes() == before


def test_legacy_cleanup_keeps_conflicts_and_mismatched_review_generations(tmp_path):
    queue = tmp_path / "remote-changes.json"
    marker = {"reason": "remote-source-missing", "event_id": "old", "returncode": 3}
    stale = {"path": "a", "event_id": "new", "download_review": marker}
    conflict = {"path": "b", "event_id": "b", "download_review": {"reason": "conflict", "event_id": "b"}}
    deletion = {"path": "c", "event_id": "old", "action": "delete", "download_review": marker}
    removable = {"path": "d", "event_id": "old", "action": "download", "download_review": marker}
    queue.write_text(json.dumps([stale, conflict, deletion, removable]))
    assert discard_legacy_missing_reviews(queue) == [removable]
    assert json.loads(queue.read_text()) == [stale, conflict, deletion]
