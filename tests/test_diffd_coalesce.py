import json

from pcloud_tools.diffd_events import parse_diff_response_text, diff_changes_to_records
from pcloud_tools.service_daemon_plan import append_plan_record


def parsed(*entries):
    return diff_changes_to_records(parse_diff_response_text(json.dumps({'diffid': 100, 'entries': list(entries)}), 'fixture').changes)


def event(path, file_id=123, diffid=10, kind='modifyfile'):
    return {'diffid': diffid, 'event': kind, 'metadata': {'path': path, 'fileid': file_id, 'isfolder': False}}


def test_create_then_rename_and_older_response_keep_latest_name(tmp_path):
    queue = tmp_path / 'remote-changes.json'
    old, = parsed(event('Documents/old.txt', diffid=10, kind='createfile'))
    append_plan_record(queue, 'TEST', old, coalesce_remote_file=True)
    first_id = json.loads(queue.read_text())[0]['event_id']
    new, = parsed(event('Documents/new.txt', diffid=12))
    append_plan_record(queue, 'TEST', new, coalesce_remote_file=True)
    records = json.loads(queue.read_text())
    assert len(records) == 1 and records[0]['path'] == 'Documents/new.txt'
    assert records[0]['event_id'] != first_id
    before = queue.read_bytes()
    append_plan_record(queue, 'TEST', old, coalesce_remote_file=True)
    assert queue.read_bytes() == before


def test_same_response_coalesces_by_identity_and_numeric_event_order():
    records = parsed(event('new.txt', diffid=12), event('old.txt', diffid=10), event('other.txt', file_id=456))
    assert [r.path for r in records] == ['new.txt', 'other.txt']
    renamed, = parsed(event('new.txt', kind='renamefile'))
    assert renamed.action == 'download'


def test_unidentified_names_and_other_files_are_preserved(tmp_path):
    queue = tmp_path / 'remote-changes.json'
    initial = [{'path': 'legacy.txt', 'event_id': 'legacy', 'custom': 1},
               {'path': 'other.txt', 'event_id': 'other', 'remote_file_id': '456', 'diffid': '3'}]
    queue.write_text(json.dumps(initial))
    new, = parsed(event('new.txt', diffid=12))
    append_plan_record(queue, 'TEST', new, coalesce_remote_file=True)
    assert json.loads(queue.read_text())[:2] == initial
    unknown, = parsed({'event': 'rename', 'path': 'unknown.txt'})
    assert unknown.action == 'rename'
