#!/usr/bin/env python3
"""Synthetic state benchmark; never reads production state or credentials.

Run from the repository root: .venv/bin/python scripts/benchmark-state-storage.py
"""

import json
from pathlib import Path
import resource
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from pcloud_tools.sqlite_state import Store, DB_NAME
from pcloud_tools.state_migration import migrate


def worker(root, mode):
    start = time.monotonic()
    if mode == "json":
        state = json.loads((root / "event-sync/state.json").read_text())
        queue = json.loads((root / "diffd/remote-changes.json").read_text())
        result = {"remaining": len(state["reconcile"]["pending"]), "queue": len(queue)}
    else:
        store = Store(root / DB_NAME)
        for _ in range(30):
            result = {
                "remaining": store.count("pending"),
                "queue": store.count("queue", "diffd"),
            }
        if mode == "append":
            for i in range(1000):
                store.append(
                    "diffd",
                    {
                        "path": f"Documents/changed-{i}",
                        "event_id": f"new-{i}",
                        "action": "download",
                    },
                )
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    print(
        json.dumps(
            {
                "mode": mode,
                "seconds": round(time.monotonic() - start, 3),
                "peak_MiB": round(
                    peak / (1024**2 if sys.platform == "darwin" else 1024), 1
                ),
                **result,
            }
        )
    )


if len(sys.argv) > 1:
    worker(Path(sys.argv[1]), sys.argv[2])
    raise SystemExit
with tempfile.TemporaryDirectory(prefix="pcloud-state-benchmark-") as temp:
    root = Path(temp)
    (root / "event-sync").mkdir()
    (root / "diffd").mkdir()
    version = {
        "exists": True,
        "size": 1024,
        "second": 123,
        "mtime_ns": 123000000000,
        "ctime_ns": 124000000000,
        "inode": 100,
        "device": 1,
        "hashes": {"sha1": "a" * 40, "md5": "b" * 32, "sha256": "c" * 64},
    }
    row = {
        "local": version,
        "cloud": {**version, "id": "100", "modified": "2026-09-28T00:00:00Z"},
    }
    with (root / "event-sync/state.json").open("w") as f:
        f.write('{"schema":"pcloud-event-sync.v1","baseline":{')
        for i in range(300000):
            f.write(
                ("," if i else "")
                + json.dumps(f"Documents/file-{i}")
                + ":"
                + json.dumps(row)
            )
        f.write(
            '},"reviews":{},"reconcile":{"id":"initial","captured":{"pushd":[],"diffd":[]},"pending":['
        )
        for i in range(300000):
            f.write(("," if i else "") + json.dumps(f"Documents/file-{i}"))
        f.write("]}}")
    with (root / "diffd/remote-changes.json").open("w") as f:
        f.write("[")
        for i in range(160000):
            f.write(
                ("," if i else "")
                + json.dumps(
                    {
                        "path": f"Documents/file-{i}",
                        "event_id": str(i),
                        "action": "download",
                        "reason": "synthetic",
                        "extra": "x" * 150,
                    }
                )
            )
        f.write("]")
    print(
        "fixture_MiB",
        round(sum(p.stat().st_size for p in root.rglob("*.json")) / 1024**2, 1),
        flush=True,
    )
    subprocess.run([sys.executable, __file__, str(root), "json"], check=True)
    t = time.monotonic()
    receipt = migrate(root)
    print("migration_seconds", round(time.monotonic() - t, 3), flush=True)
    subprocess.run([sys.executable, __file__, str(root), "sqlite"], check=True)
    subprocess.run([sys.executable, __file__, str(root), "append"], check=True)
