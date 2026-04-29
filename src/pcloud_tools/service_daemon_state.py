from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import AppConfig, ConfigIssue


@dataclass(frozen=True)
class ServiceDaemonState:
    name: str
    state_dir: Path
    pid_file: Path
    queue_file: Path
    cursor_file: Path
    last_event_file: Path
    last_plan_file: Path
    last_transfer_file: Path
    pid: int | None
    pid_running: bool | None
    queue_length: int
    cursor: str
    last_event: dict[str, Any] | None
    last_plan: dict[str, Any] | None
    last_transfer: dict[str, Any] | None
    issues: tuple[ConfigIssue, ...]


def service_daemon_state_dir(config: AppConfig, name: str) -> Path:
    return config.state_dir / name


def _state_files(config: AppConfig, name: str) -> dict[str, Path]:
    root = service_daemon_state_dir(config, name)
    return {
        "pid": root / "pid",
        "queue": root / "queue.json",
        "cursor": root / "cursor",
        "last_event": root / "last-event.json",
        "last_plan": root / "last-plan.json",
        "last_transfer": root / "last-transfer.json",
    }


def _read_text(path: Path, default: str) -> str:
    if not path.exists():
        return default
    return path.read_text().strip() or default


def _read_json(path: Path, name: str) -> tuple[Any, ConfigIssue | None]:
    if not path.exists():
        return None, None
    try:
        return json.loads(path.read_text()), None
    except (OSError, json.JSONDecodeError) as exc:
        return None, ConfigIssue(
            key=f"PCLOUD_TOOLS_{name.upper()}_STATE_{path.name.upper().replace('-', '_')}",
            level="warning",
            message=f"cannot read {name} state file {path}: {exc}",
        )


def _read_pid(path: Path, name: str) -> tuple[int | None, bool | None, ConfigIssue | None]:
    raw_pid = _read_text(path, "")
    if not raw_pid:
        return None, None, None
    try:
        pid = int(raw_pid)
    except ValueError:
        return None, None, ConfigIssue(
            key=f"PCLOUD_TOOLS_{name.upper()}_PID",
            level="warning",
            message=f"stored pid is invalid: {raw_pid!r}",
        )
    if pid <= 0:
        return None, None, ConfigIssue(
            key=f"PCLOUD_TOOLS_{name.upper()}_PID",
            level="warning",
            message=f"stored pid is invalid: {raw_pid!r}",
        )
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return pid, False, None
    except PermissionError:
        return pid, True, None
    return pid, True, None


def read_service_daemon_state(config: AppConfig, name: str) -> ServiceDaemonState:
    files = _state_files(config, name)
    issues: list[ConfigIssue] = []

    pid, pid_running, pid_issue = _read_pid(files["pid"], name)
    if pid_issue:
        issues.append(pid_issue)

    queue_payload, queue_issue = _read_json(files["queue"], name)
    if queue_issue:
        issues.append(queue_issue)
    if isinstance(queue_payload, list):
        queue_length = len(queue_payload)
    elif queue_payload is None:
        queue_length = 0
    else:
        queue_length = 0
        issues.append(
            ConfigIssue(
                key=f"PCLOUD_TOOLS_{name.upper()}_QUEUE",
                level="warning",
                message=f"{name} queue state must be a JSON list: {files['queue']}",
            )
        )

    last_event_payload, last_event_issue = _read_json(files["last_event"], name)
    if last_event_issue:
        issues.append(last_event_issue)
    last_plan_payload, last_plan_issue = _read_json(files["last_plan"], name)
    if last_plan_issue:
        issues.append(last_plan_issue)
    last_transfer_payload, last_transfer_issue = _read_json(files["last_transfer"], name)
    if last_transfer_issue:
        issues.append(last_transfer_issue)

    return ServiceDaemonState(
        name=name,
        state_dir=service_daemon_state_dir(config, name),
        pid_file=files["pid"],
        queue_file=files["queue"],
        cursor_file=files["cursor"],
        last_event_file=files["last_event"],
        last_plan_file=files["last_plan"],
        last_transfer_file=files["last_transfer"],
        pid=pid,
        pid_running=pid_running,
        queue_length=queue_length,
        cursor=_read_text(files["cursor"], "-"),
        last_event=last_event_payload if isinstance(last_event_payload, dict) else None,
        last_plan=last_plan_payload if isinstance(last_plan_payload, dict) else None,
        last_transfer=last_transfer_payload if isinstance(last_transfer_payload, dict) else None,
        issues=tuple(issues),
    )
