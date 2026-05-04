from __future__ import annotations

import configparser
import json
import os
from dataclasses import dataclass
from pathlib import Path

from .config import AppConfig


@dataclass(frozen=True)
class RclonePcloudCredentials:
    remote_name: str
    hostname: str
    access_token: str
    source_path: Path


def rclone_config_path() -> Path:
    configured = os.environ.get("RCLONE_CONFIG")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".config" / "rclone" / "rclone.conf"


def remote_name_from_remote(remote: str) -> str:
    return remote.split(":", 1)[0].strip()


def load_rclone_pcloud_credentials(config: AppConfig) -> RclonePcloudCredentials | None:
    path = rclone_config_path()
    remote_name = remote_name_from_remote(config.core_remote)
    if not remote_name or not path.exists():
        return None

    parser = configparser.ConfigParser()
    parser.read(path)
    if not parser.has_section(remote_name):
        return None

    section = parser[remote_name]
    if section.get("type", "").strip() != "pcloud":
        return None
    raw_token = section.get("token", "").strip()
    if not raw_token:
        return None
    try:
        token_payload = json.loads(raw_token)
    except json.JSONDecodeError:
        return None
    access_token = str(token_payload.get("access_token", "")).strip()
    if not access_token:
        return None

    hostname = section.get("hostname", "api.pcloud.com").strip() or "api.pcloud.com"
    return RclonePcloudCredentials(
        remote_name=remote_name,
        hostname=hostname,
        access_token=access_token,
        source_path=path,
    )
