from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .config import AppConfig

_DATE_LINE_RE = re.compile(r"^\d{4}/\d{2}/\d{2} ")


@dataclass(frozen=True)
class SyncLogPointers:
    last_sync: str
    last_error: str
    latest_rclone_log: str
    latest_stdout_log: str
    latest_stderr_log: str


@dataclass(frozen=True)
class SyncState:
    state: str
    mode: str
    activity: str
    current_log: str
    last_sync: str
    last_error: str
    last_log: str
    reason: str


@dataclass(frozen=True)
class SyncProgress:
    log_path: Path
    scanned_entries: str
    compared_entries: str
    files_transferred: str
    bytes_transferred: str
    rate: str
    eta: str
    elapsed: str
    activity: str


def sync_status_log_path(config: AppConfig) -> Path:
    return config.core_dir / "bisync_status.log"


def sync_error_log_path(config: AppConfig) -> Path:
    return config.core_dir / "bisync_error.log"


def sync_last_rclone_log_file(config: AppConfig) -> Path:
    return config.state_dir / "last-rclone-log"


def sync_last_stdout_log_file(config: AppConfig) -> Path:
    return config.state_dir / "last-stdout-log"


def sync_last_stderr_log_file(config: AppConfig) -> Path:
    return config.state_dir / "last-stderr-log"


def _read_last_line(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return "(none)"
    lines = path.read_text().splitlines()
    return lines[-1] if lines else "(none)"


def _read_pointer(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return "-"
    target = path.read_text().strip()
    if not target:
        return "-"
    return target


def read_latest_sync_logs(config: AppConfig) -> SyncLogPointers:
    return SyncLogPointers(
        last_sync=_read_last_line(sync_status_log_path(config)),
        last_error=_read_last_line(sync_error_log_path(config)),
        latest_rclone_log=_read_pointer(sync_last_rclone_log_file(config)),
        latest_stdout_log=_read_pointer(sync_last_stdout_log_file(config)),
        latest_stderr_log=_read_pointer(sync_last_stderr_log_file(config)),
    )


def latest_sync_activity(log_path: Path) -> str:
    if not log_path.exists() or not log_path.is_file():
        return "-"
    activity = "-"
    for line in log_path.read_text().splitlines()[-120:]:
        if "Transferring:" in line:
            activity = "transferring"
        elif "Checking potential conflicts" in line or "Checking:" in line:
            activity = "checking"
        elif "Applying changes" in line:
            activity = "applying"
        elif "Building Path1 and Path2 listings" in line:
            activity = "listing"
    return activity


def latest_sync_progress_block(log_path: Path) -> str:
    if not log_path.exists() or not log_path.is_file():
        return ""

    last_block = ""
    block_lines: list[str] = []
    in_block = False

    for line in log_path.read_text().splitlines():
        if _DATE_LINE_RE.match(line):
            if in_block and block_lines:
                last_block = "\n".join(block_lines)
            in_block = False
            block_lines = []
            continue

        if line.startswith("Transferred:") and "ETA" in line:
            if in_block and block_lines:
                last_block = "\n".join(block_lines)
            in_block = True
            block_lines = [line]
            continue

        if in_block:
            block_lines.append(line)

    if in_block and block_lines:
        last_block = "\n".join(block_lines)
    return last_block


def sync_progress_log_path(config: AppConfig) -> Path | None:
    pointers = read_latest_sync_logs(config)
    if pointers.latest_rclone_log != "-":
        candidate = Path(pointers.latest_rclone_log).expanduser()
        if candidate.exists():
            return candidate

    if config.log_dir.exists():
        candidates = sorted(config.log_dir.glob("bisync-*.log"), reverse=True)
        for candidate in candidates:
            if candidate.is_file():
                return candidate
    return None


def read_sync_state(config: AppConfig) -> SyncState:
    pointers = read_latest_sync_logs(config)
    current_log = pointers.latest_rclone_log
    activity = "-"
    state = "idle" if pointers.last_sync == "(none)" else "synced"
    reason = ""

    if current_log != "-":
        log_path = Path(current_log).expanduser()
        activity = latest_sync_activity(log_path)
        state = "syncing" if activity != "-" else "idle"

    if pointers.last_sync != "(none)" and "ERROR" in pointers.last_sync:
        state = "sync_error"
        reason = pointers.last_error

    return SyncState(
        state=state,
        mode="-",
        activity=activity,
        current_log=current_log,
        last_sync=pointers.last_sync,
        last_error=pointers.last_error,
        last_log=pointers.latest_rclone_log,
        reason=reason,
    )


def _extract_after_prefix(line: str, prefix: str) -> str:
    if not line.startswith(prefix):
        return ""
    return line[len(prefix) :].strip()


def parse_sync_progress(config: AppConfig) -> SyncProgress | None:
    log_path = sync_progress_log_path(config)
    if log_path is None:
        return None

    block = latest_sync_progress_block(log_path)
    if not block:
        return None

    lines = block.splitlines()
    if len(lines) < 4:
        return None

    bytes_line, checks_line, files_line, elapsed_line = lines[:4]

    scanned_entries = ""
    compared_entries = ""
    files_transferred = ""
    bytes_transferred = ""
    rate = ""
    eta = ""
    elapsed = _extract_after_prefix(elapsed_line, "Elapsed time:")

    bytes_match = re.search(r"Transferred:\s*(.+?) \/ ([^,]+),\s*([0-9]+%),\s*([^,]+), ETA (.+)$", bytes_line)
    if bytes_match:
        bytes_transferred = f"{bytes_match.group(1).strip()} / {bytes_match.group(2).strip()} ({bytes_match.group(3)})"
        rate = bytes_match.group(4).strip()
        eta = bytes_match.group(5).strip()

    checks_match = re.search(r"Checks:\s*([0-9]+) \/ ([0-9]+),\s*([0-9]+%).*Listed\s*([0-9]+)", checks_line)
    if checks_match:
        compared_entries = f"{checks_match.group(1)} / {checks_match.group(2)} ({checks_match.group(3)})"
        scanned_entries = checks_match.group(4)

    files_match = re.search(r"Transferred:\s*([0-9]+) \/ ([0-9]+),\s*([0-9]+%)", files_line)
    if files_match:
        files_transferred = f"{files_match.group(1)} / {files_match.group(2)} ({files_match.group(3)})"

    return SyncProgress(
        log_path=log_path,
        scanned_entries=scanned_entries,
        compared_entries=compared_entries,
        files_transferred=files_transferred,
        bytes_transferred=bytes_transferred,
        rate=rate,
        eta=eta,
        elapsed=elapsed,
        activity=latest_sync_activity(log_path),
    )
