# pcloud-tools project rules

## Worktree placement

Keep the primary checkout at `/Users/takafumi/p-core/dev/pcloud-tools/`. Place every additional worktree under `/Users/takafumi/p-core/dev/pcloud-tools-dev/<worktree>/` (equivalent to `~/dev/pcloud-tools-dev/<worktree>/`, since `~/dev` is a symlink). Do not create sibling worktrees directly under `~/dev/`. Use a `codex/` branch prefix unless the user requests another name. Do not move the active checkout merely to satisfy the additional-worktree convention.

## Development and production

Use `./pcloud-manager-dev` for development CLI operations and temporary fixtures for archive tests. Production wrappers and LaunchAgents must use the installed release, never development `src`, `.venv`, or `.dev-state` reports. Keep production credentials out of development configuration. Editing this checkout does not deploy a release.

Canonical documentation is `/Users/takafumi/p-core/dev/#仕様書/pcloud-manager/`; read `開発仕様書.md` for the development and deployment contract. Refresh the real-file snapshot at `docs/spec/` when canonical docs change. Machine-local migration backups and validation receipts belong under the production state directory, not in this public repository.
