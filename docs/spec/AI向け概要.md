# pcloud-manager AI向け概要

Last updated: 2026-08-28

## 最初に読む場所

- 仕様書: `/Users/takafumi/p-core/dev/#仕様書/pcloud-manager/技術仕様.md`
- 現行実装ワークツリー: `/Users/takafumi/p-core/dev/pcloud-tools/`
- CLI 入口: `/Users/takafumi/p-core/dev/pcloud-tools/src/pcloud_tools/cli.py`
- public 入口: `/Users/takafumi/bin/pcloud-manager`
- release installer: `/Users/takafumi/p-core/dev/pcloud-tools/install.sh`
- release workflow: `/Users/takafumi/p-core/dev/pcloud-tools/.github/workflows/release.yml`
- release bundle builder: `/Users/takafumi/p-core/dev/pcloud-tools/scripts/build-release-bundle.sh`
- 開発用入口: `/Users/takafumi/p-core/dev/pcloud-tools/pcloud-manager-dev`

正式版は `pcloud-tools` wheelをuv tool environmentへinstallして実行する。development checkoutはsource/test/buildの正本で、public wrapperは`${XDG_DATA_HOME:-$HOME/.local/share}/pcloud-tools/bin/`のinstalled executableへ委譲する。public wrapperに`PYTHONPATH`やdevelopment `.venv`を戻してはいけない。

root help 表示は runtime で分かれる。public `pcloud-manager` は `usage: pcloud-manager ...`、dev `./pcloud-manager-dev` は `usage: pcloud-manager-dev ...` を表示する。これは `src/pcloud_tools/cli.py` の root parser が `PCLOUD_TOOLS_DEV` を見て切り替える。

初回releaseは`0.1.0`。GitHub-only distributionで、raw.githubusercontent.comの`install.sh`がGitHub Release bundleを取得し、checksum確認後に`uv tool install`する。installerはconfig/state/rclone credentials/launchd/NAS serviceを変更しない。NASへの実導入はrelease後の別taskとして、READMEとpublic diagnosticsだけで行う。

`pcloud-manager help --ai "request" --topic <topic>` は別 AI/helper 向けの read-only JSON context generator。topic は `overview`, `safety`, `mode`, `pushd`, `diffd`, `launchd`, `transfer`, `sync`, `config`。LLM 呼び出し、生成 command 実行、runtime state mutation、private/large content 読み込みは禁止。

## 現在の分割方針

`src/pcloud_tools/cli.py` は薄い dispatcher に寄せている。各 command の実装は次のモジュールへ分離する。

- `daemon`: `/Users/takafumi/p-core/dev/pcloud-tools/src/pcloud_tools/cli_daemon.py`
- `mode`: `/Users/takafumi/p-core/dev/pcloud-tools/src/pcloud_tools/cli_mode.py`
- `pushd` / `diffd` daemon surfaces: `/Users/takafumi/p-core/dev/pcloud-tools/src/pcloud_tools/cli_service_daemon.py`
- pushd/diffd state reader: `/Users/takafumi/p-core/dev/pcloud-tools/src/pcloud_tools/service_daemon_state.py`
- download suppression/conflict and upload-origin journals: `/Users/takafumi/p-core/dev/pcloud-tools/src/pcloud_tools/download_suppression.py`
- abnormal chat notify helper: `/Users/takafumi/p-core/dev/pcloud-tools/src/pcloud_tools/chat_notify.py`
- `notify`: `/Users/takafumi/p-core/dev/pcloud-tools/src/pcloud_tools/cli_notify.py`
- `sync`: `/Users/takafumi/p-core/dev/pcloud-tools/src/pcloud_tools/cli_sync.py`
- `status` / `doctor`: `/Users/takafumi/p-core/dev/pcloud-tools/src/pcloud_tools/cli_status.py`
- release/runtime docs discovery: `/Users/takafumi/p-core/dev/pcloud-tools/src/pcloud_tools/documentation.py`
- `mount` / `umount`: `/Users/takafumi/p-core/dev/pcloud-tools/src/pcloud_tools/cli_mount.py`
- `index`: `/Users/takafumi/p-core/dev/pcloud-tools/src/pcloud_tools/cli_index.py`

作業単位は reviewer が明確に切る。指示された command 以外の分割や新機能追加には進まない。

## pushd / diffd daemon surfaces

`pcloud-pushd` / `pcloud-diffd` はlocal eventとremote changeを別queueで扱い、bounded executorがeligible recordを転送する。bisync/autosyncとdaemon loopは排他運用で、横断状態は`pcloud-manager mode status|plan|switch`が担当する。

主要な設計:

- `pcloud-pushd` はsync scope内eventを`enqueued_at`付きで`state_dir/pushd/queue.json`へappendする。resident watcherとbounded executorは別processで、current stateは`pcloud-manager pushd status --json`を見る。
- `pcloud-diffd` はpCloud `/diff`のcursorとfolder cacheを保持し、scope内changeを`state_dir/diffd/remote-changes.json`へappendする。pollerとbounded executorは別processで、current stateは`pcloud-manager diffd status --json`を見る。
- public LaunchAgentのwrite/reloadはすべてterminal human gateが必要。文書には特定machineのloaded状態や検証結果を保存しない。
- `mode status` は read-only で daemon 4本、bisync/autosync、dirty state を見る。`mode plan daemon|maintenance|pause` は予定 `launchctl` 操作を表示するだけ。`mode switch` は `PCLOUD_TOOLS_MODE_SWITCH_GATE=operator-approved-mode-switch-v1` と review flags が揃うまで実行しない。mode switch は transfer、normal sync/resync、listing cache、diffd checkpoint を実行しない。
- `pushd transfer executor-run` / `diffd transfer executor-run` は dev-state fake-rclone 専用の queue executor tick。pushd queue は transfer 命令ではなく filesystem change candidate。pushd は `state_dir/pushd/upload-candidates.json` の size / mtime_ns fingerprint が `PCLOUD_TOOLS_PUSHD_UPLOAD_SETTLE_SECONDS` 続いた path だけ upload する。executor tick 時点で存在しない `upload` 候補は、過去のupload成功履歴に関係なく即時・無通知でpruneする。以前uploadしたpathに対する明示的な`delete` / `rename` eventだけmanual reviewに残す。rclone の `source file is being updated` は tolerated settling として queue を保持し、通知しない。
- `pushd launchd executor-plist` / `diffd launchd executor-plist` は dev-state fake-rclone queue executor 用の StartInterval LaunchAgent plist surface。`.dev-state/launchd/com.example.pcloud-*-executor.dev.plist` だけを書ける。public LaunchAgent write、`launchctl` 実行、real transfer automation は行わない。
- `pushd transfer automation-gate` / `diffd transfer automation-gate` は public real-transfer queue executor automation の read-only gate。予定 public executor label/plist/StartInterval と automation command readiness を表示し、reviewer/operator approval が揃うまで public plist write / `launchctl` / automatic real transfer は行わない。
- `pushd transfer real-gate` / `diffd transfer real-gate` と `real-run` は、`--confirm-path` + `--confirm-direction` が planned transfer の exactly 1 件に一致した場合、その selected transfer だけを manual first-run 対象にできる。複数 planned record があっても、manual `real-run` は selected 1 件だけを実行し、`--consume-policy remove-on-success-retain-on-failure` の承認下では成功した matching record だけを消費し、他 record は触らない。
- `pushd transfer automation-run` / `diffd transfer automation-run` は実装済みの gated automatic real-transfer executor tick。real-transfer gate、automation gate、automation-run gate、saved shadow validation report、non-`fake-rclone` rclone、`--execute`、`--consume-on-success` が揃うまで拒否する。manual-review record は安全に除外・保持し、他のeligible transferを止めず、automation errorや反復chat通知にも転換しない。`pushd automation-run --execute` は gate が揃った tick 冒頭で missing-local upload cleanup を行ってから plan を組む。default は one transfer record per tick (`--max-records 1`) で、成功 record だけ consume し、失敗/不明/deferred record は保持する。public automation launchd review は confirmed selected target 1 件を current bounded automation tick として review でき、他 planned records は deferred として残す。
- queue が空の public automation launchd review は、直近の successful manual `real-run` を validation evidence として使える。last transfer は `mode: real-rclone-transfer`、service/direction 一致、successful result 1 件以上、failed/timeout 0 件が必要。これにより manual validation で queue を drain した後でも executor LaunchAgent を review できる。
- `pushd launchd automation-plist` / `diffd launchd automation-plist` と `automation-reload` は public real-transfer executor LaunchAgent write / bootout-bootstrap gate。public payloadはbounded executionを維持する。
- diffd download transfer は staging finalization に変わった。`state_dir/diffd/download-staging/` に rclone download してから、転送前に取った destination fingerprint と比較する。destination が変わっていなければ replace して completed suppression journal を記録する。変わっていれば existing local file を残し、downloaded content を `name.conflict-YYYYMMDD-HHMMSS.ext` に移し、remote-change record は manual review 用に保持する。
- download suppression journal は `state_dir/diffd/download-suppression-journal.json`。completed record の TTL は `PCLOUD_TOOLS_DOWNLOAD_SUPPRESSION_TTL_SECONDS` default 86400 秒。pushd plan は active/completed matching download を excluded として扱い、local fingerprint が変わったら user edit とみなして upload planning を許す。
- upload-origin journal は `state_dir/pushd/upload-origin-journal.json`。pushd upload 成功時に local fingerprint を保存し、diffd plan は same-path `diff:createfile` remote echo を `upload origin journal` として skipped にする。remote-side edit (`diff:modifyfile`) は suppression せず download planning に回す。local fingerprint が変わった場合も download planning を再度許す。
- upload-candidate journal は `state_dir/pushd/upload-candidates.json`。`stable_since` / current fingerprint / `uploaded_at` を保持し、録音中・コピー中・rename の eligibility と、未 upload 一時 path / 成功済み削除候補の区別に使う。実装は `/Users/takafumi/p-core/dev/pcloud-tools/src/pcloud_tools/service_daemon_plan.py` を先に読む。
- abnormal chat notify は default off。`pcloud-manager notify status|enable|disable|test` で `PCLOUD_TOOLS_CHAT_NOTIFY_ENABLED` を切り替える。`PCLOUD_TOOLS_CHAT_NOTIFY_CMD` default は `~/bin/notify send --to discord {message}`。通知対象は conflict、transfer timeout/failure。manual-review保留、通常success/no-op tickは通知しない。
- pushd/diffd transfer plan は sync scope/default-exclude 判定後に hidden path component を excluded/skipped にする。macOS screenshot 作成時の `screenshots/.shot...` / `screenshots/..shot...` のような transient dotfile は automatic transfer 対象にしない。
- `pushd queue prune-excluded` は excluded queue record だけを cleanup する surface。preview は read-only。public `--execute` は `--reviewer-approved-excluded-record-cleanup` と `PCLOUD_TOOLS_PUSHD_QUEUE_PRUNE_EXCLUDED_GATE=operator-approved-pushd-queue-prune-excluded-v1` が必要。
- `pushd status --xbar` / `diffd status --xbar` が現状確認の最短経路。`launchd status` は read-only `launchctl print` のみ。
- normal sync/resync、listing cache 操作、autosync launchd changes は別 gate のまま。pushd upload transfer と diffd download transfer は bounded public executor tick だけ live。

- CLI: `./pcloud-manager-dev pushd status|preview|policy`, `./pcloud-manager-dev diffd status|preview|policy`
- action id: `pushd.status.refresh`, `pushd.preview`, `pushd.policy`, `diffd.status.refresh`, `diffd.preview`, `diffd.policy`
- state reader: `.dev-state/state/{pushd,diffd}/` in dev mode
- config keys: `PCLOUD_TOOLS_PUSHD_DEBOUNCE_SECONDS`, `PCLOUD_TOOLS_PUSHD_QUEUE_LIMIT`, `PCLOUD_TOOLS_DIFFD_POLL_INTERVAL_SECONDS`, `PCLOUD_TOOLS_DIFFD_BATCH_LIMIT`, `PCLOUD_TOOLS_DOWNLOAD_SUPPRESSION_TTL_SECONDS`, `PCLOUD_TOOLS_CHAT_NOTIFY_ENABLED`, `PCLOUD_TOOLS_CHAT_NOTIFY_CMD`
- plan helper: `/Users/takafumi/p-core/dev/pcloud-tools/src/pcloud_tools/service_daemon_plan.py`
- `pushd preview` reads `.dev-state/state/pushd/queue.json` and applies sync scope/default excludes to produce upload/excluded/invalid counts
- `diffd preview` reads `.dev-state/state/diffd/remote-changes.json` plus `.dev-state/state/daemon/pending-downloads.json` and produces remote-change/pending/planned download counts
- `pushd status` / `diffd status` are read-only operator summaries. They include plan counts, manual-review counts, latest resident/API-poll last-run summaries, concise gate status, launchd registration status, last transfer summary, download suppression/conflict counts, upload-origin echo suppression counts, chat notify mode, and safe next preview/status/check actions without consuming queue/change files. They do not embed detailed launchd approval checklist data
- `pushd status --xbar` / `diffd status --xbar` use concise xbar-specific output: compact plan, last-run, launchd, gate, suppression/conflict, upload echo, and notify lines plus read-only actions only. They do not show full last-transfer payloads or real-run/real-gate/consume/clear shortcuts
- `gates status --xbar` uses concise xbar-specific output: gate counts, one compact line per gate, and safe status refresh actions only. It does not show full guarded command examples, execution gate env values, or `--execute` paths
- `pushd policy` / `diffd policy` document the current daemon scope as queue-only / diffid-only. These reports keep automatic upload/download execution, normal sync/resync, and listing cache operations out of scope
- manual plan-state commands: `pushd queue add|remove|clear`, `diffd remote-change add|remove|clear`
- manual plan-state `--execute` writes only `.dev-state/state/pushd/queue.json` or `.dev-state/state/diffd/remote-changes.json`
- `--execute` must reject even with `PCLOUD_TOOLS_DEV=1` when `PCLOUD_TOOLS_STATE_DIR` is outside `workspace/.dev-state/state`
- one-shot dry-run commands: `pushd run`, `diffd run`
- dry-run `--execute` writes only `last-plan.json`, `last-event.json`, and `cursor` under `.dev-state/state/{pushd,diffd}/`
- real-operation gate commands: `pushd gate`, `diffd gate`
- gate commands are read-only and keep any expansion beyond the currently approved queue-only fswatch resident and bounded API one-shot blocked until a separate operator/reviewer gate is opened, especially real upload/download execution
- launchd registration gate commands: `pushd launchd gate`, `diffd launchd gate`
- launchd gate commands are read-only; they show draft labels, plist paths/payloads, foreground daemon command previews, and bootstrap/rollback command examples, but do not write plists, run `launchctl`, start persistent daemons, execute transfers, run normal sync/resync, or touch listing caches
- launchd status commands: `pushd launchd status`, `diffd launchd status`
- launchd status commands are read-only; they show label/plist presence and `launchctl print gui/<uid>/<label>` status when `launchctl` is available, but do not write plists, run enable/bootstrap/bootout/disable, start daemons, execute transfers, run normal sync/resync, or touch listing caches
- launchd review commands: `pushd launchd review`, `diffd launchd review`
- launchd review commands are read-only human-review bundles before public plist write or registration. They show the public label, public plist path/payload, foreground command preview, and terminal review commands; they do not write plists, run `launchctl`, start daemons, execute transfers, run normal sync/resync, or touch listing caches
- launchd plist commands: `pushd launchd plist`, `diffd launchd plist`
- launchd plist commands preview or, with default `--execute`, write only the dev `.dev-state/launchd/*.plist` LaunchAgent review files. They do not run `launchctl`, start daemons, execute transfers, run normal sync/resync, touch listing caches, or write outside dev mode
- public launchd plist write is a separate gate: `--execute --public-write` writes only one `~/Library/LaunchAgents/com.takafumi.pcloud-{pushd,diffd}.plist` when the service-specific `PCLOUD_TOOLS_{PUSHD,DIFFD}_LAUNCHD_PLIST_GATE=operator-approved-*-launchd-plist-v1` value and all review flags are present. This gate still performs no `launchctl` registration/bootstrap and starts no persistent daemon
- operational pushd resident plist command: `pushd launchd resident-plist`
- resident plist preview shows the operational queue-only fswatch LaunchAgent payload with `PATH`, `PCLOUD_TOOLS_PUSHD_FSWATCH_RESIDENT_GATE`, resident approval flags, `--execute`, and an absolute shadow report path. `--execute` requires `PCLOUD_TOOLS_PUSHD_LAUNCHD_RESIDENT_PLIST_GATE=operator-approved-pushd-launchd-resident-plist-v1` and resident review flags; it writes the plist only and does not bootout/bootstrap
- operational pushd launchd reload command: `pushd launchd reload`
- reload preview shows `launchctl bootout` followed by `launchctl bootstrap` for the loaded `com.takafumi.pcloud-pushd` service. `--execute` requires an operational resident plist, saved ok shadow validation report, reload review flags, and `PCLOUD_TOOLS_PUSHD_LAUNCHD_RELOAD_GATE=operator-approved-pushd-launchd-reload-v1`; real reload remains human approved
- operational diffd live API one-shot plist command: `diffd launchd resident-plist`
- diffd operational plist preview shows the bounded long-poll LaunchAgent payload with `PATH`, `PCLOUD_TOOLS_DIFFD_API_LONG_POLL_GATE`, `diffd api-poll long-poll-run --live-api --max-iterations 1 --execute`, API approval flags, an absolute shadow report path, and optional `StartInterval` from `--start-interval-seconds`. `--execute` requires `PCLOUD_TOOLS_DIFFD_LAUNCHD_LONG_POLL_PLIST_GATE=operator-approved-diffd-launchd-long-poll-plist-v1` and review flags; it writes the plist only and does not bootout/bootstrap
- operational diffd launchd reload command: `diffd launchd reload`
- diffd reload preview shows `launchctl bootout` followed by `launchctl bootstrap` for `com.takafumi.pcloud-diffd`. `--execute` requires an operational bounded long-poll plist, saved ok shadow validation report, reload review flags, and `PCLOUD_TOOLS_DIFFD_LAUNCHD_RELOAD_GATE=operator-approved-diffd-launchd-reload-v1`; download transfer, normal sync/resync, listing cache operations, and autosync launchd changes stay blocked
- dev queue executor launchd plist command: `pushd launchd executor-plist`, `diffd launchd executor-plist`
- executor-plist preview/write is dev-state only. The payload runs `<dev entrypoint> <service> transfer executor-run --execute --consume-on-success --json` with `PCLOUD_TOOLS_DEV=1`, `.dev-state/state`, `.dev-state/logs`, `PCLOUD_TOOLS_TRANSFER_EXECUTION_GATE=dev-fake-rclone`, and `.dev-state/bin/fake-rclone`. `--execute` writes only `.dev-state/launchd/com.example.pcloud-*-executor.dev.plist`; it does not write public LaunchAgents or run launchctl
- launchd register commands: `pushd launchd register`, `diffd launchd register`
- launchd register commands preview the planned `launchctl enable` + `bootstrap` commands. `--execute` is the separate registration gate and requires public non-dev runtime, existing public plist, saved ok shadow validation report, all launchd approval flags, and `PCLOUD_TOOLS_{PUSHD,DIFFD}_LAUNCHD_GATE=operator-approved-*-launchd-v1`. Tests/shadow validation use fake `launchctl`; real registration must remain human approved
- fixture-backed fswatch parser command: `pushd fswatch preview --fixture <path>`
- fswatch fixture preview reads only the supplied fixture, starts no fswatch process, writes no pushd state, and reuses sync scope/default-exclude classification for planned uploads
- fswatch delete/remove and rename/move flags are preserved as `delete` / `rename` queue actions instead of being treated as automatic upload work; transfer preview routes those actions to manual review
- pushd resident queue append skips duplicate path/action records already present in the queue and refuses new resident appends after `PCLOUD_TOOLS_PUSHD_QUEUE_LIMIT`
- pushd resident debounce skips upload path/action events that match the latest successful resident append within `PCLOUD_TOOLS_PUSHD_DEBOUNCE_SECONDS`
- one-shot fswatch probe preview command: `pushd fswatch probe`
- fswatch probe preview checks command availability and reports the command argv only; it does not start fswatch and leaves the real-operation gate closed
- fixture-backed pCloud diff parser command: `diffd diff preview --fixture <path>`
- diff fixture preview reads only the supplied fixture, makes no pCloud API call, writes no diffd state, and reuses diffd download-plan classification
- diffd download-plan classification applies the sync scope file and default excludes before planned downloads are emitted; skipped remote records remain visible in the report
- one-shot pCloud API poll preview command: `diffd api-poll preview`
- API poll preview reports request method/path/query only; it makes no pCloud API call, configures no credential, writes no diffd state, and leaves the real-operation gate closed
- guarded pCloud API long-poll run records failure state only after a gated live API attempt fails; it retains the current diffid and remote-change records, includes retry/backoff metadata, and does not run an automatic retry loop
- transfer executor preview commands: `pushd transfer preview`, `diffd transfer preview`
- transfer previews emit concise human summaries and detailed `--json` planned `rclone copyto` argv only; delete/rename/move-style records and same-path pushd/diffd conflicts go to manual review and are excluded from planned transfer commands. They do not execute rclone, upload, download, or write service state
- real-transfer validation matrix commands: `pushd transfer validation-matrix`, `diffd transfer validation-matrix`
- validation matrix commands are read-only. They list small txt, Japanese filename, space filename, nested path, overwrite, and remote-only download cases with setup -> preview -> final-review check -> cleanup command examples, but execute none of them, write no state, and keep actual upload/download behind a dedicated real-transfer gate plus human confirmation
- real-transfer gate checklist commands: `pushd transfer check`, `diffd transfer check`
- transfer checks are read-only; human output is intentionally concise, while `--json` keeps the full AI/reviewer audit detail. They show saved shadow validation report status, accept `--sample-path <relative in-scope path>` for the displayed dev-state sample setup, require temp workspace/state guard and unsafe state dir guard checks, show first planned transfer, emit a dev-state-only setup -> preview -> check -> cleanup review command sequence when the plan is empty, list operator/reviewer pending approvals, and keep the real rclone/pCloud transfer gate closed
- dev fake-rclone transfer execution commands: `pushd transfer run --execute`, `diffd transfer run --execute`
- transfer execution requires `PCLOUD_TOOLS_TRANSFER_EXECUTION_GATE=dev-fake-rclone`, `PCLOUD_TOOLS_RCLONE_BIN` resolving to an executable named `fake-rclone` under `workspace/.dev-state/`, and `PCLOUD_TOOLS_STATE_DIR` under `workspace/.dev-state/state`
- fake-rclone transfer runs use `PCLOUD_TOOLS_TRANSFER_EXEC_TIMEOUT_SECONDS`, start fake-rclone in its own process group, clean up that fake process group on timeout, and write only `last-transfer.json` under `.dev-state/state/{pushd,diffd}/` for successful or failed fake execution attempts; queue/change files are not consumed and real rclone/pCloud upload-download remains blocked
- consume policy preview commands: `pushd transfer consume preview`, `diffd transfer consume preview`
- consume previews read latest dev-state `last-transfer.json` and current queue/change state, then show which successful fake-rclone records would be removed; they are read-only and do not consume queue/change files
- guarded consume commands: `pushd transfer consume run --execute`, `diffd transfer consume run --execute`
- guarded consume is dev-state only; it removes matching queue/change records for successful fake-rclone results and does not open real rclone/pCloud transfer
- dev queue executor tick commands: `pushd transfer executor-run`, `diffd transfer executor-run`
- executor-run previews planned transfer commands and manual-review blockers. `pushd executor-run --execute` immediately consumes missing `upload` candidates before planning, regardless of successful-upload history; these normal transient records do not require an xbar action. Explicit `delete` / `rename` events for previously uploaded paths remain manual review, and the xbar review row opens their transfer preview in Terminal. `--execute --consume-on-success` runs the dev fake-rclone transfer path, then consumes matching successful queue/change records. It remains dev-state only, keeps `real transfer automation gate status: closed`, refuses manual-review records before fake-rclone start, and does not enable live automatic upload/download
- real transfer automation gate commands: `pushd transfer automation-gate`, `diffd transfer automation-gate`
- automation-gate is read-only. It reuses the real-transfer final-review checklist, shows `PCLOUD_TOOLS_REAL_TRANSFER_AUTOMATION_GATE=operator-approved-real-transfer-automation-v1`, lists the planned public executor label/plist/StartInterval, and reports `automation command status: implemented-gated` while keeping `public plist writes: no`, `launchctl execution: no`, and `automatic real transfer execution: no`
- real transfer automation run commands: `pushd transfer automation-run`, `diffd transfer automation-run`
- automation-run is the public executor tick for automatic real transfer, but it is closed by default. It requires the real transfer gate, automation gate, automation-run gate, saved ok shadow validation report, `--execute`, `--consume-on-success`, and non-`fake-rclone` rclone before it transfers or consumes any record. Manual-review records are excluded and retained without blocking eligible records or producing recurring automation errors. `pushd automation-run --execute` performs the same missing-local startup cleanup once those gates are satisfied, before transfer planning. Each tick is bounded by `--max-records`, defaulting to `1`
- future public automation launchd previews: `pushd launchd automation-plist`, `pushd launchd automation-reload`, `diffd launchd automation-plist`, `diffd launchd automation-reload`
- automation-plist previews the public executor plist payload and can write it only when terminal review flags, saved shadow report, automation gate review, public wrapper check, service-specific plist gate env, and public non-dev runtime all pass. The public payload includes `automation-run --execute --consume-on-success --max-records 1`. automation-reload previews service-specific `bootout` -> `bootstrap` and can execute it only when an operational automation plist, launchctl, terminal review/rollback flags, and service-specific reload gate env all pass
- extra action id: `pushd.policy`, `pushd.run.preview`, `pushd.backfill.preview`, `pushd.gate`, `pushd.launchd.gate`, `pushd.launchd.status`, `pushd.launchd.review`, `pushd.launchd.register.preview`, `pushd.launchd.reload.preview`, `pushd.launchd.resident-plist.preview`, `pushd.launchd.plist.preview`, `pushd.transfer.preview`, `pushd.transfer.validation-matrix`, `pushd.transfer.check`, `pushd.queue.clear.preview`, `diffd.policy`, `diffd.run.preview`, `diffd.gate`, `diffd.launchd.gate`, `diffd.launchd.status`, `diffd.launchd.review`, `diffd.launchd.register.preview`, `diffd.launchd.reload.preview`, `diffd.launchd.resident-plist.preview`, `diffd.launchd.plist.preview`, `diffd.transfer.preview`, `diffd.transfer.validation-matrix`, `diffd.transfer.check`, `diffd.remote-change.clear.preview`
- shadow validation prep script: `/Users/takafumi/p-core/dev/pcloud-tools/scripts/pcloud-shadow-validation.py`
- validation script uses a temp workspace and only exercises dev preview / dry-run / gate / fixture parsers / action / safety-guard / historical last-error display paths
- validation reports can be saved with `--report-path`; guarded changes remain blocked unless the saved report has `status: ok` and every check is `ok`

まだ閉じたままにしているもの:

- diffd 側の実 rclone/pCloud download
- automatic transfer execution from diffd queue
- normal sync/resync from daemon validation flow
- listing cache operations
- autosync launchd changes
- old monolith legacy archive

## sync の扱い

`sync` はこの CLI の中で最も重い部分で、`cli_sync.py` だけで 1000 行超ある。scope、lock、background、autosync、internal run、rclone plan、dev-mode guard が絡むため、小さな command handler ではなくサブシステムとして扱う。

今後 `sync` をさらに触る場合、`cli_sync.py` 内をさらに分割する前に、代表経路のテストを先に固める。最低限、次の経路を安全な dev runtime で検証できるようにしてから構造変更する。

- `sync --json`
- `sync status --json`
- `sync status --json` / `status --detail --json` label stale `last error` records as `historical` when latest result is success
- `sync background --json`
- `sync scope --json`
- `sync check-scope --json`
- `sync progress --json`
- `sync clear-stale-lock --json`
- `action sync.preview`
- config error 時に副作用が起きないこと
- `HOME` / `XDG_CACHE_HOME` を tmp 配下へ固定し、live rclone state を読まないこと

特に `bisync_listing_recovery_state()` は `Path.home()/Library/Caches/rclone/bisync` を参照し得る。テストでは必ず `HOME` を tmp 配下に固定する。

## 変更後の確認

基本確認:

```sh
cd /Users/takafumi/p-core/dev/pcloud-tools
python3 -m compileall src
./.venv/bin/python -m pytest tests/test_cli_invariants.py -q
./pcloud-manager-dev pushd status --json
./pcloud-manager-dev pushd preview --json
./pcloud-manager-dev diffd status --json
./pcloud-manager-dev diffd preview --json
./pcloud-manager-dev pushd gate --json
./pcloud-manager-dev diffd gate --json
./pcloud-manager-dev pushd fswatch preview --fixture <fixture> --json
./pcloud-manager-dev pushd fswatch probe --json
./pcloud-manager-dev diffd diff preview --fixture <fixture> --json
./pcloud-manager-dev diffd api-poll preview --json
./pcloud-manager-dev pushd launchd gate --json
./pcloud-manager-dev pushd transfer preview --json
./pcloud-manager-dev pushd transfer validation-matrix --json
./pcloud-manager-dev pushd transfer check --json
./pcloud-manager-dev pushd transfer consume preview --json
./pcloud-manager-dev diffd transfer preview --json
./pcloud-manager-dev diffd launchd gate --json
./pcloud-manager-dev diffd transfer validation-matrix --json
./pcloud-manager-dev diffd transfer check --json
./pcloud-manager-dev diffd transfer consume preview --json
# Only with a repo-local fake-rclone under .dev-state/ and the dev-fake-rclone gate:
# ./pcloud-manager-dev pushd transfer run --execute --json
# ./pcloud-manager-dev diffd transfer run --execute --json
./pcloud-manager-dev action pushd.preview
./pcloud-manager-dev action diffd.status.refresh
python3 scripts/pcloud-shadow-validation.py --json
python3 scripts/pcloud-shadow-validation.py --report-path .dev-state/reports/shadow-validation.json
git diff --check
```

代表コマンドは変更対象に合わせて追加で通す。`--execute` は dev mode で拒否される設計なので、通常レビューでは preview / JSON / xbar 経路を確認する。

変更前の判断:

- `scripts/pcloud-shadow-validation.py` の report が失敗した場合は public `pcloud-manager` を切り替えない
- public wrapper を触る前に entrypoint、backup、rollback command、停止条件を確認する
- 既承認済みの queue-only fswatch resident / bounded API one-shot を超える launchd 変更、daemon expansion、実 upload/download、old monolith legacy archive には進まない

## pcloud-archive との境界

設定したlocal `source_root` から `pcloud-crypt:` の `remote_root` へ一方向copy/checkする用途は `pcloud-manager` へ追加せず、別 command `/Users/takafumi/p-core/bin/pcloud-archive` が担当する。crypt mountは不要で、ローカル削除はremoteへ自動伝播しない。`man pcloud-archive`、`help --detail`、`info paths` から説明を再発見できる。man pageは任意で、未設置時はdoctor issueにしない。詳細は `/Users/takafumi/p-core/dev/#仕様書/pcloud-archive/` を読む。
- failed check の `name` と `detail` を作業記録またはレビューコメントへ添えて reviewer/implementer 間で戻す
