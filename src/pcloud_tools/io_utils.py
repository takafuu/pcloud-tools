from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_text(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)
    os.replace(tmp_path, path)
    return path


def atomic_write_json(
    path: Path,
    payload: Any,
    *,
    indent: int | None = 2,
    ensure_ascii: bool = False,
    sort_keys: bool = False,
) -> Path:
    from .sqlite_state import database_for
    store = database_for(path)
    if store and path.name != 'transfer-attempts.json':
        if not isinstance(payload, list):
            raise ValueError('queue state must be a list')
        store.replace_queue(path.parent.name, payload)
        return path
    content = json.dumps(payload, indent=indent, ensure_ascii=ensure_ascii, sort_keys=sort_keys) + "\n"
    return atomic_write_text(path, content)


def read_json_state(path: Path):
    """Compatibility reader; hot paths use indexed Store operations directly."""
    from .sqlite_state import database_for
    store = database_for(path)
    if store:
        if path.name == 'transfer-attempts.json':return store.attempts(path.parent.name)
        return list(store.queue_rows(path.parent.name))
    return json.loads(path.read_text())
