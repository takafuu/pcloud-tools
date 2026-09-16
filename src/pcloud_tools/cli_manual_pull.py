"""Discovery and explicit actions for the cloud-change inbox."""
from . import conflict_resolution as cr, manual_pull as mp
from .config import load_config
from .service_daemon_plan import PlanRecord, build_pushd_plan_from_records, build_diffd_plan_from_records
from .transfer_state import read_queue_snapshot


def add_parser(subparsers):
    parser = subparsers.add_parser('manual', help='Review cloud changes and explicitly pull one version, or keep the local version.')
    commands = parser.add_subparsers(dest='manual_command', required=True)
    listing = commands.add_parser('list', help='List queued cloud paths without network access. Select a path to inspect both versions.')
    listing.add_argument('--json', action='store_true')
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
    files = cr.queue_paths(config)
    def validate(path):
        push = build_pushd_plan_from_records(config, files['pushd'], (PlanRecord(path, 'upload', 'manual review'),))
        pull = build_diffd_plan_from_records(config, files['diffd'], files['pending'], (PlanRecord(path, 'download', 'manual review'),))
        if not push.upload_records or not pull.download_records:
            raise cr.ResolutionError('path is excluded by current sync policy')
    if args.manual_command == 'list':
        snapshot = read_queue_snapshot(files['diffd'])
        if snapshot.issue:
            raise cr.ResolutionError(snapshot.issue.message)
        records = []
        for path in sorted({r.path for r in snapshot.records}):
            try:
                validate(path)
            except cr.ResolutionError:
                continue
            reason = ''
            try:
                target = cr.safe_local(config, path)
                mp.selected_queues(config, path)
                stat = target.stat() if target.exists() else None
                local = {'exists': bool(stat), 'size': stat.st_size if stat else None,
                         'mtime_ns': stat.st_mtime_ns if stat else None}
            except (OSError, cr.ResolutionError) as exc:
                reason, local = str(exc), None
            records.append({'path': path, 'available': not reason, 'reason': reason, 'local': local})
        return {'records': records, 'count': len(records), 'download mode': config.diffd_download_mode}
    remote = cr.RemoteReader(config)
    if args.manual_command == 'preview' or not getattr(args, 'execute', False):
        result = mp.preview(config, args.path, args.choice, remote, validate)
        return {k: v for k, v in result.items() if k != 'queues'}
    return mp.apply(config, args.path, args.choice, args.token, remote, validate)
