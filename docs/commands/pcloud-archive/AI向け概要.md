# pcloud-archive AI向け概要

Last updated: 2026-08-28

## 最初に読む場所

- 利用ガイド: `/Users/takafumi/p-core/dev/#仕様書/pcloud-archive/利用ガイド.md`
- 技術仕様: `/Users/takafumi/p-core/dev/#仕様書/pcloud-archive/技術仕様.md`
- release distribution: `pcloud-tools`
- installed runtime: `${XDG_DATA_HOME:-$HOME/.local/share}/pcloud-tools`
- development implementation: `/Users/takafumi/p-core/dev/pcloud-tools/src/pcloud_tools/pcloud_archive.py`
- public wrapper: `/Users/takafumi/p-core/bin/pcloud-archive`
- tests: `/Users/takafumi/p-core/dev/pcloud-tools/tests/test_pcloud_archive.py`
- man page source: `/Users/takafumi/p-core/dev/pcloud-tools/docs/man/pcloud-archive.1`
- installed man page: `/opt/homebrew/share/man/man1/pcloud-archive.1`

## 役割

`pcloud-archive` は、設定したローカル `source_root` から `pcloud-crypt:` の `remote_root` へrcloneで直接一方向コピーするCLIです。crypt mountは不要で、`pcloud-manager` のcore同期、pushd、diffd、bisyncとは別系統です。

標準手順は `doctor -> diff -> promote --dry-run -> promote --execute -> check --execute` です。ローカル削除はpCloudへ自動伝播しません。

## help / info / doctor契約

- `help`: mount不要、一方向コピー、初回config、標準手順を単独で理解できる内容を出す。
- `help config`: config path、必須キー、TOML例、編集手順を出す。`--init-config <config_path>` はstarter configを新規作成するが、既存fileは上書きせず、`source_root` は空のままにする。
- `help --detail`: man page状態、説明書ディレクトリ、文書一覧と閲覧コマンドを出す。
- `help --ai`: 説明書パスを含むread-only JSON contextを出す。LLMや生成commandは実行しない。
- `info paths`: command、implementation、config、source/remote、state、log、manifest、tombstone、man page、説明書を再発見できるようにする。
- `doctor`: config、source_root、rclone、remote connectivityを診断する。crypt mountとman pageは診断要件にしない。man未採用・未設置はissueにせず `man page status: not used` と表示する。config missing時はTOMLを展開せず `help config` と `help config --init-config <config_path>` を案内し、`info` も同じ案内を出す。

説明書は `PCLOUD_ARCHIVE_DOCS_DIR`、profileの `docs_dir`、command/project位置からの探索、installed wheel内package docsの順に解決する。developmentでは通常`/Users/takafumi/p-core/dev/#仕様書/pcloud-archive/`、releaseではpackage docsを発見する。

## 安全ルール

- mount pathへの `cp` fallbackは作らない。転送は `rclone copy` / `rclone check`。
- `source_root` 未設定・missing、rclone missing、remote unavailableでは転送しない。
- pCloud側削除は `delete-canonical` だけ。成功時にtombstoneを残す。
- tombstone済みlocal pathは再昇格しない。
- 実pCloud検証はsandbox remoteと小さいsampleから始める。

## 変更後の確認

```sh
cd /Users/takafumi/p-core/dev/pcloud-tools
./.venv/bin/python -m pytest tests/test_pcloud_archive.py -q
pcloud-archive --help
pcloud-archive help --detail
pcloud-archive info paths
pcloud-archive doctor
mandoc -T lint /Users/takafumi/p-core/dev/pcloud-tools/docs/man/pcloud-archive.1
man -w pcloud-archive
```
