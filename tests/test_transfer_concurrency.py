from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from conftest import (
    REPO_ROOT,
    _base_env,
    _install_fake_rclone,
    _payload,
    _use_default_dev_state_dir,
    _write_workspace_file,
)
from pcloud_tools.transfer_executor import run_transfer_batch
import pcloud_tools.transfer_executor as transfer_executor
from pcloud_tools.transfer_state import (
    TransferStateError,
    transfer_path_lock,
    transfer_tick_lock,
    transfer_tick_lock_status,
)


def test_writer_cutover_conflict_never_enters_attempt_creation(tmp_path: Path, monkeypatch) -> None:
    import contextlib
    from types import SimpleNamespace
    from pcloud_tools import cli_service_daemon as daemon

    @contextlib.contextmanager
    def busy_session(*args, **kwargs):
        raise TransferStateError("writer cutover is active")
        yield  # pragma: no cover

    def unexpected_execution(*args, **kwargs):
        pytest.fail("queue migration and attempt creation must wait for writer admission")

    monkeypatch.setattr(daemon, "writer_process_session", busy_session)
    monkeypatch.setattr(daemon, "_execute_transfer_commands_impl", unexpected_execution)
    result, issues, performance = daemon._execute_transfer_commands(
        [{"path": "Documents/a.txt", "command": ["never-run"]}],
        timeout_seconds=5,
        config=SimpleNamespace(state_dir=tmp_path, diffd_transfer_concurrency=1),
        service=SimpleNamespace(name="diffd"),
    )
    assert result[0]["deferred"] is True
    assert performance["started"] == 0
    assert all(issue.level != "error" for issue in issues)
    assert not (tmp_path / "diffd/transfer-attempts.json").exists()
    assert not transfer_tick_lock_status(tmp_path, "diffd")["active"]


def _sleep_commands(count: int) -> list[dict[str, object]]:
    return [
        {
            "path": f"Documents/concurrency-{index}.dat",
            "command": [sys.executable, "-c", "import time; time.sleep(0.08)"],
        }
        for index in range(count)
    ]


@pytest.mark.parametrize("concurrency", [1, 2, 4])
def test_executor_peak_is_bounded_and_parallel_batch_overlaps(concurrency: int) -> None:
    commands = _sleep_commands(8)
    start_barrier = threading.Barrier(concurrency) if concurrency > 1 else None

    def before_item(item: dict[str, object]) -> dict[str, object]:
        if start_barrier is not None:
            start_barrier.wait(timeout=3)
        return item

    result = run_transfer_batch(
        commands,
        timeout_seconds=5,
        concurrency=concurrency,
        before_item=before_item,
    )

    assert result.issues == []
    assert result.performance["selected"] == 8
    assert result.performance["started"] == 8
    assert result.performance["succeeded"] == 8
    assert result.performance["peak_concurrency"] <= concurrency
    if concurrency > 1:
        assert result.performance["peak_concurrency"] == concurrency


def test_sigterm_between_popen_and_registry_registration_cleans_child_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_popen = subprocess.Popen

    def delayed_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(0.2)
        return process

    monkeypatch.setattr(transfer_executor.subprocess, "Popen", delayed_popen)
    started = time.monotonic()
    result = run_transfer_batch(
        [{"path": "Documents/interrupted.txt", "command": ["/bin/sh", "-c", "sleep 1"]}],
        timeout_seconds=5,
    )

    assert time.monotonic() - started < 0.8
    assert result.issues == []
    assert result.results[0]["phase"] == "cancelled"
    assert result.results[0]["deferred"] is True
    assert result.results[0]["cleanup"]["terminated"] is True


def test_max_records_and_concurrency_are_independent(tmp_path: Path) -> None:
    env = _base_env(tmp_path, {"PCLOUD_TOOLS_PUSHD_TRANSFER_CONCURRENCY": "2"})
    state_dir = _use_default_dev_state_dir(env)
    _install_fake_rclone(env)
    pushd_dir = state_dir / "pushd"
    pushd_dir.mkdir(parents=True)
    for index in range(5):
        _write_workspace_file(env, f"Documents/selected-{index}.txt", f"{index}\n")
    queue = [
        {"path": f"Documents/selected-{index}.txt", "action": "upload", "reason": "test"}
        for index in range(5)
    ]
    (pushd_dir / "queue.json").write_text(json.dumps(queue))

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "transfer",
            "executor-run",
            "--max-records",
            "3",
            "--execute",
            "--consume-on-success",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    assert result.returncode == 0, result.stderr
    assert payload["details"]["executor batch limit"] == 3
    assert payload["details"]["planned transfer command count"] == 3
    assert payload["details"]["deferred transfer command count"] == 2
    assert payload["details"]["performance"]["concurrency"] == 2
    assert len(json.loads((pushd_dir / "queue.json").read_text())) == 2


def test_invalid_concurrency_rejects_before_executor_state_writes(tmp_path: Path) -> None:
    env = _base_env(tmp_path, {"PCLOUD_TOOLS_PUSHD_TRANSFER_CONCURRENCY": "5"})
    state_dir = _use_default_dev_state_dir(env)
    fake_log = _install_fake_rclone(env)
    pushd_dir = state_dir / "pushd"
    pushd_dir.mkdir(parents=True)
    queue = [{"path": "Documents/missing.txt", "action": "upload", "reason": "test"}]
    queue_file = pushd_dir / "queue.json"
    queue_file.write_text(json.dumps(queue))

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "transfer",
            "executor-run",
            "--execute",
            "--consume-on-success",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    assert result.returncode == 1
    assert "PCLOUD_TOOLS_PUSHD_TRANSFER_CONCURRENCY" in [issue["key"] for issue in payload["issues"]]
    assert json.loads(queue_file.read_text()) == queue
    assert not fake_log.exists()
    assert not (pushd_dir / "last-transfer.json").exists()


def test_status_info_and_doctor_report_resolved_concurrency_sources(tmp_path: Path) -> None:
    env = _base_env(
        tmp_path,
        {
            "PCLOUD_TOOLS_PUSHD_TRANSFER_CONCURRENCY": "3",
            "PCLOUD_TOOLS_DIFFD_TRANSFER_CONCURRENCY": "2",
            "PCLOUD_TOOLS_PCLOUD_API_TOKEN": "secret-token",
        },
    )

    reports = {}
    for command in (
        ("status", "--json"),
        ("info", "config", "--json"),
        ("doctor", "--json"),
    ):
        result = subprocess.run(
            [sys.executable, "-m", "pcloud_tools.cli", *command],
            check=False,
            capture_output=True,
            text=True,
            cwd=tmp_path,
            env=env,
        )
        assert result.returncode in {0, 1}, result.stderr
        reports[command[0]] = _payload(result)

    for payload in reports.values():
        details = payload["details"]
        assert details["pushd transfer concurrency"] == 3
        assert details["diffd transfer concurrency"] == 2
        assert details["transfer concurrency effective range"] == "1-4"
        assert details["pushd transfer concurrency source"] == "environment"
        assert details["diffd transfer concurrency source"] == "environment"
    assert reports["info"]["details"]["pCloud API token"] == "set (redacted)"


def test_tick_and_cross_direction_path_locks_defer_conflicts(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    ready = tmp_path / "ready"
    release = tmp_path / "release"
    child_code = """
import sys
import time
from pathlib import Path
from pcloud_tools.transfer_state import (
    transfer_path_lock,
    transfer_tick_lock,
    transfer_tick_lock_status,
)

root = Path(sys.argv[1])
service = sys.argv[2]
ready = Path(sys.argv[3])
release = Path(sys.argv[4])
manager = transfer_tick_lock if sys.argv[5] == "tick" else transfer_path_lock
args = (root, service) if sys.argv[5] == "tick" else (root, service, "Documents/shared.txt")
with manager(*args):
    ready.write_text("ready")
    while not release.exists():
        time.sleep(0.01)
"""

    def check_busy(service: str, kind: str) -> None:
        ready.unlink(missing_ok=True)
        release.unlink(missing_ok=True)
        child = subprocess.Popen(
            [sys.executable, "-c", child_code, str(state_dir), service, str(ready), str(release), kind],
            env={"PYTHONPATH": str(REPO_ROOT / "src"), "PATH": os.environ.get("PATH", "")},
        )
        try:
            for _ in range(300):
                if ready.exists():
                    break
                time.sleep(0.01)
            assert ready.exists()
            if kind == "tick":
                with pytest.raises(TransferStateError):
                    with transfer_tick_lock(state_dir, "pushd"):
                        pass
            else:
                with pytest.raises(TransferStateError):
                    with transfer_path_lock(state_dir, "diffd", "Documents/shared.txt"):
                        pass
        finally:
            release.write_text("release")
            assert child.wait(timeout=5) == 0

    check_busy("pushd", "tick")
    check_busy("pushd", "path")


@pytest.mark.parametrize("first_mode", ["automation-run", "real-run"])
def test_actual_cli_tick_competition_is_deferred(
    tmp_path: Path,
    first_mode: str,
) -> None:
    """A competing CLI tick is deferred while its peer owns the transfer flock."""

    env = _base_env(tmp_path)
    state_dir = _use_default_dev_state_dir(env)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    relative = "Documents/competing-cli.txt"
    local = _write_workspace_file(env, relative, "competing fixture bytes\n")
    pushd_dir = state_dir / "pushd"
    pushd_dir.mkdir(parents=True, exist_ok=True)
    (pushd_dir / "queue.json").write_text(
        json.dumps(
            [
                {
                    "path": relative,
                    "action": "upload",
                    "reason": "actual CLI competition",
                    "event_id": "competing-upload",
                }
            ]
        )
    )

    real_bin = workspace / ".dev-state" / "real-bin"
    real_bin.mkdir(parents=True, exist_ok=True)
    rclone = real_bin / "rclone"
    rclone.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys, time\n"
        "from pathlib import Path\n"
        "assert sys.argv[1] == 'copyto', sys.argv\n"
        "calls = Path(os.environ['C5_CALLS'])\n"
        "with calls.open('a') as stream:\n"
        "    stream.write(json.dumps({'pid': os.getpid(), 'argv': sys.argv[1:]}) + '\\n')\n"
        "release = Path(os.environ['C5_RELEASE'])\n"
        "deadline = time.monotonic() + 15\n"
        "while not release.exists():\n"
        "    if time.monotonic() > deadline:\n"
        "        raise SystemExit(74)\n"
        "    time.sleep(0.02)\n"
    )
    rclone.chmod(0o755)
    calls = tmp_path / "c5-calls.jsonl"
    release = tmp_path / "c5-release"
    report = tmp_path / "shadow.json"
    shadow_workspace = tmp_path / "pcloud-shadow-validation-c5" / "workspace"
    report.write_text(
        json.dumps(
            {
                "status": "ok",
                "workspace": str(shadow_workspace),
                "state_dir": str(shadow_workspace / ".dev-state" / "state"),
                "checks": [
                    {"name": name, "status": "ok"}
                    for name in (
                        "temporary workspace guard",
                        "temporary state dir guard",
                        "unsafe state dir guard",
                    )
                ],
            }
        )
    )
    env.update(
        {
            "PCLOUD_TOOLS_RCLONE_BIN": str(rclone),
            "PCLOUD_TOOLS_REAL_TRANSFER_EXECUTION_GATE": "operator-approved-real-transfer-v1",
            "PCLOUD_TOOLS_REAL_TRANSFER_AUTOMATION_GATE": "operator-approved-real-transfer-automation-v1",
            "PCLOUD_TOOLS_REAL_TRANSFER_AUTOMATION_RUN_GATE": "operator-approved-real-transfer-automation-run-v1",
            "C5_CALLS": str(calls),
            "C5_RELEASE": str(release),
            "PATH": f"{Path(sys.executable).parent}:{env.get('PATH', '')}",
        }
    )

    def command(mode: str) -> list[str]:
        result = [
            sys.executable,
            "-m",
            "pcloud_tools.cli",
            "pushd",
            "transfer",
            mode,
            "--report-path",
            str(report),
            "--execute",
            "--json",
        ]
        if mode == "real-run":
            result.extend(
                [
                    "--confirm-path",
                    relative,
                    "--confirm-direction",
                    "upload",
                    "--consume-policy",
                    "remove-on-success-retain-on-failure",
                    "--timeout-policy",
                    "reuse-fake-rclone-cleanup",
                    "--operator-reviewed-dry-run",
                    "--reviewer-approved-real-command",
                    "--reviewer-approved-consume-policy",
                ]
            )
        else:
            result.append("--consume-on-success")
        return result

    first = subprocess.Popen(
        command(first_mode),
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    second: subprocess.Popen[str] | None = None
    try:
        deadline = time.monotonic() + 10
        while not calls.exists() and first.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert calls.exists(), "the first CLI must reach rclone before the competing launch"
        assert transfer_tick_lock_status(state_dir, "pushd")["active"] is True
        second = subprocess.Popen(
            command("automation-run"),
            cwd=tmp_path,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        second_stdout, second_stderr = second.communicate(timeout=10)
        assert second.returncode == 0, second_stderr
        second_payload = json.loads(second_stdout)
        assert second_payload["status"] == "warning"
        assert "PCLOUD_TOOLS_TRANSFER_RECOVERY_PENDING" not in {
            issue["key"] for issue in second_payload["issues"]
        }
        assert "PCLOUD_TOOLS_TRANSFER_TICK_LOCK" in {
            issue["key"] for issue in second_payload["issues"]
        }
        assert second_payload["details"]["performance"]["started"] == 0
        assert second_payload["details"]["transfer results"][0]["deferred"] is True
        assert first.poll() is None, "the competing command must return before the first tick"
    finally:
        release.touch()
        first_stdout, first_stderr = first.communicate(timeout=20)
        if second is not None and second.poll() is None:
            second.kill()
            second.communicate(timeout=5)

    assert first.returncode == 0, first_stderr
    assert len(calls.read_text().splitlines()) == 1
    assert transfer_tick_lock_status(state_dir, "pushd")["active"] is False
    assert local.read_text() == "competing fixture bytes\n"


@pytest.mark.parametrize(
    ("service", "action"),
    [("pushd", "upload"), ("diffd", "download")],
)
def test_actual_cli_executor_consume_skips_deferred_tick(
    tmp_path: Path,
    service: str,
    action: str,
) -> None:
    """A deferred executor tick must not run consume against stale transfer state."""

    env = _base_env(tmp_path)
    state_dir = _use_default_dev_state_dir(env)
    workspace = Path(env["PCLOUD_TOOLS_WORKSPACE_ROOT"])
    relative = "Documents/deferred-executor.txt"
    if service == "pushd":
        _write_workspace_file(env, relative, "deferred executor fixture bytes\n")
        queue_name = "queue.json"
    else:
        queue_name = "remote-changes.json"
    service_dir = state_dir / service
    service_dir.mkdir(parents=True, exist_ok=True)
    queue_file = service_dir / queue_name
    queue_file.write_text(
        json.dumps(
            [
                {
                    "path": relative,
                    "action": action,
                    "reason": "actual deferred executor competition",
                    "event_id": f"{service}-deferred-original",
                }
            ]
        )
    )

    rclone = workspace / ".dev-state" / "bin" / "fake-rclone"
    rclone.parent.mkdir(parents=True, exist_ok=True)
    rclone.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys, time\n"
        "from pathlib import Path\n"
        "assert sys.argv[1] == 'copyto', sys.argv\n"
        "calls = Path(os.environ['C5_EXECUTOR_CALLS'])\n"
        "with calls.open('a') as stream:\n"
        "    stream.write(json.dumps({'pid': os.getpid(), 'argv': sys.argv[1:]}) + '\\n')\n"
        "if not sys.argv[3].startswith('pcloud:'):\n"
        "    destination = Path(sys.argv[3])\n"
        "    destination.parent.mkdir(parents=True, exist_ok=True)\n"
        "    destination.write_text('downloaded deferred fixture bytes\\n')\n"
        "release = Path(os.environ['C5_EXECUTOR_RELEASE'])\n"
        "deadline = time.monotonic() + 15\n"
        "while not release.exists():\n"
        "    if time.monotonic() > deadline:\n"
        "        raise SystemExit(74)\n"
        "    time.sleep(0.02)\n"
    )
    rclone.chmod(0o755)
    calls = tmp_path / "c5-executor-calls.jsonl"
    release = tmp_path / "c5-executor-release"
    env.update(
        {
            "PCLOUD_TOOLS_TRANSFER_EXECUTION_GATE": "dev-fake-rclone",
            "PCLOUD_TOOLS_RCLONE_BIN": str(rclone),
            "C5_EXECUTOR_CALLS": str(calls),
            "C5_EXECUTOR_RELEASE": str(release),
            "PATH": f"{Path(sys.executable).parent}:{env.get('PATH', '')}",
        }
    )
    command = [
        sys.executable,
        "-m",
        "pcloud_tools.cli",
        service,
        "transfer",
        "executor-run",
        "--execute",
        "--consume-on-success",
        "--json",
    ]
    first = subprocess.Popen(
        command,
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    second: subprocess.Popen[str] | None = None
    try:
        deadline = time.monotonic() + 10
        while not calls.exists() and first.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert calls.exists(), "the first executor CLI must reach rclone before the competing launch"
        assert transfer_tick_lock_status(state_dir, service)["active"] is True
        second = subprocess.Popen(
            command,
            cwd=tmp_path,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        second_stdout, second_stderr = second.communicate(timeout=10)
        assert second.returncode == 0, second_stderr
        second_payload = json.loads(second_stdout)
        assert second_payload["status"] == "warning"
        assert not [issue for issue in second_payload["issues"] if issue["level"] == "error"]
        assert "PCLOUD_TOOLS_TRANSFER_TICK_LOCK" in {
            issue["key"] for issue in second_payload["issues"]
        }
        assert second_payload["details"]["performance"]["started"] == 0
        assert second_payload["details"]["performance"]["deferred"] >= 1
        assert second_payload["details"]["consume status"] == "-"
        assert any(
            result.get("deferred") is True
            for result in second_payload["details"]["transfer results"]
        )
        assert first.poll() is None, "the competing command must return before the first tick"
        retained = json.loads(queue_file.read_text())
        assert any(record.get("event_id") == f"{service}-deferred-original" for record in retained)
    finally:
        release.touch()
        first_stdout, first_stderr = first.communicate(timeout=20)
        if second is not None and second.poll() is None:
            second.kill()
            second.communicate(timeout=5)

    assert first.returncode == 0, first_stderr
    assert len(calls.read_text().splitlines()) == 1


def test_status_distinguishes_running_tick_from_stopped_incomplete_attempt(tmp_path: Path):
    from pcloud_tools.transfer_state import create_attempt
    env = _base_env(tmp_path)
    state_dir = _use_default_dev_state_dir(env)
    created = create_attempt(state_dir, 'diffd', [{'path': 'Documents/test.txt', 'event_id': 'one'}], concurrency=1)
    assert created.issue is None

    def status():
        result = subprocess.run([sys.executable, '-m', 'pcloud_tools.cli', 'diffd', 'status', '--json'], env=env, cwd=tmp_path, capture_output=True, text=True)
        return json.loads(result.stdout)

    with transfer_tick_lock(state_dir, 'diffd'):
        running = status()
        assert running['details']['transfer executor active'] is True
        assert running['details']['transfer recovery pending attempts'] == 1
        assert not any(i['key'] == 'PCLOUD_TOOLS_TRANSFER_ATTEMPT_PENDING' for i in running['issues'])
    stopped = status()
    assert stopped['details']['transfer executor active'] is False
    assert any(i['key'] == 'PCLOUD_TOOLS_TRANSFER_ATTEMPT_PENDING' for i in stopped['issues'])
