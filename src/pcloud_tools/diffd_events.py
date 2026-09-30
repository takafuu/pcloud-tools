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
    file_id: str = ""
    modified: object = None
    size: object = None
    event_time: object = None


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
    requires_reconciliation: bool = False


def _string(value: object, default: str = "") -> str:
    return str(value if value is not None else default).strip()


def _path_requires_rclone_resolution(value: str) -> bool:
    # /diff names are pCloud-native, whereas lsjson uses rclone Standard
    # names. pCloud's default encoding also replaces BackSlash. A native
    # escape (including its quote rune) is ambiguous without the backend
    # configuration; let rclone resolve it in a scoped inventory instead of
    # consuming an event against a different, apparently absent path.
    # https://rclone.org/pcloud/#encoding
    return (value != value.strip()
            or any(ord(c) < 32 or 0x2400 <= ord(c) <= 0x241f
                   or c in "\\\x7f‛／＼␡" for c in value)
            or any(part in {"．", "．．"} for part in value.split("/")))


def _metadata_path(item: dict[str, Any], folder_paths: dict[str, str]) -> str:
    metadata = item.get("metadata")
    if not isinstance(metadata, dict):
        return ""
    raw_path = metadata.get("path", "")
    if raw_path:
        return str(raw_path)
    name = metadata.get("name", "")
    if not isinstance(name, str):
        return ""
    if _path_requires_rclone_resolution(name):
        # Legacy plan-path parsing cannot represent these names losslessly.
        # Request a full rclone inventory instead of targeting a different name.
        return ""
    if not name:
        return ""
    parent_id = _string(metadata.get("parentfolderid"), "0")
    if parent_id == "0":
        return normalize_plan_path(name)
    if parent_id not in folder_paths:
        return ""
    parent_path = folder_paths[parent_id]
    if not parent_path:
        return normalize_plan_path(name)
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
    raw_path = str(path_value or "")
    if _path_requires_rclone_resolution(raw_path):
        return InvalidDiffdRemoteChange(raw=raw, reason="path requires lossless rclone reconciliation")
    path = normalize_plan_path(path_value)
    if not path:
        return InvalidDiffdRemoteChange(raw=raw, reason="missing or unsafe path")
    diffid = _string(item.get("diffid", default_diffid), default_diffid)
    file_id = _string(metadata.get("fileid")) if isinstance(metadata, dict) else ""
    if not file_id.isdigit():
        file_id = ""
    return DiffdRemoteChange(path=path, event=event or "change", diffid=diffid or default_diffid, raw=raw, file_id=file_id,
                            modified=metadata.get("modified") if isinstance(metadata, dict) else None,
                            size=metadata.get("size") if isinstance(metadata, dict) else None, event_time=item.get("time"))


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
        requires_reconciliation=any(isinstance(i, InvalidDiffdRemoteChange) for i in parsed) or any(isinstance(i, dict) and (i.get("event") == "reset" or
            isinstance(i.get("metadata"), dict) and i["metadata"].get("isfolder")) for i in items),
    )


def parse_diff_response_fixture(path: Path, initial_folder_paths: dict[str, str] | None = None) -> DiffdResponseParseResult:
    return parse_diff_response_text(path.read_text(), str(path), initial_folder_paths)


def diff_changes_to_records(changes: tuple[DiffdRemoteChange, ...]) -> tuple[PlanRecord, ...]:
    records: list[PlanRecord] = []
    # /diff entries are ordered. Collapse repeated identity observations in
    # this response before planning, retaining the latest name/action.
    latest: dict[str, DiffdRemoteChange] = {}
    for change in changes:
        if change.file_id:
            previous = latest.get(change.file_id)
            if previous is None or not (previous.diffid.isdigit() and change.diffid.isdigit()) or int(change.diffid) >= int(previous.diffid):
                latest[change.file_id] = change
    for change in changes:
        if change.file_id and latest[change.file_id] is not change:
            continue
        event = change.event.strip().lower().replace("_", "-")
        action = "download"
        if "delete" in event or "remove" in event:
            action = "delete"
        elif "rename" in event or "move" in event:
            # With a stable file identity, fetch the new path. Never rename or
            # delete a local file as a side effect of queue coalescing.
            action = "download" if change.file_id else "rename"
        records.append(
            PlanRecord(
                path=change.path,
                action=action,
                reason=f"diff:{change.event}",
                extra={"diffid": change.diffid,
                       **({"remote_modified": change.modified} if change.modified is not None else {}),
                       **({"remote_size": change.size} if change.size is not None else {}),
                       **({"remote_event_time": change.event_time} if change.event_time is not None else {}),
                       **({"remote_file_id": change.file_id} if change.file_id else {})},
            )
        )
    return tuple(records)
