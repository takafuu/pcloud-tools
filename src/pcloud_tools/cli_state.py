"""Public migration and diagnostic entry point for sync persistence."""

import json
import sqlite3
from .config import load_config
from .sqlite_state import Store, DB_NAME
from .state_migration import migrate, preview


def add_state_parser(subparsers):
    parser = subparsers.add_parser(
        "state", help="Inspect or migrate sync state storage."
    )
    commands = parser.add_subparsers(dest="state_command", required=True)
    for name in ("info", "doctor", "migrate"):
        sub = commands.add_parser(name)
        sub.add_argument("--json", action="store_true")
        if name == "migrate":
            sub.add_argument(
                "--execute",
                action="store_true",
                help="Import under writer cutover barriers; keep verified originals.",
            )


def cmd_state(args, paths):
    config = load_config(paths).config
    try:
        data = preview(config.state_dir)
        if args.state_command == "migrate" and args.execute:
            data = migrate(config.state_dir, config)
        elif args.state_command == "doctor" and data["active"]:
            data.update(Store(config.state_dir / DB_NAME).check())
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False))
        return 1
