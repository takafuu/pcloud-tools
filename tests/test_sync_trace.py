import json
from pathlib import Path
import pytest
from pcloud_tools import sync_trace as t


def test_disabled_has_no_writes(tmp_path):
    recorder = t.Recorder(tmp_path)
    with recorder.span('local-stat-hash'):
        pass
    recorder.finish({}, True)
    assert not list(tmp_path.iterdir())


def test_bound_completed_and_private_aggregate(tmp_path):
    t.start(tmp_path, 'initial')
    r = t.Recorder(tmp_path)
    r.set_phase('comparing')
    with r.span('batch', detail=True):
        with r.span('local-stat-hash'):
            sum(range(500))
    r.finish({'reconciled_request':'initial'}, True)
    data = t.report(tmp_path)
    assert data['status'] == 'completed'
    assert data['finished_batches'] == 1
    assert data['metrics']['local-stat-hash']['calls'] == 1
    assert Path(data['log']).stat().st_mode & 0o777 == 0o600
    assert 'command' not in Path(data['log']).read_text()


def test_exception_records_type_not_message_and_preserves_retry(tmp_path):
    t.start(tmp_path, 'initial')
    r = t.Recorder(tmp_path)
    with pytest.raises(RuntimeError):
        with r.span('rclone-copy', detail=True):
            raise RuntimeError('PRIVATE_SECRET_PATH')
    r.finish({'reconcile':{'id':'initial'}}, False)
    data=t.status(tmp_path)
    assert data['status']=='active'
    text=Path(data['log']).read_text()
    assert 'RuntimeError' in text and 'PRIVATE_SECRET_PATH' not in text


def test_limits_stop_and_expiry(tmp_path, monkeypatch):
    t.start(tmp_path, 'initial')
    monkeypatch.setattr(t,'MAX_BYTES',1)
    t.Recorder(tmp_path).emit('test')
    assert t.status(tmp_path)['status']=='size-limit'
    data=t.start(tmp_path, 'second')
    data['expires_at']='2000-01-01T00:00:00+00:00'
    t.write(t.folder(tmp_path),data)
    assert t.Recorder(tmp_path).session is None
    assert t.status(tmp_path)['status']=='expired'


def test_manual_stop_and_replaced_target(tmp_path):
    t.start(tmp_path,'first')
    r=t.Recorder(tmp_path)
    with pytest.raises(ValueError): t.start(tmp_path,'first')
    t.stop(tmp_path)
    r.emit('late')
    assert t.status(tmp_path)['bytes']==0
    t.start(tmp_path,'first')
    t.Recorder(tmp_path).finish({'reconcile':{'id':'second'}},True)
    assert t.status(tmp_path)['status']=='replaced'


def test_corrupt_diagnostic_does_not_break_sync(tmp_path):
    directory=t.folder(tmp_path);directory.mkdir(parents=True)
    (directory/'session.json').write_text('{broken')
    with t.Recorder(tmp_path).span('batch'):
        pass


def test_rclone_arguments_never_logged(tmp_path):
    from pcloud_tools.event_sync_remote import RcloneRemote
    t.start(tmp_path,'initial')
    remote=object.__new__(RcloneRemote)
    remote.trace=t.Recorder(tmp_path)
    remote._run=lambda argv: {'returncode':0}
    remote.run(['lsjson','secret:PRIVATE_NAME','--password','PRIVATE_TOKEN'])
    text=Path(t.status(tmp_path)['log']).read_text()
    assert 'rclone-lsjson' in text and 'PRIVATE' not in text
