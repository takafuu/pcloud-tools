# pcloud-manager cutover readiness package

Last updated: 2026-04-26

この手順書は public `pcloud-manager` entrypoint を切り替える前の readiness package です。この文書の作成時点では、entrypoint 切替、launchd 作成・変更・load、fswatch 常駐、pCloud API long-poll、実 upload/download、old monolith archive は実行しません。

## 1. 現行 entrypoint の確認

作業 root:

```sh
cd /Users/takafumi/p-core/dev/pcloud-tools
```

確認コマンド:

```sh
command -v pcloud-manager
ls -l /Users/takafumi/bin/pcloud-manager /Users/takafumi/p-core/bin/pcloud-manager
ls -li /Users/takafumi/.zsh/functions/pcloud-manager /Users/takafumi/p-core/dotfiles/.zsh/functions/pcloud-manager
cmp -s /Users/takafumi/.zsh/functions/pcloud-manager /Users/takafumi/p-core/dotfiles/.zsh/functions/pcloud-manager; echo $?
```

2026-04-26 時点の読み取り確認:

- `command -v pcloud-manager` は `/Users/takafumi/bin/pcloud-manager`
- `/Users/takafumi/bin/pcloud-manager` は `/Users/takafumi/.zsh/functions/pcloud-manager` への symlink
- `/Users/takafumi/p-core/bin/pcloud-manager` も `/Users/takafumi/.zsh/functions/pcloud-manager` への symlink
- `/Users/takafumi/.zsh/functions/pcloud-manager` と `/Users/takafumi/p-core/dotfiles/.zsh/functions/pcloud-manager` は同一 inode の hardlink

停止条件:

- `command -v pcloud-manager` が想定外の path を返す
- `/Users/takafumi/bin/pcloud-manager` または `/Users/takafumi/p-core/bin/pcloud-manager` が想定外の target を指す
- `~/.zsh/functions/pcloud-manager` が dotfiles 側と一致しない
- dotfile 方針に合わせて `~/.zsh/functions/pcloud-manager` を symlink 化する判断が未承認

## 2. 事前 backup

切替直前に、現行 wrapper と検証 report を同じ timestamp directory に保存します。過去に提示した `backup_dir` は予約値ではなく、cutover 直前にこの手順で作成した directory だけを有効な rollback source として扱います。

```sh
set -euo pipefail
backup_dir="/Users/takafumi/p-core/dev/pcloud-tools/.dev-state/cutover-backups/$(date +%Y%m%d-%H%M%S)"
mkdir -p "${backup_dir}"
cp -p /Users/takafumi/p-core/dotfiles/.zsh/functions/pcloud-manager "${backup_dir}/pcloud-manager.current"
cp -p /Users/takafumi/p-core/dev/pcloud-tools/pcloud-manager-dev "${backup_dir}/pcloud-manager-dev.current"
command -v pcloud-manager > "${backup_dir}/command-v.txt"
ls -li /Users/takafumi/.zsh/functions/pcloud-manager /Users/takafumi/p-core/dotfiles/.zsh/functions/pcloud-manager > "${backup_dir}/entrypoint-inodes.txt"
python3 scripts/pcloud-shadow-validation.py --report-path "${backup_dir}/shadow-validation.json"
test -f "${backup_dir}/pcloud-manager.current"
test -f "${backup_dir}/pcloud-manager-dev.current"
test -f "${backup_dir}/shadow-validation.json"
printf '%s\n' "${backup_dir}"
```

backup 停止条件:

- `cp -p` が失敗する
- `pcloud-manager.current` が存在しない
- `pcloud-manager-dev.current` が存在しない
- `shadow-validation.json` が存在しない
- `shadow-validation.json` の top-level `status` が `ok` ではない
- `checks[]` に `status: ok` 以外が含まれる
- report に `temporary workspace guard` / `temporary state dir guard` がない、またはどちらかが `status: ok` ではない
- report の `workspace` が system temporary directory 直下の `pcloud-shadow-validation-* / workspace` ではない
- report の `state_dir` が `workspace/.dev-state/state` と完全一致しない

## 3. validation report gate

cutover candidate として扱える report 条件:

- `schema_version` が `pcloud-tools-shadow-validation.v1`
- top-level `status` が `ok`
- `checks[]` の全 item が `status: ok`
- `pushd dry-run state`、`diffd dry-run state`、`pushd queue not consumed`、`diffd changes not consumed`、`unsafe state dir guard` が含まれる
- `temporary workspace guard` と `temporary state dir guard` が含まれ、どちらも `status: ok`
- `workspace` が system temporary directory 直下の `pcloud-shadow-validation-* / workspace`
- `state_dir` が `workspace/.dev-state/state` と完全一致する

確認例:

```sh
python3 - <<'PY'
import json
import tempfile
from pathlib import Path

path = Path(".dev-state/reports/shadow-validation.json")
payload = json.loads(path.read_text())
checks = payload["checks"]
required = {
    "pushd dry-run state",
    "diffd dry-run state",
    "pushd queue not consumed",
    "diffd changes not consumed",
    "unsafe state dir guard",
    "temporary workspace guard",
    "temporary state dir guard",
}
names = {check["name"] for check in checks}
workspace = Path(payload["workspace"]).resolve()
state_dir = Path(payload["state_dir"]).resolve()
temp_root = Path(tempfile.gettempdir()).resolve()
assert payload["schema_version"] == "pcloud-tools-shadow-validation.v1"
assert payload["status"] == "ok"
assert all(check["status"] == "ok" for check in checks)
assert required <= names
assert workspace.parent.parent == temp_root
assert workspace.parent.name.startswith("pcloud-shadow-validation-")
assert workspace.name == "workspace"
assert state_dir == workspace / ".dev-state" / "state"
print("cutover report gate: ok")
PY
```

## 4. cutover 前の停止条件

次のどれかに該当したら public wrapper は切り替えません。

- validation report gate が失敗した
- `git diff --check` が失敗した
- `python3 -m compileall src scripts/pcloud-shadow-validation.py` が失敗した
- `./.venv/bin/python -m pytest tests/test_cli_invariants.py -q` が失敗した
- 現行 wrapper backup が存在しない
- entrypoint path / symlink / inode の確認結果が手順書と違う
- launchd job の扱いが未承認
- fswatch 常駐、pCloud API long-poll、実 upload/download の有効化条件が未承認

## 5. rollback command

rollback は「public wrapper を現行 monolith に戻す」だけです。remote cleanup、resync、launchd load、実 upload/download は含めません。

`backup_dir` は cutover 直前に作成した directory を指定します。`/Users/takafumi/.zsh` 自体が `/Users/takafumi/p-core/dotfiles/.zsh` への symlink なので、`/Users/takafumi/.zsh/functions/pcloud-manager` を個別 symlink として作り直してはいけません。dotfiles 側の実体を復元すれば、`~/.zsh/functions/pcloud-manager` も同じ実体を参照します。

```sh
set -euo pipefail
backup_dir="/Users/takafumi/p-core/dev/pcloud-tools/.dev-state/cutover-backups/YYYYMMDD-HHMMSS"
test -f "${backup_dir}/pcloud-manager.current"
cp -p "${backup_dir}/pcloud-manager.current" /Users/takafumi/p-core/dotfiles/.zsh/functions/pcloud-manager
chmod 755 /Users/takafumi/p-core/dotfiles/.zsh/functions/pcloud-manager
ln -sfn /Users/takafumi/.zsh/functions/pcloud-manager /Users/takafumi/bin/pcloud-manager
ln -sfn /Users/takafumi/.zsh/functions/pcloud-manager /Users/takafumi/p-core/bin/pcloud-manager
hash -r 2>/dev/null || true
command -v pcloud-manager
pcloud-manager status
```

この rollback 手順では、dotfile 方針に合わせて `/Users/takafumi/p-core/dotfiles/.zsh/functions/pcloud-manager` を正本として復元します。`/Users/takafumi/.zsh` が dotfiles への symlink であるため、`~/.zsh/functions/pcloud-manager` は個別の hardlink ではなく dotfiles 側の同一実体として復元されます。`test -f "${backup_dir}/pcloud-manager.current"` が失敗した場合は、dotfiles 側の上書きへ進みません。

rollback 停止条件:

- backup file がない
- `command -v pcloud-manager` が `/Users/takafumi/bin/pcloud-manager` 以外を返す
- `pcloud-manager status` が wrapper 起動前の shell error で失敗する

## 6. reviewer への報告材料

cutover readiness の報告には次を添えます。

- 更新した docs / 手順書 path
- `command -v pcloud-manager` の結果
- symlink / inode 確認結果
- backup directory の path
- validation report path
- `python3 -m compileall src scripts/pcloud-shadow-validation.py`
- `./.venv/bin/python -m pytest tests/test_cli_invariants.py -q`
- `python3 scripts/pcloud-shadow-validation.py --report-path <path>`
- `git diff --check`

## 7. public wrapper 切替後の soak checklist

public wrapper 切替後は、次の read-only command だけで安定化確認します。

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

確認ポイント:

- JSON output が `pcloud-tools-report.v1` schema を返す
- xbar output の action `bash=` が実行可能な public wrapper を指す
- `status` / `doctor` の warning は既存 live state と設定未作成に由来するものか、wrapper 起動エラーかを分ける
- この soak checklist では launchd 作成/変更/load、fswatch 常駐、pCloud API long-poll、実 upload/download、old monolith archive を実行しない
