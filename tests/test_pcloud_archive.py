from __future__ import annotations

from conftest import *


def _archive_env(tmp_path: Path) -> tuple[dict[str, str], Path, Path, Path]:
    source = tmp_path / "nas" / "archive-inbox"
    remote = tmp_path / "remote"
    state = tmp_path / "state"
    log = tmp_path / "rclone.log"
    source.mkdir(parents=True)
    remote.mkdir(parents=True)
    config = tmp_path / "config.toml"
    fake = tmp_path / "fake-rclone"
    fake.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "printf '%s\\n' \"$*\" >> \"$PCLOUD_ARCHIVE_FAKE_RCLONE_LOG\"\n"
        "cmd=\"$1\"\n"
        "shift\n"
        "case \"$cmd\" in\n"
        "  lsd) exit 0 ;;\n"
        "  lsjson)\n"
        "    printf '%s\\n' \"$PCLOUD_ARCHIVE_FAKE_REMOTE_JSON\"\n"
        "    exit 0\n"
        "    ;;\n"
        "  copy|check|deletefile) exit 0 ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    fake.chmod(0o755)
    config.write_text(
        "[defaults]\n"
        "profile = \"nas-dev\"\n"
        "\n"
        "[profiles.nas-dev]\n"
        f"source_root = \"{source}\"\n"
        "remote_root = \"pcloud-crypt:_pcloud-archive-dev\"\n"
        f"state_dir = \"{state}\"\n"
        f"log_dir = \"{tmp_path / 'logs'}\"\n"
        f"rclone_bin = \"{fake}\"\n"
        "\n"
        "[profiles.nas-dev.transfer]\n"
        "transfers = 2\n"
        "checkers = 4\n"
        "bwlimit = \"2M\"\n"
        "\n"
        "[profiles.nas-dev.ignore]\n"
        "patterns = [\".DS_Store\", \"@eaDir/**\"]\n"
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PCLOUD_ARCHIVE_")
    }
    env.update(
        {
            "PYTHONPATH": str(REPO_ROOT / "src"),
            "PCLOUD_ARCHIVE_CONFIG_FILE": str(config),
            "PCLOUD_ARCHIVE_FAKE_RCLONE_LOG": str(log),
            "PCLOUD_ARCHIVE_FAKE_REMOTE_JSON": "[]",
            "HOME": str(tmp_path / "home"),
        }
    )
    return env, source, state, log


def _run_archive(tmp_path: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pcloud_tools.pcloud_archive", *args],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env or _archive_env(tmp_path)[0],
    )


def test_pcloud_archive_help_ai_is_read_only(tmp_path: Path) -> None:
    result = _run_archive(tmp_path, "help", "--ai", "inspect archive workflow", "--topic", "workflow")

    payload = _payload(result)

    assert result.returncode == 0
    assert payload["schema_version"] == "pcloud-archive-help-ai.v1"
    assert payload["command_name"] == "pcloud-archive"
    assert payload["user_request"] == "inspect archive workflow"
    assert "promote" in payload["generated_help"]["subcommands"]
    assert any("does not call an LLM" in item for item in payload["non_goals"])


def test_pcloud_archive_doctor_detects_missing_config_and_source(tmp_path: Path) -> None:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PCLOUD_ARCHIVE_")
    }
    env.update({"PYTHONPATH": str(REPO_ROOT / "src"), "HOME": str(tmp_path / "home")})

    result = _run_archive(tmp_path, "doctor", "--json", env=env)
    payload = _payload(result)
    keys = [issue["key"] for issue in payload["issues"]]

    assert result.returncode == 1
    assert payload["status"] == "error"
    assert "PCLOUD_ARCHIVE_CONFIG" in keys
    assert "PCLOUD_ARCHIVE_SOURCE_ROOT" in keys


def test_pcloud_archive_diff_classifies_without_writing_state(tmp_path: Path) -> None:
    env, source, state, _log = _archive_env(tmp_path)
    (source / "new.txt").write_text("new\n")
    (source / "same.txt").write_text("same\n")
    (source / "changed.txt").write_text("changed-local\n")
    env["PCLOUD_ARCHIVE_FAKE_REMOTE_JSON"] = json.dumps(
        [
            {"Path": "same.txt", "Size": 5, "IsDir": False},
            {"Path": "changed.txt", "Size": 999, "IsDir": False},
            {"Path": "remote-only.txt", "Size": 1, "IsDir": False},
        ]
    )

    result = _run_archive(tmp_path, "diff", "--json", env=env)
    payload = _payload(result)

    assert result.returncode == 0
    assert payload["status"] == "ok"
    assert payload["details"]["nas only"] == 1
    assert payload["details"]["same"] == 1
    assert payload["details"]["different"] == 1
    assert payload["details"]["pCloud only"] == 1
    assert payload["details"]["state writes"] == "none"
    assert not state.exists()


def test_pcloud_archive_promote_dry_run_does_not_call_rclone_copy(tmp_path: Path) -> None:
    env, source, state, log = _archive_env(tmp_path)
    (source / "batch" / "file.txt").parent.mkdir()
    (source / "batch" / "file.txt").write_text("content\n")

    result = _run_archive(tmp_path, "promote", "batch", "--json", env=env)
    payload = _payload(result)

    assert result.returncode == 0
    assert payload["status"] == "ok"
    assert " copy " in f" {payload['details']['planned command']} "
    assert payload["details"]["state writes"] == "none"
    assert not state.exists()
    assert "copy" not in log.read_text()


def test_pcloud_archive_promote_execute_and_check_update_state(tmp_path: Path) -> None:
    env, source, state, log = _archive_env(tmp_path)
    target = source / "batch" / "file.txt"
    target.parent.mkdir()
    target.write_text("content\n")

    promote = _run_archive(tmp_path, "promote", "batch", "--execute", "--json", env=env)
    check = _run_archive(tmp_path, "check", "batch", "--execute", "--json", env=env)

    promote_payload = _payload(promote)
    check_payload = _payload(check)
    manifest = json.loads((state / "manifest.json").read_text())

    assert promote.returncode == 0
    assert check.returncode == 0
    assert promote_payload["details"]["state writes"] == "last-run only"
    assert check_payload["details"]["state writes"] == "manifest and last-run"
    assert "batch" in manifest["records"]
    assert "copy" in log.read_text()
    assert "check" in log.read_text()


def test_pcloud_archive_tombstone_blocks_repromotion(tmp_path: Path) -> None:
    env, source, state, _log = _archive_env(tmp_path)
    (source / "old.txt").write_text("old\n")
    state.mkdir(parents=True)
    (state / "tombstones.json").write_text(
        json.dumps({"schema_version": "pcloud-archive-tombstones.v1", "records": {"old.txt": {}}})
    )

    diff = _run_archive(tmp_path, "diff", "--json", env=env)
    promote = _run_archive(tmp_path, "promote", "old.txt", "--execute", "--json", env=env)

    assert _payload(diff)["details"]["tombstoned-local"] == 1
    promote_payload = _payload(promote)
    assert promote.returncode == 1
    assert promote_payload["status"] == "error"
    assert "PCLOUD_ARCHIVE_TOMBSTONE" in [issue["key"] for issue in promote_payload["issues"]]
