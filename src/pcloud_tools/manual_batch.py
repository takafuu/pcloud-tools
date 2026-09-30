"""Versioned JSON batch interface shared by GUI and headless clients."""
import json
import sys
from collections import Counter
from pathlib import Path
from . import manual_pull as mp
from .transfer_state import TransferStateError

SCHEMA = 'pcloud-manual-batch.v1'
MAX_BYTES = 16 * 1024 * 1024
MAX_ITEMS = 10000


def read_request(source, *, apply=False):
    if source == '-':
        raw = sys.stdin.buffer.read(MAX_BYTES + 1)
    else:
        with Path(source).open('rb') as handle:
            raw = handle.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError('batch JSON exceeds 16 MiB')
    document = json.loads(raw)
    if isinstance(document, dict) and document.get("schema_version") == "pcloud-tools-report.v1":
        document = document.get("details")
    return validate_request(document, apply=apply)


def validate_request(document, *, apply=False):
    if not isinstance(document, dict) or document.get('schema') != SCHEMA:
        raise ValueError('expected schema pcloud-manual-batch.v1')
    items = document.get('items')
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS:
        raise ValueError('items must contain 1..10000 selections')
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError('each selection must be an object')
        path = item.get('path')
        if not isinstance(path, str) or not path or '\x00' in path or path in seen:
            raise ValueError('paths must be nonempty, unique strings without NUL')
        seen.add(path)
        if item.get('choice') not in ('pull', 'local', 'hold'):
            raise ValueError('choice must be pull, local, or hold')
        if apply and item['choice'] != 'hold':
            token = item.get('token')
            if item.get('status') != 'ready' or not isinstance(token, str) or len(token) != 64 or any(c not in '0123456789abcdef' for c in token):
                raise ValueError('apply requires ready preview items with version-bound tokens')
    return document


def run_batch(document, config, remote, validate, *, apply=False, progress=None):
    # Validate the entire envelope before the first write, including trailing items.
    validate_request(document, apply=apply)
    results = []
    stopped = False
    for index, item in enumerate(document['items']):
        result = {'path': item['path'], 'choice': item['choice']}
        if progress:
            progress({'event': 'started', 'index': index, 'total': len(document['items']), **result})
        if item['choice'] == 'hold':
            result['status'] = 'held'
        elif stopped:
            result['status'] = 'unprocessed'
        else:
            try:
                if apply:
                    result.update(mp.apply(config, item['path'], item['choice'], item['token'], remote, validate))
                else:
                    preview = mp.preview(config, item['path'], item['choice'], remote, validate)
                    result.update({key: preview[key] for key in ('token', 'local', 'cloud')})
                    result['status'] = 'ready'
            except (OSError, ValueError, TransferStateError) as exc:
                result.update(status='failed', error=str(exc))
                # A transfer failure may leave recovery pending. Never auto-retry.
                stopped = apply
        results.append(result)
        if progress:
            progress({'event': 'finished', 'index': index, 'total': len(document['items']), 'item': result})
    counts = dict(Counter(item['status'] for item in results))
    return {'schema': SCHEMA, 'operation': 'apply' if apply else 'preview', 'items': results,
            'counts': counts, 'failed': counts.get('failed', 0),
            'message': '; '.join(f'{key}: {value}' for key, value in counts.items())}
