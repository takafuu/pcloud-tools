"""Delayed rename notifications converge through content sync and conflict archives."""
from pathlib import Path
import pytest
from test_event_sync import setup, put
from test_sqlite_state import migrated
from pcloud_tools.event_sync import EventSync, read_state
from pcloud_tools.event_sync_watch import append_records
from pcloud_tools.sqlite_state import Store


def pending_move(cfg, remote, old, new, inode, *, directory=False, held=False):
    append_records(cfg, [{'path': old, 'destination': new, 'file_id': inode,
                         'is_dir': directory, 'action': 'move', 'reason': 'fswatch:confirmed-move'}])
    if held:
        engine = EventSync(cfg, remote)
        engine.hold(old, '改名前後の対応を現在のファイルで確認できません', engine.snapshots())
        engine.state['reviews'][old]['structural'] = True
        engine.state['reconcile'] = {'id': 'long-scan', 'pending': ['Documents/z'+str(i) for i in range(80)],
                                    'captured': {'pushd': [], 'diffd': []}}
        engine.save()


def archived(cfg, path):
    return [p.read_bytes() for p in (cfg.core_dir/'.conflict').glob('*/data/'+path)]


def test_atomic_save_replaced_again_archives_old_cloud_and_syncs_current_file(setup):
    cfg, remote = setup
    old, new = 'Documents/temp', 'Documents/settings'
    original = put(cfg.core_dir, old, b'original', 100)
    inode = original.stat().st_ino
    put(cfg.core_dir, new, b'previous settings', 50)
    EventSync(cfg, remote).tick()
    migrated(cfg)
    original.rename(cfg.core_dir/new)
    replacement = put(cfg.core_dir, 'Documents/replacement', b'latest', 300)
    replacement.replace(cfg.core_dir/new)
    pending_move(cfg, remote, old, new, inode, held=True)
    for _ in range(3):
        EventSync(cfg, remote).tick(max_records=10)
    assert old not in read_state(cfg)['reviews']
    assert not (remote.root/old).exists()
    assert not (cfg.core_dir/old).exists()
    assert (remote.root/new).read_bytes() == b'latest'
    assert b'original' in archived(cfg, old)
    assert b'previous settings' in archived(cfg, new)
    assert not remote.moves


def test_renamed_directory_recreated_at_original_name_keeps_both_trees(setup):
    cfg, remote = setup
    old, new = 'Documents/captures', 'Documents/previous'
    put(cfg.core_dir, old+'/original', b'original', 100)
    inode = (cfg.core_dir/old).stat().st_ino
    EventSync(cfg, remote).tick()
    migrated(cfg)
    (cfg.core_dir/old).rename(cfg.core_dir/new)
    put(cfg.core_dir, old+'/new', b'new capture', 300)
    pending_move(cfg, remote, old, new, inode, directory=True, held=True)
    for _ in range(3):
        EventSync(cfg, remote).tick(max_records=10)
    assert old not in read_state(cfg)['reviews']
    assert (remote.root/new/'original').read_bytes() == b'original'
    assert (remote.root/old/'new').read_bytes() == b'new capture'
    assert not (remote.root/old/'original').exists()
    assert b'original' in archived(cfg, old+'/original')
    assert not remote.moves


def test_both_old_names_gone_does_not_block_current_name(setup):
    cfg, remote = setup
    EventSync(cfg, remote).tick()
    migrated(cfg)
    current = put(cfg.core_dir, 'Documents/final', b'current', 300)
    pending_move(cfg, remote, 'Documents/old', 'Documents/intermediate', current.stat().st_ino, held=True)
    append_records(cfg, [{'path': 'Documents/final', 'action': 'upload'}])
    for _ in range(3):
        EventSync(cfg, remote).tick(max_records=10)
    assert not list(read_state(cfg)['reviews'])
    assert (remote.root/'Documents/final').read_bytes() == b'current'
    assert not remote.deletes


def test_listing_failure_retries_move_without_stopping_other_uploads(setup):
    from pcloud_tools.event_sync_remote import SyncError
    cfg, remote = setup
    EventSync(cfg, remote).tick()
    migrated(cfg)
    current = put(cfg.core_dir, 'Documents/new', b'new')
    pending_move(cfg, remote, 'Documents/old', 'Documents/new', current.stat().st_ino-1, held=True)
    put(cfg.core_dir, 'Documents/other', b'other')
    append_records(cfg, [{'path': 'Documents/other', 'action': 'upload'}])
    original = remote.inventory
    def fail_listing(paths=None, **kwargs):
        if paths is None:
            raise SyncError('remote inventory failed; absence cannot be inferred')
        return original(paths, **kwargs)
    remote.inventory = fail_listing
    EventSync(cfg, remote).tick(max_records=10)
    assert 'Documents/old' in read_state(cfg)['reviews']
    assert (remote.root/'Documents/other').read_bytes() == b'other'
    assert not remote.deletes
    remote.inventory = original
    for _ in range(2):
        EventSync(cfg, remote).tick(max_records=10)
    assert 'Documents/old' not in read_state(cfg)['reviews']
    assert (remote.root/'Documents/new').read_bytes() == b'new'


def test_newer_move_event_is_not_consumed_by_old_move_recheck(setup):
    cfg, remote = setup
    EventSync(cfg, remote).tick()
    store = migrated(cfg)
    current = put(cfg.core_dir, 'Documents/new', b'new')
    pending_move(cfg, remote, 'Documents/old', 'Documents/new', current.stat().st_ino-1, held=True)
    def newer():
        append_records(cfg, [{'path': 'Documents/old', 'destination': 'Documents/new',
                              'file_id': current.stat().st_ino, 'action': 'move', 'reason': 'new generation'}])
    remote.before_inventory = newer
    EventSync(cfg, remote).tick(max_records=10)
    assert any(r.get('reason') == 'new generation' for r in store.queue_rows('pushd', ['Documents/old']))


def test_changed_cloud_identity_is_archived_instead_of_becoming_another_hold(setup):
    cfg, remote = setup
    old, new = 'Documents/old', 'Documents/new'
    original = put(cfg.core_dir, old, b'original', 100)
    inode = original.stat().st_ino
    EventSync(cfg, remote).tick()
    store = migrated(cfg)
    original.rename(cfg.core_dir/new)
    replacement = put(cfg.core_dir, 'Documents/replacement', b'latest', 300)
    replacement.replace(cfg.core_dir/new)
    pending_move(cfg, remote, old, new, inode, held=True)
    put(remote.root, old, b'independent cloud edit', 200)
    store.append('diffd', {'path': old, 'action': 'download', 'event_id': 'old-identity', 'remote_file_id': 'obsolete'})
    for _ in range(3):
        EventSync(cfg, remote).tick(max_records=10)
    assert not (remote.root/old).exists()
    assert b'independent cloud edit' in archived(cfg, old)
    assert old not in read_state(cfg)['reviews']
    assert (remote.root/new).read_bytes() == b'latest'


def test_valid_move_with_existing_cloud_destination_uses_archive_and_timestamp_policy(setup):
    cfg, remote = setup
    old, new = 'Documents/old', 'Documents/new'
    original = put(cfg.core_dir, old, b'local renamed', 100)
    inode = original.stat().st_ino
    EventSync(cfg, remote).tick()
    migrated(cfg)
    original.rename(cfg.core_dir/new)
    put(remote.root, new, b'newer cloud destination', 400)
    pending_move(cfg, remote, old, new, inode, held=True)
    for _ in range(3):
        EventSync(cfg, remote).tick(max_records=10)
    assert not remote.moves
    assert (cfg.core_dir/new).read_bytes() == b'newer cloud destination'
    assert (remote.root/new).read_bytes() == b'newer cloud destination'
    assert b'local renamed' in archived(cfg, new)
    assert b'local renamed' in archived(cfg, old)
    assert not (remote.root/old).exists()
