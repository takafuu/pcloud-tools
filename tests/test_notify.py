from __future__ import annotations

from conftest import *


def test_notify_cli_toggles_env_and_xbar_actions_are_terminal_free(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    notify_log = tmp_path / "notify.log"
    notify_cmd = tmp_path / "notify"
    notify_cmd.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> {shlex.quote(str(notify_log))}\n"
    )
    notify_cmd.chmod(0o755)
    env["PCLOUD_TOOLS_CHAT_NOTIFY_CMD"] = f"{notify_cmd} {{message}}"

    status = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "notify", "status", "--xbar"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    enable = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "notify", "enable", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    test = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "notify", "test", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    disable = subprocess.run(
        [sys.executable, "-m", "pcloud_tools.cli", "notify", "disable", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    enable_payload = _payload(enable)
    test_payload = _payload(test)
    disable_payload = _payload(disable)

    assert status.returncode == 0
    assert "Discord notify: off" in status.stdout
    assert "terminal=false" in status.stdout
    assert "Send Discord notify test" in status.stdout
    assert enable.returncode == 0
    assert enable_payload["details"]["state writes"].endswith(".env")
    assert enable_payload["details"]["chat notify enabled"] == "yes"
    assert test.returncode == 0
    assert test_payload["details"]["chat notify test result"]["attempted"] is True
    assert "pcloud-manager notify test" in notify_log.read_text()
    assert disable.returncode == 0
    assert disable_payload["details"]["chat notify enabled"] == "no"
