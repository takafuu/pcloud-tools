# Claude Review #2 (post Codex 757eff7..e6bf38f)

reviewer: Claude (Opus 4.7)
対象 commit: `757eff7` (refactor cli helpers and state writes), `991e3b8` (split cli invariant tests by area), `e6bf38f` (table-drive config loading)
ベース: 前回レビュー `claude_review.md` / `todo.md` §1〜§7

## 0. 結論

- 全 7 単位に着手済み。**実質完了**: §1 (cli_common), §4 (download_suppression 統合), §6 (tests 分割), §7 (config table-drive)。**部分着手 (todo 指定どおり)**: §2 (launchd_render だけ抽出), §3 (1 ゲートのみ移植), §5 (journal 系だけ atomic_write 化)。
- 受け入れ基準は全てクリア: `pytest -q` 157 passed (143→157)、shadow validation `status: ok`、`compileall` ok。重複 helper 12 関数は完全消滅。
- 採点としては大変良好。下記は次の往復で返したい改善点のみ。**不変条件違反は §1 に 1 件あり**(後述)、それ以外は将来単位への申し送り。

---

## 1. 不変条件違反 (1 件): `cli_status.py` に混合 commit

- commit `757eff7` "refactor cli helpers and state writes" の `cli_status.py` 差分 (+316 / -59 lines) に、helper 集約とは無関係な **新規機能** が混入している。
  - `doctor --detail` フラグ追加
  - `doctor --plain` フラグ追加
  - `daemon_state.read_daemon_state` / `service_daemon_plan.build_pushd_plan` / `build_diffd_plan` / `service_daemon_state.read_service_daemon_state` の利用追加
- これは todo §1 の不変条件「argparse の表面、CommandReport の payload、xbar/JSON 出力、exit code、いずれも変わらない」に違反する。
- リスク: reviewer audit が難しくなる。`git bisect` で「`doctor --detail` が壊れた」を追うと、refactor commit が当たる。
- 提案 (次の往復):
  - 今回の混入分は revert しなくていい(`tests/test_state_io.py` 等で動作が固定されている)。
  - **次回以降**: 「挙動変更なし」と書いた単位には新規 CLI flag / 新規依存 import を入れない。混ぜたい場合は別 commit に分割してほしい(`refactor: extract cli_common helpers` と `feat: add doctor --detail/--plain` の 2 commit に分ける)。
  - commit message を真にする: 今回は「refactor cli helpers and state writes」だが実態は「refactor + feature add」。

---

## 2. §2 続き: `cli_service_daemon/__init__.py` の次の分割単位

- 現状: `__init__.py` 10,335 行 + `launchd_render.py` 319 行 + `cli_service_daemon.py` 削除済み。
- 提案: 次は **transfer 系** の抽出が最も効果大。`pushd transfer ...` / `diffd transfer ...` の preview / validation-matrix / check / real-gate / real-run / consume / executor-run / automation-gate / automation-run の **render / print** 群 + `_real_transfer_*_details` を `cli_service_daemon/transfer_render.py` に移動。
  - 既に launchd_render が前例になっているので機械的に切れる。
  - render と print だけにとどめ、plist 構築や launchctl 実行はこの単位では触らない (前例どおり)。
- 不変条件: argparse の表面、`pushd|diffd transfer *` の出力(human/JSON/xbar)、exit code 全部維持。
- 受け入れ: tests / shadow validation 維持 + `pcloud-manager-dev pushd transfer preview --json` の出力が PR 前後で byte-exact (`generated_at` を除く)。

その後の単位は: `fswatch_render` → `api_poll_render` → `queue_render` → `status_render` → `gates_render`。最後は `__init__.py` を「dispatch + `_SERVICES` + `_add_service_parser`」だけにする。

---

## 3. §3 続き: gates registry の次の移植

- 現状: `gates.py:50` の `GATES` に 1 spec (`pushd.launchd.reload`) のみ。
- 提案: 次は対称性を取って `diffd.launchd.reload` を入れる。これにより「同種の operation を 2 サービスで持つ」spec シェイプが固まり、後続移植が機械的になる。
  - その次は `pushd.fswatch.resident` と `diffd.api.long-poll` の中核 2 つ。
  - `automation-*` (multi-flag / multi-env) は最後。
- 補足: `add_gate_review_args` (gates.py:64-66) が `--reviewer-approved-*` フラグを help なしで `add_argument` している。`pcloud-manager pushd launchd reload --help` で operator がフラグの意味を読めない。
  - 提案: `parser.add_argument(flag, action="store_true", help=f"Reviewer approval: {summary} ({flag.lstrip('-')})")` 程度を付ける。
  - これは現行 1 spec しかないうちに足しておくと、追加 spec が漸進的に綺麗な help を持つ。

---

## 4. §5 続き: atomic write 適用拡張

- 現状: `io_utils.atomic_write_json` は journal 系 (`download_suppression.py`) に適用済み。`tests/test_state_io.py:64` で `os.replace` 失敗時に旧内容が残ることを assert する crash テスト枠組みあり。
- まだ raw `write_text` が残っているファイル (`grep` で確認):
  - **`daemon_state.py:177` `diffid` 書き込み**
  - **`daemon_state.py:184` `auto_download` 書き込み**
  - `cli_archive.py:469` manifest
  - `cli_mode.py:584` mode switch run state
  - `cli_sync.py:1852, 2492` autosync / migration run state
  - `sync_exec.py:82, 127, 190-219` 各種 sync lock / log pointer
- 提案 (次の単位): `daemon_state.py` の 2 件を最優先で `atomic_write_text` 化。
  - 理由: launchd 60s 周期で動く diffd の checkpoint と、手動 `pcloud-manager daemon ...` / `pcloud-manager diffd ...` が **同じ 2 ファイル** を書き込む可能性が最も高い。
  - `diffid` は数値 1 行、`auto_download` は `"on\n"` / `"off\n"`。`atomic_write_text(path, content)` で 1:1 置換可。
  - crash テストも `test_state_io.py` パターンをコピーして 2 件追加で済む。
- それ以外 (`cli_archive`, `cli_mode`, `cli_sync`, `sync_exec`) は並行アクセスのリスクが構造的に低い (操作中に対応する gate / launchctl が動かない設計) ので、優先度を下げて良い。

---

## 5. §7 補足: config の default テンプレ仕様を明示

- `FieldSpec.default` 文字列に `{home}` / `{workspace_root}` / `{base_state_dir}` / `{base_log_dir}` / `{env_file}` を含めると、`_defaults_for_runtime` (config.py:215) で `.format(**tokens)` が展開される暗黙仕様。
- 一方 `chat_notify_cmd` の default は `"{home}/bin/notify send --to discord {{message}}"` でエスケープ済み(`{message}` は format 後に保持して、後で `cmd.format(message=...)` で使う想定)。
- これは読み手にはコードを追わないと見えない。
- 提案: `config.py` の module docstring か `FieldSpec` dataclass の docstring に、
  ```
  default テンプレ規約:
    - {home}, {workspace_root}, {base_state_dir}, {base_log_dir}, {env_file} は
      _defaults_for_runtime で format 展開される。
    - 展開後に残したい brace は {{...}} でエスケープ (例: chat_notify_cmd の {{message}})。
  ```
  程度を書いてほしい。

---

## 6. 小指摘

### 6.1 `tests/test_cli_invariants.py` は完全削除でよい

- 4 行 docstring だけ残っている。`tests/conftest.py` 自体がドキュメンテーションを兼ねるので、削除して問題ない。残すと「ここに何かあるはず」と読み手が探してしまう。

### 6.2 `from conftest import *` は pytest 慣用に反する

- `tests/test_state_io.py:3` ほか、各 test ファイルが `from conftest import *` で `_base_env` / `_run_cli` 等を取っている。
- pytest の慣用は「conftest の `@pytest.fixture` を引数で受け取る」もしくは「明示 import (`from tests.conftest import _base_env`)」。`import *` は名前空間が読みにくく、IDE 補完も効きづらい。
- 優先度低。テスト基盤の構造改善として、いずれ。

### 6.3 `cli_common.py` の旧名エイリアス

- 各 cli_* モジュールで `from .cli_common import action_command as _action_command` のように **旧名で as エイリアス** している。callsite を触らないための過渡的措置として正しい。
- 最終形 (3〜4 単位先) では、各 cli_* モジュール内の `_action_command` 等を public 名 `action_command` に書き換える単位を別途切ると、`_` プレフィックス由来の「module-private」見せかけが消えて読みやすい。優先度低。

### 6.4 `cli_status.py` の `daemon_state` / `service_daemon_plan` use

- §1 に書いた通り、混入機能の一部。`doctor --detail` で daemon state や pushd/diffd plan を bundle する設計自体は妥当(operator 価値あり)。コミット粒度の問題だけ。

---

## 7. 推奨される次のセット (todo §2/§3/§5 の続き)

順番:

1. **§5b: `daemon_state.py` の `diffid` / `auto_download` を atomic_write_text 化** (最小単位、テストパターン既存)
2. **§3b: `diffd.launchd.reload` ゲート移植 + `add_gate_review_args` に help を足す** (対称性確保 + UX 改善)
3. **§2b: `cli_service_daemon/transfer_render.py` を抽出** (機械的 move、launchd_render が前例)
4. (余力) **§7b: FieldSpec の default テンプレ仕様を docstring に明記** (1 commit / 数十行)

§1a として、混入機能との分離は遡って revert はせず、ルールだけ次回から徹底する。

---

以上。
報告書として `claude_review_2.md` に書き、`報告.md` への落とし込みは別途。
