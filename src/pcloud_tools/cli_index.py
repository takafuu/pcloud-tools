from __future__ import annotations

import argparse
import os
import subprocess
import sys

from .cli_common import (
    issue_sort_key as _issue_sort_key,
    report_issues as _report_issues,
    sort_issues as _sort_issues,
)
from .config import ConfigIssue, load_config
from .output import CommandReport, render_report
from .runtime import RuntimePaths


def add_index_parser(subparsers: argparse._SubParsersAction) -> None:
    index_parser = subparsers.add_parser("index", help="Run the configured indexer script.")
    index_parser.add_argument("index_args", nargs=argparse.REMAINDER)


def cmd_index(args: argparse.Namespace, paths: RuntimePaths) -> int:
    load_result = load_config(paths)
    config = load_result.config
    issues = _sort_issues(list(load_result.issues))
    configured_indexer = config.indexer_bin
    indexer = configured_indexer
    details: dict[str, object] = {
        "indexer": str(indexer),
        "vault remote": config.vault_remote,
        "crypt remote": config.crypt_remote,
        "args": list(args.index_args),
    }
    if not indexer.exists():
        issues = _sort_issues(
            issues
            + [
                ConfigIssue(
                    key="PCLOUD_TOOLS_INDEXER_BIN",
                    level="error",
                    message=f"indexer not found: {indexer}",
                )
            ]
        )
        report = CommandReport(
            command="index",
            status="error",
            summary="indexer script is missing",
            details=details,
            issues=_report_issues(issues),
        )
        print(render_report(report))
        return 1

    child_env = dict(os.environ)
    child_env.update(
        {
            "PCLOUD_TOOLS_VAULT_REMOTE": config.vault_remote,
            "PCLOUD_TOOLS_CRYPT_REMOTE": config.crypt_remote,
            "PCLOUD_TOOLS_STATE_DIR": str(config.state_dir),
        }
    )
    command = [sys.executable, str(indexer), *args.index_args]
    result = subprocess.run(command, check=False, env=child_env)
    return result.returncode
