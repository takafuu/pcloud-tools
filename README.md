# pcloud-tools

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

## Resolve a file conflict

The conflict-resolution commands require pcloud-manager 0.2.4 or later. The pinned v0.2.3 installation example below does not include this feature. To install this source revision, build a wheel with `./scripts/build-release-bundle.sh` and follow the [upgrade procedure](#upgrade-and-rollback) using `install.sh --wheel`.

These commands handle regular files with changes queued on both sides. Inspect the current versions before choosing which content to keep:

```sh
pcloud-manager pushd transfer resolve list --json
pcloud-manager pushd transfer resolve preview --path Documents/example.txt --strategy both --json
# Replace PREVIEW_TOKEN with the token returned by the preview:
pcloud-manager pushd transfer resolve apply --path Documents/example.txt --strategy both --token PREVIEW_TOKEN --execute --json
```

| Strategy | Result |
| --- | --- |
| `local` | Keep the local version and release its queued upload. |
| `cloud` | Release the queued download to the original local path. |
| `both` | Keep the local version under a `.local-conflict-<id>` filename, queue that copy for upload, and release the download to the original path. |

Every choice first saves both original versions under the configured state directory's `conflict-resolutions/<id>/`, together with a decision receipt. Use `pcloud-manager info paths` to find the backup location. These private backups are retained until manually removed.

The preview token binds the choice to the inspected file versions and queued records. Detected changes, backup failures, running transfer batches, or unresolved attempts block application. A successful apply updates the queue; it does not mean synchronization has finished. Existing transfer gates and scheduling still apply. Deletes, renames, missing files, symlinks, and unsupported remote hashes need separate review.

On macOS, the companion [pcloud-status xbar plugin](https://github.com/takafuu/pcloud-status) provides the same choices through **要操作 → 競合を解消…**. The menu label and flow described here are for the conflict-resolution implementation shipped with this source revision.

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
