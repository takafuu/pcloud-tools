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


def _string(value: object, default: str = "") -> str:
    return str(value if value is not None else default).strip()


def _change_from_mapping(
    item: dict[str, Any], raw: str, default_diffid: str = "0"
) -> DiffdRemoteChange | InvalidDiffdRemoteChange:
    path_value = item.get("path", item.get("name", ""))
    path = normalize_plan_path(path_value)
    if not path:
        return InvalidDiffdRemoteChange(raw=raw, reason="missing or unsafe path")
    event = _string(item.get("event", item.get("type", item.get("action", "change"))), "change")
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


def parse_diff_response_text(text: str, source: str) -> DiffdResponseParseResult:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return DiffdResponseParseResult(
            source=source,
            diffid="0",
            changes=(),
            invalid=(InvalidDiffdRemoteChange(raw=text.strip(), reason=f"invalid JSON fixture: {exc}"),),
        )

    entries = _payload_entries(payload)
    if isinstance(entries, InvalidDiffdRemoteChange):
        return DiffdResponseParseResult(source=source, diffid="0", changes=(), invalid=(entries,))

    diffid, items = entries
    parsed: list[DiffdRemoteChange | InvalidDiffdRemoteChange] = []
    for item in items:
        if isinstance(item, dict):
            parsed.append(_change_from_mapping(item, json.dumps(item, ensure_ascii=False, sort_keys=True), diffid))
        elif isinstance(item, str):
            parsed.append(_change_from_mapping({"path": item}, item, diffid))
        else:
            parsed.append(InvalidDiffdRemoteChange(raw=repr(item), reason="diff entry must be object or string"))

    return DiffdResponseParseResult(
        source=source,
        diffid=diffid,
        changes=tuple(item for item in parsed if isinstance(item, DiffdRemoteChange)),
        invalid=tuple(item for item in parsed if isinstance(item, InvalidDiffdRemoteChange)),
    )


def parse_diff_response_fixture(path: Path) -> DiffdResponseParseResult:
    return parse_diff_response_text(path.read_text(), str(path))


def diff_changes_to_records(changes: tuple[DiffdRemoteChange, ...]) -> tuple[PlanRecord, ...]:
    return tuple(
        PlanRecord(path=change.path, action="download", reason=f"diff:{change.event}")
        for change in changes
    )
