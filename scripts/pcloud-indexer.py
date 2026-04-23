#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path


def env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value else default


def now_epoch() -> int:
    return int(time.time())


def default_db_path() -> Path:
    state_dir = Path(
        env("PCLOUD_TOOLS_STATE_DIR", str(Path(__file__).resolve().parents[1] / ".dev-state" / "state"))
    )
    return state_dir / "index" / "pcloud_index.db"


class Indexer:
    def __init__(self, db_path: Path, rclone_bin: str, vault_remote: str, crypt_remote: str):
        self.db_path = db_path
        self.rclone_bin = rclone_bin
        self.remotes = {
            "vault": vault_remote,
            "crypt": crypt_remote,
        }
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS entries (
              remote_type TEXT NOT NULL,
              path TEXT NOT NULL,
              is_dir INTEGER NOT NULL,
              size INTEGER,
              modtime TEXT,
              indexed_at INTEGER NOT NULL,
              PRIMARY KEY (remote_type, path)
            );
            CREATE INDEX IF NOT EXISTS idx_entries_remote_type ON entries(remote_type);
            CREATE INDEX IF NOT EXISTS idx_entries_path ON entries(path);

            CREATE TABLE IF NOT EXISTS meta (
              remote_type TEXT PRIMARY KEY,
              last_indexed INTEGER NOT NULL,
              source_remote TEXT NOT NULL,
              item_count INTEGER NOT NULL
            );
            """
        )
        self.conn.commit()

    def _iter_lsf_csv(self, remote: str, files_only: bool):
        fmt = "pst" if files_only else "pt"
        args = [
            self.rclone_bin,
            "lsf",
            remote,
            "--recursive",
            "--csv",
            "--format",
            fmt,
        ]
        args.append("--files-only" if files_only else "--dirs-only")

        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        assert proc.stdout is not None
        reader = csv.reader(proc.stdout)
        for row in reader:
            if row:
                yield row

        stderr = proc.stderr.read() if proc.stderr else ""
        ret = proc.wait()
        if ret != 0:
            raise RuntimeError(f"rclone lsf failed ({ret}): {stderr.strip()}")

    def rebuild_target(self, target: str) -> int:
        if target not in self.remotes:
            raise ValueError(f"invalid target: {target}")

        remote = self.remotes[target]
        indexed_at = now_epoch()

        cur = self.conn.cursor()
        cur.execute("DELETE FROM entries WHERE remote_type=?", (target,))

        count = 0
        batch: list[tuple[object, ...]] = []
        batch_size = 2000

        for row in self._iter_lsf_csv(remote, files_only=True):
            path = row[0]
            size = int(row[1]) if len(row) > 1 and row[1].isdigit() else None
            modtime = row[2] if len(row) > 2 else None
            batch.append((target, path, 0, size, modtime, indexed_at))
            count += 1
            if len(batch) >= batch_size:
                cur.executemany(
                    "INSERT OR REPLACE INTO entries(remote_type,path,is_dir,size,modtime,indexed_at) VALUES (?,?,?,?,?,?)",
                    batch,
                )
                batch.clear()

        for row in self._iter_lsf_csv(remote, files_only=False):
            path = row[0]
            modtime = row[1] if len(row) > 1 else None
            batch.append((target, path, 1, None, modtime, indexed_at))
            count += 1
            if len(batch) >= batch_size:
                cur.executemany(
                    "INSERT OR REPLACE INTO entries(remote_type,path,is_dir,size,modtime,indexed_at) VALUES (?,?,?,?,?,?)",
                    batch,
                )
                batch.clear()

        if batch:
            cur.executemany(
                "INSERT OR REPLACE INTO entries(remote_type,path,is_dir,size,modtime,indexed_at) VALUES (?,?,?,?,?,?)",
                batch,
            )

        cur.execute(
            "INSERT OR REPLACE INTO meta(remote_type,last_indexed,source_remote,item_count) VALUES (?,?,?,?)",
            (target, indexed_at, remote, count),
        )
        self.conn.commit()
        return count

    def query(self, target: str, needle: str, limit: int):
        if target == "all":
            sql = (
                "SELECT remote_type, path, is_dir, size, modtime, indexed_at "
                "FROM entries WHERE path LIKE ? ORDER BY remote_type, path LIMIT ?"
            )
            params = (f"%{needle}%", limit)
        else:
            sql = (
                "SELECT remote_type, path, is_dir, size, modtime, indexed_at "
                "FROM entries WHERE remote_type=? AND path LIKE ? ORDER BY path LIMIT ?"
            )
            params = (target, f"%{needle}%", limit)
        return self.conn.execute(sql, params).fetchall()

    def stats(self, target: str):
        if target == "all":
            return self.conn.execute(
                "SELECT remote_type,last_indexed,source_remote,item_count FROM meta ORDER BY remote_type"
            ).fetchall()
        row = self.conn.execute(
            "SELECT remote_type,last_indexed,source_remote,item_count FROM meta WHERE remote_type=?",
            (target,),
        ).fetchone()
        return [row] if row else []


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pcloud-indexer", description="Build/query local metadata index for pCloud remotes"
    )
    parser.add_argument("command", choices=["build", "update", "query", "stats"], help="Action to run")
    parser.add_argument("target", choices=["vault", "crypt", "all"], help="Target remote")
    parser.add_argument("needle", nargs="?", default="", help="Search keyword for query")
    parser.add_argument("--limit", type=int, default=100, help="Max rows for query (default: 100)")
    parser.add_argument(
        "--db",
        default=env("PCLOUD_INDEX_DB", str(default_db_path())),
        help="SQLite DB path",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    rclone_bin = env("RCLONE_BIN", "/usr/local/bin/rclone")

    indexer = Indexer(
        db_path=Path(args.db),
        rclone_bin=rclone_bin,
        vault_remote=env("PCLOUD_VAULT_REMOTE", "pcloud:vault"),
        crypt_remote=env("PCLOUD_CRYPT_REMOTE", "pcloud-crypt:"),
    )

    if args.command in {"build", "update"}:
        targets = ["vault", "crypt"] if args.target == "all" else [args.target]
        for target in targets:
            start = time.time()
            count = indexer.rebuild_target(target)
            elapsed = time.time() - start
            print(f"OK build target={target} count={count} elapsed_sec={elapsed:.1f}")
        return 0

    if args.command == "query":
        if not args.needle:
            print("ERROR: query requires <needle>", file=sys.stderr)
            return 2
        rows = indexer.query(args.target, args.needle, args.limit)
        for remote_type, path, is_dir, size, modtime, indexed_at in rows:
            kind = "dir" if is_dir else "file"
            size_text = "-" if size is None else str(size)
            print(f"{remote_type}\t{kind}\t{size_text}\t{modtime or '-'}\t{path}")
        print(f"rows={len(rows)}")
        return 0

    if args.command == "stats":
        rows = indexer.stats(args.target)
        if not rows:
            print("no index data")
            return 0
        for remote_type, last_indexed, source_remote, item_count in rows:
            print(
                f"target={remote_type} source={source_remote} items={item_count} "
                f"last_indexed={time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(last_indexed))}"
            )
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
