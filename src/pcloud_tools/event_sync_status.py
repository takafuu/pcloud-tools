"""Read-only event status. No file discovery, hashing or cloud requests."""
from __future__ import annotations

from datetime import datetime, timezone
from collections.abc import Mapping, Sequence
from .sqlite_state import Store, DB_NAME, active as sqlite_active
import json
import hashlib
from .review_classification import counts
import sqlite3

from .event_sync import Scope, read_state, state_path
from .event_sync_remote import SyncError
from .transfer_state import transfer_tick_lock_status


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def review_entries(config, saved):
    """The status badge and review list share one scope/count definition."""
    reviews = saved.get("reviews")
    if not isinstance(reviews, Mapping) or any(not isinstance(r, dict) for r in reviews.values()):
        raise SyncError("確認待ちの保存情報を取得できません")
    scope = Scope(config)
    return {p: r for p, r in reviews.items() if scope.allows(p)}


def scope_signature(config):
    digest = hashlib.sha256()
    for path in (config.allowlist_file, config.manager_ignore_file):
        digest.update(path.read_bytes() if path.exists() else b'<missing>')
    digest.update(repr((config.default_excludes, str(config.core_dir), config.remote_trash_root)).encode())
    return digest.hexdigest()


def aggregate(config, saved):
    # Count review paths without decoding their detailed payloads into memory.
    scope = Scope(config)
    reviews = saved.get('reviews', {})
    reconciliation = saved.get('reconcile')
    if reconciliation is not None:
        remaining = len(reconciliation['pending'])
        phase = 'initial' if reconciliation.get('id') == 'initial' else 'reconciling'
    else:
        remaining, phase = (0, 'waiting') if saved.get('reconciled_request') else (None, 'uninitialized')
    return {'state_saved_at': saved.get('updated_at'), 'state_revision': saved.get('updated_at'),
            'reconciliation': phase, 'remaining': remaining,
            **counts(r for path, r in reviews.items() if scope.allows(path)),
            'scope_signature': scope_signature(config)}


def saved_summary(config, saved):
    reviews = review_entries(config, saved)
    reconciliation = saved.get("reconcile")
    if reconciliation is not None:
        if not isinstance(reconciliation, dict) or not isinstance(reconciliation.get("pending"), Sequence):
            raise SyncError("照合の保存情報を取得できません")
        remaining = len(reconciliation["pending"])
        phase = "initial" if reconciliation.get("id") == "initial" else "reconciling"
    elif saved.get("reconciled_request"):
        remaining, phase = 0, "waiting"
    else:
        remaining, phase = None, "uninitialized"
    return {"state_saved_at": saved.get("updated_at"), "state_revision": saved.get("updated_at"),
            "reconciliation": phase, "remaining": remaining, **counts(reviews.values())}


def _read_optional(path, expected):
    try:
        value = json.loads(path.read_text())
        if not isinstance(value, expected):
            raise ValueError("unexpected type")
        return value, datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(), None
    except FileNotFoundError:
        return None, None, "未取得"
    except (OSError, ValueError):
        return None, None, "保存情報を読み取れません"


def observe_services(config):
    # Reuse service detection without mode's legacy plan/queue traversal.
    from .cli_mode import _command_v, _daemon_services, _launchctl_print, _rclone_bisync_lock_info
    from .autosync_runtime import read_autosync_state
    from .sync_runtime import read_sync_lock_state
    binary = _command_v("launchctl")
    services = {s["name"]: _launchctl_print(binary, s["label"]) for s in _daemon_services()}
    autosync = read_autosync_state(config)
    bisync = _rclone_bisync_lock_info(config)
    sync_lock = read_sync_lock_state(config)
    warnings = []
    if autosync.loaded or sync_lock.active or bisync.get("process active") == "yes":
        warnings.append("旧bisyncの起動を検出しました。同期サービスの競合を診断してください")
    if not binary:
        warnings.append("サービスの稼働状態を取得できません")
    elif not all(s.get("loaded") for s in services.values()):
        warnings.append("同期サービスの一部が停止しています")
    return services, warnings


def snapshot(config):
    observed = timestamp()
    result = {"schema": "pcloud-event-status.v1", "observed_at": observed,
              "state_saved_at": None, "state_revision": None, "reconciliation": "unknown",
              "remaining": None, "review_count": None, "issues": []}
    issues = result["issues"]
    try:
        if not sqlite_active(config.state_dir) and not state_path(config).is_file():
            raise SyncError("同期の保存状態は未取得です")
        if sqlite_active(config.state_dir):
            summary = Store(config.state_dir / DB_NAME).get('summary', 'event')
            if not summary or summary.get('scope_signature') != scope_signature(config):
                raise SyncError('同期状態の集計更新を待っています')
            result.update({k:v for k,v in summary.items() if k != 'scope_signature'})
        else:
            result.update(saved_summary(config, read_state(config)))
    except (OSError, SyncError, ValueError, sqlite3.Error) as exc:
        issues.append(str(exc))
    if result.get('diagnostic_count', 0):
        issues.append(f"同期の診断が必要な項目が{result['diagnostic_count']}件あります。確認画面の診断欄を確認してください")
    locks = [transfer_tick_lock_status(config.state_dir, s) for s in ("pushd", "diffd")]
    active = True if any(lock["active"] for lock in locks) else None if any(lock["status"] == "unknown" for lock in locks) else False
    result["executor_active"] = active
    if active is False:
        from .transfer_recovery import inspect_recovery
        for service in ("pushd", "diffd"):
            try:
                if sqlite_active(config.state_dir):
                    pending = Store(config.state_dir / DB_NAME).has_unresolved(service)
                else:
                    recovery = inspect_recovery(config.state_dir, service)
                    pending = bool(recovery.candidates or recovery.issues)
            except (OSError, ValueError, sqlite3.Error):
                issues.append('未完了転送の保存状態を読み取れません')
                break
            if pending:
                issues.append("未完了の転送があります。診断で復旧条件を確認してください")
                break
    services, warnings = observe_services(config)
    result["services"] = services
    issues.extend(warnings)
    result["queues"] = {}
    for service, name in (("pushd", "queue.json"), ("diffd", "remote-changes.json")):
        if sqlite_active(config.state_dir):
            store = Store(config.state_dir / DB_NAME)
            try:
                result['queues'][service] = {'count': store.count('queue', service), 'saved_at': store.get('queue_updated', service)}
            except (OSError, ValueError, sqlite3.Error):
                result['queues'][service] = {'count': None, 'saved_at': None}
                issues.append('待機イベントの保存状態を読み取れません')
            continue
        records, saved_at, error = _read_optional(config.state_dir / service / name, list)
        result["queues"][service] = {"count": len(records) if records is not None else None, "saved_at": saved_at}
        if error:
            issues.append(("ローカル" if service == "pushd" else "クラウド") + "の待機イベント: " + error)
    operation, _, operation_error = _read_optional(config.state_dir / "event-sync" / "operation.json", dict)
    result['progress'], _, _ = _read_optional(config.state_dir / 'event-sync' / 'progress.json', dict)
    from .review_worker import status as review_status
    result['review_progress'] = review_status(config)
    result["operation"] = operation
    result["operation_status"] = operation_error or "取得済み"
    if operation and operation.get("status") == "failed":
        issues.append("前回の同期処理が失敗しました。診断で内容を確認してください")
    if operation and operation.get("status") == "running" and active is False:
        issues.append("前回の同期処理の終了を確認できません")
    age = None
    try:
        saved_at = datetime.fromisoformat(result["state_saved_at"])
        age = (datetime.now(timezone.utc) - saved_at).total_seconds()
    except (TypeError, ValueError):
        pass
    result["saved_state_stale"] = age is None or age < -5 or age > 120
    phase = result["reconciliation"]
    remaining = result["remaining"]
    if phase in {"initial", "reconciling"}:
        label = "初回照合" if phase == "initial" else "再照合"
        label += "中" if active is True else "待ち" if active is False else "・稼働未取得"
        label += f"・保存時点の残り {remaining:,}件"
    elif active is True:
        label = "同期処理中"
    elif phase == "unknown":
        label = "同期状態を取得できません"
    elif phase == "uninitialized":
        label = "初回照合の開始待ち"
    elif active is None or result["saved_state_stale"]:
        label = "同期状態の更新待ち"
    elif issues:
        label = "同期状態を確認してください"
    elif any(q["count"] for q in result["queues"].values()):
        label = "変更の処理待ち"
    else:
        label = "変更待ち"
    result["label"] = label
    result["color"] = "orange" if issues else "dodgerblue" if active or remaining else "black"
    return result
