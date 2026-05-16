from __future__ import annotations

from conftest import *


def test_shadow_validation_script_uses_temp_dev_state_only(tmp_path: Path) -> None:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PCLOUD_TOOLS_")
    }
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "pcloud-shadow-validation.py"), "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = _payload(result)
    assert result.returncode == 0
    assert payload["status"] == "ok"
    check_names = {check["name"] for check in payload["checks"]}
    assert "pushd dry-run state" in check_names
    assert "diffd dry-run state" in check_names
    assert "unsafe state dir guard" in check_names
    assert "temporary workspace guard" in check_names
    assert "temporary state dir guard" in check_names
    workspace = Path(str(payload["workspace"])).resolve()
    state_dir = Path(str(payload["state_dir"])).resolve()
    assert workspace.parent == Path(tempfile.gettempdir()).resolve() / workspace.parent.name
    assert workspace.parent.name.startswith("pcloud-shadow-validation-")
    assert state_dir == workspace / ".dev-state" / "state"
def test_shadow_validation_script_can_write_report_file(tmp_path: Path) -> None:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PCLOUD_TOOLS_")
    }
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    report_path = tmp_path / "reports" / "shadow-validation.json"
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "pcloud-shadow-validation.py"),
            "--report-path",
            str(report_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = json.loads(report_path.read_text())
    assert result.returncode == 0
    assert "report:" in result.stdout
    assert payload["status"] == "ok"
    assert payload["schema_version"] == "pcloud-tools-shadow-validation.v1"
    workspace = Path(str(payload["workspace"])).resolve()
    state_dir = Path(str(payload["state_dir"])).resolve()
    assert workspace.parent.name.startswith("pcloud-shadow-validation-")
    assert state_dir == workspace / ".dev-state" / "state"
def test_shadow_validation_script_summary_output_is_concise(tmp_path: Path) -> None:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PCLOUD_TOOLS_")
    }
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "pcloud-shadow-validation.py"), "--summary"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    assert result.returncode == 0
    assert "shadow validation: ok" in result.stdout
    assert "checks:" in result.stdout
    assert "- ok:" not in result.stdout
def test_shadow_validation_script_summary_can_save_full_report(tmp_path: Path) -> None:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PCLOUD_TOOLS_")
    }
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    report_path = tmp_path / "reports" / "shadow-validation-summary.json"
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "pcloud-shadow-validation.py"),
            "--summary",
            "--report-path",
            str(report_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    payload = json.loads(report_path.read_text())
    assert result.returncode == 0
    assert "shadow validation: ok" in result.stdout
    assert "report:" in result.stdout
    assert "checks:" in result.stdout
    assert "- ok:" not in result.stdout
    assert payload["status"] == "ok"
    assert payload["checks"]
