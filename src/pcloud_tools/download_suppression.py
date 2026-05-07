from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import AppConfig, ConfigIssue

_SCHEMA_VERSION = "pcloud-tools-download-suppression.v1"
_UPLOAD_ORIGIN_SCHEMA_VERSION = "pcloud-tools-upload-origin-suppression.v1"


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


@dataclass(frozen=True)
class LocalFingerprint:
    exists: bool
    size: int | None = None
    mtime_ns: int | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "exists": self.exists,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
        }


@dataclass(frozen=True)
class SuppressionRecord:
    path: str
    state: str
    direction: str
    started_at: str
    completed_at: str | None = None
    local_fingerprint: LocalFingerprint | None = None
    conflict_path: str | None = None


@dataclass(frozen=True)
class SuppressionJournal:
    path: Path
    records: tuple[SuppressionRecord, ...]
    expired_count: int
    issues: tuple[ConfigIssue, ...] = ()


def download_suppression_journal_path(config: AppConfig) -> Path:
    return config.state_dir / "diffd" / "download-suppression-journal.json"


def download_staging_dir(config: AppConfig) -> Path:
    return config.state_dir / "diffd" / "download-staging"


def upload_origin_journal_path(config: AppConfig) -> Path:
    return config.state_dir / "pushd" / "upload-origin-journal.json"


def local_fingerprint(path: Path) -> LocalFingerprint:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return LocalFingerprint(exists=False)
    except OSError:
        return LocalFingerprint(exists=False)
    if not path.is_file():
        return LocalFingerprint(exists=False)
    return LocalFingerprint(exists=True, size=stat.st_size, mtime_ns=stat.st_mtime_ns)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_text() -> str:
    return _now().isoformat()


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _fingerprint_from_payload(payload: object) -> LocalFingerprint | None:
    if not isinstance(payload, dict):
        return None
    exists = bool(payload.get("exists"))
    size = payload.get("size")
    mtime_ns = payload.get("mtime_ns")
    return LocalFingerprint(
        exists=exists,
        size=size if isinstance(size, int) else None,
        mtime_ns=mtime_ns if isinstance(mtime_ns, int) else None,
    )


def _record_from_payload(payload: object) -> SuppressionRecord | None:
    if not isinstance(payload, dict):
        return None
    path = normalize_plan_path(payload.get("path", ""))
    state = str(payload.get("state", "") or "")
    if not path or state not in {"in-progress", "completed", "conflict"}:
        return None
    direction = str(payload.get("direction", "download") or "download")
    return SuppressionRecord(
        path=path,
        state=state,
        direction=direction,
        started_at=str(payload.get("started_at") or ""),
        completed_at=str(payload.get("completed_at") or "") or None,
        local_fingerprint=_fingerprint_from_payload(payload.get("local_fingerprint")),
        conflict_path=normalize_plan_path(payload.get("conflict_path", "")) or None,
    )


def _payload_from_record(record: SuppressionRecord) -> dict[str, object]:
    payload: dict[str, object] = {
        "path": record.path,
        "state": record.state,
        "direction": record.direction,
        "started_at": record.started_at,
    }
    if record.completed_at:
        payload["completed_at"] = record.completed_at
    if record.local_fingerprint:
        payload["local_fingerprint"] = record.local_fingerprint.as_dict()
    if record.conflict_path:
        payload["conflict_path"] = record.conflict_path
    return payload


def _is_expired(record: SuppressionRecord, *, now: datetime, ttl_seconds: int) -> bool:
    if record.state == "in-progress":
        return False
    completed_at = _parse_datetime(record.completed_at)
    if completed_at is None:
        return False
    return (now - completed_at).total_seconds() > ttl_seconds


def read_download_suppression_journal(config: AppConfig) -> SuppressionJournal:
    path = download_suppression_journal_path(config)
    if not path.exists():
        return SuppressionJournal(path=path, records=(), expired_count=0)
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return SuppressionJournal(
            path=path,
            records=(),
            expired_count=0,
            issues=(
                ConfigIssue(
                    key="PCLOUD_TOOLS_DOWNLOAD_SUPPRESSION_JOURNAL",
                    level="warning",
                    message=f"cannot read download suppression journal {path}: {exc}",
                ),
            ),
        )
    raw_records = payload.get("records") if isinstance(payload, dict) else payload
    if not isinstance(raw_records, list):
        return SuppressionJournal(
            path=path,
            records=(),
            expired_count=0,
            issues=(
                ConfigIssue(
                    key="PCLOUD_TOOLS_DOWNLOAD_SUPPRESSION_JOURNAL",
                    level="warning",
                    message=f"download suppression journal must contain records list: {path}",
                ),
            ),
        )
    now = _now()
    records: list[SuppressionRecord] = []
    expired_count = 0
    for raw in raw_records:
        record = _record_from_payload(raw)
        if record is None:
            continue
        if _is_expired(record, now=now, ttl_seconds=config.download_suppression_ttl_seconds):
            expired_count += 1
            continue
        records.append(record)
    return SuppressionJournal(path=path, records=tuple(records), expired_count=expired_count)


def read_upload_origin_journal(config: AppConfig) -> SuppressionJournal:
    path = upload_origin_journal_path(config)
    if not path.exists():
        return SuppressionJournal(path=path, records=(), expired_count=0)
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return SuppressionJournal(
            path=path,
            records=(),
            expired_count=0,
            issues=(
                ConfigIssue(
                    key="PCLOUD_TOOLS_UPLOAD_ORIGIN_JOURNAL",
                    level="warning",
                    message=f"cannot read upload origin journal {path}: {exc}",
                ),
            ),
        )
    raw_records = payload.get("records") if isinstance(payload, dict) else payload
    if not isinstance(raw_records, list):
        return SuppressionJournal(
            path=path,
            records=(),
            expired_count=0,
            issues=(
                ConfigIssue(
                    key="PCLOUD_TOOLS_UPLOAD_ORIGIN_JOURNAL",
                    level="warning",
                    message=f"upload origin journal must contain records list: {path}",
                ),
            ),
        )
    now = _now()
    records: list[SuppressionRecord] = []
    expired_count = 0
    for raw in raw_records:
        record = _record_from_payload(raw)
        if record is None or record.direction != "upload":
            continue
        if _is_expired(record, now=now, ttl_seconds=config.download_suppression_ttl_seconds):
            expired_count += 1
            continue
        records.append(record)
    return SuppressionJournal(path=path, records=tuple(records), expired_count=expired_count)


def write_download_suppression_journal(config: AppConfig, records: tuple[SuppressionRecord, ...]) -> Path:
    path = download_suppression_journal_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": _SCHEMA_VERSION,
        "generated_at": _now_text(),
        "ttl_seconds": config.download_suppression_ttl_seconds,
        "records": [_payload_from_record(record) for record in records],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return path


def write_upload_origin_journal(config: AppConfig, records: tuple[SuppressionRecord, ...]) -> Path:
    path = upload_origin_journal_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": _UPLOAD_ORIGIN_SCHEMA_VERSION,
        "generated_at": _now_text(),
        "ttl_seconds": config.download_suppression_ttl_seconds,
        "records": [_payload_from_record(record) for record in records],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return path


def _replace_record(
    records: tuple[SuppressionRecord, ...],
    new_record: SuppressionRecord,
) -> tuple[SuppressionRecord, ...]:
    retained = tuple(record for record in records if record.path != new_record.path)
    return (*retained, new_record)


def mark_download_started(config: AppConfig, path: str) -> Path:
    normalized = normalize_plan_path(path)
    journal = read_download_suppression_journal(config)
    record = SuppressionRecord(
        path=normalized,
        state="in-progress",
        direction="download",
        started_at=_now_text(),
    )
    return write_download_suppression_journal(config, _replace_record(journal.records, record))


def mark_download_completed(config: AppConfig, path: str, fingerprint: LocalFingerprint) -> Path:
    normalized = normalize_plan_path(path)
    journal = read_download_suppression_journal(config)
    existing = next((record for record in journal.records if record.path == normalized), None)
    record = SuppressionRecord(
        path=normalized,
        state="completed",
        direction="download",
        started_at=existing.started_at if existing else _now_text(),
        completed_at=_now_text(),
        local_fingerprint=fingerprint,
    )
    return write_download_suppression_journal(config, _replace_record(journal.records, record))


def mark_upload_completed(config: AppConfig, path: str, fingerprint: LocalFingerprint) -> Path:
    normalized = normalize_plan_path(path)
    journal = read_upload_origin_journal(config)
    existing = next((record for record in journal.records if record.path == normalized), None)
    record = SuppressionRecord(
        path=normalized,
        state="completed",
        direction="upload",
        started_at=existing.started_at if existing else _now_text(),
        completed_at=_now_text(),
        local_fingerprint=fingerprint,
    )
    return write_upload_origin_journal(config, _replace_record(journal.records, record))


def mark_download_conflict(
    config: AppConfig,
    path: str,
    *,
    conflict_path: str,
    fingerprint: LocalFingerprint,
) -> Path:
    normalized = normalize_plan_path(path)
    journal = read_download_suppression_journal(config)
    existing = next((record for record in journal.records if record.path == normalized), None)
    record = SuppressionRecord(
        path=normalized,
        state="conflict",
        direction="download",
        started_at=existing.started_at if existing else _now_text(),
        completed_at=_now_text(),
        local_fingerprint=fingerprint,
        conflict_path=normalize_plan_path(conflict_path),
    )
    return write_download_suppression_journal(config, _replace_record(journal.records, record))


def clear_download_suppression_record(config: AppConfig, path: str) -> Path:
    normalized = normalize_plan_path(path)
    journal = read_download_suppression_journal(config)
    retained = tuple(record for record in journal.records if record.path != normalized)
    return write_download_suppression_journal(config, retained)


def download_suppression_match(
    config: AppConfig,
    path: str,
) -> tuple[bool, str, SuppressionRecord | None]:
    normalized = normalize_plan_path(path)
    if not normalized:
        return False, "", None
    journal = read_download_suppression_journal(config)
    record = next((item for item in reversed(journal.records) if item.path == normalized), None)
    if record is None:
        return False, "", None
    if record.state == "in-progress":
        return True, "download in progress", record
    if record.state == "completed" and record.local_fingerprint:
        current = local_fingerprint(config.core_dir / normalized)
        if current == record.local_fingerprint:
            return True, "download suppression journal", record
    return False, "", record


def upload_origin_match(
    config: AppConfig,
    path: str,
) -> tuple[bool, str, SuppressionRecord | None]:
    normalized = normalize_plan_path(path)
    if not normalized:
        return False, "", None
    journal = read_upload_origin_journal(config)
    record = next((item for item in reversed(journal.records) if item.path == normalized), None)
    if record is None:
        return False, "", None
    if record.state == "completed" and record.local_fingerprint:
        current = local_fingerprint(config.core_dir / normalized)
        if current == record.local_fingerprint:
            return True, "upload origin journal", record
    return False, "", record


def suppression_status_details(config: AppConfig) -> dict[str, object]:
    journal = read_download_suppression_journal(config)
    upload_journal = read_upload_origin_journal(config)
    counts = {"in-progress": 0, "completed": 0, "conflict": 0}
    latest_conflict = "-"
    for record in journal.records:
        if record.state in counts:
            counts[record.state] += 1
        if record.state == "conflict":
            latest_conflict = record.conflict_path or record.path
    return {
        "download suppression journal": str(journal.path),
        "download suppression in-progress": counts["in-progress"],
        "download suppression completed": counts["completed"],
        "download conflict count": counts["conflict"],
        "download latest conflict": latest_conflict,
        "download suppression expired records": journal.expired_count,
        "upload origin journal": str(upload_journal.path),
        "upload origin completed": sum(1 for record in upload_journal.records if record.state == "completed"),
        "upload origin expired records": upload_journal.expired_count,
    }


def conflict_copy_path(destination: Path, *, now: datetime | None = None) -> Path:
    timestamp = (now or _now()).astimezone(timezone.utc).strftime("%Y%m%d-%H%M%S")
    suffix = destination.suffix
    stem = destination.name[: -len(suffix)] if suffix else destination.name
    candidate = destination.with_name(f"{stem}.conflict-{timestamp}{suffix}")
    if not candidate.exists():
        return candidate
    for index in range(2, 1000):
        candidate = destination.with_name(f"{stem}.conflict-{timestamp}-{index}{suffix}")
        if not candidate.exists():
            return candidate
    return destination.with_name(f"{stem}.conflict-{timestamp}-{_now().timestamp():.0f}{suffix}")
