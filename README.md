# pcloud-tools

`pcloud-tools` is the new implementation root for the `pcloud-manager` migration.

Current focus:

- establish a Python-native command surface
- keep a development entrypoint isolated from real machine state
- preserve space for compatibility with the existing `pcloud-manager` workflow
- move machine configuration into `.env` plus a central Python config module

Development entrypoint:

```sh
./pcloud-manager-dev status
./pcloud-manager-dev status --detail --json
./pcloud-manager-dev status --xbar
./pcloud-manager-dev doctor
./pcloud-manager-dev doctor --repair
./pcloud-manager-dev sync status --json
./pcloud-manager-dev sync status --xbar
./pcloud-manager-dev action sync.status.refresh
./pcloud-manager-dev sync
./pcloud-manager-dev sync background --no-notify
./pcloud-manager-dev sync clear-stale-lock
./pcloud-manager-dev sync enable-autosync
./pcloud-manager-dev sync disable-autosync
./pcloud-manager-dev sync autosync-gate
./pcloud-manager-dev daemon status --json
./pcloud-manager-dev daemon set-diffid 12345
./pcloud-manager-dev daemon auto-download on
./pcloud-manager-dev daemon pending-download add Documents/example.pdf --diffid 12345
./pcloud-manager-dev daemon notification record "remote changes detected"
./pcloud-manager-dev pushd status --json
./pcloud-manager-dev pushd preview
./pcloud-manager-dev pushd run
./pcloud-manager-dev pushd gate
./pcloud-manager-dev pushd fswatch preview --fixture tests/fixtures/pushd-fswatch-events.txt
./pcloud-manager-dev pushd fswatch probe
./pcloud-manager-dev pushd fswatch resident-gate
./pcloud-manager-dev pushd transfer preview
./pcloud-manager-dev pushd transfer check
./pcloud-manager-dev pushd transfer check --confirm-path Documents/example.pdf --confirm-direction upload --consume-policy remove-on-success-retain-on-failure --timeout-policy reuse-fake-rclone-cleanup --final-review
./pcloud-manager-dev pushd transfer real-gate --confirm-path Documents/example.pdf --confirm-direction upload --consume-policy remove-on-success-retain-on-failure --timeout-policy reuse-fake-rclone-cleanup --operator-reviewed-dry-run --reviewer-approved-real-command --reviewer-approved-consume-policy
./pcloud-manager-dev pushd transfer real-run --execute
./pcloud-manager-dev pushd transfer consume preview
./pcloud-manager-dev pushd queue add Documents/example.pdf
./pcloud-manager-dev pushd queue remove Documents/example.pdf
./pcloud-manager-dev pushd queue clear
./pcloud-manager-dev diffd status --json
./pcloud-manager-dev diffd preview
./pcloud-manager-dev diffd run
./pcloud-manager-dev diffd gate
./pcloud-manager-dev diffd diff preview --fixture tests/fixtures/pcloud-diff.json
./pcloud-manager-dev diffd api-poll preview
./pcloud-manager-dev diffd api-poll long-poll-gate
./pcloud-manager-dev diffd transfer preview
./pcloud-manager-dev diffd transfer check
./pcloud-manager-dev diffd transfer check --confirm-path Documents/example.pdf --confirm-direction download --consume-policy remove-on-success-retain-on-failure --timeout-policy reuse-fake-rclone-cleanup --final-review
./pcloud-manager-dev diffd transfer real-gate --confirm-path Documents/example.pdf --confirm-direction download --consume-policy remove-on-success-retain-on-failure --timeout-policy reuse-fake-rclone-cleanup --operator-reviewed-dry-run --reviewer-approved-real-command --reviewer-approved-consume-policy
./pcloud-manager-dev diffd transfer real-run --execute
./pcloud-manager-dev diffd transfer consume preview
./pcloud-manager-dev diffd remote-change add Documents/example.pdf
./pcloud-manager-dev diffd remote-change remove Documents/example.pdf
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
- the public wrapper reads live config from `/Users/takafumi/.config/pcloud-tools/.env`; `/Users/takafumi/.config` is a symlink into `/Users/takafumi/p-core/dotfiles/.config`
- the live public `.env` is machine-local and must stay ignored by the tools repo; `.env.example` captures the shareable non-secret key set for the migrated tool
- `doctor --repair` creates a starter `.env` when one does not exist
- `status`, `doctor`, and `sync status` already support a shared JSON report schema
- JSON reports include a versioned `schema_version` and xbar-facing `actions` with command argv, terminal, and refresh metadata
- `status --xbar` and `sync status --xbar` render the same report contract as an xbar menu
- `action <id>` dispatches stable action ids such as `status.refresh`, `sync.status.refresh`, and `sync.background.preview`
- `status` now summarizes `core`, `vault`, and `crypt` directly, while `status --detail` keeps the migration diagnostics
- `doctor` now reports a top-level summary and suspected cause before the detailed diagnostics
- `status`, `doctor`, and `sync status` now include autosync launchd diagnostics
- `status`, `doctor`, and `sync status` now include `sync lock status` (`missing` / `active` / `stale` / `invalid`)
- `status`, `doctor`, `sync status`, `sync scope`, and `sync check-allowlist` warn with `PCLOUD_TOOLS_SCOPE_POLICY` when the allowlist includes source/tool roots such as `apps/`, `bin/`, `dev/`, `dotfiles/`, `project/`, or `tools/`; this is a read-only diagnostic and does not rewrite the live allowlist
- `sync background` now previews the detached launcher command and supports `--resync`, `--track-renames`, `--notify`, and `--no-notify`
- plain `sync` now uses the same preview-first bisync plan as the resync variants, and `pcloud-manager-dev sync --execute` is refused in dev mode
- foreground `sync` / `resync` / `full-resync` / `track-renames` now reject active, stale, or invalid sync locks before building a launchable run
- sync completion notifications now follow the legacy `notify local ...` path, with `osascript` fallback when needed
- sync preview now surfaces bisync listing recovery candidates, and execute recovers `path1.lst-err` / `path2.lst-err` when the primary listing files are missing
- `sync clear-stale-lock` uses the same preview-first report style and can remove a stale local sync lock from the dev state
- `sync enable-autosync` / `sync disable-autosync` use preview-first reports; `pcloud-manager-dev` keeps them non-destructive
- `sync autosync-gate` is a read-only checklist before changing launchd autosync registration; it checks the saved shadow validation report, `command -v launchctl`, autosync plist presence, enable/disable preview commands, operator preview review, plist approval, launchctl policy approval, and rollback policy approval while keeping `launchd gate status: closed` and `state writes: none`
- `daemon status` exposes diffid persistence, pending-download state, last notification state, and auto-download on/off visibility from `state_dir/daemon/`
- `daemon set-diffid`, `daemon auto-download`, `daemon pending-download`, and `daemon notification` are preview-first state commands; `--execute` only writes local daemon state files
- `pushd status` / `pushd preview` and `diffd status` / `diffd preview` expose the non-destructive scaffold for future `pcloud-pushd` / `pcloud-diffd` work; they read state under `.dev-state/state/{pushd,diffd}/` in dev mode
- `pushd status` / `diffd status` summarize any existing dev-state `last-transfer.json`, including success, failed, and timeout counts, without mutating queue/change files
- `pushd preview` classifies `.dev-state/state/pushd/queue.json` into planned uploads, allowlist/exclude skips, and invalid queue records; `diffd preview` combines `.dev-state/state/diffd/remote-changes.json` with daemon pending downloads into a download plan summary
- `pushd queue add|remove|clear` and `diffd remote-change add|remove|clear` are preview-first; `--execute` only writes the corresponding JSON files when the state dir is under `workspace/.dev-state/state`
- `pushd run` and `diffd run` are one-shot dry-run surfaces; `--execute` records only `last-plan.json`, `last-event.json`, and `cursor` under the dev state dir
- `pushd gate` and `diffd gate` are read-only real-operation gates; they keep fswatch resident daemons, pCloud API long-poll, launchd registration, and real upload/download execution explicitly blocked until a separate operator/reviewer gate is opened
- `pushd gate` and `diffd gate` mark read-only gate diagnostics as not requiring routine operator verification; they also expose `human gate status: required-before-real-work`, because the remaining work is real rclone/pCloud transfer, real validation, or archive decisions
- `pushd gate` and `diffd gate` suggested next units now point to first-target final review, read-only real-gate approvals, and holding real-run implementation until the human gate is explicitly confirmed
- `pushd fswatch preview --fixture <path>` parses fixture-backed fswatch event records and previews the upload plan without starting fswatch or writing pushd state
- `pushd fswatch probe` previews the one-shot fswatch command and command availability without running fswatch or writing pushd state
- `pushd fswatch resident-gate` is a read-only checklist before any long-running fswatch watcher can be implemented or started; it checks the saved shadow validation report, `command -v fswatch`, watch scope, operator probe review, queue policy approval, and process lifecycle approval while keeping `resident gate status: closed` and `state writes: none`
- `diffd preview` and `diffd diff preview --fixture <path>` apply the document/media allowlist and default excludes before reporting planned downloads; skipped remote records stay visible in the preview
- `diffd diff preview --fixture <path>` parses fixture-backed pCloud diff responses and previews the download plan without calling the pCloud API or writing diffd state
- `diffd api-poll preview` reports the intended one-shot pCloud API poll request shape without calling the API, configuring credentials, or writing diffd state
- `diffd api-poll long-poll-gate` is a read-only checklist before any pCloud API long-poll loop can be implemented or started; it checks the saved shadow validation report, preview request shape, diff cursor state, download scope, operator preview review, response policy approval, credential policy approval, and process lifecycle approval while keeping `long-poll gate status: closed` and `state writes: none`
- `pushd transfer preview` and `diffd transfer preview` emit concise human summaries and detailed `--json` planned `rclone copyto` argv from the current upload/download plans without running rclone or writing service state; delete/rename/move-style records and same-path pushd/diffd conflicts are routed to manual review and excluded from planned transfer commands
- `pushd transfer check` and `diffd transfer check` are read-only real-transfer gate checklists and report `real execution can run: no`; human output stays concise, while `--json` retains the full AI/reviewer audit detail. They can inspect a saved shadow validation report with `--report-path`, accept `--sample-path <relative allowlisted path>` for the displayed dev-state sample setup, require the temp workspace/state guard and unsafe state dir guard checks to be present, show the first planned transfer, emit a dev-state-only setup -> preview -> check -> cleanup review command sequence when the plan is empty, and keep the real rclone/pCloud transfer gate closed. Operator/reviewer confirmations can be recorded with `--confirm-path`, `--confirm-direction`, `--consume-policy`, and `--timeout-policy`; mismatches stay warnings and still do not open the gate. `--final-review` adds a display-only dry-run command and the exact real command only when all preflight checks are ready; blocked final reviews list the missing checks and withhold transfer command strings. Ready final reviews are marked `ready-for-separate-gate`, which still means real execution is unavailable until a separate real gate is implemented
- `pushd transfer real-gate` and `diffd transfer real-gate` are read-only scaffolds for that separate real execution gate; they force the final-review checks, report the required `PCLOUD_TOOLS_REAL_TRANSFER_EXECUTION_GATE=operator-approved-real-transfer-v1` value, forbid fake-rclone gate reuse, and still do not execute rclone or write service state
- `transfer real-gate` exposes machine-readable real execution readiness (`blocked-final-review`, `blocked-approval`, or `blocked-execution-gate`) plus `real execution can run: no`
- `transfer real-gate` approval flags (`--operator-reviewed-dry-run`, `--reviewer-approved-real-command`, and `--reviewer-approved-consume-policy`) mark the separate gate as `complete-read-only`; this is only an audit state and still does not enable execution
- `transfer real-gate` also documents the future real-run consume/rollback policy in read-only form: remove matching records only after an exact successful transfer, retain records on failure/unknown outcomes, and never auto-delete/rollback local or remote data
- `transfer real-gate` reports whether operator verification is required; read-only diagnostics are normally covered by automated validation, while human checks are reserved for first real target review, real execution gate implementation, or actual pCloud/rclone transfer
- `transfer real-gate` also exposes `human gate status`: blocked final-review reports `not-yet`, pending approvals report `required-before-real-gate`, and complete read-only approvals report `required-before-actual-transfer`
- `pushd transfer real-run` and `diffd transfer real-run` now contain the guarded real upload/download execution path. They require the same final-review arguments and approval flags as `transfer real-gate`, `PCLOUD_TOOLS_REAL_TRANSFER_EXECUTION_GATE=operator-approved-real-transfer-v1`, an executable non-`fake-rclone` `PCLOUD_TOOLS_RCLONE_BIN`, and `--execute`; otherwise they refuse before rclone starts. Successful execution records `last-transfer.json` with mode `real-rclone-transfer` but does not consume queue/change records automatically
- `pushd transfer run --execute` and `diffd transfer run --execute` are limited to dev-mode fake-rclone execution and still report `real execution can run: no`: `PCLOUD_TOOLS_TRANSFER_EXECUTION_GATE=dev-fake-rclone`, `PCLOUD_TOOLS_RCLONE_BIN=<workspace>/.dev-state/.../fake-rclone`, and state dir under `workspace/.dev-state/state` are all required; fake-rclone runs use `PCLOUD_TOOLS_TRANSFER_EXEC_TIMEOUT_SECONDS`, clean up the fake process group on timeout, record `last-transfer.json`, and never consume queue/change files; real rclone and pCloud transfer remain blocked
- `pushd transfer consume preview` and `diffd transfer consume preview` read the latest dev-state `last-transfer.json` and current queue/change file to show which successful fake-rclone records would be removed; they report `real execution can run: no`, are read-only, write no state, and do not consume queue/change files
- `pushd transfer consume run --execute` and `diffd transfer consume run --execute` are dev-state guarded consume paths; they remove only queue/change records matching successful fake-rclone results, report `real execution can run: no`, and still do not open real rclone/pCloud transfer
- `scripts/pcloud-shadow-validation.py` runs a temp-dev-state shadow validation pass over preview, dry-run, action, and safety-guard paths without touching live state or pCloud remotes; it covers the fswatch resident gate using a temp fake `fswatch` discovered through `command -v`, the pCloud API long-poll gate without making API calls, and the autosync launchd gate using a temp fake `launchctl`
- shadow validation can write a JSON report with `--report-path`; use `--summary` for concise human output while preserving full AI/reviewer detail in `--json` and saved reports. A cutover candidate must have `status: ok`, every check `status: ok`, `temporary workspace guard` / `temporary state dir guard` passing, and no evidence of live `~/.pcloud` or pCloud remote IO
- stable action ids include `sync.autosync.gate`, `pushd.status.refresh`, `pushd.preview`, `pushd.run.preview`, `pushd.gate`, `pushd.fswatch.resident-gate`, `pushd.transfer.preview`, `pushd.transfer.check`, `pushd.transfer.real-gate`, `pushd.transfer.real-run.preview`, `pushd.transfer.consume.preview`, `pushd.queue.clear.preview`, `diffd.status.refresh`, `diffd.preview`, `diffd.run.preview`, `diffd.gate`, `diffd.api-poll.long-poll-gate`, `diffd.transfer.preview`, `diffd.transfer.check`, `diffd.transfer.real-gate`, `diffd.transfer.real-run.preview`, `diffd.transfer.consume.preview`, and `diffd.remote-change.clear.preview`
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
- Remaining gates are fswatch resident implementation/start, pCloud API long-poll implementation/start, launchd/autosync execution, normal sync/resync migration validation, and old monolith archive; fswatch resident, pCloud API long-poll, and autosync launchd changes now have read-only gate checklists but are still not runnable

Live sync operations note:

- the live allowlist is document-only: `Documents/`, `scansnap/`, `screenshots/`, and `sound/`
- source/tool roots such as `apps/`, `bin/`, `dev/`, `dotfiles/`, `project/`, and `tools/` are intentionally outside pCloud sync scope
- the 2026-04-27 bisync baseline recovery rebuilt the rclone baseline for the document-only scope after a broad-scope listing caused a safety abort; rollback listing backup is `/Users/takafumi/.pcloud/bisync-listing-backups/20260427-220757/`
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
