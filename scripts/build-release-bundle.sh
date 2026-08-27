#!/bin/sh

set -eu

ROOT_DIR=$(CDPATH='' cd -- "$(dirname "$0")/.." && pwd)
OUTPUT_DIR="${1:-$ROOT_DIR/dist}"

if ! command -v uv >/dev/null 2>&1; then
    printf '%s\n' 'build-release-bundle: uv was not found' >&2
    exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
    printf '%s\n' 'build-release-bundle: python3 was not found' >&2
    exit 1
fi

TEMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/pcloud-tools-release.XXXXXX")
cleanup() {
    rm -rf "$TEMP_DIR"
}
trap cleanup EXIT HUP INT TERM

BUILD_DIR="$TEMP_DIR/build"
BUNDLE_DIR="$TEMP_DIR/bundle"
mkdir -p "$BUILD_DIR" "$BUNDLE_DIR" "$OUTPUT_DIR"

uv build --wheel --out-dir "$BUILD_DIR" "$ROOT_DIR"
set -- "$BUILD_DIR"/*.whl
[ "$#" -eq 1 ] && [ -f "$1" ] || {
    printf '%s\n' 'build-release-bundle: expected exactly one wheel' >&2
    exit 1
}
WHEEL_PATH=$1
python3 - "$WHEEL_PATH" <<'PY'
from __future__ import annotations

import configparser
import io
import sys
import zipfile
from pathlib import Path

wheel = Path(sys.argv[1])
with zipfile.ZipFile(wheel) as archive:
    names = set(archive.namelist())
    required_docs = {
        f"pcloud_tools/share/docs/{command}/{name}"
        for command in ("pcloud-manager", "pcloud-archive")
        for name in ("利用ガイド.md", "技術仕様.md", "AI向け概要.md")
    }
    required_manpages = {
        "pcloud_tools/share/man/man1/pcloud-manager.1",
        "pcloud_tools/share/man/man1/pcloud-archive.1",
    }
    missing = sorted((required_docs | required_manpages) - names)
    if missing:
        raise SystemExit(f"wheel is missing packaged documentation: {missing}")

    entry_points_name = next(name for name in names if name.endswith(".dist-info/entry_points.txt"))
    parser = configparser.ConfigParser()
    parser.read_file(io.StringIO(archive.read(entry_points_name).decode()))
    expected = {
        "pcloud-manager",
        "pcloud-tools",
        "pcloud-archive",
        "pcloud-pushd",
        "pcloud-diffd",
    }
    actual = set(parser["console_scripts"])
    if not expected.issubset(actual):
        raise SystemExit(f"wheel is missing console scripts: {sorted(expected - actual)}")
PY
cp "$WHEEL_PATH" "$BUNDLE_DIR/"

PACKAGE_VERSION=$(PYTHONPATH="$ROOT_DIR/src" python3 -c 'from pcloud_tools import __version__; print(__version__)')
printf '%s\n' "$PACKAGE_VERSION" > "$BUNDLE_DIR/VERSION"

if command -v sha256sum >/dev/null 2>&1; then
    (cd "$BUNDLE_DIR" && sha256sum "$(basename "$WHEEL_PATH")" > SHA256SUMS)
elif command -v shasum >/dev/null 2>&1; then
    (cd "$BUNDLE_DIR" && shasum -a 256 "$(basename "$WHEEL_PATH")" > SHA256SUMS)
else
    printf '%s\n' 'build-release-bundle: sha256sum or shasum is required' >&2
    exit 1
fi

BUNDLE_PATH="$OUTPUT_DIR/pcloud-tools-install.tar.gz"
CHECKSUM_PATH="$BUNDLE_PATH.sha256"
tar -czf "$BUNDLE_PATH" -C "$BUNDLE_DIR" .

if command -v sha256sum >/dev/null 2>&1; then
    (cd "$OUTPUT_DIR" && sha256sum "$(basename "$BUNDLE_PATH")" > "$(basename "$CHECKSUM_PATH")")
else
    (cd "$OUTPUT_DIR" && shasum -a 256 "$(basename "$BUNDLE_PATH")" > "$(basename "$CHECKSUM_PATH")")
fi

cp "$WHEEL_PATH" "$OUTPUT_DIR/"
printf 'release bundle: %s\n' "$BUNDLE_PATH"
printf 'release checksum: %s\n' "$CHECKSUM_PATH"
printf 'wheel: %s/%s\n' "$OUTPUT_DIR" "$(basename "$WHEEL_PATH")"
