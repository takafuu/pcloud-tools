#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import plistlib
import subprocess
import sys
import tempfile
from dataclasses import dataclass
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
        autosync_plist = workspace / ".dev-state" / "com.example.pcloud-bisync.dev.plist"
        dev_entrypoint = workspace / "pcloud-manager-dev"
        dev_entrypoint.write_text("#!/bin/sh\nexit 0\n")
        dev_entrypoint.chmod(0o755)
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
        if pushd_preview.get("details", {}).get("planned uploads") != 1:
            checks.append(CheckResult("pushd planned uploads", "error", "expected planned uploads = 1"))
        else:
            checks.append(CheckResult("pushd planned uploads", "ok", "planned uploads = 1"))
        if diffd_preview.get("details", {}).get("planned downloads") != 1:
            checks.append(CheckResult("diffd planned downloads", "error", "expected planned downloads = 1"))
        else:
            checks.append(CheckResult("diffd planned downloads", "ok", "planned downloads = 1"))

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
                        {"path": "Documents/api-shadow.txt", "event": "modified"},
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
            and api_long_poll_run.get("details", {}).get("download records appended") == 1
            and api_long_poll_run.get("details", {}).get("skipped download records") == 1
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
        launchctl_bin_dir = workspace / ".dev-state" / "launchctl-bin"
        launchctl_bin_dir.mkdir(parents=True, exist_ok=True)
        fake_launchctl = launchctl_bin_dir / "launchctl"
        fake_launchctl.write_text("#!/bin/sh\nif [ \"$1\" = \"print\" ]; then exit 1; fi\nexit 0\n")
        fake_launchctl.chmod(0o755)
        autosync_gate_env = dict(env)
        autosync_gate_env["PATH"] = f"{launchctl_bin_dir}:{env.get('PATH', '')}"
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
        migration_bin_dir = workspace / ".dev-state" / "migration-bin"
        migration_bin_dir.mkdir(parents=True, exist_ok=True)
        fake_migration_rclone = migration_bin_dir / "rclone"
        fake_migration_rclone.write_text("#!/bin/sh\nexit 0\n")
        fake_migration_rclone.chmod(0o755)
        migration_gate_env = dict(env)
        migration_gate_env["PATH"] = f"{migration_bin_dir}:{env.get('PATH', '')}"
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
        real_rclone.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$REAL_RCLONE_STUB_LOG\"\n")
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

        fake_bin_dir = workspace / ".dev-state" / "bin"
        fake_bin_dir.mkdir(parents=True, exist_ok=True)
        fake_log = workspace / ".dev-state" / "fake-rclone.log"
        fake_rclone = fake_bin_dir / "fake-rclone"
        fake_rclone.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$FAKE_RCLONE_LOG\"\n")
        fake_rclone.chmod(0o755)
        fake_env = dict(env)
        fake_env.update(
            {
                "PCLOUD_TOOLS_TRANSFER_EXECUTION_GATE": "dev-fake-rclone",
                "PCLOUD_TOOLS_RCLONE_BIN": str(fake_rclone),
                "FAKE_RCLONE_LOG": str(fake_log),
            }
        )
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
        _check_action(checks, env, "pushd.gate", "pushd real-operation gate is closed")
        _check_action(checks, env, "diffd.gate", "diffd real-operation gate is closed")
        _check_action(checks, fswatch_gate_env, "pushd.fswatch.resident-gate", "pushd fswatch resident gate is closed")
        _check_action(checks, fswatch_gate_env, "pushd.fswatch.resident-run.preview", "pushd fswatch resident execution is gated")
        _check_action(checks, env, "diffd.api-poll.long-poll-gate", "diffd pCloud API long-poll gate is closed")
        _check_action(checks, env, "diffd.api-poll.long-poll-run.preview", "diffd pCloud API long-poll execution is gated")
        _check_action(checks, env, "sync.autosync-plist.preview", "autosync plist preview is ready")
        _check_action(checks, autosync_gate_env, "sync.autosync.gate", "autosync launchd gate is closed")
        _check_action(checks, migration_gate_env, "sync.migration.gate", "sync migration validation gate is closed")
        _check_action(checks, env, "archive.old-monolith.gate", "old monolith archive gate is closed")
        _check_action(checks, env, "gates.status", "all execution gates closed")
        _check_action(checks, env, "pushd.transfer.consume.preview", "pushd transfer consume policy preview is ready")
        _check_action(checks, env, "diffd.transfer.consume.preview", "diffd transfer consume policy preview is ready")
        _check_json_command(checks, env, "status", ("status",))
        _check_json_command(checks, env, "doctor", ("doctor",))
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
