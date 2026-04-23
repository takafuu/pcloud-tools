from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

from .config import AppConfig


@dataclass(frozen=True)
class AutosyncState:
    loaded: bool
    state: str
    runs: str
    label: str
    plist: str


def read_autosync_state(config: AppConfig) -> AutosyncState:
    launchctl = shutil.which("launchctl")
    if launchctl is None:
        return AutosyncState(
            loaded=False,
            state="launchctl-missing",
            runs="-",
            label=config.autosync_label,
            plist=str(config.autosync_plist),
        )

    target = f"gui/{_uid()}/{config.autosync_label}"
    result = subprocess.run(
        [launchctl, "print", target],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return AutosyncState(
            loaded=False,
            state="not_loaded",
            runs="-",
            label=config.autosync_label,
            plist=str(config.autosync_plist),
        )

    state = "loaded"
    runs = "-"
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if line.startswith("state = "):
            state = line.split("=", 1)[1].strip()
        elif line.startswith("runs = "):
            runs = line.split("=", 1)[1].strip()

    return AutosyncState(
        loaded=True,
        state=state,
        runs=runs,
        label=config.autosync_label,
        plist=str(config.autosync_plist),
    )


def enable_autosync(config: AppConfig) -> None:
    launchctl = shutil.which("launchctl")
    if launchctl is None:
        raise RuntimeError("launchctl command not found")
    if not config.autosync_plist.exists():
        raise RuntimeError(f"autosync plist not found: {config.autosync_plist}")

    if read_autosync_state(config).loaded:
        return

    target = f"gui/{_uid()}/{config.autosync_label}"
    _run_checked([launchctl, "enable", target])
    _run_checked([launchctl, "bootstrap", f"gui/{_uid()}", str(config.autosync_plist)])


def disable_autosync(config: AppConfig) -> None:
    launchctl = shutil.which("launchctl")
    if launchctl is None:
        raise RuntimeError("launchctl command not found")

    target = f"gui/{_uid()}/{config.autosync_label}"
    if read_autosync_state(config).loaded:
        _run_checked([launchctl, "bootout", target])
    _run_checked([launchctl, "disable", target])


def _uid() -> str:
    import os

    return str(os.getuid())


def _run_checked(command: list[str]) -> None:
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        stderr = (result.stderr or result.stdout).strip()
        raise RuntimeError(stderr or f"command failed: {' '.join(command)}")
