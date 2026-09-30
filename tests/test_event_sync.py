import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from pcloud_tools.event_sync import ABSENT, EventSync, choose, read_state, request_reconciliation
from pcloud_tools.event_sync_remote import local_version, SyncError, utc_second
from pcloud_tools.event_sync_watch import append_records
from pcloud_tools.io_utils import atomic_write_json
from pcloud_tools.transfer_state import create_attempt, unresolved_attempts
from pcloud_tools.transfer_recovery import recover_attempt


def put(root, path, content=b"initial", second=100):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    os.utime(target, (second, second))
    return target


class Remote:
    def __init__(self, config, root):
        self.config, self.root = config, root
        self.copies, self.moves, self.deletes = [], [], []
        self.after_copy = None
        self.before_inventory = None
        self.fail_path = None

    def inventory(self, paths=None, filter_rules=None, *, hashes=True):
        if self.before_inventory:
            callback, self.before_inventory = self.before_inventory, None
            callback()
        paths = paths if paths is not None else [p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file()]
        result = {}
        for p in paths:
            if not (self.root / p).is_file():
                continue
            v = local_version(self.root / p)
            result[p] = {"exists": True, "size": v["size"], "second": v["second"],
                         "modified": v["second"], "id": str(v["inode"]), "hashes": v["hashes"] if hashes else {}}
        return result

    def local_paths(self, filter_rules):
        from pcloud_tools.event_sync import Scope
        return Scope(self.config).local_paths()

    def copy(self, direction, paths, staging):
        self.copies.append((direction, list(paths)))
        for p in paths:
            if p == self.fail_path:
                continue
            src, dst = (self.config.core_dir/p, self.root/p) if direction == "upload" else (self.root/p, staging/p)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        if self.after_copy:
            self.after_copy()
        return {"returncode": 1 if self.fail_path else 0}

    def delete(self, path):
        self.deletes.append(path)
        (self.root/path).unlink()
        return {"returncode": 0}

    def move(self, old, new):
        self.moves.append((old,new))
        (self.root/new).parent.mkdir(parents=True, exist_ok=True)
        os.replace(self.root/old, self.root/new)
        return {"returncode": 0}

    def move_tree(self, old, new, paths):
        for p in paths:
            self.move(old+"/"+p,new+"/"+p)
        return {"returncode": 0}


@pytest.fixture
def setup(tmp_path):
    cfg = SimpleNamespace(core_dir=tmp_path/'local', state_dir=tmp_path/'state',
        core_remote='fixture:core', allowlist_file=tmp_path/'allow', manager_ignore_file=tmp_path/'ignore',
        default_excludes=('.DS_Store','**/.DS_Store'), remote_trash_root='fixture:core/.pcloud-manager-trash',
        pushd_queue_limit=1000, pushd_upload_settle_seconds=0)
    cfg.core_dir.mkdir(); cfg.allowlist_file.write_text('Documents/\n'); cfg.manager_ignore_file.write_text('node_modules/**\n')
    cloud=tmp_path/'cloud';cloud.mkdir()
    return cfg,Remote(cfg,cloud)


def event(cfg, service, path, action, identifier):
    file=cfg.state_dir/service/('queue.json' if service=='pushd' else 'remote-changes.json')
    data=json.loads(file.read_text()) if file.exists() else []
    atomic_write_json(file,[*data,{'path':path,'action':action,'event_id':identifier}])


def ids(cfg,service):
    file=cfg.state_dir/service/('queue.json' if service=='pushd' else 'remote-changes.json')
    return [r['event_id'] for r in json.loads(file.read_text())] if file.exists() else []


def initial(setup, path='Documents/a.txt'):
    cfg,r=setup
    put(cfg.core_dir,path);put(r.root,path)
    EventSync(cfg,r).tick()
    return cfg,r,path


def test_content_and_seconds(setup):
    cfg,r=setup
    a=local_version(put(cfg.core_dir,'Documents/a',b'abc',100.9))
    b=local_version(put(r.root,'Documents/a',b'xyz',100.1))
    assert choose(a,b)[0]=='upload'
    assert choose(a,b,same_time='cloud')[0]=='download'
    assert choose(a,{**b,'second':101})[0]=='download'
    assert choose({**a,'second':102},b)[0]=='upload'
    assert choose(a,{**a,'second':99})[0]=='equal'
    assert choose(a,{**b,'hashes':{}})[0]=='hold'
    assert choose(a,{**b,'second':None})[0]=='hold'
    assert utc_second('1970-01-01T09:01:40.9+09:00')==100
    assert utc_second('1970-01-01T00:01:40') is None


def test_startup_copies_one_sided_and_retires_old_delete(setup):
    cfg,r=setup
    put(r.root,'Documents/a'); put(cfg.core_dir,'Documents/b')
    event(cfg,'pushd','Documents/a','delete','old-delete')
    EventSync(cfg,r).tick()
    assert (cfg.core_dir/'Documents/a').read_bytes()==b'initial'
    assert (r.root/'Documents/b').read_bytes()==b'initial'
    assert not r.deletes and not ids(cfg,'pushd')


def test_generation_arriving_during_reconcile_survives(setup):
    cfg,r=setup
    put(r.root,'Documents/a')
    event(cfg,'diffd','Documents/a','download','old')
    r.before_inventory=lambda:event(cfg,'diffd','Documents/a','download','new')
    EventSync(cfg,r).tick()
    assert ids(cfg,'diffd')==['new']


@pytest.mark.parametrize('failed_side', ['cloud', 'local'])
def test_discovery_failure_preserves_captured_queue_before_snapshot(setup, failed_side):
    cfg, remote = setup
    event(cfg, 'pushd', 'Documents/a', 'upload', 'pending')
    def fail(*args, **kwargs):
        raise SyncError('incomplete inventory; absence unknown')
    if failed_side == 'cloud':
        remote.inventory = fail
    else:
        remote.local_paths = fail
    with pytest.raises(SyncError, match='incomplete inventory'):
        EventSync(cfg, remote).tick()
    assert ids(cfg, 'pushd') == ['pending']
    assert 'reconcile' not in read_state(cfg)


def test_reconciliation_interruption_replays_pending_without_consuming_new_events(setup, monkeypatch):
    cfg, remote = setup
    for path in ['Documents/a', 'Documents/b']:
        put(cfg.core_dir, path); put(remote.root, path)
    event(cfg, 'pushd', 'Documents/a', 'upload', 'old')
    engine = EventSync(cfg, remote)
    local = engine.local
    def interrupted(path, *args):
        if path.endswith('/b'):
            event(cfg, 'pushd', 'Documents/a', 'upload', 'arrived')
            raise RuntimeError('interrupted between items')
        return local(path, *args)
    monkeypatch.setattr(engine, 'local', interrupted)
    with pytest.raises(RuntimeError, match='interrupted'):
        engine.tick()
    assert read_state(cfg)['reconcile']['pending'] == ['Documents/a', 'Documents/b']
    assert ids(cfg, 'pushd') == ['arrived']
    writes = []
    retry = EventSync(cfg, remote)
    save = retry.save
    def saved():
        writes.append(True)
        save()
    monkeypatch.setattr(retry, 'save', saved)
    result = retry.tick()
    assert len(result['results']) == 2 and all(row['action'] == 'equal' for row in result['results'])
    assert len(writes) == 1
    assert ids(cfg, 'pushd') == ['arrived']


def test_two_sided_edit_newer_wins_and_own_event_is_equal(setup):
    cfg,r,p=initial(setup)
    put(cfg.core_dir,p,b'local newer',300);put(r.root,p,b'cloud edit',200)
    event(cfg,'pushd',p,'upload','p');event(cfg,'diffd',p,'download','d')
    result=EventSync(cfg,r).tick()
    assert (r.root/p).read_bytes()==b'local newer'
    assert not result['reviews']
    event(cfg,'diffd',p,'download','own')
    copies=len(r.copies)
    assert EventSync(cfg,r).tick()['results'][0]['action']=='equal'
    assert len(r.copies)==copies
    put(r.root,p,b'other client',400);event(cfg,'diffd',p,'download','other')
    EventSync(cfg,r).tick()
    assert (cfg.core_dir/p).read_bytes()==b'other client'


@pytest.mark.parametrize('side',['local','cloud'])
@pytest.mark.parametrize('edited',[False,True])
def test_live_delete_and_opposite_edit(setup,side,edited):
    cfg,r,p=initial(setup)
    deleted,survivor=(cfg.core_dir,r.root) if side=='local' else (r.root,cfg.core_dir)
    (deleted/p).unlink()
    if edited:put(survivor,p,b'unreflected edit',200)
    event(cfg,'pushd' if side=='local' else 'diffd',p,'delete','delete')
    result=EventSync(cfg,r).tick()
    assert not (survivor/p).exists()
    assert not result['reviews']
    if edited:
        copies=list((cfg.core_dir/'.conflict').glob('*/data/Documents/a.txt'))
        assert copies and copies[0].read_bytes()==b'unreflected edit'


def test_restart_does_not_replay_live_delete(setup):
    cfg,r,p=initial(setup)
    (cfg.core_dir/p).unlink();event(cfg,'pushd',p,'delete','old')
    request_reconciliation(cfg,'restart')
    EventSync(cfg,r).tick()
    assert (cfg.core_dir/p).exists() and not r.deletes


def test_download_race_retains_local_and_generation(setup):
    cfg,r,p=initial(setup)
    put(r.root,p,b'cloud newer',200);event(cfg,'diffd',p,'download','old')
    def edit():
        put(cfg.core_dir,p,b'new local edit',300)
        event(cfg,'diffd',p,'download','new')
    r.after_copy=edit
    result=EventSync(cfg,r).tick()
    assert (cfg.core_dir/p).read_bytes()==b'new local edit'
    assert ids(cfg,'diffd')==['old','new'] and result['reviews']


def test_partial_batch_only_consumes_verified_items(setup):
    cfg,r=setup
    EventSync(cfg,r).tick()
    for p in ['Documents/a','Documents/b']:
        put(cfg.core_dir,p);event(cfg,'pushd',p,'upload',p)
    r.fail_path='Documents/b'
    result=EventSync(cfg,r).tick()
    assert r.copies==[('upload',['Documents/a','Documents/b'])]
    assert ids(cfg,'pushd')==['Documents/b']
    assert result['reviews'][0]['path']=='Documents/b'


def test_recovery_blocks_before_queue_cleanup(setup):
    cfg,r=setup
    event(cfg,'pushd','Documents/a','delete','a')
    attempt=create_attempt(cfg.state_dir,'pushd',[],concurrency=1)
    with pytest.raises(SyncError,match='recovery'):
        EventSync(cfg,r).tick()
    assert ids(cfg,'pushd')==['a'] and not r.copies
    assert recover_attempt(cfg.state_dir,'pushd',attempt.attempt_id,child_exit_confirmed=False,
        writers_stopped=True,latest_event_ids_rechecked=True,local_fingerprints_rechecked=True).issue


def test_edit_plus_confirmed_rename_and_destination_overwrite(setup):
    cfg,r,old=initial(setup)
    new='Documents/new.txt';put(r.root,new,b'overwritten',500)
    put(cfg.core_dir,old,b'edited then renamed',200)
    (cfg.core_dir/old).rename(cfg.core_dir/new)
    append_records(cfg,[{'path':old,'action':'upload'}, {'path':old,'action':'move','destination':new,
        'file_id':(cfg.core_dir/new).stat().st_ino,'is_dir':False}])
    EventSync(cfg,r).tick()
    assert r.moves==[(old,new)]
    assert (r.root/new).read_bytes()==b'edited then renamed'
    assert not (r.root/old).exists()


def test_scope_symlink_and_excluded_move_children(setup):
    cfg,r=setup
    put(cfg.core_dir,'Documents/old/a');put(r.root,'Documents/old/a')
    put(r.root,'Documents/old/.DS_Store');put(cfg.core_dir,'Documents/old/.DS_Store')
    EventSync(cfg,r).tick()
    (cfg.core_dir/'Documents/old').rename(cfg.core_dir/'Documents/new')
    append_records(cfg,[{'path':'Documents/old','action':'move','destination':'Documents/new',
        'file_id':(cfg.core_dir/'Documents/new').stat().st_ino,'is_dir':True}])
    EventSync(cfg,r).tick()
    assert (r.root/'Documents/new/a').exists()
    assert (r.root/'Documents/old/.DS_Store').exists()
    assert not (r.root/'Documents/new/.DS_Store').exists()

@pytest.mark.parametrize('side',['local','pull'])
def test_manual_restores_survivor_and_rejects_missing_side(setup,side):
    from pcloud_tools import manual_pull as mp
    cfg,r,path=initial(setup);cfg.sync_policy='event'
    if side=='local':
        (r.root/path).unlink();put(cfg.core_dir,path,b'unsynced',200)
        event(cfg,'diffd',path,'delete','delete-cloud')
    else:
        (cfg.core_dir/path).unlink();put(r.root,path,b'unsynced',200)
        event(cfg,'pushd',path,'delete','delete-local')
    engine=EventSync(cfg,r);engine.hold(path,'manual fixture',engine.snapshots(),engine.local(path),r.inventory([path]).get(path,ABSENT));engine.save()
    with pytest.raises(SyncError,match='ありません'):
        mp.preview(cfg,path,'local' if side=='pull' else 'pull',r,lambda p:None)
    preview=mp.preview(cfg,path,side,r,lambda p:None)
    result=mp.apply(cfg,path,side,preview['token'],r,lambda p:None)
    assert result['status']=='completed'
    assert (r.root/path).read_bytes()==(cfg.core_dir/path).read_bytes()==b'unsynced'
    assert path not in read_state(cfg)['reviews']


def test_manual_same_second_keeps_cloud_original_and_rejects_later_generation(setup):
    from pcloud_tools import manual_pull as mp
    cfg,r,path=initial(setup);cfg.sync_policy='event'
    put(cfg.core_dir,path,b'left',200);put(r.root,path,b'right',200)
    event(cfg,'pushd',path,'upload','first')
    engine=EventSync(cfg,r);engine.hold(path,'manual fixture',engine.snapshots(),engine.local(path),r.inventory([path])[path]);engine.save()
    p=mp.preview(cfg,path,'local',r,lambda p:None)
    event(cfg,'pushd',path,'upload','later')
    with pytest.raises(SyncError,match='イベントが変更'):
        mp.apply(cfg,path,'local',p['token'],r,lambda p:None)
    assert (r.root/path).read_bytes()==b'right'
    p=mp.preview(cfg,path,'local',r,lambda p:None)
    result=mp.apply(cfg,path,'local',p['token'],r,lambda p:None)
    assert any(p.read_bytes()==b'right' for p in (cfg.core_dir/'.conflict').glob('*/data/'+path))
    assert (r.root/path).read_bytes()==b'left'


def test_held_head_does_not_starve_other_paths(setup):
    cfg,r=setup
    for path in ['Documents/a','Documents/b']:
        put(cfg.core_dir,path,b'left',100);put(r.root,path,b'right',100)
    EventSync(cfg,r).tick(max_records=1);EventSync(cfg,r).tick(max_records=1)
    put(cfg.core_dir,'Documents/c',b'new',200)
    event(cfg,'pushd','Documents/c','upload','last')
    for _ in range(3):EventSync(cfg,r).tick(max_records=1)
    assert (r.root/'Documents/c').read_bytes()==b'new'


def test_unverified_move_does_not_restore_old_path(setup):
    cfg,r,path=initial(setup)
    (cfg.core_dir/path).rename(cfg.core_dir/'Documents/b')
    event(cfg,'pushd',path,'move','move')
    for _ in range(2):EventSync(cfg,r).tick()
    assert not (cfg.core_dir/path).exists()
    assert read_state(cfg)['reviews'][path]['structural']


def test_confirmed_move_hashes_only_its_sources_and_holds_unknown_source(setup):
    cfg, remote, path = initial(setup)
    destination = 'Documents/renamed.txt'
    (cfg.core_dir / path).rename(cfg.core_dir / destination)
    event(cfg, 'pushd', path, 'move', 'rename')
    queue = cfg.state_dir / 'pushd/queue.json'
    records = json.loads(queue.read_text())
    records[0].update(destination=destination, file_id=(cfg.core_dir / destination).stat().st_ino)
    atomic_write_json(queue, records)
    original = remote.inventory
    calls = []
    def inventory(paths=None, filter_rules=None, *, hashes=True):
        calls.append((paths, hashes))
        result = original(paths, filter_rules, hashes=hashes)
        if paths == [path]:
            result[path]['hashes'] = {}
        return result
    remote.inventory = inventory
    result = EventSync(cfg, remote).tick()
    assert (None, False) in calls and ([path], True) in calls
    assert (None, True) not in calls
    assert not remote.moves and (remote.root / path).exists()
    assert any(row.get('structural') for row in result['reviews'])


def test_local_scan_prunes_ignored_directories_but_retains_exceptions(setup,monkeypatch):
    from pcloud_tools.event_sync import Scope
    cfg,_=setup
    cfg.manager_ignore_file.write_text('Documents/vendor/**\n!Documents/vendor/keep.txt\nDocuments/cache/**\n')
    put(cfg.core_dir,'Documents/vendor/drop.txt');put(cfg.core_dir,'Documents/vendor/keep.txt');put(cfg.core_dir,'Documents/cache/large.txt')
    visited=[];walk=os.walk
    def tracked(*a,**kw):
        for item in walk(*a,**kw):visited.append(Path(item[0]).name);yield item
    monkeypatch.setattr(os,'walk',tracked)
    assert Scope(cfg).local_paths()=={'Documents/vendor/keep.txt'}
    assert 'cache' not in visited


def test_scope_keeps_last_matching_ignore_rule_semantics(setup):
    from pcloud_tools.event_sync import Scope
    cfg, _ = setup
    cfg.manager_ignore_file.write_text('Documents/**\n!Documents/keep/**\nDocuments/keep/private/**\n!Documents/keep/private/readme.md\n')
    scope = Scope(cfg)
    assert not scope.allows('Documents/drop.txt')
    assert scope.allows('Documents/keep/public.txt')
    assert not scope.allows('Documents/keep/private/key.txt')
    assert scope.allows('Documents/keep/private/readme.md')


def test_automation_requires_all_gates_before_engine_and_preview_is_read_only(setup,monkeypatch):
    from pcloud_tools import cli_service_daemon as cli
    from pcloud_tools.gates import GATES
    cfg,r=setup;cfg.sync_policy='event';cfg.diffd_download_mode='event'
    loaded=SimpleNamespace(config=cfg,issues=[])
    args=SimpleNamespace(execute=True,consume_on_success=True,max_records=100,report_path='fixture')
    monkeypatch.setattr(cli,'_shadow_report_check',lambda *a,**k:({'status':'ok'},[]))
    monkeypatch.setattr(cli,'_service_actions',lambda *a:[])
    monkeypatch.setattr(cli,'_resolve_real_rclone_bin',lambda c:('fixture-rclone',None))
    import pcloud_tools.event_sync_remote as remote_module
    monkeypatch.setattr(remote_module,'RcloneRemote',lambda *a:r)
    for name in ['real_transfer.execution','real_transfer.automation','real_transfer.automation-run']:
        monkeypatch.setenv(GATES[name].env_var,GATES[name].expected_value)
    missing=GATES['real_transfer.automation-run'].env_var
    monkeypatch.delenv(missing)
    refused=cli._event_sync_automation_report(args,None,SimpleNamespace(name='pushd'),loaded)
    assert refused.status=='error' and not cfg.state_dir.exists()
    monkeypatch.setenv(missing,GATES['real_transfer.automation-run'].expected_value)
    args.execute=False
    assert cli._event_sync_automation_report(args,None,SimpleNamespace(name='pushd'),loaded).status=='ok'
    assert not cfg.state_dir.exists()
    args.execute=True
    put(cfg.core_dir,'Documents/a',b'local')
    result=cli._event_sync_automation_report(args,None,SimpleNamespace(name='pushd'),loaded)
    assert result.status=='ok' and (r.root/'Documents/a').read_bytes()==b'local'


def test_encoded_api_change_reconciles_the_actual_native_name(setup):
    from pcloud_tools.diffd_events import parse_diff_response_text,diff_changes_to_records
    cfg,r,path=initial(setup,'Documents/line\nname.txt')
    put(r.root,path,b'cloud edit',200)
    parsed=parse_diff_response_text(json.dumps({'diffid':2,'entries':[{'event':'modifyfile',
        'metadata':{'path':'Documents/line␊name.txt','fileid':1,'modified':200,'size':10}}]}),'fixture')
    assert parsed.requires_reconciliation and not diff_changes_to_records(parsed.changes)
    request_reconciliation(cfg,'cloud API name requires rclone resolution')
    result=EventSync(cfg,r).tick()
    assert (cfg.core_dir/path).read_bytes()==b'cloud edit'
    assert result['results'][0]['action']=='download'
    assert not (cfg.core_dir/'Documents/line␊name.txt').exists()


def test_temporary_trace_follows_real_reconciliation_and_stops(setup):
    from pcloud_tools import sync_trace
    cfg, remote = setup
    put(cfg.core_dir, 'Documents/traced.txt')
    engine = EventSync(cfg, remote)
    engine.prepare_reconciliation(engine.snapshots())
    target = engine.state['reconcile']['id']
    sync_trace.start(cfg.state_dir, target)
    EventSync(cfg, remote).tick()
    data = sync_trace.report(cfg.state_dir)
    assert data['status'] == 'completed'
    assert data['metrics']['batch']['calls'] == 1
    assert data['metrics']['local-stat-hash']['calls'] > 0
    assert 'traced.txt' not in Path(data['log']).read_text()
