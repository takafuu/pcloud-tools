from __future__ import annotations

import os
from pathlib import Path

from .runtime import RuntimePaths


DOCUMENTATION_FILENAMES = ("利用ガイド.md", "技術仕様.md", "AI向け概要.md")


def package_share_dir() -> Path:
    return Path(__file__).resolve().parent / "share"


def command_documentation_candidates(command: str, paths: RuntimePaths) -> tuple[Path, ...]:
    candidates: list[Path] = []
    seen: set[Path] = set()

    env_key = f"PCLOUD_TOOLS_{command.upper().replace('-', '_')}_DOCS_DIR"
    explicit = os.environ.get(env_key)
    if explicit:
        _append_unique(candidates, seen, Path(explicit).expanduser())

    packaged_docs = package_share_dir() / "docs" / command
    if not paths.dev_mode:
        _append_unique(candidates, seen, packaged_docs)

    for anchor in (paths.workspace_root, Path(__file__).resolve()):
        start = anchor if anchor.is_dir() else anchor.parent
        for parent in (start, *start.parents):
            for relative in (
                Path("dev") / "#仕様書" / command,
                Path("#仕様書") / command,
                Path("docs") / "commands" / command,
                Path("docs") / "spec" if command == "pcloud-manager" else None,
            ):
                if relative is not None:
                    _append_unique(candidates, seen, parent / relative)

    _append_unique(candidates, seen, packaged_docs)
    return tuple(candidates)


def command_documentation_dir(command: str, paths: RuntimePaths) -> Path | None:
    for candidate in command_documentation_candidates(command, paths):
        if candidate.is_dir():
            return candidate
    return None


def command_documentation_files(command: str, paths: RuntimePaths) -> tuple[Path, ...]:
    directory = command_documentation_dir(command, paths)
    if directory is None:
        return ()
    return tuple(directory / name for name in DOCUMENTATION_FILENAMES)


def packaged_manpage(command: str) -> Path | None:
    candidate = package_share_dir() / "man" / "man1" / f"{command}.1"
    return candidate if candidate.is_file() else None


def _append_unique(candidates: list[Path], seen: set[Path], candidate: Path) -> None:
    if candidate in seen:
        return
    seen.add(candidate)
    candidates.append(candidate)
