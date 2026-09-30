from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys

import pytest

from test_event_sync import setup, put, EventSync
from pcloud_tools import event_sync_status as status
from pcloud_tools.io_utils import atomic_write_json
from pcloud_tools.transfer_state import transfer_tick_lock
from conftest import _base_env


def saved(cfg, **changes):
    value = {'schema': 'pcloud-event-sync.v1', 'baseline': {}, 'reviews': {},
             'updated_at': status.timestamp(), 'reconciled_request': 'initial', **changes}
    atomic_write_json(cfg.state_dir/'event-sync/state.json', value)
    for service, name in [('pushd', 'queue.json'), ('diffd', 'remote-changes.json')]:
        atomic_write_json(cfg.state_dir/service/name, [])
    return value


@pytest.fixture(autouse=True)
def no_live_services(monkeypatch):
    monkeypatch.setattr(status, 'observe_services', lambda cfg: ({}, []))


def test_saved_remaining_and_live_executor_are_distinct(setup):
    cfg, _ = setup
    state = saved(cfg, reconcile={'id': 'initial', 'pending': ['Documents/a']*11},
                  reviews={'Documents/review': {'reason': 'same second', 'local': {'exists': True}, 'cloud': {'exists': True}}, 'outside/excluded': {}})
    with transfer_tick_lock(cfg.state_dir, 'pushd'):
        snapshot = status.snapshot(cfg)
    assert snapshot['remaining'] == 11 and snapshot['review_count'] == 1
    assert snapshot['executor_active'] is True
    assert snapshot['state_revision'] == snapshot['state_saved_at'] == state['updated_at']
    assert '初回照合中' in snapshot['label'] and '11件' in snapshot['label']
    assert status.snapshot(cfg)['executor_active'] is False


@pytest.mark.parametrize('invalid', [None, '{broken', '{"schema":"pcloud-event-sync.v1","baseline":{},"reviews":[]}'])
def test_missing_or_corrupt_state_is_not_zero_or_normal(setup, invalid):
    cfg, _ = setup
    if invalid is not None:
        file = cfg.state_dir/'event-sync/state.json'; file.parent.mkdir(parents=True)
        file.write_text(invalid)
    snapshot = status.snapshot(cfg)
    assert snapshot['remaining'] is None and snapshot['review_count'] is None
    assert snapshot['issues'] and snapshot['label'] != '変更待ち'


def test_fresh_idle_and_old_state_differ(setup):
    cfg, _ = setup
    saved(cfg)
    assert status.snapshot(cfg)['label'] == '変更待ち'
    saved(cfg, updated_at=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat())
    assert status.snapshot(cfg)['label'] == '同期状態の更新待ち'


def test_failed_operation_and_missing_queue_remain_visible(setup):
    cfg, _ = setup
    saved(cfg)
    (cfg.state_dir/'diffd/remote-changes.json').unlink()
    atomic_write_json(cfg.state_dir/'event-sync/operation.json', {'status': 'failed', 'last_result': {'status': 'failed', 'finished_at': status.timestamp()}})
    snapshot = status.snapshot(cfg)
    assert snapshot['queues']['diffd']['count'] is None
    assert any('失敗' in s for s in snapshot['issues'])
    assert snapshot['label'] != '変更待ち'


def test_tick_retains_historical_result_while_next_operation_runs(setup):
    cfg, remote = setup
    put(cfg.core_dir, 'Documents/a'); put(remote.root, 'Documents/a')
    EventSync(cfg, remote).tick()
    file = cfg.state_dir/'event-sync/operation.json'
    previous = json.loads(file.read_text())['last_result']
    assert previous['counts'] == {'equal': 1}
    def while_listing():
        current = json.loads(file.read_text())
        assert current['status'] == 'running'
        assert current['last_result'] == previous
    remote.before_inventory = while_listing
    EventSync(cfg, remote).tick()


def test_cli_status_and_review_share_count_and_revision_without_rclone(tmp_path):
    env = _base_env(tmp_path, {'PCLOUD_TOOLS_DIFFD_DOWNLOAD_MODE': 'event'})
    state_dir = Path(env['PCLOUD_TOOLS_STATE_DIR'])
    state = {'schema': 'pcloud-event-sync.v1', 'baseline': {}, 'updated_at': status.timestamp(),
             'reconcile': {'id': 'initial', 'pending': ['Documents/a']*7},
             'reviews': {'Documents/a': {'path': 'Documents/a', 'reason': 'needs review', 'local': {'exists': True}, 'cloud': {'exists': True}}, 'outside/skip': {}}}
    atomic_write_json(state_dir/'event-sync/state.json', state)
    fake = tmp_path/'forbidden-rclone'
    marker = tmp_path/'rclone-was-run'
    fake.write_text('#!/bin/sh\ntouch '+str(marker)+'\nexit 99\n'); fake.chmod(0o755)
    env['PCLOUD_TOOLS_RCLONE_BIN'] = str(fake)
    reports = []
    for argv in [['status', '--detail', '--json'], ['diffd', 'transfer', 'manual', 'list', '--json']]:
        result = subprocess.run([sys.executable, '-m', 'pcloud_tools.cli', *argv], env=env, capture_output=True, text=True, timeout=10)
        reports.append(json.loads(result.stdout)['details'])
    snapshot, listing = reports[0]['event status'], reports[1]
    assert snapshot['remaining'] == 7
    assert snapshot['review_count'] == listing['count'] == 1
    assert snapshot['state_revision'] == listing['state snapshot']['state_revision'] == state['updated_at']
    assert not marker.exists()


def test_review_missing_state_reports_unavailable(tmp_path):
    env = _base_env(tmp_path, {'PCLOUD_TOOLS_DIFFD_DOWNLOAD_MODE': 'event'})
    result = subprocess.run([sys.executable, '-m', 'pcloud_tools.cli', 'diffd', 'transfer', 'manual', 'list', '--json'],
                            env=env, capture_output=True, text=True, timeout=10)
    report=json.loads(result.stdout)
    assert result.returncode != 0 and report['status']=='error'
    assert '未取得' in str(report)
    assert report.get('details',{}).get('count') is None
