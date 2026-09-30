import json
from pathlib import Path

from pcloud_tools.event_sync_watch import parse_record, records_for_batch, append_records
from test_event_sync import setup, put


def test_nul_record_keeps_tabs_newlines_spaces_and_rename_flags(tmp_path):
    old='a\n tab\t .txt';new=' renamed\n.txt '
    p=put(tmp_path,old)
    inode=p.stat().st_ino;p.rename(tmp_path/new)
    one=parse_record(f'{inode}\tUpdated,Renamed,IsFile\t{tmp_path/old}'.encode(),tmp_path)
    two=parse_record(f'{inode}\tRenamed,IsFile\t{tmp_path/new}'.encode(),tmp_path)
    records=records_for_batch([one,two],tmp_path)
    assert records[0]['path']==old and records[0]['destination']==new
    assert records[1]['path']==new and records[1]['action']=='upload'


def test_ambiguous_rename_never_invents_destination(tmp_path):
    events=[{'path':p,'file_id':99,'flags':{'Renamed','IsFile'}} for p in ['a','b','c']]
    assert all(r['action']=='delete' for r in records_for_batch(events,tmp_path))
    assert all('destination' not in r for r in records_for_batch(events,tmp_path))


def test_same_path_new_event_replaces_generation_without_losing_move(setup):
    cfg,_=setup
    append_records(cfg,[{'path':'Documents/a','action':'upload'}])
    file=cfg.state_dir/'pushd/queue.json';old=json.loads(file.read_text())[0]['event_id']
    append_records(cfg,[{'path':'Documents/a','action':'move','destination':'Documents/b'},
                        {'path':'Documents/a','action':'upload'}])
    items=json.loads(file.read_text())
    assert len(items)==2 and old not in [r['event_id'] for r in items]
    assert {r['action'] for r in items}=={'move','upload'}


def test_overflow_requests_reconciliation_instead_of_silent_drop(setup):
    cfg,_=setup;cfg.pushd_queue_limit=1
    append_records(cfg,[{'path':'Documents/a','action':'upload'},{'path':'Documents/b','action':'upload'}])
    request=json.loads((cfg.state_dir/'event-sync/reconcile-request.json').read_text())
    assert request['reason']=='local event queue overflow'


def test_real_fsevents_move_and_edit_retains_both_events(setup):
    import os, subprocess, sys, time
    import pytest
    cfg,_=setup
    binary=subprocess.run(['zsh','-c','command -v fswatch'],capture_output=True,text=True).stdout.strip()
    if sys.platform!='darwin' or not binary:pytest.skip('macOS fswatch required')
    old=put(cfg.core_dir,'Documents/old.txt')
    config_file=cfg.state_dir.parent/'watch-config.json'
    config_file.write_text(json.dumps({k:str(v) if isinstance(v,Path) else v for k,v in vars(cfg).items()}))
    source="""import json,sys
from pathlib import Path
from types import SimpleNamespace
from pcloud_tools.event_sync_watch import run
c=json.loads(Path(sys.argv[1]).read_text())
for k in ['core_dir','state_dir','allowlist_file','manager_ignore_file']:c[k]=Path(c[k])
run(SimpleNamespace(**c),sys.argv[2])
"""
    process=subprocess.Popen([sys.executable,'-c',source,str(config_file),binary],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        deadline=time.monotonic()+5
        marker=cfg.state_dir/'pushd/fswatch-resident-last-run.json'
        while not marker.exists() and time.monotonic()<deadline:time.sleep(.05)
        assert marker.exists()
        time.sleep(.7)
        new=cfg.core_dir/'Documents/new.txt';old.rename(new);new.write_bytes(b'edited after rename')
        queue=cfg.state_dir/'pushd/queue.json';records=[]
        while time.monotonic()<deadline:
            if queue.exists():records=json.loads(queue.read_text())
            if any(r['action']=='move' for r in records):break
            time.sleep(.1)
        assert any(r['action']=='move' and r['destination']=='Documents/new.txt' for r in records)
        assert any(r['action']=='upload' and r['path']=='Documents/new.txt' for r in records)
    finally:
        process.terminate()
        stdout,stderr=process.communicate(timeout=5)
    assert process.returncode==0,stderr.decode()
