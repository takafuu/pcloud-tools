from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import AppConfig, ConfigIssue


@dataclass(frozen=True)
class PendingDownload:
    path: str
    diffid: str
    reason: str
    recorded_at: str


@dataclass(frozen=True)
class NotificationRecord:
    message: str
    level: str
    recorded_at: str


@dataclass(frozen=True)
class DaemonState:
    state_dir: Path
    diffid_file: Path
    auto_download_file: Path
    pending_downloads_file: Path
    notification_file: Path
    diffid: str
    auto_download_enabled: bool
    pending_downloads: tuple[PendingDownload, ...]
    last_notification: NotificationRecord | None
    issues: tuple[ConfigIssue, ...]


def daemon_state_dir(config: AppConfig) -> Path:
    return config.state_dir / "daemon"


def _state_files(config: AppConfig) -> dict[str, Path]:
    root = daemon_state_dir(config)
    return {
        "diffid": root / "diffid",
        "auto_download": root / "auto-download",
        "pending_downloads": root / "pending-downloads.json",
        "notification": root / "last-notification.json",
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_text(path: Path, default: str) -> str:
    if not path.exists():
        return default
    return path.read_text().strip() or default


def _read_json(path: Path) -> tuple[Any, ConfigIssue | None]:
    if not path.exists():
        return None, None
    try:
        return json.loads(path.read_text()), None
    except (OSError, json.JSONDecodeError) as exc:
        return None, ConfigIssue(
            key=f"PCLOUD_TOOLS_DAEMON_STATE_{path.name.upper().replace('-', '_')}",
            level="warning",
            message=f"cannot read daemon state file {path}: {exc}",
        )


def normalize_diffid(value: str) -> str:
    diffid = value.strip()
    if not diffid or not diffid.isdigit():
        raise ValueError("diffid must be a non-negative integer")
    return diffid


def read_daemon_state(config: AppConfig) -> DaemonState:
    files = _state_files(config)
    issues: list[ConfigIssue] = []

    raw_diffid = _read_text(files["diffid"], "0")
    try:
        diffid = normalize_diffid(raw_diffid)
    except ValueError:
        diffid = raw_diffid
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_DAEMON_DIFFID",
                level="warning",
                message=f"stored diffid is invalid: {raw_diffid!r}",
            )
        )

    raw_auto_download = _read_text(files["auto_download"], "off").lower()
    if raw_auto_download in {"1", "true", "yes", "on", "enabled"}:
        auto_download_enabled = True
    elif raw_auto_download in {"0", "false", "no", "off", "disabled"}:
        auto_download_enabled = False
    else:
        auto_download_enabled = False
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_DAEMON_AUTO_DOWNLOAD",
                level="warning",
                message=f"stored auto-download value is invalid: {raw_auto_download!r}",
            )
        )

    pending_payload, pending_issue = _read_json(files["pending_downloads"])
    if pending_issue:
        issues.append(pending_issue)
    pending: list[PendingDownload] = []
    if isinstance(pending_payload, list):
        for item in pending_payload:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path", "")).strip()
            if not path:
                continue
            pending.append(
                PendingDownload(
                    path=path,
                    diffid=str(item.get("diffid", "-")),
                    reason=str(item.get("reason", "remote-change")),
                    recorded_at=str(item.get("recorded_at", "-")),
                )
            )
    elif pending_payload is not None:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_DAEMON_PENDING_DOWNLOADS",
                level="warning",
                message=f"pending downloads state must be a JSON list: {files['pending_downloads']}",
            )
        )

    notification_payload, notification_issue = _read_json(files["notification"])
    if notification_issue:
        issues.append(notification_issue)
    notification = None
    if isinstance(notification_payload, dict):
        message = str(notification_payload.get("message", "")).strip()
        if message:
            notification = NotificationRecord(
                message=message,
                level=str(notification_payload.get("level", "info")),
                recorded_at=str(notification_payload.get("recorded_at", "-")),
            )

    return DaemonState(
        state_dir=daemon_state_dir(config),
        diffid_file=files["diffid"],
        auto_download_file=files["auto_download"],
        pending_downloads_file=files["pending_downloads"],
        notification_file=files["notification"],
        diffid=diffid,
        auto_download_enabled=auto_download_enabled,
        pending_downloads=tuple(pending),
        last_notification=notification,
        issues=tuple(issues),
    )


def write_diffid(config: AppConfig, diffid: str) -> str:
    normalized = normalize_diffid(diffid)
    files = _state_files(config)
    files["diffid"].parent.mkdir(parents=True, exist_ok=True)
    files["diffid"].write_text(f"{normalized}\n")
    return normalized


def set_auto_download(config: AppConfig, enabled: bool) -> None:
    files = _state_files(config)
    files["auto_download"].parent.mkdir(parents=True, exist_ok=True)
    files["auto_download"].write_text("on\n" if enabled else "off\n")


def add_pending_download(
    config: AppConfig, path: str, diffid: str = "-", reason: str = "remote-change"
) -> PendingDownload:
    state = read_daemon_state(config)
    item = PendingDownload(path=path, diffid=diffid, reason=reason, recorded_at=_now())
    payload = [asdict(existing) for existing in state.pending_downloads]
    payload.append(asdict(item))
    state.pending_downloads_file.parent.mkdir(parents=True, exist_ok=True)
    state.pending_downloads_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return item


def clear_pending_downloads(config: AppConfig) -> int:
    state = read_daemon_state(config)
    count = len(state.pending_downloads)
    state.pending_downloads_file.parent.mkdir(parents=True, exist_ok=True)
    state.pending_downloads_file.write_text("[]\n")
    return count


def record_notification(config: AppConfig, message: str, level: str = "info") -> NotificationRecord:
    record = NotificationRecord(message=message, level=level, recorded_at=_now())
    files = _state_files(config)
    files["notification"].parent.mkdir(parents=True, exist_ok=True)
    files["notification"].write_text(json.dumps(asdict(record), indent=2, ensure_ascii=False) + "\n")
    return record
