# pcloud-tools

`pcloud-tools` is the new implementation root for the `pcloud-manager` migration.

Current focus:

- establish a Python-native command surface
- keep a development entrypoint isolated from real machine state
- preserve space for compatibility with the existing `pcloud-manager` workflow
- move machine configuration into `.env` plus a central Python config module
- operate `pcloud-pushd` / `pcloud-diffd` through preview-first, human-gated launchd surfaces while keeping each live transfer/polling scope explicit
- keep legacy bisync/autosync and the pushd/diffd daemon loop mutually exclusive through a top-level `mode` surface

Current live daemon state:

- public wrapper: `/Users/takafumi/bin/pcloud-manager` -> `/Users/takafumi/.zsh/functions/pcloud-manager` -> Python implementation in `/Users/takafumi/p-core/dev/pcloud-tools`
- public help uses the public program name: `pcloud-manager` prints `usage: pcloud-manager ...` and the public CLI description; `./pcloud-manager-dev` keeps the dev-only name and description
- `pcloud-manager mode status|plan|switch` is the exclusive operation switch. `daemon` mode keeps pushd/diffd residents and executors active while bisync stays disabled. `maintenance` and `pause` stop daemon automation; they do not run or enable bisync automatically
- `pcloud-pushd`: `com.takafumi.pcloud-pushd` is loaded and running as a launchd fswatch resident; validation confirmed one allowlisted queue append and cleanup back to `queued=0`
- `pcloud-diffd`: `com.takafumi.pcloud-diffd` is loaded with a gated bounded live API one-shot payload on launchd `StartInterval=60`; observed periodic runs advance `diffid`, skip records outside the current plan, and have appended 0 download records so far
- `diffd launchd resident-plist --start-interval-seconds N` can preview/write the gated `StartInterval` plist for bounded one-shot API polling; the current public plist has already been terminal-reviewed, written, and reloaded for 60-second polling
- `pushd transfer executor-run` / `diffd transfer executor-run` can execute one dev-state fake-rclone queue tick and optionally consume successful records; public real-transfer automation ticks are separately gated and bounded
- `pushd launchd executor-plist` / `diffd launchd executor-plist` can preview/write `.dev-state/launchd/com.example.pcloud-*-executor.dev.plist` for StartInterval-driven dev fake-rclone queue executor ticks; it never writes public LaunchAgents or runs `launchctl`
- `pushd transfer automation-gate` / `diffd transfer automation-gate` are read-only gates for public real-transfer queue executor automation; they show the planned public executor label/plist/StartInterval and require explicit reviewer/operator approval before launchd work
- `pushd transfer automation-run` / `diffd transfer automation-run` are implemented but gated automatic real-transfer executor ticks. They require the real transfer gate, automation gate, automation-run gate, saved shadow validation report, non-fake rclone, `--execute`, and `--consume-on-success`; otherwise they refuse before transfer or state mutation. Public automation ticks are bounded to one transfer record per run by default
- `pushd launchd automation-plist` / `diffd launchd automation-plist` can write the public queue executor LaunchAgent plist only with terminal-reviewed flags and the service-specific plist gate. `automation-reload` can run the service-specific `bootout -> bootstrap` only with an operational plist and reload gate
- pushd public queue executor plist has been terminal-reviewed, rewritten with `--max-records 1`, and reloaded as `com.takafumi.pcloud-pushd-executor`; observed launchd runs upload and consume queued screenshot records one per tick
- pushd/diffd transfer plans now apply `.pcloudmanagerignore` after allowlist/default-exclude checks. The default template ignores macOS/transient dot paths while allowing shareable dot samples such as `.env.sample` through `!` exception rules
- `pushd queue prune-excluded` previews excluded queue records, and `--execute` can remove only those excluded records after the dev-state guard or public `PCLOUD_TOOLS_PUSHD_QUEUE_PRUNE_EXCLUDED_GATE=operator-approved-pushd-queue-prune-excluded-v1` plus reviewer approval
- live automatic upload/download transfer is open only through bounded public queue executor ticks. Diffd API polling is live at 60-second intervals. Normal sync/resync from these daemon paths and listing cache operation remain closed
- diffd downloads now stage into `state_dir/diffd/download-staging/` before final placement. Successful finalized downloads update `state_dir/diffd/download-suppression-journal.json`, and pushd excludes matching fswatch/upload queue records while the downloaded file fingerprint remains unchanged
- pushd uploads now write `state_dir/pushd/upload-origin-journal.json` after successful transfer. Diffd excludes matching `diff:createfile` remote echo download records while the local file fingerprint remains unchanged, so a local-origin newly-created upload does not get downloaded back immediately; remote-side edits such as `diff:modifyfile` still plan downloads
- if a local same-path file changes during a diffd download, the existing local file is retained, the downloaded content is moved to `name.conflict-YYYYMMDD-HHMMSS.ext`, the remote-change record is retained for manual review, and status/xbar shows the conflict count plus latest conflict
- abnormal Discord notifications are optional and off by default. `pcloud-manager notify status|enable|disable|test` controls `PCLOUD_TOOLS_CHAT_NOTIFY_ENABLED`; when enabled, only abnormal events such as conflicts, transfer timeouts/failures, or manual-review blockage notify via `PCLOUD_TOOLS_CHAT_NOTIFY_CMD`

Public entrypoint:

```sh
pcloud-manager
pcloud-manager --help
pcloud-manager help
pcloud-manager help --ai "inspect pushd launchd status safely" --topic pushd --topic launchd
pcloud-manager info
pcloud-manager info paths
pcloud-manager status --json
pcloud-manager doctor --json
pcloud-manager mode status
pcloud-manager mode plan maintenance
pcloud-manager pushd status --xbar
pcloud-manager diffd status --xbar
pcloud-manager notify status --xbar
```

Development entrypoint:

```sh
./pcloud-manager-dev status
./pcloud-manager-dev help --ai "inspect diffd safely" --topic diffd
./pcloud-manager-dev info --json
./pcloud-manager-dev info config
./pcloud-manager-dev status --detail --json
./pcloud-manager-dev status --xbar
./pcloud-manager-dev doctor
./pcloud-manager-dev doctor --repair
./pcloud-manager-dev mode status --json
./pcloud-manager-dev mode plan daemon
./pcloud-manager-dev mode plan maintenance
./pcloud-manager-dev mode plan pause
./pcloud-manager-dev sync status --json
./pcloud-manager-dev sync status --xbar
./pcloud-manager-dev action sync.status.refresh
./pcloud-manager-dev sync
./pcloud-manager-dev sync background --no-notify
./pcloud-manager-dev notify status --xbar
./pcloud-manager-dev notify enable
./pcloud-manager-dev notify disable
./pcloud-manager-dev notify test
./pcloud-manager-dev sync clear-stale-lock
./pcloud-manager-dev sync enable-autosync
./pcloud-manager-dev sync disable-autosync
./pcloud-manager-dev sync autosync-plist
./pcloud-manager-dev sync autosync-gate
./pcloud-manager-dev sync migration-gate
./pcloud-manager-dev sync migration-gate --sync-status-report-path .dev-state/reports/sync-status.json
./pcloud-manager-dev archive old-monolith-gate
./pcloud-manager-dev gates status
./pcloud-manager-dev gates status --show-command-examples
./pcloud-manager-dev daemon status --json
./pcloud-manager-dev daemon set-diffid 12345
./pcloud-manager-dev daemon auto-download on
./pcloud-manager-dev daemon pending-download add dev-fixtures/Documents/example.pdf --diffid 12345
./pcloud-manager-dev daemon notification record "remote changes detected"
./pcloud-manager-dev pushd status --json
./pcloud-manager-dev pushd preview
./pcloud-pushd status --json
./pcloud-pushd preview
./pcloud-pushd policy
./pcloud-manager-dev pushd run
./pcloud-manager-dev pushd gate
./pcloud-manager-dev pushd launchd gate
./pcloud-manager-dev pushd launchd status
./pcloud-manager-dev pushd launchd review
./pcloud-manager-dev pushd launchd resident-plist
./pcloud-manager-dev pushd launchd executor-plist
./pcloud-manager-dev pushd launchd automation-plist
./pcloud-manager-dev pushd launchd automation-reload
./pcloud-manager-dev pushd launchd reload
./pcloud-manager-dev pushd fswatch preview --fixture tests/fixtures/pushd-fswatch-events.txt
./pcloud-manager-dev pushd fswatch probe
./pcloud-manager-dev pushd fswatch resident-gate
./pcloud-manager-dev pushd fswatch resident-run
./pcloud-manager-dev pushd transfer preview
./pcloud-manager-dev pushd transfer validation-matrix
./pcloud-manager-dev pushd transfer check
./pcloud-manager-dev pushd transfer check --confirm-path dev-fixtures/Documents/example.pdf --confirm-direction upload --consume-policy remove-on-success-retain-on-failure --timeout-policy reuse-fake-rclone-cleanup --final-review
./pcloud-manager-dev pushd transfer real-gate --confirm-path dev-fixtures/Documents/example.pdf --confirm-direction upload --consume-policy remove-on-success-retain-on-failure --timeout-policy reuse-fake-rclone-cleanup --operator-reviewed-dry-run --reviewer-approved-real-command --reviewer-approved-consume-policy
./pcloud-manager-dev pushd transfer automation-gate
./pcloud-manager-dev pushd transfer automation-run --execute --consume-on-success
./pcloud-manager-dev pushd transfer real-run --execute
./pcloud-manager-dev pushd transfer consume preview
./pcloud-manager-dev pushd transfer executor-run
./pcloud-manager-dev pushd transfer executor-run --execute --consume-on-success
./pcloud-manager-dev pushd queue add dev-fixtures/Documents/example.pdf
./pcloud-manager-dev pushd queue remove dev-fixtures/Documents/example.pdf
./pcloud-manager-dev pushd queue prune-excluded
./pcloud-manager-dev pushd queue clear
./pcloud-manager-dev diffd status --json
./pcloud-manager-dev diffd preview
./pcloud-diffd status --json
./pcloud-diffd preview
./pcloud-diffd policy
./pcloud-manager-dev diffd run
./pcloud-manager-dev diffd gate
./pcloud-manager-dev diffd launchd gate
./pcloud-manager-dev diffd launchd status
./pcloud-manager-dev diffd launchd review
./pcloud-manager-dev diffd launchd resident-plist
./pcloud-manager-dev diffd launchd resident-plist --start-interval-seconds 60
./pcloud-manager-dev diffd launchd executor-plist --start-interval-seconds 60
./pcloud-manager-dev diffd launchd automation-plist --start-interval-seconds 60
./pcloud-manager-dev diffd launchd automation-reload --start-interval-seconds 60
./pcloud-manager-dev diffd launchd reload
./pcloud-manager-dev diffd diff preview --fixture tests/fixtures/pcloud-diff.json
./pcloud-manager-dev diffd api-poll preview
./pcloud-manager-dev diffd api-poll long-poll-gate
./pcloud-manager-dev diffd api-poll long-poll-run --live-api
./pcloud-manager-dev diffd transfer preview
./pcloud-manager-dev diffd transfer validation-matrix
./pcloud-manager-dev diffd transfer check
./pcloud-manager-dev diffd transfer check --confirm-path dev-fixtures/Documents/example.pdf --confirm-direction download --consume-policy remove-on-success-retain-on-failure --timeout-policy reuse-fake-rclone-cleanup --final-review
./pcloud-manager-dev diffd transfer real-gate --confirm-path dev-fixtures/Documents/example.pdf --confirm-direction download --consume-policy remove-on-success-retain-on-failure --timeout-policy reuse-fake-rclone-cleanup --operator-reviewed-dry-run --reviewer-approved-real-command --reviewer-approved-consume-policy
./pcloud-manager-dev diffd transfer automation-gate
./pcloud-manager-dev diffd transfer automation-run --execute --consume-on-success
./pcloud-manager-dev diffd transfer real-run --execute
./pcloud-manager-dev diffd transfer consume preview
./pcloud-manager-dev diffd transfer executor-run
./pcloud-manager-dev diffd transfer executor-run --execute --consume-on-success
./pcloud-manager-dev diffd remote-change add dev-fixtures/Documents/example.pdf
./pcloud-manager-dev diffd remote-change remove dev-fixtures/Documents/example.pdf
./pcloud-manager-dev diffd remote-change clear
python3 scripts/pcloud-shadow-validation.py
python3 scripts/pcloud-shadow-validation.py --summary
python3 scripts/pcloud-shadow-validation.py --report-path .dev-state/reports/shadow-validation.json
./pcloud-manager-dev mount vault
./pcloud-manager-dev umount all
./pcloud-manager-dev index stats all
```

The development wrapper keeps config, state, and logs under `.dev-state/` in this workspace so early implementation work does not touch the live `~/.pcloud` setup.

Config notes:

- the active config file is `.dev-state/config/.env` in development mode
- the development allowlist uses `dev-fixtures/Documents/` instead of repo-root `Documents/` so real personal documents do not get materialized inside this source tree during dev validation
- the public wrapper reads live config from `/Users/takafumi/.config/pcloud-tools/.env`; `/Users/takafumi/.config` is a symlink into `/Users/takafumi/p-core/dotfiles/.config`
- the live public `.env` is machine-local and must stay ignored by the tools repo; `.env.example` captures the shareable non-secret key set for the migrated tool
- `doctor --repair` creates a starter `.env` when one does not exist
- `info` / `info paths` / `info config` expose installed/runtime paths, config source, local state/log directories, sync scope, and redacted config values without mutating state
- `status`, `doctor`, and `sync status` already support a shared JSON report schema
- `help --ai "request"` emits a read-only JSON context bundle for external AI/helper command discovery. It includes generated parser help, selected task topics, safety rules, and important paths; it does not call an LLM, execute generated commands, mutate state, or read private/large content
- JSON reports include a versioned `schema_version` and xbar-facing `actions` with command argv, terminal, and refresh metadata
- `status --xbar` and `sync status --xbar` render the same report contract as an xbar menu
- `action <id>` dispatches stable action ids such as `status.refresh`, `sync.status.refresh`, and `sync.background.preview`
- `status` now summarizes `core`, `vault`, and `crypt` directly, while `status --detail` keeps the migration diagnostics
- `doctor` now reports a top-level summary and suspected cause before the detailed diagnostics
- `status`, `doctor`, and `sync status` now include autosync launchd diagnostics
- `status`, `doctor`, and `sync status` now include `sync lock status` (`missing` / `active` / `stale` / `invalid`)
- `sync scope` accepts `/` in `.pcloud-sync-allowlist` as the shared p-core root scope for both daemon and bisync preview paths; dangerous/generated/private paths are excluded by `.pcloudmanagerignore` and generated rclone filters
- `sync background` now previews the detached launcher command and supports `--resync`, `--track-renames`, `--notify`, and `--no-notify`
- plain `sync` now uses the same preview-first bisync plan as the resync variants, and `pcloud-manager-dev sync --execute` is refused in dev mode
- foreground `sync` / `resync` / `full-resync` / `track-renames` now reject active, stale, or invalid sync locks before building a launchable run
- sync completion notifications now follow the legacy `notify local ...` path, with `osascript` fallback when needed
- abnormal chat notifications are separate from sync completion notifications. `PCLOUD_TOOLS_CHAT_NOTIFY_ENABLED=0` disables them fully; `1` enables abnormal-only Discord notifications using `PCLOUD_TOOLS_CHAT_NOTIFY_CMD`, defaulting to `~/bin/notify send --to discord {message}`. `notify test` is terminal-free and sends one explicit test message
- sync preview now surfaces bisync listing recovery candidates, and execute recovers `path1.lst-err` / `path2.lst-err` when the primary listing files are missing
- `sync clear-stale-lock` uses the same preview-first report style and can remove a stale local sync lock from the dev state
- `sync enable-autosync` / `sync disable-autosync` use preview-first reports; `pcloud-manager-dev` keeps them non-destructive
- `sync autosync-plist` previews or, with `--execute`, writes only the dev `.dev-state/com.example.pcloud-bisync.dev.plist` LaunchAgent file. It never runs `launchctl`, starts scheduled sync, or writes outside `workspace/.dev-state`; this exists so the plist can be reviewed before the separate launchd gate
- `sync autosync-gate` is a read-only checklist before changing launchd autosync registration; it checks the saved shadow validation report, `command -v launchctl`, autosync plist presence, enable/disable preview commands, operator preview review, plist approval, launchctl policy approval, and rollback policy approval while keeping `launchd gate status: closed` and `state writes: none`. When the plist is missing, it reports `autosync plist status: missing` and a `plutil -p ...` review command without writing or generating the plist
- `sync migration-gate` is a read-only checklist before running normal sync/resync migration validation; it checks the saved shadow validation report, `command -v rclone`, latest sync result, lock state, shared allowlist scope, normal/resync preview commands, operator status review, scope approval, rollback policy approval, and stop-condition approval while keeping `migration gate status: closed` and `state writes: none`. Pass `--sync-status-report-path <sync-status.json>` to use a saved read-only `pcloud-manager sync status --json` report for the latest-result, lock, scope, and autosync fields instead of the dev workspace's sample status logs
- `daemon status` exposes diffid persistence, pending-download state, last notification state, and auto-download on/off visibility from `state_dir/daemon/`
- `daemon set-diffid`, `daemon auto-download`, `daemon pending-download`, and `daemon notification` are preview-first state commands; `--execute` only writes local daemon state files
- `pushd status` / `pushd preview` and `diffd status` / `diffd preview` expose the non-destructive daemon state surface for `pcloud-pushd` / `pcloud-diffd`; they read state under `.dev-state/state/{pushd,diffd}/` in dev mode
- `./pcloud-pushd` and `./pcloud-diffd` are thin development wrappers that delegate to `./pcloud-manager-dev pushd` and `./pcloud-manager-dev diffd`; they do not start resident daemons, call the pCloud API, or open real transfer execution
- `pushd status` / `diffd status` are the read-only operator summaries for daemon readiness. They show plan counts, manual-review counts, last transfer summary, last resident/API-poll run summary, concise gate status, launchd registration status, download suppression/conflict counts, upload-origin echo suppression counts, chat-notify mode, and next safe preview/status/check actions without mutating queue/change files
- `pushd status --xbar` / `diffd status --xbar` use a concise xbar-specific renderer. The menu body shows compact plan, last-run, launchd, gate, suppression/conflict, upload echo, and notify lines, and only exposes read-only status/preview/gate/check actions; it suppresses full last-transfer payloads, real-run/real-gate shortcuts, consume actions, validation matrix actions, and clear actions
- `pushd preview` classifies `.dev-state/state/pushd/queue.json` into planned uploads, allowlist/exclude skips, and invalid queue records; `diffd preview` combines `.dev-state/state/diffd/remote-changes.json` with daemon pending downloads into a download plan summary
- `pushd policy` / `diffd policy` are read-only daemonization policy reports. They document the queue-only / diffid-only daemon scope, keep automatic transfer execution out of scope, and explicitly block normal sync/resync plus rclone listing cache operations
- `pushd queue add|remove|clear` and `diffd remote-change add|remove|clear` are preview-first; `--execute` only writes the corresponding JSON files when the state dir is under `workspace/.dev-state/state`
- `pushd run` and `diffd run` are one-shot dry-run surfaces; `--execute` records only `last-plan.json`, `last-event.json`, and `cursor` under the dev state dir
- `pushd gate` and `diffd gate` are read-only real-operation gates; they keep any expansion beyond the currently approved queue-only fswatch resident and bounded API one-shot explicitly blocked until a separate operator/reviewer gate is opened, especially real upload/download execution
- `pushd gate` and `diffd gate` mark read-only gate diagnostics as not requiring routine operator verification; they also expose `human gate status: required-before-real-work` for any optional future real rclone/pCloud transfer, real validation, or archive decision
- `pushd gate` and `diffd gate` suggested next units now point to first-target final review, read-only real-gate approvals, and holding real-run implementation until the human gate is explicitly confirmed
- `pushd launchd gate` and `diffd launchd gate` are read-only launchd registration gate surfaces. They show service labels, plist paths/payloads, foreground daemon command previews, and bootstrap/rollback command examples; write/reload/register subcommands still require their service-specific gates and human review before any `launchctl` execution
- `pushd launchd status` and `diffd launchd status` are the minimal read-only launchd monitoring surface. They report the draft label/plist path, plist presence, and `launchctl print gui/<uid>/<label>` result when `launchctl` is available; they do not write plists, enable/bootstrap/bootout/disable services, start daemons, or execute transfers
- `pushd launchd review` and `diffd launchd review` are the read-only human-review bundles before public plist write or registration. They show the public `com.takafumi.pcloud-{pushd,diffd}` label, public plist path/payload, foreground command preview, and terminal review commands while keeping `state writes: none`
- `pushd launchd plist` and `diffd launchd plist` preview, or with `--execute` write, only the dev `.dev-state/launchd/*.plist` LaunchAgent review files by default. They never run `launchctl`, never start persistent daemons, never execute transfers, and refuse default `--execute` outside dev mode or outside the repo-local `.dev-state/launchd` target
- `pushd launchd plist --execute --public-write` and `diffd launchd plist --execute --public-write` are separate public plist write gates for `~/Library/LaunchAgents/com.takafumi.pcloud-{pushd,diffd}.plist`. They require the matching `PCLOUD_TOOLS_{PUSHD,DIFFD}_LAUNCHD_PLIST_GATE=operator-approved-*-launchd-plist-v1` value plus `--operator-reviewed-plist`, `--reviewer-approved-public-target`, and `--reviewer-approved-no-bootstrap`; they still never run `launchctl`, never start daemons, and write one service plist only
- `pushd launchd resident-plist` previews the operational resident LaunchAgent plist that can later queue-only append fswatch events. It embeds Homebrew/system `PATH`, `PCLOUD_TOOLS_PUSHD_FSWATCH_RESIDENT_GATE=operator-approved-fswatch-resident-v1`, resident `--execute`, approval flags, and an absolute shadow report path. Its `--execute` path requires `PCLOUD_TOOLS_PUSHD_LAUNCHD_RESIDENT_PLIST_GATE=operator-approved-pushd-launchd-resident-plist-v1` plus resident review flags, writes the plist only, and does not run `launchctl`
- `pushd launchd reload` previews the guarded operational reload command set: `launchctl bootout gui/<uid>/com.takafumi.pcloud-pushd` followed by `launchctl bootstrap gui/<uid> ~/Library/LaunchAgents/com.takafumi.pcloud-pushd.plist`. `--execute` requires an operational resident plist, saved ok shadow validation report, reload review flags, and `PCLOUD_TOOLS_PUSHD_LAUNCHD_RELOAD_GATE=operator-approved-pushd-launchd-reload-v1`
- `diffd launchd resident-plist` previews the operational bounded live API one-shot LaunchAgent plist. It embeds Homebrew/system `PATH`, `PCLOUD_TOOLS_DIFFD_API_LONG_POLL_GATE=operator-approved-api-long-poll-v1`, `diffd api-poll long-poll-run --live-api --max-iterations 1 --execute`, API approval flags, and an absolute shadow report path. Its `--execute` path requires `PCLOUD_TOOLS_DIFFD_LAUNCHD_LONG_POLL_PLIST_GATE=operator-approved-diffd-launchd-long-poll-plist-v1` plus review flags, writes the plist only, and does not run `launchctl`
- `diffd launchd reload` previews the guarded operational reload command set for `com.takafumi.pcloud-diffd`. `--execute` requires an operational bounded long-poll plist, saved ok shadow validation report, reload review flags, and `PCLOUD_TOOLS_DIFFD_LAUNCHD_RELOAD_GATE=operator-approved-diffd-launchd-reload-v1`; it does not run download transfer, normal sync/resync, listing cache operations, or autosync launchd changes
- `pushd launchd register` and `diffd launchd register` preview the guarded `launchctl enable` + `bootstrap` command set. `--execute` requires the public non-dev runtime, an existing public plist, a saved ok shadow validation report, the service-specific `PCLOUD_TOOLS_{PUSHD,DIFFD}_LAUNCHD_GATE=operator-approved-*-launchd-v1`, and all launchd approval flags; tests and shadow validation exercise this with fake `launchctl` only
- Detailed launchd approval checks stay in `pushd launchd gate` / `diffd launchd gate`; the normal `status` surface only embeds concise gate and registration summaries for xbar/operator scanning
- `pushd fswatch preview --fixture <path>` parses fixture-backed fswatch event records and previews the upload plan without starting fswatch or writing pushd state
- `pushd fswatch probe` previews the one-shot fswatch command and command availability without running fswatch or writing pushd state
- `pushd fswatch resident-gate` is a read-only checklist before any long-running fswatch watcher can be implemented or started; it checks the saved shadow validation report, `command -v fswatch`, watch scope, operator probe review, queue policy approval, and process lifecycle approval while keeping `resident gate status: closed` and `state writes: none`
- `pushd fswatch resident-run` is the guarded foreground resident loop. It still previews by default and refuses `--execute` until `--report-path`, the resident approval flags, and `PCLOUD_TOOLS_PUSHD_FSWATCH_RESIDENT_GATE=operator-approved-fswatch-resident-v1` are present. When executed, it starts `fswatch`, converts events under the watch root into pushd queue records, records `fswatch-resident-last-run.json`, and does not run upload transfer, launchd, sync/resync, or pCloud API work. Create/update events become `upload` records; delete/remove and rename/move events keep matching actions so transfer preview routes them to manual review. Duplicate path/action records already present in the queue are skipped, and resident append stops at `PCLOUD_TOOLS_PUSHD_QUEUE_LIMIT`. Use `--max-events <n>` only for bounded validation runs
- `sync autosync-run enable|disable` is the guarded launchd execution path. It previews by default and refuses `--execute` until `--report-path`, the autosync approval flags, an available `launchctl`, the required plist for enable, and `PCLOUD_TOOLS_AUTOSYNC_LAUNCHD_GATE=operator-approved-autosync-launchd-v1` are present. When executed, it runs only the planned launchctl enable/bootstrap or bootout/disable commands and records `sync/autosync-launchd-last-run.json`; it does not run normal sync/resync directly
- `sync migration-run normal|resync` is the guarded normal/resync migration validation path. It previews by default and refuses `--execute` until `--report-path`, `--sync-status-report-path`, the migration approval flags, an available `rclone`, and `PCLOUD_TOOLS_SYNC_MIGRATION_GATE=operator-approved-sync-migration-v1` are present. When executed, it runs only the explicitly approved `rclone bisync` validation command, records sync logs/status plus `sync/migration-last-run.json`, and does not alter launchd, listing caches, fswatch, or pCloud API polling
- `diffd preview` and `diffd diff preview --fixture <path>` apply the document/media allowlist and default excludes before reporting planned downloads; skipped remote records stay visible in the preview
- `diffd diff preview --fixture <path>` parses fixture-backed pCloud diff responses and previews the download plan without calling the pCloud API or writing diffd state; it handles direct `path` entries and pCloud metadata entries that provide `name` plus `parentfolderid` when the parent folder appears in the same diff batch
- `diffd api-poll preview` reports the intended one-shot pCloud API poll request shape without calling the API, configuring credentials, or writing diffd state
- `diffd api-poll long-poll-gate` is a read-only checklist before any pCloud API long-poll loop can be implemented or started; it checks the saved shadow validation report, preview request shape, diff cursor state, download scope, operator preview review, response policy approval, credential policy approval, and process lifecycle approval while keeping `long-poll gate status: closed` and `state writes: none`
- `diffd api-poll long-poll-run` is the guarded API long-poll execution path. It still previews by default and refuses `--execute` until `--report-path`, the API approval flags, and `PCLOUD_TOOLS_DIFFD_API_LONG_POLL_GATE=operator-approved-api-long-poll-v1` are present. Fixture execution still requires `--fixture`; live API execution requires `--live-api` and is limited to `--max-iterations 1` in this build. Credentials come first from `PCLOUD_TOOLS_PCLOUD_API_TOKEN`; if unset, the command reads the pCloud remote inferred from `PCLOUD_TOOLS_CORE_REMOTE` in rclone config (`RCLONE_CONFIG` or `~/.config/rclone/rclone.conf`) and uses its OAuth `token.access_token` with `access_token`. It does not read crypt remote passwords. Live requests call `<base-url>/diff` with `diffid`, `limit`, and the auth parameter; logs and JSON reports redact the token. Successful execution appends allowlisted download records to `diffd/remote-changes.json`, writes the numeric daemon diffid, updates `diffd/folder-cache.json` for pCloud folderid-to-path metadata, and records `api-long-poll-last-run.json`. Failed gated live API attempts record only failure state with retry/backoff metadata, retain the current diffid and remote-change records, and do not run automatic retries. It does not run downloads, launchd, or sync/resync
- `pushd transfer preview` and `diffd transfer preview` emit concise human summaries and detailed `--json` planned `rclone copyto` argv from the current upload/download plans without running rclone or writing service state; delete/rename/move-style records and same-path pushd/diffd conflicts are routed to manual review and excluded from planned transfer commands
- `pushd transfer validation-matrix` and `diffd transfer validation-matrix` are read-only review surfaces for expanded real-transfer validation. They list small txt, Japanese filename, space filename, nested path, overwrite, and remote-only download cases with setup -> preview -> final-review check -> cleanup command examples, but execute none of those commands, write no state, and keep real transfer behind the dedicated real-transfer gate plus human confirmation
- `pushd transfer check` and `diffd transfer check` are read-only real-transfer gate checklists and report `real execution can run: no`; human output stays concise, while `--json` retains the full AI/reviewer audit detail. They can inspect a saved shadow validation report with `--report-path`, accept `--sample-path <relative allowlisted path>` for the displayed dev-state sample setup, require the temp workspace/state guard and unsafe state dir guard checks to be present, show the first planned transfer, emit a dev-state-only setup -> preview -> check -> cleanup review command sequence when the plan is empty, and keep the real rclone/pCloud transfer gate closed. Operator/reviewer confirmations can be recorded with `--confirm-path`, `--confirm-direction`, `--consume-policy`, and `--timeout-policy`; mismatches stay warnings and still do not open the gate. `--final-review` adds a display-only dry-run command and the exact real command only when all preflight checks are ready; blocked final reviews list the missing checks and withhold transfer command strings. Ready final reviews are marked `ready-for-separate-gate`, which still means real execution is unavailable until a separate real gate is implemented
- `pushd transfer real-gate` and `diffd transfer real-gate` are read-only scaffolds for that separate real execution gate; they force the final-review checks, report the required `PCLOUD_TOOLS_REAL_TRANSFER_EXECUTION_GATE=operator-approved-real-transfer-v1` value, forbid fake-rclone gate reuse, and still do not execute rclone or write service state. For manual first-run cleanup, `real-gate` / `real-run` may select exactly one matching planned transfer with `--confirm-path` and `--confirm-direction` even when other planned records remain; the selected target is shown as `selected transfer`
- `transfer real-gate` exposes machine-readable real execution readiness (`blocked-final-review`, `blocked-approval`, or `blocked-execution-gate`) plus `real execution can run: no`
- `transfer real-gate` approval flags (`--operator-reviewed-dry-run`, `--reviewer-approved-real-command`, and `--reviewer-approved-consume-policy`) mark the separate gate as `complete-read-only`; this is only an audit state and still does not enable execution
- `transfer real-gate` also documents the future real-run consume/rollback policy in read-only form: remove matching records only after an exact successful transfer, retain records on failure/unknown outcomes, and never auto-delete/rollback local or remote data
- `transfer real-gate` reports whether operator verification is required; read-only diagnostics are normally covered by automated validation, while human checks are reserved for first real target review, real execution gate implementation, or actual pCloud/rclone transfer
- `transfer real-gate` also exposes `human gate status`: blocked final-review reports `not-yet`, pending approvals report `required-before-real-gate`, and complete read-only approvals report `required-before-actual-transfer`
- `pushd transfer real-run` and `diffd transfer real-run` now contain the guarded real upload/download execution path. They require the same final-review arguments and approval flags as `transfer real-gate`, `PCLOUD_TOOLS_REAL_TRANSFER_EXECUTION_GATE=operator-approved-real-transfer-v1`, an executable non-`fake-rclone` `PCLOUD_TOOLS_RCLONE_BIN`, and `--execute`; otherwise they refuse before rclone starts. Successful execution records `last-transfer.json` with mode `real-rclone-transfer`; when `--consume-policy remove-on-success-retain-on-failure` is approved, it removes only the exact successful queue/change record and retains failed/unknown records. When `selected transfer` is ready, `real-run` executes and consumes only that one selected command and leaves other planned records untouched
- `pushd transfer run --execute` and `diffd transfer run --execute` are limited to dev-mode fake-rclone execution and still report `real execution can run: no`: `PCLOUD_TOOLS_TRANSFER_EXECUTION_GATE=dev-fake-rclone`, `PCLOUD_TOOLS_RCLONE_BIN=<workspace>/.dev-state/.../fake-rclone`, and state dir under `workspace/.dev-state/state` are all required; fake-rclone runs use `PCLOUD_TOOLS_TRANSFER_EXEC_TIMEOUT_SECONDS`, clean up the fake process group on timeout, record `last-transfer.json`, and never consume queue/change files; real rclone and pCloud transfer remain blocked
- `pushd transfer consume preview` and `diffd transfer consume preview` read the latest dev-state `last-transfer.json` and current queue/change file to show which successful fake-rclone records would be removed; they report `real execution can run: no`, are read-only, write no state, and do not consume queue/change files
- `pushd transfer consume run --execute` and `diffd transfer consume run --execute` are dev-state guarded consume paths; they remove only queue/change records matching successful fake-rclone results, report `real execution can run: no`, and still do not open real rclone/pCloud transfer
- `pushd transfer automation-run` and `diffd transfer automation-run` are explicit public executor ticks for real-transfer automation, but they are closed by default. Execution requires `PCLOUD_TOOLS_REAL_TRANSFER_EXECUTION_GATE=operator-approved-real-transfer-v1`, `PCLOUD_TOOLS_REAL_TRANSFER_AUTOMATION_GATE=operator-approved-real-transfer-automation-v1`, `PCLOUD_TOOLS_REAL_TRANSFER_AUTOMATION_RUN_GATE=operator-approved-real-transfer-automation-run-v1`, a saved ok shadow validation report, `--execute`, `--consume-on-success`, no manual-review records, and an executable non-`fake-rclone` `PCLOUD_TOOLS_RCLONE_BIN`. Successful records are consumed only after exact successful transfer results; failures, conflicts, timed-out results, and manual-review records are retained
- diffd download transfers use staged finalization. The rclone command writes to a state-dir staging file, then pcloud-manager compares the destination fingerprint captured before transfer. If unchanged, staging replaces the destination and a completed download suppression journal record is written. If changed, the staging file becomes an adjacent conflict copy and the source remote-change record remains for manual review
- pushd upload transfers write an upload-origin journal on success. Diffd plan generation treats same-path `diff:createfile` remote changes as skipped `upload origin journal` records while the current local fingerprint still matches the uploaded file, preventing local-origin new-file upload echo from causing an immediate download/replace. Remote-side edits such as `diff:modifyfile` are not suppressed
- If the service queue is empty, public automation launchd review can use a prior successful manual `real-run` as validation evidence. The last transfer must be `mode: real-rclone-transfer`, match the service direction, contain at least one successful result, and contain no failed/timeout result. This lets the executor LaunchAgent be reviewed after manual validation drains the queue, without inventing a new sample upload or batch policy
- `pushd queue prune-excluded` is the public-safe cleanup surface for queue records that the current plan excludes, such as `.pcloudmanagerignore` matches and hard safety excludes. Preview is read-only; public `--execute` requires `--reviewer-approved-excluded-record-cleanup` and `PCLOUD_TOOLS_PUSHD_QUEUE_PRUNE_EXCLUDED_GATE=operator-approved-pushd-queue-prune-excluded-v1`
- `archive old-monolith-gate` is a read-only checklist before archiving the old zsh `pcloud-manager` monolith; it checks the current public wrapper, dotfiles wrapper target, selected cutover backup, legacy monolith backup, rollback source, and archive target approval while keeping `archive gate status: closed` and `state writes: none`
- `archive old-monolith-run` is the guarded archive execution path. It previews by default and refuses `--execute` until the archive approval flags and `PCLOUD_TOOLS_OLD_MONOLITH_ARCHIVE_GATE=operator-approved-old-monolith-archive-v1` are present. When executed, it copies the selected `pcloud-manager.current` and `shadow-validation.json` backup into `.dev-state/old-monolith-archive/<backup>/`, writes `archive-manifest.json`, retains the source backup, and does not modify public wrappers, launchd, sync state, or remote files
- `gates status` is a concise read-only aggregate of human gates; pass `--report-path .dev-state/reports/shadow-validation.json --sync-status-report-path .dev-state/reports/sync-status.json --assume-read-only-approvals` to inspect guarded `*-run` command/env gates while keeping every execution gate closed. Add `--show-command-examples` to print read-only review commands for those guarded paths; those examples intentionally omit `--execute`
- `gates status --xbar` uses a concise xbar renderer that shows gate counts, one compact line per gate, and safe status refresh actions only. It suppresses full guarded command examples, execution gate env values, and any `--execute` path from the xbar menu
- `scripts/pcloud-shadow-validation.py` runs a temp-dev-state shadow validation pass over preview, dry-run, action, wrapper, and safety-guard paths without touching live state or pCloud remotes; it covers the thin `pcloud-pushd` / `pcloud-diffd` dev wrappers in a temp workspace, the pushd/diffd launchd gates, launchd status surface, dev-only launchd plist preview/write without launchctl registration, operational pushd/diffd plist and reload gates with fake `launchctl`, the fswatch resident gate and a bounded fake-fswatch resident run using a temp fake `fswatch` discovered through `command -v`, the pCloud API long-poll gate without live API calls, fixture-backed long-poll execution, fake local HTTP live-API execution with token redaction, rclone-config OAuth token fallback using a temp rclone config, dev-only autosync plist preview/write, the autosync launchd gate using a temp fake `launchctl`, the sync migration gate using a temp fake `rclone` plus a saved sync-status report, and the old monolith archive gate/run using a temp cutover backup
- shadow validation can write a JSON report with `--report-path`; use `--summary` for concise human output while preserving full AI/reviewer detail in `--json` and saved reports. A cutover candidate must have `status: ok`, every check `status: ok`, `temporary workspace guard` / `temporary state dir guard` passing, and no evidence of live `~/.pcloud` or pCloud remote IO
- stable action ids include `sync.autosync-plist.preview`, `sync.autosync.gate`, `sync.autosync-run.preview`, `sync.migration.gate`, `sync.migration-run.preview`, `archive.old-monolith.gate`, `archive.old-monolith-run.preview`, `gates.status`, `pushd.status.refresh`, `pushd.preview`, `pushd.policy`, `pushd.run.preview`, `pushd.backfill.preview`, `pushd.gate`, `pushd.launchd.gate`, `pushd.launchd.status`, `pushd.launchd.review`, `pushd.launchd.register.preview`, `pushd.launchd.reload.preview`, `pushd.launchd.resident-plist.preview`, `pushd.launchd.executor-plist.preview`, `pushd.launchd.automation-plist.preview`, `pushd.launchd.automation-reload.preview`, `pushd.launchd.plist.preview`, `pushd.fswatch.resident-gate`, `pushd.fswatch.resident-run.preview`, `pushd.transfer.preview`, `pushd.transfer.validation-matrix`, `pushd.transfer.check`, `pushd.transfer.real-gate`, `pushd.transfer.real-run.preview`, `pushd.transfer.consume.preview`, `pushd.transfer.automation-gate`, `pushd.queue.clear.preview`, `diffd.status.refresh`, `diffd.preview`, `diffd.policy`, `diffd.run.preview`, `diffd.gate`, `diffd.launchd.gate`, `diffd.launchd.status`, `diffd.launchd.review`, `diffd.launchd.register.preview`, `diffd.launchd.reload.preview`, `diffd.launchd.resident-plist.preview`, `diffd.launchd.executor-plist.preview`, `diffd.launchd.automation-plist.preview`, `diffd.launchd.automation-reload.preview`, `diffd.launchd.plist.preview`, `diffd.api-poll.long-poll-gate`, `diffd.api-poll.long-poll-run.preview`, `diffd.transfer.preview`, `diffd.transfer.validation-matrix`, `diffd.transfer.check`, `diffd.transfer.real-gate`, `diffd.transfer.real-run.preview`, `diffd.transfer.consume.preview`, `diffd.transfer.automation-gate`, and `diffd.remote-change.clear.preview`
- `mount` / `umount` now expose preview-first reports; `pcloud-manager-dev` refuses `--execute` so development runs do not touch live mount links
- `index` now uses the repo-local `scripts/pcloud-indexer.py`, and its default DB lives under `.dev-state/state/index/`

Limited migration validation:

- On 2026-05-04, a human-approved first real pushd upload was executed for `Documents/DQ2-呪文.png` using guarded `pushd transfer real-run --execute`
- The upload invoked `/usr/local/bin/rclone copyto /Users/takafumi/p-core/dev/pcloud-tools/Documents/DQ2-呪文.png pcloud:core/Documents/DQ2-呪文.png`, returned `0`, did not time out, and recorded `.dev-state/state/pushd/last-transfer.json` with `mode: real-rclone-transfer`
- The successful upload was consumed with `pushd transfer consume run --execute` under the approved `remove-on-success-retain-on-failure` policy; `pushd transfer preview` then reported planned transfers `0`
- On 2026-05-04, a human-approved first real diffd download was executed for the same path after backing up the local file under `.dev-state/real-transfer-backups/DQ2-呪文.png.before-download`
- The download invoked `/usr/local/bin/rclone copyto pcloud:core/Documents/DQ2-呪文.png /Users/takafumi/p-core/dev/pcloud-tools/Documents/DQ2-呪文.png`, returned `0`, did not time out, and recorded `.dev-state/state/diffd/last-transfer.json` with `mode: real-rclone-transfer`
- The downloaded file and backup both had SHA-256 `c0412cf18081b35bee90f0fd30dfd6c0d0d0a0c8a10c0f362b326de2c090cccb`
- The successful download was consumed with `diffd transfer consume run --execute` under the approved policy; `diffd transfer preview` then reported planned transfers `0`
- Completed live checks now include bounded fswatch resident start, live pCloud API `/diff` one-shot, human-gated pushd/diffd launchd reloads, public normal sync migration validation, public autosync re-enable, and old monolith archive execution. Additional daemon expansion and real transfer validation remain separate gated work and still require explicit operator gates and `--execute`

Live sync operations note:

- the live allowlist is `/`, meaning p-core root scope; `.pcloudmanagerignore` excludes secrets, dotfiles/dotdirs by default, VCS/dependency/build/cache outputs, temp/partial downloads, and moved-out `LLM/`
- source/tool roots such as `apps/`, `bin/`, `dev/`, `dotfiles/`, `project/`, and `tools/` are in daemon scope unless excluded by ignore rules
- the 2026-04-27 bisync baseline recovery was for the previous document-only scope; bisync execution is now rejected while daemon mode is loaded, and any bisync run must use the shared allowlist plus the generated safety filter
- the allowlist/filter rollback backup is `/Users/takafumi/.pcloud/allowlist-backups/20260426-052756/`
- autosync was restored on 2026-04-27 and reached `SUCCESS mode=autosync` with allowlist scope; when the latest result is success, old `last error` records are labeled as `historical` in `sync status` and `status --detail`

Shadow validation gate:

```sh
mkdir -p .dev-state/reports
python3 scripts/pcloud-shadow-validation.py --summary --report-path .dev-state/reports/shadow-validation.json
python3 scripts/pcloud-shadow-validation.py --summary
python3 scripts/pcloud-shadow-validation.py --json
```

Treat the gate as failed if the command exits non-zero, the saved JSON has top-level `status: error`, any `checks[]` item is not `ok`, `unsafe state dir guard` is missing, `temporary workspace guard` / `temporary state dir guard` is missing, or `state_dir` is not exactly `workspace/.dev-state/state` under the script-created temporary `pcloud-shadow-validation-*` workspace. A failed gate means no cutover: keep the public `pcloud-manager` wrapper pointed at the old implementation, keep launchd/fswatch/pCloud API paths disabled, and fix the preview/dry-run surface first.

Cutover readiness:

- readiness package: `/Users/takafumi/p-core/dev/#仕様書/pcloud-manager/cutover-readiness-package.md`
- it records the current public wrapper checks, backup commands, validation report gate, rollback command, and stop conditions
- public `pcloud-manager` now invokes the Python implementation; launchd, fswatch, pCloud API polling, real transfer IO, and old monolith archive are still unchanged

Post-cutover soak checklist:

```sh
pcloud-manager status --json
pcloud-manager doctor --json
pcloud-manager sync status --json
pcloud-manager daemon status --json
pcloud-manager pushd status --json
pcloud-manager diffd status --json
pcloud-manager status --xbar
pcloud-manager sync status --xbar
pcloud-manager daemon status --xbar
pcloud-manager pushd status --xbar
pcloud-manager diffd status --xbar
pcloud-manager action status.refresh
```

For xbar output, every action `bash=` field should point to an executable public wrapper such as `/Users/takafumi/bin/pcloud-manager`. This soak checklist is read-only; it must not create or load launchd jobs, start fswatch, poll the pCloud API, run upload/download execution, or archive the old monolith.
