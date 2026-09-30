# pcloud-manager AI向け概要

## フォルダ整理の処理枠を維持する（0.5.2）

新しいフォルダ通知を展開した後も、古いフォルダ保留の処理枠を維持する。0.5.1では通知が重なると選んだ保留が処理枠から外れていた。再現テストを追加し、開発環境と配布wheelの関連70件が通過した。配備は従来の停止・backup・切替・復帰手順を使う。

## 確認待ちの分類修正（0.5.1）

開発先は実装repoに登録された `codex/manual-batch-ui` worktree。repo root基準の `src/pcloud_tools/review_classification.py`、`event_sync.py`、`cli_manual_pull.py`、`event_sync_status.py`、`sqlite_state.py`、`review_worker.py` と `tests/test_review_classification.py` を読む。確認待ちは採用判断のみ、情報再確認と処理エラーは別配列・別件数に分ける。旧フォルダ保留は子ファイルを予約してから解除し、新しいイベント世代は消費しない。UI側はxbar/pcloud-status root基準の `review_assets/` が担当し、旧CLIのフォルダエラーも採用一覧から分離する。

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


## 独立した再確認と日時優先（0.4.4）

同期と再確認を独立した実行枠で並行処理する。既存の自動実行入口で再確認workerを別threadとして開始し、転送lockとは別の専用lockで重複起動を防ぐ。再確認は1回最大100件、同時1workerで、同期の100件枠を消費しない。既存2つの定期実行入口の片方が転送中でも、もう片方から再確認を開始できる。通信とディスクは共有する。

確認中はDBトランザクションを保持しない。結果反映時だけ短い書込みtransactionを使い、対象のreview・baseline・両側イベント列・ローカルstatを確認開始時と比較する。変化があれば結果を破棄して後で再確認する。workerはファイルの転送・削除やbaseline更新を行わない。完全一致する古い通知だけを整理し、同期が必要な項目は新しいイベント世代として通常同期へ戻す。通常同期のreview保存も世代を比較し、workerの結果を古いcacheで上書きしない。

両側にファイルがあり更新日時で判断できれば、新しい方を自動採用する。クラウドIDの変化だけでは保留しない。同じ内容なら転送不要。同じ秒の異なる内容、日時不明、削除と変更の競合、情報取得失敗は別途扱う。

「情報を再確認」は独立workerへ再確認を依頼する。「一覧を更新」で結果を表示する。CLIは `pcloud-manager diffd transfer manual recheck --execute`。単発実行は `pcloud-manager diffd transfer manual recheck-run --execute --max-records 100` で、`--execute` なしはpreview。xbarでは同期工程と別に、再確認の稼働状態・今回の件数・残件・更新時刻を表示する。失敗時はイベントを保持して次の定期実行で再試行する。

両workerとも既存writer leaseを使う。SQLite schema変更やLaunchAgentの追加はない。実装はrepo root基準の `src/pcloud_tools/review_worker.py`、`sqlite_state.py`、`event_sync.py`。ローカルstatが検証済みbaselineと同じなら保存済みハッシュを再利用する。


## 再照合中の通常同期（0.4.2）

SQLite使用時は、再照合中にも追加・変更イベントを処理する。通常ファイルの比較・転送対象を1バッチ最大100件（呼出側のmax_recordsが小さければその値）に抑え、最大半分をイベント優先枠、残りを保存済みの照合残件へ割り当てる。イベントがなければ照合で枠を使う。max_records=1ではイベントと照合を交互に処理する。directoryや移動の検証・展開は別途必要になる。転送自体を途中で割り込ませる方式ではないため、反映は進行中バッチの完了と書込み安定待ちの後になり、大きいファイルや通信遅延では時間がかかる。

イベント枠はローカルとクラウドへ分配し、それぞれ新しい項目と古い項目を抽出する。同じイベントIDの確認保留は優先枠を占有させず、新世代が届けば再び対象にする。各抽出の走査行数も制限する。照合対象パスにイベントがあれば通常の変更・削除判断を適用し、単なる片側欠落として復元しない。照合中に新世代が届いた場合は後のバッチへ回し、保存した世代以外を消費しない。移動・directoryイベントは既存の検証・展開処理を先に使う。背景照合の終了や再起動で優先対象を消さない。

SQLiteのローカル待機キューは旧JSON用件数上限で新しいイベントを落とさず、行単位で保持する。処理・表示の読み取り量は引き続き限定し、同じパス・操作の世代置換規則を維持する。旧JSON形式のoverflow時再照合動作は変更しない。配備前に既に記録できなかった変更は、この変更だけでは復元できず、保存された再照合要求による回収対象となる。

再照合ID・残件・既存の一時計測sessionを維持する。SQLiteのschema変更はなく、通常のrelease切替で反映する。実装repo root基準で `event_sync.py` の混在バッチ、`sqlite_state.py` の優先抽出、`event_sync_watch.py` の永続化を確認する（各ファイルは `src/pcloud_tools/` 配下）。


## 今回の再照合だけ時間を記録する（0.4.1）

`pcloud-manager trace start --until-reconciled --execute` は進行中の再照合IDに結び付けて一時計測を開始する。通常時は無効で、再照合の完了・対象の変更・7日経過・16MiB到達で記録を止める。`--execute` を省略するとpreviewのみ。同期や再照合自体の開始・停止・やり直しは行わない。手動停止は `pcloud-manager trace stop --execute`。保存済みログは削除しない。

状態と保存先は `pcloud-manager trace status --json`、集計は `pcloud-manager trace report`、診断は `pcloud-manager trace doctor --json`。`help trace`・`help --ai "同期の時間を調べる" --topic trace`・`info paths` から再発見できる。保存先は既存state配下の `diagnostics/sync-trace/`。sessionごとにJSONLを分離し、directory0700・log0600で保存する。設定ファイルやLaunchAgentへ恒久的な有効化設定は追加しない。

記録するのは時刻、工程、固定操作名、呼び出し回数、経過時間、Python CPU時間、終了した子processのCPU時間、例外型のみ。ファイル名・コマンド引数・認証情報・stdout/stderr・内容は保存しない。ローカル処理はバッチ内集計で、ファイルごとのログ書込みを避ける。rclone呼び出しは開始と終了、工程は切替、バッチは終了時に記録する。書込み障害は同期を中断させない。

集計は終了したバッチが対象。開始のみ残った操作は中断・実行中の可能性があり、JSONLで確認する。batch・cloud-inventory・rclone等の時間は入れ子なので単純合算しない。経過時間とCPU時間の差には通信・ディスク・ロック・スケジューリング等の待機が含まれ、それだけでネットワーク待ちと断定しない。過去の計測前の処理は復元できない。コードは `src/pcloud_tools/sync_trace.py` と `cli_trace.py`、計測入口は `event_sync.py` と `event_sync_remote.py`（実装repo root基準）。


## 現行保存方式（0.4.0）

SQLite移行の入口は`sqlite_state.py`、`state_migration.py`、`cli_state.py`。`event_sync.py`が必要な記録を読み、`event_sync_status.py`は小さな集計を読む。旧JSONを直接編集しても移行後のDBには反映されない。旧JSONへのfallbackや稼働後の古いbackup復元は行わない。[SQLite状態管理](SQLite状態管理-20260928.md)を先に読む。


## 現行イベント同期の表示（0.4.0）

イベント同期の現在の入口は `status --detail --json`。0.3.2ではCLIが `pcloud-event-status.v1` を集約し、xbarはその保存キャッシュを表示する。初回照合の保存残件・確認待ち件数・保存日時と、取得時点のexecutor稼働を区別する。未取得は未取得、古い保存状態は更新待ちとし、全件一致や同期完了を推測しない。確認画面の件数も同じScopeと保存stateから計算し、取得した版の保存時刻を表示する。画面更新でクラウドの走査・ハッシュ取得は行わない。

メニューの先頭には今していること・観測できた照合残件の減少・情報取得時刻・必要な対応を表示する。対応対象がある場合だけ「対応が必要なファイル → 内容を確認する…」を表示し、正常な最新情報で対象が0件なら対応不要と案内する。警告時は診断へ誘導し、古い情報では現在の稼働・対応不要を断定しない。その下に「状態の詳細」「診断」「Mounts」を置く。要操作の重複入口、Mode、独立Push/Pull、調査用Preview、Shortcuts、一括mount/unmountは表示しない。vault/cryptの個別操作とxbar本体の末尾メニューは維持する。CLIの既存コマンドは廃止しない。旧bisyncの起動やサービス停止など実際の異常は状態に残す。

以下には従来モード・過去版の記録も含む。現行イベント同期の実装詳細は[イベント同期実装記録](イベント同期実装記録-20260923.md)を優先する。配備の完了は導入先のrelease receiptで確認する。

Last updated: 2026-09-08

追加worktreeは `/Users/takafumi/p-core/dev/pcloud-tools-dev/<worktree>/` に集約する。これは `~/dev/pcloud-tools-dev/<worktree>/` と同じ場所（`~/dev` は `/Users/takafumi/p-core/dev` へのsymlink）。`~/dev` 直下へworktreeを増やさない。主checkoutは `/Users/takafumi/p-core/dev/pcloud-tools/` に維持する。

## 開発・本番の境界を先に確認する

本番CLIとLaunchAgentはinstall済みwheelを使う。古い `.zsh/functions/pcloud-manager` もpublic wrapperへの委譲だけにする。本番の検証レポートは `~/.pcloud/validation/` のハッシュ付き実ファイルに保存し、開発 `.dev-state` を本番から参照しない。`pcloud-manager-dev` はrclone認証/cacheも開発用へ分離し、親shellのAPI token/public入口を引き継がない。開発用設定へ本番credentialをコピーしない。

変更前に実機のwrapper、loaded LaunchAgentのprogram/argumentsを確認する。資料上の分離だけで実機の分離を断定しない。公開版の更新はrelease installで行い、source checkoutをpublic `PYTHONPATH` に足さない。詳細は [開発仕様書](開発仕様書.md) と技術仕様の「開発・本番の実行境界」を参照。

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

releaseはsemantic version tagで管理する。GitHub-only distributionで、raw.githubusercontent.comの`install.sh`がGitHub Release bundleを取得し、checksum確認後に`uv tool install`する。installerはconfig/state/rclone credentials/launchd/NAS serviceを変更しない。別machineへの実導入はrelease後の別taskとして、READMEとpublic diagnosticsだけで行う。

`pcloud-manager help --ai "request" --topic <topic>` は別 AI/helper 向けの read-only JSON context generator。topic は `overview`, `safety`, `mode`, `pushd`, `diffd`, `launchd`, `transfer`, `sync`, `config`。LLM 呼び出し、生成 command 実行、runtime state mutation、private/large content 読み込みは禁止。

## 現在の分割方針

`src/pcloud_tools/cli.py` は薄い dispatcher に寄せている。各 command の実装は次のモジュールへ分離する。

- `daemon`: `/Users/takafumi/p-core/dev/pcloud-tools/src/pcloud_tools/cli_daemon.py`
- `mode`: `/Users/takafumi/p-core/dev/pcloud-tools/src/pcloud_tools/cli_mode.py`
- `pushd` / `diffd` daemon surfaces: `/Users/takafumi/p-core/dev/pcloud-tools/src/pcloud_tools/cli_service_daemon/__init__.py`
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
- `pushd transfer automation-run` / `diffd transfer automation-run` は実装済みの gated automatic real-transfer executor tick。real-transfer gate、automation gate、automation-run gate、saved shadow validation report、non-`fake-rclone` rclone、`--execute`、`--consume-on-success` が揃うまで拒否する。manual-review record は安全に除外・保持し、他のeligible transferを止めず、automation errorや反復chat通知にも転換しない。`pushd automation-run --execute` は gate が揃った tick 冒頭で missing-local upload cleanup を行ってから plan を組む。CLI実行の既定は one transfer record per tick (`--max-records 1`) で、成功 record だけ consume し、失敗/不明/deferred record は保持する。public automation launchd review は confirmed selected target 1 件を current bounded automation tick として review でき、他 planned records は deferred として残す。
- pushd/diffd executorの同時転送数は `PCLOUD_TOOLS_PUSHD_TRANSFER_CONCURRENCY` / `PCLOUD_TOOLS_DIFFD_TRANSFER_CONCURRENCY`（整数1〜4、既定1）で設定し、`--max-records`とは独立して次tickから反映する。status/info/doctorには解決値とsource layerを表示する。
- executorはqueue/journalの短時間process lock、方向をまたぐpath lock、service tick lockを使う。各last-transferにはperformance計測とrecordの開始/終了/実行秒数を保存し、event_id一致時だけ選択世代をconsumeする。旧queueは互換読取し、previewは書き込まない。
- 常駐watcher、API poller、executorなどの長寿命writerはservice lifetime sessionを保持し、切替・切り戻しは同じqueue/journalへ書く全writerを停止してchildの終了を確認してから `writer_cutover_session` で行う。通常の復旧は設定をconcurrency=1へ戻して次tickから反映し、packageの切替・rollbackとは分けて確認する。
- `pushd transfer recovery preview` / `diffd transfer recovery preview` は未確定attemptをread-only表示し、`recovery run --execute --attempt-id <id> --child-exit-confirmed --writers-stopped --latest-event-ids-rechecked --local-fingerprints-rechecked` は全writer停止、所有child終了、最新event_id、local fingerprintの再確認を要求する。復旧は候補再評価の解放だけで、成功receiptやqueue consumeを作らない。
- 未確定attemptが残る間は全executorの再起動・consumeを拒否し、明示的な復旧後も元attemptのevent_idと転送前fingerprintを再評価する。復旧後にlocal fingerprintが変わっていればdownloadを上書きせずconflict/manual-reviewとして保持する。
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
- normal sync/resync、listing cache 操作、autosync launchd changes は別 gate のまま。pushd/diffd transfer は bounded public executor tick と専用gateで管理し、現在の開閉・loaded状態・健全性は status/doctor/launchd status の実行結果で確認する。

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
- automation-run is the public executor tick for automatic real transfer and is controlled by dedicated gates. It requires the real transfer gate, automation gate, automation-run gate, saved ok shadow validation report, `--execute`, `--consume-on-success`, and non-`fake-rclone` rclone before it transfers or consumes any record. Manual-review records are excluded and retained without blocking eligible records or producing recurring automation errors. `pushd automation-run --execute` performs the same missing-local startup cleanup once those gates are satisfied, before transfer planning. A direct CLI invocation defaults to `--max-records 1`; the current gate/loaded health is determined by runtime status.
- future public automation launchd previews: `pushd launchd automation-plist`, `pushd launchd automation-reload`, `diffd launchd automation-plist`, `diffd launchd automation-reload`
- automation-plist previews the public executor plist payload and can write it only when terminal review flags, saved shadow report, automation gate review, public wrapper check, service-specific plist gate env, and public non-dev runtime all pass. The generated public payload defaults to `automation-run --execute --consume-on-success --max-records 10`; the operational check accepts any positive integer. This generated payload default is separate from the direct CLI default of `--max-records 1`. automation-reload previews service-specific `bootout` -> `bootstrap` and can execute it only when an operational automation plist, launchctl, terminal review/rollback flags, and service-specific reload gate env all pass
- extra action id: `pushd.policy`, `pushd.run.preview`, `pushd.backfill.preview`, `pushd.gate`, `pushd.launchd.gate`, `pushd.launchd.status`, `pushd.launchd.review`, `pushd.launchd.register.preview`, `pushd.launchd.reload.preview`, `pushd.launchd.resident-plist.preview`, `pushd.launchd.plist.preview`, `pushd.transfer.preview`, `pushd.transfer.validation-matrix`, `pushd.transfer.check`, `pushd.queue.clear.preview`, `diffd.policy`, `diffd.run.preview`, `diffd.gate`, `diffd.launchd.gate`, `diffd.launchd.status`, `diffd.launchd.review`, `diffd.launchd.register.preview`, `diffd.launchd.reload.preview`, `diffd.launchd.resident-plist.preview`, `diffd.launchd.plist.preview`, `diffd.transfer.preview`, `diffd.transfer.validation-matrix`, `diffd.transfer.check`, `diffd.remote-change.clear.preview`
- shadow validation prep script: `/Users/takafumi/p-core/dev/pcloud-tools/scripts/pcloud-shadow-validation.py`
- validation script uses a temp workspace and only exercises dev preview / dry-run / gate / fixture parsers / action / safety-guard / historical last-error display paths
- validation reports can be saved with `--report-path`; guarded changes remain blocked unless the saved report has `status: ok` and every check is `ok`

別の gate と runtime 状態で確認するもの:

- diffd 側の実 rclone/pCloud download 機能と専用gateは実装済みで、設定上の開閉・loaded状態・健全性はこの概要から決めず `status` / `doctor` / `launchd status` の実行結果で確認する
- automatic transfer execution from diffd queue の実行可否は専用gateとruntime状態で確認する
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

## 古いdownload依頼・表示の再開地点

v0.2.3では転送元不在を確認待ちに残さず、download_review.pyで一致するeventだけ自動除外する。旧版のconfirmed-missing markerもadmitted tickで除外し証跡をstateへ保存する。local/cursor/new eventは保持し、認証・通信・child終了不明を不在として扱わない。performance.obsoleteは成功・失敗と分離する。

名前変更はdiffd_events.pyのremote_file_idとdiffidで集約し、service_daemon_plan.append_plan_recordのcoalesce_remote_fileで既存queueの古い同一file IDを置換する。追跡不能な旧recordの推測削除は禁止。transfer executor activeはstatusのtick lock観測値で、実行中を人の復旧待ちと誤表示しない。

回帰検証はtest_download_review.py、test_diffd_coalesce.py、test_plan_snapshots.py、test_transfer_concurrency.py。pcloud-status別rootは要操作の件数・理由・確認先をメニュー最上部へ出す。残り作業と公開releaseは実態を検証して判断する。

## 個別競合解消

0.2.4は `pushd|diffd transfer resolve list|preview|apply` を提供します。repository root基準の `src/pcloud_tools/conflict_resolution.py` → `cli_conflict.py` → `tests/test_conflict_resolution.py` を参照。xbar project rootの `resolve_ui.py` が選択・確認を行います。両版を私有stateへ退避し、世代照合後に1つのqueueだけをatomic更新します。scope・gate・cursorは不変。planned件数が大量になる原因調査とは別です。


## 手動で選択するクラウド取り込み（0.2.5）

`PCLOUD_TOOLS_DIFFD_DOWNLOAD_MODE=manual` は自動download executorを転送・queue消費なしで終了させる。API pollerは変更検出を続け、xbarにクラウド変更件数を表示する。互換既定値は `auto`。自動pushは継続するが、検出済みの同じパスのcloud変更は従来の競合保留対象となる。検出前の変更まで保護する保証はない。

xbarの「クラウド変更」からTerminalでファイル番号を選び、サイズと更新日時を見て、クラウド版を取り込む・ローカル版を採用する・保留から選ぶ。CLIは `diffd transfer manual list --json`、`preview --path PATH --choice pull|local --json`、`apply --path PATH --choice pull|local --token TOKEN --execute`。preview tokenはパス・設定・両側の内容・queue世代に結び付く。確認後に変化した場合は再確認が必要。

明示的な採用前に両版をstate配下の `manual-pulls/` に退避する。pullは一時ファイルから置換し、取り込みの再uploadを抑止する。localはuploadを予約する。元からローカルにないファイルでlocalを選ぶこと、delete/rename、symlink、scope外はこの入口では扱わない。保留・取消では転送しない。失敗時はreceiptと未確定attemptを保持して自動再試行せず復旧対象とする。退避は自動削除しない。

ファイル移動・改名ではmtimeだけで同一ファイルと判断しない。移動先の内容とcloud保存を確認して旧パスを別途整理する。cloudだけにあるファイルを自動pullで復活させない。同期除外のdirectoryを変更するときは既存queueにも同じscopeを適用する。

## 0.2.6の一括処理と画面

実装root基準で `src/pcloud_tools/manual_batch.py`、`cli_manual_pull.py`、`manual_pull.py`、`tests/test_manual_batch.py` の順に読む。batchは既存の個別preview/applyを呼び、GUI専用の同期判定を作らない。JSONの公開契約は技術仕様の「一括JSON処理」を参照。stdinとfileは同じschemaで、preview reportをそのままapplyへ渡せる。

画面はpcloud-status側のreview_app.py / review_bridge.py / review_assets。xbarは起動入口、pywebviewは表示と選択だけを担当する。既存の本番wheel分離・停止切替契約を維持する。

## 古いクラウド情報の再確認（0.4.3）

確認画面の「情報を再確認」、または `pcloud-manager diffd transfer manual recheck --execute` で再確認を依頼する。`--execute` なしはプレビュー。依頼は同期処理の区切りで処理されるため、直ちに完了するとは限らない。「一覧を更新」で結果を確認する。情報未確認の項目と、採用する版を選べる項目を区別し、取得失敗を含む一括確認では反映へ進めない。

古いファイルIDのイベントは、最新クラウド情報が検証済みbaselineと一致し、ローカル内容も両方と一致する場合だけ自動解除する。ファイルの転送・削除は行わず、取得した世代のイベントのみを消費する。それ以外は現在の両側情報を付けて採用判断へ回し、明示的な採用は最新情報に結び付いたtokenで再検証する。取得失敗ではイベントと保留を保持する。

SQLiteの再照合中は、通常イベント枠の一部（100件バッチなら最大10件）で古いIDの保留を順次再確認する。走査量とcursorを制限し、全件照合や新着同期を占有しない。小さな再確認依頼ファイルを別に保存するため、実行中バッチの状態保存によって依頼が失われない。DB schema・同期範囲・既存計測sessionは変更しない。

### 0.4.5

Review counts are recomputed from committed review rows inside the same short SQLite transaction. A sync batch cannot restore an older count after the independent review worker completes. No database schema change.
