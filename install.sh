#!/bin/sh

set -eu

REPOSITORY="takafuu/pcloud-tools"
UV_BOOTSTRAP_VERSION="0.10.7"
PYTHON_VERSION="3.11"
RELEASE_VERSION="latest"
RUNTIME_DIR="${PCLOUD_TOOLS_RUNTIME_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/pcloud-tools}"
BIN_DIR="${PCLOUD_TOOLS_BIN_DIR:-$HOME/bin}"
LOCAL_WHEEL=""
DRY_RUN=0
BOOTSTRAP_UV=1

usage() {
    cat <<'EOF'
Install pcloud-tools into an isolated uv tool environment.

Usage:
  install.sh [options]

Options:
  --version VERSION      Install a release tag such as 0.1.0 or v0.1.0.
  --runtime-dir DIR      Store the uv tool environment under DIR.
  --bin-dir DIR          Install thin public wrappers under DIR.
  --python VERSION       Python version for the uv tool environment (default: 3.11).
  --wheel PATH           Install a local wheel instead of downloading a release.
  --no-bootstrap-uv      Fail instead of installing uv when it is unavailable.
  --dry-run              Show resolved actions without changing files.
  -h, --help             Show this help.

Environment overrides:
  PCLOUD_TOOLS_RUNTIME_DIR
  PCLOUD_TOOLS_BIN_DIR
  PCLOUD_TOOLS_UV_VERSION

The installer does not create or modify pcloud-tools config, runtime state,
rclone.conf, pCloud credentials, remotes, launchd jobs, or NAS services.
EOF
}

die() {
    printf 'pcloud-tools installer: ERROR: %s\n' "$*" >&2
    exit 1
}

note() {
    printf 'pcloud-tools installer: %s\n' "$*"
}

run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '+ '
        printf '%s ' "$@"
        printf '\n'
        return 0
    fi
    "$@"
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

verify_checksum() {
    checksum_file=$1
    checksum_dir=$2
    if command -v sha256sum >/dev/null 2>&1; then
        (cd "$checksum_dir" && sha256sum -c "$(basename "$checksum_file")")
        return
    fi
    if command -v shasum >/dev/null 2>&1; then
        (cd "$checksum_dir" && shasum -a 256 -c "$(basename "$checksum_file")")
        return
    fi
    die "sha256sum or shasum is required to verify the release bundle"
}

write_wrapper() {
    command_name=$1
    target=$2
    temp_target="${target}.tmp.$$"
    if [ -d "$target" ]; then
        die "wrapper target is a directory: $target"
    fi
    if [ "$DRY_RUN" -eq 1 ]; then
        note "would install wrapper: $target -> $RUNTIME_DIR/bin/$command_name"
        return
    fi
    {
        printf '%s\n' '#!/bin/sh'
        printf '%s\n' 'set -eu'
        # shellcheck disable=SC2016
        printf 'runtime_dir=${PCLOUD_TOOLS_RUNTIME_DIR:-%s}\n' "$(printf '%s' "$RUNTIME_DIR" | sed "s/'/'\\\\''/g; s/^/'/; s/$/'/")"
        printf 'export PCLOUD_TOOLS_PUBLIC_ENTRYPOINT=%s\n' "$(printf '%s' "$BIN_DIR/pcloud-manager" | sed "s/'/'\\\\''/g; s/^/'/; s/$/'/")"
        # shellcheck disable=SC2016
        printf 'exec "$runtime_dir/bin/%s" "$@"\n' "$command_name"
    } > "$temp_target"
    chmod 755 "$temp_target"
    mv -f "$temp_target" "$target"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --version)
            [ "$#" -ge 2 ] || die "--version requires a value"
            RELEASE_VERSION=$2
            shift 2
            ;;
        --runtime-dir)
            [ "$#" -ge 2 ] || die "--runtime-dir requires a value"
            RUNTIME_DIR=$2
            shift 2
            ;;
        --bin-dir)
            [ "$#" -ge 2 ] || die "--bin-dir requires a value"
            BIN_DIR=$2
            shift 2
            ;;
        --python)
            [ "$#" -ge 2 ] || die "--python requires a value"
            PYTHON_VERSION=$2
            shift 2
            ;;
        --wheel)
            [ "$#" -ge 2 ] || die "--wheel requires a value"
            LOCAL_WHEEL=$2
            shift 2
            ;;
        --no-bootstrap-uv)
            BOOTSTRAP_UV=0
            shift
            ;;
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "unknown option: $1"
            ;;
    esac
done

case "$RUNTIME_DIR" in
    ""|"/") die "unsafe runtime directory: ${RUNTIME_DIR:-<empty>}" ;;
esac
case "$BIN_DIR" in
    ""|"/") die "unsafe wrapper directory: ${BIN_DIR:-<empty>}" ;;
esac

UV_VERSION="${PCLOUD_TOOLS_UV_VERSION:-$UV_BOOTSTRAP_VERSION}"
UV_BIN=""
if command -v uv >/dev/null 2>&1; then
    UV_BIN=$(command -v uv)
elif [ "$BOOTSTRAP_UV" -eq 0 ]; then
    die "uv was not found; install uv or rerun without --no-bootstrap-uv"
else
    require_command curl
    UV_INSTALL_DIR="$RUNTIME_DIR/uv"
    UV_INSTALL_SCRIPT="$RUNTIME_DIR/uv-install.sh"
    UV_INSTALL_URL="https://astral.sh/uv/$UV_VERSION/install.sh"
    run mkdir -p "$RUNTIME_DIR"
    run curl -LsSf "$UV_INSTALL_URL" -o "$UV_INSTALL_SCRIPT"
    if [ "$DRY_RUN" -eq 0 ]; then
        env UV_UNMANAGED_INSTALL="$UV_INSTALL_DIR" UV_NO_MODIFY_PATH=1 sh "$UV_INSTALL_SCRIPT"
    else
        note "would bootstrap uv $UV_VERSION into $UV_INSTALL_DIR"
    fi
    UV_BIN="$UV_INSTALL_DIR/uv"
fi

TEMP_DIR=""
cleanup() {
    if [ -n "$TEMP_DIR" ] && [ -d "$TEMP_DIR" ]; then
        rm -rf "$TEMP_DIR"
    fi
}
trap cleanup EXIT HUP INT TERM

WHEEL_PATH=$LOCAL_WHEEL
if [ -z "$WHEEL_PATH" ]; then
    require_command curl
    require_command tar
    TEMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/pcloud-tools-install.XXXXXX")
    if [ "$RELEASE_VERSION" = "latest" ]; then
        RELEASE_BASE="https://github.com/$REPOSITORY/releases/latest/download"
    else
        case "$RELEASE_VERSION" in
            v*) RELEASE_TAG=$RELEASE_VERSION ;;
            *) RELEASE_TAG="v$RELEASE_VERSION" ;;
        esac
        RELEASE_BASE="https://github.com/$REPOSITORY/releases/download/$RELEASE_TAG"
    fi
    BUNDLE_NAME="pcloud-tools-install.tar.gz"
    CHECKSUM_NAME="$BUNDLE_NAME.sha256"
    run curl -LfsS "$RELEASE_BASE/$BUNDLE_NAME" -o "$TEMP_DIR/$BUNDLE_NAME"
    run curl -LfsS "$RELEASE_BASE/$CHECKSUM_NAME" -o "$TEMP_DIR/$CHECKSUM_NAME"
    if [ "$DRY_RUN" -eq 1 ]; then
        note "would verify and install the release bundle from $RELEASE_BASE"
        WHEEL_PATH="$TEMP_DIR/pcloud_tools-RELEASE-py3-none-any.whl"
    else
        verify_checksum "$TEMP_DIR/$CHECKSUM_NAME" "$TEMP_DIR"
        tar -tzf "$TEMP_DIR/$BUNDLE_NAME" | while IFS= read -r entry; do
            case "$entry" in
                ./|./VERSION|./SHA256SUMS|./pcloud_tools-*.whl) ;;
                *) die "release bundle contains an unexpected path: $entry" ;;
            esac
        done
        mkdir -p "$TEMP_DIR/unpacked"
        tar -xzf "$TEMP_DIR/$BUNDLE_NAME" -C "$TEMP_DIR/unpacked"
        verify_checksum "$TEMP_DIR/unpacked/SHA256SUMS" "$TEMP_DIR/unpacked"
        set -- "$TEMP_DIR"/unpacked/*.whl
        [ "$#" -eq 1 ] && [ -f "$1" ] || die "release bundle must contain exactly one wheel"
        WHEEL_PATH=$1
    fi
else
    case "$WHEEL_PATH" in
        /*) ;;
        *) WHEEL_PATH="$PWD/$WHEEL_PATH" ;;
    esac
    [ "$DRY_RUN" -eq 1 ] || [ -f "$WHEEL_PATH" ] || die "wheel not found: $WHEEL_PATH"
    case "$WHEEL_PATH" in
        *.whl) ;;
        *) die "--wheel must point to a .whl file" ;;
    esac
fi

note "runtime directory: $RUNTIME_DIR"
note "wrapper directory: $BIN_DIR"
note "uv: $UV_BIN"
note "wheel: $WHEEL_PATH"

run mkdir -p "$RUNTIME_DIR/tools" "$RUNTIME_DIR/bin" "$BIN_DIR"
if [ "$DRY_RUN" -eq 0 ]; then
    UV_TOOL_DIR="$RUNTIME_DIR/tools" \
    UV_TOOL_BIN_DIR="$RUNTIME_DIR/bin" \
        "$UV_BIN" tool install --force --python "$PYTHON_VERSION" --no-config "$WHEEL_PATH"
else
    note "would run uv tool install in the isolated runtime"
fi

for command_name in pcloud-manager pcloud-tools pcloud-archive pcloud-pushd pcloud-diffd; do
    if [ "$DRY_RUN" -eq 0 ] && [ ! -x "$RUNTIME_DIR/bin/$command_name" ]; then
        die "installed package did not provide expected command: $command_name"
    fi
    write_wrapper "$command_name" "$BIN_DIR/$command_name"
done

if [ "$DRY_RUN" -eq 0 ]; then
    "$RUNTIME_DIR/bin/pcloud-manager" --version
    "$RUNTIME_DIR/bin/pcloud-archive" --version
fi

note "installation complete"
note "next: $BIN_DIR/pcloud-manager info"
note "next: $BIN_DIR/pcloud-manager doctor"
note "NAS archive setup: $BIN_DIR/pcloud-archive help config"
