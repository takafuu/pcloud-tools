from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import AppConfig, ConfigIssue
from .daemon_state import DaemonState
from .download_suppression import LocalFingerprint, download_suppression_match, local_fingerprint, upload_origin_match
from .io_utils import atomic_write_json, atomic_write_text
from .manager_ignore import manager_ignore_match
from .service_daemon_state import ServiceDaemonState
from .sync_scope import SyncScopeInfo, sync_allowlist_info


@dataclass(frozen=True)
class PlanRecord:
    path: str
    action: str
    reason: str


@dataclass(frozen=True)
class PushdPlan:
    queue_file: Path
    total: int
    upload_count: int
    excluded_count: int
    invalid_count: int
    upload_records: tuple[PlanRecord, ...]
    excluded_records: tuple[PlanRecord, ...]
    invalid_records: tuple[PlanRecord, ...]
    issues: tuple[ConfigIssue, ...]


@dataclass(frozen=True)
class DiffdPlan:
    remote_changes_file: Path
    pending_downloads_file: Path
    remote_change_count: int
    pending_download_count: int
    download_count: int
    skipped_count: int
    remote_change_records: tuple[PlanRecord, ...]
    pending_download_records: tuple[PlanRecord, ...]
    download_records: tuple[PlanRecord, ...]
    skipped_records: tuple[PlanRecord, ...]
    issues: tuple[ConfigIssue, ...]


@dataclass(frozen=True)
class PlanUpdateResult:
    file: Path
    before_count: int
    after_count: int
    issue: ConfigIssue | None = None


@dataclass(frozen=True)
class PlanAppendPolicyResult:
    file: Path
    before_count: int
    after_count: int
    appended: bool
    skipped_reason: str
    issue: ConfigIssue | None = None


@dataclass(frozen=True)
class DryRunStateResult:
    last_plan_file: Path
    last_event_file: Path
    cursor_file: Path
    cursor: str


@dataclass(frozen=True)
class MissingLocalQueueCleanupResult:
    file: Path
    before_count: int
    after_count: int
    annotated_count: int
    pruned_count: int
    fresh_missing_count: int
    stale_missing_count: int
    issue: ConfigIssue | None = None


@dataclass(frozen=True)
class UploadCandidateClassification:
    ready_records: tuple[PlanRecord, ...]
    settling_records: tuple[PlanRecord, ...]
    vanished_records: tuple[PlanRecord, ...]
    deletion_review_records: tuple[PlanRecord, ...]
    journal_file: Path
    journal_written: bool = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_utc_datetime(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, str):
        normalized = value.strip()
        if normalized.endswith("Z"):
            normalized = f"{normalized[:-1]}+00:00"
        value = datetime.fromisoformat(normalized)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_utc_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return _coerce_utc_datetime(value)
    except ValueError:
        return None


def _pushd_missing_local_prune_ttl_seconds(config: AppConfig) -> int:
    return max(0, config.pushd_missing_local_prune_ttl_seconds)


def _upload_candidate_journal_path(config: AppConfig) -> Path:
    return config.state_dir / "pushd" / "upload-candidates.json"


def _fingerprint_payload(fingerprint: LocalFingerprint) -> dict[str, object]:
    return fingerprint.as_dict()


def _fingerprint_from_payload(payload: object) -> LocalFingerprint | None:
    if not isinstance(payload, dict):
        return None
    size = payload.get("size")
    mtime_ns = payload.get("mtime_ns")
    return LocalFingerprint(
        exists=bool(payload.get("exists")),
        size=size if isinstance(size, int) else None,
        mtime_ns=mtime_ns if isinstance(mtime_ns, int) else None,
    )


def _read_upload_candidate_journal(config: AppConfig) -> dict[str, dict[str, object]]:
    path = _upload_candidate_journal_path(config)
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    raw_records = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(raw_records, list):
        return {}
    records: dict[str, dict[str, object]] = {}
    for raw in raw_records:
        if not isinstance(raw, dict):
            continue
        path_value = normalize_plan_path(raw.get("path", ""))
        if path_value:
            records[path_value] = dict(raw)
    return records


def _write_upload_candidate_journal(
    config: AppConfig,
    records: dict[str, dict[str, object]],
) -> Path:
    path = _upload_candidate_journal_path(config)
    payload = {
        "schema_version": "pcloud-tools-upload-candidates.v1",
        "generated_at": _now(),
        "records": [records[key] for key in sorted(records)],
    }
    return atomic_write_json(path, payload, sort_keys=True)


def upload_candidate_was_uploaded(config: AppConfig, path: str) -> bool:
    entry = _read_upload_candidate_journal(config).get(normalize_plan_path(path))
    return bool(entry and entry.get("uploaded_at"))


def classify_upload_candidates(
    config: AppConfig,
    records: tuple[PlanRecord, ...],
    *,
    observed_at: datetime | str | None = None,
    write: bool = False,
) -> UploadCandidateClassification:
    now = _coerce_utc_datetime(observed_at)
    now_text = now.isoformat()
    settle_seconds = max(0, int(getattr(config, "pushd_upload_settle_seconds", 0)))
    journal = _read_upload_candidate_journal(config)
    updated = dict(journal)
    ready: list[PlanRecord] = []
    settling: list[PlanRecord] = []
    vanished: list[PlanRecord] = []
    deletion_review: list[PlanRecord] = []

    for record in records:
        path = normalize_plan_path(record.path)
        if not path:
            ready.append(record)
            continue
        local_path = config.core_dir / path
        fingerprint = local_fingerprint(local_path)
        entry = dict(journal.get(path, {}))
        uploaded_at = entry.get("uploaded_at")
        fswatch_candidate = record.reason.startswith("fswatch")

        if not fingerprint.exists:
            destructive_action = record.action in {"delete", "rename"}
            if not uploaded_at or not destructive_action:
                updated.pop(path, None)
            if destructive_action and (uploaded_at or not fswatch_candidate):
                deletion_review.append(
                    PlanRecord(
                        path,
                        "delete",
                        "previously uploaded local path disappeared" if uploaded_at else record.reason,
                    )
                )
            else:
                vanished.append(PlanRecord(path, record.action, "uncommitted local candidate disappeared"))
            continue

        previous = _fingerprint_from_payload(entry.get("fingerprint"))
        stable_since = _parse_utc_datetime(entry.get("stable_since"))
        same_fingerprint = previous == fingerprint and stable_since is not None
        if not same_fingerprint:
            stable_since = now

        updated[path] = {
            "path": path,
            "fingerprint": _fingerprint_payload(fingerprint),
            "stable_since": stable_since.isoformat(),
            "observed_at": now_text,
            **({"uploaded_at": uploaded_at} if uploaded_at else {}),
        }
        stable_age = max(0, int((now - stable_since).total_seconds()))
        effective_record = (
            PlanRecord(path, "upload", record.reason)
            if fswatch_candidate and record.action in {"delete", "rename"}
            else record
        )
        if settle_seconds > 0 and stable_age < settle_seconds:
            settling.append(
                PlanRecord(
                    path,
                    effective_record.action,
                    f"waiting for unchanged local fingerprint ({stable_age}s < {settle_seconds}s)",
                )
            )
            continue
        ready.append(effective_record)

    journal_written = False
    if write and updated != journal:
        _write_upload_candidate_journal(config, updated)
        journal_written = True
    return UploadCandidateClassification(
        ready_records=tuple(ready),
        settling_records=tuple(settling),
        vanished_records=tuple(vanished),
        deletion_review_records=tuple(deletion_review),
        journal_file=_upload_candidate_journal_path(config),
        journal_written=journal_written,
    )


def reset_upload_candidate_settling(config: AppConfig, path: str) -> Path:
    normalized = normalize_plan_path(path)
    journal = _read_upload_candidate_journal(config)
    existing = dict(journal.get(normalized, {}))
    now_text = _now()
    journal[normalized] = {
        "path": normalized,
        "fingerprint": _fingerprint_payload(local_fingerprint(config.core_dir / normalized)),
        "stable_since": now_text,
        "observed_at": now_text,
        **({"uploaded_at": existing["uploaded_at"]} if existing.get("uploaded_at") else {}),
    }
    return _write_upload_candidate_journal(config, journal)


def mark_upload_candidate_completed(config: AppConfig, path: str) -> Path:
    normalized = normalize_plan_path(path)
    journal = _read_upload_candidate_journal(config)
    now_text = _now()
    journal[normalized] = {
        "path": normalized,
        "fingerprint": _fingerprint_payload(local_fingerprint(config.core_dir / normalized)),
        "stable_since": now_text,
        "observed_at": now_text,
        "uploaded_at": now_text,
    }
    return _write_upload_candidate_journal(config, journal)


def _read_json_list(path: Path, key_prefix: str) -> tuple[list[Any], ConfigIssue | None]:
    if not path.exists():
        return [], None
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return [], ConfigIssue(
            key=key_prefix,
            level="warning",
            message=f"cannot read plan state file {path}: {exc}",
        )
    if not isinstance(payload, list):
        return [], ConfigIssue(
            key=key_prefix,
            level="warning",
            message=f"plan state must be a JSON list: {path}",
        )
    return payload, None


def normalize_plan_path(value: object) -> str:
    path = str(value or "").strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    while path.startswith("/"):
        path = path[1:]
    parts = [part for part in path.split("/") if part]
    if any(part in {".", ".."} for part in parts):
        return ""
    return "/".join(parts)


def _record_from_item(item: Any, default_action: str = "sync") -> PlanRecord:
    if isinstance(item, str):
        return PlanRecord(path=normalize_plan_path(item), action=default_action, reason="-")
    if isinstance(item, dict):
        return PlanRecord(
            path=normalize_plan_path(item.get("path", "")),
            action=str(item.get("action", item.get("op", default_action))),
            reason=str(item.get("reason", "-")),
        )
    return PlanRecord(path="", action=default_action, reason="invalid queue item")


def _matches_allowlist(path: str, entries: tuple[str, ...]) -> bool:
    for entry in entries:
        if entry == "/":
            return True
        clean_entry = entry[:-1] if entry.endswith("/") else entry
        if entry.endswith("/") and (path == clean_entry or path.startswith(f"{clean_entry}/")):
            return True
        if path == clean_entry:
            return True
    return False


def _matches_exclude(path: str, excludes: tuple[str, ...]) -> bool:
    for pattern in excludes:
        clean_pattern = pattern.strip().lstrip("/")
        if not clean_pattern:
            continue
        if fnmatch.fnmatch(path, clean_pattern) or fnmatch.fnmatch(f"/{path}", f"/{clean_pattern}"):
            return True
        if "/" not in clean_pattern and fnmatch.fnmatch(Path(path).name, clean_pattern):
            return True
    return False


def _is_partial_transfer_path(path: str) -> bool:
    return Path(path).name.endswith(".partial")


def _is_local_upload_directory(config: AppConfig, record: PlanRecord) -> bool:
    if record.action != "upload":
        return False
    try:
        return (config.core_dir / record.path).is_dir()
    except OSError:
        return False


def _record_payload(
    path: str,
    action: str,
    reason: str,
    *,
    enqueued_at: str | None = None,
) -> dict[str, str]:
    payload = {"path": path, "action": action, "reason": reason}
    if enqueued_at:
        payload["enqueued_at"] = enqueued_at
    return payload


def record_payloads(records: tuple[PlanRecord, ...]) -> list[dict[str, str]]:
    return [_record_payload(record.path, record.action, record.reason) for record in records]


def append_plan_record(
    path: Path,
    key_prefix: str,
    record: PlanRecord,
    *,
    include_enqueued_at: bool = False,
) -> PlanUpdateResult:
    payload, issue = _read_json_list(path, key_prefix)
    if issue:
        return PlanUpdateResult(file=path, before_count=0, after_count=0, issue=issue)
    updated = [
        *payload,
        _record_payload(
            record.path,
            record.action,
            record.reason,
            enqueued_at=_now() if include_enqueued_at else None,
        ),
    ]
    atomic_write_json(path, updated)
    return PlanUpdateResult(file=path, before_count=len(payload), after_count=len(updated))


def append_plan_record_with_policy(
    path: Path,
    key_prefix: str,
    record: PlanRecord,
    *,
    max_records: int,
    enqueued_at: str | None = None,
) -> PlanAppendPolicyResult:
    payload, issue = _read_json_list(path, key_prefix)
    if issue:
        return PlanAppendPolicyResult(
            file=path,
            before_count=0,
            after_count=0,
            appended=False,
            skipped_reason="state read failed",
            issue=issue,
        )
    before_count = len(payload)
    for item in payload:
        existing = _record_from_item(item, record.action)
        if existing.path == record.path and existing.action == record.action:
            return PlanAppendPolicyResult(
                file=path,
                before_count=before_count,
                after_count=before_count,
                appended=False,
                skipped_reason="duplicate path/action",
            )
    if max_records >= 0 and before_count >= max_records:
        return PlanAppendPolicyResult(
            file=path,
            before_count=before_count,
            after_count=before_count,
            appended=False,
            skipped_reason="queue limit reached",
            issue=ConfigIssue(
                key=key_prefix,
                level="warning",
                message=f"plan state already has {before_count} records; limit is {max_records}",
            ),
        )
    updated = [*payload, _record_payload(record.path, record.action, record.reason, enqueued_at=enqueued_at)]
    atomic_write_json(path, updated)
    return PlanAppendPolicyResult(
        file=path,
        before_count=before_count,
        after_count=len(updated),
        appended=True,
        skipped_reason="",
    )


def clear_plan_records(path: Path, key_prefix: str) -> PlanUpdateResult:
    payload, issue = _read_json_list(path, key_prefix)
    if issue:
        return PlanUpdateResult(file=path, before_count=0, after_count=0, issue=issue)
    atomic_write_json(path, [])
    return PlanUpdateResult(file=path, before_count=len(payload), after_count=0)


def remove_plan_records(path: Path, key_prefix: str, target_path: str, *, write: bool = True) -> PlanUpdateResult:
    payload, issue = _read_json_list(path, key_prefix)
    if issue:
        return PlanUpdateResult(file=path, before_count=0, after_count=0, issue=issue)
    normalized_target = normalize_plan_path(target_path)
    updated = [
        item for item in payload
        if normalize_plan_path(item.get("path", "") if isinstance(item, dict) else item) != normalized_target
    ]
    if write:
        atomic_write_json(path, updated)
    return PlanUpdateResult(file=path, before_count=len(payload), after_count=len(updated))


def remove_plan_record_exact(
    path: Path,
    key_prefix: str,
    target_path: str,
    target_action: str,
    *,
    target_reason: str | None = None,
    write: bool = True,
    max_records: int | None = 1,
) -> PlanUpdateResult:
    payload, issue = _read_json_list(path, key_prefix)
    if issue:
        return PlanUpdateResult(file=path, before_count=0, after_count=0, issue=issue)
    normalized_target = normalize_plan_path(target_path)
    normalized_action = str(target_action or "").strip()
    updated: list[Any] = []
    removed = 0
    for item in payload:
        record = _record_from_item(item, "upload")
        matches = (
            record.path == normalized_target
            and record.action == normalized_action
            and (target_reason is None or record.reason == target_reason)
            and (max_records is None or removed < max_records)
        )
        if matches:
            removed += 1
            continue
        updated.append(item)
    if write:
        atomic_write_json(path, updated)
    return PlanUpdateResult(file=path, before_count=len(payload), after_count=len(updated))


def _configured_trash_relative_root(config: AppConfig) -> str:
    root = str(getattr(config, "remote_trash_root", "") or "").strip().rstrip("/")
    remote = str(getattr(config, "core_remote", "") or "").strip().rstrip("/")
    if remote and root.startswith(f"{remote}/"):
        return normalize_plan_path(root[len(remote) + 1:]) or ".pcloud-manager-trash"
    return ".pcloud-manager-trash"


def _is_configured_trash_path(config: AppConfig, path: str) -> bool:
    root = _configured_trash_relative_root(config)
    clean = normalize_plan_path(path)
    return clean == root or clean.startswith(f"{root}/")


def _is_planned_pushd_upload_record(config: AppConfig, queue_file: Path, record: PlanRecord) -> bool:
    is_fswatch_candidate = record.reason.startswith("fswatch") and record.action in {"delete", "rename"}
    if record.action != "upload" and not is_fswatch_candidate:
        return False
    plan = build_pushd_plan_from_records(config, queue_file, (record,), total=1)
    return bool(plan.upload_records)


def _queue_item_missing_since(item: object) -> datetime | None:
    if not isinstance(item, dict):
        return None
    return _parse_utc_datetime(item.get("missing_since"))


def _planned_missing_local_upload_record(config: AppConfig, queue_file: Path, item: object) -> PlanRecord | None:
    record = _record_from_item(item, "upload")
    destructive_uploaded_path = (
        record.action in {"delete", "rename"}
        and upload_candidate_was_uploaded(config, record.path)
    )
    if (
        _is_planned_pushd_upload_record(config, queue_file, record)
        and not (config.core_dir / record.path).exists()
        and not destructive_uploaded_path
    ):
        return record
    return None


def _queue_item_with_missing_since(item: object, record: PlanRecord, missing_since: str) -> object:
    if isinstance(item, dict):
        updated = dict(item)
        updated["missing_since"] = missing_since
        return updated
    payload = _record_payload(record.path, record.action, record.reason)
    payload["missing_since"] = missing_since
    return payload


def annotate_missing_local_upload_records(
    config: AppConfig,
    queue_file: Path,
    key_prefix: str = "PCLOUD_TOOLS_PUSHD_QUEUE",
    *,
    observed_at: datetime | str | None = None,
    write: bool = True,
) -> MissingLocalQueueCleanupResult:
    payload, issue = _read_json_list(queue_file, key_prefix)
    if issue:
        return MissingLocalQueueCleanupResult(
            file=queue_file,
            before_count=0,
            after_count=0,
            annotated_count=0,
            pruned_count=0,
            fresh_missing_count=0,
            stale_missing_count=0,
            issue=issue,
        )

    now = _coerce_utc_datetime(observed_at)
    missing_since = now.isoformat()
    updated: list[object] = []
    annotated_count = 0
    fresh_missing_count = 0
    for item in payload:
        record = _planned_missing_local_upload_record(config, queue_file, item)
        if record is not None:
            fresh_missing_count += 1
            if _queue_item_missing_since(item) is None:
                updated.append(_queue_item_with_missing_since(item, record, missing_since))
                annotated_count += 1
                continue
        updated.append(item)

    if write and updated != payload:
        atomic_write_json(queue_file, updated)
    return MissingLocalQueueCleanupResult(
        file=queue_file,
        before_count=len(payload),
        after_count=len(updated),
        annotated_count=annotated_count,
        pruned_count=0,
        fresh_missing_count=fresh_missing_count,
        stale_missing_count=0,
    )


def cleanup_stale_missing_local_upload_records(
    config: AppConfig,
    queue_file: Path,
    key_prefix: str = "PCLOUD_TOOLS_PUSHD_QUEUE",
    *,
    observed_at: datetime | str | None = None,
    write: bool = True,
) -> MissingLocalQueueCleanupResult:
    annotated = annotate_missing_local_upload_records(
        config,
        queue_file,
        key_prefix,
        observed_at=observed_at,
        write=write,
    )
    if annotated.issue:
        return annotated

    pruned = prune_stale_missing_local_upload_records(
        config,
        queue_file,
        key_prefix,
        observed_at=observed_at,
        write=write,
    )
    return MissingLocalQueueCleanupResult(
        file=queue_file,
        before_count=annotated.before_count,
        after_count=pruned.after_count,
        annotated_count=annotated.annotated_count,
        pruned_count=pruned.pruned_count,
        fresh_missing_count=pruned.fresh_missing_count,
        stale_missing_count=pruned.stale_missing_count,
        issue=pruned.issue,
    )


def prune_stale_missing_local_upload_records(
    config: AppConfig,
    queue_file: Path,
    key_prefix: str = "PCLOUD_TOOLS_PUSHD_QUEUE",
    *,
    observed_at: datetime | str | None = None,
    write: bool = True,
) -> MissingLocalQueueCleanupResult:
    payload, issue = _read_json_list(queue_file, key_prefix)
    if issue:
        return MissingLocalQueueCleanupResult(
            file=queue_file,
            before_count=0,
            after_count=0,
            annotated_count=0,
            pruned_count=0,
            fresh_missing_count=0,
            stale_missing_count=0,
            issue=issue,
        )

    now = _coerce_utc_datetime(observed_at)
    ttl_seconds = _pushd_missing_local_prune_ttl_seconds(config)
    updated: list[object] = []
    pruned_count = 0
    fresh_missing_count = 0
    stale_missing_count = 0
    for item in payload:
        if _planned_missing_local_upload_record(config, queue_file, item) is None:
            updated.append(item)
            continue

        missing_since = _queue_item_missing_since(item)
        if missing_since is None:
            fresh_missing_count += 1
            updated.append(item)
            continue

        if (now - missing_since).total_seconds() >= ttl_seconds:
            pruned_count += 1
            stale_missing_count += 1
            continue

        fresh_missing_count += 1
        updated.append(item)

    if write and updated != payload:
        atomic_write_json(queue_file, updated)
    return MissingLocalQueueCleanupResult(
        file=queue_file,
        before_count=len(payload),
        after_count=len(updated),
        annotated_count=0,
        pruned_count=pruned_count,
        fresh_missing_count=fresh_missing_count,
        stale_missing_count=stale_missing_count,
    )


def force_prune_missing_local_upload_records(
    config: AppConfig,
    queue_file: Path,
    key_prefix: str = "PCLOUD_TOOLS_PUSHD_QUEUE",
    *,
    write: bool = True,
) -> MissingLocalQueueCleanupResult:
    payload, issue = _read_json_list(queue_file, key_prefix)
    if issue:
        return MissingLocalQueueCleanupResult(
            file=queue_file,
            before_count=0,
            after_count=0,
            annotated_count=0,
            pruned_count=0,
            fresh_missing_count=0,
            stale_missing_count=0,
            issue=issue,
        )

    updated: list[object] = []
    pruned_count = 0
    for item in payload:
        if _planned_missing_local_upload_record(config, queue_file, item) is not None:
            pruned_count += 1
            continue
        updated.append(item)

    if write and updated != payload:
        atomic_write_json(queue_file, updated)
    return MissingLocalQueueCleanupResult(
        file=queue_file,
        before_count=len(payload),
        after_count=len(updated),
        annotated_count=0,
        pruned_count=pruned_count,
        fresh_missing_count=0,
        stale_missing_count=pruned_count,
    )


def cleanup_missing_local_upload_records_for_executor_start(
    config: AppConfig,
    queue_file: Path,
    key_prefix: str = "PCLOUD_TOOLS_PUSHD_QUEUE",
    *,
    observed_at: datetime | str | None = None,
    write: bool = True,
) -> MissingLocalQueueCleanupResult:
    del observed_at
    return force_prune_missing_local_upload_records(
        config,
        queue_file,
        key_prefix,
        write=write,
    )


def prune_missing_local_upload_records(
    config: AppConfig,
    queue_file: Path,
    key_prefix: str = "PCLOUD_TOOLS_PUSHD_QUEUE",
    *,
    write: bool = True,
) -> MissingLocalQueueCleanupResult:
    return force_prune_missing_local_upload_records(
        config,
        queue_file,
        key_prefix,
        write=write,
    )


def record_dry_run_state(
    state: ServiceDaemonState,
    service_name: str,
    plan_summary: str,
    counts: dict[str, int],
    records: dict[str, list[dict[str, str]]],
) -> DryRunStateResult:
    generated_at = _now()
    cursor = f"{service_name}:dry-run:{generated_at}"
    plan_payload: dict[str, Any] = {
        "service": service_name,
        "mode": "dry-run",
        "generated_at": generated_at,
        "plan_summary": plan_summary,
        "counts": counts,
        "records": records,
    }
    event_payload = {
        "service": service_name,
        "event": "dry-run",
        "message": f"{service_name} dry-run recorded",
        "recorded_at": generated_at,
        "cursor": cursor,
    }
    atomic_write_json(state.last_plan_file, plan_payload)
    atomic_write_json(state.last_event_file, event_payload)
    atomic_write_text(state.cursor_file, f"{cursor}\n")
    return DryRunStateResult(
        last_plan_file=state.last_plan_file,
        last_event_file=state.last_event_file,
        cursor_file=state.cursor_file,
        cursor=cursor,
    )


def build_pushd_plan(config: AppConfig, state: ServiceDaemonState) -> tuple[PushdPlan, SyncScopeInfo]:
    scope = sync_allowlist_info(config)
    payload, queue_issue = _read_json_list(state.queue_file, "PCLOUD_TOOLS_PUSHD_QUEUE")
    issues: list[ConfigIssue] = []
    if queue_issue:
        issues.append(queue_issue)

    records = tuple(_record_from_item(item, "upload") for item in payload)
    plan = build_pushd_plan_from_records(config, state.queue_file, records, issues=tuple(issues), total=len(payload))
    return plan, scope


def build_pushd_plan_from_records(
    config: AppConfig,
    source_file: Path,
    records: tuple[PlanRecord, ...],
    issues: tuple[ConfigIssue, ...] = (),
    total: int | None = None,
) -> PushdPlan:
    scope = sync_allowlist_info(config)
    upload: list[PlanRecord] = []
    excluded: list[PlanRecord] = []
    invalid: list[PlanRecord] = []
    for record in records:
        if not record.path:
            invalid.append(record)
        elif _is_configured_trash_path(config, record.path):
            excluded.append(PlanRecord(record.path, record.action, "remote trash root"))
        elif scope.allowlist_status != "loaded" or not _matches_allowlist(record.path, scope.entries):
            excluded.append(PlanRecord(record.path, record.action, "outside sync scope"))
        elif _matches_exclude(record.path, config.default_excludes):
            excluded.append(PlanRecord(record.path, record.action, "default exclude"))
        elif _is_partial_transfer_path(record.path):
            excluded.append(PlanRecord(record.path, record.action, "partial transfer file"))
        elif (ignore_match := manager_ignore_match(config, record.path)) and ignore_match.ignored:
            excluded.append(PlanRecord(record.path, record.action, ignore_match.reason))
        elif _is_local_upload_directory(config, record):
            excluded.append(PlanRecord(record.path, record.action, "directory upload not supported"))
        elif record.action == "upload" and (
            suppressed_match := download_suppression_match(config, record.path)
        )[0]:
            _suppressed, reason, _journal_record = suppressed_match
            excluded.append(PlanRecord(record.path, record.action, reason or "download suppression journal"))
        else:
            upload.append(record)

    return PushdPlan(
        queue_file=source_file,
        total=len(records) if total is None else total,
        upload_count=len(upload),
        excluded_count=len(excluded),
        invalid_count=len(invalid),
        upload_records=tuple(upload),
        excluded_records=tuple(excluded),
        invalid_records=tuple(invalid),
        issues=tuple(issues),
    )


def build_diffd_plan(config: AppConfig, state: ServiceDaemonState, daemon_state: DaemonState) -> DiffdPlan:
    remote_changes_file = state.state_dir / "remote-changes.json"
    remote_payload, remote_issue = _read_json_list(
        remote_changes_file, "PCLOUD_TOOLS_DIFFD_REMOTE_CHANGES"
    )
    issues: list[ConfigIssue] = []
    if remote_issue:
        issues.append(remote_issue)

    remote_records = tuple(_record_from_item(item, "download") for item in remote_payload)
    pending_records = tuple(
        PlanRecord(path=item.path, action="download", reason=item.reason)
        for item in daemon_state.pending_downloads
    )
    return build_diffd_plan_from_records(
        config=config,
        remote_changes_file=remote_changes_file,
        pending_downloads_file=daemon_state.pending_downloads_file,
        remote_records=remote_records,
        pending_records=pending_records,
        issues=tuple(issues),
    )


def build_diffd_plan_from_records(
    config: AppConfig,
    remote_changes_file: Path,
    pending_downloads_file: Path,
    remote_records: tuple[PlanRecord, ...],
    pending_records: tuple[PlanRecord, ...] = (),
    issues: tuple[ConfigIssue, ...] = (),
) -> DiffdPlan:
    scope = sync_allowlist_info(config)
    download_records: list[PlanRecord] = []
    skipped_records: list[PlanRecord] = []
    for record in (*remote_records, *pending_records):
        if not record.path:
            skipped_records.append(record)
        elif _is_configured_trash_path(config, record.path):
            skipped_records.append(PlanRecord(record.path, record.action, "remote trash root"))
        elif scope.allowlist_status != "loaded" or not _matches_allowlist(record.path, scope.entries):
            skipped_records.append(PlanRecord(record.path, record.action, "outside sync scope"))
        elif _matches_exclude(record.path, config.default_excludes):
            skipped_records.append(PlanRecord(record.path, record.action, "default exclude"))
        elif _is_partial_transfer_path(record.path):
            skipped_records.append(PlanRecord(record.path, record.action, "partial transfer file"))
        elif (ignore_match := manager_ignore_match(config, record.path)) and ignore_match.ignored:
            skipped_records.append(PlanRecord(record.path, record.action, ignore_match.reason))
        elif record.action == "download" and record.reason == "diff:createfile" and (
            upload_match := upload_origin_match(config, record.path)
        )[0]:
            _matched, reason, _journal_record = upload_match
            skipped_records.append(PlanRecord(record.path, record.action, reason or "upload origin journal"))
        else:
            download_records.append(record)

    return DiffdPlan(
        remote_changes_file=remote_changes_file,
        pending_downloads_file=pending_downloads_file,
        remote_change_count=len(remote_records),
        pending_download_count=len(pending_records),
        download_count=len(download_records),
        skipped_count=len(skipped_records),
        remote_change_records=remote_records,
        pending_download_records=pending_records,
        download_records=tuple(download_records),
        skipped_records=tuple(skipped_records),
        issues=issues,
    )
