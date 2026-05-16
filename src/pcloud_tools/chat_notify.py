from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
import os
from pathlib import Path

from .config import AppConfig, ConfigIssue, parse_env_file


@dataclass(frozen=True)
class ChatNotifyResult:
    attempted: bool
    enabled: bool
    command: tuple[str, ...]
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    issue: ConfigIssue | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "attempted": self.attempted,
            "enabled": self.enabled,
            "command": list(self.command),
            "returncode": self.returncode if self.returncode is not None else "-",
            "stdout": self.stdout,
            "stderr": self.stderr,
            "issue": self.issue.message if self.issue else "-",
        }


def build_chat_notify_command(config: AppConfig, message: str) -> tuple[str, ...]:
    parts = shlex.split(config.chat_notify_cmd)
    if "{message}" in parts:
        return tuple(message if part == "{message}" else part for part in parts)
    return tuple([*parts, message])


def chat_notify_status(config: AppConfig) -> dict[str, object]:
    command = build_chat_notify_command(config, "<message>")
    dedupe_seconds = os.environ.get("PCLOUD_TOOLS_CHAT_NOTIFY_DEDUPE_SECONDS", "3600")
    return {
        "chat notify enabled": "yes" if config.chat_notify_enabled else "no",
        "chat notify command": list(command),
        "chat notify mode": "abnormal-only" if config.chat_notify_enabled else "off",
        "chat notify dedupe seconds": dedupe_seconds,
        "chat notify env enabled key": "PCLOUD_TOOLS_CHAT_NOTIFY_ENABLED",
        "chat notify env command key": "PCLOUD_TOOLS_CHAT_NOTIFY_CMD",
        "chat notify env dedupe key": "PCLOUD_TOOLS_CHAT_NOTIFY_DEDUPE_SECONDS",
    }


def send_chat_notification(
    config: AppConfig,
    message: str,
    *,
    force: bool = False,
    timeout_seconds: int = 10,
) -> ChatNotifyResult:
    command = build_chat_notify_command(config, message)
    if not config.chat_notify_enabled and not force:
        return ChatNotifyResult(attempted=False, enabled=False, command=command)
    if not command:
        return ChatNotifyResult(
            attempted=False,
            enabled=config.chat_notify_enabled,
            command=command,
            issue=ConfigIssue(
                key="PCLOUD_TOOLS_CHAT_NOTIFY_CMD",
                level="warning",
                message="chat notify command is empty",
            ),
        )
    try:
        completed = subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ChatNotifyResult(
            attempted=True,
            enabled=config.chat_notify_enabled,
            command=command,
            issue=ConfigIssue(
                key="PCLOUD_TOOLS_CHAT_NOTIFY",
                level="warning",
                message=f"chat notify command failed to run: {exc}",
            ),
        )
    issue = None
    if completed.returncode != 0:
        issue = ConfigIssue(
            key="PCLOUD_TOOLS_CHAT_NOTIFY",
            level="warning",
            message=f"chat notify command exited {completed.returncode}",
        )
    return ChatNotifyResult(
        attempted=True,
        enabled=config.chat_notify_enabled,
        command=command,
        returncode=completed.returncode,
        stdout=completed.stdout.strip(),
        stderr=completed.stderr.strip(),
        issue=issue,
    )


def set_chat_notify_enabled(env_file: Path, enabled: bool) -> Path:
    env_file.parent.mkdir(parents=True, exist_ok=True)
    key = "PCLOUD_TOOLS_CHAT_NOTIFY_ENABLED"
    value = "1" if enabled else "0"
    if not env_file.exists():
        env_file.write_text(f"{key}={value}\n")
        return env_file
    lines = env_file.read_text().splitlines()
    updated: list[str] = []
    replaced = False
    for line in lines:
        if line.strip().startswith(f"{key}="):
            updated.append(f"{key}={value}")
            replaced = True
        else:
            updated.append(line)
    if not replaced:
        updated.append(f"{key}={value}")
    env_file.write_text("\n".join(updated) + "\n")
    return env_file


def configured_chat_notify_enabled(env_file: Path) -> bool:
    try:
        values = parse_env_file(env_file)
    except Exception:
        # Notification setup failures must not block the core workflow.
        return False
    return values.get("PCLOUD_TOOLS_CHAT_NOTIFY_ENABLED", "0").strip() in {"1", "true", "yes", "on"}
