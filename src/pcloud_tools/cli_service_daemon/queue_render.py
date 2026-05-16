from __future__ import annotations

import argparse

from ..cli_common import print_report
from ..output import CommandReport


def print_pushd_queue_report(report: CommandReport, args: argparse.Namespace) -> None:
    print_report(report, args)


def print_diffd_remote_change_report(report: CommandReport, args: argparse.Namespace) -> None:
    print_report(report, args)
