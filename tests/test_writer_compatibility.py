from __future__ import annotations

import json
from pathlib import Path

from conftest import _base_env, _write_workspace_file
from pcloud_tools.diffd_events import DiffdRemoteChange, diff_changes_to_records
from pcloud_tools.service_daemon_plan import (
    PlanRecord,
    append_plan_record_with_policy,
    classify_upload_candidates,
)
from pcloud_tools.transfer_state import consume_event_ids, ensure_event_ids, read_queue_snapshot


def test_legacy_queue_and_unknown_fields_survive_event_id_backfill(tmp_path: Path) -> None:
    queue_file = tmp_path / "queue.json"
    queue_file.write_text(
        json.dumps(
            [
                "Documents/legacy.txt",
                {
                    "path": "Documents/current.txt",
                    "action": "upload",
                    "reason": "fswatch:Updated",
                    "enqueued_at": "2026-09-08T00:00:00+00:00",
                    "watch_token": "keep-me",
                },
            ]
        )
    )

    before = read_queue_snapshot(queue_file)
    assert before.records[0].path == "Documents/legacy.txt"
    assert before.records[1].payload["watch_token"] == "keep-me"

    update = ensure_event_ids(queue_file, write=True)
    assert update.issue is None
    payload = json.loads(queue_file.read_text())
    assert payload[1]["watch_token"] == "keep-me"
    assert all(item["event_id"] for item in payload)

    first_id = payload[0]["event_id"]
    second_id = payload[1]["event_id"]
    queue_file.write_text(
        json.dumps(
            [
                payload[0],
                payload[1],
                {"path": "Documents/legacy.txt", "action": "upload", "reason": "new", "event_id": "new-id"},
            ]
        )
    )
    consumed = consume_event_ids(queue_file, [first_id])
    assert consumed.removed_event_ids == (first_id,)
    assert consumed.stale_event_ids == ()
    retained = json.loads(queue_file.read_text())
    assert [item["event_id"] for item in retained] == [second_id, "new-id"]


def test_same_path_action_observation_replaces_generation_without_losing_new_event(tmp_path: Path) -> None:
    queue_file = tmp_path / "queue.json"
    queue_file.write_text(
        json.dumps(
            [
                {
                    "path": "Documents/generation.txt",
                    "action": "upload",
                    "reason": "old",
                    "event_id": "old-event",
                    "enqueued_at": "2026-09-08T00:00:00+00:00",
                    "watch_token": "old-token",
                }
            ]
        )
    )

    result = append_plan_record_with_policy(
        queue_file,
        "PCLOUD_TOOLS_PUSHD_QUEUE",
        PlanRecord(
            "Documents/generation.txt",
            "upload",
            "new",
            event_id="new-event",
            extra={"watch_token": "new-token", "source": "watcher"},
        ),
        max_records=1,
        enqueued_at="2026-09-08T00:05:00+00:00",
    )

    assert result.appended is True
    assert result.skipped_reason == "replaced generation"
    payload = json.loads(queue_file.read_text())
    assert payload == [
        {
            "path": "Documents/generation.txt",
            "action": "upload",
            "reason": "new",
            "event_id": "new-event",
            "enqueued_at": "2026-09-08T00:00:00+00:00",
            "watch_token": "new-token",
            "source": "watcher",
            "observed_at": payload[0]["observed_at"],
        }
    ]
    consumed = consume_event_ids(queue_file, ["old-event"])
    assert consumed.removed_event_ids == ()
    assert consumed.stale_event_ids == ("old-event",)
    assert json.loads(queue_file.read_text())[0]["event_id"] == "new-event"


def test_diffid_is_preserved_as_remote_change_metadata() -> None:
    records = diff_changes_to_records(
        (DiffdRemoteChange(path="Documents/remote.txt", event="modified", diffid="123", raw="fixture"),)
    )
    assert records[0].extra == {"diffid": "123"}


def test_read_only_candidate_classification_does_not_create_state(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    config = type(
        "Config",
        (),
        {
            "core_dir": workspace,
            "state_dir": Path(env["PCLOUD_TOOLS_STATE_DIR"]),
            "pushd_upload_settle_seconds": 0,
            "default_excludes": (),
        },
    )()
    _write_workspace_file(env, "Documents/read-only.txt", "data\n")

    result = classify_upload_candidates(
        config,
        (PlanRecord("Documents/read-only.txt", "upload", "test"),),
        write=False,
    )

    assert result.ready_records[0].path == "Documents/read-only.txt"
    assert not (config.state_dir / "pushd").exists()
