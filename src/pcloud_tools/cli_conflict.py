"""Public conflict resolution commands used by the xbar operator UI."""
from . import conflict_resolution as cr
from .config import load_config
from .service_daemon_plan import PlanRecord, build_pushd_plan_from_records, build_diffd_plan_from_records
from .transfer_state import read_queue_snapshot


def add_parser(subparsers):
    parser = subparsers.add_parser('resolve', help='Resolve one both-side file conflict after version-bound confirmation; preserve both originals.')
    commands = parser.add_subparsers(dest='resolve_command', required=True)
    listing = commands.add_parser('list', help='List queued both-side conflicts; read-only.')
    listing.add_argument('--json', action='store_true')
    for name in ('preview', 'apply'):
        child = commands.add_parser(name, help='Inspect current versions.' if name == 'preview' else 'Save both originals and release the selected transfer direction.')
        child.add_argument('--path', required=True)
        child.add_argument('--strategy', required=True, choices=['local', 'cloud', 'both'])
        if name == 'apply':
            child.add_argument('--token', required=True, help='Exact token from preview; stale versions are refused.')
            child.add_argument('--execute', action='store_true', help='Apply the approved queue change. No immediate remote writes.')
        child.add_argument('--json', action='store_true')


def run(args, paths):
    loaded = load_config(paths)
    errors = [issue.message for issue in loaded.issues if issue.level == 'error']
    if errors:
        raise cr.ResolutionError('; '.join(errors))
    config = loaded.config
    files = cr.queue_paths(config)

    def validate(path, sibling=False):
        push = build_pushd_plan_from_records(config, files['pushd'], (PlanRecord(path, 'upload', 'resolution'),))
        pull = build_diffd_plan_from_records(config, files['diffd'], files['pending'], (PlanRecord(path, 'download', 'resolution'),))
        if not push.upload_records or (not sibling and not pull.download_records):
            raise cr.ResolutionError('path is excluded or suppressed by current sync policy')

    if args.resolve_command == 'list':
        snapshots = {key: read_queue_snapshot(file) for key, file in files.items()}
        for snapshot in snapshots.values():
            if snapshot.issue:
                raise cr.ResolutionError(snapshot.issue.message)
        uploads = {r.path for r in snapshots['pushd'].records}
        downloads = {r.path for r in snapshots['diffd'].records}
        records = []
        for path in sorted(uploads & downloads):
            reason = ''
            try:
                cr.safe_local(config, path)
                cr.queues(config, path)
                validate(path)
            except (OSError, cr.ResolutionError) as exc:
                reason = str(exc)
            records.append({'path': path, 'available': not reason, 'reason': reason})
        return {'records': records, 'count': len(records)}
    remote = cr.RemoteReader(config)
    if args.resolve_command == 'preview' or not getattr(args, 'execute', False):
        result = cr.preview(config, args.path, args.strategy, remote, validate)
        # The token includes raw generations but the UI needs no private queue payload.
        return {key: value for key, value in result.items() if key != 'queues'}
    return cr.apply(config, args.path, args.strategy, args.token, remote, validate)
