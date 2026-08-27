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

## Requirements

- macOS or Linux
- `curl`, `tar`, and either `sha256sum` or `shasum`
- [rclone](https://rclone.org/install/) with the required pCloud remotes already configured

The installer bootstraps a pinned `uv` and Python runtime when needed. macOS `launchd` integration is optional and is not installed automatically.

## Install

The recommended first installation pins the release version and lets you inspect the installer before running it:

```sh
curl -LfsS https://raw.githubusercontent.com/takafuu/pcloud-tools/v0.1.0/install.sh -o pcloud-tools-install.sh
less pcloud-tools-install.sh
sh pcloud-tools-install.sh --version v0.1.0
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

Release wheels and installer bundles are built and published by the GitHub Actions release workflow when a `v*` tag is pushed.
