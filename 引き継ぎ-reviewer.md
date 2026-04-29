# 引き継ぎ Reviewer

## 次スレ開始手順

1. 最初に読む: /Users/takafumi/p-core/dev/pcloud-tools/引き継ぎ-reviewer.md
2. 次に読む: /Users/takafumi/p-core/dev/pcloud-tools/報告.md
3. 次に読む: /Users/takafumi/p-core/dev/pcloud-tools/引き継ぎ.md
4. 次に読む: /Users/takafumi/p-core/dev/pcloud-tools/todo.md
5. 次に読む: /Users/takafumi/p-core/dev/#仕様書/pcloud-manager/AI向け概要.md
6. 必要なら読む: /Users/takafumi/p-core/dev/#仕様書/pcloud-manager/技術仕様.md

## このスレの役割

- このスレは reviewer 専用。実装は別スレの coder が進め、ここでは `報告.md` を起点にレビューだけ返す。
- reviewer 返答は `報告.md` の `To Implementer Codex:` に追記する。
- 解決済みログの退避先は `/Users/takafumi/p-core/dev/pcloud-tools/報告済み.md`。

## 現在地

- CLI 分割は reviewer 観点では一区切り。`src/pcloud_tools/cli.py` は薄い dispatcher になり、command 実装は `cli_action.py`, `cli_daemon.py`, `cli_index.py`, `cli_mount.py`, `cli_status.py`, `cli_sync.py` へ分離済み。
- `status` / `doctor` / `sync` / `sync status` / `mount` / `umount` / `index` / `daemon` / `action` の代表経路は確認済み。
- `todo.md` 上はまだ全実装完了ではない。未完了は `pcloud-pushd` / `pcloud-diffd` と、その後の shadow/limited migration validation / cutover / rollback。
- `報告.md` の最新 `To Implementer Codex:` では、次の作業単位として `pcloud-pushd` / `pcloud-diffd` の非破壊 scaffold だけを指示済み。

## reviewer として維持する判断

- `報告.md` は双方読み書きする共有帳票なので、そのままの名前で維持する。
- handoff は coder と衝突しないよう reviewer 側だけ suffix を付ける。
- dev isolation は最優先で見る。live remote / live links / live index / launchd 実行に dev 側から触れないこと。
- review は findings-first。追加指摘がなければ `追加指摘なし` を `報告.md` に返す。
- 実装担当へ返すときは reviewer が作業単位を明確に切る。細かすぎる往復と広すぎる変更を避けるため、「ここまで一気にやる」「ここで止めて報告する」「次の単位」を必要に応じて明記する。
- 特に refactor やテスト基盤追加では、先に不変条件を固定する単位、次に挙動変更なしの移動単位、最後に機能追加単位へ分ける。
- `報告.md` を読むときは、ついでに `wc -l 報告.md 報告済み.md` とファイルサイズも確認する。`報告.md` が長文化してきたら、解決済み往復を `報告済み.md` へ退避し、現役の `To Reviewer Codex:` 依頼と最新の `To Implementer Codex:` 指示だけ残す。
- `todo.md` の未完了がすべて解消され、reviewer 観点でも残作業がなければ、曖昧に止めずに「全部実装終わりです」と明示する。未完了が残っている間は、何が残っているかと次の作業単位を `報告.md` に返す。

## 今も効いている重要レビュー論点

- `pcloud-pushd` / `pcloud-diffd` は大物なので、最初は非破壊 scaffold だけを見る。fswatch 常駐、pCloud API long-poll、実 upload/download、launchd、public entrypoint 切替を一気に許可しない。
- dev mode から live remote / live links / live index / live launchd / live rclone cache を触らないこと。
- `sync` はサブシステム扱い。内部追加分割より先に代表経路テストを固める。
- `sync status` と lock 判定は machine-readable contract の一部なので、誤検知・見かけ上の healthy/idle は厳しめに見る。
- listing recovery は lock 取得後にだけ副作用を出す、という判断を戻さない。

## 未完了

- repo 全体の大きい未完了は `/Users/takafumi/p-core/dev/pcloud-tools/todo.md` が正本。
- reviewer 観点では、次は `pcloud-pushd` / `pcloud-diffd` の state/report/action 契約、dev isolation、xbar 向け action 契約が主戦場。
- `報告.md` に新しい `To Reviewer Codex:` が来たら、その commit/diff と実コマンド挙動を必ず突き合わせる。

## 注意点

- repo 直下の `Documents/` は既存ユーザーデータと見える。reviewer は触らない。
- 未追跡の `報告済み.md` は reviewer 管理ファイル。必要に応じて残す。
- `引き継ぎ.md` は coder 側 handoff と見て扱う。reviewer 側の再開点はこの `引き継ぎ-reviewer.md`。

## 次の一手

- 次スレ再開時はまず `報告.md` の末尾、`todo.md`、`git status --short` を見る。
- 新しい依頼が無ければ、最新 `To Implementer Codex:` の次作業単位が出ているので、それを coder 側に進めさせる。
- 新しい `To Reviewer Codex:` が入ったら、差分確認、実コマンド確認、必要なら `報告.md` へ返答、という順で進める。
