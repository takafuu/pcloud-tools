from pathlib import Path
from types import SimpleNamespace

import pytest

from pcloud_tools import download_suppression as suppression
from pcloud_tools.service_daemon_plan import PlanRecord, build_diffd_plan_from_records, build_pushd_plan_from_records


@pytest.mark.parametrize("direction", ["upload", "download"])
def test_plan_reads_journal_once_and_next_plan_refreshes_it(tmp_path: Path, monkeypatch, direction):
    core = tmp_path / "workspace"
    core.mkdir()
    allowlist = core / "scope"
    allowlist.write_text("Documents/\n")
    config = SimpleNamespace(
        core_dir=core, state_dir=tmp_path / "state", allowlist_file=allowlist,
        manager_ignore_file=core / "ignore", core_remote="pcloud:core", default_excludes=(),
        download_suppression_ttl_seconds=86400,
    )
    local = core / "Documents/0.txt"
    local.parent.mkdir()
    local.write_text("unchanged\n")
    record = suppression.SuppressionRecord(
        path="Documents/0.txt", direction="upload" if direction == "download" else "download",
        state="completed", started_at="", completed_at=None,
        local_fingerprint=suppression.local_fingerprint(local),
    )
    write = suppression.write_upload_origin_journal if direction == "download" else suppression.write_download_suppression_journal
    write(config, (record,))
    records = tuple(PlanRecord(f"Documents/{i}.txt", direction, "diff:createfile") for i in range(1000))
    reads = []
    original = suppression._read_journal

    def tracked_read(config, kind):
        reads.append(kind)
        return original(config, kind)

    monkeypatch.setattr(suppression, "_read_journal", tracked_read)

    def plan():
        if direction == "download":
            result = build_diffd_plan_from_records(config, tmp_path / "remote.json", tmp_path / "pending.json", records)
            return result.download_records
        return build_pushd_plan_from_records(config, tmp_path / "queue.json", records).upload_records

    assert len(plan()) == 999
    assert len(reads) == 1
    write(config, ())
    assert len(plan()) == 1000
    assert len(reads) == 2
