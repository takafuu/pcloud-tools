"""Temporary reconciliation tracing controls."""
import json
import sqlite3
import os
from .config import load_config
from . import sync_trace


def add_trace_parser(subparsers):
    parser = subparsers.add_parser('trace', help='Record bounded timing diagnostics for the current reconciliation.')
    commands = parser.add_subparsers(dest='trace_command', required=True)
    for name in ('start', 'stop', 'status', 'report', 'doctor'):
        sub = commands.add_parser(name)
        sub.add_argument('--json', action='store_true', help='Emit machine-readable JSON.')
        if name in ('start', 'stop'):
            sub.add_argument('--execute', action='store_true', help='Apply the trace control change; otherwise preview.')
        if name == 'start':
            sub.add_argument('--until-reconciled', action='store_true', required=True,
                             help='Stop after this reconciliation, at 7 days, or 16 MiB. Does not restart reconciliation.')


def cmd_trace(args, paths):
    config = load_config(paths).config
    try:
        name = args.trace_command
        data = sync_trace.status(config.state_dir)
        if name == 'start':
            from .event_sync import read_state
            target = (read_state(config).get('reconcile') or {}).get('id')
            if not target:
                raise ValueError('No active reconciliation to trace.')
            data = sync_trace.start(config.state_dir, target) if args.execute else {**data, 'preview': 'Start tracing the current reconciliation; maximum 7 days / 16 MiB.'}
        elif name == 'stop':
            data = sync_trace.stop(config.state_dir) if args.execute else {**data, 'preview': 'Stop recording; retain existing logs.'}
        elif name == 'report':
            data = sync_trace.report(config.state_dir)
        elif name == 'doctor':
            directory = sync_trace.folder(config.state_dir)
            if directory.exists() and (directory.is_symlink() or not os.access(directory, os.R_OK | os.W_OK)):
                raise ValueError('Trace directory is not safely accessible.')
            data['check'] = 'ok'
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print('Sync trace: ' + data['status'])
            if data.get('preview'): print(data['preview'])
            if data.get('log'): print('Log: ' + data['log'])
            if data.get('expires_at'): print('Expires: ' + data['expires_at'])
            for operation, metric in data.get('metrics', {}).items():
                print(f'{operation}: {metric["calls"]} calls, wall {metric["wall_seconds"]:.3f}s, Python CPU {metric["python_cpu_seconds"]:.3f}s, child CPU {metric["child_cpu_seconds"]:.3f}s')
            if data.get('note'): print(data['note'])
        return 0
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        if args.json: print(json.dumps({'status': 'error', 'message': str(exc)}))
        else: print('ERROR: ' + str(exc))
        return 1
