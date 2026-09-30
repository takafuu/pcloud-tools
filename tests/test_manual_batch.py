import json
import pytest
from pcloud_tools import manual_batch as batch, manual_pull as mp
from pcloud_tools import cli_manual_pull as cli
from test_manual_pull import state


def request(path, choice='pull'):
    return {'schema': batch.SCHEMA, 'items': [{'path': path, 'choice': choice}]}


def test_preview_then_apply_preserves_original(state):
    config, path, local, remote, validate = state
    events = []
    preview = batch.run_batch(request(path), config, remote, validate, progress=events.append)
    assert local.read_bytes() == b'local version'
    assert preview['items'][0]['status'] == 'ready'
    result = batch.run_batch(preview, config, remote, validate, apply=True)
    assert result['counts'] == {'completed': 1}
    assert local.read_bytes() == remote.content
    assert events[-1]['item']['token']


def test_full_validation_precedes_any_mutation(state):
    config, path, local, remote, validate = state
    preview = batch.run_batch(request(path), config, remote, validate)
    preview['items'].append({'path': 'other', 'choice': 'local', 'status': 'ready', 'token': 'bad'})
    with pytest.raises(ValueError):
        batch.run_batch(preview, config, remote, validate, apply=True)
    assert local.read_bytes() == b'local version'


def test_stale_first_stops_remaining_without_retry(state, monkeypatch):
    config, path, local, remote, validate = state
    preview = batch.run_batch(request(path), config, remote, validate)
    preview['items'].append({**preview['items'][0], 'path': 'other'})
    local.write_bytes(b'changed')
    result = batch.run_batch(preview, config, remote, validate, apply=True)
    assert result['counts'] == {'failed': 1, 'unprocessed': 1}
    assert local.read_bytes() == b'changed'


def test_hold_and_duplicate_rejection(state):
    config, path, local, remote, validate = state
    assert batch.run_batch(request(path, 'hold'), config, remote, validate, apply=True)['counts'] == {'held': 1}
    doc = request(path); doc['items'] *= 2
    with pytest.raises(ValueError, match='unique'):
        batch.run_batch(doc, config, remote, validate)


def test_file_and_stdin_share_schema(tmp_path, monkeypatch):
    import io
    from types import SimpleNamespace
    doc = request('日本語 " space.txt')
    raw = json.dumps(doc).encode()
    file = tmp_path/'selection.json'; file.write_bytes(raw)
    assert batch.read_request(str(file)) == doc
    monkeypatch.setattr(batch.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(raw)))
    assert batch.read_request('-') == doc


def test_cli_parser():
    import argparse
    parser = argparse.ArgumentParser(); cli.add_parser(parser.add_subparsers())
    args = parser.parse_args(['manual','batch','preview','--input','-','--json','--progress-jsonl'])
    assert args.batch_command == 'preview' and args.progress_jsonl


def test_full_report_can_be_passed_as_file(tmp_path):
    doc = request('file.txt')
    path = tmp_path/'preview.json'
    path.write_text(json.dumps({'schema_version':'pcloud-tools-report.v1','details':doc}))
    assert batch.read_request(str(path)) == doc
