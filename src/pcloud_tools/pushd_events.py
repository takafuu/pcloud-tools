from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .service_daemon_plan import PlanRecord, normalize_plan_path


@dataclass(frozen=True)
class PushdFswatchEvent:
    path: str
    flags: tuple[str, ...]
    raw: str


@dataclass(frozen=True)
class InvalidPushdEvent:
    raw: str
    reason: str


@dataclass(frozen=True)
class PushdFswatchParseResult:
    source: Path
    events: tuple[PushdFswatchEvent, ...]
    invalid: tuple[InvalidPushdEvent, ...]


def _flags_from_value(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(flag for flag in value.replace(",", " ").split() if flag)
    if isinstance(value, list):
        return tuple(str(flag).strip() for flag in value if str(flag).strip())
    return (str(value).strip(),) if str(value).strip() else ()


def _event_from_mapping(item: dict[str, Any], raw: str) -> PushdFswatchEvent | InvalidPushdEvent:
    path = normalize_plan_path(item.get("path", ""))
    if not path:
        return InvalidPushdEvent(raw=raw, reason="missing or unsafe path")
    flags = _flags_from_value(item.get("flags", item.get("events")))
    return PushdFswatchEvent(path=path, flags=flags, raw=raw)


def _event_from_line(line: str) -> PushdFswatchEvent | InvalidPushdEvent:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return InvalidPushdEvent(raw=line, reason="blank or comment")
    if stripped.startswith("{"):
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as exc:
            return InvalidPushdEvent(raw=line, reason=f"invalid JSON event: {exc}")
        if not isinstance(payload, dict):
            return InvalidPushdEvent(raw=line, reason="JSON event must be an object")
        return _event_from_mapping(payload, stripped)

    if "\t" in stripped:
        path_part, flags_part = stripped.split("\t", 1)
        flags = _flags_from_value(flags_part)
    else:
        path_part = stripped
        flags = ()

    path = normalize_plan_path(path_part)
    if not path:
        return InvalidPushdEvent(raw=line, reason="missing or unsafe path")
    return PushdFswatchEvent(path=path, flags=flags, raw=line)


def parse_fswatch_event_line(line: str) -> PushdFswatchEvent | InvalidPushdEvent:
    return _event_from_line(line)


def _events_from_payload(payload: Any, raw_text: str) -> list[PushdFswatchEvent | InvalidPushdEvent]:
    if not isinstance(payload, list):
        return [InvalidPushdEvent(raw=raw_text, reason="JSON fixture must be a list")]
    events: list[PushdFswatchEvent | InvalidPushdEvent] = []
    for item in payload:
        if isinstance(item, dict):
            events.append(_event_from_mapping(item, json.dumps(item, ensure_ascii=False, sort_keys=True)))
        elif isinstance(item, str):
            events.append(_event_from_line(item))
        else:
            events.append(InvalidPushdEvent(raw=repr(item), reason="fixture item must be object or string"))
    return events


def parse_fswatch_fixture(path: Path) -> PushdFswatchParseResult:
    text = path.read_text()
    stripped = text.strip()
    if stripped.startswith("["):
        try:
            parsed = _events_from_payload(json.loads(stripped), stripped)
        except json.JSONDecodeError as exc:
            parsed = [InvalidPushdEvent(raw=stripped, reason=f"invalid JSON fixture: {exc}")]
    else:
        parsed = [_event_from_line(line) for line in text.splitlines()]

    return PushdFswatchParseResult(
        source=path,
        events=tuple(item for item in parsed if isinstance(item, PushdFswatchEvent)),
        invalid=tuple(item for item in parsed if isinstance(item, InvalidPushdEvent)),
    )


def fswatch_events_to_records(events: tuple[PushdFswatchEvent, ...]) -> tuple[PlanRecord, ...]:
    records: list[PlanRecord] = []
    for event in events:
        normalized_flags = {flag.strip().lower().replace("_", "-") for flag in event.flags}
        action = "upload"
        if any("remove" in flag or "delete" in flag for flag in normalized_flags):
            action = "delete"
        elif any("rename" in flag or "move" in flag for flag in normalized_flags):
            action = "rename"
        reason = "fswatch"
        if event.flags:
            reason = f"fswatch:{','.join(event.flags)}"
        records.append(PlanRecord(path=event.path, action=action, reason=reason))
    return tuple(records)
