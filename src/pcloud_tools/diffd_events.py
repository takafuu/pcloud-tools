from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .service_daemon_plan import PlanRecord, normalize_plan_path


@dataclass(frozen=True)
class DiffdRemoteChange:
    path: str
    event: str
    diffid: str
    raw: str


@dataclass(frozen=True)
class InvalidDiffdRemoteChange:
    raw: str
    reason: str


@dataclass(frozen=True)
class DiffdResponseParseResult:
    source: str
    diffid: str
    changes: tuple[DiffdRemoteChange, ...]
    invalid: tuple[InvalidDiffdRemoteChange, ...]
    folder_paths: dict[str, str]


def _string(value: object, default: str = "") -> str:
    return str(value if value is not None else default).strip()


def _metadata_path(item: dict[str, Any], folder_paths: dict[str, str]) -> str:
    metadata = item.get("metadata")
    if not isinstance(metadata, dict):
        return ""
    path = normalize_plan_path(metadata.get("path", ""))
    if path:
        return path
    name = _string(metadata.get("name"))
    if not name:
        return ""
    parent_id = _string(metadata.get("parentfolderid"), "0")
    if parent_id == "0":
        return normalize_plan_path(name)
    parent_path = folder_paths.get(parent_id)
    if not parent_path:
        return ""
    return normalize_plan_path(f"{parent_path}/{name}")


def _remember_folder_path(item: dict[str, Any], folder_paths: dict[str, str]) -> None:
    metadata = item.get("metadata")
    if not isinstance(metadata, dict) or not metadata.get("isfolder"):
        return
    folder_id = _string(metadata.get("folderid"))
    path = _metadata_path(item, folder_paths)
    if folder_id and path:
        folder_paths[folder_id] = path


def _change_from_mapping(
    item: dict[str, Any], raw: str, default_diffid: str = "0", folder_paths: dict[str, str] | None = None
) -> DiffdRemoteChange | InvalidDiffdRemoteChange | None:
    event = _string(item.get("event", item.get("type", item.get("action", "change"))), "change")
    metadata = item.get("metadata")
    if event in {"reset", "modifyuserinfo"}:
        return None
    if isinstance(metadata, dict) and metadata.get("isfolder"):
        return None
    path_value = item.get("path", "")
    if not path_value and isinstance(metadata, dict):
        path_value = _metadata_path(item, folder_paths or {})
    if not path_value:
        path_value = item.get("name", "")
    path = normalize_plan_path(path_value)
    if not path:
        return InvalidDiffdRemoteChange(raw=raw, reason="missing or unsafe path")
    diffid = _string(item.get("diffid", default_diffid), default_diffid)
    return DiffdRemoteChange(path=path, event=event or "change", diffid=diffid or default_diffid, raw=raw)


def _payload_entries(payload: Any) -> tuple[str, list[Any]] | InvalidDiffdRemoteChange:
    if isinstance(payload, list):
        return "0", payload
    if not isinstance(payload, dict):
        return InvalidDiffdRemoteChange(raw=repr(payload), reason="diff fixture must be an object or list")
    entries = payload.get("entries", payload.get("changes", payload.get("diff", [])))
    if not isinstance(entries, list):
        return InvalidDiffdRemoteChange(raw=json.dumps(payload, ensure_ascii=False), reason="diff entries must be a list")
    return _string(payload.get("diffid", payload.get("newdiffid", "0")), "0"), entries


def parse_diff_response_text(
    text: str, source: str, initial_folder_paths: dict[str, str] | None = None
) -> DiffdResponseParseResult:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return DiffdResponseParseResult(
            source=source,
            diffid="0",
            changes=(),
            invalid=(InvalidDiffdRemoteChange(raw=text.strip(), reason=f"invalid JSON fixture: {exc}"),),
            folder_paths=dict(initial_folder_paths or {}),
        )

    entries = _payload_entries(payload)
    if isinstance(entries, InvalidDiffdRemoteChange):
        return DiffdResponseParseResult(
            source=source,
            diffid="0",
            changes=(),
            invalid=(entries,),
            folder_paths=dict(initial_folder_paths or {}),
        )

    diffid, items = entries
    folder_paths: dict[str, str] = dict(initial_folder_paths or {})
    parsed: list[DiffdRemoteChange | InvalidDiffdRemoteChange] = []
    for item in items:
        if isinstance(item, dict):
            _remember_folder_path(item, folder_paths)
            change = _change_from_mapping(item, json.dumps(item, ensure_ascii=False, sort_keys=True), diffid, folder_paths)
            if change is not None:
                parsed.append(change)
        elif isinstance(item, str):
            parsed.append(_change_from_mapping({"path": item}, item, diffid))
        else:
            parsed.append(InvalidDiffdRemoteChange(raw=repr(item), reason="diff entry must be object or string"))

    return DiffdResponseParseResult(
        source=source,
        diffid=diffid,
        changes=tuple(item for item in parsed if isinstance(item, DiffdRemoteChange)),
        invalid=tuple(item for item in parsed if isinstance(item, InvalidDiffdRemoteChange)),
        folder_paths=folder_paths,
    )


def parse_diff_response_fixture(path: Path, initial_folder_paths: dict[str, str] | None = None) -> DiffdResponseParseResult:
    return parse_diff_response_text(path.read_text(), str(path), initial_folder_paths)


def diff_changes_to_records(changes: tuple[DiffdRemoteChange, ...]) -> tuple[PlanRecord, ...]:
    return tuple(
        PlanRecord(path=change.path, action="download", reason=f"diff:{change.event}")
        for change in changes
    )
