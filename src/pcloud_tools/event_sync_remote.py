"""Small rclone adapter for versioned event synchronization.

rclone owns listing, copying, server moves, retries and transfer concurrency.
The caller owns decisions, generation checks and recovery receipts.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile
from datetime import datetime, timezone
import json

from .transfer_executor import run_transfer_batch
from .transfer_state import TransferStateError


class SyncError(TransferStateError):
    pass


LOCAL_ENCODING = "Slash,InvalidUtf8,Dot"


def _encode_component(value, controls):
    # The supported UTF-8 subset of rclone lib/encoder (v1.75.1).
    # Both local and Standard encode slash/NUL/dot; only Standard adds Ctl/Del.
    if value in {".", ".."}:
        return value.replace(".", "．")
    if value in {"．", "．．"}:
        return "".join("‛" + c for c in value)
    result = []
    for c in value:
        if c == "\0": result.append("␀")
        elif c == "/": result.append("／")
        elif c in "‛␀／": result.append("‛" + c)
        elif controls and 0 < ord(c) < 32: result.append(chr(0x2400 + ord(c)))
        elif controls and c == "\x7f": result.append("␡")
        elif controls and (0x2401 <= ord(c) <= 0x241f or c == "␡"): result.append("‛" + c)
        else: result.append(c)
    return "".join(result)


def _decode_component(value, controls):
    dots = {"．": ".", "．．": "..", "‛．": "．", "‛．‛．": "．．"}
    if value in dots:
        return dots[value]
    replaced = {"␀": "\0", "／": "/", "‛": "‛"}
    if controls:
        replaced.update({chr(0x2400 + c): chr(c) for c in range(1, 32)})
        replaced["␡"] = "\x7f"
    result, i = [], 0
    while i < len(value):
        c = value[i]
        if c == "‛" and i + 1 < len(value):
            following = value[i + 1]
            if following in replaced:
                result.append(following); i += 2; continue
            # The local InvalidUtf8 flag treats quoted hex pairs as raw bytes.
            # Python's UTF-8 JSON/path boundary cannot safely reinterpret those.
            if not controls and len(value[i + 1:i + 3]) == 2 and all(x in "0123456789abcdefABCDEF" for x in value[i + 1:i + 3]):
                raise SyncError("ファイル名の文字コード対応を確認できません")
            result.append(c)
        else:
            result.append(replaced.get(c, c))
        i += 1
    return "".join(result)


def rclone_path(path: str, *, decode=False) -> str:
    """Compose Standard.Encode(Local.Decode()) or the official inverse.

    Pin the normal local backend encoding, not Raw. Slash/dot stay encoded
    on both sides; never reinterpret a fullwidth slash as a POSIX separator.
    Reject non-roundtripping representations instead of guessing a new name.
    """
    result = []
    for component in relative_path(path).split("/"):
        try:
            component.encode("utf-8")
            native = _decode_component(component, controls=decode)
            converted = _encode_component(native, controls=not decode)
            restored = _encode_component(_decode_component(converted, controls=not decode), controls=decode)
        except UnicodeError as exc:
            raise SyncError("ファイル名の文字コード対応を確認できません") from exc
        if restored != component:
            raise SyncError("ファイル名の往復対応を確認できません")
        result.append(converted)
    return relative_path("/".join(result))


def relative_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\0" in value:
        raise SyncError("invalid relative path")
    p = PurePosixPath(value)
    if p.is_absolute() or ".." in p.parts or str(p) != value or value == ".":
        raise SyncError("invalid relative path")
    return value


def safe_local(root: Path, value: str) -> Path:
    value = relative_path(value)
    target = root.resolve()
    for part in PurePosixPath(value).parts:
        target = target / part
        if target.is_symlink():
            raise SyncError("symlink requires review")
    return target


def utc_second(value: object) -> int | None:
    try:
        if isinstance(value, (float, int)) and not isinstance(value, bool):
            return int(value // 1)
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return None
            return int(parsed.astimezone(timezone.utc).timestamp() // 1)
    except (ValueError, OverflowError, OSError):
        pass
    return None


def stat_version(path: Path) -> dict:
    try:
        s = path.lstat()
    except FileNotFoundError:
        return {"exists": False}
    if not stat.S_ISREG(s.st_mode):
        raise SyncError("only regular files may be synchronized automatically")
    return {"exists": True, "size": s.st_size, "mtime_ns": s.st_mtime_ns,
            "ctime_ns": s.st_ctime_ns, "inode": s.st_ino, "device": s.st_dev}


def local_version(path: Path, algorithms=("sha1", "md5")) -> dict:
    before = stat_version(path)
    if not before["exists"]:
        return before
    hashes = {a: hashlib.new(a) for a in algorithms if a in {"md5", "sha1", "sha256"}}
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if opened.st_ino != before["inode"] or opened.st_dev != before["device"]:
            raise SyncError("local source changed while opening")
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            for h in hashes.values():
                h.update(chunk)
    if stat_version(path) != before:
        raise SyncError("local source changed while hashing")
    return {**before, "second": before["mtime_ns"] // 1_000_000_000,
            "hashes": {a: h.hexdigest() for a, h in hashes.items()}}


def same_content(left: dict, right: dict) -> bool | None:
    if not left.get("exists") or not right.get("exists"):
        return None
    if left.get("size") is None or right.get("size") is None:
        return None
    if left["size"] != right["size"]:
        return False
    common = set(left.get("hashes", {})) & set(right.get("hashes", {}))
    if not common:
        return None
    return all(left["hashes"][a] == right["hashes"][a] for a in common)


def remote_version(item: dict) -> dict:
    if item.get("IsDir"):
        raise SyncError("remote directory requires enumeration")
    hashes = {str(a).lower().replace("-", ""): str(v).lower()
              for a, v in (item.get("Hashes") or {}).items() if v}
    return {"exists": True, "size": item.get("Size"), "second": utc_second(item.get("ModTime")),
            "modified": item.get("ModTime"), "id": str(item.get("ID", "")), "hashes": hashes}


class RcloneRemote:
    def __init__(self, config, binary=None):
        self.config = config
        self.binary = binary or config.rclone_bin
        self.root = config.core_remote.rstrip("/")
        self.on_started = None
        self.on_finished = None
        self.calls: list[dict] = []
        self.discovery_issues = []

    def target(self, path):
        return self.root + "/" + rclone_path(path)

    def run(self, argv):
        trace = getattr(self, 'trace', None)
        if trace is None:
            return self._run(argv)
        # Only the fixed verb is logged, never paths, arguments or stdout.
        verb = argv[0] if argv and argv[0] in {'lsjson', 'copy', 'moveto', 'move', 'deletefile'} else 'other'
        with trace.span('rclone-' + verb, detail=True):
            return self._run(argv)

    def _run(self, argv):
        encoding = [] if "--local-encoding" in argv else ["--local-encoding", LOCAL_ENCODING]
        batch = run_transfer_batch([{"command": [self.binary, *argv, *encoding], "path": "event-sync"}],
                                   timeout_seconds=self.config.transfer_exec_timeout_seconds,
                                   on_process_started=self.on_started,
                                   on_process_finished=self.on_finished)
        result = batch.results[0]
        self.calls.append(result)
        if result.get("requires_child_exit_confirmation"):
            raise SyncError("rclone child exit is uncertain; recovery required")
        if result.get("timed_out") or result.get("deferred"):
            raise SyncError("rclone interrupted; recovery required")
        return result

    def _manifest(self, paths, directory):
        file = Path(directory) / "files"
        file.write_bytes(b"".join(rclone_path(p).encode("utf-8") + b"\0" for p in paths))
        return str(file)

    def inventory(self, paths=None, filter_rules=None, *, hashes=True):
        return self._inventory(self.root, paths, filter_rules, hashes=hashes)

    def local_paths(self, filter_rules):
        # Let rclone prune excluded trees instead of walking every local entry in
        # Python. The caller reapplies scope checks before using any path.
        return set(self._inventory(str(self.config.core_dir), None, filter_rules, hashes=False))

    def _inventory(self, root, paths, filter_rules, *, hashes):
        with tempfile.TemporaryDirectory(prefix="pcloud-list-") as temporary:
            args = ["lsjson", root, "--recursive", "--files-only"]
            native_local = root == str(getattr(self.config, "core_dir", None)) and root != self.root and paths is None
            if native_local:
                # Discovery must retain even noncanonical local names. Read native
                # names through Raw, then validate the transfer representation per
                # selected path. Never silently invent the normalized target name.
                args += ["--local-encoding", "Raw"]
            if hashes:
                args += ["--hash"]
            elif paths is None:
                args += ["--fast-list"]
            if paths is not None:
                if not paths:
                    return {}
                args += ["--files-from0", self._manifest(paths, temporary)]
            elif filter_rules is not None:
                file = Path(temporary) / "filter"
                file.write_text("\n".join(filter_rules) + "\n")
                args += ["--filter-from", str(file)]
            result = self.run(args)
        if result.get("returncode") != 0:
            raise SyncError("remote inventory failed; absence cannot be inferred")
        try:
            items = json.loads(result["stdout"])
            if not isinstance(items, list):
                raise ValueError("not a list")
            versions = {}
            issues = []
            for item in items:
                raw_path = item["Path"]
                if not isinstance(raw_path, str):
                    raise ValueError("invalid inventory path type")
                relative_path(raw_path)
                try:
                    if native_local:
                        parts = [_decode_component(c, True) for c in raw_path.split("/")]
                        if any("/" in c for c in parts):
                            raise SyncError("ファイル名の対応を取得できません")
                        path = relative_path("/".join(parts))
                    else:
                        path = rclone_path(raw_path, decode=True)
                except SyncError:
                    if root != self.root or paths is not None or hashes:
                        raise
                    # Preserve unknown mappings as unavailable; do not infer
                    # absence or ask the user to rename an existing file.
                    issues.append({"path": raw_path, "cloud": remote_version(item),
                                   "reason": "ファイル名の対応を取得できません。再照合が必要です"})
                    continue
                if path in versions:
                    raise ValueError("ambiguous duplicate inventory path")
                versions[path] = remote_version(item)
            if root == self.root and paths is None and not hashes:
                self.discovery_issues = issues
            return versions
        except (ValueError, KeyError, TypeError) as exc:
            raise SyncError("invalid remote inventory") from exc

    def copy(self, direction, paths, staging: Path):
        with tempfile.TemporaryDirectory(prefix="pcloud-copy-") as temporary:
            source, destination = ((str(self.config.core_dir), self.root) if direction == "upload"
                                   else (self.root, str(staging)))
            # Selection has already passed the manager's complete scope rules.
            # files-from0 intentionally takes precedence over rclone filters.
            result = self.run(["copy", source, destination, "--files-from0", self._manifest(paths, temporary),
                               "--no-traverse", "--ignore-times", "--transfers",
                               str(max(self.config.pushd_transfer_concurrency, self.config.diffd_transfer_concurrency)),
                               "--use-json-log", "--stats", "1s"])
        return result

    def move(self, old, new):
        return self.run(["moveto", self.target(old), self.target(new), "--ignore-times"])

    def move_tree(self, old, new, paths):
        with tempfile.TemporaryDirectory(prefix="pcloud-move-") as temporary:
            return self.run(["moveto", self.target(old), self.target(new),
                             "--files-from0", self._manifest(paths, temporary), "--ignore-times"])

    def delete(self, path):
        # pCloud's ordinary delete keeps its native trash/version history.
        return self.run(["deletefile", self.target(path)])
