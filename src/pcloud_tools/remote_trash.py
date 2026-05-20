from __future__ import annotations

import json
import re
import secrets
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .io_utils import atomic_write_json
from .service_daemon_plan import normalize_plan_path


TRASH_ROOT = ".pcloud-manager-trash"
TRASH_OBJECTS_DIR = "objects"
TRASH_SCHEMA_VERSION = "pcloud-tools-remote-trash.v1"

_SHORT_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_UNSAFE_SEPARATOR_CHARS = {
    ":",
    "\u02d0",
    "\u0589",
    "\u05c3",
    "\u0703",
    "\u0704",
    "\u16ec",
    "\ufe13",
    "\ufe55",
    "\uff1a",
}


@dataclass(frozen=True)
class TrashIdentity:
    timestamp: datetime
    short_id: str

    @property
    def timestamp_id(self) -> str:
        return format_trash_timestamp(self.timestamp)

    @property
    def item_id(self) -> str:
        return f"{self.timestamp_id}-{self.short_id}"

    @property
    def date_parts(self) -> tuple[str, str, str]:
        timestamp = _coerce_timestamp(self.timestamp)
        return (
            f"{timestamp.year:04d}",
            f"{timestamp.month:02d}",
            f"{timestamp.day:02d}",
        )


@dataclass(frozen=True)
class TrashPaths:
    item_id: str
    original_path: str
    display_name: str
    object_path: str
    metadata_path: str
    created_at: str


@dataclass(frozen=True)
class TrashMetadata:
    schema_version: str
    item_id: str
    original_path: str
    object_path: str
    metadata_path: str
    display_name: str
    operation: str
    created_at: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class TrashIndexRecord:
    item_id: str
    original_path: str
    object_path: str
    metadata_path: str
    display_name: str
    operation: str
    created_at: str
    updated_at: str
    status: str
    metadata_payload: dict[str, Any]


def _coerce_timestamp(timestamp: datetime | None) -> datetime:
    if timestamp is None:
        return datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def format_trash_timestamp(timestamp: datetime) -> str:
    return _coerce_timestamp(timestamp).strftime("%Y%m%dT%H%M%S%fZ")


def normalize_original_path(path: object) -> str:
    normalized = normalize_plan_path(path)
    if not normalized:
        raise ValueError("remote trash original path must not be empty or unsafe")
    if is_trash_path(normalized):
        raise ValueError(f"remote trash cannot manage its own trash path: {normalized}")
    return normalized


def is_trash_root_path(path: object) -> bool:
    return normalize_plan_path(path) == TRASH_ROOT


def is_trash_path(path: object) -> bool:
    normalized = normalize_plan_path(path)
    return normalized == TRASH_ROOT or normalized.startswith(f"{TRASH_ROOT}/")


def sanitize_display_filename(original_relative_path: object, *, fallback: str = "item") -> str:
    normalized = normalize_plan_path(original_relative_path)
    raw_name = normalized.rsplit("/", 1)[-1] if normalized else str(original_relative_path or "")
    pieces: list[str] = []
    previous_replacement = False
    for char in raw_name:
        unsafe = (
            char in {"/", "\\"}
            or char in _UNSAFE_SEPARATOR_CHARS
            or unicodedata.category(char).startswith("C")
        )
        if unsafe:
            if not previous_replacement:
                pieces.append("_")
                previous_replacement = True
            continue
        pieces.append(char)
        previous_replacement = False
    sanitized = "".join(pieces).strip(" ._")
    if not sanitized or sanitized in {".", ".."}:
        return fallback
    return sanitized


def _validate_short_id(short_id: str) -> str:
    if not short_id or not _SHORT_ID_RE.match(short_id):
        raise ValueError("remote trash short_id must contain only letters, digits, '_' or '-'")
    return short_id


def new_trash_identity(
    timestamp: datetime | None = None,
    *,
    short_id: str | None = None,
    short_id_factory: Callable[[], str] | None = None,
) -> TrashIdentity:
    timestamp = _coerce_timestamp(timestamp)
    generated_short_id = short_id
    if generated_short_id is None:
        factory = short_id_factory or (lambda: secrets.token_hex(4))
        generated_short_id = factory()
    return TrashIdentity(timestamp=timestamp, short_id=_validate_short_id(generated_short_id))


def build_object_path(original_relative_path: object, timestamp: datetime, short_id: str) -> str:
    identity = new_trash_identity(timestamp, short_id=short_id)
    original_path = normalize_original_path(original_relative_path)
    display_name = sanitize_display_filename(original_path)
    year, month, day = identity.date_parts
    return "/".join(
        (
            TRASH_ROOT,
            TRASH_OBJECTS_DIR,
            year,
            month,
            day,
            f"{identity.item_id}__{display_name}",
        )
    )


def build_metadata_path(original_relative_path: object, timestamp: datetime, short_id: str) -> str:
    return f"{build_object_path(original_relative_path, timestamp, short_id)}.json"


def build_trash_paths(
    original_relative_path: object,
    timestamp: datetime | None = None,
    *,
    short_id: str | None = None,
    short_id_factory: Callable[[], str] | None = None,
) -> TrashPaths:
    identity = new_trash_identity(timestamp, short_id=short_id, short_id_factory=short_id_factory)
    original_path = normalize_original_path(original_relative_path)
    display_name = sanitize_display_filename(original_path)
    return TrashPaths(
        item_id=identity.item_id,
        original_path=original_path,
        display_name=display_name,
        object_path=build_object_path(original_path, identity.timestamp, identity.short_id),
        metadata_path=build_metadata_path(original_path, identity.timestamp, identity.short_id),
        created_at=_coerce_timestamp(identity.timestamp).isoformat(),
    )


def configured_trash_relative_root(remote_trash_root: str, core_remote: str) -> str:
    root = str(remote_trash_root or "").strip().rstrip("/")
    remote = str(core_remote or "").strip().rstrip("/")
    if not root or not remote or not root.startswith(f"{remote}/"):
        return TRASH_ROOT
    relative = normalize_plan_path(root[len(remote) + 1:])
    return relative or TRASH_ROOT


def is_configured_trash_path(path: object, *, remote_trash_root: str, core_remote: str) -> bool:
    root = configured_trash_relative_root(remote_trash_root, core_remote)
    normalized = normalize_plan_path(path)
    return normalized == root or normalized.startswith(f"{root}/")


def build_unique_trash_paths(
    original_relative_path: object,
    timestamp: datetime | None = None,
    *,
    short_id_factory: Callable[[], str] | None = None,
    exists: Callable[[str], bool] | None = None,
    max_attempts: int = 100,
) -> TrashPaths:
    exists = exists or (lambda _path: False)
    last_paths: TrashPaths | None = None
    for _attempt in range(max_attempts):
        paths = build_trash_paths(
            original_relative_path,
            timestamp,
            short_id_factory=short_id_factory,
        )
        last_paths = paths
        if not exists(paths.object_path) and not exists(paths.metadata_path):
            return paths
    suffix = f" after {max_attempts} attempts"
    if last_paths is not None:
        suffix = f" for {last_paths.original_path}{suffix}"
    raise FileExistsError(f"could not allocate unique remote trash path{suffix}")


def metadata_payload(
    paths: TrashPaths,
    *,
    operation: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": TRASH_SCHEMA_VERSION,
        "id": paths.item_id,
        "item_id": paths.item_id,
        "original_path": paths.original_path,
        "original_name": paths.display_name,
        "object_path": paths.object_path,
        "trash_object_path": paths.object_path,
        "metadata_path": paths.metadata_path,
        "display_name": paths.display_name,
        "operation": operation,
        "reason": operation,
        "source": "pcloud-tools",
        "size": None,
        "trashed_at": paths.created_at,
        "created_at": paths.created_at,
    }
    if extra:
        payload["extra"] = dict(extra)
    return payload


def metadata_from_payload(payload: dict[str, Any]) -> TrashMetadata:
    schema_version = str(payload.get("schema_version", ""))
    if schema_version != TRASH_SCHEMA_VERSION:
        raise ValueError(f"unsupported remote trash metadata schema: {schema_version}")
    required = (
        "item_id",
        "original_path",
        "object_path",
        "metadata_path",
        "display_name",
        "operation",
        "created_at",
    )
    missing = [key for key in required if not payload.get(key)]
    if missing:
        raise ValueError(f"remote trash metadata missing required keys: {', '.join(missing)}")
    original_path = normalize_original_path(payload["original_path"])
    object_path = normalize_plan_path(payload["object_path"])
    metadata_path = normalize_plan_path(payload["metadata_path"])
    if not is_trash_path(object_path):
        raise ValueError(f"remote trash object path is outside trash root: {object_path}")
    if not is_trash_path(metadata_path):
        raise ValueError(f"remote trash metadata path is outside trash root: {metadata_path}")
    return TrashMetadata(
        schema_version=schema_version,
        item_id=str(payload["item_id"]),
        original_path=original_path,
        object_path=object_path,
        metadata_path=metadata_path,
        display_name=str(payload["display_name"]),
        operation=str(payload["operation"]),
        created_at=str(payload["created_at"]),
        payload=dict(payload),
    )


def write_metadata_file(path: Path, payload: dict[str, Any]) -> Path:
    metadata_from_payload(payload)
    return atomic_write_json(path, payload, sort_keys=True)


def read_metadata_file(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"remote trash metadata must be a JSON object: {path}")
    metadata_from_payload(payload)
    return payload


def init_index(db_path: Path) -> Path:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS remote_trash_index (
                item_id TEXT PRIMARY KEY,
                original_path TEXT NOT NULL,
                object_path TEXT NOT NULL,
                metadata_path TEXT NOT NULL,
                display_name TEXT NOT NULL,
                operation TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                status TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_remote_trash_original_path "
            "ON remote_trash_index(original_path)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_remote_trash_status "
            "ON remote_trash_index(status)"
        )
    return db_path


def write_index_record(
    db_path: Path,
    payload: dict[str, Any],
    *,
    status: str = "active",
    updated_at: str | None = None,
) -> TrashIndexRecord:
    metadata = metadata_from_payload(payload)
    updated_at = updated_at or datetime.now(timezone.utc).isoformat()
    metadata_json = json.dumps(metadata.payload, ensure_ascii=False, sort_keys=True)
    init_index(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO remote_trash_index (
                item_id,
                original_path,
                object_path,
                metadata_path,
                display_name,
                operation,
                created_at,
                updated_at,
                status,
                metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(item_id) DO UPDATE SET
                original_path = excluded.original_path,
                object_path = excluded.object_path,
                metadata_path = excluded.metadata_path,
                display_name = excluded.display_name,
                operation = excluded.operation,
                created_at = excluded.created_at,
                updated_at = excluded.updated_at,
                status = excluded.status,
                metadata_json = excluded.metadata_json
            """,
            (
                metadata.item_id,
                metadata.original_path,
                metadata.object_path,
                metadata.metadata_path,
                metadata.display_name,
                metadata.operation,
                metadata.created_at,
                updated_at,
                status,
                metadata_json,
            ),
        )
    record = read_index_record(db_path, metadata.item_id)
    if record is None:
        raise RuntimeError(f"remote trash index write did not persist item: {metadata.item_id}")
    return record


def read_index_record(db_path: Path, item_id: str) -> TrashIndexRecord | None:
    if not db_path.exists():
        return None
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT item_id, original_path, object_path, metadata_path, display_name,
                   operation, created_at, updated_at, status, metadata_json
            FROM remote_trash_index
            WHERE item_id = ?
            """,
            (item_id,),
        ).fetchone()
    if row is None:
        return None
    return _index_record_from_row(row)


def list_index_records(db_path: Path, *, status: str | None = None) -> tuple[TrashIndexRecord, ...]:
    if not db_path.exists():
        return ()
    query = (
        "SELECT item_id, original_path, object_path, metadata_path, display_name, "
        "operation, created_at, updated_at, status, metadata_json "
        "FROM remote_trash_index"
    )
    params: tuple[str, ...] = ()
    if status is not None:
        query += " WHERE status = ?"
        params = (status,)
    query += " ORDER BY created_at, item_id"
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(query, params).fetchall()
    return tuple(_index_record_from_row(row) for row in rows)


def update_index_record(
    db_path: Path,
    item_id: str,
    *,
    status: str | None = None,
    metadata_payload_update: dict[str, Any] | None = None,
    updated_at: str | None = None,
) -> TrashIndexRecord:
    existing = read_index_record(db_path, item_id)
    if existing is None:
        raise KeyError(item_id)
    payload = dict(existing.metadata_payload)
    if metadata_payload_update:
        payload.update(metadata_payload_update)
    return write_index_record(
        db_path,
        payload,
        status=status or existing.status,
        updated_at=updated_at or datetime.now(timezone.utc).isoformat(),
    )


def _index_record_from_row(row: sqlite3.Row) -> TrashIndexRecord:
    metadata_payload = json.loads(str(row["metadata_json"]))
    if not isinstance(metadata_payload, dict):
        raise ValueError(f"remote trash index row has invalid metadata JSON: {row['item_id']}")
    return TrashIndexRecord(
        item_id=str(row["item_id"]),
        original_path=str(row["original_path"]),
        object_path=str(row["object_path"]),
        metadata_path=str(row["metadata_path"]),
        display_name=str(row["display_name"]),
        operation=str(row["operation"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        status=str(row["status"]),
        metadata_payload=metadata_payload,
    )
