#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
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
                "--confirm-path",
                "Documents/shadow-upload.pdf",
                "--confirm-direction",
                "upload",
                "--consume-policy",
                "remove-on-success-retain-on-failure",
                "--timeout-policy",
                "reuse-fake-rclone-cleanup",
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
                "--confirm-path",
                "Documents/shadow-download.pdf",
                "--confirm-direction",
                "download",
                "--consume-policy",
                "remove-on-success-retain-on-failure",
                "--timeout-policy",
                "reuse-fake-rclone-cleanup",
            ),
        )
        if (
            pushd_confirmed_check.get("details", {}).get("operator target confirmation status") == "ok"
            and pushd_confirmed_check.get("details", {}).get("consume policy status") == "ok"
            and pushd_confirmed_check.get("details", {}).get("timeout policy status") == "ok"
            and pushd_confirmed_check.get("details", {}).get("real transfer gate status") == "closed"
        ):
            checks.append(CheckResult("pushd transfer confirmation accepted", "ok", "operator review accepted"))
        else:
            checks.append(CheckResult("pushd transfer confirmation accepted", "error", "confirmation mismatch"))
        if (
            diffd_confirmed_check.get("details", {}).get("operator target confirmation status") == "ok"
            and diffd_confirmed_check.get("details", {}).get("consume policy status") == "ok"
            and diffd_confirmed_check.get("details", {}).get("timeout policy status") == "ok"
            and diffd_confirmed_check.get("details", {}).get("real transfer gate status") == "closed"
        ):
            checks.append(CheckResult("diffd transfer confirmation accepted", "ok", "operator review accepted"))
        else:
            checks.append(CheckResult("diffd transfer confirmation accepted", "error", "confirmation mismatch"))

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
        if pushd_transfer_run.get("details", {}).get("execution gate") == "open: dev-fake-rclone":
            checks.append(CheckResult("pushd transfer fake gate", "ok", "fake-rclone gate opened"))
        else:
            checks.append(CheckResult("pushd transfer fake gate", "error", "fake-rclone gate did not open"))
        if diffd_transfer_run.get("details", {}).get("execution gate") == "open: dev-fake-rclone":
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
        ):
            checks.append(CheckResult("pushd transfer consume read-only", "ok", "planned removals = 1"))
        else:
            checks.append(CheckResult("pushd transfer consume read-only", "error", "unexpected consume preview"))
        if (
            diffd_consume.get("details", {}).get("state writes") == "none"
            and diffd_consume.get("details", {}).get("planned record removals") == 1
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
        ):
            checks.append(CheckResult("pushd transfer consume guarded run", "ok", "removed one queue item"))
        else:
            checks.append(CheckResult("pushd transfer consume guarded run", "error", "consume run mismatch"))
        if (
            diffd_consume_run.get("details", {}).get("records to remove") == 1
            and diffd_consume_run.get("details", {}).get("records after") == 0
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
        for check in report["checks"]:
            print(f"- {check['status']}: {check['name']}: {check['detail']}")
    return 1 if report["status"] == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
