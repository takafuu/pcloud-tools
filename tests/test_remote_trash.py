from __future__ import annotations

from datetime import datetime, timezone

import pytest

from pcloud_tools.remote_trash import (
    TRASH_ROOT,
    build_metadata_path,
    build_object_path,
    build_trash_paths,
    build_unique_trash_paths,
    is_trash_path,
    is_trash_root_path,
    list_index_records,
    metadata_payload,
    normalize_original_path,
    read_index_record,
    sanitize_display_filename,
    update_index_record,
    write_index_record,
)


def test_japanese_filename_is_preserved_in_object_path_and_metadata() -> None:
    timestamp = datetime(2026, 5, 20, 1, 2, 3, 456789, tzinfo=timezone.utc)
    paths = build_trash_paths("受信/通知書.pdf", timestamp, short_id="a1b2c3d4")

    assert paths.item_id == "20260520T010203456789Z-a1b2c3d4"
    assert paths.display_name == "通知書.pdf"
    assert paths.original_path == "受信/通知書.pdf"
    assert paths.object_path == (
        f"{TRASH_ROOT}/objects/2026/05/20/"
        "20260520T010203456789Z-a1b2c3d4__通知書.pdf"
    )
    assert paths.metadata_path == (
        f"{TRASH_ROOT}/objects/2026/05/20/"
        "20260520T010203456789Z-a1b2c3d4__通知書.pdf.json"
    )

    payload = metadata_payload(paths, operation="delete")
    assert payload["original_path"] == "受信/通知書.pdf"
    assert payload["display_name"] == "通知書.pdf"
    assert payload["object_path"].endswith("__通知書.pdf")


def test_build_path_helpers_share_timestamp_and_short_id_layout() -> None:
    timestamp = datetime(2026, 12, 31, 23, 59, 1, tzinfo=timezone.utc)

    assert build_object_path("Documents/report.pdf", timestamp, "feedface") == (
        f"{TRASH_ROOT}/objects/2026/12/31/20261231T235901000000Z-feedface__report.pdf"
    )
    assert build_metadata_path("Documents/report.pdf", timestamp, "feedface") == (
        f"{TRASH_ROOT}/objects/2026/12/31/20261231T235901000000Z-feedface__report.pdf.json"
    )


def test_duplicate_safe_ids_are_retried_when_candidate_paths_exist() -> None:
    timestamp = datetime(2026, 5, 20, 10, 0, 0, tzinfo=timezone.utc)
    first = build_trash_paths("Documents/delete-me.txt", timestamp, short_id="11111111")
    existing_paths = {first.object_path}
    short_ids = iter(("11111111", "22222222"))

    second = build_unique_trash_paths(
        "Documents/delete-me.txt",
        timestamp,
        short_id_factory=lambda: next(short_ids),
        exists=existing_paths.__contains__,
    )

    assert second.item_id == "20260520T100000000000Z-22222222"
    assert second.object_path != first.object_path
    assert second.metadata_path != first.metadata_path
    assert second.object_path.endswith("__delete-me.txt")


def test_unsafe_display_names_preserve_unicode_and_extension_without_being_authoritative() -> None:
    timestamp = datetime(2026, 5, 20, tzinfo=timezone.utc)
    paths = build_trash_paths("顧客/危険:名：通知書\x07.pdf", timestamp, short_id="abc123")
    payload = metadata_payload(paths, operation="rename-old-path")

    assert sanitize_display_filename("顧客/危険:名：通知書\x07.pdf") == "危険_名_通知書_.pdf"
    assert paths.display_name == "危険_名_通知書_.pdf"
    assert paths.object_path.endswith("__危険_名_通知書_.pdf")
    assert payload["original_path"] == "顧客/危険:名：通知書\x07.pdf"


def test_trash_root_detection_rejects_managing_trash_contents() -> None:
    assert is_trash_root_path(TRASH_ROOT)
    assert is_trash_root_path(f"/{TRASH_ROOT}/")
    assert is_trash_path(f"{TRASH_ROOT}/objects/2026/05/20/item")
    assert not is_trash_path(f"Documents/{TRASH_ROOT}/objects/item")
    assert normalize_original_path("Documents/report.pdf") == "Documents/report.pdf"

    with pytest.raises(ValueError, match="cannot manage its own trash path"):
        normalize_original_path(f"{TRASH_ROOT}/objects/2026/05/20/item")


def test_sqlite_index_roundtrip_and_status_update(tmp_path) -> None:
    timestamp = datetime(2026, 5, 20, 9, 30, tzinfo=timezone.utc)
    db_path = tmp_path / "remote-trash.sqlite3"
    paths = build_trash_paths("受信/通知書.pdf", timestamp, short_id="abcd1234")
    payload = metadata_payload(paths, operation="delete", extra={"reason": "local-delete"})

    written = write_index_record(db_path, payload, updated_at="2026-05-20T09:31:00+00:00")
    read_back = read_index_record(db_path, paths.item_id)
    updated = update_index_record(
        db_path,
        paths.item_id,
        status="restored",
        updated_at="2026-05-20T09:32:00+00:00",
    )
    records = list_index_records(db_path)

    assert written.item_id == paths.item_id
    assert read_back is not None
    assert read_back.original_path == "受信/通知書.pdf"
    assert read_back.display_name == "通知書.pdf"
    assert read_back.metadata_payload == payload
    assert updated.status == "restored"
    assert updated.updated_at == "2026-05-20T09:32:00+00:00"
    assert records == (updated,)
