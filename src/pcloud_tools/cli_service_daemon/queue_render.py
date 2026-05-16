"""Home for queue-specific human renderers.

Currently a thin pass-through to cli_common.print_report; expand when queue
payloads grow custom formatting needs.
"""

from __future__ import annotations

import argparse

from ..cli_common import print_report
from ..output import CommandReport


def print_pushd_queue_report(report: CommandReport, args: argparse.Namespace) -> None:
    print_report(report, args)


def print_diffd_remote_change_report(report: CommandReport, args: argparse.Namespace) -> None:
    print_report(report, args)
