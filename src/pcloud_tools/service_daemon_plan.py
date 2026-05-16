from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import AppConfig, ConfigIssue
from .daemon_state import DaemonState
from .download_suppression import download_suppression_match, upload_origin_match
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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def _record_payload(path: str, action: str, reason: str) -> dict[str, str]:
    return {"path": path, "action": action, "reason": reason}


def record_payloads(records: tuple[PlanRecord, ...]) -> list[dict[str, str]]:
    return [_record_payload(record.path, record.action, record.reason) for record in records]


def append_plan_record(path: Path, key_prefix: str, record: PlanRecord) -> PlanUpdateResult:
    payload, issue = _read_json_list(path, key_prefix)
    if issue:
        return PlanUpdateResult(file=path, before_count=0, after_count=0, issue=issue)
    updated = [*payload, _record_payload(record.path, record.action, record.reason)]
    atomic_write_json(path, updated)
    return PlanUpdateResult(file=path, before_count=len(payload), after_count=len(updated))


def append_plan_record_with_policy(
    path: Path,
    key_prefix: str,
    record: PlanRecord,
    *,
    max_records: int,
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
    updated = [*payload, _record_payload(record.path, record.action, record.reason)]
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
        elif scope.allowlist_status != "loaded" or not _matches_allowlist(record.path, scope.entries):
            excluded.append(PlanRecord(record.path, record.action, "outside allowlist"))
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
        elif scope.allowlist_status != "loaded" or not _matches_allowlist(record.path, scope.entries):
            skipped_records.append(PlanRecord(record.path, record.action, "outside allowlist"))
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
