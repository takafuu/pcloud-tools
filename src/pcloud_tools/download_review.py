"""Keep a missing remote source for review without consuming its event."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .download_suppression import normalize_plan_path
from .io_utils import atomic_write_json
from .transfer_state import TransferStateError, writer_state_lock


REVIEW_FIELD = "download_review"
MISSING_SOURCE_REASON = "remote source missing; retained for manual review"


def missing_remote_source(result: dict[str, object]) -> bool:
    """Do not confuse a destination, auth, or network failure with a missing source.

    rclone exit 3/4 alone is insufficient: they also cover destination errors.
    Require an explicit source-side diagnostic from the failed copyto.
    """

    if result.get("direction") != "download" or result.get("returncode") not in {3, 4}:
        return False
    if result.get("timed_out") or result.get("requires_child_exit_confirmation"):
        return False
    output = str(result.get("stderr") or "").lower()
    return any(
        diagnostic in output
        for diagnostic in (
            "error reading source root directory: directory not found",
            "failed to open source object: object not found",
            "source object not found",
        )
    )


def review_reason(event_id: str | None, extra: dict[str, Any] | None) -> str:
    marker = (extra or {}).get(REVIEW_FIELD)
    if (
        event_id
        and isinstance(marker, dict)
        and marker.get("event_id") == event_id
        and marker.get("reason") == "remote-source-missing"
    ):
        return MISSING_SOURCE_REASON
    return ""


def read_review_queue(path: Path) -> list[Any]:
    try:
        payload = json.loads(path.read_text())
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as exc:
        raise TransferStateError(f"cannot read download review queue: {exc}") from exc
    if not isinstance(payload, list):
        raise TransferStateError("download review queue must contain a list")
    return payload


def review_records(path: Path) -> list[dict[str, object]]:
    return [
        {
            "path": item.get("path"),
            "event_id": item.get("event_id"),
            "reason": MISSING_SOURCE_REASON,
            "review": item[REVIEW_FIELD],
        }
        for item in read_review_queue(path)
        if isinstance(item, dict) and review_reason(item.get("event_id"), item)
    ]


def mark_missing_source(path: Path, result: dict[str, object]) -> int:
    event_id = str(result.get("event_id") or "")
    target = normalize_plan_path(result.get("path"))
    if not event_id or not target:
        return 0
    with writer_state_lock(path):
        payload = read_review_queue(path)
        marked = 0
        for item in payload:
            if not isinstance(item, dict):
                continue
            if item.get("event_id") != event_id or normalize_plan_path(item.get("path")) != target:
                continue
            item[REVIEW_FIELD] = {
                "event_id": event_id,
                "reason": "remote-source-missing",
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "returncode": result.get("returncode"),
            }
            marked += 1
        if marked:
            atomic_write_json(path, payload)
        return marked


def retry_review(path: Path, target: str, event_id: str, *, execute: bool) -> int:
    """Remove only the matching review marker; a later tick rechecks the event."""

    import contextlib

    with writer_state_lock(path) if execute else contextlib.nullcontext():
        payload = read_review_queue(path)
        matched = 0
        for item in payload:
            if not isinstance(item, dict):
                continue
            if (
                normalize_plan_path(item.get("path")) == normalize_plan_path(target)
                and item.get("event_id") == event_id
                and review_reason(event_id, item)
            ):
                item.pop(REVIEW_FIELD)
                matched += 1
        if execute and matched:
            atomic_write_json(path, payload)
        return matched
