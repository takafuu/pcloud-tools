import shutil
import subprocess
import json
import hashlib
from types import SimpleNamespace

import pytest

from pcloud_tools.event_sync_remote import RcloneRemote, local_version, same_content, rclone_path, SyncError
from test_event_sync import put, setup


def test_old_fullwidth_slash_hold_requeues_same_id_without_resetting_cut(setup):
    from pcloud_tools.event_sync import EventSync, read_state
    from pcloud_tools.io_utils import atomic_write_json
    from test_event_sync import event, ids
    cfg, remote = setup
    path = 'Documents/travel／receipt.pdf'
    for root in (cfg.core_dir, remote.root):
        put(root, path)
        put(root, 'Documents/other')
    cloud = remote.inventory([path])[path]
    saved = {'schema': 'pcloud-event-sync.v1', 'baseline': {},
        'reviews': {path: {'path': path, 'structural': True, 'unrepresentable_name': True, 'cloud': cloud}},
        'reconcile': {'id': 'initial', 'pending': ['Documents/other'], 'captured': {'pushd': [], 'diffd': []}}}
    atomic_write_json(cfg.state_dir/'event-sync/state.json', saved)
    event(cfg, 'pushd', path, 'upload', 'new-arrival')
    result = EventSync(cfg, remote).tick(max_records=1)
    assert [(r['path'], r['action']) for r in result['results']] == [(path, 'equal')]
    assert path not in read_state(cfg)['reviews']
    assert read_state(cfg)['reconcile']['pending'] == ['Documents/other']
    assert ids(cfg, 'pushd') == ['new-arrival']
    assert not remote.copies and not remote.moves and not remote.deletes


def test_old_name_mapping_missing_id_stays_unknown(setup):
    from pcloud_tools.event_sync import EventSync, read_state
    from pcloud_tools.io_utils import atomic_write_json
    cfg, remote = setup
    path = 'Documents/travel／receipt.pdf'
    put(cfg.core_dir, path)
    saved = {'schema': 'pcloud-event-sync.v1', 'baseline': {}, 'reconciled_request': 'initial',
        'reviews': {path: {'path': path, 'structural': True, 'unrepresentable_name': True, 'cloud': {'id': 'original'}}}}
    atomic_write_json(cfg.state_dir/'event-sync/state.json', saved)
    EventSync(cfg, remote).tick()
    held = read_state(cfg)['reviews'][path]
    assert held['unrepresentable_name'] and '未取得' in held['reason']
    assert not remote.copies and not remote.deletes


@pytest.mark.parametrize('path', ['Documents/a／b', 'Documents/a‛／b', 'Documents/．', 'Documents/．．',
                                     'Documents/‛．', 'Documents/line\nname', 'Documents/tab\tname', 'Documents/␊.txt'])
def test_name_mapping_round_trip(path):
    assert rclone_path(rclone_path(path), decode=True) == path


@pytest.mark.parametrize('path', ['Documents/‛BF', 'Documents/‛␊／.txt', 'Documents/unpaired‛'])
def test_non_roundtripping_names_do_not_silently_retarget(path):
    with pytest.raises(SyncError):
        rclone_path(path)


@pytest.mark.parametrize('response', [
    {'returncode': 1, 'stdout': '[]'},
    {'returncode': 0, 'stdout': '[{"Path": "Documents/a"},'},
    {'returncode': 0, 'stdout': '[{"Path": "../outside"}]'},
])
def test_metadata_inventory_failure_is_not_an_empty_success(response):
    remote = RcloneRemote(SimpleNamespace(core_remote='fixture:core', rclone_bin='fixture'))
    remote.run = lambda args: response
    with pytest.raises(SyncError):
        remote.inventory(filter_rules=('- /**',), hashes=False)


def test_real_rclone_metadata_discovery_filters_without_losing_selected_hashes(setup):
    from pcloud_tools.event_sync import EventSync, read_state
    from pcloud_tools.sync_scope import prepare_sync_filter_rules, sync_allowlist_info
    cfg, fixture = setup
    binary = subprocess.run(['zsh', '-c', 'command -v rclone'], capture_output=True, text=True).stdout.strip()
    if not binary:
        pytest.skip('rclone unavailable')
    cfg.rclone_bin = binary
    cfg.core_remote = str(fixture.root)
    cfg.transfer_exec_timeout_seconds = 30
    cfg.pushd_transfer_concurrency = cfg.diffd_transfer_concurrency = 1
    cfg.manager_ignore_file.write_text('Documents/vendor/**\n!Documents/vendor/keep.txt\nDocuments/cache/**\n')
    names = ['Documents/a.txt', 'Documents/line\nname.txt', 'Documents/vendor/keep.txt', 'Documents/native／name', 'Documents/quoted‛／name', 'Documents/．']
    for root in (cfg.core_dir, fixture.root):
        for path in names:
            put(root, path)
        put(root, 'Documents/vendor/drop.txt')
        put(root, 'Documents/cache/drop.txt')
        put(root, 'Documents/.DS_Store')
        put(root, 'outside/never.txt')
    remote = RcloneRemote(cfg)
    filters = prepare_sync_filter_rules(cfg, sync_allowlist_info(cfg).entries)
    metadata = remote.inventory(filter_rules=filters, hashes=False)
    assert set(metadata) == set(names)
    assert all(not value['hashes'] for value in metadata.values())
    assert remote.local_paths(filters) == set(names)
    selected = remote.inventory([names[0]])
    assert same_content(local_version(cfg.core_dir / names[0]), selected[names[0]]) is True
    calls = []
    run = remote.run
    def record(args):
        calls.append(args)
        return run(args)
    remote.run = record
    result = EventSync(cfg, remote).tick(max_records=1)
    assert [row['action'] for row in result['results']] == ['equal']
    assert result['reconcile remaining'] == len(names)-1
    assert len(read_state(cfg)['baseline']) == 1
    discovery = [args for args in calls if '--filter-from' in args]
    assert len(discovery) == 2 and all('--hash' not in args for args in discovery)
    assert any('--files-from0' in args and '--hash' in args for args in calls)


def test_real_rclone_batch_nul_names_and_filtered_directory_move(tmp_path):
    binary=subprocess.run(['zsh','-c','command -v rclone'],capture_output=True,text=True).stdout.strip()
    if not binary:
        pytest.skip('rclone unavailable')
    cfg=SimpleNamespace(core_dir=tmp_path/'local',core_remote=str(tmp_path/'remote'),rclone_bin=binary,
        transfer_exec_timeout_seconds=30,pushd_transfer_concurrency=2,diffd_transfer_concurrency=2)
    cfg.core_dir.mkdir();(tmp_path/'remote').mkdir();stage=tmp_path/'stage';stage.mkdir()
    paths=['old/日本語.txt','old/ line\n tab\t.txt ','old/‛‛␊／.txt','old/literal／slash.txt','old/quoted‛／slash.txt','old/␡.txt','old/．','old/．．']
    for p in paths:put(cfg.core_dir,p,b'local bytes')
    put(tmp_path/'remote','old/.DS_Store',b'excluded')
    r=RcloneRemote(cfg)
    result=r.copy('upload',paths,stage)
    assert result['returncode']==0
    cloud=r.inventory([*paths,'old/missing.txt'])
    assert set(cloud)==set(paths)
    assert same_content(local_version(cfg.core_dir/paths[0]),cloud[paths[0]]) is True
    assert r.move_tree('old','new',[p[4:] for p in paths])['returncode']==0
    assert (tmp_path/'remote/old/.DS_Store').exists()
    assert not (tmp_path/'remote/new/.DS_Store').exists()
    newpaths=['new/'+p[4:] for p in paths]
    assert r.copy('download',newpaths,stage)['returncode']==0
    assert all((stage/p).read_bytes()==b'local bytes' for p in newpaths)


def test_real_rclone_pcloud_encoding_preserves_native_slash_and_detects_collision(tmp_path, monkeypatch):
    binary = shutil.which('rclone')
    if not binary:
        pytest.skip('rclone unavailable')
    conf = tmp_path/'rclone.conf'
    conf.write_text('[fixture]\ntype = local\nencoding = Slash,BackSlash,Del,Ctl,InvalidUtf8,Dot\n')
    monkeypatch.setenv('RCLONE_CONFIG', str(conf))
    native = tmp_path/'native'; native.mkdir()
    cfg = SimpleNamespace(core_dir=tmp_path/'local', core_remote='fixture,encoding="Slash,BackSlash,Del,Ctl,InvalidUtf8,Dot":'+str(native), rclone_bin=binary,
        transfer_exec_timeout_seconds=30, pushd_transfer_concurrency=1, diffd_transfer_concurrency=1)
    cfg.core_dir.mkdir(); stage=tmp_path/'stage';stage.mkdir()
    paths=['folder/receipt／travel.pdf','folder/receipt‛／travel.pdf', 'folder/line\nname', 'folder/．']
    for path in paths: put(cfg.core_dir,path,path.encode())
    remote=RcloneRemote(cfg)
    assert remote.copy('upload',paths,stage)['returncode']==0
    assert (native/paths[0]).is_file() and (native/paths[1]).is_file()
    assert not (native/'folder/receipt/travel.pdf').exists()
    assert set(remote.inventory(paths))==set(paths)
    assert remote.copy('download',paths,stage)['returncode']==0
    assert all((stage/path).read_bytes()==(cfg.core_dir/path).read_bytes() for path in paths)
    # Native cloud control and its unquoted symbol can map to one Standard path.
    # Reject the whole inventory rather than select one object arbitrarily.
    put(native,'folder/collision\x01',b'one');put(native,'folder/collision␁',b'two')
    with pytest.raises(SyncError, match='invalid remote inventory'):
        remote.inventory(hashes=False)


def test_old_name_metadata_failure_never_repeats_rename_advice(setup):
    from pcloud_tools.event_sync import EventSync, read_state
    from pcloud_tools.io_utils import atomic_write_json
    cfg, remote=setup
    path='Documents/existing／name.pdf'
    atomic_write_json(cfg.state_dir/'event-sync/state.json', {'schema':'pcloud-event-sync.v1','baseline':{},
        'reviews':{path:{'path':path,'structural':True,'unrepresentable_name':True,'cloud':{'id':'original'},
                        'reason':'クラウド側で名前を変更してください'}},
        'reconcile':{'id':'initial','pending':['Documents/next'],'captured':{'pushd':[],'diffd':[]}}})
    def unavailable(*args,**kwargs):raise SyncError('metadata unavailable')
    remote.inventory=unavailable
    with pytest.raises(SyncError,match='metadata unavailable'):EventSync(cfg,remote).tick()
    state=read_state(cfg)
    assert '未取得' in state['reviews'][path]['reason']
    assert '名前を変更' not in state['reviews'][path]['reason']
    assert state['reconcile']['pending']==['Documents/next']
    assert not remote.copies and not remote.deletes


@pytest.mark.parametrize('deleted', [False, True])
def test_api_id_mismatch_never_targets_another_object(setup, deleted):
    from pcloud_tools.event_sync import EventSync, read_state
    from pcloud_tools.io_utils import atomic_write_json
    cfg, remote=setup
    path='Documents/object.txt'
    put(cfg.core_dir,path);put(remote.root,path)
    EventSync(cfg,remote).tick()
    if deleted:(remote.root/path).unlink()
    else:put(remote.root,path,b'new cloud',second=200)
    atomic_write_json(cfg.state_dir/'diffd/remote-changes.json',[
        {'path':path,'action':'delete' if deleted else 'download','event_id':'mismatched', 'remote_file_id':'different-object'}])
    result=EventSync(cfg,remote).tick()
    if not deleted:
        assert result['results'][0]['action']=='download'
        assert (cfg.core_dir/path).read_bytes()==b'new cloud'
        assert path not in read_state(cfg)['reviews']
    else:
        assert result['results'][0]['action']=='delete-local'
        assert not (cfg.core_dir/path).exists()
        assert not remote.copies and not remote.deletes

@pytest.mark.parametrize('deleted', [False, True])
def test_matching_api_id_keeps_normal_download_and_delete(setup, deleted):
    from pcloud_tools.event_sync import EventSync
    from pcloud_tools.io_utils import atomic_write_json
    cfg, remote=setup;path='Documents/object.txt'
    put(cfg.core_dir,path);put(remote.root,path)
    EventSync(cfg,remote).tick()
    expected=remote.inventory([path])[path]['id']
    if deleted:(remote.root/path).unlink()
    else:put(remote.root,path,b'new cloud',second=200)
    atomic_write_json(cfg.state_dir/'diffd/remote-changes.json',[
        {'path':path,'action':'delete' if deleted else 'download','event_id':'matching', 'remote_file_id':expected}])
    result=EventSync(cfg,remote).tick()
    assert result['results'][0]['action']==('delete-local' if deleted else 'download')
    assert not (cfg.core_dir/path).exists() if deleted else (cfg.core_dir/path).read_bytes()==b'new cloud'
