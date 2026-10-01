"""Event reconciliation policy; rclone remains the transfer engine."""
from __future__ import annotations
from .sync_trace import Recorder, timed
from .config import EVENT_RECONCILE_BATCH_LIMIT

import contextlib
import hashlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import time
import uuid

from .event_sync_remote import (RcloneRemote, SyncError, local_version, relative_path, rclone_path,
                                safe_local, same_content, stat_version)
from .io_utils import atomic_write_json
from .sqlite_state import Store, DB_NAME, active, PendingRows
from .manager_ignore import load_manager_ignore_rules, manager_ignore_match
from .service_daemon_plan import _matches_allowlist, _matches_exclude, _is_configured_trash_path
from .sync_scope import sync_allowlist_info, prepare_sync_filter_rules
from .transfer_recovery import inspect_recovery
from .transfer_state import (consume_event_ids, create_attempt, ensure_event_ids,
                             mark_attempt_child, clear_attempt_child, read_queue_snapshot,
                             transfer_tick_lock, update_attempt, writer_process_session,
                             writer_state_lock)

ABSENT = {"exists": False}
ID_MISMATCH = "クラウド名とイベントのファイルIDが一致しません。再照合が必要です"
UNSUPPORTED_EVENT = '未対応のイベントです。再照合または復旧確認が必要です'
CONTENT_EVENTS = {'upload', 'download', 'delete', 'sync', 'change', 'create', 'created', 'update', 'updated', 'modify', 'modified'}


def obsolete_identity_event(local, cloud, baseline, local_events, cloud_events):
    """Retire only an obsolete object notification, with unchanged verified current content."""
    if not cloud.get('exists') or not local.get('exists') or not baseline:
        return False
    mismatched = [r for r in cloud_events if r.get('remote_file_id') is not None
                  and str(r['remote_file_id']) != str(cloud.get('id'))]
    return (bool(mismatched) and bool(cloud.get('id'))
            and all(r.get('action') in CONTENT_EVENTS for r in local_events + cloud_events)
            and cloud == baseline.get('cloud')
            and same_content(local, baseline.get('local', ABSENT)) is True
            and same_content(local, cloud) is True)



def now():
    return datetime.now(timezone.utc).isoformat()


def state_path(config):
    return config.state_dir / "event-sync" / "state.json"


def read_state(config):
    if active(config.state_dir):
        return Store(config.state_dir / DB_NAME).load_state()
    path = state_path(config)
    if not path.exists():
        return {"schema": "pcloud-event-sync.v1", "baseline": {}, "reviews": {}}
    try:
        state = json.loads(path.read_text())
        if state.get("schema") != "pcloud-event-sync.v1" or not isinstance(state.get("baseline"), dict):
            raise ValueError("unsupported state")
        return state
    except (ValueError, OSError, AttributeError) as exc:
        raise SyncError("event sync state is unreadable; retain queues for recovery") from exc


def request_reconciliation(config, reason):
    path = config.state_dir / "event-sync" / "reconcile-request.json"
    with writer_state_lock(path):
        atomic_write_json(path, {"id": uuid.uuid4().hex, "reason": reason, "created_at": now()})


class Scope:
    def __init__(self, config):
        self.config = config
        self.scope = sync_allowlist_info(config)
        if self.scope.allowlist_status != "loaded":
            raise SyncError("sync allowlist is unavailable")
        self.rules = load_manager_ignore_rules(config)

    def allows(self, path):
        try:
            relative_path(path)
        except SyncError:
            return False
        if path == ".conflict" or path.startswith(".conflict/"):
            return False
        if not _matches_allowlist(path, self.scope.entries):
            return False
        if _matches_exclude(path, self.config.default_excludes) or _is_configured_trash_path(self.config, path):
            return False
        if Path(path).name.endswith(".partial") or Path(path).name.startswith(".pcloud-event-"):
            return False
        # manager_ignore_match uses the last matching rule. Start there so a
        # broad late rule does not rescan every earlier pattern for each file.
        for rule in reversed(self.rules):
            match = manager_ignore_match(self.config, path, rules=(rule,))
            if match:
                return not match.ignored
        return True

    def local_paths(self):
        # Traverse only allowlisted roots; do not follow directory symlinks.
        found = set()
        for entry in self.scope.entries:
            start = self.config.core_dir if entry == "/" else safe_local(self.config.core_dir, entry.rstrip("/"))
            if start.is_file():
                p = start.relative_to(self.config.core_dir).as_posix()
                if self.allows(p):
                    found.add(p)
            elif start.is_dir():
                for directory, dirs, files in os.walk(start, followlinks=False):
                    kept = []
                    for d in dirs:
                        child = Path(directory) / d
                        relative = child.relative_to(self.config.core_dir).as_posix()
                        match = manager_ignore_match(self.config, relative + "/", rules=self.rules)
                        # An allow exception can reinclude descendants; retain traversal
                        # only for those configured trees (wildcard exceptions are conservative).
                        exceptions = [r.pattern for r in self.rules if r.allow]
                        reinclude = any(p.startswith(relative + "/") or any(c in p for c in "*?[") for p in exceptions)
                        excluded = _matches_exclude(relative, self.config.default_excludes) or _is_configured_trash_path(self.config, relative)
                        if not child.is_symlink() and not excluded and (not match or not match.ignored or reinclude):
                            kept.append(d)
                    dirs[:] = kept
                    for name in files:
                        p = (Path(directory) / name).relative_to(self.config.core_dir).as_posix()
                        if self.allows(p):
                            found.add(p)
        return found


def choose(local, cloud, *, local_delete=False, cloud_delete=False, baseline=None, reconcile=False, same_time="local"):
    """Pure decision table. Unknown state never grants destructive authority."""
    if local.get("exists") and cloud.get("exists"):
        equal = same_content(local, cloud)
        if equal is True:
            return "equal", "内容一致"
        if equal is None:
            return "hold", "内容ハッシュを比較できません"
        left, right = local.get("second"), cloud.get("second")
        if left is None or right is None:
            return "hold", "更新日時を取得できません"
        if left == right:
            return ("upload", "同時刻・ローカル優先") if same_time == "local" else ("download", "同時刻・クラウド優先")
        return ("upload", "ローカルが新しい") if left > right else ("download", "クラウドが新しい")
    if not local.get("exists") and not cloud.get("exists"):
        return "equal", "両側にファイルなし"
    if reconcile:
        return ("upload", "再照合: ローカルのみ") if local.get("exists") else ("download", "再照合: クラウドのみ")
    baseline = baseline or {}
    if local_delete and not local.get("exists"):
        return "delete-cloud", "確認済みのローカル削除を採用"
    if cloud_delete and not cloud.get("exists"):
        return "delete-local", "確認済みのクラウド削除を採用"
    return ("upload", "ローカルのみ") if local.get("exists") else ("download", "クラウドのみ")


class EventSync:
    def __init__(self, config, remote=None):
        self.config = config
        self.remote = remote or RcloneRemote(config)
        self.scope = Scope(config)
        self.files = {"pushd": config.state_dir / "pushd" / "queue.json",
                      "diffd": config.state_dir / "diffd" / "remote-changes.json"}
        self.state = read_state(config)
        self.state.setdefault("reviews", {})
        self.results = []
        self.archive_results = []
        self.attempt = None
        self.receipt_dir = None
        self.staging = None
        self.reconciliation_batch = False
        self.local_cache = {}
        self.progress_file = config.state_dir / "event-sync" / "progress.json"

    @timed("state-save")
    def save(self):
        self.state["updated_at"] = now()
        if active(self.config.state_dir):
            from .event_sync_status import aggregate
            Store(self.config.state_dir / DB_NAME).save_state(self.state, aggregate(self.config, self.state), self.scope.allows)
        else:
            atomic_write_json(state_path(self.config), self.state)

    def snapshots(self, paths=None):
        if active(self.config.state_dir):
            store = Store(self.config.state_dir / DB_NAME)
            for service in self.files:store.ensure_ids(service)
            return {service: store.queue_rows(service, paths) for service in self.files}
        result = {}
        for service, file in self.files.items():
            assigned = ensure_event_ids(file, write=True)
            if assigned.issue:
                raise SyncError(assigned.issue.message)
            snapshot = read_queue_snapshot(file)
            if snapshot.issue:
                raise SyncError(snapshot.issue.message)
            result[service] = list(snapshot.raw_records)
        return result

    def consume(self, selected, paths=None):
        for service, records in selected.items():
            ids = [r["event_id"] for r in records if isinstance(r, dict) and r.get("event_id")
                   and (paths is None or r.get("path") in paths)]
            result = consume_event_ids(self.files[service], ids)
            if result.issue:
                raise SyncError(result.issue.message)

    def begin_attempt(self, records):
        if self.attempt is not None:
            return
        result = create_attempt(self.config.state_dir, "pushd", records, concurrency=1)
        if result.issue:
            raise SyncError(result.issue.message)
        self.attempt = result.attempt_id
        self.receipt_dir = self.config.state_dir / "event-sync" / "receipts" / self.attempt
        self.receipt_dir.mkdir(parents=True, mode=0o700)
        self.staging = self.receipt_dir / "staging"
        self.staging.mkdir(mode=0o700)
        self.remote.on_started = lambda _item, pid: self._check(mark_attempt_child(self.config.state_dir, "pushd", self.attempt, pid))
        self.remote.on_finished = lambda _item, pid: self._check(clear_attempt_child(self.config.state_dir, "pushd", self.attempt, pid))

    @staticmethod
    def _check(result):
        if result.issue:
            raise SyncError(result.issue.message)

    @timed("local-stat-hash")
    def local(self, path, cloud=None):
        target = safe_local(self.config.core_dir, path)
        current = stat_version(target)
        cached = self.local_cache.get(path) or self.state['baseline'].get(path, {}).get('local', {})
        if current.get('exists') and cached.get('hashes') and all(cached.get(k) == v for k, v in current.items()):
            return cached
        version = local_version(target, ("sha1", "md5", "sha256"))
        self.local_cache[path] = version
        return version

    def progress(self, phase, completed=None, total=None):
        if getattr(self, "trace", None): self.trace.set_phase(phase)
        atomic_write_json(self.progress_file, {'schema': 'pcloud-event-progress.v1',
            'phase': phase, 'updated_at': now(), 'completed': completed, 'total': total})

    @timed("cloud-inventory")
    def current_cloud(self, paths, *, allow_cache=False, changed=()):
        if not allow_cache:
            return self.remote.inventory(paths)
        metadata = self.remote.inventory(paths, hashes=False)
        changed = set(changed)
        versions, needed = {}, []
        for path in paths:
            current = metadata.get(path, ABSENT)
            cached = self.state['baseline'].get(path, {}).get('cloud', {})
            if (path not in changed and current.get('exists') and current.get('id') and cached.get('hashes')
                    and all(current.get(k) == cached.get(k) for k in ('exists','id','size','second','modified'))):
                versions[path] = cached
            else:
                needed.append(path)
        if needed:versions.update(self.remote.inventory(needed))
        return versions

    def hold(self, path, reason, selected, local=None, cloud=None):
        self.state["reviews"][path] = {"path": path, "reason": reason, "local": local,
            "cloud": cloud, "event_ids": {s: [r.get("event_id") for r in rr if isinstance(r, dict) and r.get("path") == path]
                                           for s, rr in selected.items()}}
        self.results.append({"path": path, "action": "hold", "reason": reason})

    def expand_directory(self, path, selected, cloud):
        """Repair old file-shaped directory events without choosing/deleting content."""
        from .event_sync_watch import append_records
        target = safe_local(self.config.core_dir, path)
        if not target.is_dir():
            return False
        if cloud.get('exists'):
            raise SyncError('ローカルのフォルダとクラウドのファイルが同じパスにあります。構成の確認が必要です')
        before = target.stat()
        if not hasattr(self, '_directory_inventory'):
            filters = prepare_sync_filter_rules(self.config, self.scope.scope.entries)
            self._directory_inventory = (set(self.remote.inventory(filter_rules=filters, hashes=False))
                                         | set(self.remote.local_paths(filters)))
        children = sorted(p for p in self._directory_inventory
                          if p.startswith(path + '/') and self.scope.allows(p))
        after = target.stat()
        if (before.st_ino, before.st_mtime_ns, before.st_ctime_ns) != (after.st_ino, after.st_mtime_ns, after.st_ctime_ns):
            raise SyncError('フォルダが変更されました。次回に内容を再確認します')
        if self.defer_changed_generation(path, selected):
            return True
        # Persist children first: a crash may requeue work but cannot lose it.
        # A sync event does not infer deletions from the directory's timestamp.
        append_records(self.config, [{'path': p, 'action': 'sync', 'reason': 'directory-content-recheck'} for p in children])
        self.state['reviews'].pop(path, None)
        self.state['baseline'].pop(path, None)
        self.save()
        self.consume(selected, {path})
        self.results.append({'path': path, 'action': 'directory-expanded', 'children': len(children)})
        return True

    def complete(self, path, selected, local, cloud, action):
        if local.get("exists") and cloud.get("exists"):
            self.state["baseline"][path] = {"local": local, "cloud": cloud}
        else:
            self.state["baseline"].pop(path, None)
        self.state["reviews"].pop(path, None)
        # Reconciliation already persisted its entire pending list and captured
        # generation before retiring old events. Commit its progress once per
        # batch; a crash replays the still-pending paths and leaves new events.
        # Ordinary/manual work still commits proof before queue consumption.
        if not self.reconciliation_batch:
            self.save()
            self.consume(selected, {path})
        self.results.append({"path": path, "action": action, "verified": True})

    def prepare_reconciliation(self, selected):
        request_file = self.config.state_dir / "event-sync" / "reconcile-request.json"
        request = json.loads(request_file.read_text()) if request_file.exists() else {"id": "initial"}
        if self.state.get("reconcile"):
            # The snapshot was committed before queue cleanup. Replay is generation safe.
            if active(self.config.state_dir):
                Store(self.config.state_dir / DB_NAME).consume_captured()
            else:
                self.consume(self.state["reconcile"]["captured"])
            return
        if self.state.get("reconciled_request") == request["id"]:
            return
        filters = prepare_sync_filter_rules(self.config, self.scope.scope.entries)
        # Discovery needs names only. pCloud hashes require a separate API call
        # per object; fetch them later, for this tick's bounded selected paths.
        self.progress("discovering")
        remote = self.remote.inventory(filter_rules=filters, hashes=False)
        local = self.remote.local_paths(filters)
        paths = {p for p in set(remote) | set(local) if self.scope.allows(p)}
        paths |= {r["path"] for rr in selected.values() for r in rr if isinstance(r, dict) and self.scope.allows(r.get("path"))}
        # Display the original Standard spelling, but block it as an operation
        # target even if a distinct, representable local name happens to match.
        for p, r in list(self.state["reviews"].items()):
            if r.get("unrepresentable_name"):
                self.state["reviews"].pop(p)
        for issue in getattr(self.remote, "discovery_issues", []):
            path = issue["path"]
            if not self.scope.allows(path):
                continue
            self.state["reviews"][path] = {**issue, "local": None, "structural": True,
                "unrepresentable_name": True, "event_ids": {"pushd": [], "diffd": []}}
            paths.discard(path)
        self.state["reconcile"] = {"id": request["id"], "pending": sorted(paths), "captured": selected}
        # Keep verified versions as a stat-keyed hash cache across recovery scans.
        self.save()
        if active(self.config.state_dir):
            Store(self.config.state_dir / DB_NAME).consume_captured()
        else:
            self.consume(selected)

    def generation_unchanged(self, path, selected):
        if not getattr(self, 'interleaving', False):
            return True
        store = Store(self.config.state_dir / DB_NAME)
        current = {service: store.queue_rows(service, [path]) for service in self.files}
        return all({r.get('event_id') for r in current[service]} ==
                   {r.get('event_id') for r in selected[service] if r.get('path') == path}
                   for service in self.files)

    def defer_changed_generation(self, path, selected):
        if self.generation_unchanged(path, selected):
            return False
        self.results.append({'path': path, 'action': 'waiting', 'reason': '新しい変更を次のバッチで再確認します'})
        return True

    def interleaved_paths(self, reconciliation, max_records):
        store = Store(self.config.state_dir / DB_NAME)
        limit = min(max_records, EVENT_RECONCILE_BATCH_LIMIT)
        if limit <= 0:
            return [], {'pushd': [], 'diffd': []}, set()
        # A one-file caller alternates; larger batches reserve half for the backlog.
        live_limit = max(1, limit // 2)
        if limit == 1 and self.state.get('interleave_turn') == 'background':
            live_limit = 0
        repairs, repair_cursor = store.review_candidates(
            ('only regular files may be synchronized automatically', UNSUPPORTED_EVENT,
             'フォルダが変更されました。次回に内容を再確認します', 'アップロード結果を確認できません'),
            self.state.get('directory_review_cursor'), min(10, live_limit), self.scope.allows)
        self.state['directory_review_cursor'] = repair_cursor
        live = list(dict.fromkeys([*repairs, *store.priority_paths(live_limit, self.scope.allows, self.state['reviews'])]))[:live_limit]
        structural = {s: list(rows) for s, rows in self.snapshots(live).items()} if live else {'pushd': [], 'diffd': []}
        self.reconciliation_batch = False
        if any(r.get('action') in {'move', 'directory'} for r in structural['pushd']):
            self.expand_structural_events(structural)
            # Preserve the reserved repair work when structural expansion refreshes live events.
            live = list(dict.fromkeys([*repairs, *store.priority_paths(live_limit, self.scope.allows, self.state['reviews'])]))[:live_limit]
        paths = list(live)
        for path in reconciliation['pending'][:limit]:
            if len(paths) >= limit:
                break
            if path not in paths:
                paths.append(path)
            if len(paths) >= limit:
                break
        self.state['interleave_turn'] = 'background' if live else 'live'
        selected = {s: list(rows) for s, rows in self.snapshots(paths).items()}
        event_paths = {r['path'] for rows in selected.values() for r in rows}
        return paths, selected, event_paths

    def repair_name_reviews(self):
        """Requeue old Raw-decoder holds only after the original cloud ID matches.

        Preserve the existing reconciliation cut and every later queue generation.
        This is a bounded metadata read, not a restart of full discovery.
        """
        old = {p: r for p, r in self.state["reviews"].items() if r.get("unrepresentable_name")}
        candidates = {}
        updated = False
        for path, record in old.items():
            reason = "クラウド名とファイルIDの対応は未取得です。再照合が必要です"
            if record.get("reason") != reason:
                record["reason"] = reason
                updated = True
            try:
                local = rclone_path(path, decode=True)
                if self.scope.allows(local):
                    candidates[local] = (path, record)
            except SyncError:
                record["reason"] = "ファイル名の対応を取得できません。再照合が必要です"
        if updated:
            self.save()
        if not candidates:
            return
        versions = self.remote.inventory(list(candidates), hashes=False)
        repaired = []
        for local, (path, record) in candidates.items():
            expected = (record.get("cloud") or {}).get("id")
            current = versions.get(local)
            if not expected or not current or current.get("id") != expected or (local != path and local in self.state["reviews"]):
                record["reason"] = "クラウド名とファイルIDの対応は未取得です。再照合が必要です"
                continue
            self.state["reviews"].pop(path)
            repaired.append(local)
        if repaired:
            reconciliation = self.state.get("reconcile")
            if reconciliation is None:
                request_file = self.config.state_dir / "event-sync" / "reconcile-request.json"
                request = json.loads(request_file.read_text()) if request_file.exists() else {"id": "initial"}
                reconciliation = {"id": request["id"], "pending": [], "captured": {"pushd": [], "diffd": []}}
                self.state["reconcile"] = reconciliation
            reconciliation["pending"] = list(dict.fromkeys([*repaired, *reconciliation["pending"]]))
            self.save()

    def backup_local(self, path, expected):
        target = safe_local(self.config.core_dir, path)
        backup = self.receipt_dir / "local" / path
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, backup)
        if same_content(self.local(path), expected) is not True or stat_version(target) != {k: expected[k] for k in stat_version(target)}:
            raise SyncError("local changed while saving original")
        if same_content(local_version(backup, expected.get("hashes", {})), expected) is not True:
            raise SyncError("local backup could not be verified")
        return backup

    def archive_loser(self, item, selected):
        from .conflict_archive import capture
        path, local, cloud, action = item['path'], item['local'], item['cloud'], item['action']
        baseline = self.state['baseline'].get(path, {})
        collision = (local.get('exists') and cloud.get('exists') and same_content(local, cloud) is not True
                     and (item.get('manual') or local.get('second') == cloud.get('second') or (same_content(local, baseline.get('local', ABSENT)) is not True
                                               and same_content(cloud, baseline.get('cloud', ABSENT)) is not True)))
        deleted_edit = (action == 'delete-local' and same_content(local, baseline.get('local', ABSENT)) is not True
                        or action == 'delete-cloud' and same_content(cloud, baseline.get('cloud', ABSENT)) is not True)
        if not (collision or deleted_edit):
            return True
        if collision or deleted_edit:
            side = 'local' if action in ('download', 'delete-local') else 'cloud'
            self.progress('archiving', 0, 1)
            result = capture(self.config, self.remote, path, side, local if side == 'local' else cloud)
            self.archive_results.append({'path': path, 'side': side, **result})
        # A failed backup permits progress, but never grants permission to replace
        # a version edited while the backup was being attempted.
        if self.defer_changed_generation(path, selected):
            return False
        if self.local(path, cloud) != local or self.current_cloud([path]).get(path, ABSENT) != cloud:
            self.results.append({'path': path, 'action': 'waiting', 'reason': '退避中に変更されたため再評価します'})
            return False
        return True

    def transfer(self, direction, items, selected):
        if not items:
            return
        # Recheck every selected source/destination immediately before starting a batch.
        cloud_now = self.current_cloud([i["path"] for i in items])
        stable = []
        for item in items:
            path = item["path"]
            if self.defer_changed_generation(path, selected):
                continue
            if self.local(path, item["cloud"]) != item["local"] or cloud_now.get(path, ABSENT) != item["cloud"]:
                self.hold(path, "転送前に変更されました", selected)
                continue
            if self.archive_loser(item, selected):
                stable.append(item)
        if not stable:
            return
        self.begin_attempt(stable)
        self.progress(direction, 0, len(stable))
        result = self.remote.copy(direction, [i["path"] for i in stable], self.staging)
        self.progress("verifying", 0, len(stable))
        after = self.current_cloud([i["path"] for i in stable])
        for item in stable:
            path, local, cloud = item["path"], item["local"], item["cloud"]
            current = self.local(path, cloud)
            remote_after = after.get(path, ABSENT)
            if direction == "upload":
                if current != local or same_content(local, remote_after) is not True:
                    self.hold(path, "アップロード結果を確認できません", selected, current, remote_after)
                    continue
                self.complete(path, selected, current, remote_after, "upload")
            else:
                staged_path = safe_local(self.staging, path)
                staged = local_version(staged_path, cloud.get("hashes", {}))
                if current != local or remote_after != cloud or same_content(staged, cloud) is not True:
                    self.hold(path, "ダウンロード中に変更されたか、結果を確認できません", selected, current, remote_after)
                    continue
                if self.defer_changed_generation(path, selected):
                    continue
                target = safe_local(self.config.core_dir, path)
                target.parent.mkdir(parents=True, exist_ok=True)
                safe_local(self.config.core_dir, path)
                if self.local(path, cloud) != local:
                    self.hold(path, "取り込み直前にローカルが変更されました", selected)
                    continue
                # os.replace is atomic on the local volume; keep staging on that volume.
                sibling = target.with_name(".pcloud-event-" + uuid.uuid4().hex)
                try:
                    shutil.copy2(staged_path, sibling)
                    if self.local(path, cloud) != local:
                        raise SyncError("local changed before replacement")
                    if self.defer_changed_generation(path, selected):
                        continue
                    os.replace(sibling, target)
                finally:
                    sibling.unlink(missing_ok=True)
                final = self.local(path, cloud)
                self.complete(path, selected, final, cloud, "download")
        return result

    def delete(self, item, selected):
        path, local, cloud, action = item["path"], item["local"], item["cloud"], item["action"]
        if self.local(path, cloud) != local or self.current_cloud([path]).get(path, ABSENT) != cloud:
            self.hold(path, "削除前に変更されました", selected)
            return
        if self.defer_changed_generation(path, selected):
            return
        if not self.archive_loser(item, selected):
            return
        self.begin_attempt([item])
        if action == "delete-cloud":
            result = self.remote.delete(path)
            if result.get("returncode") != 0 or self.current_cloud([path]).get(path, ABSENT).get("exists"):
                self.hold(path, "クラウド削除結果を確認できません", selected)
                return
        else:
            if self.local(path) != local:
                self.hold(path, "退避中にローカルが変更されました", selected)
                return
            if self.defer_changed_generation(path, selected):
                return
            safe_local(self.config.core_dir, path).unlink()
        self.complete(path, selected, ABSENT, ABSENT, action)

    def expand_structural_events(self, selected):
        from .event_sync_watch import append_records
        structural = [r for r in selected["pushd"] if isinstance(r, dict) and r.get("action") in {"move", "directory"}]
        if not structural:
            return
        filters = prepare_sync_filter_rules(self.config, self.scope.scope.entries)
        inventory = self.remote.inventory(filter_rules=filters, hashes=False)
        local_inventory = None
        def local_paths():
            nonlocal local_inventory
            if local_inventory is None:
                local_inventory = self.remote.local_paths(filters)
            return local_inventory
        moves = {r["path"]: r for r in structural if r.get("action") == "move"}
        handled = set()
        for record in structural:
            old = record["path"]
            if old in handled:
                continue
            if record["action"] == "directory":
                paths = {p for p in inventory if p.startswith(old + "/")} | {p for p in local_paths() if p.startswith(old + "/")}
                append_records(self.config, [{"path": p, "action": "upload" if safe_local(self.config.core_dir, p).exists() else "delete",
                                             "reason": "fswatch:directory-child"} for p in sorted(paths)])
                self.consume({"pushd": [record], "diffd": []})
                self.state["reviews"].pop(old, None)
                continue
            chain = [record]
            new = record.get("destination")
            while new in moves and moves[new].get("file_id") == record.get("file_id") and new not in handled and moves[new] not in chain:
                chain.append(moves[new])
                new = moves[new].get("destination")
            try:
                destination = safe_local(self.config.core_dir, new)
                if safe_local(self.config.core_dir, old).exists() or not destination.exists() or destination.stat().st_ino != record.get("file_id"):
                    raise SyncError("改名前後の対応を現在のファイルで確認できません")
                if record.get("is_dir"):
                    mappings = {p: new + p[len(old):] for p in inventory if p.startswith(old + "/")}
                else:
                    mappings = {old: new} if old in inventory else {}
                inside = {p: q for p, q in mappings.items() if self.scope.allows(p) and self.scope.allows(q)}
                # Crossing the scope boundary never copies data out of scope.
                outside = [p for p, q in mappings.items() if self.scope.allows(p) and not self.scope.allows(q)]
                append_records(self.config, [{"path": p, "action": "delete", "reason": "fswatch:move-out-of-scope"} for p in outside])
                if inside:
                    before_move = self.current_cloud(list(inside))
                    if any(not before_move.get(p, {}).get("hashes") for p in inside):
                        raise SyncError("移動対象のクラウド版を検証できません")
                    self.begin_attempt([{**record, "mappings": inside}])
                    result = (self.remote.move_tree(old, new, [p[len(old) + 1:] for p in inside]) if record.get("is_dir")
                              else self.remote.move(old, new))
                    after = self.current_cloud([*inside, *inside.values()])
                    if result.get("returncode") != 0 or any(p in after or same_content(before_move[p], after.get(q, ABSENT)) is not True for p, q in inside.items()):
                        raise SyncError("クラウド移動結果を確認できません。復旧確認が必要です")
                    for p, q in inside.items():
                        self.state["baseline"].pop(p, None)
                        self.state["reviews"].pop(p, None)
                        inventory[q] = after[q]
                        inventory.pop(p, None)
                destinations = ([p for p in local_paths() if p.startswith(new + "/")] if record.get("is_dir") else [new])
                append_records(self.config, [{"path": p, "action": "upload", "reason": "fswatch:edit-after-move"} for p in destinations])
                old_names = {r["path"] for r in chain}
                # The content recheck above was queued before retiring old edits.
                self.consume({"pushd": [r for r in selected["pushd"] if isinstance(r, dict) and r.get("path") in old_names], "diffd": []})
                for old_name in old_names:
                    self.state["reviews"].pop(old_name, None)
                handled.update(old_names)
                self.results.append({"path": old, "destination": new, "action": "move", "verified": True})
            except (OSError, SyncError) as exc:
                self.hold(old, str(exc), selected)
                self.state["reviews"][old]["structural"] = True
                if self.attempt:
                    # A partially completed directory move must not be blindly replayed.
                    raise

    def review_preview(self, path, choice, selected=None):
        if not self.scope.allows(path) or choice not in {"pull", "local"}:
            raise SyncError("invalid review selection or excluded path")
        review = self.state.get("reviews", {}).get(path)
        from .review_classification import category
        if not review or category(review) != 'choice':
            raise SyncError("この項目は更新されたか、移動の復旧確認が必要です。一覧を更新してください")
        if selected is None:
            if active(self.config.state_dir):
                store = Store(self.config.state_dir / DB_NAME)
                selected = {service: store.queue_rows(service, [path]) for service in self.files}
            else:
                selected = {}
            for service, file in (() if active(self.config.state_dir) else self.files.items()):
                snapshot = read_queue_snapshot(file)
                if snapshot.issue:
                    raise SyncError(snapshot.issue.message)
                selected[service] = list(snapshot.raw_records)
        queues = {service: [r for r in records if isinstance(r, dict) and r.get("path") == path]
                  for service, records in selected.items()}
        if any(r.get("action") in {"move", "directory"} for r in queues["pushd"]):
            raise SyncError("移動イベントの復旧確認が必要です")
        cloud = self.current_cloud([path]).get(path, ABSENT)
        local = self.local(path)
        source = local if choice == "local" else cloud
        if not source.get("exists"):
            raise SyncError("採用する側にファイルがありません。残っている側を選んで復元してください")
        document = {"path": path, "choice": choice, "local": local, "cloud": cloud,
                    "queues": queues, "core": str(self.config.core_dir.resolve()),
                    "remote": self.config.core_remote, "state": str(self.config.state_dir.resolve())}
        token = hashlib.sha256(json.dumps(document, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return {**document, "token": token}

    def tick(self, max_records=100, manual=None):
        self.trace = Recorder(self.config.state_dir)
        self.remote.trace = self.trace
        success = False
        try:
            with self.trace.span('batch', detail=True):
                result = self._tick(max_records, manual)
            success = True
            return result
        finally:
            try:
                self.trace.finish(self.state, success)
            except (OSError, ValueError, KeyError, TypeError):
                pass

    def _tick(self, max_records=100, manual=None):
        with contextlib.ExitStack() as stack:
            for service in ("pushd", "diffd"):
                with self.trace.span("executor-lock"):
                    stack.enter_context(transfer_tick_lock(self.config.state_dir, service, blocking=False))
                if active(self.config.state_dir):
                    pending_recovery = Store(self.config.state_dir / DB_NAME).has_unresolved(service)
                else:
                    recovery = inspect_recovery(self.config.state_dir, service)
                    pending_recovery = bool(recovery.candidates or recovery.issues)
                if pending_recovery:
                    raise SyncError("unfinished transfer recovery must complete first")
            for service in ("pushd", "diffd"):
                stack.enter_context(writer_process_session(self.config.state_dir, service, generation="event-sync"))
            # Reload after taking shared executor locks (two schedulers may invoke this).
            self.state = read_state(self.config)
            pending = self.config.state_dir / "daemon" / "pending-downloads.json"
            if pending.exists() and json.loads(pending.read_text()):
                raise SyncError("legacy pending downloads require recovery before event synchronization")
            selected = self.snapshots()
            operation_file = self.config.state_dir / "event-sync" / "operation.json"
            try:
                previous = json.loads(operation_file.read_text()).get("last_result")
            except (OSError, ValueError, AttributeError):
                previous = None
            operation = {"schema": "pcloud-event-operation.v1", "status": "running", "started_at": now(),
                         "last_result": previous}
            atomic_write_json(operation_file, operation)
            try:
                approved = None
                had_reconciliation = bool(self.state.get("reconcile"))
                if manual:
                    approved = self.review_preview(manual["path"], manual["choice"], selected)
                    if approved["token"] != manual["token"]:
                        raise SyncError("採用確認後にファイルまたはイベントが変更されました。もう一度確認してください")
                    reconciliation = None
                    paths = [manual["path"]]
                else:
                    self.repair_name_reviews()
                    self.prepare_reconciliation(selected)
                    reconciliation = self.state.get("reconcile")
                event_paths = set()
                interleaved = bool(reconciliation) and had_reconciliation and active(self.config.state_dir) and not manual
                self.interleaving = interleaved
                self.reconciliation_batch = bool(reconciliation)
                if manual:
                    pass
                elif interleaved:
                    paths, selected, event_paths = self.interleaved_paths(reconciliation, max_records)
                    # Preserve the existing batched save when no foreground events exist.
                    self.reconciliation_batch = not bool(event_paths)
                elif reconciliation:
                    limit = min(max_records, EVENT_RECONCILE_BATCH_LIMIT) if active(self.config.state_dir) else max_records
                    paths = reconciliation["pending"][:limit]
                    # New events arrived after the reconciliation cut; do not consume them.
                    selected = {"pushd": [], "diffd": []}
                elif active(self.config.state_dir):
                    store = Store(self.config.state_dir / DB_NAME)
                    structural = store.structural_rows()
                    if structural:
                        old_paths = [r['path'] for r in structural]
                        self.expand_structural_events({s:list(rows) for s,rows in self.snapshots(old_paths).items()})
                    paths = store.candidate_paths(self.state.get('cursor'), max_records, self.scope.allows)
                    if paths:self.state['cursor'] = paths[-1]
                    paths = [p for p in paths if not self.state['reviews'].get(p, {}).get('structural')]
                else:
                    self.expand_structural_events(selected)
                    selected = self.snapshots()
                    paths = list(dict.fromkeys(r["path"] for rr in selected.values() for r in rr
                                               if isinstance(r, dict) and r.get("action") not in {"move", "directory"} and self.scope.allows(r.get("path"))))
                    structural_paths = {r["path"] for r in selected["pushd"] if isinstance(r, dict) and r.get("action") in {"move", "directory"}}
                    paths = list(dict.fromkeys([*paths, *(p for p, r in self.state["reviews"].items() if not r.get("structural"))]))
                    paths = [p for p in paths if p not in structural_paths]
                    # Rotate pending paths, so permanent review items cannot starve
                    # later arrivals beyond the per-tick batch limit.
                    cursor = self.state.get("cursor")
                    paths = sorted(paths)
                    if cursor:
                        paths = [p for p in paths if p > cursor] + [p for p in paths if p <= cursor]
                    paths = paths[:max_records]
                    if paths:
                        self.state["cursor"] = paths[-1]
                if active(self.config.state_dir) and not reconciliation:
                    selected = {s: list(rows) for s, rows in self.snapshots(paths).items()}
                paths = [p for p in paths if self.scope.allows(p)
                         and not self.state["reviews"].get(p, {}).get("unrepresentable_name")]
                supported = []
                for path in paths:
                    try:
                        rclone_path(path)
                        supported.append(path)
                    except SyncError as exc:
                        self.hold(path, str(exc), selected)
                        self.state["reviews"][path]["structural"] = True
                paths = supported
                self.progress("comparing", 0, len(paths))
                cloud_versions = self.current_cloud(paths, allow_cache=not reconciliation and not manual,
                    changed=[r['path'] for r in selected['diffd'] if isinstance(r,dict)])
                transfers = {"upload": [], "download": []}
                for path_index, path in enumerate(paths):
                    if path_index % 25 == 0:
                        self.progress("comparing", path_index, len(paths))
                    try:
                        if self.defer_changed_generation(path, selected):
                            continue
                        if not manual and any(r.get('path') == path and r.get('action') in {'move', 'directory'} for r in selected['pushd']):
                            self.results.append({'path': path, 'action': 'waiting', 'reason': 'フォルダ作成・移動イベントを次のバッチで処理します'})
                            continue
                        cloud = cloud_versions.get(path, ABSENT)
                        if not manual and self.expand_directory(path, selected, cloud):
                            continue
                        local = self.local(path, cloud)
                        local_events = [r for r in selected["pushd"] if isinstance(r, dict) and r.get("path") == path]
                        cloud_events = [r for r in selected["diffd"] if isinstance(r, dict) and r.get("path") == path]
                        if obsolete_identity_event(local, cloud, self.state['baseline'].get(path), local_events, cloud_events):
                            # Fresh cloud inventory + verified baseline + current local hashes
                            # agree. No copying/deleting is needed; consume captured IDs only.
                            self.complete(path, selected, local, cloud, 'equal')
                            continue
                        # An API-native event must still name the same cloud object.
                        # Use the already fetched batch metadata (or saved ID for a
                        # deletion), never infer a different object from spelling.
                        identity_changed = False
                        for record in cloud_events:
                            expected_id = record.get("remote_file_id")
                            if expected_id is None:
                                continue
                            identity = cloud if cloud.get("exists") else (self.state["baseline"].get(path, {}).get("cloud", {})
                                        if record.get("action") == "delete" else {})
                            if not identity.get("id") or str(identity["id"]) != str(expected_id):
                                identity_changed = True
                        if identity_changed and not manual and not (local.get("exists") and cloud.get("exists")) and not any(r.get("action") == "delete" for r in cloud_events):
                            # Inventory succeeded: preserve both versions for explicit,
                            # token-bound review instead of choosing a replacement object.
                            self.hold(path, 'クラウド側の版が変わっています。内容を確認して採用する版を選んでください',
                                      selected, local, cloud)
                            continue
                        action, reason = choose(local, cloud, reconcile=bool(reconciliation) and path not in event_paths,
                            local_delete=any(r.get("action") == "delete" for r in local_events),
                            cloud_delete=any(r.get("action") == "delete" for r in cloud_events),
                            baseline=self.state["baseline"].get(path), same_time=getattr(self.config, "conflict_same_time", "local"))
                        chosen = [r for r in local_events if r.get("event_sync_choice") == "local"]
                        if chosen:
                            approved = chosen[-1]
                            expected_cloud = approved.get("event_sync_cloud", {})
                            expected_local = approved.get("event_sync_local", {})
                            if (local.get("hashes", {}).get("sha256") == expected_local.get("sha256")
                                    and local.get("mtime_ns") == expected_local.get("mtime_ns")
                                    and cloud.get("hashes") == expected_cloud.get("hashes")
                                    and cloud.get("size") == expected_cloud.get("size")
                                    and cloud.get("modified") == expected_cloud.get("modified")
                                    and cloud.get("id") == str(expected_cloud.get("id") or "")):
                                action, reason = "upload", "明示的に選択したローカル版"
                            else:
                                action, reason = "hold", "採用確認後にファイルが変更されました"
                        if approved:
                            if local != approved["local"] or cloud != approved["cloud"]:
                                raise SyncError("採用確認後にファイルが変更されました")
                            action = "upload" if manual["choice"] == "local" else "download"
                            if same_content(local, cloud) is True:
                                action = "equal"
                        if any(r.get("action") not in {"upload", "download", "delete", "sync", "change", "create", "created", "update", "updated", "modify", "modified"} for r in local_events + cloud_events):
                            action, reason = "hold", UNSUPPORTED_EVENT
                        if not approved and action == "upload" and self.config.pushd_upload_settle_seconds > 0:
                            stable = self.state.setdefault("settling", {})
                            stamp = {k: v for k, v in local.items() if k not in {"hashes", "second"}}
                            previous = stable.get(path, {})
                            if previous.get("version") != stamp:
                                stable[path] = {"version": stamp, "since": time.time()}
                            if time.time() - stable[path]["since"] < self.config.pushd_upload_settle_seconds:
                                self.results.append({"path": path, "action": "waiting", "reason": "書き込み完了待ち"})
                                continue
                        item = {"path": path, "action": action, "local": local, "cloud": cloud, "manual": bool(manual)}
                        if action == "equal":
                            self.complete(path, selected, local, cloud, action)
                        elif action == "hold":
                            self.hold(path, reason, selected, local, cloud)
                            if any(r.get('action') not in CONTENT_EVENTS for r in local_events + cloud_events):
                                self.state['reviews'][path]['diagnostic'] = True
                        elif action.startswith("delete-"):
                            self.delete(item, selected)
                        else:
                            transfers[action].append(item)
                    except (OSError, SyncError) as exc:
                        self.hold(path, str(exc), selected)
                for direction, items in transfers.items():
                    self.transfer(direction, items, selected)
                if reconciliation:
                    done = {r["path"] for r in self.results if r.get("action") != "waiting"}
                    if isinstance(reconciliation['pending'], PendingRows):
                        reconciliation['pending'].discard_many(done)
                    else:
                        reconciliation["pending"] = [p for p in reconciliation["pending"] if p not in done]
                    if not reconciliation["pending"]:
                        self.state["reconciled_request"] = reconciliation["id"]
                        self.state.pop("reconcile")
                self.save()
                from .conflict_archive import maintain
                maintain(self.config)
                if self.attempt:
                    self._check(update_attempt(self.config.state_dir, "pushd", self.attempt,
                        phase="completed", status="completed", requires_child_exit_confirmation=False,
                        results=self.results, receipt_directory=str(self.receipt_dir)))
                counts = {}
                for row in self.results:
                    action = row.get("action", "unknown")
                    counts[action] = counts.get(action, 0) + 1
                operation.update(status="success", last_result={"status": "success", "started_at": operation["started_at"],
                    "finished_at": now(), "counts": counts, "review_count": len(self.state["reviews"]),
                    "reconcile_remaining": len(self.state.get("reconcile", {}).get("pending", []))})
                atomic_write_json(operation_file, operation)
                self.progress("idle", len(paths), len(paths))
            except BaseException as exc:
                self.progress("failed")
                operation.update(status="failed", last_result={"status": "failed", "started_at": operation["started_at"],
                    "finished_at": now(), "error_type": type(exc).__name__})
                atomic_write_json(operation_file, operation)
                if self.attempt:
                    update_attempt(self.config.state_dir, "pushd", self.attempt,
                                   phase="needs-recovery", status="blocked", results=self.results)
                raise
        return {"results": self.results, "conflict archives": self.archive_results, "reviews": list(self.state["reviews"].values()),
                "reconcile remaining": len(self.state.get("reconcile", {}).get("pending", [])),
                "sync policy": "event", "state file": str(state_path(self.config)),
                "backup_directory": str(self.config.core_dir / ".conflict"),
                "receipt_directory": str(self.receipt_dir) if self.receipt_dir else None}
