# Todo

Goal: replace the monolithic `pcloud-manager` with a Python-centered `pcloud-tools` implementation, keep `pcloud-manager` as the public entrypoint, and migrate to `pushd`/`diffd` as the main sync path with `bisync` retained as fallback.

- [ ] Define the config model and migration path from `config.zsh` to `.env` plus a central config module and doctor checks
- [ ] Define the shared status/action contract: human CLI output, versioned JSON output, and xbar-facing command dispatch
- [ ] Port `status`, `status --detail`, `doctor`, `sync status`, `sync progress`, `sync scope`, and `sync check-allowlist` with practical output compatibility
- [ ] Port `sync` execution flows including lock handling, scope guard, listing recovery, autosync controls, and `bisync` fallback commands
- [ ] Port `mount`, `umount`, and `index` into the same CLI surface so they stay on the main migration track
- [ ] Design and implement daemon state for remote changes, `diffid` persistence, notifications, pending-download state, and auto-download on/off visibility
- [ ] Implement `pcloud-pushd` and `pcloud-diffd` plus the CLI actions they expose for xbar and manual operations
- [ ] Run shadow/limited migration validation, document cutover and rollback, switch the public `pcloud-manager` entrypoint, and archive the old monolith
