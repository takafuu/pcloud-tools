from pcloud_tools.review_worker import ReviewWorker
import json
import sqlite3
from pathlib import Path
import pytest
from test_event_sync import setup, put, Remote
from pcloud_tools.event_sync import EventSync, read_state, request_reconciliation
from pcloud_tools.event_sync_watch import append_records
from pcloud_tools.sqlite_state import Store, DB_NAME
from pcloud_tools.state_migration import migrate, JsonStream
from pcloud_tools.io_utils import atomic_write_json, read_json_state
from pcloud_tools.transfer_state import (
    create_attempt,
    update_attempt,
    unresolved_attempts,
    mark_attempt_child,
    clear_attempt_child,
    consume_event_ids,
    writer_process_session,
)
from pcloud_tools.service_daemon_plan import PlanRecord, append_plan_record


def migrated(cfg):
    migrate(cfg.state_dir)
    return Store(cfg.state_dir / DB_NAME)


def test_preserves_state_and_backup_then_restarts_remaining(setup):
    cfg, remote = setup
    for i in range(5):
        put(cfg.core_dir, f"Documents/{i}", bytes([i]))
        put(remote.root, f"Documents/{i}", bytes([i]))
    EventSync(cfg, remote).tick(max_records=2)
    before = json.loads((cfg.state_dir / "event-sync/state.json").read_text())
    report = migrate(cfg.state_dir)
    state = read_state(cfg)
    assert dict(state["baseline"]) == before["baseline"]
    assert list(state["reconcile"]["pending"]) == before["reconcile"]["pending"]
    backup = Path(report["backup"]) / "event-sync/state.json"
    assert json.loads(backup.read_text()) == before
    assert migrate(cfg.state_dir)["already_migrated"]
    result = EventSync(cfg, remote).tick(max_records=2)
    assert result["reconcile remaining"] == 1
    assert EventSync(cfg, remote).tick(max_records=2)["reconcile remaining"] == 0
    assert len(read_state(cfg)["baseline"]) == 5


def test_generation_cut_does_not_consume_event_during_inventory(setup):
    cfg, remote = setup
    store = migrated(cfg)
    put(cfg.core_dir, "Documents/a")
    put(remote.root, "Documents/a")
    append_records(cfg, [{"path": "Documents/a", "action": "upload"}])
    old = list(store.queue_rows("pushd"))[0]["event_id"]
    remote.before_inventory = lambda: append_records(
        cfg, [{"path": "Documents/a", "action": "upload"}]
    )
    EventSync(cfg, remote).tick()
    rows = list(store.queue_rows("pushd"))
    assert len(rows) == 1 and rows[0]["event_id"] != old


def test_upload_download_conflict_delete_and_new_event(setup):
    cfg, remote = setup
    store = migrated(cfg)
    p = "Documents/a"
    put(cfg.core_dir, p)
    put(remote.root, p)
    EventSync(cfg, remote).tick()
    put(cfg.core_dir, p, b"new", 200)
    append_records(cfg, [{"path": p, "action": "upload"}])
    assert EventSync(cfg, remote).tick()["results"][0]["action"] == "upload"
    assert (remote.root / p).read_bytes() == b"new"
    put(remote.root, p, b"cloud", 300)
    store.append("diffd", {"path": p, "action": "download", "event_id": "remote-1"})
    assert EventSync(cfg, remote).tick()["results"][0]["action"] == "download"
    assert (cfg.core_dir / p).read_bytes() == b"cloud"
    put(cfg.core_dir, p, b"left", 400)
    put(remote.root, p, b"right", 400)
    append_records(cfg, [{"path": p, "action": "upload"}])
    assert EventSync(cfg, remote).tick()["results"][0]["action"] == "upload"
    assert (remote.root / p).read_bytes() == b"left"
    (cfg.core_dir / p).unlink()
    append_records(cfg, [{"path": p, "action": "delete"}])
    assert EventSync(cfg, remote).tick()["results"][0]["action"] == "delete-cloud"
    assert not (remote.root / p).exists()


def test_attempt_updates_touch_only_selected_attempt(setup, monkeypatch):
    cfg, _ = setup
    store = migrated(cfg)
    a = create_attempt(cfg.state_dir, "pushd", [{"path": "Documents/a"}], concurrency=1)
    assert unresolved_attempts(cfg.state_dir, "pushd")[0]["attempt_id"] == a.attempt_id
    mark_attempt_child(cfg.state_dir, "pushd", a.attempt_id, 123)
    clear_attempt_child(cfg.state_dir, "pushd", a.attempt_id, 123)
    update_attempt(
        cfg.state_dir, "pushd", a.attempt_id, phase="completed", status="completed"
    )
    assert not unresolved_attempts(cfg.state_dir, "pushd")
    assert store.attempts("pushd")[0]["child_pids"] == []


def test_migration_rejects_unfinished_and_corruption_without_activation(setup):
    cfg, _ = setup
    attempt = create_attempt(cfg.state_dir, "pushd", [], concurrency=1)
    with pytest.raises(ValueError, match="unfinished"):
        migrate(cfg.state_dir)
    assert not (cfg.state_dir / DB_NAME).exists()
    update_attempt(
        cfg.state_dir, "pushd", attempt.attempt_id, phase="released", status="released"
    )
    p = cfg.state_dir / "event-sync/state.json"
    p.parent.mkdir(parents=True)
    p.write_text('{"schema":')
    with pytest.raises(ValueError):
        migrate(cfg.state_dir)
    assert not (cfg.state_dir / DB_NAME).exists()
    assert p.read_text() == '{"schema":'


def test_migration_refuses_live_writer(setup):
    cfg, _ = setup
    with writer_process_session(cfg.state_dir, "pushd"):
        with pytest.raises(RuntimeError):
            migrate(cfg.state_dir)
    assert not (cfg.state_dir / DB_NAME).exists()


def test_coalesce_latest_identity_preserves_unknown_and_unrelated(setup):
    cfg, _ = setup
    store = migrated(cfg)
    p = cfg.state_dir / "diffd/remote-changes.json"
    for path, diffid, ident in [
        ("Documents/old", "2", "a"),
        ("Documents/new", "3", "b"),
        ("Documents/stale", "1", "c"),
    ]:
        append_plan_record(
            p,
            "TEST",
            PlanRecord(
                path,
                "download",
                "fixture",
                event_id=ident,
                extra={"remote_file_id": "5", "diffid": diffid, "unknown": {"x": 1}},
            ),
            coalesce_remote_file=True,
        )
    assert [r["event_id"] for r in store.queue_rows("diffd")] == ["b"]
    assert list(store.queue_rows("diffd"))[0]["unknown"] == {"x": 1}
    result = consume_event_ids(p, ["a"])
    assert result.removed_count == 0
    assert consume_event_ids(p, ["b"]).removed_count == 1


def test_status_never_reads_large_json_or_completed_attempts(setup, monkeypatch):
    cfg, remote = setup
    migrated(cfg)
    put(cfg.core_dir, "Documents/a")
    put(remote.root, "Documents/a")
    EventSync(cfg, remote).tick()
    from pcloud_tools import event_sync_status as status

    monkeypatch.setattr(status, "observe_services", lambda _: ({}, []))

    def forbidden_attempt_load(*a, **kw):
        raise AssertionError("status must not load attempt payloads")

    monkeypatch.setattr(Store, "attempts", forbidden_attempt_load)
    original = Path.read_text

    def guarded(path, *a, **kw):
        if path.name in (
            "state.json",
            "queue.json",
            "remote-changes.json",
            "transfer-attempts.json",
        ):
            raise AssertionError("large legacy JSON read")
        return original(path, *a, **kw)

    monkeypatch.setattr(Path, "read_text", guarded)
    snapshot = status.snapshot(cfg)
    assert snapshot["remaining"] == 0 and snapshot["review_count"] == 0
    assert snapshot["queues"]["pushd"]["count"] == 0


def test_chunk_boundary_stream_and_truncated_rejected():
    import io

    value = {"a": "あ" * 70000, "nested": [1, 2, 3]}
    stream = JsonStream(io.StringIO(json.dumps([value, 123456789])))
    assert list(stream.array()) == [value, 123456789]
    stream.finish()
    with pytest.raises(ValueError):
        list(JsonStream(io.StringIO('[1, {"a":')).array())


def test_unchanged_local_reuses_hash_but_same_mtime_edit_does_not(setup, monkeypatch):
    cfg, remote = setup
    store = migrated(cfg)
    path = "Documents/a"
    put(cfg.core_dir, path, b"aa")
    put(remote.root, path, b"aa")
    EventSync(cfg, remote).tick()
    import pcloud_tools.event_sync as engine

    real = engine.local_version
    calls = []

    def count(*args, **kwargs):
        calls.append(args[0])
        return real(*args, **kwargs)

    monkeypatch.setattr(engine, "local_version", count)
    append_records(cfg, [{"path": path, "action": "upload"}])
    EventSync(cfg, remote).tick()
    assert calls == []
    put(cfg.core_dir, path, b"bb", 100)
    append_records(cfg, [{"path": path, "action": "upload"}])
    assert EventSync(cfg, remote).tick()["results"][0]["action"] == "upload"
    assert calls


def test_cloud_event_never_reuses_same_metadata_hash(setup):
    cfg, remote = setup
    store = migrated(cfg)
    path = "Documents/a"
    put(cfg.core_dir, path, b"aa")
    put(remote.root, path, b"aa")
    EventSync(cfg, remote).tick()
    put(remote.root, path, b"bb", 100)
    store.append("diffd", {"path": path, "action": "download", "event_id": "changed"})
    assert EventSync(cfg, remote).tick()["results"][0]["action"] == "upload"
    assert (cfg.core_dir / path).read_bytes() == b"aa"


def test_transaction_rollback_keeps_state_and_summary_together(setup, monkeypatch):
    cfg, remote = setup
    store = migrated(cfg)
    put(cfg.core_dir, "Documents/a")
    put(remote.root, "Documents/a")
    EventSync(cfg, remote).tick()
    saved = store.get("summary", "event")
    state = store.load_state()
    state["baseline"]["Documents/new"] = {"local": {"exists": True}}
    real = Store.put

    def fail(db, namespace, key, value):
        if namespace == "baseline":
            raise RuntimeError("disk failure fixture")
        return real(db, namespace, key, value)

    monkeypatch.setattr(Store, "put", staticmethod(fail))
    with pytest.raises(RuntimeError):
        store.save_state(state, {"bad": "summary"})
    assert store.get("summary", "event") == saved
    assert "Documents/new" not in store.load_state()["baseline"]


def test_corrupt_database_status_fails_closed(setup, monkeypatch):
    cfg, _ = setup
    migrated(cfg)
    (cfg.state_dir / DB_NAME).write_bytes(b"broken database")
    from pcloud_tools import event_sync_status as status

    monkeypatch.setattr(status, "observe_services", lambda _: ({}, []))
    snapshot = status.snapshot(cfg)
    assert snapshot["remaining"] is None
    assert snapshot["queues"]["diffd"]["count"] is None
    assert snapshot["issues"]


def test_transfer_rechecks_remote_even_when_decision_used_cached_hash(setup):
    cfg, remote = setup
    store = migrated(cfg)
    path = "Documents/a"
    put(cfg.core_dir, path, b"aa")
    put(remote.root, path, b"aa")
    EventSync(cfg, remote).tick()
    put(cfg.core_dir, path, b"cc", 200)
    put(remote.root, path, b"bb", 100)
    append_records(cfg, [{"path": path, "action": "upload"}])
    result = EventSync(cfg, remote).tick()
    assert result["results"][0]["action"] == "hold"
    assert (remote.root / path).read_bytes() == b"bb"


def unfinished(setup, total=12):
    cfg, remote = setup
    for i in range(total):
        path=f'Documents/old-{i:03d}'
        put(cfg.core_dir,path);put(remote.root,path)
    EventSync(cfg,remote).tick(max_records=1)
    return cfg,remote,migrated(cfg)


def test_new_local_and_cloud_files_run_before_full_reconcile(setup):
    cfg,remote,store=unfinished(setup)
    put(cfg.core_dir,'Documents/new-local',b'local')
    put(remote.root,'Documents/new-cloud',b'cloud')
    append_records(cfg,[{'path':'Documents/new-local','action':'upload'}])
    store.append('diffd',{'path':'Documents/new-cloud','action':'download','event_id':'cloud-new'})
    result=EventSync(cfg,remote).tick(max_records=4)
    assert (remote.root/'Documents/new-local').read_bytes()==b'local'
    assert (cfg.core_dir/'Documents/new-cloud').read_bytes()==b'cloud'
    assert 0 < result['reconcile remaining'] < 11
    assert store.count('queue','pushd')==store.count('queue','diffd')==0


def test_continuous_arrivals_do_not_starve_backlog_and_single_slot_alternates(setup):
    cfg,remote,store=unfinished(setup)
    before=len(read_state(cfg)['reconcile']['pending'])
    for i in range(4):
        path=f'Documents/new-{i}'
        put(cfg.core_dir,path)
        append_records(cfg,[{'path':path,'action':'upload'}])
        result=EventSync(cfg,remote).tick(max_records=1)
        assert len(result['results'])<=1
    assert len(read_state(cfg)['reconcile']['pending'])<before
    assert remote.copies


def test_pending_delete_without_baseline_holds_instead_of_resurrecting(setup):
    cfg,remote,store=unfinished(setup)
    path='Documents/old-010'
    (cfg.core_dir/path).unlink()
    append_records(cfg,[{'path':path,'action':'delete'}])
    EventSync(cfg,remote).tick(max_records=4)
    assert not (cfg.core_dir/path).exists()
    assert not (remote.root/path).exists()
    assert path not in read_state(cfg)['reviews']
    assert path not in list(read_state(cfg)['reconcile']['pending'])


def test_pending_delete_with_verified_baseline_is_not_restored(setup):
    cfg,remote,store=unfinished(setup)
    path='Documents/old-000'  # already verified by the first batch
    (cfg.core_dir/path).unlink()
    append_records(cfg,[{'path':path,'action':'delete'}])
    EventSync(cfg,remote).tick(max_records=4)
    assert not (cfg.core_dir/path).exists() and not (remote.root/path).exists()


def test_same_path_event_arriving_during_transfer_survives_interleave(setup):
    cfg,remote,store=unfinished(setup)
    path='Documents/fresh'
    put(cfg.core_dir,path)
    append_records(cfg,[{'path':path,'action':'upload'}])
    old=list(store.queue_rows('pushd'))[0]['event_id']
    remote.after_copy=lambda: append_records(cfg,[{'path':path,'action':'upload'}])
    EventSync(cfg,remote).tick(max_records=4)
    rows=list(store.queue_rows('pushd'))
    assert len(rows)==1 and rows[0]['event_id']!=old


def test_sqlite_queue_keeps_new_files_past_old_json_limit(setup):
    cfg,remote,store=unfinished(setup)
    cfg.pushd_queue_limit=1
    append_records(cfg,[{'path':f'Documents/new-{i}','action':'upload'} for i in range(3)])
    assert store.count('queue','pushd')==3


def test_structural_move_during_reconcile_does_not_restore_old_name(setup):
    cfg,remote,store=unfinished(setup)
    old='Documents/old-010';new='Documents/moved'
    (cfg.core_dir/old).rename(cfg.core_dir/new)
    append_records(cfg,[{'path':old,'action':'move','destination':new,'file_id':(cfg.core_dir/new).stat().st_ino,'is_dir':False}])
    EventSync(cfg,remote).tick(max_records=4)
    assert (remote.root/new).exists() and not (remote.root/old).exists()
    assert not (cfg.core_dir/old).exists()


def test_priority_sampling_includes_recent_and_old_and_skips_unchanged_holds(setup):
    cfg,remote,store=unfinished(setup)
    for i in range(20):store.append('pushd',{'path':f'Documents/event-{i:02d}','action':'upload','event_id':str(i)})
    paths=store.priority_paths(8,lambda p:True,{'Documents/event-19':{'event_ids':{'pushd':['19']}}})
    assert 'Documents/event-18' in paths and 'Documents/event-00' in paths
    assert 'Documents/event-19' not in paths


def test_delete_arriving_during_background_inventory_is_deferred(setup):
    cfg,remote,store=unfinished(setup)
    path='Documents/old-001'
    def deleted():
        (cfg.core_dir/path).unlink()
        append_records(cfg,[{'path':path,'action':'delete'}])
    remote.before_inventory=deleted
    result=EventSync(cfg,remote).tick(max_records=4)
    assert not (cfg.core_dir/path).exists()
    assert any(r['path']==path and r['action']=='waiting' for r in result['results'])
    assert path in list(read_state(cfg)['reconcile']['pending'])
    assert list(store.queue_rows('pushd',[path]))


def test_interleaved_failure_keeps_unprocessed_queue_and_resumes(setup,monkeypatch):
    cfg,remote,store=unfinished(setup)
    path='Documents/new'
    put(cfg.core_dir,path)
    append_records(cfg,[{'path':path,'action':'upload'}])
    engine=EventSync(cfg,remote)
    def interrupted(*args,**kwargs):raise RuntimeError('interrupted')
    monkeypatch.setattr(engine,'current_cloud',interrupted)
    with pytest.raises(RuntimeError):engine.tick(max_records=4)
    assert store.count('queue','pushd')==1
    assert len(read_state(cfg)['reconcile']['pending'])==11
    EventSync(cfg,remote).tick(max_records=4)
    assert (remote.root/path).exists()
    assert store.count('queue','pushd')==0


def stale_identity_fixture(setup):
    cfg,remote,store=unfinished(setup,total=20)
    path='Documents/old-000'
    store.append('diffd',{'path':path,'action':'download','remote_file_id':'obsolete-id','event_id':'old-id-event'})
    from pcloud_tools.event_sync import ID_MISMATCH
    state=read_state(cfg)
    state['reviews'][path]={'path':path,'reason':ID_MISMATCH,'event_ids':{'pushd':[],'diffd':['old-id-event']}}
    engine=EventSync(cfg,remote);engine.state=state;engine.save()
    return cfg,remote,store,path


def test_old_id_review_is_rechecked_and_cleared_without_transfer(setup):
    cfg,remote,store,path=stale_identity_fixture(setup)
    ReviewWorker(cfg,remote).tick()
    assert path not in read_state(cfg)['reviews']
    assert not list(store.queue_rows('diffd',[path]))
    assert not remote.copies and not remote.deletes and not remote.moves


@pytest.mark.parametrize('change',['local','cloud','missing-baseline','missing-cloud','unknown-action'])
def test_old_id_without_full_proof_stays_held(setup,change):
    cfg,remote,store,path=stale_identity_fixture(setup)
    if change=='local':put(cfg.core_dir,path,b'edited',200)
    elif change=='cloud':put(remote.root,path,b'cloud edit',200)
    elif change=='missing-cloud':(remote.root/path).unlink()
    elif change=='missing-baseline':
        engine=EventSync(cfg,remote);engine.state['baseline'].pop(path);engine.save()
    else:store.append('pushd',{'path':path,'action':'unknown','event_id':'unknown'})
    ReviewWorker(cfg,remote).tick()
    assert (path in read_state(cfg)['reviews']) is (change == 'unknown-action')
    assert list(store.queue_rows('diffd',[path]))
    assert not remote.copies and not remote.deletes


def test_stale_delete_for_replaced_object_does_not_delete_verified_current_file(setup):
    cfg,remote,store,path=stale_identity_fixture(setup)
    store.replace_queue('diffd',[{'path':path,'action':'delete','remote_file_id':'obsolete-id','event_id':'old-id-event'}])
    ReviewWorker(cfg,remote).tick()
    assert (cfg.core_dir/path).exists() and (remote.root/path).exists()
    assert path not in read_state(cfg)['reviews']
    assert not remote.deletes


def test_recheck_failure_preserves_review_and_queue(setup):
    from pcloud_tools.event_sync_remote import SyncError
    cfg,remote,store,path=stale_identity_fixture(setup)
    remote.before_inventory=lambda: (_ for _ in ()).throw(SyncError('inventory failed'))
    assert ReviewWorker(cfg,remote).tick()['status']=='failed'
    assert path in read_state(cfg)['reviews']
    assert list(store.queue_rows('diffd',[path]))


def test_review_scan_is_bounded_and_rotates(setup):
    cfg,remote,store,path=stale_identity_fixture(setup)
    from pcloud_tools.event_sync import ID_MISMATCH
    a,cursor=store.review_candidates(ID_MISMATCH,None,1,lambda p:True)
    b,_=store.review_candidates(ID_MISMATCH,cursor,1,lambda p:True)
    assert a==b==[path]


def test_changed_identity_can_be_resolved_with_fresh_manual_token(setup):
    cfg,remote,store,path=stale_identity_fixture(setup)
    put(remote.root,path,b'new cloud version',100)
    ReviewWorker(cfg,remote).tick()
    engine=EventSync(cfg,remote);engine.hold(path,'manual fixture',engine.snapshots(),engine.local(path),remote.inventory([path])[path]);engine.save()
    preview=EventSync(cfg,remote).review_preview(path,'pull')
    EventSync(cfg,remote).tick(manual={'path':path,'choice':'pull','token':preview['token']})
    assert (cfg.core_dir/path).read_bytes()==b'new cloud version'
    assert path not in read_state(cfg)['reviews']


def test_recheck_cli_previews_then_requests_without_choosing_versions(setup,monkeypatch):
    from types import SimpleNamespace
    from pcloud_tools import cli_manual_pull
    cfg,remote,store,path=stale_identity_fixture(setup)
    cfg.sync_policy='event'
    cfg.diffd_download_mode='auto'
    monkeypatch.setattr(cli_manual_pull,'load_config',lambda paths:SimpleNamespace(config=cfg,issues=[]))
    request=cfg.state_dir/'event-sync/review-recheck-request.json'
    args=SimpleNamespace(manual_command='recheck',execute=False)
    result=cli_manual_pull.run(args,None)
    row=next(r for r in result['rechecks'] if r['path']==path)
    assert row['needs_recheck'] and not row['available']
    assert not request.exists()
    args.execute=True
    cli_manual_pull.run(args,None)
    assert request.exists() and list(store.queue_rows('diffd',[path]))
    ReviewWorker(cfg,remote).tick()
    assert store.get('review-worker','state')['request_id']==json.loads(request.read_text())['id']
    assert path not in read_state(cfg)['reviews']
