from __future__ import annotations

from conftest import *


def test_suppression_journal_wrappers_round_trip_records(tmp_path: Path) -> None:
    config = _minimal_journal_config(tmp_path)
    completed_at = datetime.now(timezone.utc).isoformat()
    download_record = SuppressionRecord(
        path="Documents/downloaded.txt",
        state="completed",
        direction="download",
        started_at=completed_at,
        completed_at=completed_at,
        local_fingerprint=LocalFingerprint(exists=True, size=12, mtime_ns=34),
    )
    upload_record = SuppressionRecord(
        path="Documents/uploaded.txt",
        state="completed",
        direction="upload",
        started_at=completed_at,
        completed_at=completed_at,
        local_fingerprint=LocalFingerprint(exists=True, size=56, mtime_ns=78),
    )

    download_path = write_download_suppression_journal(config, (download_record,))
    upload_path = write_upload_origin_journal(config, (upload_record, download_record))

    download_payload = json.loads(download_path.read_text())
    upload_payload = json.loads(upload_path.read_text())
    assert download_payload["schema_version"] == "pcloud-tools-download-suppression.v1"
    assert upload_payload["schema_version"] == "pcloud-tools-upload-origin-suppression.v1"
    assert read_download_suppression_journal(config).records == (download_record,)
    assert read_upload_origin_journal(config).records == (upload_record,)
def test_mark_completed_keeps_started_at_for_download_and_upload_journals(tmp_path: Path) -> None:
    config = _minimal_journal_config(tmp_path)
    fingerprint = LocalFingerprint(exists=True, size=123, mtime_ns=456)
    download_started = SuppressionRecord(
        path="Documents/downloaded.txt",
        state="in-progress",
        direction="download",
        started_at="2026-05-07T00:00:00+00:00",
    )
    upload_started = SuppressionRecord(
        path="Documents/uploaded.txt",
        state="in-progress",
        direction="upload",
        started_at="2026-05-08T00:00:00+00:00",
    )
    write_download_suppression_journal(config, (download_started,))
    write_upload_origin_journal(config, (upload_started,))

    mark_download_completed(config, "/Documents/downloaded.txt", fingerprint)
    mark_upload_completed(config, "/Documents/uploaded.txt", fingerprint)

    download_record = read_download_suppression_journal(config).records[0]
    upload_record = read_upload_origin_journal(config).records[0]
    assert download_record.started_at == download_started.started_at
    assert download_record.direction == "download"
    assert download_record.local_fingerprint == fingerprint
    assert upload_record.started_at == upload_started.started_at
    assert upload_record.direction == "upload"
    assert upload_record.local_fingerprint == fingerprint
def test_atomic_write_json_preserves_format_and_original_on_replace_failure(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    path = tmp_path / "state.json"
    payload = {"records": [{"path": "Documents/example.txt", "action": "upload"}]}
    expected = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"

    atomic_write_json(path, payload)

    assert path.read_text() == expected

    original = path.read_text()
    real_replace = os.replace

    def fail_replace(src: object, dst: object) -> None:
        if Path(dst) == path:
            raise OSError("simulated replace failure")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", fail_replace)
    try:
        atomic_write_json(path, {"records": []})
    except OSError as exc:
        assert str(exc) == "simulated replace failure"
    else:
        raise AssertionError("atomic_write_json should raise when os.replace fails")

    assert path.read_text() == original
    assert list(tmp_path.glob(".state.json.*.tmp"))
def test_launchctl_command_runner_retries_bootstrap_input_output_error(tmp_path: Path) -> None:
    from pcloud_tools.cli_service_daemon import _run_launchctl_commands

    fake_launchctl = tmp_path / "launchctl"
    attempts_file = tmp_path / "attempts"
    fake_launchctl.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"bootstrap\" ]; then\n"
        f"  count=$(cat {shlex.quote(str(attempts_file))} 2>/dev/null || printf 0)\n"
        "  count=$((count + 1))\n"
        f"  printf '%s' \"$count\" > {shlex.quote(str(attempts_file))}\n"
        "  if [ \"$count\" = 1 ]; then\n"
        "    printf 'Bootstrap failed: 5: Input/output error\\n' >&2\n"
        "    exit 5\n"
        "  fi\n"
        "fi\n"
        "exit 0\n"
    )
    fake_launchctl.chmod(0o755)

    results = _run_launchctl_commands(
        [[str(fake_launchctl), "bootstrap", "gui/501", "/tmp/example.plist"]],
        retry_bootstrap_io_error=True,
        retry_delay_seconds=0,
    )

    assert [result["returncode"] for result in results] == [5, 0]
    assert results[0]["tolerated"] is True
    assert results[0]["retry"] == "scheduled"
    assert results[1]["retry"] == "attempted"
def test_daemon_auto_download_execute_does_not_write_on_config_error(tmp_path: Path) -> None:
    env = _base_env(tmp_path, {"PCLOUD_TOOLS_VAULT_PORT": "bad"})
    result = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "daemon", "auto-download", "on", "--execute", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    assert result.returncode == 1
    assert payload["status"] == "error"
    assert not (_state_dir(env) / "daemon" / "auto-download").exists()
