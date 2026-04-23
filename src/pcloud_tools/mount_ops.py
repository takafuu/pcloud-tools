from __future__ import annotations

import shlex
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .config import AppConfig

VALID_MOUNT_ENGINES = {"webdav", "nfs"}


class MountCommandError(ValueError):
    """Raised when mount command options are invalid."""


class MountExecutionError(RuntimeError):
    """Raised when a mount operation cannot be completed."""


@dataclass(frozen=True)
class LayerSpec:
    name: str
    enabled: bool
    remote: str
    mount_dir: Path
    link_dir: Path
    engine: str
    port: int


@dataclass(frozen=True)
class MountLayerState:
    name: str
    enabled: bool
    state: str
    engine: str
    port: int
    pid: str
    target: Path
    remote: str
    mounted: bool
    link_state: str
    link_target: str


def layer_specs(config: AppConfig) -> dict[str, LayerSpec]:
    return {
        "vault": LayerSpec(
            name="vault",
            enabled=config.enable_vault_layer,
            remote=config.vault_remote,
            mount_dir=config.vault_mount_dir,
            link_dir=config.vault_dir,
            engine=config.vault_engine,
            port=config.vault_port,
        ),
        "crypt": LayerSpec(
            name="crypt",
            enabled=config.enable_crypt_layer,
            remote=config.crypt_remote,
            mount_dir=config.crypt_mount_dir,
            link_dir=config.crypt_dir,
            engine=config.crypt_engine,
            port=config.crypt_port,
        ),
    }


def resolve_layers(config: AppConfig, target: str) -> list[LayerSpec]:
    specs = layer_specs(config)
    if target == "all":
        return [specs["vault"], specs["crypt"]]
    if target in specs:
        return [specs[target]]
    raise MountCommandError(f"invalid target: {target}")


def mount_layer_state(spec: LayerSpec) -> MountLayerState:
    mount_line = _mount_line_for_target(spec.mount_dir)
    mounted = mount_line is not None
    engine = spec.engine
    port = spec.port
    pid = "-"
    state = "mounted" if mounted else "not_mounted"

    if mount_line is not None:
        parsed_engine, parsed_port = _parse_mount_line(mount_line)
        if parsed_engine:
            engine = parsed_engine
        if parsed_port is not None:
            port = parsed_port

    serve_info = _find_serve_process(spec.remote)
    if serve_info is not None:
        pid = str(serve_info.pid)
        engine = serve_info.engine
        port = serve_info.port

    if not spec.enabled:
        state = "disabled"
    elif not mounted and serve_info is not None:
        state = "error"

    link_state, link_target = _link_state(spec.link_dir)
    return MountLayerState(
        name=spec.name,
        enabled=spec.enabled,
        state=state,
        engine=engine,
        port=port,
        pid=pid,
        target=spec.mount_dir,
        remote=spec.remote,
        mounted=mounted,
        link_state=link_state,
        link_target=link_target,
    )


def preview_mount_operations(spec: LayerSpec, execute: bool) -> list[str]:
    action = "run" if execute else "would run"
    mount_command = _render_mount_command(spec)
    return [
        f"{action} `rclone serve {spec.engine} {spec.remote} --addr 127.0.0.1:{spec.port}`",
        f"{action} `{mount_command}` onto {spec.mount_dir}",
        f"{action} symlink {spec.link_dir} -> {spec.mount_dir}",
    ]


def preview_umount_operations(spec: LayerSpec, execute: bool) -> list[str]:
    action = "run" if execute else "would run"
    return [
        f"{action} unmount for {spec.mount_dir} if mounted",
        f"{action} stop rclone serve process on port {spec.port}",
        f"{action} remove symlink {spec.link_dir} if it points to the mount",
    ]


def execute_mount(spec: LayerSpec, rclone_bin: str) -> None:
    if not spec.enabled:
        raise MountExecutionError(f"{spec.name} layer is disabled in config")
    if spec.engine not in VALID_MOUNT_ENGINES:
        raise MountExecutionError(f"invalid engine for {spec.name}: {spec.engine}")

    spec.mount_dir.mkdir(parents=True, exist_ok=True)
    _safe_unmount_path(spec.mount_dir)
    _stop_serve_by_port(spec.port)

    log_path = Path(f"/tmp/pcloud-{spec.name}-{spec.port}.log")
    with log_path.open("ab") as log_file:
        process = subprocess.Popen(
            [
                rclone_bin,
                "serve",
                spec.engine,
                spec.remote,
                "--addr",
                f"127.0.0.1:{spec.port}",
                "--vfs-cache-mode",
                "full",
                "--vfs-fast-fingerprint",
            ],
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    if not _wait_for_port(spec.port):
        process.terminate()
        raise MountExecutionError(f"{spec.name} serve did not listen on port {spec.port}")

    mount_command = _mount_command(spec)
    result = subprocess.run(mount_command, capture_output=True, text=True)
    if result.returncode != 0:
        process.terminate()
        stderr = (result.stderr or result.stdout).strip()
        raise MountExecutionError(
            f"failed to mount {spec.name} on {spec.mount_dir}: {stderr or 'unknown error'}"
        )

    _ensure_mount_link(spec.link_dir, spec.mount_dir)


def execute_umount(spec: LayerSpec) -> None:
    if not spec.enabled:
        raise MountExecutionError(f"{spec.name} layer is disabled in config")
    _safe_unmount_path(spec.mount_dir)
    _stop_serve_by_port(spec.port)
    _remove_mount_link(spec.link_dir)


def _render_mount_command(spec: LayerSpec) -> str:
    return " ".join(shlex.quote(part) for part in _mount_command(spec))


def _mount_command(spec: LayerSpec) -> list[str]:
    if spec.engine == "webdav":
        command = shutil.which("mount_webdav")
        if command is None:
            raise MountExecutionError("mount_webdav command not found")
        return [command, f"http://127.0.0.1:{spec.port}", str(spec.mount_dir)]
    if spec.engine == "nfs":
        command = shutil.which("mount_nfs")
        if command is None:
            raise MountExecutionError("mount_nfs command not found")
        return [
            command,
            "-o",
            f"port={spec.port},mountport={spec.port},tcp",
            "127.0.0.1:/",
            str(spec.mount_dir),
        ]
    raise MountExecutionError(f"unsupported engine: {spec.engine}")


@dataclass(frozen=True)
class _ServeInfo:
    pid: int
    engine: str
    port: int


def _find_serve_process(remote: str) -> _ServeInfo | None:
    result = subprocess.run(
        ["ps", "-ax", "-o", "pid=", "-o", "command="],
        capture_output=True,
        text=True,
        check=False,
    )
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        pid = int(parts[0])
        try:
            argv = shlex.split(parts[1])
        except ValueError:
            continue
        if len(argv) < 6:
            continue
        executable = Path(argv[0]).name
        if executable != "rclone" or argv[1:3] != ["serve", "webdav"] and argv[1:3] != ["serve", "nfs"]:
            continue
        engine = argv[2]
        if argv[3] != remote:
            continue
        port = _extract_addr_port(argv)
        if port is None:
            continue
        return _ServeInfo(pid=pid, engine=engine, port=port)
    return None


def _extract_addr_port(argv: list[str]) -> int | None:
    for index, token in enumerate(argv):
        if token == "--addr" and index + 1 < len(argv):
            return _parse_addr_port(argv[index + 1])
        if token.startswith("--addr="):
            return _parse_addr_port(token.split("=", 1)[1])
    return None


def _parse_addr_port(value: str) -> int | None:
    if not value.startswith("127.0.0.1:"):
        return None
    port_text = value.rsplit(":", 1)[-1]
    return int(port_text) if port_text.isdigit() else None


def _mount_line_for_target(target: Path) -> str | None:
    result = subprocess.run(["mount"], capture_output=True, text=True, check=False)
    needle = f" on {target} "
    for line in result.stdout.splitlines():
        if needle in line:
            return line
    return None


def _parse_mount_line(line: str) -> tuple[str | None, int | None]:
    engine = None
    port = None
    if "(nfs" in line:
        engine = "nfs"
    elif "(webdav" in line:
        engine = "webdav"
    marker = "127.0.0.1:"
    if marker in line:
        port_text = line.split(marker, 1)[1].split("/", 1)[0].split(" ", 1)[0]
        if port_text.isdigit():
            port = int(port_text)
    return engine, port


def _link_state(link_path: Path) -> tuple[str, str]:
    if link_path.is_symlink():
        target = str(link_path.readlink())
        return "symlink", target
    if link_path.exists():
        return "path", str(link_path)
    return "missing", "-"


def _ensure_mount_link(link_path: Path, mount_dir: Path) -> None:
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    mount_dir.mkdir(parents=True, exist_ok=True)

    if link_path.is_symlink():
        current = link_path.readlink()
        if current == mount_dir:
            return
        link_path.unlink()
        link_path.symlink_to(mount_dir)
        return

    if link_path.exists():
        backup = link_path.with_name(f"{link_path.name}.prelink-{timestamp}")
        link_path.rename(backup)

    link_path.symlink_to(mount_dir)


def _remove_mount_link(link_path: Path) -> None:
    if link_path.is_symlink():
        link_path.unlink()
        return
    if link_path.is_dir():
        entries = [path for path in link_path.iterdir() if path.name != ".DS_Store"]
        if not entries:
            link_path.rmdir()


def _safe_unmount_path(target: Path) -> None:
    if _mount_line_for_target(target) is None:
        return
    diskutil = shutil.which("diskutil")
    if diskutil is not None:
        result = subprocess.run(
            [diskutil, "unmount", "force", str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return
    umount = shutil.which("umount")
    if umount is not None:
        subprocess.run([umount, "-f", str(target)], capture_output=True, text=True, check=False)


def _stop_serve_by_port(port: int) -> None:
    pkill = shutil.which("pkill")
    if pkill is None:
        return
    for engine in ("webdav", "nfs"):
        subprocess.run(
            [pkill, "-f", f"rclone serve {engine} .*127\\.0\\.0\\.1:{port}"],
            capture_output=True,
            text=True,
            check=False,
        )


def _wait_for_port(port: int, timeout_seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.1)
    return False
