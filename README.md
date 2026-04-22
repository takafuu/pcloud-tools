# pcloud-tools

`pcloud-tools` is the new implementation root for the `pcloud-manager` migration.

Current focus:

- establish a Python-native command surface
- keep a development entrypoint isolated from real machine state
- preserve space for compatibility with the existing `pcloud-manager` workflow
- move machine configuration into `.env` plus a central Python config module

Development entrypoint:

```sh
./pcloud-manager-dev status
./pcloud-manager-dev doctor
./pcloud-manager-dev doctor --repair
```

The development wrapper keeps config, state, and logs under `.dev-state/` in this workspace so early implementation work does not touch the live `~/.pcloud` setup.

Config notes:

- the active config file is `.dev-state/config/.env` in development mode
- `.env.example` captures the intended key set for the migrated tool
- `doctor --repair` creates a starter `.env` when one does not exist
