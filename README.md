# pcloud-tools

## 確認待ちは採用する版を選べるファイルだけ（0.5.1）

確認画面の採用一覧には、クラウド版またはローカル版を選べるファイルだけを表示します。フォルダは同期処理が子ファイルの確認へ展開し、情報取得エラーや移動の復旧は採用ボタンのない診断欄へ分けます。旧版から残ったフォルダの確認待ちも順次整理します。列挙に失敗した場合や同名のファイルと衝突した場合は、データとイベントを保持します。

`diffd transfer manual list --json` は採用対象を `records`、情報の再確認を `rechecks`、処理上の問題を `diagnostics` に返します。`review_count` は採用対象の件数です。DB構造・同期範囲・資格情報の変更はありません。

## 双方向同期の競合方針（0.5.0）

この方針は、以前の「同時刻の異なる内容や削除・編集の競合を人の確認待ちにする」動作を置き換える。通常の更新はファイルの更新日時が新しい方を採用し、両側を揃える。同期実行時刻を比較に使わず、コピーで元の更新日時を保持する。比較精度は従来どおり秒単位で、同じ秒なら設定した側を採用する。時計ずれや日時を保持したコピーと実際の編集順の違いは運用上許容する。

両側の変更が衝突した場合と、同時刻で内容が違う場合は、不採用版をローカルcore直下の `.conflict/` へ退避する。確認済みの削除イベントと編集が衝突した場合は、編集版を退避して通常の場所では削除を採用する。通信失敗や情報未取得は削除とみなさない。情報の取得に成功したことと、対象イベントの世代確認が前提となる。

退避が失敗しても同期・上書き・削除を続行する。失敗はstate直下の `conflict-archive-failures.jsonl` と当該実行結果に記録する。ただし退避中に元のファイルやイベントが変わった場合は、古い判断で上書きせず、そのファイルだけ次の再評価へ回す。他の対象は進める。

設定は既存config dirの `.env` に記載する。場所は `pcloud-manager info` で確認できる。

| キー | 初期値 | 意味 |
| --- | --- | --- |
| `PCLOUD_TOOLS_CONFLICT_SAME_TIME` | `local` | 同時刻の採用側。`local` または `cloud` |
| `PCLOUD_TOOLS_CONFLICT_RETENTION_DAYS` | `14` | 退避の保持日数。非負整数 |
| `PCLOUD_TOOLS_CONFLICT_MAX_BYTES` | `100000000000` | 退避内容の合計容量上限。非負整数、十進の100GB |

期間超過分を削除し、容量超過時は古い退避から削除する。新規退避前に予定サイズ分の空きを確保する。単体で容量上限を超えるファイルは退避を省略し、失敗を記録して同期を続ける。管理対象はこの機能が作成した退避で、上限は内容の容量を対象とする。索引やメタデータの容量は含めない。保持処理は同期実行時と退避作成時に行い、停止中は次回実行まで遅れる。

退避はUUIDのディレクトリごとに、元の相対パスを保った `data/` と `record.json` を保存する。`.conflict/` は通常の同期・監視・全件列挙から除外する。索引はstate直下の `conflict-archive.sqlite3` に保存する。launchdの標準ライブラリ運用を維持するため、既存状態DBと同様に標準のsqlite3とパラメータ化したSQLを使う。既存同期DBのschemaは変更しない。

退避一覧は `pcloud-manager diffd transfer manual archives --json`、Finderで開く場合は同コマンドに `--open` を付ける。xbarにも「退避した競合版を開く」を用意する。録画など書込み継続中のファイルについては新しい運用を確定していないため、今回の変更では既存の書込み安定待ち設定を維持する。


## SQLite state storage (0.4.0)

Sync baselines, pending reconciliation paths, event queues, and transfer attempts use row-oriented SQLite after an explicit offline migration. Existing generations and progress are preserved. Normal ticks load a bounded batch; unchanged local fingerprints reuse hashes, and transfer/deletion preflight checks still fetch current remote hashes. Status and xbar read compact aggregates and show the processing phase and last progress update.

Use `pcloud-manager state info --json`, `state migrate --json` (preview), and `state doctor --json`. Run `state migrate --execute --json` only after draining transfers and stopping all writers. Verified originals remain under the configured state directory; never restore old JSON after the migrated runtime has processed new work. See [storage and migration details](docs/spec/SQLite状態管理-20260928.md). The synthetic benchmark is `scripts/benchmark-state-storage.py`.


`pcloud-tools` is a Python CLI suite for controlling pCloud through [rclone](https://rclone.org/pcloud/). It provides preview-first sync operations, one-way encrypted archives, and optional local/remote change daemons without hiding the underlying rclone model.

The project is designed for deliberate personal operation: inspect configuration, preview a change, and execute it explicitly. pCloud credentials and crypt settings remain in `rclone.conf`; `pcloud-tools` does not manage them.

## Why this exists

I built this for my own pCloud setup because I did not want to install FUSE or depend on kernel extensions just to move and verify files. The core workflows use ordinary rclone operations, and the encrypted archive workflow does not require mounting the crypt remote.

This repository is public in case the code is useful to someone else, but it remains a personal-use project rather than a supported product. There is no compatibility roadmap, service guarantee, or commitment to respond to every issue or pull request.

## How it works

1. `pcloud-manager` reads a local root directory and a sync allowlist. The allowlist, `.pcloudmanagerignore`, and built-in exclusions become the filter shared by push, pull, and maintenance sync operations.
2. `pcloud-pushd` watches in-scope local changes with `fswatch` and appends them to an upload queue. A bounded executor turns eligible queue records into `rclone copyto` uploads.
3. `pcloud-diffd` polls the pCloud `/diff` API, keeps a cursor and folder cache, and appends in-scope remote changes to a download queue. A separate bounded executor performs eligible `rclone copyto` downloads.
4. Deletes, renames, conflicts, excluded paths, and unstable files are held, skipped, or sent to manual review rather than blindly mirrored. Preview, queue inspection, and execution are separate command surfaces.
5. Queue-based push/pull is the normal daemon model. `rclone bisync` remains available as a separate maintenance mode; the manager prevents both models from running at the same time.

Optional `vault` and `crypt` mount layers avoid `rclone mount`: the tool runs `rclone serve webdav` or `rclone serve nfs`, then uses the operating system's `mount_webdav` or `mount_nfs` client. This is also FUSE-free, although the availability of those native mount commands depends on the operating system.

`pcloud-archive` is a separate support workflow for direct one-way copy and verification from any configured local directory to `pcloud-crypt:`. It does not use the push/pull queues and does not require a mount.

## Commands

| Command | Purpose |
| --- | --- |
| `pcloud-manager` | Inspect configuration and status, run diagnostics, and manage sync, mount, index, daemon, and migration workflows. |
| `pcloud-archive` | Copy selected files from a local archive directory to `pcloud-crypt:` and verify them without mounting the crypt remote. |
| `pcloud-pushd` | Observe local file changes and expose the upload-side queue and executor workflow. |
| `pcloud-diffd` | Observe pCloud changes and expose the download-side queue and executor workflow. |

`pcloud-tools` is an alias of `pcloud-manager`.

Confirmed missing remote download sources are automatically retired by queue event ID. Other events and local files remain intact; no user action is needed. File IDs coalesce obsolete names into the latest queued name. Authentication, network failures, and unresolved conflicts remain pending.

## Requirements

- macOS or Linux
- `curl`, `tar`, and either `sha256sum` or `shasum`
- [rclone](https://rclone.org/install/) with the required pCloud remotes already configured

The installer bootstraps a pinned `uv` and Python runtime when needed. macOS `launchd` integration is optional and is not installed automatically.

## Install

The recommended first installation pins the release version and lets you inspect the installer before running it:

```sh
curl -LfsS https://raw.githubusercontent.com/takafuu/pcloud-tools/v0.2.3/install.sh -o pcloud-tools-install.sh
less pcloud-tools-install.sh
sh pcloud-tools-install.sh --version v0.2.3
rm pcloud-tools-install.sh
```

For a short trusted-host installation of the latest release:

```sh
curl -LfsS https://raw.githubusercontent.com/takafuu/pcloud-tools/main/install.sh | sh
```

By default, the installer creates an isolated runtime under `${XDG_DATA_HOME:-$HOME/.local/share}/pcloud-tools` and thin command wrappers under `$HOME/bin`. It downloads the GitHub Release bundle, verifies its SHA-256 checksum, and installs the wheel with `uv tool install`.

The installer does not create or modify configuration, state, `rclone.conf`, credentials, remotes, `launchd` jobs, or NAS services. Run `sh install.sh --help` for path overrides, local-wheel installation, and dry-run options.

## First checks

```sh
pcloud-manager --version
pcloud-manager info
pcloud-manager doctor
pcloud-manager status
```

`doctor` reports missing configuration, rclone, remotes, and other machine-specific requirements without inventing credentials.

## Configure pcloud-manager

The normal configuration file is:

```text
~/.config/pcloud-tools/.env
```

Start from [`.env.example`](.env.example), set paths and remote names for the machine, and then run `pcloud-manager doctor` again. The manager expects ordinary rclone remote syntax such as `pcloud:` and `pcloud-crypt:`.

Useful discovery commands:

```sh
pcloud-manager help
pcloud-manager help --detail
pcloud-manager info paths
pcloud-manager gates
```

## Concurrent transfers (v0.2.0)

Upload and download executors support one to four concurrent transfers per service. Both default to one, so installing the release keeps serial execution until configuration is changed:

```dotenv
PCLOUD_TOOLS_PUSHD_TRANSFER_CONCURRENCY=1
PCLOUD_TOOLS_DIFFD_TRANSFER_CONCURRENCY=1
```

Set either value to `2` or `4` in the existing `.env` to enable bounded parallel execution on the next tick. `--max-records` limits how many records a tick selects; concurrency limits how many selected transfers run at once. Manual `real-run` remains limited to the single confirmed file. Invalid settings are rejected before transfers or state updates.

The executor uses process and path locks, consumes only the selected queue event generation, preserves files edited during transfers, and holds incomplete attempts for explicit recovery. Status reports expose configured concurrency, observed peak concurrency, elapsed time, and success/deferred/conflict counts. Preview and inspection do not start transfers or rewrite state. See the [recovery and writer-stop procedure](docs/spec/利用ガイド.md#本番用検証レポートと切り戻し).

The original concurrency change included 261 tests and independent review of concurrency, queue generations, process cleanup, crash recovery, and writer cutover. In a local 12-file fixture with a fixed 0.18-second delay per fake-rclone transfer, three runs per setting produced these median wall times through the development CLI:

| Direction | Concurrency 1 | Concurrency 2 | Concurrency 4 |
| --- | ---: | ---: | ---: |
| Upload | 2.912 s | 1.621 s | 0.964 s |
| Download | 2.918 s | 1.619 s | 0.960 s |

These measurements demonstrate scheduling overlap; they are not a claim about pCloud network throughput. Existing gates, sync scope, archive encryption, and deletion policies remain in effect.

## Upgrade and rollback

Use versioned releases and keep the previous wheel or installer bundle. Before upgrading or downgrading, stop every process that can write the same queue or journals: watcher, poller, executors, manual transfers, and backfill. Let started transfers finish or confirm their child processes have exited. Save the current runtime, public wrappers, service definitions, configuration, and queue/journal state in a private backup outside this repository.

Install a pinned release using the inspected installer, or a saved wheel:

```sh
sh pcloud-tools-install.sh --version v0.2.3
# Or use an already verified local wheel:
sh pcloud-tools-install.sh --wheel /path/to/pcloud_tools-0.2.3-py3-none-any.whl
```

Verify `pcloud-manager --version`, `pcloud-manager info`, and `pcloud-manager doctor`, then restore the previously loaded services. Keep the installed runtime independent of the source checkout. To return to the previous package, repeat the writer-stop and backup procedure and install the pinned previous release:

```sh
sh pcloud-tools-install.sh --version v0.1.1
```

Changing concurrency back to `1` is the normal performance rollback and takes effect on the next tick without a package downgrade. Installing an older package does not undo local or remote file changes. Do not replace current queues with an older backup after new work has occurred without reconciling those changes first. Never run old and new writers against the same state simultaneously.

## Configure pcloud-archive

`pcloud-archive` is the simplest route for adding files to an encrypted pCloud archive from a Mac, NAS, or other machine. It performs a one-way `rclone copy`: new and changed local files are uploaded, while local deletion is not propagated automatically.

Create the starter configuration:

```sh
pcloud-archive help config --init-config ~/.config/pcloud-archive/config.toml
```

Edit `source_root` and `remote_root`, then inspect the result before copying anything:

```sh
pcloud-archive doctor
pcloud-archive diff
pcloud-archive promote path/to/item --dry-run
pcloud-archive promote path/to/item --execute
pcloud-archive check path/to/item --execute
```

The crypt remote does not need to be mounted. Authentication and encryption passwords remain owned by rclone.

## Safety model

- Read-only inspection and previews are the normal starting point.
- Transfer, delete, mode-switch, and service-registration paths require explicit execution flags or gates.
- `pcloud-archive` does not mirror local deletions to pCloud; remote deletion has a separate explicit command.
- Runtime state and logs are stored outside the repository.
- Installation is separate from machine configuration and service setup.

Always review the command output before opening a gate or adding `--execute`.

## Documentation

- [pcloud-manager usage guide](docs/spec/利用ガイド.md)
- [pcloud-manager technical specification](docs/spec/技術仕様.md)
- [pcloud-manager AI overview](docs/spec/AI向け概要.md)
- [pcloud-archive usage guide](docs/commands/pcloud-archive/利用ガイド.md)
- [pcloud-archive technical specification](docs/commands/pcloud-archive/技術仕様.md)
- [pcloud-archive AI overview](docs/commands/pcloud-archive/AI向け概要.md)

After installation, bundled documentation paths can also be rediscovered with `pcloud-manager info paths` and `pcloud-archive info paths`.

## License

This project is available under the [MIT License](LICENSE).

## Development

The repository is the development checkout, not the installed runtime:

```sh
uv sync --extra test
uv run pytest -q
./pcloud-manager-dev --help
uv run pcloud-archive --help
```

Use `./pcloud-manager-dev` for development CLI work. It isolates configuration, state, logs, rclone configuration, and caches under `.dev-state/`, and does not inherit the shell's pCloud API token or public action entrypoint. Its rclone configuration is `.dev-state/config/rclone.conf`; a missing development configuration does not fall back to production credentials. Test archive operations with temporary configuration, source, and remote fixtures. `uv run` alone does not isolate production configuration.

Production commands and LaunchAgents must use the installed release through the public wrappers, including legacy compatibility entrypoints. Promote accepted validation reports to hash-named regular files under `~/.pcloud/validation/` before referencing them from production jobs; production must not depend on a report in the development checkout. See the [development contract](docs/spec/開発仕様書.md) and [usage guide](docs/spec/利用ガイド.md) for validation promotion and rollback.

Release wheels and installer bundles are built and published by the GitHub Actions release workflow when a `v*` tag is pushed.

## Batch cloud review (0.2.6)

`pcloud-manager diffd transfer manual batch preview --input selections.json --json` accepts `pcloud-manual-batch.v1` with `items` containing `path` and `choice` (`pull`, `local`, `hold`). Save and inspect the report, then pass it to `batch apply --input preview.json --execute --json`. Use `--input -` for stdin. JSONL progress is opt-in on stderr with `--progress-jsonl`. Apply stops after the first failure; per-item results distinguish completed, upload-queued, failed, held, and unprocessed. Existing version checks and original-file backups apply equally to GUI and headless clients.

### Event synchronization (0.3.0)

Set `PCLOUD_TOOLS_DIFFD_DOWNLOAD_MODE=event` through the documented stopped-writer deployment procedure to select the event engine. Identical content is skipped; otherwise the newer UTC whole-second timestamp wins in either direction. Equal timestamps use the configured preferred side; losing conflict versions are archived locally. Confirmed deletion/edit conflicts archive the edited version and preserve the deletion. Only actionable version choices appear in the review list; unavailable information and recovery issues appear in diagnostics. Startup/resume reconciles the allowed scope, so an offline deletion may reappear. Live deletions require an unchanged last-sync version. No bisync is used.

See [the event implementation and operations note](docs/spec/イベント同期実装記録-20260923.md) for watcher behavior, recovery, state, explicit adoption and migration. `auto` and `manual` retain their previous behavior until explicitly changed. Archive verification uses `cryptcheck` when the configured destination backend is known to be crypt.

## 今回の再照合だけ時間を記録する（0.4.1）

`pcloud-manager trace start --until-reconciled --execute` は進行中の再照合IDに結び付けて一時計測を開始する。通常時は無効で、再照合の完了・対象の変更・7日経過・16MiB到達で記録を止める。`--execute` を省略するとpreviewのみ。同期や再照合自体の開始・停止・やり直しは行わない。手動停止は `pcloud-manager trace stop --execute`。保存済みログは削除しない。

状態と保存先は `pcloud-manager trace status --json`、集計は `pcloud-manager trace report`、診断は `pcloud-manager trace doctor --json`。`help trace`・`help --ai "同期の時間を調べる" --topic trace`・`info paths` から再発見できる。保存先は既存state配下の `diagnostics/sync-trace/`。sessionごとにJSONLを分離し、directory0700・log0600で保存する。設定ファイルやLaunchAgentへ恒久的な有効化設定は追加しない。

記録するのは時刻、工程、固定操作名、呼び出し回数、経過時間、Python CPU時間、終了した子processのCPU時間、例外型のみ。ファイル名・コマンド引数・認証情報・stdout/stderr・内容は保存しない。ローカル処理はバッチ内集計で、ファイルごとのログ書込みを避ける。rclone呼び出しは開始と終了、工程は切替、バッチは終了時に記録する。書込み障害は同期を中断させない。

集計は終了したバッチが対象。開始のみ残った操作は中断・実行中の可能性があり、JSONLで確認する。batch・cloud-inventory・rclone等の時間は入れ子なので単純合算しない。経過時間とCPU時間の差には通信・ディスク・ロック・スケジューリング等の待機が含まれ、それだけでネットワーク待ちと断定しない。過去の計測前の処理は復元できない。コードは `src/pcloud_tools/sync_trace.py` と `cli_trace.py`、計測入口は `event_sync.py` と `event_sync_remote.py`（実装repo root基準）。


## 再照合中の通常同期（0.4.2）

SQLite使用時は、再照合中にも追加・変更イベントを処理する。通常ファイルの比較・転送対象を1バッチ最大100件（呼出側のmax_recordsが小さければその値）に抑え、最大半分をイベント優先枠、残りを保存済みの照合残件へ割り当てる。イベントがなければ照合で枠を使う。max_records=1ではイベントと照合を交互に処理する。転送自体を途中で割り込ませる方式ではないため、反映は進行中バッチの完了と書込み安定待ちの後になり、大きいファイルや通信遅延では時間がかかる。

イベント枠はローカルとクラウドへ分配し、それぞれ新しい項目と古い項目を抽出する。同じイベントIDの確認保留は優先枠を占有させず、新世代が届けば再び対象にする。各抽出の走査行数も制限する。照合対象パスにイベントがあれば通常の変更・削除判断を適用し、単なる片側欠落として復元しない。照合中に新世代が届いた場合は後のバッチへ回し、保存した世代以外を消費しない。移動・directoryイベントは既存の検証・展開処理を先に使う。背景照合の終了や再起動で優先対象を消さない。

SQLiteのローカル待機キューは旧JSON用件数上限で新しいイベントを落とさず、行単位で保持する。処理・表示の読み取り量は引き続き限定し、同じパス・操作の世代置換規則を維持する。旧JSON形式のoverflow時再照合動作は変更しない。配備前に既に記録できなかった変更は、この変更だけでは復元できず、保存された再照合要求による回収対象となる。

再照合ID・残件・既存の一時計測sessionを維持する。SQLiteのschema変更はなく、通常のrelease切替で反映する。実装repo root基準で `event_sync.py` の混在バッチ、`sqlite_state.py` の優先抽出、`event_sync_watch.py` の永続化を確認する（各ファイルは `src/pcloud_tools/` 配下）。


## 古いクラウド情報の再確認（0.4.3）

確認画面の「情報を再確認」、または `pcloud-manager diffd transfer manual recheck --execute` で再確認を依頼する。`--execute` なしはプレビュー。依頼は同期処理の区切りで処理されるため、直ちに完了するとは限らない。「一覧を更新」で結果を確認する。情報未確認の項目と、採用する版を選べる項目を区別し、取得失敗を含む一括確認では反映へ進めない。

古いファイルIDのイベントは、最新クラウド情報が検証済みbaselineと一致し、ローカル内容も両方と一致する場合だけ自動解除する。ファイルの転送・削除は行わず、取得した世代のイベントのみを消費する。それ以外は現在の両側情報を付けて採用判断へ回し、明示的な採用は最新情報に結び付いたtokenで再検証する。取得失敗ではイベントと保留を保持する。

SQLiteの再照合中は、通常イベント枠の一部（100件バッチなら最大10件）で古いIDの保留を順次再確認する。走査量とcursorを制限し、全件照合や新着同期を占有しない。小さな再確認依頼ファイルを別に保存するため、実行中バッチの状態保存によって依頼が失われない。DB schema・同期範囲・既存計測sessionは変更しない。

## 独立した再確認と日時優先（0.4.4）

同期と再確認を独立した実行枠で並行処理する。既存の自動実行入口で再確認workerを別threadとして開始し、転送lockとは別の専用lockで重複起動を防ぐ。再確認は1回最大100件、同時1workerで、同期の100件枠を消費しない。既存2つの定期実行入口の片方が転送中でも、もう片方から再確認を開始できる。通信とディスクは共有する。

確認中はDBトランザクションを保持しない。結果反映時だけ短い書込みtransactionを使い、対象のreview・baseline・両側イベント列・ローカルstatを確認開始時と比較する。変化があれば結果を破棄して後で再確認する。workerはファイルの転送・削除やbaseline更新を行わない。完全一致する古い通知だけを整理し、同期が必要な項目は新しいイベント世代として通常同期へ戻す。通常同期のreview保存も世代を比較し、workerの結果を古いcacheで上書きしない。

両側にファイルがあり更新日時で判断できれば、新しい方を自動採用する。クラウドIDの変化だけでは保留しない。同じ内容なら転送不要。同じ秒の異なる内容、日時不明、削除と変更の競合、情報取得失敗は別途扱う。

「情報を再確認」は独立workerへ再確認を依頼する。「一覧を更新」で結果を表示する。CLIは `pcloud-manager diffd transfer manual recheck --execute`。単発実行は `pcloud-manager diffd transfer manual recheck-run --execute --max-records 100` で、`--execute` なしはpreview。xbarでは同期工程と別に、再確認の稼働状態・今回の件数・残件・更新時刻を表示する。失敗時はイベントを保持して次の定期実行で再試行する。

両workerとも既存writer leaseを使う。SQLite schema変更やLaunchAgentの追加はない。実装はrepo root基準の `src/pcloud_tools/review_worker.py`、`sqlite_state.py`、`event_sync.py`。ローカルstatが検証済みbaselineと同じなら保存済みハッシュを再利用する。


### 0.4.5

Review counts are recomputed from committed review rows inside the same short SQLite transaction. A sync batch cannot restore an older count after the independent review worker completes. No database schema change.
