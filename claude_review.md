# Claude Review (for Codex)

reviewer: Claude (Opus 4.7)
worktree: `.claude/worktrees/modest-poincare-f58a4f`
ベース commit: `918ea5b` (Add exclusive pcloud mode switch)
読了範囲:
- `引き継ぎ.md`, `引き継ぎ-reviewer.md`, `thread-status.md`, `報告.md`, `報告済み.md` (概観)
- `#仕様書/pcloud-manager/AI向け概要.md`, `技術仕様.md`
- `README.md` (抜粋), `for-xbar.md` (未深掘り)
- `src/pcloud_tools/` 全モジュールの構造、`cli_service_daemon.py` の頭/`cmd_service_daemon` 周辺、`cli_sync.py` の構造、`config.py`, `output.py`, `download_suppression.py`, `cli.py`, `cli_status.py` 等の中身
- `tests/test_cli_invariants.py` のサイズと test 構成

本書は「動作に問題がある」という指摘ではなく、現状の preview-first / gated 設計はそのまま維持した上での **保守性・正確性・スケール** の論点まとめ。Codex への戻し材料として残す。

---

## 0. 全体所感

仕様/コードどちらも「事故を起こさない」方向に振り切られていて、その判断軸は支持できる。
- 多段ゲート、preview-first、`--reviewer-approved-*` フラグ、dev-state 限定の `--execute`、shadow validation report 必須化、conflict copy、suppression journal による echo 抑制、`.pcloudmanagerignore` の `!` 例外。
- ここを薄める提案はしない。逆に「契約は維持したまま、機械が間違えにくく、人間が読みやすい形に絞り込む」観点だけ書く。

ただし、現状のコードは **その保守的設計が文字列リテラルとコピペで実装されている** ところに集中的に脆さがある。下記 §1〜§3 が主戦場。

---

## 1. クリティカル: モジュールサイズと重複

### 1.1 `cli_service_daemon.py` 10,676 行 / top-level def 246 個

- 1 ファイルに `pushd` / `diffd`、`launchd` 系全サブコマンド、`fswatch` parser 統合、`api-poll`、`transfer preview / check / validation-matrix / real-gate / real-run / consume / executor-run / automation-gate / automation-run`、`queue` 操作、subprocess 制御、xbar/human/JSON render が全部入っている。
- `cmd_service_daemon` の dispatch は綺麗だが、その先の実装が 1 ファイルに同居しているせいで、reviewer 観点では「どのレイヤを触っているか」が一目で見えない。テスト失敗時のスタックトレースも全部このファイル内に着弾する。
- 提案: 不変条件を固定した状態で、**挙動変更なしの移動だけ** を 1 単位として分割する。先頭は機械的に切れる以下を勧める:
  - `cli_service_daemon/__init__.py` (dispatch + `_SERVICES` + `_add_service_parser` のみ)
  - `cli_service_daemon/launchd.py` (`_render_service_launchd_*`, `_print_service_launchd_*`, `_*_plist`, `_launchctl_*`)
  - `cli_service_daemon/transfer.py` (`pushd transfer ...`, `diffd transfer ...` 全部、`real-*`, `automation-*`)
  - `cli_service_daemon/fswatch.py` (`pushd fswatch ...` + resident)
  - `cli_service_daemon/api_poll.py` (`diffd api-poll ...` + folder-cache + long-poll-run)
  - `cli_service_daemon/queue.py` (`pushd queue ...`, `diffd remote-change ...`, `prune-excluded`)
  - `cli_service_daemon/status.py` (`_service_status_report`, `_render_service_status_xbar`, `_status_*_details`)
  - `cli_service_daemon/gates.py` (`_*_gate_*`, `_real_*_review_*`, ゲート approval flag 共通化)
- 「reviewer 観点で残作業がある」前提を崩さず、現行の `cmd_service_daemon` dispatch を温存したまま、import 関係だけ書き換える単位で進めれば、`test_cli_invariants.py` 143 件を維持しながら段階的に終わるはず。

### 1.2 `cli_sync.py` 2,562 行 / 63 def

- `sync_status` / `sync_progress` / `sync_execution` / `sync_scope` / `sync_check_allowlist` / `sync_clear_stale_lock` / `sync_background` / `sync_internal_run` / `sync_autosync_*` / `sync_migration_*` が同居。
- `引き継ぎ-reviewer.md:40` で「`sync` はサブシステム扱い、内部追加分割より先に代表経路テストを固める」とある通り、まず分割しないで代表経路テストを並走可能にする方が優先で、これは正しい順序。
- ただし、`autosync_*` と `migration_*` は既に独立した動詞群なので、 `cli_sync/autosync.py`、`cli_sync/migration.py` への分離は他より早く着手して安全。

### 1.3 `tests/test_cli_invariants.py` 10,026 行 / 148 test

- `pytest tests/test_cli_invariants.py -q` で 143 passed という記述があるが、現在は 148 関数。`148 - 143 = 5` が skip かどうかも単一ファイルだと追いづらい。
- ファイル名が「invariants」だが実態は CLI 統合テスト全般 (sync, mode, pushd/diffd 各層、launchctl runner)。
- 提案: モジュール分割と並行して `tests/cli/`, `tests/service_daemon/`, `tests/sync/`, `tests/config/` 程度に物理分割すると、Codex/Claude 双方が「触ったレイヤだけ走らせる」が現実的になる。フィクスチャ helper は `tests/conftest.py` に寄せる。

### 1.4 cli_* モジュール群に同名 helper が 10 重複

`grep -lE "^def (_has_errors|_status_from_issues|_has_warnings|_report_issues|_issue_sort_key|_sort_issues|_exit_code_for_report|_entrypoint_command|_action_command|_shell_command|_output_format|_print_report)\("` の結果:

```
cli_gates.py / cli_archive.py / cli_mount.py / cli_notify.py / cli_index.py /
cli_daemon.py / cli_sync.py / cli_mode.py / cli_service_daemon.py / cli_status.py
```

全部に同じシグネチャ・同じ実装でコピーされている (`cli_status.py` と `cli_service_daemon.py` で `_has_errors` / `_status_from_issues` 確認済み)。

- リスク: 片方だけ「`_status_from_issues` の優先度を warning -> info に下げる」のような調整をされると、レポート挙動がモジュール間で乖離する。実際 cli_service_daemon.py:1003 の `_print_report` は他にも複製されている。
- 提案: `pcloud_tools/cli_common.py` (もしくは `output.py` に追加) に
  - `has_errors(issues)`, `has_warnings(issues)`, `status_from_issues(issues)`
  - `report_issues(issues)`, `sort_issues(issues)`, `issue_sort_key(issue)`
  - `exit_code_for_report(report)`, `print_report(report, args)`, `output_format(args)`
  - `entrypoint_command(paths)`, `action_command(paths, action_id)`, `shell_command(value)`

  を集約し、全 cli_* モジュールがそこから import する。**挙動変更なしの単位** で 1 PR に閉じる。

---

## 2. ゲート/承認フラグの表現が文字列リテラル散在

### 2.1 `_*_GATE_VALUE` 定数が 22 個

```
cli_archive.py:                   _OLD_MONOLITH_ARCHIVE_GATE_VALUE
cli_sync.py:                      _AUTOSYNC_LAUNCHD_GATE_VALUE, _SYNC_MIGRATION_GATE_VALUE
cli_mode.py:                      _MODE_SWITCH_GATE_VALUE
cli_service_daemon.py:            18 個(pushd/diffd × launchd / plist / reload / fswatch / api-poll / catchup / checkpoint / automation / automation-run / transfer / queue-remove / queue-prune-excluded + execution)
```

- ゲートごとに「env var 名」「期待値」「対応する CLI コマンド」「approval flag セット」が散らばっている。
- 例: `PCLOUD_TOOLS_PUSHD_LAUNCHD_RELOAD_GATE = "operator-approved-pushd-launchd-reload-v1"` は、`_PUSHD_LAUNCHD_RELOAD_GATE_VALUE` 定義箇所、`config.py` の field、それを参照する `cli_service_daemon.py` の `_*_reload_*` ハンドラ、と 3 箇所に散る。
- 提案: 1 箇所に gate registry を作る。

  ```python
  # pcloud_tools/gates.py
  @dataclass(frozen=True)
  class GateSpec:
      name: str                       # "pushd.launchd.reload"
      env_var: str                    # "PCLOUD_TOOLS_PUSHD_LAUNCHD_RELOAD_GATE"
      expected_value: str             # "operator-approved-pushd-launchd-reload-v1"
      approval_flags: tuple[str, ...] # ("reviewer-approved-bootout-bootstrap", ...)
      summary: str
  ```

  - `GATES["pushd.launchd.reload"]` で全要素引ける。
  - argparse 側は `add_gate_review_args(parser, GATES["pushd.launchd.reload"])` で statically 生成。
  - validation も `validate_gate(GATES["pushd.launchd.reload"], args, env)` 共通関数に集約。
  - 仕様書(`AI向け概要.md`)で列挙されている operator-approved-* 名と Python 側の対応を 1 個のテーブルから生成できるので、ドリフトが起きづらい。

### 2.2 同じ `--reviewer-approved-*` フラグセットが複数 parser で繰り返し定義されている

- `cli_service_daemon.py:186-199`、`cli_service_daemon.py:234-246`、その他で `_add_automation_review_args`、`_real_gate_args` などのヘルパーが部分的に切られているが、まだ網羅されていない。argparse の add_argument が **466 回** 呼ばれている時点で、半数以上は重複用途。
- 提案: §2.1 と合わせて、`approval_flags` を持つ GateSpec から argparse 引数を生成する単一関数で再構成。フラグ追加忘れによる「ゲート開いてないつもりが開いてた / 逆に開かない」事故を構造的に防げる。

### 2.3 `--json` / `--xbar` の重複

- `add_argument("--json", action="store_true", ...)` だけで `cli_service_daemon.py` に 94 回登場。
- 提案: `def add_output_flags(parser, *, xbar=True)` を共通化して全 parser から呼ぶ。

---

## 3. クリティカル寄り: `download_suppression.py` の構造的コピペ

- `read_download_suppression_journal` (160-204) と `read_upload_origin_journal` (207-251) は本体ロジックがほぼ同一で、`upload` の方だけ `record.direction != "upload"` filter があるだけ。
- `write_download_suppression_journal` (254-264) と `write_upload_origin_journal` (267-277) は schema_version だけ違って同じ。
- `mark_download_completed` と `mark_upload_completed` も同様。
- リスク:
  - 既に TTL が `PCLOUD_TOOLS_DOWNLOAD_SUPPRESSION_TTL_SECONDS` を **両方が** 参照していて、命名が誤誘導している(`read_upload_origin_journal` でも同じ env を使う)。
  - 片方だけ修正されたときに気付きにくい(`SCHEMA_VERSION` がローカル定数化されているため)。
- 提案:
  - `JournalKind = Literal["download_suppression", "upload_origin"]` を導入し、`_read_journal(config, kind)` / `_write_journal(config, kind, records)` の単一実装にまとめる。
  - 上位 API (`mark_download_completed` 等) はその上の薄いラッパに保つ。

---

## 4. 並行アクセス・耐障害性

### 4.1 atomic write が徹底されていない

- `download_suppression.py:263, 276` は `path.write_text(json.dumps(...) + "\n")` で直接 truncate-write。
- 同じく `service_daemon_plan.py`, `service_daemon_state.py`, `daemon_state.py`, `cli_service_daemon.py` 内の各種 `last-*.json`、`last-transfer.json`、`api-long-poll-last-run.json` 等の書き込みも、流し読みでは tmp + `os.replace` パターンを徹底している様子がない(全数点検は未実施 → Codex 側で要確認)。
- リスク: launchd の StartInterval=60s で executor / poller が走り、同時に operator が手動 CLI を叩く運用がある以上、書き込み中の crash で空 / partial JSON が残ると次回 read が壊れる(ConfigIssue で warning にはなるが、`status` が "warning" で見えるだけ)。
- 提案: `pcloud_tools/io_utils.py` に `atomic_write_json(path, payload)` を作って全書き込みパスをここに通す。並行書き込み自体は file lock (`fcntl.flock`) で守る。少なくとも queue/journal/last-transfer の 3 系統。

### 4.2 journal の読み込みが broken-JSON で空に化ける

- §1 で書いた warning ConfigIssue は出るが、運用者が `status` を見ないと気付かない。
- 提案: notify 設計に「journal corruption」を 1 種別追加するか、`doctor` の bundle に必ず含める。

---

## 5. Config 配線の冗長性

- `config.py:213` の `load_config` は AppConfig field 30+ 個ぶん `_path_value` / `_string_value` / `_int_from_value` / `_csv_value` / `_bool_from_value` を個別に呼ぶ。
- 新規 env var を増やす際、`_defaults_for_runtime`, AppConfig dataclass, `load_config` の 3 箇所を必ず編集する必要がある。1 箇所漏れると黙って default に化ける。
- 提案: field spec をテーブル化:
  ```python
  FIELDS: tuple[FieldSpec, ...] = (
      FieldSpec("core_dir", "PCLOUD_TOOLS_CORE_DIR", "path", default=...),
      FieldSpec("vault_port", "PCLOUD_TOOLS_VAULT_PORT", "int", default="5566"),
      ...
  )
  ```
  `load_config` は `for spec in FIELDS: setattr_value = parse(spec, values)` を回すだけにする。`AppConfig` も `@dataclass` のまま、フィールド宣言から spec を生成する補助も検討可。

### 5.1 env 値展開の循環/再帰ガード

- `_expand_value` は `${VAR}` 形式を mapping/os.environ から引いて文字列置換するが、置換先にまた `${...}` が含まれていても再帰展開しないし、循環参照のガードもない(現状の default テーブルに循環がないだけ)。
- リスク: ユーザの `.env` で `A=${B}` `B=${A}` を書かれても黙る/壊れる。
- 提案: 展開深さに上限を付ける or 1 段展開仕様であることを `config.py` のモジュール docstring に明示する。

### 5.2 quote 剥がし仕様

- `parse_env_file` は単純 `'..'` / `".."` を剥がすだけ。エスケープ未対応。
- 現状の `.env.example` の運用ではおそらく問題ないが、`PCLOUD_TOOLS_CHAT_NOTIFY_CMD` 等にスペースや特殊文字を入れる運用が今後出ると壊れる。
- 提案: 仕様コメントで「単純な KEY=VALUE しかサポートしない」と明示。複雑な値は `${...}` で別 env から渡す運用に固定する。

---

## 6. `cli.py` dispatch の構造

- `cli.py:76-132` は if/elif の long chain で 8 種類の「`cmd_*` を呼んで None なら print_help して 1 を返す」パターンを繰り返している。
- 提案: ハンドラテーブル化。
  ```python
  HANDLERS: dict[str, Callable[[Namespace, RuntimePaths], int | None]] = {
      "info": cmd_info,
      "status": cmd_status,
      ...
  }
  ```
  `pushd` / `diffd` は同じ `cmd_service_daemon` に向ける。これだけで `cli.py` は 50 行程度に縮む。

---

## 7. `CommandReport.details` の暗黙規約

- `output.py:106-112` の xbar renderer は `if isinstance(value, list): lines.append(f"{key}: {len(value)}")` を使う。つまり「list は件数で見せる」「dict は何もしない」「str/int はそのまま見せる」が暗黙ルールになっている。
- リスク: 新規 detail を追加する際に「これは件数で出てほしいのに dict にしたから消える」「文字列に間違って改行が入って xbar が壊れる」を踏みやすい。
- 提案:
  - `details` の代わりに `DetailItem(key, value, render_hint=Literal["count","scalar","raw","list"])` を許容する。
  - 既存 dict 形を deprecate する移行期間を設ければ、tests が網羅できているので機械的に置換可能。

---

## 8. xbar 表面の対称性

- `pushd status --xbar` と `diffd status --xbar` は仕様書で「compact」とされ、`gates status --xbar` も同様。一方 `mode status --xbar` などは説明書にしか触れない。
- 提案: 仕様書側に「xbar surface を持つコマンドの完全リスト」を 1 箇所まとめる。`for-xbar.md` がその役割を担っているなら、`AI向け概要.md` から `for-xbar.md` への参照を必須とする。

---

## 9. ドキュメント運用

### 9.1 `AI向け概要.md` (215 行) / `技術仕様.md` (590 行) / `README.md` (約 48 KB)

- 三者で「現在の live 状態」「契約」「履歴的決定」が混ざる。とくに `AI向け概要.md` の "現在の live 状態" は週単位で内容が変わるので、commit のたびに大きな diff が出る。
- 提案:
  - `AI向け概要.md` から live status を切り出し、`docs/live-state.md` のような可変ファイルに分離。仕様の不変条件 (`pushd は queue-only`, `--execute は dev-state 限定` 等) は概要にだけ残す。
  - `cutover-readiness-package.md` は複数箇所で「historical」と注記されているが、まだ仕様参照ツリーに残っている。`docs/archive/` に物理移動して、参照を 1 箇所に絞った方が混乱しない。

### 9.2 `README.md` (48 KB)

- public CLI 利用者向けには重すぎる。`README.md` は overview + 入口だけにして、運用詳細は `docs/operations.md` に寄せる。

### 9.3 `引き継ぎ.md` と `引き継ぎ-reviewer.md` の役割境界

- 良くできている。ただ「coder スレ用 / reviewer スレ用」の 2 系統がある事自体を、最初の 1 行で明示しておくと未来の人(or AI)が迷わない。

---

## 10. 細かい指摘

- **`chat_notify.py:138` `except Exception:`**: 唯一の bare Exception catch。理由コメントが欲しい(通知失敗で本処理を絶対に止めない、という意図のはず)。
- **`_VAR_PATTERN.sub` 再帰深さ無制限**: §5.1 と同件。
- **`cli_service_daemon.py:1007 _shell_command` と同 1013 `_xbar_escape`**: `output.py` の同名処理と被っている。集約候補。
- **`ServiceDefinition` の `summary_name` / `status_help` / `preview_help`**: その他 launchd/transfer/etc. の help 文字列はベタ書きなので、定義場所が分散。`ServiceDefinition` を拡張して help レジストリ化する手はある(優先度低)。
- **`pushd transfer automation-run` の `--max-records 10` (executor 実機) vs `--max-records 1` (default)**: 仕様書 (AI向け概要.md:51) で「default は one transfer record per tick」と書いてあるが、報告.md:6 では実機 `--max-records 10`。これ自体は意図的(`pushd-executor` plist が `--max-records 10` で書かれている)だが、ドキュメントだけ追うと食い違いに見える。注記が欲しい。
- **`/Users/takafumi/p-core/dev/pcloud-tools/Documents`**: 引き継ぎ-reviewer.md:52 に「触らない」と明記。良い。 ただし `.gitignore` に該当 entry があるか念のため確認推奨(レビュー時点では未確認)。
- **rclone bisync の `local__Users_takafumi_p-core..pcloud_core.lck`**: ハードコードされた path 表現が `cli_sync.py` 内に複数あれば集約余地。未深掘り。

---

## 11. 次の作業単位として勧める並び

ぜんぶ「挙動変更なし、テスト維持」のリファクタ単位で書く。

1. **`cli_common.py` 集約** (§1.4): `_has_errors` 系 12 関数を 1 箇所に。各 cli_* は import に置換。テスト 143 件を維持。これは reviewer 観点で 1 PR 1 完結。
2. **`cli_service_daemon.py` 分割の第 1 単位** (§1.1): まず `launchd` 系の `_render_*` / `_print_*` / `_*_plist_*` だけを `cli_service_daemon/launchd_render.py` に move。export を維持する re-export を `__init__.py` に書けば call site は触らずに済む。
3. **gate registry 導入** (§2): `gates.py` を新設し、まず 1 ゲートだけ移植(例: `pushd.launchd.reload`)。テストを通したまま漸進。
4. **`download_suppression.py` 統合** (§3): `_read_journal(kind)` / `_write_journal(kind)` 化。挙動は維持。
5. **atomic write 共通化** (§4.1): `io_utils.atomic_write_json` を作って、まず journal 系から差し替え。
6. **テスト分割** (§1.3): モジュール分割と並行して `tests/` を物理分割。
7. **config の spec テーブル化** (§5): 影響大きいので最後の方。先に gate registry が落ち着いてからの方が安全。

---

## 12. 触らない方が良いと思うもの

- `--reviewer-approved-*` フラグの数自体は維持。減らすと安全性が下がる。
- `dev-state` 限定の `--execute` 仕様。
- `.pcloudmanagerignore` の `!` 例外と `.partial` の hard exclude。
- `mode switch` の terminal gate と `PCLOUD_TOOLS_MODE_SWITCH_GATE`。
- `normal sync/resync` / listing cache / autosync launchd changes を別 gate に置いている運用 invariant。

---

以上。
本書は recommend だけ書いてあり、実装変更は加えていない。
Codex 側で取捨選択し、`報告.md` の `To Implementer Codex:` / `To Reviewer Codex:` の往復に落とし込んで進めて欲しい。
