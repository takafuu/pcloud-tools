"""Discovery and explicit actions for the cloud-change inbox."""
from . import conflict_resolution as cr, manual_pull as mp
from .config import load_config
from . import manual_batch
import json
import sys
from .service_daemon_plan import PlanRecord, build_pushd_plan_from_records, build_diffd_plan_from_records
from .transfer_state import read_queue_snapshot


def add_parser(subparsers):
    parser = subparsers.add_parser('manual', help='Review cloud changes and explicitly pull one version, or keep the local version.')
    commands = parser.add_subparsers(dest='manual_command', required=True)
    archives = commands.add_parser('archives', help='List losing versions kept in the local .conflict archive.')
    archives.add_argument('--json', action='store_true')
    archives.add_argument('--open', action='store_true', help='Open the local archive folder in Finder.')
    listing = commands.add_parser('list', help='List queued cloud paths without network access. Select a path to inspect both versions.')
    listing.add_argument('--json', action='store_true')
    recheck = commands.add_parser('recheck', help='Request background verification of stale cloud identities; never choose a version. Preview by default.')
    recheck.add_argument('--execute', action='store_true')
    recheck.add_argument('--json', action='store_true')
    worker = commands.add_parser('recheck-run', help='Run one independent verification batch; no file transfers. Preview by default.')
    worker.add_argument('--execute', action='store_true')
    worker.add_argument('--json', action='store_true')
    worker.add_argument('--max-records', type=int, default=100)
    batch = commands.add_parser('batch', help='Versioned JSON selections: preview first, then apply the returned details.')
    batch_commands = batch.add_subparsers(dest='batch_command', required=True)
    for operation in ('preview', 'apply'):
        child = batch_commands.add_parser(operation)
        child.add_argument('--input', required=True, help='JSON file or - for stdin; schema pcloud-manual-batch.v1, items[{path,choice}].')
        child.add_argument('--json', action='store_true')
        child.add_argument('--progress-jsonl', action='store_true', help='Emit item progress as JSON lines to stderr; stdout remains one report.')
        if operation == 'apply':
            child.add_argument('--execute', action='store_true', help='Required to apply preview tokens; stops after the first failure.')
    for name in ('preview', 'apply'):
        child = commands.add_parser(name, help='Inspect both versions and obtain a token.' if name == 'preview' else 'Apply exactly the reviewed version; preserve originals.')
        child.add_argument('--path', required=True)
        child.add_argument('--choice', choices=['pull', 'local'], required=True)
        child.add_argument('--json', action='store_true')
        if name == 'apply':
            child.add_argument('--token', required=True)
            child.add_argument('--execute', action='store_true')


def run(args, paths):
    loaded = load_config(paths)
    errors = [i.message for i in loaded.issues if i.level == 'error']
    if errors:
        raise cr.ResolutionError('; '.join(errors))
    config = loaded.config
    if args.manual_command == 'archives':
        from .conflict_archive import listing, safe_root
        if args.open:
            import subprocess
            subprocess.run(['/usr/bin/open', str(safe_root(config))], check=True)
        return listing(config)
    files = cr.queue_paths(config)
    def validate(path):
        push = build_pushd_plan_from_records(config, files['pushd'], (PlanRecord(path, 'upload', 'manual review'),))
        pull = build_diffd_plan_from_records(config, files['diffd'], files['pending'], (PlanRecord(path, 'download', 'manual review'),))
        if not push.upload_records or not pull.download_records:
            raise cr.ResolutionError('path is excluded by current sync policy')
    if args.manual_command == 'recheck-run':
        if config.sync_policy != 'event' or args.max_records <= 0:
            raise cr.ResolutionError('イベント同期と正の件数上限が必要です')
        if not args.execute:
            return {'execute': False, 'message': '同期と独立した再確認を実行します。ファイル転送は行いません。'}
        from .review_worker import ReviewWorker
        from .event_sync_remote import RcloneRemote
        return ReviewWorker(config, RcloneRemote(config)).tick(args.max_records)
    recheck_message = None
    if args.manual_command == 'recheck':
        if config.sync_policy != 'event':
            raise cr.ResolutionError('再確認はイベント同期で利用できます')
        recheck_message = '古いクラウド情報の再確認を依頼します。ファイルの採用・削除は行いません。'
        if args.execute:
            from .io_utils import atomic_write_json
            from .event_sync import now
            import uuid
            atomic_write_json(config.state_dir / 'event-sync' / 'review-recheck-request.json',
                              {'id': uuid.uuid4().hex, 'requested_at': now()})
            recheck_message = '再確認を依頼しました。同期とは独立して順次確認します。「一覧を更新」で結果を確認してください。'
    if args.manual_command in ('list', 'recheck'):
        snapshot = read_queue_snapshot(files['diffd']) if config.sync_policy != 'event' else None
        if snapshot is not None and snapshot.issue:
            raise cr.ResolutionError(snapshot.issue.message)
        records = []
        event_reviews = {}
        state_summary = {}
        if config.sync_policy == 'event':
            from .event_sync import read_state, state_path
            from .event_sync_status import review_entries, saved_summary, timestamp
            from .sqlite_state import active
            if not active(config.state_dir) and not state_path(config).is_file():
                raise cr.ResolutionError('同期の保存状態は未取得です')
            saved = read_state(config)
            event_reviews = review_entries(config, saved)
            state_summary = {**saved_summary(config, saved), 'observed_at': timestamp()}
        diagnostics, rechecks = [], []
        from .review_classification import category
        listed = set(event_reviews) if config.sync_policy == 'event' else {r.path for r in snapshot.records}
        for path in sorted(listed):
            try:
                if config.sync_policy != 'event':
                    validate(path)
            except cr.ResolutionError:
                continue
            kind = category(event_reviews[path]) if config.sync_policy == 'event' else 'choice'
            reason = ''
            selected = {}
            try:
                target = cr.safe_local(config, path)
                selected = {} if config.sync_policy == 'event' else mp.selected_queues(config, path)
                if event_reviews.get(path,{}).get('structural'):
                    raise cr.ResolutionError(event_reviews[path]['reason'])
                if target.exists() and not target.is_file():
                    kind = 'diagnostic'
                    raise cr.ResolutionError('フォルダは同期処理が中のファイルを再確認します。版の採用操作は不要です。')
                stat = target.stat() if target.exists() else None
                local = {'exists': bool(stat), 'size': stat.st_size if stat else None,
                         'mtime_ns': stat.st_mtime_ns if stat else None}
            except (OSError, cr.ResolutionError) as exc:
                reason, local = str(exc), None
                kind = 'diagnostic'
            from .event_sync import ID_MISMATCH
            needs_recheck = event_reviews.get(path, {}).get('reason') == ID_MISMATCH
            if needs_recheck:
                reason = 'クラウド情報の再確認が必要です。「情報を再確認」を押してください。版の採用はまだ不要です。'
            destination = records if kind == 'choice' else rechecks if kind == 'recheck' else diagnostics
            destination.append({'path': path, 'available': kind == 'choice' and not reason, 'needs_recheck': needs_recheck,
                            'status': 'needs-recheck' if needs_recheck else None, 'reason': reason or event_reviews.get(path,{}).get('reason',''),
                            'local': local, 'conflict': bool(selected.get('pushd')), 'cloud': event_reviews.get(path,{}).get('cloud')})
        return {'records': records, 'diagnostics': diagnostics, 'rechecks': rechecks, 'message': recheck_message, 'count': len(records), 'download mode': config.diffd_download_mode,
                'sync policy':config.sync_policy, 'state snapshot': state_summary}
    if args.manual_command == 'batch':
        apply = args.batch_command == 'apply'
        if apply and not args.execute:
            raise ValueError('batch apply requires --execute and reviewed tokens')
        document = manual_batch.read_request(args.input, apply=apply)
        def progress(event):
            print(json.dumps(event, ensure_ascii=False), file=sys.stderr, flush=True)
        return manual_batch.run_batch(document, config, cr.RemoteReader(config), validate, apply=apply,
                                      progress=progress if args.progress_jsonl else None)
    remote = cr.RemoteReader(config)
    if args.manual_command == 'preview' or not getattr(args, 'execute', False):
        result = mp.preview(config, args.path, args.choice, remote, validate)
        return {k: v for k, v in result.items() if k != 'queues'}
    return mp.apply(config, args.path, args.choice, args.token, remote, validate)
