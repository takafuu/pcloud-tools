from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import _base_env, _install_fake_rclone, _minimal_journal_config, _use_default_dev_state_dir
from pcloud_tools.cli_service_daemon import _finalize_download_transfer
from pcloud_tools.download_suppression import (
    local_fingerprint,
    mark_download_completed as real_mark_download_completed,
    read_download_suppression_journal,
)
from pcloud_tools.transfer_recovery import inspect_recovery, recover_attempt, recovery_issues
from pcloud_tools.transfer_state import (
    consume_event_ids,
    create_attempt,
    read_attempts,
    update_attempt,
    TransferStateError,
    writer_cutover_session,
    writer_lifetime_lock_status,
    writer_state_lock,
)


def test_recovery_requires_explicit_checks_and_never_consumes_queue(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    queue_file = state_dir / "pushd" / "queue.json"
    queue_file.parent.mkdir(parents=True)
    queue_file.write_text(json.dumps([{"path": "Documents/held.txt", "event_id": "evt-1"}]))
    created = create_attempt(
        state_dir,
        "pushd",
        [{"path": "Documents/held.txt", "event_id": "evt-1"}],
        concurrency=2,
    )
    assert created.issue is None
    update_attempt(
        state_dir,
        "pushd",
        created.attempt_id,
        phase="child-uncertain",
        status="held",
        child_pids=[12345],
        hold_reason="child exit could not be confirmed",
    )

    preview = inspect_recovery(state_dir, "pushd")
    assert len(preview.candidates) == 1
    candidate = preview.candidates[0]
    assert candidate.attempt_id == created.attempt_id
    assert candidate.phase == "child-uncertain"
    assert candidate.requires_child_exit_confirmation is True
    assert any("child exit could not be confirmed" in issue.message for issue in recovery_issues(preview))

    blocked = recover_attempt(
        state_dir,
        "pushd",
        created.attempt_id,
        child_exit_confirmed=False,
        writers_stopped=True,
        latest_event_ids_rechecked=True,
        local_fingerprints_rechecked=True,
    )
    assert blocked.status == "blocked"
    assert blocked.issue is not None
    assert read_attempts(state_dir, "pushd")[0]["status"] == "held"
    assert json.loads(queue_file.read_text())[0]["event_id"] == "evt-1"

    blocked_writers = recover_attempt(
        state_dir,
        "pushd",
        created.attempt_id,
        child_exit_confirmed=True,
        writers_stopped=False,
        latest_event_ids_rechecked=True,
        local_fingerprints_rechecked=True,
    )
    assert blocked_writers.status == "blocked"
    assert blocked_writers.issue is not None
    assert "all queue/journal writers stopped" in blocked_writers.issue.message

    released = recover_attempt(
        state_dir,
        "pushd",
        created.attempt_id,
        child_exit_confirmed=True,
        writers_stopped=True,
        latest_event_ids_rechecked=True,
        local_fingerprints_rechecked=True,
    )
    assert released.issue is None
    assert released.status == "released"
    assert inspect_recovery(state_dir, "pushd").candidates == ()
    assert json.loads(queue_file.read_text())[0]["event_id"] == "evt-1"


def _download_boundary_item(config, *, event_id: str, staging_path: Path, final_path: Path) -> dict[str, object]:
    return {
        "path": "Documents/boundary.txt",
        "direction": "download",
        "event_id": event_id,
        "staging_path": str(staging_path),
        "final_path": str(final_path),
        "pre_transfer_fingerprint": local_fingerprint(final_path).as_dict(),
    }


def test_download_termination_boundaries_preserve_new_event_and_user_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise both crash windows around replacement, journal, and consume."""

    config = _minimal_journal_config(tmp_path)
    config.core_dir.mkdir(parents=True, exist_ok=True)
    state_dir = config.state_dir
    diffd_dir = state_dir / "diffd"
    diffd_dir.mkdir(parents=True)
    queue_file = diffd_dir / "remote-changes.json"
    final_path = config.core_dir / "Documents" / "boundary.txt"
    final_path.parent.mkdir(parents=True)

    # Crash after replace and before the completion journal. The destination
    # has the downloaded bytes, but no receipt exists and the queue remains.
    final_path.write_text("before replace crash\n")
    first_staging = diffd_dir / "download-staging" / "old-event.staging"
    first_staging.parent.mkdir(parents=True)
    first_staging.write_text("remote old generation\n")
    queue_file.write_text(json.dumps([{"path": "Documents/boundary.txt", "event_id": "old-event"}]))
    first_item = _download_boundary_item(
        config, event_id="old-event", staging_path=first_staging, final_path=final_path
    )

    def terminate_before_journal(*args, **kwargs):
        raise SystemExit("injected after replace before journal")

    monkeypatch.setattr("pcloud_tools.cli_service_daemon.mark_download_completed", terminate_before_journal)
    with pytest.raises(SystemExit, match="after replace before journal"):
        _finalize_download_transfer(config, first_item)
    assert final_path.read_text() == "remote old generation\n"
    assert not first_staging.exists()
    assert not (diffd_dir / "download-suppression-journal.json").exists()

    # A user edit and a newer remote event arrive before restart. Recovery only
    # releases the held attempt; it never manufactures a receipt or consumes.
    final_path.write_text("user edit after replace crash\n")
    queue_file.write_text(
        json.dumps(
            [
                {"path": "Documents/boundary.txt", "event_id": "old-event"},
                {"path": "Documents/boundary.txt", "event_id": "new-event"},
            ]
        )
    )
    held = create_attempt(state_dir, "diffd", [first_item], concurrency=1)
    update_attempt(
        state_dir,
        "diffd",
        held.attempt_id,
        phase="replace-before-journal",
        status="held",
        child_pids=[],
        hold_reason="injected termination after download replace before journal",
    )
    released = recover_attempt(
        state_dir,
        "diffd",
        held.attempt_id,
        child_exit_confirmed=True,
        writers_stopped=True,
        latest_event_ids_rechecked=True,
        local_fingerprints_rechecked=True,
    )
    assert released.status == "released"
    assert final_path.read_text() == "user edit after replace crash\n"
    assert [item["event_id"] for item in json.loads(queue_file.read_text())] == [
        "old-event",
        "new-event",
    ]

    # Crash after journal persistence and before queue consume. Restart may
    # consume only the selected old generation; the new event and user edit
    # remain intact.
    final_path.write_text("before journal crash\n")
    second_staging = diffd_dir / "download-staging" / "old-event-2.staging"
    second_staging.write_text("remote journal generation\n")
    second_item = _download_boundary_item(
        config, event_id="old-event-2", staging_path=second_staging, final_path=final_path
    )
    def terminate_after_journal(*args, **kwargs):
        real_mark_download_completed(*args, **kwargs)
        raise SystemExit("injected after journal before consume")

    monkeypatch.setattr("pcloud_tools.cli_service_daemon.mark_download_completed", terminate_after_journal)
    with pytest.raises(SystemExit, match="after journal before consume"):
        _finalize_download_transfer(config, second_item)
    journal = read_download_suppression_journal(config)
    assert journal.records[-1].path == "Documents/boundary.txt"
    assert journal.records[-1].state == "completed"
    final_path.write_text("user edit after journal crash\n")
    queue_file.write_text(
        json.dumps(
            [
                {"path": "Documents/boundary.txt", "event_id": "old-event-2"},
                {"path": "Documents/boundary.txt", "event_id": "new-event-2"},
            ]
        )
    )
    held_after_journal = create_attempt(state_dir, "diffd", [second_item], concurrency=1)
    update_attempt(
        state_dir,
        "diffd",
        held_after_journal.attempt_id,
        phase="journal-saved-before-consume",
        status="held",
        child_pids=[],
        hold_reason="injected termination after journal before consume",
    )
    released_after_journal = recover_attempt(
        state_dir,
        "diffd",
        held_after_journal.attempt_id,
        child_exit_confirmed=True,
        writers_stopped=True,
        latest_event_ids_rechecked=True,
        local_fingerprints_rechecked=True,
    )
    assert released_after_journal.status == "released"
    consumed = consume_event_ids(queue_file, ["old-event-2"])
    assert consumed.removed_event_ids == ("old-event-2",)
    assert [item["event_id"] for item in json.loads(queue_file.read_text())] == ["new-event-2"]
    assert final_path.read_text() == "user edit after journal crash\n"


def test_cutover_fixture_requires_every_old_writer_stopped_before_new_writer(
    tmp_path: Path,
) -> None:
    """Exercise watcher/poller/executor/manual/backfill stop and mixed-version gates."""

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    roles = ("watcher", "poller", "executor", "manual", "backfill")
    writer_paths = {
        role: state_dir / "diffd" / "writer-locks" / f"{role}.lock" for role in roles
    }
    child_code = """
import sys
import time
from pathlib import Path
from pcloud_tools.transfer_state import TransferStateError, writer_state_lock

target = Path(sys.argv[1])
ready = Path(sys.argv[2])
release = Path(sys.argv[3])
manifest = Path(sys.argv[4])
version = sys.argv[5]
mode = sys.argv[6]
target.parent.mkdir(parents=True, exist_ok=True)
manifest.parent.mkdir(parents=True, exist_ok=True)
if mode == "probe":
    try:
        with writer_state_lock(target, blocking=False):
            ready.write_text("acquired")
    except TransferStateError:
        ready.write_text("blocked")
    raise SystemExit(0)
with writer_state_lock(target, blocking=False):
    manifest.write_text(version)
    ready.write_text(version)
    while not release.exists():
        time.sleep(0.01)
"""

    def active_writer_roles() -> list[str]:
        active: list[str] = []
        for role, target in writer_paths.items():
            try:
                with writer_state_lock(target, blocking=False):
                    pass
            except Exception:
                active.append(role)
        return active

    for role, target in writer_paths.items():
        ready = tmp_path / f"{role}.ready"
        release = tmp_path / f"{role}.release"
        manifest = tmp_path / "writer-manifest" / f"{role}.version"
        old = subprocess.Popen(
            [
                sys.executable,
                "-c",
                child_code,
                str(target),
                str(ready),
                str(release),
                str(manifest),
                "old",
                "hold",
            ],
            env={"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
        )
        try:
            for _ in range(300):
                if ready.exists():
                    break
                time.sleep(0.01)
            assert ready.read_text() == "old"
            assert manifest.read_text() == "old"
            # One shared cutover barrier blocks every queue/journal writer,
            # even when each role would otherwise use a different file lock.
            assert active_writer_roles() == list(roles)

            new_ready = tmp_path / f"{role}.new.ready"
            probe_target = writer_paths["poller" if role != "poller" else "watcher"]
            new = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    child_code,
                    str(probe_target),
                    str(new_ready),
                    str(release),
                    str(manifest),
                    "new",
                    "probe",
                ],
                check=False,
                env={"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
            )
            assert new.returncode == 0
            assert new_ready.read_text() == "blocked"
            assert manifest.read_text() == "old"
        finally:
            release.write_text("stop old writer")
            assert old.wait(timeout=5) == 0
        assert active_writer_roles() == []

    # The cutover fixture grants update/downgrade permission only after every
    # representative writer lock is observed free.
    (state_dir / "cutover-approved").write_text("old and new writers stopped")
    assert (state_dir / "cutover-approved").read_text() == "old and new writers stopped"


def test_cutover_session_blocks_active_writer_and_replacement_process(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    target = state_dir / "diffd" / "remote-changes.json"

    with writer_state_lock(target, blocking=False):
        with pytest.raises(TransferStateError, match="all writer processes"):
            with writer_cutover_session(state_dir, "diffd", blocking=False):
                pass

    assert writer_lifetime_lock_status(state_dir, "diffd")["active"] is False
    probe_code = """
import sys
from pathlib import Path
from pcloud_tools.transfer_state import TransferStateError, writer_state_lock
target = Path(sys.argv[1])
ready = Path(sys.argv[2])
try:
    with writer_state_lock(target, blocking=False):
        ready.write_text("acquired")
except TransferStateError:
    ready.write_text("blocked")
"""
    ready = tmp_path / "replacement.ready"
    with writer_cutover_session(state_dir, "diffd", blocking=False):
        assert writer_lifetime_lock_status(state_dir, "diffd")["active"] is True
        probe = subprocess.run(
            [sys.executable, "-c", probe_code, str(target), str(ready)],
            check=False,
            env={"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
        )
        assert probe.returncode == 0
        assert ready.read_text() == "blocked"

    with writer_state_lock(target, blocking=False):
        pass
    assert writer_lifetime_lock_status(state_dir, "diffd")["active"] is False


@pytest.mark.parametrize("boundary", ("before-journal", "after-journal"))
def test_cli_recovery_boundary_blocks_restart_and_preserves_edit(
    tmp_path: Path,
    boundary: str,
) -> None:
    """Exercise an actual executor crash and the explicit recovery route."""

    env = _base_env(tmp_path)
    state_dir = _use_default_dev_state_dir(env)
    fake_log = _install_fake_rclone(env)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    relative = "Documents/boundary.txt"
    local = workspace / relative
    local.parent.mkdir(parents=True)
    local.write_text("original local bytes\n")
    queue_file = state_dir / "diffd" / "remote-changes.json"
    queue_file.parent.mkdir(parents=True)
    queue_file.write_text(
        json.dumps(
            [
                {
                    "path": relative,
                    "action": "download",
                    "reason": "fixture",
                    "event_id": "original-event",
                }
            ]
        )
    )

    mark_completed_body = "original(*args,**kwargs)" if boundary == "after-journal" else "pass"
    boot = "\n".join(
        [
            "import os,sys",
            "import pcloud_tools.cli_service_daemon as daemon",
            "from pcloud_tools.cli import main",
            "original=daemon.mark_download_completed",
            "def crash(*args,**kwargs):",
            f"    {mark_completed_body}",
            "    os._exit(72)",
            "daemon.mark_download_completed=crash",
            "sys.argv=['pcloud-manager','diffd','transfer','executor-run','--execute','--consume-on-success','--json']",
            "raise SystemExit(main())",
        ]
    )
    crashed = subprocess.run(
        [sys.executable, "-c", boot],
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert crashed.returncode == 72
    assert local.read_text() == "fake download\n"
    attempt_file = state_dir / "diffd" / "transfer-attempts.json"
    assert attempt_file.exists()
    crashed_attempt = json.loads(attempt_file.read_text())[0]
    assert crashed_attempt["status"] == "in_progress"
    assert crashed_attempt["requires_child_exit_confirmation"] is True
    if boundary == "after-journal":
        journal = json.loads(
            (state_dir / "diffd" / "download-suppression-journal.json").read_text()
        )
        assert journal["records"][-1]["state"] == "completed"

    def cli(*arguments: str) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
        result = subprocess.run(
            [sys.executable, "-m", "pcloud_tools.cli", *arguments, "--json"],
            cwd=tmp_path,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result, json.loads(result.stdout)

    local.write_text("user edit after abrupt exit\n")
    added, _ = cli("diffd", "remote-change", "add", relative, "--execute")
    assert added.returncode == 0
    observed_queue = queue_file.read_bytes()
    assert any(
        item.get("event_id") != "original-event"
        for item in json.loads(observed_queue)
    )
    upload_added, _ = cli("pushd", "queue", "add", relative, "--execute")
    assert upload_added.returncode == 0

    restarted, restart_payload = cli(
        "diffd",
        "transfer",
        "executor-run",
        "--execute",
        "--consume-on-success",
    )
    assert restarted.returncode != 0
    assert restart_payload["details"]["state writes"] == "none"
    assert len(fake_log.read_text().splitlines()) == 1
    assert queue_file.read_bytes() == observed_queue
    assert local.read_text() == "user edit after abrupt exit\n"
    held_bytes = attempt_file.read_bytes()

    _, preview = cli("diffd", "transfer", "recovery", "preview")
    candidates = preview["details"]["attempts"]
    assert len(candidates) == 1
    assert candidates[0]["requires child exit confirmation"] is True

    common = (
        "diffd",
        "transfer",
        "recovery",
        "run",
        "--execute",
        "--attempt-id",
        candidates[0]["attempt_id"],
        "--writers-stopped",
        "--latest-event-ids-rechecked",
        "--local-fingerprints-rechecked",
    )
    refused, refused_payload = cli(*common)
    assert refused.returncode != 0
    assert attempt_file.read_bytes() == held_bytes
    assert any(
        "child exit confirmation" in item["message"]
        for item in refused_payload["issues"]
    )

    recovered, recovery_payload = cli(*common, "--child-exit-confirmed")
    assert recovered.returncode == 0
    assert recovery_payload["details"]["attempt recovery status"] == "released"
    assert queue_file.read_bytes() == observed_queue
    assert local.read_text() == "user edit after abrupt exit\n"

    restarted_after_recovery, after_recovery_payload = cli(
        "diffd",
        "transfer",
        "executor-run",
        "--execute",
        "--consume-on-success",
    )
    assert restarted_after_recovery.returncode != 0
    assert after_recovery_payload["details"]["state writes"] == "none"
    assert len(fake_log.read_text().splitlines()) == 1
    assert queue_file.read_bytes() == observed_queue
    assert local.read_text() == "user edit after abrupt exit\n"
