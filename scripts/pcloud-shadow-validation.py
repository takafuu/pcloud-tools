#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import threading
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    detail: str


def _base_env(root: Path) -> dict[str, str]:
    workspace = root / "workspace"
    config_dir = workspace / ".dev-state" / "config"
    state_dir = workspace / ".dev-state" / "state"
    log_dir = workspace / ".dev-state" / "logs"
    home_dir = root / "home"
    cache_dir = root / "cache"
    for path in (workspace, config_dir, state_dir, log_dir, home_dir, cache_dir):
        path.mkdir(parents=True, exist_ok=True)
    (workspace / ".pcloud-sync-allowlist").write_text("Documents/\n")
    (config_dir / ".env").write_text("# shadow validation env\n")

    env = {key: value for key, value in os.environ.items() if not key.startswith("PCLOUD_TOOLS_")}
    env.update(
        {
            "PYTHONPATH": str(REPO_ROOT / "src"),
            "PCLOUD_TOOLS_DEV": "1",
            "PCLOUD_TOOLS_WORKSPACE_ROOT": str(workspace),
            "PCLOUD_TOOLS_CONFIG_DIR": str(config_dir),
            "PCLOUD_TOOLS_STATE_DIR": str(state_dir),
            "PCLOUD_TOOLS_LOG_DIR": str(log_dir),
            "HOME": str(home_dir),
            "XDG_CACHE_HOME": str(cache_dir),
        }
    )
    public_bin = home_dir / "bin"
    public_bin.mkdir(parents=True, exist_ok=True)
    public_wrapper = public_bin / "pcloud-manager"
    public_wrapper.write_text("#!/bin/sh\n# pcloud_tools.cli release wrapper fixture\nexit 0\n")
    public_wrapper.chmod(0o755)
    env["PCLOUD_TOOLS_PUBLIC_ENTRYPOINT"] = str(public_wrapper)
    env["PATH"] = f"{public_bin}:{env.get('PATH', '')}"
    return env


def _run_cli(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", *args],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def _payload(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return json.loads(result.stdout)


def _check_json_command(
    checks: list[CheckResult],
    env: dict[str, str],
    name: str,
    args: tuple[str, ...],
    allowed_status: set[str] | None = None,
) -> dict[str, Any]:
    allowed_status = allowed_status or {"ok", "warning"}
    result = _run_cli(env, *args, "--json")
    if result.returncode not in {0, 1}:
        checks.append(CheckResult(name, "error", f"return code {result.returncode}: {result.stderr.strip()}"))
        return {}
    try:
        payload = _payload(result)
    except json.JSONDecodeError as exc:
        checks.append(CheckResult(name, "error", f"invalid JSON output: {exc}"))
        return {}
    status = str(payload.get("status", ""))
    if status not in allowed_status:
        checks.append(CheckResult(name, "error", f"unexpected status {status!r}"))
        return payload
    checks.append(CheckResult(name, "ok", str(payload.get("summary", "-"))))
    return payload


def _check_action(checks: list[CheckResult], env: dict[str, str], action_id: str, expected: str) -> None:
    result = _run_cli(env, "action", action_id)
    if result.returncode != 0:
        checks.append(CheckResult(action_id, "error", f"return code {result.returncode}: {result.stderr.strip()}"))
        return
    if expected not in result.stdout:
        checks.append(CheckResult(action_id, "error", f"missing expected text {expected!r}"))
        return
    checks.append(CheckResult(action_id, "ok", expected))


def _check_dev_wrapper(
    checks: list[CheckResult],
    workspace: Path,
    wrapper_name: str,
    args: tuple[str, ...],
    expected_command: str,
    expected_state_dir: Path,
) -> None:
    source_link = workspace / "src"
    if not source_link.exists():
        source_link.symlink_to(REPO_ROOT / "src", target_is_directory=True)
    for name in ("pcloud-manager-dev", "pcloud-pushd", "pcloud-diffd"):
        target = workspace / name
        target.write_text((REPO_ROOT / name).read_text())
        target.chmod(0o755)
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PCLOUD_TOOLS_") and key != "PYTHONPATH"
    }
    env["HOME"] = str(workspace.parent / "wrapper-home")
    env["XDG_CACHE_HOME"] = str(workspace.parent / "wrapper-cache")
    Path(env["HOME"]).mkdir(parents=True, exist_ok=True)
    Path(env["XDG_CACHE_HOME"]).mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [str(workspace / wrapper_name), *args, "--json"],
        cwd=workspace,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        checks.append(CheckResult(wrapper_name, "error", f"return code {result.returncode}: {result.stderr.strip()}"))
        return
    try:
        payload = _payload(result)
    except json.JSONDecodeError as exc:
        checks.append(CheckResult(wrapper_name, "error", f"invalid JSON output: {exc}"))
        return
    if payload.get("command") == expected_command and payload.get("details", {}).get("state dir") == str(
        expected_state_dir
    ):
        checks.append(CheckResult(wrapper_name, "ok", f"{wrapper_name} delegates to {expected_command}"))
    else:
        checks.append(CheckResult(wrapper_name, "error", f"unexpected wrapper payload: {payload.get('summary', '-')}"))


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def run_validation() -> dict[str, Any]:
    checks: list[CheckResult] = []
    with tempfile.TemporaryDirectory(prefix="pcloud-shadow-validation-") as temp_name:
        root = Path(temp_name)
        env = _base_env(root)
        workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
        state_dir = Path(env["PCLOUD_TOOLS_STATE_DIR"])
        temp_root = Path(tempfile.gettempdir()).resolve()
        expected_state_dir = (workspace / ".dev-state" / "state").resolve()
        actual_workspace = workspace.resolve()
        actual_state_dir = state_dir.resolve()

        if (
            _is_relative_to(actual_workspace, temp_root)
            and actual_workspace.name == "workspace"
            and actual_workspace.parent.name.startswith("pcloud-shadow-validation-")
        ):
            checks.append(CheckResult("temporary workspace guard", "ok", str(actual_workspace)))
        else:
            checks.append(
                CheckResult(
                    "temporary workspace guard",
                    "error",
                    f"workspace is not under temp root {temp_root}: {actual_workspace}",
                )
            )
        if actual_state_dir == expected_state_dir and _is_relative_to(actual_state_dir, temp_root):
            checks.append(CheckResult("temporary state dir guard", "ok", str(actual_state_dir)))
        else:
            checks.append(
                CheckResult(
                    "temporary state dir guard",
                    "error",
                    f"state dir {actual_state_dir} does not match {expected_state_dir}",
                )
            )
        saved_shadow_report = workspace / "saved-shadow-validation.json"
        saved_shadow_report.write_text(
            json.dumps(
                {
                    "status": "ok",
                    "workspace": str(workspace),
                    "state_dir": str(state_dir),
                    "checks": [
                        {"name": "temporary workspace guard", "status": "ok"},
                        {"name": "temporary state dir guard", "status": "ok"},
                        {"name": "unsafe state dir guard", "status": "ok"},
                    ],
                },
                sort_keys=True,
            )
        )
        shadow_upload = workspace / "Documents" / "shadow-upload.pdf"
        shadow_upload.parent.mkdir(parents=True, exist_ok=True)
        shadow_upload.write_text("shadow upload\n")
        autosync_plist = workspace / ".dev-state" / "com.example.pcloud-bisync.dev.plist"
        dev_entrypoint = workspace / "pcloud-manager-dev"
        dev_entrypoint.write_text("#!/bin/sh\nexit 0\n")
        dev_entrypoint.chmod(0o755)
        help_ai = _run_cli(
            env,
            "help",
            "--ai",
            "inspect pushd launchd status safely",
            "--topic",
            "pushd",
            "--topic",
            "launchd",
        )
        try:
            help_ai_payload = _payload(help_ai)
        except json.JSONDecodeError:
            help_ai_payload = {}
        if (
            help_ai.returncode == 0
            and help_ai_payload.get("schema_version") == "pcloud-tools-help-ai.v1"
            and help_ai_payload.get("command_name") == "pcloud-manager-dev"
            and help_ai_payload.get("runtime_mode") == "dev"
            and "pushd" in help_ai_payload.get("generated_help", {}).get("subcommands", {})
            and not any(state_dir.iterdir())
        ):
            checks.append(CheckResult("help ai context read-only", "ok", "help --ai emits JSON context"))
        else:
            checks.append(CheckResult("help ai context read-only", "error", "help --ai context mismatch or state mutation"))
        autosync_plist_preview = _check_json_command(
            checks,
            env,
            "sync autosync plist preview",
            ("sync", "autosync-plist"),
        )
        if (
            autosync_plist_preview.get("details", {}).get("state writes") == "none"
            and autosync_plist_preview.get("details", {}).get("launchctl execution") == "no"
            and not autosync_plist.exists()
        ):
            checks.append(CheckResult("sync autosync plist preview read-only", "ok", "preview writes no plist"))
        else:
            checks.append(CheckResult("sync autosync plist preview read-only", "error", "preview mutated state"))
        autosync_plist_write = _check_json_command(
            checks,
            env,
            "sync autosync plist write",
            ("sync", "autosync-plist", "--execute"),
        )
        if autosync_plist.exists():
            plist_payload = plistlib.loads(autosync_plist.read_bytes())
        else:
            plist_payload = {}
        if (
            autosync_plist_write.get("details", {}).get("state writes") == "autosync plist only"
            and autosync_plist_write.get("details", {}).get("launchctl execution") == "no"
            and autosync_plist_write.get("details", {}).get("scheduled sync execution") == "no"
            and plist_payload.get("Label") == "com.example.pcloud-bisync.dev"
            and plist_payload.get("ProgramArguments", [])[-3:] == ["sync", "background", "--execute"]
        ):
            checks.append(CheckResult("sync autosync plist write dev-only", "ok", str(autosync_plist)))
        else:
            checks.append(CheckResult("sync autosync plist write dev-only", "error", "plist write mismatch"))

        _check_dev_wrapper(
            checks,
            workspace,
            "pcloud-pushd",
            ("status",),
            "pushd status",
            state_dir / "pushd",
        )
        _check_dev_wrapper(
            checks,
            workspace,
            "pcloud-diffd",
            ("preview",),
            "diffd preview",
            state_dir / "diffd",
        )

        _check_json_command(
            checks,
            env,
            "pushd queue add",
            ("pushd", "queue", "add", "Documents/shadow-upload.pdf", "--execute"),
        )
        _check_json_command(
            checks,
            env,
            "diffd remote-change add",
            ("diffd", "remote-change", "add", "Documents/shadow-download.pdf", "--execute"),
        )

        pushd_preview = _check_json_command(checks, env, "pushd preview", ("pushd", "preview"))
        diffd_preview = _check_json_command(checks, env, "diffd preview", ("diffd", "preview"))
        pushd_status = _check_json_command(checks, env, "pushd status", ("pushd", "status"))
        diffd_status = _check_json_command(checks, env, "diffd status", ("diffd", "status"))
        if pushd_preview.get("details", {}).get("planned uploads") != 1:
            checks.append(CheckResult("pushd planned uploads", "error", "expected planned uploads = 1"))
        else:
            checks.append(CheckResult("pushd planned uploads", "ok", "planned uploads = 1"))
        if diffd_preview.get("details", {}).get("planned downloads") != 1:
            checks.append(CheckResult("diffd planned downloads", "error", "expected planned downloads = 1"))
        else:
            checks.append(CheckResult("diffd planned downloads", "ok", "planned downloads = 1"))
        if (
            pushd_status.get("details", {}).get("state writes") == "none"
            and pushd_status.get("details", {}).get("planned uploads") == 1
            and pushd_status.get("details", {}).get("launchd gate") == "closed"
            and pushd_status.get("details", {}).get("transfer gate") == "closed"
            and "last resident run status" in pushd_status.get("details", {})
            and "preflight checks" not in pushd_status.get("details", {})
        ):
            checks.append(CheckResult("pushd status read-only aggregate", "ok", "status summarizes plan/gates"))
        else:
            checks.append(CheckResult("pushd status read-only aggregate", "error", "pushd status summary mismatch"))
        if (
            diffd_status.get("details", {}).get("state writes") == "none"
            and diffd_status.get("details", {}).get("planned downloads") == 1
            and diffd_status.get("details", {}).get("launchd gate") == "closed"
            and diffd_status.get("details", {}).get("transfer gate") == "closed"
            and "last api poll run status" in diffd_status.get("details", {})
            and "preflight checks" not in diffd_status.get("details", {})
        ):
            checks.append(CheckResult("diffd status read-only aggregate", "ok", "status summarizes plan/gates"))
        else:
            checks.append(CheckResult("diffd status read-only aggregate", "error", "diffd status summary mismatch"))
        pushd_status_xbar = _run_cli(env, "pushd", "status", "--xbar")
        diffd_status_xbar = _run_cli(env, "diffd", "status", "--xbar")
        if (
            pushd_status_xbar.returncode == 0
            and "plan: uploads=1" in pushd_status_xbar.stdout
            and "last resident:" in pushd_status_xbar.stdout
            and "real-run" not in pushd_status_xbar.stdout
            and "validation-matrix" not in pushd_status_xbar.stdout
            and "last transfer:" not in pushd_status_xbar.stdout
        ):
            checks.append(CheckResult("pushd status xbar concise", "ok", "xbar shows safe summary"))
        else:
            checks.append(CheckResult("pushd status xbar concise", "error", "pushd xbar summary mismatch"))
        if (
            diffd_status_xbar.returncode == 0
            and "plan: downloads=1" in diffd_status_xbar.stdout
            and "last api poll:" in diffd_status_xbar.stdout
            and "real-run" not in diffd_status_xbar.stdout
            and "validation-matrix" not in diffd_status_xbar.stdout
            and "last transfer:" not in diffd_status_xbar.stdout
        ):
            checks.append(CheckResult("diffd status xbar concise", "ok", "xbar shows safe summary"))
        else:
            checks.append(CheckResult("diffd status xbar concise", "error", "diffd xbar summary mismatch"))
        gates_status_xbar = _run_cli(env, "gates", "status", "--xbar")
        if (
            gates_status_xbar.returncode == 0
            and "gate summary:" in gates_status_xbar.stdout
            and "pushd fswatch resident: gate=closed" in gates_status_xbar.stdout
            and "read-only command examples" not in gates_status_xbar.stdout
            and "PCLOUD_TOOLS_PUSHD_FSWATCH_RESIDENT_GATE" not in gates_status_xbar.stdout
            and "--execute" not in gates_status_xbar.stdout
        ):
            checks.append(CheckResult("gates status xbar concise", "ok", "xbar shows safe gate summary"))
        else:
            checks.append(CheckResult("gates status xbar concise", "error", "gates xbar summary mismatch"))

        pushd_policy = _check_json_command(checks, env, "pushd policy", ("pushd", "policy"))
        diffd_policy = _check_json_command(checks, env, "diffd policy", ("diffd", "policy"))
        if pushd_policy.get("details", {}).get("state writes") == "none":
            checks.append(CheckResult("pushd daemon policy read-only", "ok", "state writes none"))
        else:
            checks.append(CheckResult("pushd daemon policy read-only", "error", "policy mutated state"))
        if diffd_policy.get("details", {}).get("state writes") == "none":
            checks.append(CheckResult("diffd daemon policy read-only", "ok", "state writes none"))
        else:
            checks.append(CheckResult("diffd daemon policy read-only", "error", "policy mutated state"))
        pushd_launchd_gate = _check_json_command(
            checks,
            env,
            "pushd launchd gate",
            ("pushd", "launchd", "gate"),
        )
        diffd_launchd_gate = _check_json_command(
            checks,
            env,
            "diffd launchd gate",
            ("diffd", "launchd", "gate"),
        )
        pushd_launchd_plist_path = workspace / ".dev-state" / "launchd" / "com.example.pcloud-pushd.dev.plist"
        diffd_launchd_plist_path = workspace / ".dev-state" / "launchd" / "com.example.pcloud-diffd.dev.plist"
        pushd_launchd_plist_preview = _check_json_command(
            checks,
            env,
            "pushd launchd plist preview",
            ("pushd", "launchd", "plist"),
        )
        if (
            pushd_launchd_plist_preview.get("details", {}).get("state writes") == "none"
            and pushd_launchd_plist_preview.get("details", {}).get("launchctl execution") == "no"
            and not pushd_launchd_plist_path.exists()
        ):
            checks.append(CheckResult("pushd launchd plist preview read-only", "ok", "preview writes no plist"))
        else:
            checks.append(CheckResult("pushd launchd plist preview read-only", "error", "preview mutated state"))
        pushd_launchd_plist_write = _check_json_command(
            checks,
            env,
            "pushd launchd plist write",
            ("pushd", "launchd", "plist", "--execute"),
        )
        diffd_launchd_plist_write = _check_json_command(
            checks,
            env,
            "diffd launchd plist write",
            ("diffd", "launchd", "plist", "--execute"),
        )
        pushd_plist_payload = plistlib.loads(pushd_launchd_plist_path.read_bytes()) if pushd_launchd_plist_path.exists() else {}
        diffd_plist_payload = plistlib.loads(diffd_launchd_plist_path.read_bytes()) if diffd_launchd_plist_path.exists() else {}
        if (
            pushd_launchd_plist_write.get("details", {}).get("state writes") == "launchd plist only"
            and pushd_launchd_plist_write.get("details", {}).get("launchctl execution") == "no"
            and pushd_plist_payload.get("Label") == "com.example.pcloud-pushd.dev"
            and pushd_plist_payload.get("ProgramArguments", [])[1:4] == ["pushd", "fswatch", "resident-run"]
        ):
            checks.append(CheckResult("pushd launchd plist write dev-only", "ok", str(pushd_launchd_plist_path)))
        else:
            checks.append(CheckResult("pushd launchd plist write dev-only", "error", "pushd plist write mismatch"))
        if (
            diffd_launchd_plist_write.get("details", {}).get("state writes") == "launchd plist only"
            and diffd_launchd_plist_write.get("details", {}).get("launchctl execution") == "no"
            and diffd_plist_payload.get("Label") == "com.example.pcloud-diffd.dev"
            and diffd_plist_payload.get("ProgramArguments", [])[1:4] == ["diffd", "api-poll", "long-poll-run"]
        ):
            checks.append(CheckResult("diffd launchd plist write dev-only", "ok", str(diffd_launchd_plist_path)))
        else:
            checks.append(CheckResult("diffd launchd plist write dev-only", "error", "diffd plist write mismatch"))
        public_bin = root / "public-bin"
        public_bin.mkdir(parents=True, exist_ok=True)
        public_pcloud_manager = public_bin / "pcloud-manager"
        public_pcloud_manager.write_text("#!/bin/sh\nprintf 'shadow public pcloud-manager\\n'\n")
        public_pcloud_manager.chmod(0o755)
        public_launchctl_log = root / "public-launchctl.log"
        public_launchctl = public_bin / "launchctl"
        public_launchctl.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$PUBLIC_LAUNCHCTL_LOG\"\nexit 0\n")
        public_launchctl.chmod(0o755)
        public_fswatch = public_bin / "fswatch"
        public_fswatch.write_text("#!/bin/sh\nprintf 'shadow public fswatch\\n'\n")
        public_fswatch.chmod(0o755)
        public_env = dict(env)
        public_env.pop("PCLOUD_TOOLS_DEV", None)
        public_env["PCLOUD_TOOLS_CORE_DIR"] = str(workspace)
        public_env["PCLOUD_TOOLS_ALLOWLIST_FILE"] = str(workspace / ".pcloud-sync-allowlist")
        public_env["PCLOUD_TOOLS_PCLOUD_API_TOKEN"] = "shadow-token"
        public_env["PCLOUD_TOOLS_PUBLIC_ENTRYPOINT"] = str(public_pcloud_manager)
        public_env["PATH"] = f"{public_bin}:{public_env.get('PATH', '')}"
        public_env["PUBLIC_LAUNCHCTL_LOG"] = str(public_launchctl_log)
        public_pushd_plist_path = Path(public_env["HOME"]) / "Library" / "LaunchAgents" / "com.takafumi.pcloud-pushd.plist"
        public_diffd_plist_path = Path(public_env["HOME"]) / "Library" / "LaunchAgents" / "com.takafumi.pcloud-diffd.plist"
        pushd_launchd_review = _check_json_command(
            checks,
            public_env,
            "pushd launchd review",
            ("pushd", "launchd", "review"),
        )
        diffd_launchd_review = _check_json_command(
            checks,
            public_env,
            "diffd launchd review",
            ("diffd", "launchd", "review"),
        )
        if (
            pushd_launchd_review.get("details", {}).get("state writes") == "none"
            and pushd_launchd_review.get("details", {}).get("launchctl execution") == "no"
            and pushd_launchd_review.get("details", {}).get("service label") == "com.takafumi.pcloud-pushd"
            and pushd_launchd_review.get("details", {}).get("program arguments", [])[:4]
            == [str(public_pcloud_manager), "pushd", "fswatch", "resident-run"]
            and not public_pushd_plist_path.exists()
        ):
            checks.append(CheckResult("pushd launchd review read-only", "ok", "public plist review only"))
        else:
            checks.append(CheckResult("pushd launchd review read-only", "error", "pushd review mismatch"))
        if (
            diffd_launchd_review.get("details", {}).get("state writes") == "none"
            and diffd_launchd_review.get("details", {}).get("launchctl execution") == "no"
            and diffd_launchd_review.get("details", {}).get("service label") == "com.takafumi.pcloud-diffd"
            and diffd_launchd_review.get("details", {}).get("foreground command preview", [])[:4]
            == [str(public_pcloud_manager), "diffd", "api-poll", "long-poll-run"]
            and not public_diffd_plist_path.exists()
        ):
            checks.append(CheckResult("diffd launchd review read-only", "ok", "public plist review only"))
        else:
            checks.append(CheckResult("diffd launchd review read-only", "error", "diffd review mismatch"))
        public_closed = _run_cli(
            public_env,
            "pushd",
            "launchd",
            "plist",
            "--execute",
            "--public-write",
            "--json",
        )
        try:
            public_closed_payload = _payload(public_closed)
        except json.JSONDecodeError:
            public_closed_payload = {}
        if (
            public_closed.returncode == 1
            and public_closed_payload.get("status") == "error"
            and public_closed_payload.get("details", {}).get("state writes") == "none"
            and not public_pushd_plist_path.exists()
        ):
            checks.append(CheckResult("pushd launchd public plist gate closed", "ok", "public write refused"))
        else:
            checks.append(CheckResult("pushd launchd public plist gate closed", "error", "closed gate wrote or returned ok"))
        public_open_env = dict(public_env)
        public_open_env["PCLOUD_TOOLS_PUSHD_LAUNCHD_PLIST_GATE"] = "operator-approved-pushd-launchd-plist-v1"
        public_open = _check_json_command(
            checks,
            public_open_env,
            "pushd launchd public plist write",
            (
                "pushd",
                "launchd",
                "plist",
                "--execute",
                "--public-write",
                "--operator-reviewed-plist",
                "--reviewer-approved-public-target",
                "--reviewer-approved-no-bootstrap",
            ),
        )
        public_pushd_payload = (
            plistlib.loads(public_pushd_plist_path.read_bytes()) if public_pushd_plist_path.exists() else {}
        )
        if (
            public_open.get("details", {}).get("state writes") == "public launchd plist only"
            and public_open.get("details", {}).get("launchctl execution") == "no"
            and public_open.get("details", {}).get("persistent daemon start") == "no"
            and public_pushd_payload.get("Label") == "com.takafumi.pcloud-pushd"
            and public_pushd_payload.get("ProgramArguments", [])[:4]
            == [str(public_pcloud_manager), "pushd", "fswatch", "resident-run"]
            and not public_diffd_plist_path.exists()
            and not public_launchctl_log.exists()
        ):
            checks.append(CheckResult("pushd launchd public plist write gated", "ok", str(public_pushd_plist_path)))
        else:
            checks.append(CheckResult("pushd launchd public plist write gated", "error", "public plist write mismatch"))
        resident_report_path = root / "launchd-resident-plist-shadow-report.json"
        resident_report_path.write_text(
            json.dumps(
                {
                    "status": "ok",
                    "workspace": str(workspace),
                    "state_dir": str(state_dir),
                    "checks": [
                        {"name": "temporary workspace guard", "status": "ok"},
                        {"name": "temporary state dir guard", "status": "ok"},
                        {"name": "unsafe state dir guard", "status": "ok"},
                    ],
                }
            )
        )
        resident_preview = _check_json_command(
            checks,
            public_env,
            "pushd launchd resident plist preview",
            ("pushd", "launchd", "resident-plist", "--report-path", str(resident_report_path)),
        )
        if (
            resident_preview.get("details", {}).get("state writes") == "none"
            and resident_preview.get("details", {}).get("launchctl execution") == "no"
            and resident_preview.get("details", {}).get("persistent daemon start") == "no"
        ):
            checks.append(CheckResult("pushd launchd resident plist preview read-only", "ok", "no launchctl"))
        else:
            checks.append(CheckResult("pushd launchd resident plist preview read-only", "error", "resident preview mismatch"))
        resident_closed = _run_cli(
            public_env,
            "pushd",
            "launchd",
            "resident-plist",
            "--report-path",
            str(resident_report_path),
            "--execute",
            "--json",
        )
        try:
            resident_closed_payload = _payload(resident_closed)
        except json.JSONDecodeError:
            resident_closed_payload = {}
        if (
            resident_closed.returncode == 1
            and resident_closed_payload.get("details", {}).get("state writes") == "none"
        ):
            checks.append(CheckResult("pushd launchd resident plist gate closed", "ok", "resident write refused"))
        else:
            checks.append(CheckResult("pushd launchd resident plist gate closed", "error", "closed resident gate wrote"))
        public_resident_env = dict(public_env)
        public_resident_env["PCLOUD_TOOLS_PUSHD_LAUNCHD_RESIDENT_PLIST_GATE"] = (
            "operator-approved-pushd-launchd-resident-plist-v1"
        )
        resident_open = _check_json_command(
            checks,
            public_resident_env,
            "pushd launchd resident plist write",
            (
                "pushd",
                "launchd",
                "resident-plist",
                "--report-path",
                str(resident_report_path),
                "--operator-reviewed-resident-command",
                "--reviewer-approved-resident-environment",
                "--reviewer-approved-no-bootstrap",
                "--execute",
            ),
        )
        resident_payload = plistlib.loads(public_pushd_plist_path.read_bytes()) if public_pushd_plist_path.exists() else {}
        resident_env = resident_payload.get("EnvironmentVariables", {})
        if (
            resident_open.get("details", {}).get("state writes") == "public launchd resident plist only"
            and resident_open.get("details", {}).get("launchctl execution") == "no"
            and resident_open.get("details", {}).get("persistent daemon start") == "no"
            and resident_payload.get("ProgramArguments", [])[:4]
            == [str(public_pcloud_manager), "pushd", "fswatch", "resident-run"]
            and "--execute" in resident_payload.get("ProgramArguments", [])
            and resident_env.get("PCLOUD_TOOLS_PUSHD_FSWATCH_RESIDENT_GATE") == "operator-approved-fswatch-resident-v1"
            and "/opt/homebrew/bin" in resident_env.get("PATH", "")
        ):
            checks.append(CheckResult("pushd launchd resident plist write gated", "ok", "resident plist written"))
        else:
            checks.append(CheckResult("pushd launchd resident plist write gated", "error", "resident plist mismatch"))
        reload_preview = _check_json_command(
            checks,
            public_env,
            "pushd launchd reload preview",
            ("pushd", "launchd", "reload", "--report-path", str(resident_report_path)),
        )
        if (
            reload_preview.get("details", {}).get("launchctl execution") == "no"
            and reload_preview.get("details", {}).get("persistent daemon start") == "no"
            and reload_preview.get("details", {}).get("resident plist status") == "operational"
        ):
            checks.append(CheckResult("pushd launchd reload preview read-only", "ok", "launchctl not executed"))
        else:
            checks.append(CheckResult("pushd launchd reload preview read-only", "error", "reload preview mismatch"))
        reload_closed = _run_cli(
            public_env,
            "pushd",
            "launchd",
            "reload",
            "--report-path",
            str(resident_report_path),
            "--execute",
            "--json",
        )
        try:
            reload_closed_payload = _payload(reload_closed)
        except json.JSONDecodeError:
            reload_closed_payload = {}
        if (
            reload_closed.returncode == 1
            and reload_closed_payload.get("details", {}).get("launchctl execution") == "no"
        ):
            checks.append(CheckResult("pushd launchd reload gate closed", "ok", "reload refused"))
        else:
            checks.append(CheckResult("pushd launchd reload gate closed", "error", "closed reload gate ran"))
        if public_launchctl_log.exists():
            public_launchctl_log.write_text("")
        public_reload_env = dict(public_env)
        public_reload_env["PCLOUD_TOOLS_PUSHD_LAUNCHD_RELOAD_GATE"] = "operator-approved-pushd-launchd-reload-v1"
        reload_open = _check_json_command(
            checks,
            public_reload_env,
            "pushd launchd reload fake launchctl",
            (
                "pushd",
                "launchd",
                "reload",
                "--report-path",
                str(resident_report_path),
                "--operator-reviewed-resident-plist",
                "--reviewer-approved-bootout-bootstrap",
                "--reviewer-approved-rollback-policy",
                "--execute",
            ),
        )
        reload_launchctl_log = public_launchctl_log.read_text() if public_launchctl_log.exists() else ""
        if (
            reload_open.get("details", {}).get("launchctl execution") == "yes"
            and reload_open.get("details", {}).get("persistent daemon start") == "yes-if-bootstrap-succeeds"
            and reload_open.get("details", {}).get("state writes") == "launchctl reload only"
            and "bootout gui/" in reload_launchctl_log
            and "bootstrap gui/" in reload_launchctl_log
        ):
            checks.append(CheckResult("pushd launchd reload fake launchctl state", "ok", "fake launchctl recorded"))
        else:
            checks.append(CheckResult("pushd launchd reload fake launchctl state", "error", "reload mismatch"))
        diffd_operational_preview = _check_json_command(
            checks,
            public_env,
            "diffd launchd operational plist preview",
            (
                "diffd",
                "launchd",
                "resident-plist",
                "--report-path",
                str(resident_report_path),
                "--start-interval-seconds",
                "60",
            ),
        )
        if (
            diffd_operational_preview.get("details", {}).get("state writes") == "none"
            and diffd_operational_preview.get("details", {}).get("launchctl execution") == "no"
            and diffd_operational_preview.get("details", {}).get("persistent daemon start") == "no"
            and diffd_operational_preview.get("details", {}).get("start interval seconds") == 60
        ):
            checks.append(CheckResult("diffd launchd operational plist preview read-only", "ok", "no launchctl"))
        else:
            checks.append(CheckResult("diffd launchd operational plist preview read-only", "error", "operational preview mismatch"))
        public_diffd_operational_env = dict(public_env)
        public_diffd_operational_env["PCLOUD_TOOLS_DIFFD_LAUNCHD_LONG_POLL_PLIST_GATE"] = (
            "operator-approved-diffd-launchd-long-poll-plist-v1"
        )
        diffd_operational_open = _check_json_command(
            checks,
            public_diffd_operational_env,
            "diffd launchd operational plist write",
            (
                "diffd",
                "launchd",
                "resident-plist",
                "--report-path",
                str(resident_report_path),
                "--start-interval-seconds",
                "60",
                "--operator-reviewed-resident-command",
                "--reviewer-approved-resident-environment",
                "--reviewer-approved-no-bootstrap",
                "--execute",
            ),
        )
        diffd_operational_payload = (
            plistlib.loads(public_diffd_plist_path.read_bytes()) if public_diffd_plist_path.exists() else {}
        )
        diffd_operational_env = diffd_operational_payload.get("EnvironmentVariables", {})
        if (
            diffd_operational_open.get("details", {}).get("state writes") == "public launchd resident plist only"
            and diffd_operational_open.get("details", {}).get("launchctl execution") == "no"
            and diffd_operational_payload.get("ProgramArguments", [])[:4]
            == [str(public_pcloud_manager), "diffd", "api-poll", "long-poll-run"]
            and "--live-api" in diffd_operational_payload.get("ProgramArguments", [])
            and "--max-iterations" in diffd_operational_payload.get("ProgramArguments", [])
            and "--execute" in diffd_operational_payload.get("ProgramArguments", [])
            and diffd_operational_payload.get("StartInterval") == 60
            and diffd_operational_env.get("PCLOUD_TOOLS_DIFFD_API_LONG_POLL_GATE")
            == "operator-approved-api-long-poll-v1"
        ):
            checks.append(CheckResult("diffd launchd operational plist write gated", "ok", "operational plist written"))
        else:
            checks.append(CheckResult("diffd launchd operational plist write gated", "error", "operational plist mismatch"))
        diffd_reload_preview = _check_json_command(
            checks,
            public_env,
            "diffd launchd reload preview",
            ("diffd", "launchd", "reload", "--report-path", str(resident_report_path)),
        )
        if (
            diffd_reload_preview.get("details", {}).get("launchctl execution") == "no"
            and diffd_reload_preview.get("details", {}).get("persistent daemon start") == "no"
            and diffd_reload_preview.get("details", {}).get("resident plist status") == "operational"
        ):
            checks.append(CheckResult("diffd launchd reload preview read-only", "ok", "launchctl not executed"))
        else:
            checks.append(CheckResult("diffd launchd reload preview read-only", "error", "reload preview mismatch"))
        public_launchctl_log.write_text("")
        public_diffd_reload_env = dict(public_env)
        public_diffd_reload_env["PCLOUD_TOOLS_DIFFD_LAUNCHD_RELOAD_GATE"] = "operator-approved-diffd-launchd-reload-v1"
        diffd_reload_open = _check_json_command(
            checks,
            public_diffd_reload_env,
            "diffd launchd reload fake launchctl",
            (
                "diffd",
                "launchd",
                "reload",
                "--report-path",
                str(resident_report_path),
                "--operator-reviewed-resident-plist",
                "--reviewer-approved-bootout-bootstrap",
                "--reviewer-approved-rollback-policy",
                "--execute",
            ),
        )
        diffd_reload_launchctl_log = public_launchctl_log.read_text() if public_launchctl_log.exists() else ""
        if (
            diffd_reload_open.get("details", {}).get("launchctl execution") == "yes"
            and diffd_reload_open.get("details", {}).get("persistent daemon start") == "yes-if-bootstrap-succeeds"
            and diffd_reload_open.get("details", {}).get("state writes") == "launchctl reload only"
            and "bootout gui/" in diffd_reload_launchctl_log
            and "bootstrap gui/" in diffd_reload_launchctl_log
        ):
            checks.append(CheckResult("diffd launchd reload fake launchctl state", "ok", "fake launchctl recorded"))
        else:
            checks.append(CheckResult("diffd launchd reload fake launchctl state", "error", "reload mismatch"))
        register_report_path = root / "launchd-register-shadow-report.json"
        register_report_path.write_text(
            json.dumps(
                {
                    "status": "ok",
                    "workspace": str(workspace),
                    "state_dir": str(state_dir),
                    "checks": [
                        {"name": "temporary workspace guard", "status": "ok"},
                        {"name": "temporary state dir guard", "status": "ok"},
                        {"name": "unsafe state dir guard", "status": "ok"},
                        {"name": "launchd register shadow", "status": "ok"},
                    ],
                }
            )
        )
        register_preview = _check_json_command(
            checks,
            public_env,
            "pushd launchd register preview",
            ("pushd", "launchd", "register"),
        )
        preview_launchctl_log = public_launchctl_log.read_text() if public_launchctl_log.exists() else ""
        if (
            register_preview.get("details", {}).get("launchctl execution") == "no"
            and register_preview.get("details", {}).get("persistent daemon start") == "no"
            and "enable gui/" not in preview_launchctl_log
        ):
            checks.append(CheckResult("pushd launchd register preview read-only", "ok", "launchctl not executed"))
        else:
            checks.append(CheckResult("pushd launchd register preview read-only", "error", "preview ran launchctl"))
        register_closed = _run_cli(public_env, "pushd", "launchd", "register", "--execute", "--json")
        try:
            register_closed_payload = _payload(register_closed)
        except json.JSONDecodeError:
            register_closed_payload = {}
        closed_launchctl_log = public_launchctl_log.read_text() if public_launchctl_log.exists() else ""
        if (
            register_closed.returncode == 1
            and register_closed_payload.get("status") == "error"
            and register_closed_payload.get("details", {}).get("launchctl execution") == "no"
            and "enable gui/" not in closed_launchctl_log
        ):
            checks.append(CheckResult("pushd launchd register gate closed", "ok", "launchctl refused"))
        else:
            checks.append(CheckResult("pushd launchd register gate closed", "error", "closed gate ran launchctl"))
        if public_launchctl_log.exists():
            public_launchctl_log.write_text("")
        public_register_env = dict(public_open_env)
        public_register_env["PCLOUD_TOOLS_PUSHD_LAUNCHD_GATE"] = "operator-approved-pushd-launchd-v1"
        register_open = _check_json_command(
            checks,
            public_register_env,
            "pushd launchd register fake launchctl",
            (
                "pushd",
                "launchd",
                "register",
                "--report-path",
                str(register_report_path),
                "--operator-reviewed-daemon-command",
                "--reviewer-approved-plist-policy",
                "--reviewer-approved-launchctl-policy",
                "--reviewer-approved-rollback-policy",
                "--execute",
            ),
        )
        register_launchctl_log = public_launchctl_log.read_text() if public_launchctl_log.exists() else ""
        if (
            register_open.get("details", {}).get("launchctl execution") == "yes"
            and register_open.get("details", {}).get("persistent daemon start") == "yes-if-bootstrap-succeeds"
            and register_open.get("details", {}).get("state writes") == "launchctl registration only"
            and "enable gui/" in register_launchctl_log
            and "bootstrap gui/" in register_launchctl_log
        ):
            checks.append(CheckResult("pushd launchd register fake launchctl state", "ok", "fake launchctl recorded"))
        else:
            checks.append(CheckResult("pushd launchd register fake launchctl state", "error", "register mismatch"))
        pushd_launchd_status = _check_json_command(
            checks,
            env,
            "pushd launchd status",
            ("pushd", "launchd", "status"),
        )
        diffd_launchd_status = _check_json_command(
            checks,
            env,
            "diffd launchd status",
            ("diffd", "launchd", "status"),
        )
        if (
            pushd_launchd_gate.get("details", {}).get("launchd gate status") == "closed"
            and pushd_launchd_gate.get("details", {}).get("state writes") == "none"
            and pushd_launchd_gate.get("details", {}).get("launchd can register") == "no"
            and pushd_launchd_gate.get("details", {}).get("plist status") == "draft-only; not written by this command"
        ):
            checks.append(CheckResult("pushd launchd gate read-only", "ok", "launchd registration blocked"))
        else:
            checks.append(CheckResult("pushd launchd gate read-only", "error", "launchd gate mismatch"))
        if (
            diffd_launchd_gate.get("details", {}).get("launchd gate status") == "closed"
            and diffd_launchd_gate.get("details", {}).get("state writes") == "none"
            and diffd_launchd_gate.get("details", {}).get("launchd can register") == "no"
            and diffd_launchd_gate.get("details", {}).get("plist status") == "draft-only; not written by this command"
        ):
            checks.append(CheckResult("diffd launchd gate read-only", "ok", "launchd registration blocked"))
        else:
            checks.append(CheckResult("diffd launchd gate read-only", "error", "launchd gate mismatch"))
        if (
            pushd_launchd_status.get("details", {}).get("state writes") == "none"
            and pushd_launchd_status.get("details", {}).get("launchd can register") == "no"
            and pushd_launchd_status.get("details", {}).get("launchctl execution") in {"print-only", "none"}
        ):
            checks.append(CheckResult("pushd launchd status read-only", "ok", "launchd status did not mutate"))
        else:
            checks.append(CheckResult("pushd launchd status read-only", "error", "launchd status mismatch"))
        if (
            diffd_launchd_status.get("details", {}).get("state writes") == "none"
            and diffd_launchd_status.get("details", {}).get("launchd can register") == "no"
            and diffd_launchd_status.get("details", {}).get("launchctl execution") in {"print-only", "none"}
        ):
            checks.append(CheckResult("diffd launchd status read-only", "ok", "launchd status did not mutate"))
        else:
            checks.append(CheckResult("diffd launchd status read-only", "error", "launchd status mismatch"))

        fswatch_fixture = workspace / "pushd-fswatch-events.txt"
        fswatch_fixture.write_text("Documents/shadow-upload.pdf\tCreated Updated\nprivate/skip.txt\tCreated\n")
        fswatch_preview = _check_json_command(
            checks,
            env,
            "pushd fswatch fixture preview",
            ("pushd", "fswatch", "preview", "--fixture", str(fswatch_fixture)),
        )
        if fswatch_preview.get("details", {}).get("planned uploads") == 1:
            checks.append(CheckResult("pushd fswatch fixture planned uploads", "ok", "planned uploads = 1"))
        else:
            checks.append(
                CheckResult(
                    "pushd fswatch fixture planned uploads",
                    "error",
                    "expected planned uploads = 1",
                )
            )
        fswatch_probe = _check_json_command(
            checks,
            env,
            "pushd fswatch probe preview",
            ("pushd", "fswatch", "probe"),
        )
        if fswatch_probe.get("details", {}).get("gate status") == "closed":
            checks.append(CheckResult("pushd fswatch probe gate closed", "ok", "probe is preview-only"))
        else:
            checks.append(
                CheckResult(
                    "pushd fswatch probe gate closed",
                    "error",
                    "missing closed gate status",
                )
            )
        fswatch_bin_dir = workspace / ".dev-state" / "fswatch-bin"
        fswatch_bin_dir.mkdir(parents=True, exist_ok=True)
        fake_fswatch = fswatch_bin_dir / "fswatch"
        fake_fswatch.write_text("#!/bin/sh\nexit 0\n")
        fake_fswatch.chmod(0o755)
        fswatch_gate_env = dict(env)
        fswatch_gate_env["PATH"] = f"{fswatch_bin_dir}:{env.get('PATH', '')}"
        fswatch_resident_gate = _check_json_command(
            checks,
            fswatch_gate_env,
            "pushd fswatch resident gate",
            (
                "pushd",
                "fswatch",
                "resident-gate",
                "--report-path",
                str(saved_shadow_report),
                "--operator-reviewed-probe",
                "--reviewer-approved-queue-policy",
                "--reviewer-approved-process-policy",
            ),
        )
        if (
            fswatch_resident_gate.get("details", {}).get("resident gate status") == "closed"
            and fswatch_resident_gate.get("details", {}).get("resident can start") == "no"
            and fswatch_resident_gate.get("details", {}).get("state writes") == "none"
            and fswatch_resident_gate.get("details", {}).get("fswatch availability") == "available"
            and fswatch_resident_gate.get("details", {}).get("resident approval status") == "complete-read-only"
            and "--one-event" not in fswatch_resident_gate.get("details", {}).get("resident command preview", [])
        ):
            checks.append(CheckResult("pushd fswatch resident gate closed", "ok", "resident start is gated"))
        else:
            checks.append(CheckResult("pushd fswatch resident gate closed", "error", "resident gate mismatch"))
        resident_run_closed = _check_json_command(
            checks,
            fswatch_gate_env,
            "pushd fswatch resident-run gate closed",
            (
                "pushd",
                "fswatch",
                "resident-run",
                "--report-path",
                str(saved_shadow_report),
                "--operator-reviewed-probe",
                "--reviewer-approved-queue-policy",
                "--reviewer-approved-process-policy",
                "--execute",
            ),
            allowed_status={"error"},
        )
        if (
            resident_run_closed.get("details", {}).get("state writes") == "none"
            and resident_run_closed.get("details", {}).get("resident can start") == "no"
        ):
            checks.append(CheckResult("pushd fswatch resident-run closed no writes", "ok", "resident gate refused"))
        else:
            checks.append(
                CheckResult("pushd fswatch resident-run closed no writes", "error", "closed gate wrote state")
            )
        fake_fswatch.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$PCLOUD_TOOLS_WORKSPACE_ROOT/Documents/resident-shadow.txt\"\n"
        )
        resident_run_env = dict(fswatch_gate_env)
        resident_run_env["PCLOUD_TOOLS_PUSHD_FSWATCH_RESIDENT_GATE"] = "operator-approved-fswatch-resident-v1"
        resident_run = _check_json_command(
            checks,
            resident_run_env,
            "pushd fswatch resident-run fake",
            (
                "pushd",
                "fswatch",
                "resident-run",
                "--report-path",
                str(saved_shadow_report),
                "--operator-reviewed-probe",
                "--reviewer-approved-queue-policy",
                "--reviewer-approved-process-policy",
                "--max-events",
                "1",
                "--execute",
            ),
        )
        if (
            resident_run.get("details", {}).get("resident can start") == "yes"
            and resident_run.get("details", {}).get("queue records appended") == 1
            and resident_run.get("details", {}).get("state writes") == "pushd queue and resident run state"
        ):
            checks.append(CheckResult("pushd fswatch resident-run fake queue", "ok", "queue append recorded"))
        else:
            checks.append(CheckResult("pushd fswatch resident-run fake queue", "error", "resident run mismatch"))
        _check_json_command(
            checks,
            env,
            "pushd fswatch resident-run cleanup",
            ("pushd", "queue", "remove", "Documents/resident-shadow.txt", "--execute"),
        )
        fake_fswatch.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$PCLOUD_TOOLS_WORKSPACE_ROOT/Documents/resident-duplicate.txt\"\n"
            "printf '%s\\n' \"$PCLOUD_TOOLS_WORKSPACE_ROOT/Documents/resident-duplicate.txt\"\n"
        )
        duplicate_run = _check_json_command(
            checks,
            resident_run_env,
            "pushd fswatch resident-run duplicate skip",
            (
                "pushd",
                "fswatch",
                "resident-run",
                "--report-path",
                str(saved_shadow_report),
                "--operator-reviewed-probe",
                "--reviewer-approved-queue-policy",
                "--reviewer-approved-process-policy",
                "--max-events",
                "2",
                "--execute",
            ),
        )
        if (
            duplicate_run.get("details", {}).get("queue records appended") == 1
            and duplicate_run.get("details", {}).get("debounce events skipped") == 1
        ):
            checks.append(CheckResult("pushd fswatch resident-run debounce skip", "ok", "debounce skipped"))
        else:
            checks.append(CheckResult("pushd fswatch resident-run debounce skip", "error", "debounce was not skipped"))
        _check_json_command(
            checks,
            env,
            "pushd fswatch resident-run duplicate cleanup",
            ("pushd", "queue", "remove", "Documents/resident-duplicate.txt", "--execute"),
        )

        diff_fixture = workspace / "pcloud-diff.json"
        diff_fixture.write_text(
            json.dumps(
                {
                    "diffid": "shadow-1",
                    "entries": [
                        {"path": "Documents/shadow-download.pdf", "event": "modified"},
                        {"path": "", "event": "invalid"},
                    ],
                }
            )
        )
        diff_fixture_preview = _check_json_command(
            checks,
            env,
            "diffd diff fixture preview",
            ("diffd", "diff", "preview", "--fixture", str(diff_fixture)),
        )
        if diff_fixture_preview.get("details", {}).get("planned downloads") == 1:
            checks.append(CheckResult("diffd diff fixture planned downloads", "ok", "planned downloads = 1"))
        else:
            checks.append(
                CheckResult(
                    "diffd diff fixture planned downloads",
                    "error",
                    "expected planned downloads = 1",
                )
            )
        api_poll_preview = _check_json_command(
            checks,
            env,
            "diffd api-poll preview",
            ("diffd", "api-poll", "preview"),
        )
        if api_poll_preview.get("details", {}).get("gate status") == "closed":
            checks.append(CheckResult("diffd api-poll gate closed", "ok", "API poll is preview-only"))
        else:
            checks.append(
                CheckResult(
                    "diffd api-poll gate closed",
                    "error",
                    "missing closed gate status",
                )
            )
        api_long_poll_gate = _check_json_command(
            checks,
            env,
            "diffd api-poll long-poll gate",
            (
                "diffd",
                "api-poll",
                "long-poll-gate",
                "--report-path",
                str(saved_shadow_report),
                "--operator-reviewed-preview",
                "--reviewer-approved-response-policy",
                "--reviewer-approved-credential-policy",
                "--reviewer-approved-process-policy",
            ),
        )
        if (
            api_long_poll_gate.get("details", {}).get("long-poll gate status") == "closed"
            and api_long_poll_gate.get("details", {}).get("long-poll can start") == "no"
            and api_long_poll_gate.get("details", {}).get("state writes") == "none"
            and api_long_poll_gate.get("details", {}).get("long-poll approval status") == "complete-read-only"
            and api_long_poll_gate.get("details", {}).get("request path") == "/diff"
        ):
            checks.append(CheckResult("diffd api-poll long-poll gate closed", "ok", "long-poll start is gated"))
        else:
            checks.append(
                CheckResult("diffd api-poll long-poll gate closed", "error", "long-poll gate mismatch")
            )
        api_long_poll_fixture = workspace / "pcloud-api-long-poll.json"
        api_long_poll_fixture.write_text(
            json.dumps(
                {
                    "diffid": "123",
                    "entries": [
                        {"diffid": 0, "event": "reset"},
                        {
                            "diffid": 1,
                            "event": "createfolder",
                            "metadata": {
                                "isfolder": True,
                                "folderid": 10,
                                "parentfolderid": 0,
                                "name": "Documents",
                            },
                        },
                        {"path": "Documents/api-shadow.txt", "event": "modified"},
                        {
                            "diffid": 2,
                            "event": "createfile",
                            "metadata": {
                                "isfolder": False,
                                "parentfolderid": 10,
                                "name": "api-shadow-metadata.txt",
                            },
                        },
                        {"path": "private/api-shadow.txt", "event": "modified"},
                    ],
                }
            )
        )
        api_long_poll_run_closed = _check_json_command(
            checks,
            env,
            "diffd api-poll long-poll-run gate closed",
            (
                "diffd",
                "api-poll",
                "long-poll-run",
                "--report-path",
                str(saved_shadow_report),
                "--operator-reviewed-preview",
                "--reviewer-approved-response-policy",
                "--reviewer-approved-credential-policy",
                "--reviewer-approved-process-policy",
                "--fixture",
                str(api_long_poll_fixture),
                "--execute",
            ),
            allowed_status={"error"},
        )
        api_long_poll_remote_changes = workspace / ".dev-state" / "state" / "diffd" / "remote-changes.json"
        api_long_poll_diffid = workspace / ".dev-state" / "state" / "daemon" / "diffid"
        closed_remote_changes = []
        if api_long_poll_remote_changes.exists():
            closed_remote_changes = json.loads(api_long_poll_remote_changes.read_text())
        closed_has_api_shadow = any(
            isinstance(item, dict) and item.get("path") == "Documents/api-shadow.txt"
            for item in closed_remote_changes
        )
        closed_diffid = api_long_poll_diffid.read_text().strip() if api_long_poll_diffid.exists() else "-"
        if (
            api_long_poll_run_closed.get("details", {}).get("state writes") == "none"
            and api_long_poll_run_closed.get("details", {}).get("long-poll can start") == "no"
            and not closed_has_api_shadow
            and closed_diffid != "123"
        ):
            checks.append(CheckResult("diffd api-poll long-poll-run closed no writes", "ok", "long-poll gate refused"))
        else:
            checks.append(
                CheckResult(
                    "diffd api-poll long-poll-run closed no writes",
                    "error",
                    "closed API long-poll gate wrote state",
                )
            )
        api_long_poll_run_env = dict(env)
        api_long_poll_run_env["PCLOUD_TOOLS_DIFFD_API_LONG_POLL_GATE"] = "operator-approved-api-long-poll-v1"
        api_long_poll_run = _check_json_command(
            checks,
            api_long_poll_run_env,
            "diffd api-poll long-poll-run fixture",
            (
                "diffd",
                "api-poll",
                "long-poll-run",
                "--report-path",
                str(saved_shadow_report),
                "--operator-reviewed-preview",
                "--reviewer-approved-response-policy",
                "--reviewer-approved-credential-policy",
                "--reviewer-approved-process-policy",
                "--fixture",
                str(api_long_poll_fixture),
                "--max-iterations",
                "1",
                "--execute",
            ),
        )
        if (
            api_long_poll_run.get("details", {}).get("long-poll can start") == "yes"
            and api_long_poll_run.get("details", {}).get("download records appended") == 2
            and api_long_poll_run.get("details", {}).get("skipped download records") == 1
            and api_long_poll_run.get("details", {}).get("invalid diff changes") == 0
            and api_long_poll_run.get("details", {}).get("written diffid") == "123"
            and api_long_poll_remote_changes.exists()
            and api_long_poll_diffid.read_text().strip() == "123"
        ):
            checks.append(CheckResult("diffd api-poll long-poll-run state", "ok", "remote change and diffid recorded"))
        else:
            checks.append(CheckResult("diffd api-poll long-poll-run state", "error", "long-poll run mismatch"))
        _check_json_command(
            checks,
            env,
            "diffd api-poll long-poll-run cleanup",
            ("diffd", "remote-change", "remove", "Documents/api-shadow.txt", "--execute"),
        )
        _check_json_command(
            checks,
            env,
            "diffd api-poll long-poll-run metadata cleanup",
            ("diffd", "remote-change", "remove", "Documents/api-shadow-metadata.txt", "--execute"),
        )
        api_long_poll_cache_fixture = workspace / "pcloud-api-long-poll-cache.json"
        api_long_poll_cache_fixture.write_text(
            json.dumps(
                {
                    "diffid": "124",
                    "entries": [
                        {
                            "diffid": 124,
                            "event": "createfile",
                            "metadata": {
                                "isfolder": False,
                                "parentfolderid": 10,
                                "name": "api-shadow-cache.txt",
                            },
                        },
                    ],
                }
            )
        )
        api_long_poll_cache_run = _check_json_command(
            checks,
            api_long_poll_run_env,
            "diffd api-poll long-poll-run folder cache",
            (
                "diffd",
                "api-poll",
                "long-poll-run",
                "--report-path",
                str(saved_shadow_report),
                "--operator-reviewed-preview",
                "--reviewer-approved-response-policy",
                "--reviewer-approved-credential-policy",
                "--reviewer-approved-process-policy",
                "--fixture",
                str(api_long_poll_cache_fixture),
                "--max-iterations",
                "1",
                "--execute",
            ),
        )
        if (
            api_long_poll_cache_run.get("details", {}).get("folder cache entries before", 0) >= 1
            and api_long_poll_cache_run.get("details", {}).get("download records appended") == 1
            and api_long_poll_cache_run.get("details", {}).get("invalid diff changes") == 0
        ):
            checks.append(CheckResult("diffd api-poll long-poll-run folder cache state", "ok", "cached folder path reused"))
        else:
            checks.append(CheckResult("diffd api-poll long-poll-run folder cache state", "error", "folder cache mismatch"))
        _check_json_command(
            checks,
            env,
            "diffd api-poll long-poll-run folder cache cleanup",
            ("diffd", "remote-change", "remove", "Documents/api-shadow-cache.txt", "--execute"),
        )
        folder_cache_add = _check_json_command(
            checks,
            env,
            "diffd folder-cache add",
            ("diffd", "folder-cache", "add", "29913863697", "bench_test", "--execute"),
        )
        folder_cache_status = _check_json_command(
            checks,
            env,
            "diffd folder-cache status",
            ("diffd", "folder-cache", "status"),
        )
        folder_entries = folder_cache_status.get("details", {}).get("entries", [])
        if (
            folder_cache_add.get("details", {}).get("state writes") == "diffd folder cache"
            and any(
                isinstance(item, dict)
                and item.get("folder_id") == "29913863697"
                and item.get("path") == "bench_test"
                for item in folder_entries
            )
        ):
            checks.append(CheckResult("diffd folder-cache state", "ok", "folder cache mapping recorded"))
        else:
            checks.append(CheckResult("diffd folder-cache state", "error", "folder cache mapping missing"))
        _check_json_command(
            checks,
            env,
            "diffd folder-cache remove",
            ("diffd", "folder-cache", "remove", "29913863697", "--execute"),
        )
        api_requests: list[dict[str, list[str]]] = []

        class ApiHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parsed_url = urllib.parse.urlparse(self.path)
                api_requests.append(urllib.parse.parse_qs(parsed_url.query))
                body = json.dumps(
                    {
                        "diffid": "456",
                        "entries": [
                            {"path": "Documents/api-live-shadow.txt", "event": "modified"},
                            {"path": "private/api-live-shadow.txt", "event": "modified"},
                        ],
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                return

        api_server = ThreadingHTTPServer(("127.0.0.1", 0), ApiHandler)
        api_thread = threading.Thread(target=api_server.serve_forever, daemon=True)
        api_thread.start()
        try:
            api_live_env = dict(api_long_poll_run_env)
            api_live_env["PCLOUD_TOOLS_PCLOUD_API_BASE_URL"] = f"http://127.0.0.1:{api_server.server_port}"
            api_live_env["PCLOUD_TOOLS_PCLOUD_API_TOKEN"] = "shadow-token"
            api_live_env["PCLOUD_TOOLS_PCLOUD_API_TIMEOUT_SECONDS"] = "5"
            api_live_run = _check_json_command(
                checks,
                api_live_env,
                "diffd api-poll long-poll-run fake live API",
                (
                    "diffd",
                    "api-poll",
                    "long-poll-run",
                    "--report-path",
                    str(saved_shadow_report),
                    "--operator-reviewed-preview",
                    "--reviewer-approved-response-policy",
                    "--reviewer-approved-credential-policy",
                    "--reviewer-approved-process-policy",
                    "--live-api",
                    "--max-iterations",
                    "1",
                    "--execute",
                ),
            )
        finally:
            api_server.shutdown()
            api_server.server_close()
        live_remote_changes = json.loads(api_long_poll_remote_changes.read_text()) if api_long_poll_remote_changes.exists() else []
        live_has_record = any(
            isinstance(item, dict) and item.get("path") == "Documents/api-live-shadow.txt"
            for item in live_remote_changes
        )
        if (
            api_live_run.get("details", {}).get("live API requested") == "yes"
            and api_live_run.get("details", {}).get("download records appended") == 1
            and api_live_run.get("details", {}).get("written diffid") == "456"
            and api_requests
            and api_requests[0].get("auth") == ["shadow-token"]
            and "shadow-token" not in json.dumps(api_live_run, ensure_ascii=False)
            and live_has_record
            and api_long_poll_diffid.read_text().strip() == "456"
        ):
            checks.append(CheckResult("diffd api-poll long-poll-run fake live API state", "ok", "fake API diff recorded"))
        else:
            checks.append(CheckResult("diffd api-poll long-poll-run fake live API state", "error", "fake live API mismatch"))
        _check_json_command(
            checks,
            env,
            "diffd api-poll long-poll-run fake live API cleanup",
            ("diffd", "remote-change", "remove", "Documents/api-live-shadow.txt", "--execute"),
        )
        rclone_config = workspace / ".dev-state" / "rclone.conf"
        api_server = ThreadingHTTPServer(("127.0.0.1", 0), ApiHandler)
        rclone_config.write_text(
            "\n".join(
                [
                    "[pcloud]",
                    "type = pcloud",
                    f"hostname = http://127.0.0.1:{api_server.server_port}",
                    'token = {"access_token":"rclone-shadow-token","token_type":"bearer","expiry":"0001-01-01T00:00:00Z"}',
                    "",
                    "[pcloud-crypt]",
                    "type = crypt",
                    "remote = pcloud:crypt",
                    "password = should-not-be-read",
                    "password2 = should-not-be-read",
                    "",
                ]
            )
        )
        api_thread = threading.Thread(target=api_server.serve_forever, daemon=True)
        api_thread.start()
        try:
            rclone_live_env = dict(api_long_poll_run_env)
            rclone_live_env["RCLONE_CONFIG"] = str(rclone_config)
            rclone_live_env["PCLOUD_TOOLS_PCLOUD_API_TIMEOUT_SECONDS"] = "5"
            api_rclone_run = _check_json_command(
                checks,
                rclone_live_env,
                "diffd api-poll long-poll-run rclone config fake API",
                (
                    "diffd",
                    "api-poll",
                    "long-poll-run",
                    "--report-path",
                    str(saved_shadow_report),
                    "--operator-reviewed-preview",
                    "--reviewer-approved-response-policy",
                    "--reviewer-approved-credential-policy",
                    "--reviewer-approved-process-policy",
                    "--live-api",
                    "--max-iterations",
                    "1",
                    "--execute",
                ),
            )
        finally:
            api_server.shutdown()
            api_server.server_close()
        rclone_has_record = False
        if api_long_poll_remote_changes.exists():
            rclone_remote_changes = json.loads(api_long_poll_remote_changes.read_text())
            rclone_has_record = any(
                isinstance(item, dict) and item.get("path") == "Documents/api-live-shadow.txt"
                for item in rclone_remote_changes
            )
        if (
            api_rclone_run.get("details", {}).get("API credential source") == "rclone config"
            and api_rclone_run.get("details", {}).get("API auth parameter") == "access_token"
            and api_rclone_run.get("details", {}).get("download records appended") == 1
            and "rclone-shadow-token" not in json.dumps(api_rclone_run, ensure_ascii=False)
            and "should-not-be-read" not in json.dumps(api_rclone_run, ensure_ascii=False)
            and rclone_has_record
        ):
            checks.append(CheckResult("diffd api-poll long-poll-run rclone config fake API state", "ok", "rclone token diff recorded"))
        else:
            checks.append(CheckResult("diffd api-poll long-poll-run rclone config fake API state", "error", "rclone fake API mismatch"))
        _check_json_command(
            checks,
            env,
            "diffd api-poll long-poll-run rclone config fake API cleanup",
            ("diffd", "remote-change", "remove", "Documents/api-live-shadow.txt", "--execute"),
        )
        launchctl_bin_dir = workspace / ".dev-state" / "launchctl-bin"
        launchctl_bin_dir.mkdir(parents=True, exist_ok=True)
        fake_launchctl = launchctl_bin_dir / "launchctl"
        autosync_launchctl_log = workspace / ".dev-state" / "fake-launchctl.log"
        fake_launchctl.write_text(
            "#!/bin/sh\n"
            "if [ -n \"$PCLOUD_TOOLS_FAKE_LAUNCHCTL_LOG\" ]; then "
            "printf '%s\\n' \"$*\" >> \"$PCLOUD_TOOLS_FAKE_LAUNCHCTL_LOG\"; "
            "fi\n"
            "if [ \"$1\" = \"print\" ]; then exit 1; fi\n"
            "exit 0\n"
        )
        fake_launchctl.chmod(0o755)
        autosync_gate_env = dict(env)
        autosync_gate_env["PATH"] = f"{launchctl_bin_dir}:{env.get('PATH', '')}"
        autosync_gate_env["PCLOUD_TOOLS_FAKE_LAUNCHCTL_LOG"] = str(autosync_launchctl_log)
        autosync_gate = _check_json_command(
            checks,
            autosync_gate_env,
            "sync autosync launchd gate",
            (
                "sync",
                "autosync-gate",
                "--report-path",
                str(saved_shadow_report),
                "--operator-reviewed-preview",
                "--reviewer-approved-plist",
                "--reviewer-approved-launchctl-policy",
                "--reviewer-approved-rollback-policy",
            ),
        )
        if (
            autosync_gate.get("details", {}).get("launchd gate status") == "closed"
            and autosync_gate.get("details", {}).get("autosync changes can run") == "no"
            and autosync_gate.get("details", {}).get("state writes") == "none"
            and autosync_gate.get("details", {}).get("autosync approval status") == "complete-read-only"
            and autosync_gate.get("details", {}).get("launchctl availability") == "available"
        ):
            checks.append(CheckResult("sync autosync launchd gate closed", "ok", "launchd changes are gated"))
        else:
            checks.append(CheckResult("sync autosync launchd gate closed", "error", "autosync gate mismatch"))
        autosync_run_closed = _check_json_command(
            checks,
            autosync_gate_env,
            "sync autosync-run gate closed",
            (
                "sync",
                "autosync-run",
                "enable",
                "--report-path",
                str(saved_shadow_report),
                "--operator-reviewed-preview",
                "--reviewer-approved-plist",
                "--reviewer-approved-launchctl-policy",
                "--reviewer-approved-rollback-policy",
                "--execute",
            ),
            allowed_status={"error"},
        )
        autosync_run_state = workspace / ".dev-state" / "state" / "sync" / "autosync-launchd-last-run.json"
        closed_launchctl_log = autosync_launchctl_log.read_text() if autosync_launchctl_log.exists() else ""
        if (
            autosync_run_closed.get("details", {}).get("state writes") == "none"
            and autosync_run_closed.get("details", {}).get("autosync changes can run") == "no"
            and "enable gui/" not in closed_launchctl_log
            and not autosync_run_state.exists()
        ):
            checks.append(CheckResult("sync autosync-run closed no writes", "ok", "launchd gate refused"))
        else:
            checks.append(CheckResult("sync autosync-run closed no writes", "error", "closed launchd gate ran"))
        autosync_launchctl_log.write_text("")
        autosync_run_env = dict(autosync_gate_env)
        autosync_run_env["PCLOUD_TOOLS_AUTOSYNC_LAUNCHD_GATE"] = "operator-approved-autosync-launchd-v1"
        autosync_run = _check_json_command(
            checks,
            autosync_run_env,
            "sync autosync-run fake launchctl",
            (
                "sync",
                "autosync-run",
                "enable",
                "--report-path",
                str(saved_shadow_report),
                "--operator-reviewed-preview",
                "--reviewer-approved-plist",
                "--reviewer-approved-launchctl-policy",
                "--reviewer-approved-rollback-policy",
                "--execute",
            ),
        )
        open_launchctl_log = autosync_launchctl_log.read_text()
        if (
            autosync_run.get("details", {}).get("autosync changes can run") == "yes"
            and autosync_run.get("details", {}).get("state writes") == "autosync launchd run state"
            and "enable gui/" in open_launchctl_log
            and "bootstrap gui/" in open_launchctl_log
            and autosync_run_state.exists()
        ):
            checks.append(CheckResult("sync autosync-run fake launchctl state", "ok", "fake launchctl recorded"))
        else:
            checks.append(CheckResult("sync autosync-run fake launchctl state", "error", "autosync-run mismatch"))
        backup_dir = workspace / ".dev-state" / "cutover-backups" / "20260426-040551"
        backup_dir.mkdir(parents=True, exist_ok=True)
        (backup_dir / "pcloud-manager.current").write_text(
            '#!/bin/zsh\nPCLOUD_MANAGER_CONFIG="${HOME}/.config/pcloud-manager/config.zsh"\n'
        )
        (backup_dir / "shadow-validation.json").write_text(json.dumps({"status": "ok", "checks": []}))
        archive_gate = _check_json_command(
            checks,
            env,
            "old monolith archive gate",
            (
                "archive",
                "old-monolith-gate",
                "--backup-dir",
                str(backup_dir),
                "--operator-reviewed-current-wrapper",
                "--reviewer-approved-backup-source",
                "--reviewer-approved-rollback-policy",
                "--reviewer-approved-archive-target",
            ),
        )
        if (
            archive_gate.get("details", {}).get("archive gate status") == "closed"
            and archive_gate.get("details", {}).get("archive can run") == "no"
            and archive_gate.get("details", {}).get("state writes") == "none"
            and archive_gate.get("details", {}).get("archive approval status") == "complete-read-only"
        ):
            checks.append(CheckResult("old monolith archive gate closed", "ok", "old monolith archive is gated"))
        else:
            checks.append(CheckResult("old monolith archive gate closed", "error", "archive gate mismatch"))
        archive_run_closed = _check_json_command(
            checks,
            env,
            "old monolith archive-run gate closed",
            (
                "archive",
                "old-monolith-run",
                "--backup-dir",
                str(backup_dir),
                "--operator-reviewed-current-wrapper",
                "--reviewer-approved-backup-source",
                "--reviewer-approved-rollback-policy",
                "--reviewer-approved-archive-target",
                "--execute",
            ),
            allowed_status={"error"},
        )
        archive_target = workspace / ".dev-state" / "old-monolith-archive" / "20260426-040551"
        if (
            archive_run_closed.get("details", {}).get("state writes") == "none"
            and archive_run_closed.get("details", {}).get("archive can run") == "no"
            and not archive_target.exists()
        ):
            checks.append(CheckResult("old monolith archive-run closed no writes", "ok", "archive gate refused"))
        else:
            checks.append(CheckResult("old monolith archive-run closed no writes", "error", "closed archive gate wrote"))
        archive_run_env = dict(env)
        archive_run_env["PCLOUD_TOOLS_OLD_MONOLITH_ARCHIVE_GATE"] = "operator-approved-old-monolith-archive-v1"
        archive_run = _check_json_command(
            checks,
            archive_run_env,
            "old monolith archive-run copy",
            (
                "archive",
                "old-monolith-run",
                "--backup-dir",
                str(backup_dir),
                "--operator-reviewed-current-wrapper",
                "--reviewer-approved-backup-source",
                "--reviewer-approved-rollback-policy",
                "--reviewer-approved-archive-target",
                "--execute",
            ),
        )
        if (
            archive_run.get("details", {}).get("archive can run") == "yes"
            and archive_run.get("details", {}).get("state writes") == "archive target copy and manifest"
            and (archive_target / "pcloud-manager.current").exists()
            and (archive_target / "shadow-validation.json").exists()
            and (archive_target / "archive-manifest.json").exists()
            and (backup_dir / "pcloud-manager.current").exists()
        ):
            checks.append(CheckResult("old monolith archive-run copy state", "ok", str(archive_target)))
        else:
            checks.append(CheckResult("old monolith archive-run copy state", "error", "archive-run mismatch"))
        migration_bin_dir = workspace / ".dev-state" / "migration-bin"
        migration_bin_dir.mkdir(parents=True, exist_ok=True)
        fake_migration_rclone = migration_bin_dir / "rclone"
        migration_rclone_log = workspace / ".dev-state" / "fake-migration-rclone.log"
        fake_migration_rclone.write_text(
            "#!/bin/sh\n"
            "if [ -n \"$PCLOUD_TOOLS_FAKE_MIGRATION_RCLONE_LOG\" ]; then "
            "printf '%s\\n' \"$*\" >> \"$PCLOUD_TOOLS_FAKE_MIGRATION_RCLONE_LOG\"; "
            "fi\n"
            "exit 0\n"
        )
        fake_migration_rclone.chmod(0o755)
        migration_gate_env = dict(env)
        migration_gate_env["PATH"] = f"{migration_bin_dir}:{env.get('PATH', '')}"
        migration_gate_env["PCLOUD_TOOLS_FAKE_MIGRATION_RCLONE_LOG"] = str(migration_rclone_log)
        (workspace / "bisync_status.log").write_text("2026-05-04 12:00:00 SUCCESS mode=autosync\n")
        migration_gate = _check_json_command(
            checks,
            migration_gate_env,
            "sync migration validation gate",
            (
                "sync",
                "migration-gate",
                "--report-path",
                str(saved_shadow_report),
                "--operator-reviewed-status",
                "--reviewer-approved-scope",
                "--reviewer-approved-rollback-policy",
                "--reviewer-approved-stop-conditions",
            ),
        )
        if (
            migration_gate.get("details", {}).get("migration gate status") == "closed"
            and migration_gate.get("details", {}).get("sync/resync can run") == "no"
            and migration_gate.get("details", {}).get("state writes") == "none"
            and migration_gate.get("details", {}).get("migration approval status") == "complete-read-only"
            and migration_gate.get("details", {}).get("sync state") == "synced"
        ):
            checks.append(CheckResult("sync migration validation gate closed", "ok", "sync/resync validation is gated"))
        else:
            checks.append(CheckResult("sync migration validation gate closed", "error", "migration gate mismatch"))
        (workspace / "bisync_status.log").write_text("2026-04-24 15:43:23 ERROR mode=resync\n")
        saved_sync_status_report = workspace / "saved-sync-status.json"
        saved_sync_status_report.write_text(
            json.dumps(
                {
                    "command": "sync status",
                    "status": "ok",
                    "details": {
                        "sync state": "synced",
                        "last result": "2026-05-04 12:00:00 SUCCESS mode=autosync",
                        "last error": "2026-04-30 10:54:28 historical failure",
                        "last error status": "historical",
                        "sync lock status": "missing",
                        "sync lock active": "no",
                        "sync lock pid": "-",
                        "scope status": "loaded",
                        "scope entries": 4,
                        "last resync scope": "allowlist",
                        "allowlist": str(workspace / ".pcloud-sync-allowlist"),
                        "autosync state": "active",
                        "autosync runs": "7",
                    },
                }
            )
        )
        saved_status_migration_gate = _check_json_command(
            checks,
            migration_gate_env,
            "sync migration saved status gate",
            (
                "sync",
                "migration-gate",
                "--report-path",
                str(saved_shadow_report),
                "--sync-status-report-path",
                str(saved_sync_status_report),
                "--operator-reviewed-status",
                "--reviewer-approved-scope",
                "--reviewer-approved-rollback-policy",
                "--reviewer-approved-stop-conditions",
            ),
        )
        if (
            saved_status_migration_gate.get("details", {}).get("sync status source") == "saved sync status report"
            and saved_status_migration_gate.get("details", {}).get("migration approval status")
            == "complete-read-only"
            and saved_status_migration_gate.get("details", {}).get("sync state") == "synced"
        ):
            checks.append(CheckResult("sync migration saved status accepted", "ok", str(saved_sync_status_report)))
        else:
            checks.append(CheckResult("sync migration saved status accepted", "error", "saved status mismatch"))
        migration_run_closed = _check_json_command(
            checks,
            migration_gate_env,
            "sync migration-run gate closed",
            (
                "sync",
                "migration-run",
                "normal",
                "--report-path",
                str(saved_shadow_report),
                "--sync-status-report-path",
                str(saved_sync_status_report),
                "--operator-reviewed-status",
                "--reviewer-approved-scope",
                "--reviewer-approved-rollback-policy",
                "--reviewer-approved-stop-conditions",
                "--execute",
            ),
            allowed_status={"error"},
        )
        migration_run_state = workspace / ".dev-state" / "state" / "sync" / "migration-last-run.json"
        closed_migration_rclone_log = migration_rclone_log.read_text() if migration_rclone_log.exists() else ""
        if (
            migration_run_closed.get("details", {}).get("state writes") == "none"
            and migration_run_closed.get("details", {}).get("sync/resync can run") == "no"
            and "bisync" not in closed_migration_rclone_log
            and not migration_run_state.exists()
        ):
            checks.append(CheckResult("sync migration-run closed no writes", "ok", "sync migration gate refused"))
        else:
            checks.append(CheckResult("sync migration-run closed no writes", "error", "closed migration gate ran"))
        migration_rclone_log.write_text("")
        migration_run_env = dict(migration_gate_env)
        migration_run_env["PCLOUD_TOOLS_SYNC_MIGRATION_GATE"] = "operator-approved-sync-migration-v1"
        rclone_lock_name = f"local_{str(workspace).replace('/', '_')}..pcloud_core.lck"
        rclone_lock_file = root / "cache" / "rclone" / "bisync" / rclone_lock_name
        rclone_lock_file.parent.mkdir(parents=True, exist_ok=True)
        rclone_lock_file.write_text(
            json.dumps(
                {
                    "Session": str(rclone_lock_file.with_suffix("")),
                    "PID": "999999",
                    "TimeRenewed": "2026-04-24T15:42:31+09:00",
                    "TimeExpires": "2226-03-07T15:42:31+09:00",
                }
            )
        )
        migration_run_lock = _check_json_command(
            checks,
            migration_run_env,
            "sync migration-run rclone lock blocked",
            (
                "sync",
                "migration-run",
                "normal",
                "--report-path",
                str(saved_shadow_report),
                "--sync-status-report-path",
                str(saved_sync_status_report),
                "--operator-reviewed-status",
                "--reviewer-approved-scope",
                "--reviewer-approved-rollback-policy",
                "--reviewer-approved-stop-conditions",
                "--execute",
            ),
            allowed_status={"error"},
        )
        if (
            migration_run_lock.get("details", {}).get("rclone bisync lock status") == "present"
            and migration_run_lock.get("details", {}).get("state writes") == "none"
            and "bisync" not in migration_rclone_log.read_text()
        ):
            checks.append(CheckResult("sync migration-run rclone lock guard", "ok", "rclone lock blocked sync"))
        else:
            checks.append(CheckResult("sync migration-run rclone lock guard", "error", "rclone lock did not block sync"))
        rclone_lock_file.unlink()
        migration_run = _check_json_command(
            checks,
            migration_run_env,
            "sync migration-run fake rclone",
            (
                "sync",
                "migration-run",
                "normal",
                "--report-path",
                str(saved_shadow_report),
                "--sync-status-report-path",
                str(saved_sync_status_report),
                "--operator-reviewed-status",
                "--reviewer-approved-scope",
                "--reviewer-approved-rollback-policy",
                "--reviewer-approved-stop-conditions",
                "--execute",
            ),
        )
        open_migration_rclone_log = migration_rclone_log.read_text()
        if (
            migration_run.get("details", {}).get("sync/resync can run") == "yes"
            and migration_run.get("details", {}).get("state writes") == "sync logs, lock, status, and migration run state"
            and migration_run.get("details", {}).get("exit code") == 0
            and "bisync" in open_migration_rclone_log
            and "SUCCESS mode=normal" in (workspace / "bisync_status.log").read_text()
            and migration_run_state.exists()
        ):
            checks.append(CheckResult("sync migration-run fake rclone state", "ok", "fake sync recorded"))
        else:
            checks.append(CheckResult("sync migration-run fake rclone state", "error", "migration-run mismatch"))
        pushd_transfer = _check_json_command(
            checks,
            env,
            "pushd transfer preview",
            ("pushd", "transfer", "preview"),
        )
        diffd_transfer = _check_json_command(
            checks,
            env,
            "diffd transfer preview",
            ("diffd", "transfer", "preview"),
        )
        if pushd_transfer.get("details", {}).get("gate status") == "closed":
            checks.append(CheckResult("pushd transfer gate closed", "ok", "upload commands are preview-only"))
        else:
            checks.append(CheckResult("pushd transfer gate closed", "error", "missing closed gate status"))
        if diffd_transfer.get("details", {}).get("gate status") == "closed":
            checks.append(CheckResult("diffd transfer gate closed", "ok", "download commands are preview-only"))
        else:
            checks.append(CheckResult("diffd transfer gate closed", "error", "missing closed gate status"))
        pushd_transfer_matrix = _check_json_command(
            checks,
            env,
            "pushd transfer validation matrix",
            ("pushd", "transfer", "validation-matrix"),
        )
        diffd_transfer_matrix = _check_json_command(
            checks,
            env,
            "diffd transfer validation matrix",
            ("diffd", "transfer", "validation-matrix"),
        )
        pushd_matrix_details = pushd_transfer_matrix.get("details", {})
        diffd_matrix_details = diffd_transfer_matrix.get("details", {})
        if (
            pushd_matrix_details.get("state writes") == "none"
            and pushd_matrix_details.get("real execution can run") == "no"
            and pushd_matrix_details.get("case count") == 5
        ):
            checks.append(CheckResult("pushd transfer validation matrix read-only", "ok", "5 upload cases"))
        else:
            checks.append(CheckResult("pushd transfer validation matrix read-only", "error", "matrix mismatch"))
        diffd_case_ids = {
            str(case.get("id", ""))
            for case in diffd_matrix_details.get("cases", [])
            if isinstance(case, dict)
        }
        if (
            diffd_matrix_details.get("state writes") == "none"
            and diffd_matrix_details.get("real execution can run") == "no"
            and diffd_matrix_details.get("case count") == 6
            and "remote-only-download" in diffd_case_ids
        ):
            checks.append(CheckResult("diffd transfer validation matrix read-only", "ok", "6 download cases"))
        else:
            checks.append(CheckResult("diffd transfer validation matrix read-only", "error", "matrix mismatch"))
        pushd_transfer_check = _check_json_command(
            checks,
            env,
            "pushd transfer gate checklist",
            ("pushd", "transfer", "check"),
        )
        diffd_transfer_check = _check_json_command(
            checks,
            env,
            "diffd transfer gate checklist",
            ("diffd", "transfer", "check"),
        )
        if (
            pushd_transfer_check.get("details", {}).get("real transfer gate status") == "closed"
            and pushd_transfer_check.get("details", {}).get("state writes") == "none"
            and pushd_transfer_check.get("details", {}).get("real execution can run") == "no"
        ):
            checks.append(CheckResult("pushd transfer checklist read-only", "ok", "real gate closed"))
        else:
            checks.append(CheckResult("pushd transfer checklist read-only", "error", "checklist is not read-only"))
        if pushd_transfer_check.get("details", {}).get("first planned transfer status") == "ready":
            checks.append(CheckResult("pushd transfer checklist sample-ready", "ok", "first planned transfer ready"))
        else:
            checks.append(CheckResult("pushd transfer checklist sample-ready", "error", "first planned transfer missing"))
        if (
            diffd_transfer_check.get("details", {}).get("real transfer gate status") == "closed"
            and diffd_transfer_check.get("details", {}).get("state writes") == "none"
            and diffd_transfer_check.get("details", {}).get("real execution can run") == "no"
        ):
            checks.append(CheckResult("diffd transfer checklist read-only", "ok", "real gate closed"))
        else:
            checks.append(CheckResult("diffd transfer checklist read-only", "error", "checklist is not read-only"))
        if diffd_transfer_check.get("details", {}).get("first planned transfer status") == "ready":
            checks.append(CheckResult("diffd transfer checklist sample-ready", "ok", "first planned transfer ready"))
        else:
            checks.append(CheckResult("diffd transfer checklist sample-ready", "error", "first planned transfer missing"))
        pushd_confirmed_check = _check_json_command(
            checks,
            env,
            "pushd transfer confirmed checklist",
            (
                "pushd",
                "transfer",
                "check",
                "--report-path",
                str(saved_shadow_report),
                "--confirm-path",
                "Documents/shadow-upload.pdf",
                "--confirm-direction",
                "upload",
                "--consume-policy",
                "remove-on-success-retain-on-failure",
                "--timeout-policy",
                "reuse-fake-rclone-cleanup",
                "--final-review",
            ),
        )
        diffd_confirmed_check = _check_json_command(
            checks,
            env,
            "diffd transfer confirmed checklist",
            (
                "diffd",
                "transfer",
                "check",
                "--report-path",
                str(saved_shadow_report),
                "--confirm-path",
                "Documents/shadow-download.pdf",
                "--confirm-direction",
                "download",
                "--consume-policy",
                "remove-on-success-retain-on-failure",
                "--timeout-policy",
                "reuse-fake-rclone-cleanup",
                "--final-review",
            ),
        )
        if (
            pushd_confirmed_check.get("details", {}).get("operator target confirmation status") == "ok"
            and pushd_confirmed_check.get("details", {}).get("consume policy status") == "ok"
            and pushd_confirmed_check.get("details", {}).get("timeout policy status") == "ok"
            and pushd_confirmed_check.get("details", {}).get("final review status") == "ready"
            and pushd_confirmed_check.get("details", {}).get("dry-run display status") == "ready"
            and pushd_confirmed_check.get("details", {}).get("real transfer gate status") == "closed"
        ):
            checks.append(CheckResult("pushd transfer final review ready", "ok", "operator review accepted"))
        else:
            checks.append(CheckResult("pushd transfer final review ready", "error", "confirmation mismatch"))
        if (
            diffd_confirmed_check.get("details", {}).get("operator target confirmation status") == "ok"
            and diffd_confirmed_check.get("details", {}).get("consume policy status") == "ok"
            and diffd_confirmed_check.get("details", {}).get("timeout policy status") == "ok"
            and diffd_confirmed_check.get("details", {}).get("final review status") == "ready"
            and diffd_confirmed_check.get("details", {}).get("dry-run display status") == "ready"
            and diffd_confirmed_check.get("details", {}).get("real transfer gate status") == "closed"
        ):
            checks.append(CheckResult("diffd transfer final review ready", "ok", "operator review accepted"))
        else:
            checks.append(CheckResult("diffd transfer final review ready", "error", "confirmation mismatch"))
        pushd_real_gate = _check_json_command(
            checks,
            env,
            "pushd transfer real-gate",
            (
                "pushd",
                "transfer",
                "real-gate",
                "--report-path",
                str(saved_shadow_report),
                "--confirm-path",
                "Documents/shadow-upload.pdf",
                "--confirm-direction",
                "upload",
                "--consume-policy",
                "remove-on-success-retain-on-failure",
                "--timeout-policy",
                "reuse-fake-rclone-cleanup",
                "--operator-reviewed-dry-run",
                "--reviewer-approved-real-command",
                "--reviewer-approved-consume-policy",
            ),
        )
        diffd_real_gate = _check_json_command(
            checks,
            env,
            "diffd transfer real-gate",
            (
                "diffd",
                "transfer",
                "real-gate",
                "--report-path",
                str(saved_shadow_report),
                "--confirm-path",
                "Documents/shadow-download.pdf",
                "--confirm-direction",
                "download",
                "--consume-policy",
                "remove-on-success-retain-on-failure",
                "--timeout-policy",
                "reuse-fake-rclone-cleanup",
                "--operator-reviewed-dry-run",
                "--reviewer-approved-real-command",
                "--reviewer-approved-consume-policy",
            ),
        )
        if (
            str(pushd_real_gate.get("details", {}).get("real transfer execution gate status", "")).startswith(
                "closed: requires PCLOUD_TOOLS_REAL_TRANSFER_EXECUTION_GATE="
            )
            and pushd_real_gate.get("details", {}).get("fake-rclone gate reuse") == "forbidden"
            and pushd_real_gate.get("details", {}).get("separate real gate approval status")
            == "complete-read-only"
            and pushd_real_gate.get("details", {}).get("future real-run policy status")
            == "documented-read-only"
            and pushd_real_gate.get("details", {}).get("future real-run policy state writes") == "none"
            and pushd_real_gate.get("details", {}).get("operator verification required") == "not-now"
            and pushd_real_gate.get("details", {}).get("human gate status")
            == "required-before-actual-transfer"
            and pushd_real_gate.get("details", {}).get("real execution readiness") == "blocked-execution-gate"
            and pushd_real_gate.get("details", {}).get("real execution can run") == "no"
        ):
            checks.append(CheckResult("pushd transfer real-gate closed", "ok", "real execution unavailable"))
        else:
            checks.append(CheckResult("pushd transfer real-gate closed", "error", "real gate unexpectedly open"))
        if (
            str(diffd_real_gate.get("details", {}).get("real transfer execution gate status", "")).startswith(
                "closed: requires PCLOUD_TOOLS_REAL_TRANSFER_EXECUTION_GATE="
            )
            and diffd_real_gate.get("details", {}).get("fake-rclone gate reuse") == "forbidden"
            and diffd_real_gate.get("details", {}).get("separate real gate approval status")
            == "complete-read-only"
            and diffd_real_gate.get("details", {}).get("future real-run policy status")
            == "documented-read-only"
            and diffd_real_gate.get("details", {}).get("future real-run policy state writes") == "none"
            and diffd_real_gate.get("details", {}).get("operator verification required") == "not-now"
            and diffd_real_gate.get("details", {}).get("human gate status")
            == "required-before-actual-transfer"
            and diffd_real_gate.get("details", {}).get("real execution readiness") == "blocked-execution-gate"
            and diffd_real_gate.get("details", {}).get("real execution can run") == "no"
        ):
            checks.append(CheckResult("diffd transfer real-gate closed", "ok", "real execution unavailable"))
        else:
            checks.append(CheckResult("diffd transfer real-gate closed", "error", "real gate unexpectedly open"))
        pushd_automation_gate = _check_json_command(
            checks,
            env | {"PCLOUD_TOOLS_REAL_TRANSFER_AUTOMATION_GATE": "operator-approved-real-transfer-automation-v1"},
            "pushd transfer automation-gate",
            (
                "pushd",
                "transfer",
                "automation-gate",
                "--report-path",
                str(saved_shadow_report),
                "--confirm-path",
                "Documents/shadow-upload.pdf",
                "--confirm-direction",
                "upload",
                "--consume-policy",
                "remove-on-success-retain-on-failure",
                "--timeout-policy",
                "reuse-fake-rclone-cleanup",
                "--operator-reviewed-dry-run",
                "--reviewer-approved-real-command",
                "--reviewer-approved-consume-policy",
                "--operator-reviewed-real-transfer-gate",
                "--reviewer-approved-automation-command",
                "--reviewer-approved-launchd-policy",
                "--reviewer-approved-rollback-policy",
            ),
        )
        diffd_automation_gate = _check_json_command(
            checks,
            env,
            "diffd transfer automation-gate",
            (
                "diffd",
                "transfer",
                "automation-gate",
                "--report-path",
                str(saved_shadow_report),
                "--confirm-path",
                "Documents/shadow-download.pdf",
                "--confirm-direction",
                "download",
                "--consume-policy",
                "remove-on-success-retain-on-failure",
                "--timeout-policy",
                "reuse-fake-rclone-cleanup",
                "--operator-reviewed-dry-run",
                "--reviewer-approved-real-command",
                "--reviewer-approved-consume-policy",
            ),
        )
        if (
            pushd_automation_gate.get("details", {}).get("automation gate status") == "closed"
            and pushd_automation_gate.get("details", {}).get("automation command status") == "implemented-gated"
            and pushd_automation_gate.get("details", {}).get("automation gate env provided") == "yes"
            and pushd_automation_gate.get("details", {}).get("automation gate env honored") == "no"
            and pushd_automation_gate.get("details", {}).get("public plist writes") == "no"
            and pushd_automation_gate.get("details", {}).get("automatic real transfer execution") == "no"
        ):
            checks.append(CheckResult("pushd transfer automation gate closed", "ok", "automation gated"))
        else:
            checks.append(CheckResult("pushd transfer automation gate closed", "error", "automation gate mismatch"))
        if (
            diffd_automation_gate.get("details", {}).get("automation gate status") == "closed"
            and diffd_automation_gate.get("details", {}).get("automation command status") == "implemented-gated"
            and diffd_automation_gate.get("details", {}).get("automation gate env provided") == "no"
            and diffd_automation_gate.get("details", {}).get("public plist writes") == "no"
            and diffd_automation_gate.get("details", {}).get("automatic real transfer execution") == "no"
        ):
            checks.append(CheckResult("diffd transfer automation gate closed", "ok", "automation gated"))
        else:
            checks.append(CheckResult("diffd transfer automation gate closed", "error", "automation gate mismatch"))
        automation_run_env = env | {
            "PCLOUD_TOOLS_REAL_TRANSFER_EXECUTION_GATE": "operator-approved-real-transfer-v1",
            "PCLOUD_TOOLS_REAL_TRANSFER_AUTOMATION_GATE": "operator-approved-real-transfer-automation-v1",
        }
        pushd_automation_run = _check_json_command(
            checks,
            automation_run_env,
            "pushd transfer automation-run gate refusal",
            ("pushd", "transfer", "automation-run", "--execute", "--consume-on-success"),
            allowed_status={"error"},
        )
        diffd_automation_run = _check_json_command(
            checks,
            automation_run_env,
            "diffd transfer automation-run gate refusal",
            ("diffd", "transfer", "automation-run", "--execute", "--consume-on-success"),
            allowed_status={"error"},
        )
        if (
            pushd_automation_run.get("details", {}).get("automation command status") == "implemented-gated"
            and pushd_automation_run.get("details", {}).get("automation can run") == "no"
            and pushd_automation_run.get("details", {}).get("real transfer gate env honored") == "yes"
            and pushd_automation_run.get("details", {}).get("automation gate env honored") == "yes"
            and pushd_automation_run.get("details", {}).get("automation run gate env honored") == "no"
            and pushd_automation_run.get("details", {}).get("state writes") == "none"
            and pushd_automation_run.get("details", {}).get("automatic real transfer execution") == "no"
        ):
            checks.append(CheckResult("pushd transfer automation-run no-op", "ok", "automation-run refused"))
        else:
            checks.append(CheckResult("pushd transfer automation-run no-op", "error", "automation-run mismatch"))
        if (
            diffd_automation_run.get("details", {}).get("automation command status") == "implemented-gated"
            and diffd_automation_run.get("details", {}).get("automation can run") == "no"
            and diffd_automation_run.get("details", {}).get("state writes") == "none"
            and diffd_automation_run.get("details", {}).get("automatic queue/change consumption") == "no"
        ):
            checks.append(CheckResult("diffd transfer automation-run no-op", "ok", "automation-run refused"))
        else:
            checks.append(CheckResult("diffd transfer automation-run no-op", "error", "automation-run mismatch"))
        pushd_launchd_automation_plist = _check_json_command(
            checks,
            env | {"PCLOUD_TOOLS_REAL_TRANSFER_AUTOMATION_GATE": "operator-approved-real-transfer-automation-v1"},
            "pushd launchd automation plist preview",
            (
                "pushd",
                "launchd",
                "automation-plist",
                "--report-path",
                str(saved_shadow_report),
                "--confirm-path",
                "Documents/shadow-upload.pdf",
                "--confirm-direction",
                "upload",
                "--consume-policy",
                "remove-on-success-retain-on-failure",
                "--timeout-policy",
                "reuse-fake-rclone-cleanup",
                "--operator-reviewed-dry-run",
                "--reviewer-approved-real-command",
                "--reviewer-approved-consume-policy",
                "--operator-reviewed-real-transfer-gate",
                "--reviewer-approved-automation-command",
                "--reviewer-approved-launchd-policy",
                "--reviewer-approved-rollback-policy",
            ),
        )
        diffd_launchd_automation_reload = _check_json_command(
            checks,
            env,
            "diffd launchd automation reload preview",
            (
                "diffd",
                "launchd",
                "automation-reload",
                "--report-path",
                str(saved_shadow_report),
                "--confirm-path",
                "Documents/shadow-download.pdf",
                "--confirm-direction",
                "download",
                "--consume-policy",
                "remove-on-success-retain-on-failure",
                "--timeout-policy",
                "reuse-fake-rclone-cleanup",
                "--operator-reviewed-dry-run",
                "--reviewer-approved-real-command",
                "--reviewer-approved-consume-policy",
                "--operator-reviewed-real-transfer-gate",
                "--reviewer-approved-automation-command",
                "--reviewer-approved-launchd-policy",
                "--reviewer-approved-rollback-policy",
                "--operator-reviewed-automation-plist",
                "--reviewer-approved-bootout-bootstrap",
            ),
        )
        if (
            pushd_launchd_automation_plist.get("details", {}).get("automation command status") == "implemented-gated"
            and pushd_launchd_automation_plist.get("details", {}).get("public plist writes") == "no"
            and pushd_launchd_automation_plist.get("details", {}).get("launchctl execution") == "no"
            and pushd_launchd_automation_plist.get("details", {}).get("automatic real transfer execution") == "no"
        ):
            checks.append(CheckResult("pushd launchd automation plist preview-only", "ok", "public plist blocked"))
        else:
            checks.append(CheckResult("pushd launchd automation plist preview-only", "error", "automation plist mismatch"))
        if (
            diffd_launchd_automation_reload.get("details", {}).get("automation command status") == "implemented-gated"
            and diffd_launchd_automation_reload.get("details", {}).get("launchd can reload") == "no"
            and diffd_launchd_automation_reload.get("details", {}).get("launchctl execution") == "no"
            and diffd_launchd_automation_reload.get("details", {}).get("automatic real transfer execution") == "no"
        ):
            checks.append(CheckResult("diffd launchd automation reload preview-only", "ok", "launchctl blocked"))
        else:
            checks.append(CheckResult("diffd launchd automation reload preview-only", "error", "automation reload mismatch"))
        real_run_env = env | {
            "PCLOUD_TOOLS_REAL_TRANSFER_EXECUTION_GATE": "shadow-attempt",
            "PCLOUD_TOOLS_TRANSFER_EXECUTION_GATE": "dev-fake-rclone",
        }
        pushd_real_run = _check_json_command(
            checks,
            real_run_env,
            "pushd transfer real-run refusal",
            ("pushd", "transfer", "real-run", "--execute"),
            allowed_status={"error"},
        )
        diffd_real_run = _check_json_command(
            checks,
            real_run_env,
            "diffd transfer real-run refusal",
            ("diffd", "transfer", "real-run", "--execute"),
            allowed_status={"error"},
        )
        if (
            str(pushd_real_run.get("details", {}).get("real transfer execution gate status", "")).startswith(
                "closed: requires PCLOUD_TOOLS_REAL_TRANSFER_EXECUTION_GATE="
            )
            and pushd_real_run.get("details", {}).get("state writes") == "none"
            and pushd_real_run.get("details", {}).get("real gate env provided") == "yes"
            and pushd_real_run.get("details", {}).get("real gate env honored") == "no"
            and pushd_real_run.get("details", {}).get("fake-rclone gate env honored") == "no"
            and pushd_real_run.get("details", {}).get("real execution can run") == "no"
        ):
            checks.append(CheckResult("pushd transfer real-run blocked", "ok", "real execution refused"))
        else:
            checks.append(CheckResult("pushd transfer real-run blocked", "error", "real-run not refused"))
        if (
            str(diffd_real_run.get("details", {}).get("real transfer execution gate status", "")).startswith(
                "closed: requires PCLOUD_TOOLS_REAL_TRANSFER_EXECUTION_GATE="
            )
            and diffd_real_run.get("details", {}).get("state writes") == "none"
            and diffd_real_run.get("details", {}).get("real gate env provided") == "yes"
            and diffd_real_run.get("details", {}).get("real gate env honored") == "no"
            and diffd_real_run.get("details", {}).get("fake-rclone gate env honored") == "no"
            and diffd_real_run.get("details", {}).get("real execution can run") == "no"
        ):
            checks.append(CheckResult("diffd transfer real-run blocked", "ok", "real execution refused"))
        else:
            checks.append(CheckResult("diffd transfer real-run blocked", "error", "real-run not refused"))

        real_bin_dir = workspace / ".dev-state" / "real-bin"
        real_bin_dir.mkdir(parents=True, exist_ok=True)
        real_log = workspace / ".dev-state" / "real-rclone-stub.log"
        real_rclone = real_bin_dir / "rclone"
        real_rclone.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$*\" >> \"$REAL_RCLONE_STUB_LOG\"\n"
            "if [ \"$1\" = \"copyto\" ]; then\n"
            "  dest=\"$3\"\n"
            "  case \"$dest\" in\n"
            "    pcloud:*) ;;\n"
            "    *) mkdir -p \"$(dirname \"$dest\")\" && printf 'real stub download\\n' > \"$dest\" ;;\n"
            "  esac\n"
            "fi\n"
        )
        real_rclone.chmod(0o755)
        real_env = dict(env)
        real_env.update(
            {
                "PCLOUD_TOOLS_REAL_TRANSFER_EXECUTION_GATE": "operator-approved-real-transfer-v1",
                "PCLOUD_TOOLS_RCLONE_BIN": str(real_rclone),
                "REAL_RCLONE_STUB_LOG": str(real_log),
            }
        )
        pushd_real_run_stub = _check_json_command(
            checks,
            real_env,
            "pushd transfer real-run stub",
            (
                "pushd",
                "transfer",
                "real-run",
                "--report-path",
                str(saved_shadow_report),
                "--confirm-path",
                "Documents/shadow-upload.pdf",
                "--confirm-direction",
                "upload",
                "--consume-policy",
                "remove-on-success-retain-on-failure",
                "--timeout-policy",
                "reuse-fake-rclone-cleanup",
                "--operator-reviewed-dry-run",
                "--reviewer-approved-real-command",
                "--reviewer-approved-consume-policy",
                "--execute",
            ),
        )
        diffd_real_run_stub = _check_json_command(
            checks,
            real_env,
            "diffd transfer real-run stub",
            (
                "diffd",
                "transfer",
                "real-run",
                "--report-path",
                str(saved_shadow_report),
                "--confirm-path",
                "Documents/shadow-download.pdf",
                "--confirm-direction",
                "download",
                "--consume-policy",
                "remove-on-success-retain-on-failure",
                "--timeout-policy",
                "reuse-fake-rclone-cleanup",
                "--operator-reviewed-dry-run",
                "--reviewer-approved-real-command",
                "--reviewer-approved-consume-policy",
                "--execute",
            ),
        )
        real_log_lines = real_log.read_text().splitlines() if real_log.exists() else []
        if (
            pushd_real_run_stub.get("details", {}).get("real transfer execution gate status")
            == "open: operator-approved-real-transfer-v1"
            and pushd_real_run_stub.get("details", {}).get("real execution readiness") == "executed"
            and pushd_real_run_stub.get("details", {}).get("real gate env honored") == "yes"
            and len(real_log_lines) >= 1
        ):
            checks.append(CheckResult("pushd transfer real-run guarded stub", "ok", "stub rclone executed"))
        else:
            checks.append(CheckResult("pushd transfer real-run guarded stub", "error", "real-run stub mismatch"))
        if (
            diffd_real_run_stub.get("details", {}).get("real transfer execution gate status")
            == "open: operator-approved-real-transfer-v1"
            and diffd_real_run_stub.get("details", {}).get("real execution readiness") == "executed"
            and diffd_real_run_stub.get("details", {}).get("real gate env honored") == "yes"
            and len(real_log_lines) >= 2
        ):
            checks.append(CheckResult("diffd transfer real-run guarded stub", "ok", "stub rclone executed"))
        else:
            checks.append(CheckResult("diffd transfer real-run guarded stub", "error", "real-run stub mismatch"))
        _check_json_command(
            checks,
            env,
            "pushd queue restore after real-run stub consume",
            ("pushd", "queue", "add", "Documents/shadow-upload.pdf", "--execute"),
        )
        _check_json_command(
            checks,
            env,
            "diffd remote-change restore after real-run stub consume",
            ("diffd", "remote-change", "add", "Documents/shadow-download.pdf", "--execute"),
        )

        fake_bin_dir = workspace / ".dev-state" / "bin"
        fake_bin_dir.mkdir(parents=True, exist_ok=True)
        fake_log = workspace / ".dev-state" / "fake-rclone.log"
        fake_rclone = fake_bin_dir / "fake-rclone"
        fake_rclone.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$*\" >> \"$FAKE_RCLONE_LOG\"\n"
            "if [ \"$1\" = \"copyto\" ]; then\n"
            "  dest=\"$3\"\n"
            "  case \"$dest\" in\n"
            "    pcloud:*) ;;\n"
            "    *) mkdir -p \"$(dirname \"$dest\")\" && printf 'fake download\\n' > \"$dest\" ;;\n"
            "  esac\n"
            "fi\n"
        )
        fake_rclone.chmod(0o755)
        fake_env = dict(env)
        fake_env.update(
            {
                "PCLOUD_TOOLS_TRANSFER_EXECUTION_GATE": "dev-fake-rclone",
                "PCLOUD_TOOLS_RCLONE_BIN": str(fake_rclone),
                "FAKE_RCLONE_LOG": str(fake_log),
            }
        )
        pushd_executor_plist = _check_json_command(
            checks,
            fake_env,
            "pushd launchd executor plist write",
            ("pushd", "launchd", "executor-plist", "--execute"),
        )
        diffd_executor_plist = _check_json_command(
            checks,
            fake_env,
            "diffd launchd executor plist write",
            ("diffd", "launchd", "executor-plist", "--start-interval-seconds", "30", "--execute"),
        )
        pushd_executor_plist_path = workspace / ".dev-state" / "launchd" / "com.example.pcloud-pushd-executor.dev.plist"
        diffd_executor_plist_path = workspace / ".dev-state" / "launchd" / "com.example.pcloud-diffd-executor.dev.plist"
        pushd_executor_payload = (
            plistlib.loads(pushd_executor_plist_path.read_bytes()) if pushd_executor_plist_path.exists() else {}
        )
        diffd_executor_payload = (
            plistlib.loads(diffd_executor_plist_path.read_bytes()) if diffd_executor_plist_path.exists() else {}
        )
        if (
            pushd_executor_plist.get("details", {}).get("state writes") == "launchd executor plist only"
            and pushd_executor_plist.get("details", {}).get("launchctl execution") == "no"
            and pushd_executor_payload.get("ProgramArguments", [])[1:4] == ["pushd", "transfer", "executor-run"]
            and pushd_executor_payload.get("StartInterval") == 60
            and pushd_executor_payload.get("EnvironmentVariables", {}).get("PCLOUD_TOOLS_RCLONE_BIN") == str(fake_rclone)
        ):
            checks.append(CheckResult("pushd launchd executor plist dev-only", "ok", "fake executor plist written"))
        else:
            checks.append(CheckResult("pushd launchd executor plist dev-only", "error", "pushd executor plist mismatch"))
        if (
            diffd_executor_plist.get("details", {}).get("state writes") == "launchd executor plist only"
            and diffd_executor_plist.get("details", {}).get("launchctl execution") == "no"
            and diffd_executor_payload.get("ProgramArguments", [])[1:4] == ["diffd", "transfer", "executor-run"]
            and diffd_executor_payload.get("StartInterval") == 30
            and diffd_executor_payload.get("EnvironmentVariables", {}).get("PCLOUD_TOOLS_RCLONE_BIN") == str(fake_rclone)
        ):
            checks.append(CheckResult("diffd launchd executor plist dev-only", "ok", "fake executor plist written"))
        else:
            checks.append(CheckResult("diffd launchd executor plist dev-only", "error", "diffd executor plist mismatch"))
        pushd_transfer_run = _check_json_command(
            checks,
            fake_env,
            "pushd transfer fake-rclone run",
            ("pushd", "transfer", "run", "--execute"),
        )
        diffd_transfer_run = _check_json_command(
            checks,
            fake_env,
            "diffd transfer fake-rclone run",
            ("diffd", "transfer", "run", "--execute"),
        )
        if (
            pushd_transfer_run.get("details", {}).get("execution gate") == "open: dev-fake-rclone"
            and pushd_transfer_run.get("details", {}).get("real execution can run") == "no"
        ):
            checks.append(CheckResult("pushd transfer fake gate", "ok", "fake-rclone gate opened"))
        else:
            checks.append(CheckResult("pushd transfer fake gate", "error", "fake-rclone gate did not open"))
        if (
            diffd_transfer_run.get("details", {}).get("execution gate") == "open: dev-fake-rclone"
            and diffd_transfer_run.get("details", {}).get("real execution can run") == "no"
        ):
            checks.append(CheckResult("diffd transfer fake gate", "ok", "fake-rclone gate opened"))
        else:
            checks.append(CheckResult("diffd transfer fake gate", "error", "fake-rclone gate did not open"))
        if fake_log.exists() and len(fake_log.read_text().splitlines()) == 2:
            checks.append(CheckResult("fake-rclone transfer calls", "ok", str(fake_log)))
        else:
            checks.append(CheckResult("fake-rclone transfer calls", "error", f"unexpected fake log: {fake_log}"))

        pushd_consume = _check_json_command(
            checks,
            env,
            "pushd transfer consume preview",
            ("pushd", "transfer", "consume", "preview"),
        )
        diffd_consume = _check_json_command(
            checks,
            env,
            "diffd transfer consume preview",
            ("diffd", "transfer", "consume", "preview"),
        )
        if (
            pushd_consume.get("details", {}).get("state writes") == "none"
            and pushd_consume.get("details", {}).get("planned record removals") == 1
            and pushd_consume.get("details", {}).get("real execution can run") == "no"
        ):
            checks.append(CheckResult("pushd transfer consume read-only", "ok", "planned removals = 1"))
        else:
            checks.append(CheckResult("pushd transfer consume read-only", "error", "unexpected consume preview"))
        if (
            diffd_consume.get("details", {}).get("state writes") == "none"
            and diffd_consume.get("details", {}).get("planned record removals") == 1
            and diffd_consume.get("details", {}).get("real execution can run") == "no"
        ):
            checks.append(CheckResult("diffd transfer consume read-only", "ok", "planned removals = 1"))
        else:
            checks.append(CheckResult("diffd transfer consume read-only", "error", "unexpected consume preview"))
        pushd_consume_run = _check_json_command(
            checks,
            env,
            "pushd transfer consume run",
            ("pushd", "transfer", "consume", "run", "--execute"),
        )
        diffd_consume_run = _check_json_command(
            checks,
            env,
            "diffd transfer consume run",
            ("diffd", "transfer", "consume", "run", "--execute"),
        )
        if (
            pushd_consume_run.get("details", {}).get("records to remove") == 1
            and pushd_consume_run.get("details", {}).get("records after") == 0
            and pushd_consume_run.get("details", {}).get("real execution can run") == "no"
        ):
            checks.append(CheckResult("pushd transfer consume guarded run", "ok", "removed one queue item"))
        else:
            checks.append(CheckResult("pushd transfer consume guarded run", "error", "consume run mismatch"))
        if (
            diffd_consume_run.get("details", {}).get("records to remove") == 1
            and diffd_consume_run.get("details", {}).get("records after") == 0
            and diffd_consume_run.get("details", {}).get("real execution can run") == "no"
        ):
            checks.append(CheckResult("diffd transfer consume guarded run", "ok", "removed one remote change"))
        else:
            checks.append(CheckResult("diffd transfer consume guarded run", "error", "consume run mismatch"))
        _check_json_command(
            checks,
            env,
            "pushd queue restore after consume",
            ("pushd", "queue", "add", "Documents/shadow-upload.pdf", "--execute"),
        )
        _check_json_command(
            checks,
            env,
            "diffd remote-change restore after consume",
            ("diffd", "remote-change", "add", "Documents/shadow-download.pdf", "--execute"),
        )
        pushd_executor = _check_json_command(
            checks,
            fake_env,
            "pushd transfer executor run",
            ("pushd", "transfer", "executor-run", "--execute", "--consume-on-success"),
        )
        diffd_executor = _check_json_command(
            checks,
            fake_env,
            "diffd transfer executor run",
            ("diffd", "transfer", "executor-run", "--execute", "--consume-on-success"),
        )
        if (
            pushd_executor.get("details", {}).get("records consumed") == 1
            and pushd_executor.get("details", {}).get("real transfer automation gate status") == "closed"
        ):
            checks.append(CheckResult("pushd transfer executor guarded run", "ok", "fake transfer consumed queue"))
        else:
            checks.append(CheckResult("pushd transfer executor guarded run", "error", "executor run mismatch"))
        if (
            diffd_executor.get("details", {}).get("records consumed") == 1
            and diffd_executor.get("details", {}).get("real transfer automation gate status") == "closed"
        ):
            checks.append(CheckResult("diffd transfer executor guarded run", "ok", "fake transfer consumed changes"))
        else:
            checks.append(CheckResult("diffd transfer executor guarded run", "error", "executor run mismatch"))
        upload_origin_journal = state_dir / "pushd" / "upload-origin-journal.json"
        upload_origin_records = _read_json(upload_origin_journal).get("records", []) if upload_origin_journal.exists() else []
        if upload_origin_records and upload_origin_records[0].get("direction") == "upload":
            checks.append(CheckResult("pushd upload origin journal", "ok", str(upload_origin_journal)))
        else:
            checks.append(CheckResult("pushd upload origin journal", "error", "missing upload-origin record"))
        _check_json_command(
            checks,
            env,
            "diffd remote-change add upload echo",
            (
                "diffd",
                "remote-change",
                "add",
                "Documents/shadow-upload.pdf",
                "--reason",
                "diff:createfile",
                "--execute",
            ),
        )
        upload_echo_preview = _check_json_command(
            checks,
            env,
            "diffd upload echo suppression preview",
            ("diffd", "preview"),
        )
        skipped = upload_echo_preview.get("details", {}).get("skipped download record details", [])
        if skipped and skipped[0].get("reason") == "upload origin journal":
            checks.append(CheckResult("diffd upload echo suppression", "ok", "upload echo skipped"))
        else:
            checks.append(CheckResult("diffd upload echo suppression", "error", "upload echo was not skipped"))
        _check_json_command(
            checks,
            env,
            "diffd remote-change remove upload echo",
            ("diffd", "remote-change", "remove", "Documents/shadow-upload.pdf", "--execute"),
        )
        _check_json_command(
            checks,
            env,
            "pushd queue restore after executor",
            ("pushd", "queue", "add", "Documents/shadow-upload.pdf", "--execute"),
        )
        _check_json_command(
            checks,
            env,
            "diffd remote-change restore after executor",
            ("diffd", "remote-change", "add", "Documents/shadow-download.pdf", "--execute"),
        )

        _check_json_command(checks, env, "pushd run", ("pushd", "run", "--execute"))
        _check_json_command(checks, env, "diffd run", ("diffd", "run", "--execute"))
        pushd_gate = _check_json_command(checks, env, "pushd real gate", ("pushd", "gate"))
        diffd_gate = _check_json_command(checks, env, "diffd real gate", ("diffd", "gate"))
        if pushd_gate.get("details", {}).get("gate status") == "closed":
            checks.append(CheckResult("pushd gate closed", "ok", "real operations blocked"))
        else:
            checks.append(CheckResult("pushd gate closed", "error", "missing closed gate status"))
        if diffd_gate.get("details", {}).get("gate status") == "closed":
            checks.append(CheckResult("diffd gate closed", "ok", "real operations blocked"))
        else:
            checks.append(CheckResult("diffd gate closed", "error", "missing closed gate status"))
        if (
            pushd_gate.get("details", {}).get("operator verification required") == "no"
            and diffd_gate.get("details", {}).get("operator verification required") == "no"
            and pushd_gate.get("details", {}).get("human gate status") == "required-before-real-work"
            and diffd_gate.get("details", {}).get("human gate status") == "required-before-real-work"
        ):
            checks.append(CheckResult("service gate operator verification", "ok", "read-only gate checks are automated"))
        else:
            checks.append(CheckResult("service gate operator verification", "error", "unexpected human verification gate"))

        pushd_plan = state_dir / "pushd" / "last-plan.json"
        diffd_plan = state_dir / "diffd" / "last-plan.json"
        pushd_queue = state_dir / "pushd" / "queue.json"
        diffd_changes = state_dir / "diffd" / "remote-changes.json"
        if pushd_plan.exists() and _read_json(pushd_plan)["counts"]["planned_uploads"] == 1:
            checks.append(CheckResult("pushd dry-run state", "ok", str(pushd_plan)))
        else:
            checks.append(CheckResult("pushd dry-run state", "error", f"missing or invalid {pushd_plan}"))
        if diffd_plan.exists() and _read_json(diffd_plan)["counts"]["planned_downloads"] == 1:
            checks.append(CheckResult("diffd dry-run state", "ok", str(diffd_plan)))
        else:
            checks.append(CheckResult("diffd dry-run state", "error", f"missing or invalid {diffd_plan}"))
        if pushd_queue.exists() and len(_read_json(pushd_queue)) == 1:
            checks.append(CheckResult("pushd queue not consumed", "ok", str(pushd_queue)))
        else:
            checks.append(CheckResult("pushd queue not consumed", "error", f"unexpected queue state: {pushd_queue}"))
        if diffd_changes.exists() and len(_read_json(diffd_changes)) == 1:
            checks.append(CheckResult("diffd changes not consumed", "ok", str(diffd_changes)))
        else:
            checks.append(CheckResult("diffd changes not consumed", "error", f"unexpected changes state: {diffd_changes}"))

        pushd_remove = _check_json_command(
            checks,
            env,
            "pushd queue remove",
            ("pushd", "queue", "remove", "Documents/shadow-upload.pdf", "--execute"),
        )
        diffd_remove = _check_json_command(
            checks,
            env,
            "diffd remote-change remove",
            ("diffd", "remote-change", "remove", "Documents/shadow-download.pdf", "--execute"),
        )
        if pushd_remove.get("details", {}).get("queue items removed") == 1:
            checks.append(CheckResult("pushd queue remove targeted", "ok", "removed one queue item"))
        else:
            checks.append(CheckResult("pushd queue remove targeted", "error", "expected one queue item removed"))
        if diffd_remove.get("details", {}).get("remote changes removed") == 1:
            checks.append(CheckResult("diffd remote-change remove targeted", "ok", "removed one remote change"))
        else:
            checks.append(
                CheckResult("diffd remote-change remove targeted", "error", "expected one remote change removed")
            )

        (workspace / "bisync_status.log").write_text("2026-04-27 23:13:00 SUCCESS mode=autosync\n")
        (workspace / "bisync_error.log").write_text("2026-04-27 22:34:15 ERROR rclone command not found\n")
        sync_status = _check_json_command(checks, env, "sync status historical last error", ("sync", "status"))
        status_detail = _check_json_command(checks, env, "status detail historical last error", ("status", "--detail"))
        if sync_status.get("details", {}).get("last error status") == "historical":
            checks.append(CheckResult("sync status historical last error marker", "ok", "last error status = historical"))
        else:
            checks.append(CheckResult("sync status historical last error marker", "error", "missing historical marker"))
        if status_detail.get("details", {}).get("last sync error status") == "historical":
            checks.append(
                CheckResult("status detail historical last error marker", "ok", "last sync error status = historical")
            )
        else:
            checks.append(CheckResult("status detail historical last error marker", "error", "missing historical marker"))

        unsafe_env = dict(env)
        unsafe_state = root / "fake-live"
        unsafe_state.mkdir()
        unsafe_env["PCLOUD_TOOLS_STATE_DIR"] = str(unsafe_state)
        unsafe = _check_json_command(
            checks,
            unsafe_env,
            "unsafe state dir refusal",
            ("pushd", "run", "--execute"),
            allowed_status={"error"},
        )
        issue_keys = {str(issue.get("key", "")) for issue in unsafe.get("issues", [])}
        if "PCLOUD_TOOLS_DEV_STATE_DIR" not in issue_keys:
            checks.append(CheckResult("unsafe state dir issue key", "error", "missing PCLOUD_TOOLS_DEV_STATE_DIR"))
        elif (unsafe_state / "pushd" / "last-plan.json").exists():
            checks.append(CheckResult("unsafe state dir write", "error", "unsafe last-plan.json was created"))
        else:
            checks.append(CheckResult("unsafe state dir guard", "ok", "write refused"))

        _check_action(checks, env, "pushd.run.preview", "pushd run preview is ready")
        _check_action(checks, env, "diffd.run.preview", "diffd run preview is ready")
        _check_action(checks, env, "mode.status.refresh", "pcloud-manager mode is")
        _check_action(checks, env, "mode.plan.daemon", "mode switch to daemon is")
        _check_action(checks, env, "mode.plan.maintenance", "mode switch to maintenance is")
        _check_action(checks, env, "mode.plan.pause", "mode switch to pause is")
        _check_action(checks, env, "pushd.gate", "pushd real-operation gate is closed")
        _check_action(checks, env, "diffd.gate", "diffd real-operation gate is closed")
        _check_action(checks, env, "pushd.launchd.status", "pushd launchd status is")
        _check_action(checks, env, "diffd.launchd.status", "diffd launchd status is")
        _check_action(checks, env, "pushd.launchd.review", "pushd launchd human review is required")
        _check_action(checks, env, "diffd.launchd.review", "diffd launchd human review is required")
        _check_action(checks, env, "pushd.launchd.resident-plist.preview", "pushd launchd resident plist is gated")
        _check_action(checks, env, "pushd.launchd.reload.preview", "pushd launchd reload is gated")
        _check_action(checks, env, "diffd.launchd.resident-plist.preview", "diffd launchd resident plist is gated")
        _check_action(checks, env, "diffd.launchd.reload.preview", "diffd launchd reload is gated")
        _check_action(checks, env, "pushd.launchd.executor-plist.preview", "pushd launchd executor plist preview is ready")
        _check_action(checks, env, "diffd.launchd.executor-plist.preview", "diffd launchd executor plist preview is ready")
        _check_action(checks, env, "pushd.launchd.automation-plist.preview", "pushd launchd automation plist is gated")
        _check_action(checks, env, "diffd.launchd.automation-reload.preview", "diffd launchd automation reload is gated")
        _check_action(checks, env, "pushd.launchd.plist.preview", "pushd launchd plist preview is ready")
        _check_action(checks, env, "diffd.launchd.plist.preview", "diffd launchd plist preview is ready")
        _check_action(checks, fswatch_gate_env, "pushd.fswatch.resident-gate", "pushd fswatch resident gate is closed")
        _check_action(checks, fswatch_gate_env, "pushd.fswatch.resident-run.preview", "pushd fswatch resident execution is gated")
        _check_action(checks, env, "diffd.api-poll.long-poll-gate", "diffd pCloud API long-poll gate is closed")
        _check_action(checks, env, "diffd.api-poll.long-poll-run.preview", "diffd pCloud API long-poll execution is gated")
        _check_action(checks, env, "sync.autosync-plist.preview", "autosync plist preview is ready")
        _check_action(checks, autosync_gate_env, "sync.autosync.gate", "autosync launchd gate is closed")
        _check_action(checks, autosync_gate_env, "sync.autosync-run.preview", "autosync launchd execution is gated")
        _check_action(checks, migration_gate_env, "sync.migration.gate", "sync migration validation gate is closed")
        _check_action(checks, migration_gate_env, "sync.migration-run.preview", "sync migration execution is gated")
        _check_action(checks, env, "archive.old-monolith.gate", "old monolith archive gate is closed")
        _check_action(checks, env, "archive.old-monolith-run.preview", "old monolith archive execution is gated")
        _check_action(checks, env, "gates.status", "all execution gates closed")
        _check_action(checks, env, "pushd.transfer.automation-gate", "pushd real transfer automation gate is closed")
        _check_action(checks, env, "diffd.transfer.automation-gate", "diffd real transfer automation gate is closed")
        _check_action(checks, env, "pushd.transfer.consume.preview", "pushd transfer consume policy preview is ready")
        _check_action(checks, env, "diffd.transfer.consume.preview", "diffd transfer consume policy preview is ready")
        _check_json_command(checks, env, "status", ("status",))
        _check_json_command(checks, env, "doctor", ("doctor",))
        _check_json_command(checks, env, "mode status", ("mode", "status"))
        _check_json_command(checks, env, "mode plan maintenance", ("mode", "plan", "maintenance"))
        _check_json_command(checks, env, "daemon status", ("daemon", "status"))

        failed = [check for check in checks if check.status != "ok"]
        return {
            "schema_version": "pcloud-tools-shadow-validation.v1",
            "status": "error" if failed else "ok",
            "workspace": str(Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])),
            "state_dir": str(state_dir),
            "checks": [check.__dict__ for check in checks],
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run pcloud-tools shadow validation against temp dev state.")
    parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    parser.add_argument("--summary", action="store_true", help="Emit a concise human summary without per-check lines.")
    parser.add_argument("--report-path", type=Path, help="Write the structured JSON report to this path.")
    args = parser.parse_args()
    report = run_validation()
    if args.report_path:
        args.report_path.parent.mkdir(parents=True, exist_ok=True)
        args.report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        print(f"shadow validation: {report['status']}")
        if args.report_path:
            print(f"report: {args.report_path}")
        if args.summary:
            checks = list(report["checks"])
            ok_count = sum(1 for check in checks if check["status"] == "ok")
            error_count = sum(1 for check in checks if check["status"] != "ok")
            print(f"checks: {ok_count} ok; {error_count} failed; {len(checks)} total")
        else:
            for check in report["checks"]:
                print(f"- {check['status']}: {check['name']}: {check['detail']}")
    return 1 if report["status"] == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
