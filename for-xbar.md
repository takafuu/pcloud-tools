# for-xbar.md

`pcloud-status` 側で `pcloud-manager` の daemon 状態を menu bar に統合するときのメモ。

参照先:

- `/Users/takafumi/p-core/dev/#仕様書/pcloud-status/技術仕様.md`
- `/Users/takafumi/p-core/dev/xbar/pcloud-status/pcloud-status.30s.sh`

## 推奨コマンド

現行の `/Users/takafumi/p-core/dev/xbar/pcloud-status/pcloud-status.30s.sh` は、すでに Python 埋め込み + JSON report 読み取り方式になっている。これは良い方向なので、`--xbar` の文字列 parse ではなく JSON contract を正本にする。

```sh
pcloud-manager status --detail --json
pcloud-manager sync status --json
pcloud-manager sync progress --json
pcloud-manager pushd status --json
pcloud-manager pushd preview --json
pcloud-manager diffd status --json
pcloud-manager diffd preview --json
pcloud-manager daemon status --json
pcloud-manager gates status --json
```

追加で読むとよいもの:

```sh
pcloud-manager notify status --json
```

`notify status --json` は read-only なので、30 秒 interval で読んでよい。現行 `COMMANDS` にはまだ `notify` がない。

互換用に `pushd status --xbar` / `diffd status --xbar` / `notify status --xbar` / `gates status --xbar` もあるが、統合 plugin 本体では JSON のままでよい。

## Menu への足し方

現行 plugin にはすでに `Push`, `Pull`, `Gates` section がある。新規 `Daemons` section を足すより、既存 section に次の行を足すのが差分が小さい。

```text
Push
  download suppressions: completed/conflicts/latest
  upload echo: suppressed
  notify: abnormal-only

Pull
  download suppressions: completed/conflicts/latest
  upload echo: suppressed
  notify: abnormal-only
```

`Gates` は現行のままでよい。ただし gate rows は長くなりやすいので、top-level には gate count だけ、詳細は submenu に留める。

`pushd status --xbar` / `diffd status --xbar` は単体 plugin としても成立する出力。統合側では先頭行の `pCloud OK/WARN/ERR` と最初の `---` を global top line に使わない。現行どおり JSON から global top line を組み立てる。

## 現行 source への具体的な差分

`COMMANDS` に notify を追加する。

```py
"notify": [PCLOUD_MANAGER, "notify", "status", "--json"],
```

`first_failure()` の loop にも `"notify"` を入れると、notify command 自体の JSON failure が top line error に出せる。ただし notify disabled は正常状態なので warning/error にしない。

```py
for name in ("status", "sync", "pushd", "diffd", "daemon", "gates", "notify"):
```

`first_health_warning()` / `first_health_error()` に notify を含めるかは任意。`notify status` は disabled でも `status: ok` なので含めても問題ない。

`details` 取得も足す。

```py
notify_details = details("notify")
```

`Push` section に足す候補:

```py
emit_subitem(
    f"download journal: completed={pushd_details.get('download suppression completed', 0)}; "
    f"conflicts={pushd_details.get('download conflict count', 0)}"
)
emit_subitem(f"upload echo: suppressed={pushd_details.get('upload origin completed', 0)}")
emit_subitem(f"notify: {notify_details.get('chat notify mode', pushd_details.get('chat notify mode', '-'))}")
```

`Pull` section に足す候補:

```py
emit_subitem(
    f"download journal: completed={diffd_details.get('download suppression completed', 0)}; "
    f"conflicts={diffd_details.get('download conflict count', 0)}"
)
emit_subitem(f"upload echo: suppressed={diffd_details.get('upload origin completed', 0)}")
emit_subitem(f"notify: {notify_details.get('chat notify mode', diffd_details.get('chat notify mode', '-'))}")
```

conflict が 1 以上なら、目立つ色にする。

```py
conflicts = to_int(diffd_details.get("download conflict count", 0))
emit_subitem_action(
    f"download conflicts: {conflicts}; latest={diffd_details.get('download latest conflict', '-')}",
    color="orange" if conflicts else None,
)
```

## 重要な表示項目

JSON としては次の details keys が出る。現行 plugin は `pushd_details` / `diffd_details` から直接読むのがよい。

- `download suppression completed`
- `download conflict count`
- `download latest conflict`
- `download suppression expired records`
- `upload origin completed`
- `upload origin expired records`
- `missing local upload records`
- `missing local upload record details`
- `chat notify mode`
- `chat notify dedupe seconds`

意味:

- `upload origin completed`: pushd upload 成功後に、同一 local fingerprint が残っている upload-origin journal record 数。remote echo download を skip できる状態。
- `download suppression completed`: diffd download 成功後に、同一 local fingerprint が残っている download suppression record 数。download-generated fswatch event を upload から除外できる状態。
- `download conflict count`: diffd download 中に same-path local change を検出し、conflict copy を作った数。0 以外は目立たせる。
- `missing local upload records`: pushd queue にあるが local source file がもう存在しない upload record 数。ユーザーが一瞬作ったファイルを upload 前に削除した状態として扱い、transfer failure ではなく stale cleanup 対象として表示する。
- `chat notify dedupe seconds`: 同じ異常通知を再送しない秒数。timeout などが毎 tick 続いても Discord を連投しないための cooldown。

## Missing Local Uploads

Push 側の Activity 表示には `missing local upload records` を `stale` または `missing` として出す。

例:

```text
⚠️ Activity: push 0 / stale 3 / pull 0
```

展開例:

```text
Push
  ⚠️ Missing local upload records: 3
    Ignore missing local upload records
    Documents/test1.jpeg
    Documents/test2.jpeg
    Documents/pcloud_txt.txt
```

cleanup action は stable action id から呼ぶ。

```sh
pcloud-manager action pushd.queue.prune-missing-local
```

xbar 属性:

```text
terminal=false refresh=true
```

Label:

```text
Ignore missing local upload records
```

Tooltip / 補足:

```text
Use when files were created locally and then intentionally removed before upload.
```

この action は missing local upload record だけを queue から消す。存在している upload record、excluded record、invalid record、delete/rename/move の manual-review record は対象外。xbar 側で独自に queue file を編集しない。

diffd 側では、missing local upload record は opposite-side conflict として扱われない。つまり remote download が同じ path に来た場合でも、missing upload queue が原因で pull が manual-review にならない。

## Actions

`--xbar` 出力の action 行は `pcloud-manager action <id>` に寄せてあるので、xbar 側で gated command を手組みしない。

例:

```text
Refresh pushd state | bash=/Users/takafumi/bin/pcloud-manager terminal=false refresh=true param1=action param2=pushd.status.refresh
Preview pushd plan | bash=/Users/takafumi/bin/pcloud-manager terminal=true refresh=false param1=action param2=pushd.preview
```

`terminal=false` は xbar から直接押してよい軽い refresh/status/test 系。`terminal=true` は terminal で人間確認するものとして扱う。

通知系は terminal-free でよい。現行 plugin に入れるなら `Notify` submenu を作る。

```sh
pcloud-manager action notify.chat.status
pcloud-manager action notify.chat.enable
pcloud-manager action notify.chat.disable
pcloud-manager action notify.chat.test
pcloud-manager action pushd.queue.prune-missing-local
```

実装例:

```py
emit("---")
emit_submenu("Notify")
emit_subitem(f"discord: {notify_details.get('chat notify mode', '-')}")
emit_subitem(f"dedupe: {notify_details.get('chat notify dedupe seconds', '-') }s")
emit_subitem_action("Enable Discord abnormal notify", bash=PCLOUD_MANAGER, params=["action", "notify.chat.enable"], terminal=False, refresh=True)
emit_subitem_action("Disable Discord notify", bash=PCLOUD_MANAGER, params=["action", "notify.chat.disable"], terminal=False, refresh=True)
emit_subitem_action("Send Discord notify test", bash=PCLOUD_MANAGER, params=["action", "notify.chat.test"], terminal=False, refresh=True)
```

Missing local cleanup の例:

```py
missing_uploads = to_int(pushd_details.get("missing local upload records", 0))
if missing_uploads:
    emit_subitem_action(
        "Ignore missing local upload records",
        bash=PCLOUD_MANAGER,
        params=["action", "pushd.queue.prune-missing-local"],
        terminal=False,
        refresh=True,
    )
```

## 避けること

xbar 側では次を直接出さない、または少なくとも terminal human review の深い menu に隔離する。

- `transfer real-run`
- `transfer automation-run`
- `launchd reload`
- `launchd register`
- `launchd automation-reload`
- `queue clear`
- `remote-change clear`
- `sync --execute`
- `resync`
- listing cache 操作

特に `launchctl bootstrap/bootout` や real upload/download command は、xbar plugin が自動で作ったり実行したりしない。必要なときは `pcloud-manager ... --xbar` が出す `terminal=true` action から人間が terminal で見る。

## JSON contract

見る field は `status`, `summary`, `details`, `issues`, `actions`。

優先して表示する `details` keys は次。

- `planned uploads`, `pending queue items`, `manual review transfer records`
- `missing local upload records`
- `planned downloads`, `remote changes`, `daemon diffid`
- `launchd registration`, `launchd loaded`
- `last api poll run status`, `last api poll run summary`
- `download conflict count`, `download latest conflict`
- `upload origin completed`
- `chat notify mode`
- `chat notify dedupe seconds`

`actions` は `id`, `label`, `command`, `terminal`, `refresh` をそのまま使える。

現行 plugin はすでに `emit_manager_action()` / `emit_subitem_action()` で action を明示実装している。今後 action を増やすときも、`pcloud-manager action <id>` を呼ぶ形に寄せる。

## Executor Batch Size

public queue executor plist は `automation-run --max-records <N>` を持つ。xbar 側では `1` 固定と仮定しない。

当面の標準は `--max-records 10`。表示するなら `automation batch limit` / `ProgramArguments` から読む。ユーザー向けには「1 tick あたり最大 N 件」として出す。

## 更新間隔

現行 plugin は 30 秒 interval。pushd/diffd public executor と diffd poll は 60 秒 tick なので、30 秒更新で十分。連続 click で refresh しても read-only だが、xbar 側で同時多重起動を避ける lock はあるとよい。

## Info Surface

`pcloud-manager info paths` と `.pcloudmanagerignore` の内容は xbar に出さない。必要なときは CLI で確認する。

xbar は運用状態だけに絞る:

- queue / planned / manual-review
- launchd loaded
- last-run
- conflict / missing-local
- notify mode
