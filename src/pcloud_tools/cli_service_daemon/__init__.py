from __future__ import annotations

import argparse
import json
import os
import plistlib
import shlex
import signal
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..chat_notify import build_chat_notify_command, chat_notify_status, send_chat_notification
from ..cli_common import (
    action_command,
    entrypoint_command,
    exit_code_for_report,
    has_errors,
    has_warnings,
    issue_sort_key,
    output_format,
    print_report,
    report_issues,
    shell_command,
    sort_issues,
    status_from_issues,
)
from ..config import AppConfig, ConfigIssue, load_config
from ..daemon_state import DaemonState, read_daemon_state, write_diffid
from ..diffd_events import (
    DiffdResponseParseResult,
    diff_changes_to_records,
    parse_diff_response_fixture,
    parse_diff_response_text,
)
from ..download_suppression import (
    clear_download_suppression_record,
    conflict_copy_path,
    download_staging_dir,
    local_fingerprint,
    mark_download_completed,
    mark_download_conflict,
    mark_download_started,
    mark_upload_completed,
    suppression_status_details,
)
from ..gates import GATES, GateSpec, add_gate_review_args, validate_gate
from ..io_utils import atomic_write_json
from .api_poll_render import (
    print_api_long_poll_gate_report as _print_api_long_poll_gate_report,
    print_api_long_poll_run_report as _print_api_long_poll_run_report,
    print_diffd_folder_cache_report as _print_diffd_folder_cache_report,
)
from .fswatch_render import (
    print_fswatch_resident_gate_report as _print_fswatch_resident_gate_report,
    print_fswatch_resident_run_report as _print_fswatch_resident_run_report,
)
from .launchd_render import (
    print_service_launchd_gate_report as _print_service_launchd_gate_report,
    print_service_launchd_plist_report as _print_service_launchd_plist_report,
    print_service_launchd_register_report as _print_service_launchd_register_report,
    print_service_launchd_reload_report as _print_service_launchd_reload_report,
    print_service_launchd_resident_plist_report as _print_service_launchd_resident_plist_report,
    print_service_launchd_review_report as _print_service_launchd_review_report,
    print_service_launchd_status_report as _print_service_launchd_status_report,
    render_service_launchd_gate_human as _render_service_launchd_gate_human,
    render_service_launchd_plist_human as _render_service_launchd_plist_human,
    render_service_launchd_register_human as _render_service_launchd_register_human,
    render_service_launchd_reload_human as _render_service_launchd_reload_human,
    render_service_launchd_resident_plist_human as _render_service_launchd_resident_plist_human,
    render_service_launchd_review_human as _render_service_launchd_review_human,
    render_service_launchd_status_human as _render_service_launchd_status_human,
)
from .transfer_render import (
    print_real_transfer_run_report as _print_real_transfer_run_report,
    print_transfer_check_report as _print_transfer_check_report,
    print_transfer_consume_report as _print_transfer_consume_report,
    print_transfer_preview_report as _print_transfer_preview_report,
    print_validation_matrix_report as _print_validation_matrix_report,
)
from ..output import CommandReport, ReportAction, render_report
from ..rclone_config import load_rclone_pcloud_credentials, rclone_config_path
from ..pushd_events import InvalidPushdEvent, fswatch_events_to_records, parse_fswatch_event_line, parse_fswatch_fixture
from ..runtime import RuntimePaths, action_entrypoint_command, detect_runtime_paths
from ..service_daemon_plan import (
    DiffdPlan,
    PlanRecord,
    PushdPlan,
    append_plan_record,
    append_plan_record_with_policy,
    build_diffd_plan,
    build_diffd_plan_from_records,
    build_pushd_plan,
    build_pushd_plan_from_records,
    clear_plan_records,
    normalize_plan_path,
    record_dry_run_state,
    record_payloads,
    remove_plan_records,
)
from ..service_daemon_state import ServiceDaemonState, read_service_daemon_state
from ..sync_scope import ScopeBaseline, SyncScopeInfo, scope_issues, sync_allowlist_info


@dataclass(frozen=True)
class ServiceDefinition:
    name: str
    summary_name: str
    status_help: str
    preview_help: str


_SERVICES = {
    "pushd": ServiceDefinition(
        name="pushd",
        summary_name="local push daemon",
        status_help="Inspect pcloud-pushd scaffold state.",
        preview_help="Preview the pcloud-pushd scaffold plan.",
    ),
    "diffd": ServiceDefinition(
        name="diffd",
        summary_name="remote diff daemon",
        status_help="Inspect pcloud-diffd scaffold state.",
        preview_help="Preview the pcloud-diffd scaffold plan.",
    ),
}

_SIMPLE_TRANSFER_ACTIONS = {
    "change",
    "create",
    "created",
    "download",
    "modify",
    "modified",
    "sync",
    "update",
    "updated",
    "upload",
}
_MANUAL_REVIEW_ACTION_TOKENS = ("delete", "remove", "rename", "move")
_CONSUME_POLICIES = (
    "remove-on-success-retain-on-failure",
    "retain-all",
    "manual-review",
)
_TIMEOUT_POLICIES = (
    "reuse-fake-rclone-cleanup",
    "manual-review",
)
_DIFFD_API_CATCHUP_GATE_VALUE = "operator-approved-api-catchup-v1"
_DIFFD_API_CHECKPOINT_GATE_VALUE = "operator-approved-api-checkpoint-v1"
_PUSHD_LAUNCHD_PLIST_GATE_VALUE = "operator-approved-pushd-launchd-plist-v1"
_DIFFD_LAUNCHD_PLIST_GATE_VALUE = "operator-approved-diffd-launchd-plist-v1"
_LAUNCHD_RESIDENT_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
_QUEUE_EXECUTOR_START_INTERVAL_SECONDS = 60
_PUBLIC_QUEUE_EXECUTOR_MAX_RECORDS = 10
_REAL_TRANSFER_AUTOMATION_GATE_VALUE = "operator-approved-real-transfer-automation-v1"
_REAL_TRANSFER_AUTOMATION_RUN_GATE_VALUE = "operator-approved-real-transfer-automation-run-v1"
_PUSHD_QUEUE_REMOVE_GATE_VALUE = "operator-approved-pushd-queue-remove-v1"
_PUSHD_QUEUE_PRUNE_EXCLUDED_GATE_VALUE = "operator-approved-pushd-queue-prune-excluded-v1"


def _add_transfer_automation_gate_parser(
    transfer_subparsers: argparse._SubParsersAction,
    *,
    direction: str,
) -> None:
    parser = transfer_subparsers.add_parser(
        "automation-gate",
        help=f"Read-only checklist before any automatic real {direction} queue executor launchd work.",
    )
    parser.add_argument("--report-path", type=Path)
    parser.add_argument("--sample-path")
    parser.add_argument("--confirm-path")
    parser.add_argument("--confirm-direction", choices=("upload", "download"))
    parser.add_argument("--consume-policy", choices=_CONSUME_POLICIES)
    parser.add_argument("--timeout-policy", choices=_TIMEOUT_POLICIES)
    parser.add_argument("--operator-reviewed-dry-run", action="store_true")
    parser.add_argument("--reviewer-approved-real-command", action="store_true")
    parser.add_argument("--reviewer-approved-consume-policy", action="store_true")
    parser.add_argument("--operator-reviewed-real-transfer-gate", action="store_true")
    parser.add_argument("--reviewer-approved-automation-command", action="store_true")
    parser.add_argument("--reviewer-approved-launchd-policy", action="store_true")
    parser.add_argument("--reviewer-approved-rollback-policy", action="store_true")
    parser.add_argument(
        "--start-interval-seconds",
        type=int,
        default=_QUEUE_EXECUTOR_START_INTERVAL_SECONDS,
        help="Proposed public LaunchAgent StartInterval for future automatic real queue executor ticks.",
    )
    parser.add_argument(
        "--max-records",
        type=int,
        default=_PUBLIC_QUEUE_EXECUTOR_MAX_RECORDS,
        help="Maximum transfer records a public queue executor tick may process.",
    )
    parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")


def _add_transfer_automation_run_parser(transfer_subparsers: argparse._SubParsersAction) -> None:
    parser = transfer_subparsers.add_parser(
        "automation-run",
        help="Run one guarded public real-transfer queue executor tick after the automation gates open.",
    )
    parser.add_argument("--report-path", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--consume-on-success", action="store_true")
    parser.add_argument(
        "--max-records",
        type=int,
        default=1,
        help="Maximum planned transfer records to execute in one automation tick.",
    )
    parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")


def _add_automation_review_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--report-path", type=Path)
    parser.add_argument("--sample-path")
    parser.add_argument("--confirm-path")
    parser.add_argument("--confirm-direction", choices=("upload", "download"))
    parser.add_argument("--consume-policy", choices=_CONSUME_POLICIES)
    parser.add_argument("--timeout-policy", choices=_TIMEOUT_POLICIES)
    parser.add_argument("--operator-reviewed-dry-run", action="store_true")
    parser.add_argument("--reviewer-approved-real-command", action="store_true")
    parser.add_argument("--reviewer-approved-consume-policy", action="store_true")
    parser.add_argument("--operator-reviewed-real-transfer-gate", action="store_true")
    parser.add_argument("--reviewer-approved-automation-command", action="store_true")
    parser.add_argument("--reviewer-approved-launchd-policy", action="store_true")
    parser.add_argument("--reviewer-approved-rollback-policy", action="store_true")
    parser.add_argument(
        "--start-interval-seconds",
        type=int,
        default=_QUEUE_EXECUTOR_START_INTERVAL_SECONDS,
        help="Proposed public LaunchAgent StartInterval for future automatic real queue executor ticks.",
    )
    parser.add_argument(
        "--max-records",
        type=int,
        default=_PUBLIC_QUEUE_EXECUTOR_MAX_RECORDS,
        help="Maximum transfer records a public queue executor tick may process.",
    )


def _add_service_launchd_parser(subparsers: argparse._SubParsersAction, service: ServiceDefinition) -> None:
    launchd_parser = subparsers.add_parser(
        "launchd", help="Review the read-only launchd gate before persistent daemon registration."
    )
    launchd_subparsers = launchd_parser.add_subparsers(dest="launchd_command")
    launchd_status_parser = launchd_subparsers.add_parser(
        "status", help="Read the pcloud-pushd/diffd launchd registration status without changing it."
    )
    launchd_status_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    launchd_status_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
    launchd_review_parser = launchd_subparsers.add_parser(
        "review", help="Show the final read-only human review bundle before launchd plist write/registration."
    )
    launchd_review_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    launchd_review_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
    launchd_register_parser = launchd_subparsers.add_parser(
        "register", help="Preview or run the guarded launchd registration after explicit approval."
    )
    launchd_register_parser.add_argument("--report-path", type=Path)
    launchd_register_parser.add_argument("--operator-reviewed-daemon-command", action="store_true")
    launchd_register_parser.add_argument("--reviewer-approved-plist-policy", action="store_true")
    launchd_register_parser.add_argument("--reviewer-approved-launchctl-policy", action="store_true")
    launchd_register_parser.add_argument("--reviewer-approved-rollback-policy", action="store_true")
    launchd_register_parser.add_argument("--execute", action="store_true")
    launchd_register_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    launchd_register_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
    launchd_reload_parser = launchd_subparsers.add_parser(
        "reload", help="Preview or run guarded launchd bootout/bootstrap after operational plist approval."
    )
    launchd_reload_parser.add_argument("--report-path", type=Path)
    launchd_reload_parser.add_argument("--operator-reviewed-resident-plist", action="store_true")
    add_gate_review_args(launchd_reload_parser, GATES[f"{service.name}.launchd.reload"])
    launchd_reload_parser.add_argument("--execute", action="store_true")
    launchd_reload_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    launchd_reload_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
    launchd_resident_plist_parser = launchd_subparsers.add_parser(
        "resident-plist",
        help="Preview or write the guarded operational LaunchAgent plist without launchctl.",
    )
    launchd_resident_plist_parser.add_argument("--report-path", type=Path)
    launchd_resident_plist_gate_name = (
        "pushd.launchd.resident-plist"
        if service.name == "pushd"
        else "diffd.launchd.long-poll-plist"
    )
    add_gate_review_args(launchd_resident_plist_parser, GATES[launchd_resident_plist_gate_name])
    launchd_resident_plist_parser.add_argument(
        "--start-interval-seconds",
        type=int,
        help="For diffd only, add launchd StartInterval for bounded one-shot API polling.",
    )
    launchd_resident_plist_parser.add_argument("--execute", action="store_true")
    launchd_resident_plist_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    launchd_resident_plist_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
    launchd_executor_plist_parser = launchd_subparsers.add_parser(
        "executor-plist",
        help="Preview or write a dev-state queue executor LaunchAgent plist without launchctl.",
    )
    launchd_executor_plist_parser.add_argument(
        "--start-interval-seconds",
        type=int,
        default=_QUEUE_EXECUTOR_START_INTERVAL_SECONDS,
        help="LaunchAgent StartInterval for one dev fake-rclone queue executor tick.",
    )
    launchd_executor_plist_parser.add_argument("--execute", action="store_true")
    launchd_executor_plist_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    launchd_executor_plist_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
    launchd_automation_plist_parser = launchd_subparsers.add_parser(
        "automation-plist",
        help="Preview the future public real-transfer queue executor LaunchAgent plist without writing it.",
    )
    _add_automation_review_args(launchd_automation_plist_parser)
    launchd_automation_plist_parser.add_argument("--operator-reviewed-automation-command", action="store_true")
    launchd_automation_plist_parser.add_argument("--reviewer-approved-automation-environment", action="store_true")
    launchd_automation_plist_parser.add_argument("--reviewer-approved-no-bootstrap", action="store_true")
    launchd_automation_plist_parser.add_argument("--execute", action="store_true")
    launchd_automation_plist_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    launchd_automation_plist_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
    launchd_automation_reload_parser = launchd_subparsers.add_parser(
        "automation-reload",
        help="Preview future public real-transfer queue executor launchd bootout/bootstrap without running it.",
    )
    _add_automation_review_args(launchd_automation_reload_parser)
    launchd_automation_reload_parser.add_argument("--operator-reviewed-automation-plist", action="store_true")
    launchd_automation_reload_parser.add_argument("--reviewer-approved-bootout-bootstrap", action="store_true")
    launchd_automation_reload_parser.add_argument("--execute", action="store_true")
    launchd_automation_reload_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    launchd_automation_reload_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
    launchd_plist_parser = launchd_subparsers.add_parser(
        "plist", help="Preview or write a dev-state LaunchAgent plist for review without running launchctl."
    )
    launchd_plist_parser.add_argument(
        "--execute",
        action="store_true",
        help="Write only the dev .dev-state launchd plist; launchctl is never executed.",
    )
    launchd_plist_parser.add_argument(
        "--public-write",
        action="store_true",
        help="Permit public user LaunchAgent plist write when the dedicated plist gate is open.",
    )
    launchd_plist_parser.add_argument("--operator-reviewed-plist", action="store_true")
    launchd_plist_parser.add_argument("--reviewer-approved-public-target", action="store_true")
    launchd_plist_parser.add_argument("--reviewer-approved-no-bootstrap", action="store_true")
    launchd_plist_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    launchd_plist_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
    launchd_gate_parser = launchd_subparsers.add_parser(
        "gate", help="Read-only checklist before any pcloud-pushd/diffd launchd registration."
    )
    launchd_gate_parser.add_argument("--report-path", type=Path)
    add_gate_review_args(launchd_gate_parser, GATES[f"{service.name}.launchd.gate"])
    launchd_gate_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    launchd_gate_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")


def add_service_daemon_parsers(subparsers: argparse._SubParsersAction) -> None:
    for service in _SERVICES.values():
        _add_service_parser(subparsers, service)


def _add_service_parser(
    subparsers: argparse._SubParsersAction, service: ServiceDefinition
) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(service.name, help=f"Inspect {service.summary_name} scaffold.")
    parser.set_defaults(service_name=service.name)
    service_subparsers = parser.add_subparsers(dest="service_command")

    status_parser = service_subparsers.add_parser("status", help=service.status_help)
    status_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    status_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")

    preview_parser = service_subparsers.add_parser("preview", help=service.preview_help)
    preview_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    preview_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")

    policy_parser = service_subparsers.add_parser(
        "policy", help=f"Inspect the queue-only daemonization policy for {service.name}."
    )
    policy_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    policy_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")

    run_parser = service_subparsers.add_parser("run", help=f"Preview a {service.name} one-shot dry run.")
    run_parser.add_argument(
        "--execute",
        action="store_true",
        help="Record the dry-run result under the dev state dir instead of only previewing it.",
    )
    run_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    run_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")

    gate_parser = service_subparsers.add_parser(
        "gate", help=f"Check the read-only gate before real {service.name} implementation work."
    )
    gate_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    gate_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")

    _add_service_launchd_parser(service_subparsers, service)

    if service.name == "pushd":
        fswatch_parser = service_subparsers.add_parser(
            "fswatch", help="Preview pushd fswatch fixture events without starting fswatch."
        )
        fswatch_subparsers = fswatch_parser.add_subparsers(dest="fswatch_command")
        fswatch_preview_parser = fswatch_subparsers.add_parser(
            "preview", help="Parse a fixture and preview the resulting upload plan."
        )
        fswatch_preview_parser.add_argument("--fixture", required=True, type=Path)
        fswatch_preview_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        fswatch_preview_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
        fswatch_probe_parser = fswatch_subparsers.add_parser(
            "probe", help="Preview the one-shot fswatch probe command without running it."
        )
        fswatch_probe_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        fswatch_probe_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
        fswatch_resident_gate_parser = fswatch_subparsers.add_parser(
            "resident-gate", help="Read-only checklist before starting a resident fswatch watcher."
        )
        fswatch_resident_gate_parser.add_argument("--report-path", type=Path)
        add_gate_review_args(fswatch_resident_gate_parser, GATES["pushd.fswatch.resident"])
        fswatch_resident_gate_parser.add_argument(
            "--json", action="store_true", help="Emit structured JSON output."
        )
        fswatch_resident_gate_parser.add_argument(
            "--xbar", action="store_true", help="Emit xbar menu output."
        )
        fswatch_resident_run_parser = fswatch_subparsers.add_parser(
            "resident-run", help="Run the foreground fswatch resident loop after the dedicated gate opens."
        )
        fswatch_resident_run_parser.add_argument("--report-path", type=Path)
        add_gate_review_args(fswatch_resident_run_parser, GATES["pushd.fswatch.resident"])
        fswatch_resident_run_parser.add_argument("--max-events", type=int)
        fswatch_resident_run_parser.add_argument("--execute", action="store_true")
        fswatch_resident_run_parser.add_argument(
            "--json", action="store_true", help="Emit structured JSON output."
        )
        fswatch_resident_run_parser.add_argument(
            "--xbar", action="store_true", help="Emit xbar menu output."
        )
        transfer_parser = service_subparsers.add_parser(
            "transfer", help="Preview upload executor commands without running transfers."
        )
        transfer_subparsers = transfer_parser.add_subparsers(dest="transfer_command")
        transfer_preview_parser = transfer_subparsers.add_parser(
            "preview", help="Emit planned upload commands from the current pushd plan."
        )
        transfer_preview_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        transfer_preview_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
        transfer_matrix_parser = transfer_subparsers.add_parser(
            "validation-matrix", help="Show read-only real upload validation matrix command examples."
        )
        transfer_matrix_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        transfer_matrix_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
        transfer_check_parser = transfer_subparsers.add_parser(
            "check", help="Read-only checklist for the real upload transfer gate."
        )
        transfer_check_parser.add_argument(
            "--report-path",
            type=Path,
            help="Optional saved shadow validation report to inspect without running validation.",
        )
        transfer_check_parser.add_argument(
            "--sample-path",
            help="Relative allowlisted path to use in the displayed dev-state sample setup command.",
        )
        transfer_check_parser.add_argument(
            "--confirm-path",
            help="Operator-confirmed relative path for the first real upload review.",
        )
        transfer_check_parser.add_argument(
            "--confirm-direction",
            choices=("upload", "download"),
            help="Operator-confirmed direction for the first real transfer review.",
        )
        transfer_check_parser.add_argument(
            "--consume-policy",
            choices=_CONSUME_POLICIES,
            help="Reviewer-approved record consumption policy for the first real transfer review.",
        )
        transfer_check_parser.add_argument(
            "--timeout-policy",
            choices=_TIMEOUT_POLICIES,
            help="Reviewer-approved timeout/process cleanup policy for the first real transfer review.",
        )
        transfer_check_parser.add_argument(
            "--final-review",
            action="store_true",
            help="Show the final read-only dry-run review before opening a separate real-transfer gate.",
        )
        transfer_check_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        transfer_check_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
        transfer_real_gate_parser = transfer_subparsers.add_parser(
            "real-gate",
            help="Read-only scaffold for the separate real upload execution gate.",
        )
        transfer_real_gate_parser.add_argument(
            "--report-path",
            type=Path,
            help="Optional saved shadow validation report to inspect without running validation.",
        )
        transfer_real_gate_parser.add_argument(
            "--sample-path",
            help="Relative allowlisted path to use in the displayed dev-state sample setup command.",
        )
        transfer_real_gate_parser.add_argument(
            "--confirm-path",
            help="Operator-confirmed relative path for the first real upload review.",
        )
        transfer_real_gate_parser.add_argument(
            "--confirm-direction",
            choices=("upload", "download"),
            help="Operator-confirmed direction for the first real transfer review.",
        )
        transfer_real_gate_parser.add_argument(
            "--consume-policy",
            choices=_CONSUME_POLICIES,
            help="Reviewer-approved record consumption policy for the first real transfer review.",
        )
        transfer_real_gate_parser.add_argument(
            "--timeout-policy",
            choices=_TIMEOUT_POLICIES,
            help="Reviewer-approved timeout/process cleanup policy for the first real transfer review.",
        )
        transfer_real_gate_parser.add_argument(
            "--operator-reviewed-dry-run",
            action="store_true",
            help="Record that the operator reviewed the displayed dry-run command.",
        )
        transfer_real_gate_parser.add_argument(
            "--reviewer-approved-real-command",
            action="store_true",
            help="Record reviewer approval for the exact real transfer command.",
        )
        transfer_real_gate_parser.add_argument(
            "--reviewer-approved-consume-policy",
            action="store_true",
            help="Record reviewer approval for the post-success consume policy.",
        )
        transfer_real_gate_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        transfer_real_gate_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
        _add_transfer_automation_gate_parser(transfer_subparsers, direction="upload")
        _add_transfer_automation_run_parser(transfer_subparsers)
        transfer_real_run_parser = transfer_subparsers.add_parser(
            "real-run",
            help="Run guarded real upload execution only after the real-transfer gate is open.",
        )
        transfer_real_run_parser.add_argument("--report-path", type=Path)
        transfer_real_run_parser.add_argument("--sample-path")
        transfer_real_run_parser.add_argument("--confirm-path")
        transfer_real_run_parser.add_argument("--confirm-direction", choices=("upload", "download"))
        transfer_real_run_parser.add_argument("--consume-policy", choices=_CONSUME_POLICIES)
        transfer_real_run_parser.add_argument("--timeout-policy", choices=_TIMEOUT_POLICIES)
        transfer_real_run_parser.add_argument("--operator-reviewed-dry-run", action="store_true")
        transfer_real_run_parser.add_argument("--reviewer-approved-real-command", action="store_true")
        transfer_real_run_parser.add_argument("--reviewer-approved-consume-policy", action="store_true")
        transfer_real_run_parser.add_argument("--execute", action="store_true")
        transfer_real_run_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        transfer_real_run_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
        transfer_run_parser = transfer_subparsers.add_parser(
            "run", help="Run dev-mode fake-rclone upload executor commands behind the transfer gate."
        )
        transfer_run_parser.add_argument(
            "--execute",
            action="store_true",
            help="Execute only when the dev fake-rclone transfer gate and dev state guard are open.",
        )
        transfer_run_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        transfer_run_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
        transfer_executor_parser = transfer_subparsers.add_parser(
            "executor-run",
            help="Run one queue executor tick with fake-rclone and optional dev-state consume.",
        )
        transfer_executor_parser.add_argument("--execute", action="store_true")
        transfer_executor_parser.add_argument("--consume-on-success", action="store_true")
        transfer_executor_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        transfer_executor_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
        transfer_consume_parser = transfer_subparsers.add_parser(
            "consume", help="Preview queue consumption after successful transfer records."
        )
        transfer_consume_subparsers = transfer_consume_parser.add_subparsers(dest="consume_command")
        transfer_consume_preview_parser = transfer_consume_subparsers.add_parser(
            "preview", help="Read-only preview of queue records that would be removed."
        )
        transfer_consume_preview_parser.add_argument(
            "--json", action="store_true", help="Emit structured JSON output."
        )
        transfer_consume_preview_parser.add_argument(
            "--xbar", action="store_true", help="Emit xbar menu output."
        )
        transfer_consume_run_parser = transfer_consume_subparsers.add_parser(
            "run", help="Remove matched queue records only when --execute is provided."
        )
        transfer_consume_run_parser.add_argument(
            "--execute",
            action="store_true",
            help="Remove matched queue records under the dev state dir.",
        )
        transfer_consume_run_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        transfer_consume_run_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")

        queue_parser = service_subparsers.add_parser("queue", help="Preview or update pushd queue state.")
        queue_subparsers = queue_parser.add_subparsers(dest="queue_command")
        queue_add_parser = queue_subparsers.add_parser("add")
        queue_add_parser.add_argument("path")
        queue_add_parser.add_argument("--action", default="upload")
        queue_add_parser.add_argument("--reason", default="manual")
        queue_add_parser.add_argument(
            "--execute",
            action="store_true",
            help="Append the queue record under the dev state dir instead of only previewing it.",
        )
        queue_add_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        queue_clear_parser = queue_subparsers.add_parser("clear")
        queue_clear_parser.add_argument(
            "--execute",
            action="store_true",
            help="Clear the queue file under the dev state dir instead of only previewing it.",
        )
        queue_clear_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        queue_remove_parser = queue_subparsers.add_parser("remove")
        queue_remove_parser.add_argument("path")
        queue_remove_parser.add_argument(
            "--execute",
            action="store_true",
            help="Remove matching queue records under the dev state dir instead of only previewing it.",
        )
        queue_remove_parser.add_argument("--reviewer-approved-queue-record-removal", action="store_true")
        queue_remove_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        queue_prune_parser = queue_subparsers.add_parser(
            "prune-excluded",
            help="Preview or remove queue records that the current plan excludes.",
        )
        queue_prune_parser.add_argument(
            "--execute",
            action="store_true",
            help="Remove excluded queue records under the active state dir after the relevant gate opens.",
        )
        queue_prune_parser.add_argument("--reviewer-approved-excluded-record-cleanup", action="store_true")
        queue_prune_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        queue_prune_missing_parser = queue_subparsers.add_parser(
            "prune-missing-local",
            help="Preview or remove upload records whose local source file no longer exists.",
        )
        queue_prune_missing_parser.add_argument("--execute", action="store_true")
        queue_prune_missing_parser.add_argument("--reviewer-approved-missing-local-cleanup", action="store_true")
        queue_prune_missing_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        queue_prune_missing_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")

    if service.name == "diffd":
        diff_parser = service_subparsers.add_parser(
            "diff", help="Preview diffd pCloud diff fixture responses without calling the API."
        )
        diff_subparsers = diff_parser.add_subparsers(dest="diff_command")
        diff_preview_parser = diff_subparsers.add_parser(
            "preview", help="Parse a fixture and preview the resulting download plan."
        )
        diff_preview_parser.add_argument("--fixture", required=True, type=Path)
        diff_preview_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        diff_preview_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")

        folder_cache_parser = service_subparsers.add_parser(
            "folder-cache", help="Preview or update diffd pCloud folder metadata cache under the dev state dir."
        )
        folder_cache_subparsers = folder_cache_parser.add_subparsers(dest="folder_cache_command")
        folder_cache_status_parser = folder_cache_subparsers.add_parser(
            "status", help="Inspect cached pCloud folder id to path mappings."
        )
        folder_cache_status_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        folder_cache_add_parser = folder_cache_subparsers.add_parser(
            "add", help="Add one folder id to path mapping, preview-only unless --execute is provided."
        )
        folder_cache_add_parser.add_argument("folder_id")
        folder_cache_add_parser.add_argument("path")
        folder_cache_add_parser.add_argument(
            "--execute",
            action="store_true",
            help="Write the folder cache mapping under the dev state dir.",
        )
        folder_cache_add_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        folder_cache_remove_parser = folder_cache_subparsers.add_parser(
            "remove", help="Remove one folder id mapping, preview-only unless --execute is provided."
        )
        folder_cache_remove_parser.add_argument("folder_id")
        folder_cache_remove_parser.add_argument(
            "--execute",
            action="store_true",
            help="Write the folder cache removal under the dev state dir.",
        )
        folder_cache_remove_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        folder_cache_clear_parser = folder_cache_subparsers.add_parser(
            "clear", help="Clear all folder cache mappings, preview-only unless --execute is provided."
        )
        folder_cache_clear_parser.add_argument(
            "--execute",
            action="store_true",
            help="Clear the folder cache file under the dev state dir.",
        )
        folder_cache_clear_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")

        api_poll_parser = service_subparsers.add_parser(
            "api-poll", help="Preview a one-shot pCloud API poll without calling the API."
        )
        api_poll_subparsers = api_poll_parser.add_subparsers(dest="api_poll_command")
        api_poll_preview_parser = api_poll_subparsers.add_parser(
            "preview", help="Report the intended one-shot API poll request shape."
        )
        api_poll_preview_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        api_poll_preview_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
        api_poll_long_poll_gate_parser = api_poll_subparsers.add_parser(
            "long-poll-gate", help="Read-only checklist before enabling diffd pCloud API long-poll."
        )
        api_poll_long_poll_gate_parser.add_argument("--report-path", type=Path)
        add_gate_review_args(api_poll_long_poll_gate_parser, GATES["diffd.api.long-poll"])
        api_poll_long_poll_gate_parser.add_argument(
            "--json", action="store_true", help="Emit structured JSON output."
        )
        api_poll_long_poll_gate_parser.add_argument(
            "--xbar", action="store_true", help="Emit xbar menu output."
        )
        api_poll_long_poll_run_parser = api_poll_subparsers.add_parser(
            "long-poll-run",
            help="Run guarded fixture-backed API long-poll processing after the dedicated gate opens.",
        )
        api_poll_long_poll_run_parser.add_argument("--report-path", type=Path)
        add_gate_review_args(api_poll_long_poll_run_parser, GATES["diffd.api.long-poll"])
        api_poll_long_poll_run_parser.add_argument("--fixture", type=Path)
        api_poll_long_poll_run_parser.add_argument(
            "--live-api",
            action="store_true",
            help="Call the live pCloud /diff API after the dedicated gate opens.",
        )
        api_poll_long_poll_run_parser.add_argument(
            "--block",
            action="store_true",
            help="Pass block=1 to the live pCloud /diff request.",
        )
        api_poll_long_poll_run_parser.add_argument("--max-iterations", type=int)
        api_poll_long_poll_run_parser.add_argument("--reviewer-approved-catchup-policy", action="store_true")
        api_poll_long_poll_run_parser.add_argument("--execute", action="store_true")
        api_poll_long_poll_run_parser.add_argument(
            "--json", action="store_true", help="Emit structured JSON output."
        )
        api_poll_long_poll_run_parser.add_argument(
            "--xbar", action="store_true", help="Emit xbar menu output."
        )
        api_poll_checkpoint_parser = api_poll_subparsers.add_parser(
            "checkpoint",
            help="Set the diffd cursor to the current pCloud diffid after the dedicated gate opens.",
        )
        api_poll_checkpoint_parser.add_argument("--report-path", type=Path)
        api_poll_checkpoint_parser.add_argument("--operator-reviewed-checkpoint", action="store_true")
        api_poll_checkpoint_parser.add_argument("--reviewer-approved-checkpoint-policy", action="store_true")
        api_poll_checkpoint_parser.add_argument("--execute", action="store_true")
        api_poll_checkpoint_parser.add_argument(
            "--json", action="store_true", help="Emit structured JSON output."
        )
        api_poll_checkpoint_parser.add_argument(
            "--xbar", action="store_true", help="Emit xbar menu output."
        )

        transfer_parser = service_subparsers.add_parser(
            "transfer", help="Preview download executor commands without running transfers."
        )
        transfer_subparsers = transfer_parser.add_subparsers(dest="transfer_command")
        transfer_preview_parser = transfer_subparsers.add_parser(
            "preview", help="Emit planned download commands from the current diffd plan."
        )
        transfer_preview_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        transfer_preview_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
        transfer_matrix_parser = transfer_subparsers.add_parser(
            "validation-matrix", help="Show read-only real download validation matrix command examples."
        )
        transfer_matrix_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        transfer_matrix_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
        transfer_check_parser = transfer_subparsers.add_parser(
            "check", help="Read-only checklist for the real download transfer gate."
        )
        transfer_check_parser.add_argument(
            "--report-path",
            type=Path,
            help="Optional saved shadow validation report to inspect without running validation.",
        )
        transfer_check_parser.add_argument(
            "--sample-path",
            help="Relative allowlisted path to use in the displayed dev-state sample setup command.",
        )
        transfer_check_parser.add_argument(
            "--confirm-path",
            help="Operator-confirmed relative path for the first real download review.",
        )
        transfer_check_parser.add_argument(
            "--confirm-direction",
            choices=("upload", "download"),
            help="Operator-confirmed direction for the first real transfer review.",
        )
        transfer_check_parser.add_argument(
            "--consume-policy",
            choices=_CONSUME_POLICIES,
            help="Reviewer-approved record consumption policy for the first real transfer review.",
        )
        transfer_check_parser.add_argument(
            "--timeout-policy",
            choices=_TIMEOUT_POLICIES,
            help="Reviewer-approved timeout/process cleanup policy for the first real transfer review.",
        )
        transfer_check_parser.add_argument(
            "--final-review",
            action="store_true",
            help="Show the final read-only dry-run review before opening a separate real-transfer gate.",
        )
        transfer_check_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        transfer_check_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
        transfer_real_gate_parser = transfer_subparsers.add_parser(
            "real-gate",
            help="Read-only scaffold for the separate real download execution gate.",
        )
        transfer_real_gate_parser.add_argument(
            "--report-path",
            type=Path,
            help="Optional saved shadow validation report to inspect without running validation.",
        )
        transfer_real_gate_parser.add_argument(
            "--sample-path",
            help="Relative allowlisted path to use in the displayed dev-state sample setup command.",
        )
        transfer_real_gate_parser.add_argument(
            "--confirm-path",
            help="Operator-confirmed relative path for the first real download review.",
        )
        transfer_real_gate_parser.add_argument(
            "--confirm-direction",
            choices=("upload", "download"),
            help="Operator-confirmed direction for the first real transfer review.",
        )
        transfer_real_gate_parser.add_argument(
            "--consume-policy",
            choices=_CONSUME_POLICIES,
            help="Reviewer-approved record consumption policy for the first real transfer review.",
        )
        transfer_real_gate_parser.add_argument(
            "--timeout-policy",
            choices=_TIMEOUT_POLICIES,
            help="Reviewer-approved timeout/process cleanup policy for the first real transfer review.",
        )
        transfer_real_gate_parser.add_argument(
            "--operator-reviewed-dry-run",
            action="store_true",
            help="Record that the operator reviewed the displayed dry-run command.",
        )
        transfer_real_gate_parser.add_argument(
            "--reviewer-approved-real-command",
            action="store_true",
            help="Record reviewer approval for the exact real transfer command.",
        )
        transfer_real_gate_parser.add_argument(
            "--reviewer-approved-consume-policy",
            action="store_true",
            help="Record reviewer approval for the post-success consume policy.",
        )
        transfer_real_gate_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        transfer_real_gate_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
        _add_transfer_automation_gate_parser(transfer_subparsers, direction="download")
        _add_transfer_automation_run_parser(transfer_subparsers)
        transfer_real_run_parser = transfer_subparsers.add_parser(
            "real-run",
            help="Run guarded real download execution only after the real-transfer gate is open.",
        )
        transfer_real_run_parser.add_argument("--report-path", type=Path)
        transfer_real_run_parser.add_argument("--sample-path")
        transfer_real_run_parser.add_argument("--confirm-path")
        transfer_real_run_parser.add_argument("--confirm-direction", choices=("upload", "download"))
        transfer_real_run_parser.add_argument("--consume-policy", choices=_CONSUME_POLICIES)
        transfer_real_run_parser.add_argument("--timeout-policy", choices=_TIMEOUT_POLICIES)
        transfer_real_run_parser.add_argument("--operator-reviewed-dry-run", action="store_true")
        transfer_real_run_parser.add_argument("--reviewer-approved-real-command", action="store_true")
        transfer_real_run_parser.add_argument("--reviewer-approved-consume-policy", action="store_true")
        transfer_real_run_parser.add_argument("--execute", action="store_true")
        transfer_real_run_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        transfer_real_run_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
        transfer_run_parser = transfer_subparsers.add_parser(
            "run", help="Run dev-mode fake-rclone download executor commands behind the transfer gate."
        )
        transfer_run_parser.add_argument(
            "--execute",
            action="store_true",
            help="Execute only when the dev fake-rclone transfer gate and dev state guard are open.",
        )
        transfer_run_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        transfer_run_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
        transfer_executor_parser = transfer_subparsers.add_parser(
            "executor-run",
            help="Run one queue executor tick with fake-rclone and optional dev-state consume.",
        )
        transfer_executor_parser.add_argument("--execute", action="store_true")
        transfer_executor_parser.add_argument("--consume-on-success", action="store_true")
        transfer_executor_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        transfer_executor_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
        transfer_consume_parser = transfer_subparsers.add_parser(
            "consume", help="Preview remote-change consumption after successful transfer records."
        )
        transfer_consume_subparsers = transfer_consume_parser.add_subparsers(dest="consume_command")
        transfer_consume_preview_parser = transfer_consume_subparsers.add_parser(
            "preview", help="Read-only preview of remote-change records that would be removed."
        )
        transfer_consume_preview_parser.add_argument(
            "--json", action="store_true", help="Emit structured JSON output."
        )
        transfer_consume_preview_parser.add_argument(
            "--xbar", action="store_true", help="Emit xbar menu output."
        )
        transfer_consume_run_parser = transfer_consume_subparsers.add_parser(
            "run", help="Remove matched remote-change records only when --execute is provided."
        )
        transfer_consume_run_parser.add_argument(
            "--execute",
            action="store_true",
            help="Remove matched remote-change records under the dev state dir.",
        )
        transfer_consume_run_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        transfer_consume_run_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")

        remote_parser = service_subparsers.add_parser(
            "remote-change", help="Preview or update diffd remote change state."
        )
        remote_subparsers = remote_parser.add_subparsers(dest="remote_change_command")
        remote_add_parser = remote_subparsers.add_parser("add")
        remote_add_parser.add_argument("path")
        remote_add_parser.add_argument("--action", default="download")
        remote_add_parser.add_argument("--reason", default="manual")
        remote_add_parser.add_argument(
            "--execute",
            action="store_true",
            help="Append the remote-change record under the dev state dir instead of only previewing it.",
        )
        remote_add_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        remote_clear_parser = remote_subparsers.add_parser("clear")
        remote_clear_parser.add_argument(
            "--execute",
            action="store_true",
            help="Clear the remote-change file under the dev state dir instead of only previewing it.",
        )
        remote_clear_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        remote_remove_parser = remote_subparsers.add_parser("remove")
        remote_remove_parser.add_argument("path")
        remote_remove_parser.add_argument(
            "--execute",
            action="store_true",
            help="Remove matching remote-change records under the dev state dir instead of only previewing it.",
        )
        remote_remove_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    return parser


def cmd_service_daemon(args: argparse.Namespace, paths: RuntimePaths) -> int | None:
    service = _SERVICES[getattr(args, "service_name")]
    if args.service_command == "status":
        return cmd_service_status(args, paths, service)
    if args.service_command == "preview":
        return cmd_service_preview(args, paths, service)
    if args.service_command == "policy":
        return cmd_service_policy(args, paths, service)
    if args.service_command == "run":
        return cmd_service_run(args, paths, service)
    if args.service_command == "gate":
        return cmd_service_gate(args, paths, service)
    if args.service_command == "launchd":
        return cmd_service_launchd(args, paths, service)
    if service.name == "pushd" and args.service_command == "fswatch":
        return cmd_pushd_fswatch(args, paths)
    if service.name == "diffd" and args.service_command == "diff":
        return cmd_diffd_diff(args, paths)
    if service.name == "diffd" and args.service_command == "folder-cache":
        return cmd_diffd_folder_cache(args, paths)
    if service.name == "diffd" and args.service_command == "api-poll":
        return cmd_diffd_api_poll(args, paths)
    if service.name in {"pushd", "diffd"} and args.service_command == "transfer":
        return cmd_service_transfer(args, paths, service)
    if service.name == "pushd" and args.service_command == "queue":
        return cmd_pushd_queue(args, paths)
    if service.name == "diffd" and args.service_command == "remote-change":
        return cmd_diffd_remote_change(args, paths)
    return None


def _xbar_escape(value: object) -> str:
    return str(value).replace("\n", " ").replace("|", "/")


def _xbar_status_label(status: str) -> str:
    if status == "ok":
        return "OK"
    if status == "warning":
        return "WARN"
    return "ERR"


def _xbar_action(action: ReportAction) -> str:
    fields = [
        f"bash={shlex.quote(action.command[0])}",
        f"terminal={'true' if action.terminal else 'false'}",
        f"refresh={'true' if action.refresh else 'false'}",
    ]
    for index, arg in enumerate(action.command[1:], start=1):
        fields.append(f"param{index}={shlex.quote(arg)}")
    return f"{_xbar_escape(action.label)} | {' '.join(fields)}"


def _service_status_xbar_action_ids(service: ServiceDefinition) -> set[str]:
    common = {
        f"{service.name}.status.refresh",
        f"{service.name}.preview",
        f"{service.name}.launchd.status",
        f"{service.name}.launchd.gate",
        f"{service.name}.transfer.check",
    }
    if service.name == "pushd":
        common.add("pushd.fswatch.resident-gate")
        common.add("pushd.queue.prune-missing-local")
    else:
        common.add("diffd.api-poll.long-poll-gate")
    return common


def _render_service_status_xbar(report: CommandReport, service: ServiceDefinition) -> str:
    details = report.details
    conflict_line = (
        f"conflicts={details.get('download conflict count', '-')}; "
        f"latest={details.get('download latest conflict', '-')}"
    )
    if service.name == "pushd":
        plan_line = (
            f"plan: uploads={details.get('planned uploads', '-')}; "
            f"missing={details.get('missing local upload records', '-')}; "
            f"manual={details.get('manual review transfer records', '-')}; "
            f"queued={details.get('pending queue items', '-')}"
        )
        last_run_line = (
            f"last resident: {details.get('last resident run status', '-')}; "
            f"{details.get('last resident run summary', '-')}"
        )
        service_gate = f"resident={details.get('resident gate', '-')}"
    else:
        plan_line = (
            f"plan: downloads={details.get('planned downloads', '-')}; "
            f"manual={details.get('manual review transfer records', '-')}; "
            f"remote={details.get('remote changes', '-')}; diffid={details.get('daemon diffid', '-')}"
        )
        last_run_line = (
            f"last api poll: {details.get('last api poll run status', '-')}; "
            f"{details.get('last api poll run summary', '-')}"
        )
        service_gate = f"long-poll={details.get('long-poll gate', '-')}"
    allowed_actions = _service_status_xbar_action_ids(service)
    notify_line = f"notify: {details.get('chat notify mode', '-')}"
    if details.get("chat notify dedupe seconds", "-") != "-":
        notify_line += f"; dedupe={details.get('chat notify dedupe seconds', '-')}s"
    lines = [
        f"pCloud {_xbar_status_label(report.status)}",
        "---",
        _xbar_escape(report.summary),
        _xbar_escape(plan_line),
        _xbar_escape(last_run_line),
        _xbar_escape(
            f"launchd: {details.get('launchd registration', '-')}; loaded={details.get('launchd loaded', '-')}"
        ),
        _xbar_escape(
            f"gates: real={details.get('real-operation gate', '-')}; {service_gate}; "
            f"transfer={details.get('transfer gate', '-')}"
        ),
        _xbar_escape(f"download journal: {conflict_line}"),
        _xbar_escape(f"missing local uploads: {details.get('missing local upload records', '-')}")
        if service.name == "pushd"
        else _xbar_escape("missing local uploads: -"),
        _xbar_escape(f"upload echo: suppressed={details.get('upload origin completed', '-')}"),
        _xbar_escape(notify_line),
    ]
    if service.name == "pushd":
        missing_records = details.get("missing local upload record details", [])
        if isinstance(missing_records, list) and missing_records:
            lines.append("---")
            lines.append(_xbar_escape("Missing local upload records"))
            for record in missing_records[:5]:
                if not isinstance(record, dict):
                    continue
                reason = record.get("reason", "-")
                lines.append(_xbar_escape(f"{record.get('path', '-')} ({reason})"))
            if len(missing_records) > 5:
                lines.append(_xbar_escape(f"... and {len(missing_records) - 5} more"))
    if report.issues:
        lines.append("---")
        for issue in report.issues:
            lines.append(f"{issue.level}: {_xbar_escape(issue.message)}")
    actions = [action for action in report.actions if action.id in allowed_actions]
    if actions:
        lines.append("---")
        for action in actions:
            lines.append(_xbar_action(action))
    return "\n".join(lines)


def _real_gate_args(args: argparse.Namespace, *, allow_confirmed_subset: bool = False) -> argparse.Namespace:
    values = vars(args).copy()
    values["final_review"] = True
    values["allow_confirmed_subset"] = allow_confirmed_subset
    return argparse.Namespace(**values)


def _service_actions(paths: RuntimePaths, service: ServiceDefinition) -> list[ReportAction]:
    actions = [
        ReportAction(
            id=f"{service.name}.status.refresh",
            label=f"Refresh {service.name} state",
            command=action_command(paths, f"{service.name}.status.refresh"),
        ),
        ReportAction(
            id=f"{service.name}.preview",
            label=f"Preview {service.name} plan",
            command=action_command(paths, f"{service.name}.preview"),
            terminal=True,
            refresh=False,
        ),
        ReportAction(
            id=f"{service.name}.policy",
            label=f"Inspect {service.name} daemon policy",
            command=action_command(paths, f"{service.name}.policy"),
            terminal=True,
            refresh=False,
        ),
        ReportAction(
            id=f"{service.name}.run.preview",
            label=f"Preview {service.name} dry run",
            command=action_command(paths, f"{service.name}.run.preview"),
            terminal=True,
            refresh=False,
        ),
        ReportAction(
            id=f"{service.name}.gate",
            label=f"Check {service.name} real gate",
            command=action_command(paths, f"{service.name}.gate"),
            terminal=True,
            refresh=False,
        ),
        ReportAction(
            id=f"{service.name}.launchd.gate",
            label=f"Check {service.name} launchd gate",
            command=action_command(paths, f"{service.name}.launchd.gate"),
            terminal=True,
            refresh=False,
        ),
        ReportAction(
            id=f"{service.name}.launchd.status",
            label=f"Inspect {service.name} launchd status",
            command=action_command(paths, f"{service.name}.launchd.status"),
            terminal=True,
            refresh=True,
        ),
        ReportAction(
            id=f"{service.name}.launchd.review",
            label=f"Review {service.name} launchd plist and foreground command",
            command=action_command(paths, f"{service.name}.launchd.review"),
            terminal=True,
            refresh=False,
        ),
        ReportAction(
            id=f"{service.name}.launchd.register.preview",
            label=f"Preview {service.name} launchd registration",
            command=action_command(paths, f"{service.name}.launchd.register.preview"),
            terminal=True,
            refresh=False,
        ),
        ReportAction(
            id=f"{service.name}.launchd.reload.preview",
            label=f"Preview {service.name} launchd reload",
            command=action_command(paths, f"{service.name}.launchd.reload.preview"),
            terminal=True,
            refresh=False,
        ),
        ReportAction(
            id=f"{service.name}.launchd.resident-plist.preview",
            label=f"Preview {service.name} operational launchd plist",
            command=action_command(paths, f"{service.name}.launchd.resident-plist.preview"),
            terminal=True,
            refresh=False,
        ),
        ReportAction(
            id=f"{service.name}.launchd.executor-plist.preview",
            label=f"Preview {service.name} queue executor launchd plist",
            command=action_command(paths, f"{service.name}.launchd.executor-plist.preview"),
            terminal=True,
            refresh=False,
        ),
        ReportAction(
            id=f"{service.name}.launchd.automation-plist.preview",
            label=f"Preview {service.name} real transfer automation launchd plist",
            command=action_command(paths, f"{service.name}.launchd.automation-plist.preview"),
            terminal=True,
            refresh=False,
        ),
        ReportAction(
            id=f"{service.name}.launchd.automation-reload.preview",
            label=f"Preview {service.name} real transfer automation launchd reload",
            command=action_command(paths, f"{service.name}.launchd.automation-reload.preview"),
            terminal=True,
            refresh=False,
        ),
        ReportAction(
            id=f"{service.name}.launchd.plist.preview",
            label=f"Preview {service.name} launchd plist",
            command=action_command(paths, f"{service.name}.launchd.plist.preview"),
            terminal=True,
            refresh=False,
        ),
        ReportAction(
            id=f"{service.name}.transfer.preview",
            label=f"Preview {service.name} transfer commands",
            command=action_command(paths, f"{service.name}.transfer.preview"),
            terminal=True,
            refresh=False,
        ),
        ReportAction(
            id=f"{service.name}.transfer.validation-matrix",
            label=f"Review {service.name} transfer validation matrix",
            command=action_command(paths, f"{service.name}.transfer.validation-matrix"),
            terminal=True,
            refresh=False,
        ),
        ReportAction(
            id=f"{service.name}.transfer.check",
            label=f"Check {service.name} transfer gate",
            command=action_command(paths, f"{service.name}.transfer.check"),
            terminal=True,
            refresh=False,
        ),
        ReportAction(
            id=f"{service.name}.transfer.real-gate",
            label=f"Check {service.name} real transfer gate",
            command=action_command(paths, f"{service.name}.transfer.real-gate"),
            terminal=True,
            refresh=False,
        ),
        ReportAction(
            id=f"{service.name}.transfer.automation-gate",
            label=f"Check {service.name} transfer automation gate",
            command=action_command(paths, f"{service.name}.transfer.automation-gate"),
            terminal=True,
            refresh=False,
        ),
        ReportAction(
            id=f"{service.name}.transfer.real-run.preview",
            label=f"Preview {service.name} real transfer run",
            command=action_command(paths, f"{service.name}.transfer.real-run.preview"),
            terminal=True,
            refresh=False,
        ),
        ReportAction(
            id=f"{service.name}.transfer.executor-run.preview",
            label=f"Preview {service.name} transfer executor tick",
            command=action_command(paths, f"{service.name}.transfer.executor-run.preview"),
            terminal=True,
            refresh=False,
        ),
        ReportAction(
            id=f"{service.name}.transfer.consume.preview",
            label=f"Preview {service.name} transfer consume policy",
            command=action_command(paths, f"{service.name}.transfer.consume.preview"),
            terminal=True,
            refresh=False,
        ),
    ]
    if service.name == "pushd":
        actions.append(
            ReportAction(
                id="pushd.fswatch.resident-gate",
                label="Check pushd fswatch resident gate",
                command=action_command(paths, "pushd.fswatch.resident-gate"),
                terminal=True,
                refresh=False,
            )
        )
        actions.append(
            ReportAction(
                id="pushd.fswatch.resident-run.preview",
                label="Preview pushd fswatch resident run",
                command=action_command(paths, "pushd.fswatch.resident-run.preview"),
                terminal=True,
                refresh=False,
            )
        )
        actions.append(
            ReportAction(
                id="pushd.queue.clear.preview",
                label="Preview clear pushd queue",
                command=action_command(paths, "pushd.queue.clear.preview"),
                terminal=True,
                refresh=False,
            )
        )
        actions.append(
            ReportAction(
                id="pushd.queue.prune-missing-local",
                label="Ignore missing local upload records",
                command=action_command(paths, "pushd.queue.prune-missing-local"),
                terminal=False,
                refresh=True,
            )
        )
    if service.name == "diffd":
        actions.append(
            ReportAction(
                id="diffd.api-poll.long-poll-gate",
                label="Check diffd API long-poll gate",
                command=action_command(paths, "diffd.api-poll.long-poll-gate"),
                terminal=True,
                refresh=False,
            )
        )
        actions.append(
            ReportAction(
                id="diffd.api-poll.long-poll-run.preview",
                label="Preview diffd API long-poll run",
                command=action_command(paths, "diffd.api-poll.long-poll-run.preview"),
                terminal=True,
                refresh=False,
            )
        )
        actions.append(
            ReportAction(
                id="diffd.remote-change.clear.preview",
                label="Preview clear diffd remote changes",
                command=action_command(paths, "diffd.remote-change.clear.preview"),
                terminal=True,
                refresh=False,
            )
        )
    return actions


def _state_details(state: ServiceDaemonState) -> dict[str, object]:
    if state.pid is None:
        process_state = "not recorded"
    elif state.pid_running:
        process_state = "running"
    else:
        process_state = "stale"
    transfer_summary, transfer_status = _last_transfer_summary(state.last_transfer)

    return {
        "state dir": str(state.state_dir),
        "pid": state.pid if state.pid is not None else "-",
        "process state": process_state,
        "pid file": str(state.pid_file),
        "queue length": state.queue_length,
        "queue file": str(state.queue_file),
        "cursor": state.cursor,
        "cursor file": str(state.cursor_file),
        "last event": state.last_event or {},
        "last event file": str(state.last_event_file),
        "last plan": state.last_plan or {},
        "last plan file": str(state.last_plan_file),
        "last transfer": state.last_transfer or {},
        "last transfer file": str(state.last_transfer_file),
        "last transfer summary": transfer_summary,
        "last transfer status": transfer_status,
    }


def _last_transfer_summary(payload: dict[str, object] | None) -> tuple[str, str]:
    if not payload:
        return "-", "none"
    results = payload.get("results")
    if not isinstance(results, list) or not results:
        return "results: 0", "unknown"

    timed_out = 0
    failed = 0
    succeeded = 0
    for result in results:
        if not isinstance(result, dict):
            failed += 1
            continue
        if result.get("timed_out") is True:
            timed_out += 1
            continue
        if result.get("returncode") == 0:
            succeeded += 1
            continue
        failed += 1

    if timed_out:
        status = "timeout"
    elif failed:
        status = "failed"
    else:
        status = "success"
    return (
        f"success: {succeeded}; failed: {failed}; timeout: {timed_out}; total: {len(results)}",
        status,
    )


def _read_status_json_file(path: Path, issue_key: str, label: str) -> tuple[dict[str, object] | None, ConfigIssue | None]:
    if not path.exists():
        return None, None
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return None, ConfigIssue(
            key=issue_key,
            level="warning",
            message=f"cannot read {label} status file {path}: {exc}",
        )
    if not isinstance(payload, dict):
        return None, ConfigIssue(
            key=issue_key,
            level="warning",
            message=f"{label} status file must contain a JSON object: {path}",
        )
    return payload, None


def _record_count(payload: dict[str, object] | None, key: str) -> int:
    if not payload:
        return 0
    value = payload.get(key)
    return len(value) if isinstance(value, list) else 0


def _pushd_last_resident_run_details(config: AppConfig) -> tuple[dict[str, object], list[ConfigIssue]]:
    state_file = _resident_run_state_file(config)
    payload, issue = _read_status_json_file(
        state_file,
        "PCLOUD_TOOLS_PUSHD_FSWATCH_RESIDENT_LAST_RUN",
        "pushd fswatch resident last-run",
    )
    issues = [issue] if issue else []
    if not payload:
        return {
            "last resident run file": str(state_file),
            "last resident run status": "none",
            "last resident run summary": "-",
            "last resident run finished at": "-",
        }, issues
    returncode = payload.get("returncode")
    payload_status = str(payload.get("status") or "")
    if payload_status == "running":
        status = "running"
    elif returncode == 0:
        status = "success"
    elif returncode is None:
        status = "unknown"
    else:
        status = "failed"
    appended = _record_count(payload, "appended_records")
    duplicate = _record_count(payload, "duplicate_records")
    debounce = _record_count(payload, "debounce_records")
    queue_limit = _record_count(payload, "queue_limit_records")
    excluded = _record_count(payload, "excluded_records")
    invalid = _record_count(payload, "invalid_records")
    processed = appended + duplicate + debounce + queue_limit + excluded + invalid
    return {
        "last resident run file": str(state_file),
        "last resident run status": status,
        "last resident run summary": (
            f"events: {processed}; appended: {appended}; duplicate: {duplicate}; "
            f"debounce: {debounce}; queue-limit: {queue_limit}; excluded: {excluded}; invalid: {invalid}"
        ),
        "last resident run started at": str(payload.get("started_at") or "-"),
        "last resident run updated at": str(payload.get("updated_at") or "-"),
        "last resident run finished at": str(payload.get("finished_at") or "-"),
        "last resident run pid": payload.get("pid") or "-",
        "last resident run last raw event": str(payload.get("last_raw_event") or "-"),
        "last resident run last normalized event": str(payload.get("last_normalized_event") or "-"),
        "last resident run returncode": returncode if returncode is not None else "-",
    }, issues


def _diffd_last_api_poll_run_details(config: AppConfig) -> tuple[dict[str, object], list[ConfigIssue]]:
    state_file = _diffd_api_long_poll_run_state_file(config)
    payload, issue = _read_status_json_file(
        state_file,
        "PCLOUD_TOOLS_DIFFD_API_LONG_POLL_LAST_RUN",
        "diffd API long-poll last-run",
    )
    issues = [issue] if issue else []
    if not payload:
        return {
            "last api poll run file": str(state_file),
            "last api poll run status": "none",
            "last api poll run summary": "-",
            "last api poll run finished at": "-",
        }, issues
    if payload.get("failure"):
        status = "failed"
    elif payload.get("written_diffid") not in {None, "-"}:
        status = "success"
    else:
        status = "unknown"
    appended = _record_count(payload, "appended_records")
    skipped = _record_count(payload, "skipped_records")
    invalid = _record_count(payload, "invalid_records")
    parsed = payload.get("parsed_diff_changes", "-")
    return {
        "last api poll run file": str(state_file),
        "last api poll run status": status,
        "last api poll run summary": (
            f"parsed: {parsed}; appended: {appended}; skipped: {skipped}; invalid: {invalid}; "
            f"diffid: {payload.get('previous_diffid', '-')} -> {payload.get('written_diffid', '-')}"
        ),
        "last api poll run finished at": str(payload.get("finished_at") or "-"),
        "last api poll run source": str(payload.get("source") or "-"),
        "last api poll run live api": "yes" if payload.get("live_api") is True else "no",
    }, issues


def _status_plan_details(
    config: AppConfig,
    state: ServiceDaemonState,
    service: ServiceDefinition,
) -> tuple[dict[str, object], list[ConfigIssue]]:
    issues: list[ConfigIssue] = []
    if service.name == "pushd":
        plan, scope = build_pushd_plan(config, state)
        issues.extend(plan.issues)
        issues.extend(scope_issues(scope))
        present_upload_records, missing_local_records = _split_missing_local_upload_records(
            config, plan.upload_records
        )
        records, manual_review_records = _filter_manual_review_transfers(
            present_upload_records,
            _opposite_transfer_candidates(config, service),
        )
        return {
            "plan summary": _pushd_plan_summary(plan),
            "pending queue items": plan.total,
            "planned uploads": len(records),
            "missing local upload records": len(missing_local_records),
            "missing local upload record details": _plan_records(missing_local_records),
            "manual review transfer records": len(manual_review_records),
            "excluded queue items": plan.excluded_count,
            "invalid queue items": plan.invalid_count,
            "allowlist status": scope.allowlist_status,
            "allowlist entries": scope.allowlist_count,
        }, issues

    daemon_state = read_daemon_state(config)
    issues.extend(daemon_state.issues)
    plan = build_diffd_plan(config, state, daemon_state)
    issues.extend(plan.issues)
    records, manual_review_records = _filter_manual_review_transfers(
        plan.download_records,
        _opposite_transfer_candidates(config, service),
    )
    folder_cache = _read_diffd_folder_cache(config)
    return {
        "plan summary": _diffd_plan_summary(plan),
        "daemon diffid": daemon_state.diffid,
        "folder cache entries": len(folder_cache),
        "remote changes": plan.remote_change_count,
        "pending downloads": plan.pending_download_count,
        "planned downloads": len(records),
        "manual review transfer records": len(manual_review_records),
        "skipped download records": plan.skipped_count,
    }, issues


def _status_gate_details(paths: RuntimePaths, config: AppConfig, service: ServiceDefinition) -> dict[str, object]:
    launchd_status = _service_launchd_status_report(paths, service)
    launchd_details = launchd_status.details
    if service.name == "pushd":
        service_gate_label = "resident gate"
        service_gate_status = "open-env-only" if _resident_gate_open(config) else "closed"
        service_can_start_label = "resident can start"
    else:
        service_gate_label = "long-poll gate"
        service_gate_status = "open-env-only" if _api_long_poll_gate_open(config) else "closed"
        service_can_start_label = "long-poll can start"
    gate_summary = {
        "real-operation gate": "closed",
        service_gate_label: service_gate_status,
        service_can_start_label: "no",
        "launchd gate": "closed",
        "launchd registration": str(launchd_details.get("registration status", "unknown")),
        "launchd loaded": str(launchd_details.get("launchd loaded", "unknown")),
        "transfer gate": "closed",
        "real transfer execution": "blocked",
    }
    next_safe_actions = [
        f"{service.name} preview",
        f"{service.name} launchd status",
        f"{service.name} launchd gate",
        f"{service.name} transfer check",
    ]
    return {
        "gate summary": gate_summary,
        "real-operation gate": gate_summary["real-operation gate"],
        service_gate_label: service_gate_status,
        service_can_start_label: "no",
        "launchd gate": "closed",
        "launchd registration": gate_summary["launchd registration"],
        "launchd loaded": gate_summary["launchd loaded"],
        "transfer gate": "closed",
        "real transfer execution": "blocked",
        "next safe actions": next_safe_actions,
    }


def _service_status_report(paths: RuntimePaths, service: ServiceDefinition) -> CommandReport:
    load_result = load_config(paths)
    state = read_service_daemon_state(load_result.config, service.name)
    state_details = _state_details(state)
    process_state = state_details["process state"]
    if service.name == "pushd":
        last_run_details, last_run_issues = _pushd_last_resident_run_details(load_result.config)
    else:
        last_run_details, last_run_issues = _diffd_last_api_poll_run_details(load_result.config)
    plan_details, plan_issues = _status_plan_details(load_result.config, state, service)
    gate_details = _status_gate_details(paths, load_result.config, service)
    issues = sort_issues(
        list(load_result.issues)
        + list(state.issues)
        + last_run_issues
        + plan_issues
    )
    if service.name == "pushd" and int(plan_details.get("missing local upload records", 0) or 0) > 0:
        issues = sort_issues(
            [
                *issues,
                ConfigIssue(
                    key="PCLOUD_TOOLS_PUSHD_QUEUE_MISSING_LOCAL",
                    level="warning",
                    message=(
                        "pushd queue has upload records whose local source file is missing; "
                        "review or ignore them from the queue cleanup action"
                    ),
                ),
            ]
        )
    queued_label = "queued" if service.name == "pushd" else "remote"
    queued_count = plan_details.get("pending queue items", plan_details.get("remote changes", state.queue_length))
    planned_count = plan_details.get("planned uploads", plan_details.get("planned downloads", 0))
    missing_count = plan_details.get("missing local upload records", 0) if service.name == "pushd" else 0
    manual_count = plan_details.get("manual review transfer records", 0)
    if service.name == "pushd":
        plan_summary_fragment = f"planned: {planned_count}; stale: {missing_count}; manual-review: {manual_count}"
    else:
        plan_summary_fragment = f"planned: {planned_count}; manual-review: {manual_count}"
    return CommandReport(
        command=f"{service.name} status",
        status=status_from_issues(issues),
        summary=(
            f"{service.name}: {process_state}; {queued_label}: {queued_count}; "
            f"{plan_summary_fragment}; "
            f"launchd: {gate_details['launchd registration']}"
        ),
        details={
            **state_details,
            "state writes": "none",
            **plan_details,
            **last_run_details,
            **gate_details,
            **suppression_status_details(load_result.config),
            **chat_notify_status(load_result.config),
        },
        issues=report_issues(issues),
        actions=_service_actions(paths, service),
    )


def cmd_service_status(
    args: argparse.Namespace, paths: RuntimePaths, service: ServiceDefinition
) -> int:
    report = _service_status_report(paths, service)
    if output_format(args) == "xbar":
        print(_render_service_status_xbar(report, service))
        return exit_code_for_report(report)
    print_report(report, args)
    return exit_code_for_report(report)


def _plan_records(records) -> list[dict[str, str]]:
    return [
        {"path": record.path, "action": record.action, "reason": record.reason}
        for record in records
    ]


def _scope_details(scope: SyncScopeInfo) -> dict[str, object]:
    baseline: ScopeBaseline = scope.baseline
    return {
        "allowlist status": scope.allowlist_status,
        "allowlist entries": scope.allowlist_count,
        "allowlist message": scope.allowlist_message,
        "scope baseline": f"{baseline.mode} ({baseline.status})",
    }


def _pushd_plan_details(config: AppConfig, plan: PushdPlan, scope: SyncScopeInfo) -> dict[str, object]:
    present_upload_records, missing_local_records = _split_missing_local_upload_records(
        config, plan.upload_records
    )
    return {
        "plan source": str(plan.queue_file),
        "plan summary": (
            f"upload: {len(present_upload_records)}; missing-local: {len(missing_local_records)}; "
            f"excluded: {plan.excluded_count}; "
            f"invalid: {plan.invalid_count}"
        ),
        "pending queue items": plan.total,
        "planned uploads": len(present_upload_records),
        "missing local upload records": len(missing_local_records),
        "excluded queue items": plan.excluded_count,
        "invalid queue items": plan.invalid_count,
        "planned upload records": _plan_records(present_upload_records),
        "missing local upload record details": _plan_records(missing_local_records),
        "excluded queue records": _plan_records(plan.excluded_records),
        "invalid queue records": _plan_records(plan.invalid_records),
        **_scope_details(scope),
    }


def _diffd_plan_details(plan: DiffdPlan) -> dict[str, object]:
    return {
        "remote changes file": str(plan.remote_changes_file),
        "pending downloads file": str(plan.pending_downloads_file),
        "plan summary": (
            f"downloads: {plan.download_count}; remote changes: {plan.remote_change_count}; "
            f"pending downloads: {plan.pending_download_count}; skipped: {plan.skipped_count}"
        ),
        "remote changes": plan.remote_change_count,
        "pending downloads": plan.pending_download_count,
        "planned downloads": plan.download_count,
        "skipped download records": plan.skipped_count,
        "remote change records": _plan_records(plan.remote_change_records),
        "pending download records": _plan_records(plan.pending_download_records),
        "planned download records": _plan_records(plan.download_records),
        "skipped download record details": _plan_records(plan.skipped_records),
    }


def _pushd_plan_summary(plan: PushdPlan) -> str:
    return f"upload: {plan.upload_count}; excluded: {plan.excluded_count}; invalid: {plan.invalid_count}"


def _diffd_plan_summary(plan: DiffdPlan) -> str:
    return (
        f"downloads: {plan.download_count}; remote changes: {plan.remote_change_count}; "
        f"pending downloads: {plan.pending_download_count}; skipped: {plan.skipped_count}"
    )


def _transfer_manual_review_reason(record: PlanRecord, opposite_paths: set[str]) -> str:
    action = record.action.strip().lower().replace("_", "-")
    if any(token in action for token in _MANUAL_REVIEW_ACTION_TOKENS):
        return f"{record.action} action requires manual review"
    if action not in _SIMPLE_TRANSFER_ACTIONS:
        return f"{record.action} action is not a simple create/update transfer"
    if record.path in opposite_paths:
        return "same path also has an opposite-side change"
    return ""


def _filter_manual_review_transfers(
    records: tuple[PlanRecord, ...],
    opposite_records: tuple[PlanRecord, ...],
) -> tuple[tuple[PlanRecord, ...], tuple[PlanRecord, ...]]:
    opposite_paths = {record.path for record in opposite_records if record.path}
    transfer_records: list[PlanRecord] = []
    manual_review_records: list[PlanRecord] = []
    for record in records:
        reason = _transfer_manual_review_reason(record, opposite_paths)
        if reason:
            manual_review_records.append(PlanRecord(record.path, record.action, reason))
        else:
            transfer_records.append(record)
    return tuple(transfer_records), tuple(manual_review_records)


def _split_missing_local_upload_records(
    config: AppConfig,
    records: tuple[PlanRecord, ...],
) -> tuple[tuple[PlanRecord, ...], tuple[PlanRecord, ...]]:
    present_records: list[PlanRecord] = []
    missing_records: list[PlanRecord] = []
    for record in records:
        if record.action == "upload" and not (config.core_dir / record.path).exists():
            missing_records.append(PlanRecord(record.path, record.action, "local source file is missing"))
        else:
            present_records.append(record)
    return tuple(present_records), tuple(missing_records)


def _opposite_transfer_candidates(config: AppConfig, service: ServiceDefinition) -> tuple[PlanRecord, ...]:
    if service.name == "pushd":
        diffd_state = read_service_daemon_state(config, "diffd")
        daemon_state = read_daemon_state(config)
        diffd_plan = build_diffd_plan(config, diffd_state, daemon_state)
        return diffd_plan.download_records

    pushd_state = read_service_daemon_state(config, "pushd")
    pushd_plan, _scope = build_pushd_plan(config, pushd_state)
    present_records, _missing_records = _split_missing_local_upload_records(config, pushd_plan.upload_records)
    return present_records


def _manual_review_issue(service: ServiceDefinition, count: int) -> ConfigIssue | None:
    if count == 0:
        return None
    return ConfigIssue(
        key=f"PCLOUD_TOOLS_{service.name.upper()}_TRANSFER_MANUAL_REVIEW",
        level="warning",
        message=(
            f"{count} {service.name} transfer record(s) require manual review and were "
            "excluded from planned transfer commands"
        ),
    )


def _transfer_plan_summary(service: ServiceDefinition, counts: dict[str, int]) -> str:
    if service.name == "pushd":
        return (
            f"upload: {counts['planned uploads']}; "
            f"missing local: {counts.get('missing local upload records', 0)}; "
            f"manual review: {counts['manual review transfer records']}; "
            f"excluded: {counts['excluded queue items']}; invalid: {counts['invalid queue items']}"
        )
    return (
        f"downloads: {counts['planned downloads']}; "
        f"manual review: {counts['manual review transfer records']}; "
        f"remote changes: {counts['remote changes']}; pending downloads: {counts['pending downloads']}; "
        f"skipped: {counts['skipped download records']}"
    )


def _real_transfer_target_confirmation(
    args: argparse.Namespace,
    service: ServiceDefinition,
    commands: list[dict[str, object]],
) -> tuple[dict[str, object], list[ConfigIssue], list[dict[str, object]]]:
    confirmed_path_raw = getattr(args, "confirm_path", None)
    confirmed_direction = getattr(args, "confirm_direction", None)
    confirmed_path = normalize_plan_path(confirmed_path_raw) if confirmed_path_raw else ""
    matching_commands = [
        command
        for command in commands
        if isinstance(command, dict)
        and str(command.get("path", "")) == confirmed_path
        and str(command.get("direction", "")) == str(confirmed_direction or "")
    ]
    expected = matching_commands[0] if len(matching_commands) == 1 else commands[0] if commands else {}
    expected_path = str(expected.get("path", "")) if isinstance(expected, dict) else ""
    expected_direction = str(expected.get("direction", "")) if isinstance(expected, dict) else ""

    details: dict[str, object] = {
        "name": "first real run target",
        "confirmed path": confirmed_path or "-",
        "confirmed direction": confirmed_direction or "-",
        "expected path": expected_path or "-",
        "expected direction": expected_direction or "-",
    }
    if not confirmed_path_raw and not confirmed_direction:
        details.update(
            {
                "status": "pending",
                "detail": "operator must confirm exact path and direction before opening the real gate",
            }
        )
        return details, [], []

    problems: list[str] = []
    if not confirmed_path:
        problems.append("missing confirmed path")
    if not confirmed_direction:
        problems.append("missing confirmed direction")
    if confirmed_path and confirmed_direction:
        if len(matching_commands) == 0:
            problems.append(
                f"confirmed target {confirmed_path!r} {confirmed_direction!r} does not match any planned transfer"
            )
        elif len(matching_commands) > 1:
            problems.append(
                f"confirmed target {confirmed_path!r} {confirmed_direction!r} matches multiple planned transfers"
            )

    if problems:
        detail = "; ".join(problems)
        details.update({"status": "not-ok", "detail": detail})
        return details, [
            ConfigIssue(
                key=f"PCLOUD_TOOLS_{service.name.upper()}_REAL_TRANSFER_TARGET_CONFIRMATION",
                level="warning",
                message=f"first real transfer target confirmation failed: {detail}",
            )
        ], []

    details.update(
        {
            "status": "ok",
            "detail": f"confirmed {confirmed_direction} target {confirmed_path}",
        }
    )
    return details, [], matching_commands


def _real_transfer_policy_check(
    args: argparse.Namespace,
    option_name: str,
    checklist_name: str,
    pending_detail: str,
) -> dict[str, object]:
    value = getattr(args, option_name, None)
    if not value:
        return {
            "name": checklist_name,
            "status": "pending",
            "detail": pending_detail,
        }
    return {
        "name": checklist_name,
        "status": "ok",
        "detail": value,
    }


def _dry_run_transfer_command(command: object) -> list[str]:
    if not isinstance(command, list) or not command:
        return []
    return [str(part) for part in command] + ["--dry-run"]


def _final_real_transfer_review_details(
    *,
    requested: bool,
    checklist: list[dict[str, object]],
    commands: list[dict[str, object]],
    manual_review_count: int,
    total_command_count: int | None = None,
) -> dict[str, object]:
    if not requested:
        return {
            "final review requested": False,
            "final review status": "not requested",
        }

    blockers: list[str] = []
    blocker_details: list[dict[str, object]] = []
    for check in checklist:
        if check.get("status") != "ok":
            name = str(check.get("name", "unknown check"))
            blockers.append(name)
            blocker_details.append(
                {
                    "name": name,
                    "status": str(check.get("status", "-")),
                    "detail": str(check.get("detail", "-")),
                }
            )
    if len(commands) != 1:
        count = total_command_count if total_command_count is not None else len(commands)
        detail = f"planned transfer count is {count}"
        blockers.append(detail)
        blocker_details.append(
            {
                "name": "planned transfer count",
                "status": "not-ok",
                "detail": detail,
            }
        )
    if manual_review_count:
        detail = f"manual review transfer records = {manual_review_count}"
        blockers.append(detail)
        blocker_details.append(
            {
                "name": "manual review transfer records",
                "status": "not-ok",
                "detail": detail,
            }
        )

    first = commands[0] if len(commands) == 1 else {}
    real_command = first.get("command") if isinstance(first, dict) else []
    dry_run_command = [] if blockers else _dry_run_transfer_command(real_command)
    if blockers:
        display_status = "blocked"
        display_note = "blocked; fix the listed checks before displaying dry-run or real transfer commands"
        gate_opening_status = "blocked"
        gate_opening_note = "real transfer gate cannot be considered until all final-review checks are ready"
        next_checks: list[str] = []
    else:
        display_status = "ready" if dry_run_command else "missing"
        display_note = "display only; rclone is not executed and the real-transfer gate remains closed"
        gate_opening_status = "ready-for-separate-gate"
        gate_opening_note = (
            "ready for a separate implementation gate review; real transfer execution is still unavailable"
        )
        next_checks = [
            "operator confirms the displayed dry-run command was reviewed",
            "reviewer approves the exact real command path and direction",
            "real execute gate must be added separately and must not reuse the fake-rclone gate",
            "record consumption must follow the displayed consume policy after transfer success",
        ]
    return {
        "final review requested": True,
        "final review status": "ready" if not blockers else "blocked",
        "final review blockers": blockers,
        "final review blocker details": blocker_details,
        "dry-run transfer command": dry_run_command,
        "real transfer command": real_command if isinstance(real_command, list) and not blockers else [],
        "dry-run display status": display_status,
        "dry-run display note": display_note,
        "real transfer gate opening status": gate_opening_status,
        "real transfer gate opening note": gate_opening_note,
        "separate real gate next checks": next_checks,
    }


def _real_gate_approval_details(args: argparse.Namespace, final_review_status: object) -> dict[str, object]:
    checks = [
        {
            "name": "operator dry-run review",
            "status": "ok" if getattr(args, "operator_reviewed_dry_run", False) else "pending",
            "detail": "operator reviewed the displayed dry-run command",
        },
        {
            "name": "reviewer real command approval",
            "status": "ok" if getattr(args, "reviewer_approved_real_command", False) else "pending",
            "detail": "reviewer approved the exact real command path and direction",
        },
        {
            "name": "reviewer consume policy approval",
            "status": "ok" if getattr(args, "reviewer_approved_consume_policy", False) else "pending",
            "detail": "reviewer approved post-success record consumption policy",
        },
    ]
    if final_review_status != "ready":
        status = "blocked"
    elif all(check["status"] == "ok" for check in checks):
        status = "complete-read-only"
    else:
        status = "pending"
    return {
        "separate real gate approval status": status,
        "separate real gate approval checks": checks,
        "separate real gate approval note": (
            "approval checks are recorded only; real transfer execution remains unavailable"
        ),
    }


def _prior_real_transfer_validation_details(
    state: ServiceDaemonState,
    service: ServiceDefinition,
) -> dict[str, object]:
    payload = state.last_transfer if isinstance(state.last_transfer, dict) else {}
    results = payload.get("results") if isinstance(payload, dict) else None
    commands = payload.get("planned_transfer_commands") if isinstance(payload, dict) else None
    successful = [
        item
        for item in results
        if isinstance(item, dict)
        and item.get("returncode") == 0
        and not item.get("timed_out")
        and item.get("path")
        and item.get("direction")
    ] if isinstance(results, list) else []
    failed = [
        item
        for item in results
        if isinstance(item, dict)
        and (item.get("returncode") != 0 or item.get("timed_out"))
    ] if isinstance(results, list) else []
    expected_direction = "upload" if service.name == "pushd" else "download"
    direction_ok = all(str(item.get("direction", "")) == expected_direction for item in successful)
    service_ok = str(payload.get("service", "")) == service.name if payload else False
    mode = str(payload.get("mode", "")) if payload else ""
    mode_ok = mode in {"real-rclone-transfer", "real-rclone-automation-transfer"}
    status = "ok" if service_ok and mode_ok and successful and not failed and direction_ok else "missing"
    if not payload:
        detail = "last-transfer.json is missing"
    elif not service_ok:
        detail = f"last transfer service is {payload.get('service', '-')}"
    elif not mode_ok:
        detail = f"last transfer mode is {payload.get('mode', '-')}"
    elif failed:
        detail = f"last transfer has failed/timeout result count {len(failed)}"
    elif not successful:
        detail = "last transfer has no successful result"
    elif not direction_ok:
        detail = f"last transfer direction does not match {expected_direction}"
    else:
        paths = ", ".join(str(item.get("path", "")) for item in successful)
        detail = f"successful {expected_direction} validation: {paths}"
    return {
        "prior real transfer validation status": status,
        "prior real transfer validation detail": detail,
        "prior real transfer file": str(state.last_transfer_file),
        "prior real transfer generated at": str(payload.get("generated_at", "-")) if payload else "-",
        "prior real transfer mode": str(payload.get("mode", "-")) if payload else "-",
        "prior real transfer success count": len(successful),
        "prior real transfer failed count": len(failed),
        "prior real transfer command count": len(commands) if isinstance(commands, list) else 0,
    }


def _operator_verification_details(final_review_status: object, approval_status: object) -> dict[str, object]:
    if final_review_status != "ready":
        required = "no"
        scope = "blocked/read-only diagnostics; automated validation is enough"
        human_gate_status = "not-yet"
        human_gate_reason = "final-review checks are blocked before any human real-transfer review"
    elif approval_status == "complete-read-only":
        required = "not-now"
        scope = "read-only approvals are complete; actual transfer still requires explicit real-run execution"
        human_gate_status = "required-before-actual-transfer"
        human_gate_reason = "actual pCloud/rclone transfer still requires an explicit operator run command"
    else:
        required = "yes-before-real-gate"
        scope = "operator/reviewer approval remains pending before any future real execution gate"
        human_gate_status = "required-before-real-gate"
        human_gate_reason = "operator/reviewer approval is pending for the first real target"
    return {
        "operator verification required": required,
        "operator verification scope": scope,
        "human gate status": human_gate_status,
        "human gate reason": human_gate_reason,
        "next human check trigger": (
            "first real target review, real execution gate implementation, or actual pCloud/rclone transfer"
        ),
    }


def _real_execution_readiness_details(final_review_status: object, approval_status: object) -> dict[str, object]:
    if final_review_status != "ready":
        readiness = "blocked-final-review"
        reason = "final-review checks are not ready"
    elif approval_status != "complete-read-only":
        readiness = "blocked-approval"
        reason = "read-only operator/reviewer approvals are incomplete"
    else:
        readiness = "blocked-execution-gate"
        reason = "guarded real-run exists but requires the dedicated real execution gate and --execute"
    return {
        "real execution readiness": readiness,
        "real execution blocked reason": reason,
        "real execution can run": "no",
    }


def _future_real_run_policy_details(service: ServiceDefinition) -> dict[str, object]:
    record_name = "pushd queue record" if service.name == "pushd" else "diffd remote-change record"
    return {
        "future real-run policy status": "documented-read-only",
        "future real-run success policy": (
            f"remove matching {record_name} only after rclone exits 0 and the exact transfer target is recorded"
        ),
        "future real-run failure policy": f"retain matching {record_name} for retry/manual review",
        "future real-run unknown policy": (
            f"retain matching {record_name} when timeout, partial transfer, or result verification is unclear"
        ),
        "future real-run rollback policy": (
            "no automatic local/remote delete or rollback; record the failure and require operator review"
        ),
        "future real-run policy state writes": "none",
    }


def _pushd_preview_details(
    paths: RuntimePaths, config: AppConfig, plan: PushdPlan, scope: SyncScopeInfo
) -> dict[str, object]:
    return {
        "planned action": "preview pcloud-pushd scaffold",
        "implementation status": "scaffold only; fswatch and upload execution are disabled",
        "dev mode": "on" if paths.dev_mode else "off",
        "watch root": str(config.core_dir),
        "target remote": config.core_remote,
        "allowlist file": str(config.allowlist_file),
        "manager ignore file": str(config.manager_ignore_file),
        "default excludes": list(config.default_excludes),
        "debounce seconds": config.pushd_debounce_seconds,
        "queue limit": config.pushd_queue_limit,
        "state dir": str(config.state_dir / "pushd"),
        **_pushd_plan_details(config, plan, scope),
    }


def _diffd_preview_details(
    paths: RuntimePaths, config: AppConfig, daemon_state: DaemonState, plan: DiffdPlan
) -> dict[str, object]:
    return {
        "planned action": "preview pcloud-diffd scaffold",
        "implementation status": "scaffold only; pCloud API long-poll and downloads are disabled",
        "dev mode": "on" if paths.dev_mode else "off",
        "remote root": config.core_remote,
        "poll interval seconds": config.diffd_poll_interval_seconds,
        "batch limit": config.diffd_batch_limit,
        "state dir": str(config.state_dir / "diffd"),
        "daemon diffid": daemon_state.diffid,
        "auto-download": "on" if daemon_state.auto_download_enabled else "off",
        **_diffd_plan_details(plan),
    }


def _service_preview_report(paths: RuntimePaths, service: ServiceDefinition) -> CommandReport:
    load_result = load_config(paths)
    state = read_service_daemon_state(load_result.config, service.name)
    issues = list(load_result.issues) + list(state.issues)
    if service.name == "pushd":
        plan, scope = build_pushd_plan(load_result.config, state)
        issues.extend(scope_issues(scope))
        issues.extend(plan.issues)
        details = _pushd_preview_details(paths, load_result.config, plan, scope)
    else:
        daemon_state = read_daemon_state(load_result.config)
        issues.extend(daemon_state.issues)
        plan = build_diffd_plan(load_result.config, state, daemon_state)
        issues.extend(plan.issues)
        details = _diffd_preview_details(paths, load_result.config, daemon_state, plan)
    issues = sort_issues(issues)
    details.update(
        {
            "pid file": str(state.pid_file),
            "queue file": str(state.queue_file),
            "cursor file": str(state.cursor_file),
            "last plan file": str(state.last_plan_file),
        }
    )

    return CommandReport(
        command=f"{service.name} preview",
        status=status_from_issues(issues),
        summary=f"{service.name} scaffold preview is ready",
        details=details,
        issues=report_issues(issues),
        actions=_service_actions(paths, service),
    )


def cmd_service_preview(
    args: argparse.Namespace, paths: RuntimePaths, service: ServiceDefinition
) -> int:
    report = _service_preview_report(paths, service)
    print_report(report, args)
    return exit_code_for_report(report)


def _service_policy_details(config: AppConfig, service: ServiceDefinition) -> dict[str, object]:
    scope = sync_allowlist_info(config)
    baseline_label = f"{scope.baseline.mode} ({scope.baseline.status})"
    shared_blocked = [
        "automatic upload/download transfer execution",
        "launchd registration or bootstrap",
        "normal sync/resync execution",
        "rclone bisync listing cache delete/move",
    ]
    if service.name == "pushd":
        return {
            "daemonization policy status": "documented-read-only",
            "initial daemon scope": "fswatch resident watcher appends pushd queue records only",
            "event source": "fswatch foreground resident-run before any launchd integration",
            "queue file": str(config.state_dir / "pushd" / "queue.json"),
            "allowlist policy": f"{scope.allowlist_status}; {baseline_label}; entries={scope.allowlist_count}",
            "event-to-queue policy": (
                "allowlisted create/update events become upload records after default-exclude, "
                ".pcloudmanagerignore, and hard safety filtering; delete/remove and rename/move events "
                "stay in the queue with matching actions for manual review before transfer"
            ),
            "duplicate event policy": "skip duplicate path/action records already present in the pushd queue",
            "debounce policy": (
                f"configured debounce window is {config.pushd_debounce_seconds}s; "
                "skip upload path/action events that match a recent successful resident append"
            ),
            "queue limit policy": f"do not append new resident records once queue has {config.pushd_queue_limit} records",
            "restart policy": "foreground guarded run first; persistent restart policy waits for a later launchd gate",
            "log policy": "record fswatch-resident-last-run.json; do not write transfer logs",
            "queue/transfer separation": "queue append does not execute, preview, or consume rclone upload transfer",
            "blocked operations": shared_blocked + ["pCloud API long-poll"],
            "operator confirmation required before": [
                "unbounded resident process",
                "launchd registration",
                "real upload transfer validation",
            ],
            "state writes": "none",
        }
    return {
        "daemonization policy status": "documented-read-only",
        "initial daemon scope": "pCloud /diff polling appends diffd remote-change records only",
        "event source": "guarded long-poll-run one-shot before any continuous loop or launchd integration",
        "remote changes file": str(config.state_dir / "diffd" / "remote-changes.json"),
        "cursor file": str(config.state_dir / "daemon" / "diffid"),
        "folder cache file": str(config.state_dir / "diffd" / "folder-cache.json"),
        "allowlist policy": f"{scope.allowlist_status}; {baseline_label}; entries={scope.allowlist_count}",
        "response policy": (
            "accepted pCloud file events are path-normalized, filtered through allowlist/default excludes/"
            ".pcloudmanagerignore, and only planned downloads are appended as remote-change records"
        ),
        "cursor policy": "mutate diffid only after an accepted response has been parsed without fatal errors",
        "failure policy": "retain current diffid and existing remote-change records; record failure state for retry/manual review after a gated live API attempt",
        "retry policy": "retry is manual or future scheduler-driven; this command does not loop after failure",
        "backoff policy": f"next retry should wait at least {config.diffd_poll_interval_seconds}s after a failed API attempt",
        "queue/transfer separation": "remote-change append does not execute, preview, or consume rclone download transfer",
        "blocked operations": shared_blocked + ["fswatch resident process"],
        "operator confirmation required before": [
            "continuous or blocking live API loop",
            "launchd registration",
            "real download transfer validation",
        ],
        "state writes": "none",
    }


def _service_policy_report(paths: RuntimePaths, service: ServiceDefinition) -> CommandReport:
    load_result = load_config(paths)
    issues = sort_issues(list(load_result.issues))
    return CommandReport(
        command=f"{service.name} policy",
        status=status_from_issues(issues),
        summary=f"{service.name} daemonization policy is documented",
        details=_service_policy_details(load_result.config, service),
        issues=report_issues(issues),
        actions=_service_actions(paths, service),
    )


def cmd_service_policy(
    args: argparse.Namespace, paths: RuntimePaths, service: ServiceDefinition
) -> int:
    report = _service_policy_report(paths, service)
    print_report(report, args)
    return exit_code_for_report(report)


def _gate_details(paths: RuntimePaths, config: AppConfig, service: ServiceDefinition) -> dict[str, object]:
    shared_requirements = [
        "saved shadow validation report with status ok",
        "reviewer approval recorded in report handoff",
        "explicit operator gate for this real operation",
    ]
    if service.name == "pushd":
        blocked = [
            "fswatch resident daemon",
            "launchd registration",
            "real upload execution",
            "queue consumption against live state",
        ]
        next_units = [
            "capture first real upload target with transfer check --final-review",
            "complete read-only real-gate approvals without opening execution",
            "hold real-run implementation until the human gate is explicitly confirmed",
        ]
    else:
        blocked = [
            "pCloud API long-poll",
            "launchd registration",
            "real download execution",
            "diff cursor mutation against live state",
        ]
        next_units = [
            "capture first real download target with transfer check --final-review",
            "complete read-only real-gate approvals without opening execution",
            "hold real-run implementation until the human gate is explicitly confirmed",
        ]
    return {
        "gate status": "closed",
        "allowed work": "dev-state preview/status/plan/report/test only",
        "operator verification required": "no",
        "operator verification scope": "read-only gate diagnostics; automated validation is enough",
        "human gate status": "required-before-real-work",
        "human gate reason": (
            "remaining work includes real rclone/pCloud transfer, real validation, or archive decisions"
        ),
        "next human check trigger": (
            "first real target review, real execution gate implementation, or actual pCloud/rclone transfer"
        ),
        "dev mode": "on" if paths.dev_mode else "off",
        "state dir": str(config.state_dir / service.name),
        "workspace root": str(paths.workspace_root),
        "shadow validation command": "python3 scripts/pcloud-shadow-validation.py --json",
        "blocked operations": blocked,
        "required before opening": shared_requirements,
        "suggested next units": next_units,
    }


def _service_gate_report(paths: RuntimePaths, service: ServiceDefinition) -> CommandReport:
    load_result = load_config(paths)
    issues = list(load_result.issues)
    issues.append(
        ConfigIssue(
            key=f"PCLOUD_TOOLS_{service.name.upper()}_REAL_GATE",
            level="warning",
            message=(
                f"{service.name} real operations remain gated; "
                "use preview/dev-state paths until the dedicated gate is explicitly opened"
            ),
        )
    )
    issues = sort_issues(issues)
    return CommandReport(
        command=f"{service.name} gate",
        status=status_from_issues(issues),
        summary=f"{service.name} real-operation gate is closed",
        details=_gate_details(paths, load_result.config, service),
        issues=report_issues(issues),
        actions=_service_actions(paths, service),
    )


def cmd_service_gate(
    args: argparse.Namespace, paths: RuntimePaths, service: ServiceDefinition
) -> int:
    report = _service_gate_report(paths, service)
    print_report(report, args)
    return exit_code_for_report(report)


def _service_launchd_label(paths: RuntimePaths, service: ServiceDefinition) -> str:
    prefix = "com.example" if paths.dev_mode else "com.takafumi"
    suffix = ".dev" if paths.dev_mode else ""
    return f"{prefix}.pcloud-{service.name}{suffix}"


def _service_public_launchd_label(service: ServiceDefinition) -> str:
    return f"com.takafumi.pcloud-{service.name}"


def _service_public_executor_launchd_label(service: ServiceDefinition) -> str:
    return f"com.takafumi.pcloud-{service.name}-executor"


def _service_public_launchd_plist_path(label: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def _service_launchd_plist_path(paths: RuntimePaths, label: str) -> Path:
    if paths.dev_mode:
        return paths.workspace_root / ".dev-state" / "launchd" / f"{label}.plist"
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def _service_launchd_program_arguments(service: ServiceDefinition, *, entrypoint: str) -> list[str]:
    if service.name == "pushd":
        return [entrypoint, "pushd", "fswatch", "resident-run"]
    return [entrypoint, "diffd", "api-poll", "long-poll-run"]


def _service_launchd_gate_spec(service: ServiceDefinition) -> GateSpec:
    return GATES[f"{service.name}.launchd.gate"]


def _service_launchd_plist_gate_value(service: ServiceDefinition) -> str:
    return _PUSHD_LAUNCHD_PLIST_GATE_VALUE if service.name == "pushd" else _DIFFD_LAUNCHD_PLIST_GATE_VALUE


def _service_launchd_plist_gate_env(service: ServiceDefinition) -> str:
    return f"PCLOUD_TOOLS_{service.name.upper()}_LAUNCHD_PLIST_GATE"


def _service_launchd_automation_plist_gate_env(service: ServiceDefinition) -> str:
    return f"PCLOUD_TOOLS_{service.name.upper()}_LAUNCHD_AUTOMATION_PLIST_GATE"


def _service_launchd_automation_plist_gate_value(service: ServiceDefinition) -> str:
    return f"operator-approved-{service.name}-launchd-automation-plist-v1"


def _service_launchd_automation_reload_gate_env(service: ServiceDefinition) -> str:
    return f"PCLOUD_TOOLS_{service.name.upper()}_LAUNCHD_AUTOMATION_RELOAD_GATE"


def _service_launchd_automation_reload_gate_value(service: ServiceDefinition) -> str:
    return f"operator-approved-{service.name}-launchd-automation-reload-v1"


def _service_launchd_plist_payload(
    paths: RuntimePaths,
    service: ServiceDefinition,
    *,
    label: str,
    entrypoint: str,
) -> dict[str, object]:
    return {
        "Label": label,
        "ProgramArguments": _service_launchd_program_arguments(service, entrypoint=entrypoint),
        "RunAtLoad": True,
        "KeepAlive": False,
        "WorkingDirectory": str(paths.workspace_root),
        "StandardOutPath": str(paths.log_dir / f"{service.name}-launchd.out"),
        "StandardErrorPath": str(paths.log_dir / f"{service.name}-launchd.err"),
    }


def _service_launchd_executor_label(service: ServiceDefinition) -> str:
    return f"com.example.pcloud-{service.name}-executor.dev"


def _service_launchd_executor_plist_path(paths: RuntimePaths, label: str) -> Path:
    return paths.workspace_root / ".dev-state" / "launchd" / f"{label}.plist"


def _service_launchd_executor_plist_payload(
    paths: RuntimePaths,
    service: ServiceDefinition,
    *,
    label: str,
    entrypoint: str,
    start_interval_seconds: int,
) -> dict[str, object]:
    fake_rclone = paths.workspace_root / ".dev-state" / "bin" / "fake-rclone"
    state_dir = paths.workspace_root / ".dev-state" / "state"
    config_dir = paths.workspace_root / ".dev-state" / "config"
    log_dir = paths.workspace_root / ".dev-state" / "logs"
    return {
        "Label": label,
        "ProgramArguments": [
            entrypoint,
            service.name,
            "transfer",
            "executor-run",
            "--execute",
            "--consume-on-success",
            "--json",
        ],
        "EnvironmentVariables": {
            "PATH": _LAUNCHD_RESIDENT_PATH,
            "PCLOUD_TOOLS_DEV": "1",
            "PCLOUD_TOOLS_WORKSPACE_ROOT": str(paths.workspace_root),
            "PCLOUD_TOOLS_CONFIG_DIR": str(config_dir),
            "PCLOUD_TOOLS_STATE_DIR": str(state_dir),
            "PCLOUD_TOOLS_LOG_DIR": str(log_dir),
            "PCLOUD_TOOLS_TRANSFER_EXECUTION_GATE": _TRANSFER_EXECUTION_GATE_VALUE,
            "PCLOUD_TOOLS_RCLONE_BIN": str(fake_rclone),
        },
        "RunAtLoad": False,
        "KeepAlive": False,
        "StartInterval": start_interval_seconds,
        "WorkingDirectory": str(paths.workspace_root),
        "StandardOutPath": str(log_dir / f"{service.name}-executor-launchd.out"),
        "StandardErrorPath": str(log_dir / f"{service.name}-executor-launchd.err"),
    }


def _service_public_executor_launchd_plist_payload(
    paths: RuntimePaths,
    service: ServiceDefinition,
    *,
    label: str,
    entrypoint: str,
    start_interval_seconds: int,
    max_records: int,
    report_path: Path | None = None,
) -> dict[str, object]:
    program_arguments = [
        entrypoint,
        service.name,
        "transfer",
        "automation-run",
        "--execute",
        "--consume-on-success",
        "--max-records",
        str(max_records),
    ]
    if report_path is not None:
        program_arguments.extend(["--report-path", str(report_path.expanduser())])
    program_arguments.append("--json")
    return {
        "Label": label,
        "ProgramArguments": program_arguments,
        "EnvironmentVariables": {
            "PATH": _LAUNCHD_RESIDENT_PATH,
            "PCLOUD_TOOLS_REAL_TRANSFER_EXECUTION_GATE": _REAL_TRANSFER_EXECUTION_GATE_VALUE,
            "PCLOUD_TOOLS_REAL_TRANSFER_AUTOMATION_GATE": _REAL_TRANSFER_AUTOMATION_GATE_VALUE,
            "PCLOUD_TOOLS_REAL_TRANSFER_AUTOMATION_RUN_GATE": _REAL_TRANSFER_AUTOMATION_RUN_GATE_VALUE,
            "PCLOUD_TOOLS_TRANSFER_EXEC_TIMEOUT_SECONDS": str(
                _PUBLIC_REAL_TRANSFER_TIMEOUT_SECONDS
            ),
        },
        "RunAtLoad": False,
        "KeepAlive": False,
        "StartInterval": start_interval_seconds,
        "WorkingDirectory": str(paths.workspace_root),
        "StandardOutPath": str(paths.log_dir / f"{service.name}-real-transfer-executor-launchd.out"),
        "StandardErrorPath": str(paths.log_dir / f"{service.name}-real-transfer-executor-launchd.err"),
    }


def _path_is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _service_launchd_plist_report(
    args: argparse.Namespace,
    paths: RuntimePaths,
    service: ServiceDefinition,
) -> CommandReport:
    load_result = load_config(paths)
    issues = list(load_result.issues)
    execute = bool(getattr(args, "execute", False))
    public_write = bool(getattr(args, "public_write", False))
    entrypoint = action_entrypoint_command(paths)
    label = _service_launchd_label(paths, service)
    plist_path = _service_launchd_plist_path(paths, label)
    payload = _service_launchd_plist_payload(paths, service, label=label, entrypoint=entrypoint)
    dev_launchd_root = paths.workspace_root / ".dev-state" / "launchd"
    public_launchd_root = Path.home() / "Library" / "LaunchAgents"
    plist_gate_env = _service_launchd_plist_gate_env(service)
    plist_gate_value = _service_launchd_plist_gate_value(service)
    plist_gate_open = os.environ.get(plist_gate_env) == plist_gate_value
    public_approvals = [
        {
            "name": "operator plist review",
            "status": "ok" if getattr(args, "operator_reviewed_plist", False) else "pending",
            "detail": "operator reviewed label, ProgramArguments, working directory, logs, RunAtLoad, and KeepAlive",
        },
        {
            "name": "public target approval",
            "status": "ok" if getattr(args, "reviewer_approved_public_target", False) else "pending",
            "detail": f"reviewer approved writing one service plist under {public_launchd_root}",
        },
        {
            "name": "no-bootstrap approval",
            "status": "ok" if getattr(args, "reviewer_approved_no_bootstrap", False) else "pending",
            "detail": "reviewer approved plist write only; no launchctl registration in this gate",
        },
        {
            "name": "public plist gate env",
            "status": "ok" if plist_gate_open else "pending",
            "detail": f"{plist_gate_env}={plist_gate_value}",
        },
    ]
    public_approval_status = (
        "complete-read-only" if all(item["status"] == "ok" for item in public_approvals) else "pending"
    )
    if public_write:
        target_kind = "public"
        expected_root = public_launchd_root
        state_write_label = "public launchd plist only"
    else:
        target_kind = "dev"
        expected_root = dev_launchd_root
        state_write_label = "launchd plist only"
    details: dict[str, object] = {
        "execute": "yes" if execute else "no",
        "public write requested": "yes" if public_write else "no",
        "planned action": (
            f"{'write' if execute else 'preview'} {service.name} {target_kind} LaunchAgent plist"
        ),
        "implementation status": (
            "public plist write gate; launchctl is not executed"
            if public_write
            else "dev-state plist scaffold only; launchctl is not executed"
        ),
        "service label": label,
        "plist path": str(plist_path),
        "plist status": "present" if plist_path.exists() else "missing",
        "plist payload": payload,
        "program arguments": payload.get("ProgramArguments", []),
        "working directory": payload.get("WorkingDirectory", "-"),
        "standard out path": payload.get("StandardOutPath", "-"),
        "standard error path": payload.get("StandardErrorPath", "-"),
        "run at load": payload.get("RunAtLoad", False),
        "keep alive": payload.get("KeepAlive", False),
        "environment strategy": "not embedded; future public registration gate must review required gate env separately",
        "plist target kind": target_kind,
        "expected plist root": str(expected_root),
        "public plist gate env var": plist_gate_env,
        "public plist gate accepted value": plist_gate_value,
        "public plist gate status": "open" if plist_gate_open else "closed",
        "public plist approval status": public_approval_status,
        "public plist preflight checks": public_approvals,
        "state writes": state_write_label if execute else "none",
        "launchctl execution": "no",
        "persistent daemon start": "no",
        "automatic transfer execution": "no",
        "normal sync/resync": "no",
        "listing cache operations": "no",
        "blocked operations": [
            "launchctl enable",
            "launchctl bootstrap",
            "launchctl bootout",
            "launchctl disable",
            "starting persistent daemon",
            "automatic upload/download transfer execution",
            "normal sync/resync",
            "listing cache operations",
        ],
    }
    if public_write and paths.dev_mode:
        issues.append(
            ConfigIssue(
                key=f"PCLOUD_TOOLS_{service.name.upper()}_LAUNCHD_PLIST_PUBLIC_RUNTIME",
                level="error",
                message=f"{service.name} public plist write must use the public non-dev pcloud-manager runtime",
            )
        )
    if execute and not public_write and not paths.dev_mode:
        issues.append(
            ConfigIssue(
                key=f"PCLOUD_TOOLS_{service.name.upper()}_LAUNCHD_PLIST_EXECUTION",
                level="error",
                message=f"{service.name} launchd plist --execute is limited to pcloud-manager-dev/dev mode",
            )
        )
    if execute and public_write and public_approval_status != "complete-read-only":
        issues.append(
            ConfigIssue(
                key=f"PCLOUD_TOOLS_{service.name.upper()}_LAUNCHD_PLIST_PUBLIC_APPROVAL",
                level="error",
                message=(
                    f"{service.name} public plist write requires {plist_gate_env}={plist_gate_value} "
                    "and all public plist approval flags"
                ),
            )
        )
    if execute and not _path_is_relative_to(plist_path, expected_root):
        issues.append(
            ConfigIssue(
                key=f"PCLOUD_TOOLS_{service.name.upper()}_LAUNCHD_PLIST_PATH",
                level="error",
                message=f"refusing to write {service.name} launchd plist outside {expected_root}: {plist_path}",
            )
        )
    issues = sort_issues(issues)
    if has_errors(issues):
        details["state writes"] = "none"
    if execute and not has_errors(issues):
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        plist_path.write_bytes(plistlib.dumps(payload, sort_keys=True))
        details["plist status"] = "written"
    return CommandReport(
        command=f"{service.name} launchd plist",
        status=status_from_issues(issues),
        summary=(
            f"{service.name} launchd plist written"
            if execute and not has_errors(issues)
            else f"{service.name} launchd plist preview is ready"
        ),
        details=details,
        issues=report_issues(issues),
        actions=_service_actions(paths, service),
    )


def _service_launchd_executor_plist_report(
    args: argparse.Namespace,
    paths: RuntimePaths,
    service: ServiceDefinition,
) -> CommandReport:
    load_result = load_config(paths)
    issues = list(load_result.issues)
    execute = bool(getattr(args, "execute", False))
    start_interval_seconds = int(getattr(args, "start_interval_seconds", _QUEUE_EXECUTOR_START_INTERVAL_SECONDS))
    entrypoint = action_entrypoint_command(paths)
    label = _service_launchd_executor_label(service)
    plist_path = _service_launchd_executor_plist_path(paths, label)
    expected_root = paths.workspace_root / ".dev-state" / "launchd"
    fake_rclone = paths.workspace_root / ".dev-state" / "bin" / "fake-rclone"
    payload = _service_launchd_executor_plist_payload(
        paths,
        service,
        label=label,
        entrypoint=entrypoint,
        start_interval_seconds=start_interval_seconds,
    )
    checks = [
        {
            "name": "dev runtime",
            "status": "ok" if paths.dev_mode else "pending",
            "detail": "executor launchd plist write is limited to pcloud-manager-dev/dev mode",
        },
        {
            "name": "dev-state target",
            "status": "ok" if _path_is_relative_to(plist_path, expected_root) else "pending",
            "detail": str(plist_path),
        },
        {
            "name": "fake-rclone binary",
            "status": "ok" if fake_rclone.exists() and os.access(fake_rclone, os.X_OK) else "pending",
            "detail": str(fake_rclone),
        },
        {
            "name": "queue executor interval",
            "status": "ok" if start_interval_seconds > 0 else "pending",
            "detail": f"StartInterval={start_interval_seconds}",
        },
        {
            "name": "parallel dangerous gates",
            "status": "ok",
            "detail": "real rclone/pCloud transfer automation, normal sync/resync, listing cache operations, and public launchd changes stay out of scope",
        },
    ]
    if start_interval_seconds <= 0:
        issues.append(
            ConfigIssue(
                key=f"PCLOUD_TOOLS_{service.name.upper()}_EXECUTOR_START_INTERVAL",
                level="error",
                message="executor launchd StartInterval must be a positive number of seconds",
            )
        )
    if execute and not paths.dev_mode:
        issues.append(
            ConfigIssue(
                key=f"PCLOUD_TOOLS_{service.name.upper()}_EXECUTOR_PLIST_RUNTIME",
                level="error",
                message=f"{service.name} launchd executor-plist --execute is limited to pcloud-manager-dev/dev mode",
            )
        )
    if execute and not fake_rclone.exists():
        issues.append(
            ConfigIssue(
                key=f"PCLOUD_TOOLS_{service.name.upper()}_EXECUTOR_FAKE_RCLONE",
                level="error",
                message=f"cannot write executor plist until fake-rclone exists at {fake_rclone}",
            )
        )
    if execute and fake_rclone.exists() and not os.access(fake_rclone, os.X_OK):
        issues.append(
            ConfigIssue(
                key=f"PCLOUD_TOOLS_{service.name.upper()}_EXECUTOR_FAKE_RCLONE",
                level="error",
                message=f"fake-rclone is not executable: {fake_rclone}",
            )
        )
    if execute and not _path_is_relative_to(plist_path, expected_root):
        issues.append(
            ConfigIssue(
                key=f"PCLOUD_TOOLS_{service.name.upper()}_EXECUTOR_PLIST_PATH",
                level="error",
                message=f"refusing to write {service.name} executor plist outside {expected_root}: {plist_path}",
            )
        )
    issues = sort_issues(issues)
    details: dict[str, object] = {
        "execute": "yes" if execute else "no",
        "planned action": f"{'write' if execute else 'preview'} {service.name} dev-state queue executor LaunchAgent plist",
        "implementation status": "dev-state fake-rclone queue executor plist; launchctl and public launchd are not touched",
        "executor automation target": "dev-state fake-rclone only",
        "real transfer automation gate status": "closed",
        "service label": label,
        "plist path": str(plist_path),
        "plist status": "present" if plist_path.exists() else "missing",
        "plist payload": payload,
        "program arguments": payload.get("ProgramArguments", []),
        "environment variables": payload.get("EnvironmentVariables", {}),
        "working directory": payload.get("WorkingDirectory", "-"),
        "standard out path": payload.get("StandardOutPath", "-"),
        "standard error path": payload.get("StandardErrorPath", "-"),
        "run at load": payload.get("RunAtLoad", False),
        "keep alive": payload.get("KeepAlive", False),
        "start interval seconds": payload.get("StartInterval", "-"),
        "preflight checks": checks,
        "state writes": "launchd executor plist only" if execute and not has_errors(issues) else "none",
        "launchctl execution": "no",
        "public launchd changes": "no",
        "persistent daemon start": "no",
        "automatic real transfer execution": "no",
        "normal sync/resync": "no",
        "listing cache operations": "no",
        "blocked operations": [
            "public LaunchAgent plist write",
            "launchctl enable",
            "launchctl bootstrap",
            "launchctl bootout",
            "launchctl disable",
            "real rclone/pCloud transfer automation",
            "normal sync/resync",
            "listing cache operations",
        ],
    }
    if execute and not has_errors(issues):
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        plist_path.write_bytes(plistlib.dumps(payload, sort_keys=True))
        details["plist status"] = "written"
    return CommandReport(
        command=f"{service.name} launchd executor-plist",
        status=status_from_issues(issues),
        summary=(
            f"{service.name} launchd executor plist written"
            if execute and not has_errors(issues)
            else f"{service.name} launchd executor plist preview is ready"
        ),
        details=details,
        issues=report_issues(issues),
        actions=_service_actions(paths, service),
    )


def _automation_gate_review_report(
    args: argparse.Namespace,
    paths: RuntimePaths,
    service: ServiceDefinition,
) -> CommandReport:
    return _transfer_automation_gate_report(args, paths, service)


def _service_launchd_automation_plist_report(
    args: argparse.Namespace,
    paths: RuntimePaths,
    service: ServiceDefinition,
) -> CommandReport:
    execute = bool(getattr(args, "execute", False))
    gate_report = _automation_gate_review_report(args, paths, service)
    issues = _config_issues_from_report(gate_report)
    interval = int(getattr(args, "start_interval_seconds", _QUEUE_EXECUTOR_START_INTERVAL_SECONDS))
    max_records = int(getattr(args, "max_records", _PUBLIC_QUEUE_EXECUTOR_MAX_RECORDS))
    report_path = getattr(args, "report_path", None)
    shadow_check, shadow_issues = _shadow_report_check(
        report_path,
        issue_key=f"PCLOUD_TOOLS_{service.name.upper()}_LAUNCHD_AUTOMATION_PLIST_SHADOW_REPORT",
    )
    issues.extend(shadow_issues)
    public_entrypoint = _command_v("pcloud-manager") or "pcloud-manager"
    label = _service_public_executor_launchd_label(service)
    plist_path = _service_public_launchd_plist_path(label)
    payload = _service_public_executor_launchd_plist_payload(
        paths,
        service,
        label=label,
        entrypoint=public_entrypoint,
        start_interval_seconds=interval,
        max_records=max_records,
        report_path=report_path,
    )
    expected_root = Path.home() / "Library" / "LaunchAgents"
    plist_gate_env = _service_launchd_automation_plist_gate_env(service)
    plist_gate_value = _service_launchd_automation_plist_gate_value(service)
    plist_gate_open = os.environ.get(plist_gate_env) == plist_gate_value
    checks = [
        shadow_check,
        {
            "name": "automation gate review",
            "status": "ok" if gate_report.details.get("automation approval status") == "ready-for-launchd-review" else "pending",
            "detail": str(gate_report.details.get("automation approval status", "pending")),
        },
        {
            "name": "public pcloud-manager wrapper",
            "status": "ok" if public_entrypoint != "pcloud-manager" else "pending",
            "detail": public_entrypoint,
        },
        {
            "name": "automation command review",
            "status": "ok" if getattr(args, "operator_reviewed_automation_command", False) else "pending",
            "detail": "operator reviewed automation-run ProgramArguments with --execute and --consume-on-success",
        },
        {
            "name": "automation environment approval",
            "status": "ok" if getattr(args, "reviewer_approved_automation_environment", False) else "pending",
            "detail": "reviewer approved real-transfer, automation, and automation-run gate env values in plist",
        },
        {
            "name": "no-bootstrap approval",
            "status": "ok" if getattr(args, "reviewer_approved_no_bootstrap", False) else "pending",
            "detail": "reviewer approved writing plist only; no bootout/bootstrap in this gate",
        },
        {
            "name": "public executor plist target",
            "status": "ok" if _path_is_relative_to(plist_path, expected_root) else "pending",
            "detail": str(plist_path),
        },
        {
            "name": "automation plist gate env",
            "status": "ok" if plist_gate_open else "pending",
            "detail": f"{plist_gate_env}={plist_gate_value}",
        },
        {
            "name": "automation interval",
            "status": "ok" if interval > 0 else "pending",
            "detail": f"StartInterval={interval}",
        },
        {
            "name": "automation batch limit",
            "status": "ok" if max_records > 0 else "pending",
            "detail": f"max-records={max_records}",
        },
        {
            "name": "automation batch limit",
            "status": "ok" if max_records > 0 else "pending",
            "detail": f"max-records={max_records}",
        },
        {
            "name": "parallel dangerous gates",
            "status": "ok",
            "detail": "launchctl execution, normal sync/resync, listing cache operations, and autosync launchd changes stay blocked",
        },
    ]
    approval_status = "complete" if all(check["status"] == "ok" for check in checks) else "pending"
    if interval <= 0:
        issues.append(
            ConfigIssue(
                key=f"PCLOUD_TOOLS_{service.name.upper()}_LAUNCHD_AUTOMATION_INTERVAL",
                level="error",
                message="public executor launchd StartInterval must be a positive number of seconds",
            )
        )
    if max_records <= 0:
        issues.append(
            ConfigIssue(
                key=f"PCLOUD_TOOLS_{service.name.upper()}_LAUNCHD_AUTOMATION_BATCH_LIMIT",
                level="error",
                message="public executor launchd --max-records must be a positive integer",
            )
        )
    if execute and paths.dev_mode:
        issues.append(
            ConfigIssue(
                key=f"PCLOUD_TOOLS_{service.name.upper()}_LAUNCHD_AUTOMATION_PLIST_RUNTIME",
                level="error",
                message=f"{service.name} launchd automation-plist --execute must use the public non-dev pcloud-manager runtime",
            )
        )
    if execute and approval_status != "complete":
        issues.append(
            ConfigIssue(
                key=f"PCLOUD_TOOLS_{service.name.upper()}_LAUNCHD_AUTOMATION_PLIST_APPROVAL",
                level="error",
                message=f"{service.name} automation plist write requires shadow report, reviews, and {plist_gate_env}={plist_gate_value}",
            )
        )
    if execute and not _path_is_relative_to(plist_path, expected_root):
        issues.append(
            ConfigIssue(
                key=f"PCLOUD_TOOLS_{service.name.upper()}_LAUNCHD_AUTOMATION_PLIST_PATH",
                level="error",
                message=f"refusing to write {service.name} automation plist outside {expected_root}: {plist_path}",
            )
        )
    issues = sort_issues(issues)
    details: dict[str, object] = {
        "execute": "yes" if execute else "no",
        "planned action": f"{'write' if execute else 'preview'} {service.name} public real-transfer queue executor LaunchAgent plist",
        "implementation status": "public automation plist gate; launchctl is not executed",
        "automation gate status": "open" if approval_status == "complete" else "closed",
        "automation command status": "implemented-gated",
        "public executor plist can write": "yes" if approval_status == "complete" else "no",
        "service label": label,
        "plist path": str(plist_path),
        "plist status": "present" if plist_path.exists() else "missing",
        "plist payload": payload,
        "program arguments": payload.get("ProgramArguments", []),
        "environment variables": payload.get("EnvironmentVariables", {}),
        "transfer timeout seconds": _PUBLIC_REAL_TRANSFER_TIMEOUT_SECONDS,
        "transfer timeout policy": (
            "public real-transfer automation uses a long wall-clock guard; "
            "rclone is still responsible for stalled I/O detection"
        ),
        "working directory": payload.get("WorkingDirectory", "-"),
        "standard out path": payload.get("StandardOutPath", "-"),
        "standard error path": payload.get("StandardErrorPath", "-"),
        "run at load": payload.get("RunAtLoad", False),
        "keep alive": payload.get("KeepAlive", False),
        "start interval seconds": payload.get("StartInterval", "-"),
        "automation batch limit": max_records,
        "automation gate summary": gate_report.summary,
        "automation gate details": gate_report.details,
        "preflight checks": checks,
        "approval status": approval_status,
        "automation plist gate env var": plist_gate_env,
        "automation plist gate accepted value": plist_gate_value,
        "state writes": "public automation LaunchAgent plist only" if execute and not has_errors(issues) else "none",
        "public plist writes": "yes" if execute and not has_errors(issues) else "no",
        "launchctl execution": "no",
        "automatic real transfer execution": "no",
        "normal sync/resync": "no",
        "listing cache operations": "no",
        "blocked operations": [
            "launchctl enable",
            "launchctl bootstrap",
            "launchctl bootout",
            "launchctl disable",
            "normal sync/resync",
            "listing cache operations",
            "autosync launchd changes",
        ],
        "next human check trigger": "terminal review before public automation plist write or reload",
    }
    if execute and not has_errors(issues):
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        plist_path.write_bytes(plistlib.dumps(payload, sort_keys=True))
        details["plist status"] = "written"
    return CommandReport(
        command=f"{service.name} launchd automation-plist",
        status=status_from_issues(issues),
        summary=(
            f"{service.name} launchd automation plist written"
            if execute and not has_errors(issues)
            else f"{service.name} launchd automation plist is gated"
        ),
        details=details,
        issues=report_issues(issues),
        actions=_service_actions(paths, service),
    )


def _service_launchd_automation_reload_report(
    args: argparse.Namespace,
    paths: RuntimePaths,
    service: ServiceDefinition,
) -> CommandReport:
    execute = bool(getattr(args, "execute", False))
    plist_report = _service_launchd_automation_plist_report(args, paths, service)
    issues: list[ConfigIssue] = []
    shadow_check, shadow_issues = _shadow_report_check(
        getattr(args, "report_path", None),
        issue_key=f"PCLOUD_TOOLS_{service.name.upper()}_LAUNCHD_AUTOMATION_RELOAD_SHADOW_REPORT",
    )
    issues.extend(shadow_issues)
    launchctl_bin = _command_v("launchctl")
    launchctl = launchctl_bin or "launchctl"
    label = _service_public_executor_launchd_label(service)
    plist_path = _service_public_launchd_plist_path(label)
    target = f"gui/{os.getuid()}/{label}"
    planned_commands = [
        [launchctl, "bootout", target],
        [launchctl, "bootstrap", f"gui/{os.getuid()}", str(plist_path)],
    ]
    rollback_commands = [
        [launchctl, "bootout", target],
        [launchctl, "disable", target],
    ]
    operational_status, operational_detail = _public_executor_plist_operational_status(plist_path, service)
    reload_gate_env = _service_launchd_automation_reload_gate_env(service)
    reload_gate_value = _service_launchd_automation_reload_gate_value(service)
    reload_gate_open = os.environ.get(reload_gate_env) == reload_gate_value
    checks = [
        shadow_check,
        {
            "name": "launchctl binary",
            "status": "ok" if launchctl_bin else "pending",
            "detail": launchctl_bin or "launchctl not found by command -v",
        },
        {
            "name": "operational automation plist",
            "status": "ok" if operational_status == "operational" else "pending",
            "detail": operational_detail,
        },
        {
            "name": "automation plist review",
            "status": "ok" if getattr(args, "operator_reviewed_automation_plist", False) else "pending",
            "detail": "operator reviewed public executor plist before launchd reload",
        },
        {
            "name": "bootout/bootstrap approval",
            "status": "ok" if getattr(args, "reviewer_approved_bootout_bootstrap", False) else "pending",
            "detail": "reviewer approved bootout then bootstrap for this executor service only",
        },
        {
            "name": "rollback policy approval",
            "status": "ok" if getattr(args, "reviewer_approved_rollback_policy", False) else "pending",
            "detail": "reviewer approved disabling only this executor service on failure",
        },
        {
            "name": "automation reload gate env",
            "status": "ok" if reload_gate_open else "pending",
            "detail": f"{reload_gate_env}={reload_gate_value}",
        },
        {
            "name": "parallel dangerous gates",
            "status": "ok",
            "detail": "normal sync/resync, listing cache operations, and autosync launchd changes stay blocked",
        },
    ]
    approval_status = "complete" if all(check["status"] == "ok" for check in checks) else "pending"
    if execute and paths.dev_mode:
        issues.append(
            ConfigIssue(
                key=f"PCLOUD_TOOLS_{service.name.upper()}_LAUNCHD_AUTOMATION_RELOAD_RUNTIME",
                level="error",
                message=f"{service.name} launchd automation-reload --execute must use the public non-dev pcloud-manager runtime",
            )
        )
    if execute and approval_status != "complete":
        issues.append(
            ConfigIssue(
                key=f"PCLOUD_TOOLS_{service.name.upper()}_LAUNCHD_AUTOMATION_RELOAD_APPROVAL",
                level="error",
                message=f"{service.name} automation reload requires operational plist, shadow report, approvals, launchctl, and {reload_gate_env}={reload_gate_value}",
            )
        )
    issues = sort_issues(issues)
    launchctl_results: list[dict[str, object]] = []
    details: dict[str, object] = {
        "execute": "yes" if execute else "no",
        "planned action": f"{'run' if execute else 'preview'} {service.name} public real-transfer queue executor launchd reload",
        "implementation status": "guarded automation launchd reload path" if execute else "automation launchd reload preview only; launchctl is not executed",
        "automation gate status": "open" if approval_status == "complete" else "closed",
        "automation command status": "implemented-gated",
        "launchd can reload": "yes" if approval_status == "complete" else "no",
        "service label": label,
        "plist path": str(plist_path),
        "plist status": "present" if plist_path.exists() else "missing",
        "operational automation plist status": operational_status,
        "automation plist summary": plist_report.summary,
        "planned launchctl commands": planned_commands,
        "rollback command examples": rollback_commands,
        "preflight checks": checks,
        "approval status": approval_status,
        "automation reload gate env var": reload_gate_env,
        "automation reload gate accepted value": reload_gate_value,
        "state writes": "launchctl automation reload only" if execute and not has_errors(issues) else "none",
        "public plist writes": "no",
        "launchctl execution": "yes" if execute and not has_errors(issues) else "no",
        "automatic real transfer execution": "yes-if-bootstrap-succeeds-and-queue-has-records" if execute and not has_errors(issues) else "no",
        "normal sync/resync": "no",
        "listing cache operations": "no",
        "blocked operations": [
            "normal sync/resync",
            "listing cache operations",
            "autosync launchd changes",
        ],
        "next human check trigger": "terminal review before public automation launchd reload",
    }
    if execute and not has_errors(issues):
        launchctl_results = _run_launchctl_commands(planned_commands, tolerate_missing_bootout=True)
        details["launchctl results"] = launchctl_results
        failed = [
            result
            for result in launchctl_results
            if result.get("returncode") != 0 and not result.get("tolerated")
        ]
        if failed:
            issues.append(
                ConfigIssue(
                    key=f"PCLOUD_TOOLS_{service.name.upper()}_LAUNCHD_AUTOMATION_RELOAD_LAUNCHCTL",
                    level="error",
                    message=f"{service.name} automation launchd reload failed: {failed[0].get('stderr') or failed[0].get('stdout')}",
                )
            )
            issues = sort_issues(issues)
    return CommandReport(
        command=f"{service.name} launchd automation-reload",
        status=status_from_issues(issues),
        summary=(
            f"{service.name} launchd automation reload completed"
            if execute and not has_errors(issues)
            else f"{service.name} launchd automation reload is gated"
        ),
        details=details,
        issues=report_issues(issues),
        actions=_service_actions(paths, service),
    )

def _service_launchd_review_report(
    paths: RuntimePaths,
    service: ServiceDefinition,
) -> CommandReport:
    load_result = load_config(paths)
    config = load_result.config
    issues = list(load_result.issues)
    public_entrypoint = _command_v("pcloud-manager") or "pcloud-manager"
    if public_entrypoint == "pcloud-manager":
        issues.append(
            ConfigIssue(
                key=f"PCLOUD_TOOLS_{service.name.upper()}_PUBLIC_ENTRYPOINT",
                level="warning",
                message="pcloud-manager was not found by command -v; review the public wrapper path before plist write",
            )
        )
    label = _service_public_launchd_label(service)
    plist_path = _service_public_launchd_plist_path(label)
    payload = _service_launchd_plist_payload(paths, service, label=label, entrypoint=public_entrypoint)
    foreground_command = (
        [public_entrypoint, "pushd", "fswatch", "resident-run", "--json"]
        if service.name == "pushd"
        else [public_entrypoint, "diffd", "api-poll", "long-poll-run", "--json"]
    )
    review_commands = [
        [public_entrypoint, service.name, "launchd", "review"],
        [public_entrypoint, service.name, "launchd", "plist", "--public-write"],
        foreground_command,
    ]
    details: dict[str, object] = {
        "planned action": f"review {service.name} public launchd plist and foreground daemon command",
        "implementation status": "read-only human review bundle; plist is not written and launchctl is not executed",
        "human review status": "required-in-terminal-before-plist-write-or-registration",
        "human gate status": "required-before-public-launchd-write-or-registration",
        "state writes": "none",
        "launchctl execution": "no",
        "persistent daemon start": "no",
        "automatic transfer execution": "no",
        "normal sync/resync": "no",
        "listing cache operations": "no",
        "dev mode": "on" if paths.dev_mode else "off",
        "runtime workspace root": str(paths.workspace_root),
        "runtime state dir": str(config.state_dir / service.name),
        "service label": label,
        "plist path": str(plist_path),
        "plist status": "present" if plist_path.exists() else "missing",
        "plist payload": payload,
        "program arguments": payload.get("ProgramArguments", []),
        "working directory": payload.get("WorkingDirectory", "-"),
        "standard out path": payload.get("StandardOutPath", "-"),
        "standard error path": payload.get("StandardErrorPath", "-"),
        "run at load": payload.get("RunAtLoad", False),
        "keep alive": payload.get("KeepAlive", False),
        "foreground command preview": foreground_command,
        "terminal review commands": review_commands,
        "next blocked step": "human must review terminal output before plist write or launchd registration",
        "blocked operations": [
            "writing public LaunchAgent plist",
            "launchctl enable",
            "launchctl bootstrap",
            "launchctl bootout",
            "launchctl disable",
            "starting persistent daemon",
            "automatic upload/download transfer execution",
            "normal sync/resync",
            "listing cache operations",
        ],
    }
    issues.append(
        ConfigIssue(
            key=f"PCLOUD_TOOLS_{service.name.upper()}_LAUNCHD_HUMAN_REVIEW",
            level="warning",
            message=f"{service.name} launchd plist/foreground command still requires human terminal review",
        )
    )
    issues = sort_issues(issues)
    return CommandReport(
        command=f"{service.name} launchd review",
        status=status_from_issues(issues),
        summary=f"{service.name} launchd human review is required",
        details=details,
        issues=report_issues(issues),
        actions=_service_actions(paths, service),
    )


def _service_launchd_registration_checks(
    args: argparse.Namespace,
    *,
    service: ServiceDefinition,
    launchctl_bin: str | None,
    plist_path: Path,
) -> tuple[list[dict[str, str]], list[ConfigIssue]]:
    checks: list[dict[str, str]] = []
    issues: list[ConfigIssue] = []
    shadow_check, shadow_issues = _shadow_report_check(
        getattr(args, "report_path", None),
        issue_key=f"PCLOUD_TOOLS_{service.name.upper()}_LAUNCHD_REGISTER_SHADOW_REPORT",
    )
    checks.append(shadow_check)
    issues.extend(shadow_issues)
    launchd_gate = validate_gate(_service_launchd_gate_spec(service), args, os.environ)
    launchd_gate_env = launchd_gate.spec.env_var
    launchd_gate_value = launchd_gate.spec.expected_value
    launchd_gate_open = launchd_gate.env_ok
    checks.extend(
        [
            {
                "name": "launchctl binary",
                "status": "ok" if launchctl_bin else "pending",
                "detail": launchctl_bin or "launchctl not found by command -v",
            },
            {
                "name": "public plist present",
                "status": "ok" if plist_path.exists() else "pending",
                "detail": str(plist_path),
            },
            {
                "name": "daemon command review",
                "status": "ok" if launchd_gate.flag_ok("--operator-reviewed-daemon-command") else "pending",
                "detail": "operator reviewed the foreground daemon command",
            },
            {
                "name": "plist policy approval",
                "status": "ok" if launchd_gate.flag_ok("--reviewer-approved-plist-policy") else "pending",
                "detail": "reviewer approved public plist label, path, logs, working directory, and ProgramArguments",
            },
            {
                "name": "launchctl policy approval",
                "status": "ok" if launchd_gate.flag_ok("--reviewer-approved-launchctl-policy") else "pending",
                "detail": "reviewer approved launchctl enable/bootstrap behavior",
            },
            {
                "name": "rollback policy approval",
                "status": "ok" if launchd_gate.flag_ok("--reviewer-approved-rollback-policy") else "pending",
                "detail": "reviewer approved bootout/disable rollback order",
            },
            {
                "name": "launchd execution gate env",
                "status": "ok" if launchd_gate_open else "pending",
                "detail": f"{launchd_gate_env}={launchd_gate_value}",
            },
            {
                "name": "parallel dangerous gates",
                "status": "ok",
                "detail": "real transfer execution, normal sync/resync, listing cache operations, and autosync launchd changes stay out of scope",
            },
        ]
    )
    return checks, issues


def _launchctl_bootout_missing_is_tolerable(command: list[str], result: subprocess.CompletedProcess[str]) -> bool:
    if len(command) < 2 or command[1] != "bootout" or result.returncode == 0:
        return False
    output = f"{result.stdout}\n{result.stderr}"
    return result.returncode == 3 and "No such process" in output


def _launchctl_bootstrap_io_error_is_retryable(
    command: list[str],
    result: subprocess.CompletedProcess[str],
) -> bool:
    if len(command) < 2 or command[1] != "bootstrap" or result.returncode == 0:
        return False
    output = f"{result.stdout}\n{result.stderr}"
    return result.returncode == 5 and "Input/output error" in output


def _run_launchctl_commands(
    commands: list[list[str]],
    *,
    tolerate_missing_bootout: bool = False,
    retry_bootstrap_io_error: bool = False,
    retry_delay_seconds: float = 0.5,
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for command in commands:
        result = subprocess.run(command, check=False, capture_output=True, text=True)
        tolerated = tolerate_missing_bootout and _launchctl_bootout_missing_is_tolerable(command, result)
        retryable = retry_bootstrap_io_error and _launchctl_bootstrap_io_error_is_retryable(command, result)
        results.append(
            {
                "command": shell_command(command),
                "argv": command,
                "returncode": result.returncode,
                "stdout": result.stdout[:2000],
                "stderr": result.stderr[:2000],
                "tolerated": tolerated or retryable,
                "tolerance reason": (
                    "service was not loaded before bootstrap"
                    if tolerated
                    else "bootstrap returned Input/output error; retrying once"
                    if retryable
                    else "-"
                ),
                "retry": "scheduled" if retryable else "no",
            }
        )
        if retryable:
            time.sleep(retry_delay_seconds)
            retry_result = subprocess.run(command, check=False, capture_output=True, text=True)
            results.append(
                {
                    "command": shell_command(command),
                    "argv": command,
                    "returncode": retry_result.returncode,
                    "stdout": retry_result.stdout[:2000],
                    "stderr": retry_result.stderr[:2000],
                    "tolerated": False,
                    "tolerance reason": "-",
                    "retry": "attempted",
                }
            )
            if retry_result.returncode != 0:
                break
            continue
        if result.returncode != 0 and not tolerated:
            break
    return results


def _service_launchd_register_report(
    args: argparse.Namespace,
    paths: RuntimePaths,
    service: ServiceDefinition,
) -> CommandReport:
    load_result = load_config(paths)
    issues = list(load_result.issues)
    execute = bool(getattr(args, "execute", False))
    launchctl_bin = _command_v("launchctl")
    label = _service_launchd_label(paths, service)
    plist_path = _service_launchd_plist_path(paths, label)
    target = f"gui/{os.getuid()}/{label}"
    planned_commands = [
        [launchctl_bin or "launchctl", "enable", target],
        [launchctl_bin or "launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path)],
    ]
    rollback_commands = [
        [launchctl_bin or "launchctl", "bootout", target],
        [launchctl_bin or "launchctl", "disable", target],
    ]
    checks, check_issues = _service_launchd_registration_checks(
        args, service=service, launchctl_bin=launchctl_bin, plist_path=plist_path
    )
    issues.extend(check_issues)
    approval_status = "complete" if all(check["status"] == "ok" for check in checks) else "pending"
    if execute and paths.dev_mode:
        issues.append(
            ConfigIssue(
                key=f"PCLOUD_TOOLS_{service.name.upper()}_LAUNCHD_REGISTER_RUNTIME",
                level="error",
                message=f"{service.name} launchd register --execute must use the public non-dev pcloud-manager runtime",
            )
        )
    if execute and approval_status != "complete":
        issues.append(
            ConfigIssue(
                key=f"PCLOUD_TOOLS_{service.name.upper()}_LAUNCHD_REGISTER_APPROVAL",
                level="error",
                message=f"{service.name} launchd register requires saved shadow validation, plist, launchctl, approvals, and gate env",
            )
        )
    launchctl_results: list[dict[str, object]] = []
    details: dict[str, object] = {
        "execute": "yes" if execute else "no",
        "planned action": f"{'run' if execute else 'preview'} {service.name} launchd registration",
        "implementation status": (
            "guarded launchctl registration path"
            if execute
            else "launchd registration preview only; launchctl is not executed"
        ),
        "launchd gate status": "open" if approval_status == "complete" else "closed",
        "launchd can register": "yes" if approval_status == "complete" else "no",
        "service label": label,
        "plist path": str(plist_path),
        "plist status": "present" if plist_path.exists() else "missing",
        "launchctl availability": "available" if launchctl_bin else "missing",
        "launchctl binary": launchctl_bin or "-",
        "planned launchctl commands": planned_commands,
        "rollback command examples": rollback_commands,
        "preflight checks": checks,
        "approval status": approval_status,
        "state writes": "launchctl registration only" if execute and not has_errors(sort_issues(issues)) else "none",
        "launchctl execution": "yes" if execute and not has_errors(sort_issues(issues)) else "no",
        "persistent daemon start": "yes-if-bootstrap-succeeds" if execute and not has_errors(sort_issues(issues)) else "no",
        "automatic transfer execution": "no",
        "normal sync/resync": "no",
        "listing cache operations": "no",
        "success policy": "verify launchd status, logs, and queue-only behavior immediately after registration",
        "failure policy": "stop on first launchctl failure and use rollback commands only after operator review",
        "blocked operations": [
            "automatic upload/download transfer execution",
            "normal sync/resync",
            "listing cache operations",
            "autosync launchd changes",
        ],
    }
    issues = sort_issues(issues)
    if execute and not has_errors(issues):
        launchctl_results = _run_launchctl_commands(planned_commands, retry_bootstrap_io_error=True)
        details["launchctl results"] = launchctl_results
        failed = [
            result for result in launchctl_results
            if result.get("returncode") != 0 and not result.get("tolerated")
        ]
        if failed:
            issues.append(
                ConfigIssue(
                    key=f"PCLOUD_TOOLS_{service.name.upper()}_LAUNCHD_REGISTER_LAUNCHCTL",
                    level="error",
                    message=f"{service.name} launchd registration failed: {failed[0].get('stderr') or failed[0].get('stdout')}",
                )
            )
            issues = sort_issues(issues)
    return CommandReport(
        command=f"{service.name} launchd register",
        status=status_from_issues(issues),
        summary=(
            f"{service.name} launchd registration completed"
            if execute and not has_errors(issues)
            else f"{service.name} launchd registration is gated"
        ),
        details=details,
        issues=report_issues(issues),
        actions=_service_actions(paths, service),
    )


def _pushd_launchd_resident_program_arguments(entrypoint: str, report_path: Path | None) -> list[str]:
    args = [
        entrypoint,
        "pushd",
        "fswatch",
        "resident-run",
        "--operator-reviewed-probe",
        "--reviewer-approved-queue-policy",
        "--reviewer-approved-process-policy",
        "--execute",
    ]
    if report_path is not None:
        args.extend(["--report-path", str(report_path.expanduser().resolve())])
    return args


def _diffd_launchd_long_poll_program_arguments(entrypoint: str, report_path: Path | None) -> list[str]:
    args = [
        entrypoint,
        "diffd",
        "api-poll",
        "long-poll-run",
        "--operator-reviewed-preview",
        "--reviewer-approved-response-policy",
        "--reviewer-approved-credential-policy",
        "--reviewer-approved-process-policy",
        "--live-api",
        "--max-iterations",
        "1",
        "--execute",
    ]
    if report_path is not None:
        args.extend(["--report-path", str(report_path.expanduser().resolve())])
    return args


def _service_launchd_operational_plist_payload(
    paths: RuntimePaths,
    service: ServiceDefinition,
    *,
    label: str,
    entrypoint: str,
    report_path: Path | None,
    start_interval_seconds: int | None = None,
) -> dict[str, object]:
    if service.name == "pushd":
        program_arguments = _pushd_launchd_resident_program_arguments(entrypoint, report_path)
        environment = {
            "PATH": _LAUNCHD_RESIDENT_PATH,
            GATES["pushd.fswatch.resident"].env_var: GATES["pushd.fswatch.resident"].expected_value,
        }
    else:
        program_arguments = _diffd_launchd_long_poll_program_arguments(entrypoint, report_path)
        environment = {
            "PATH": _LAUNCHD_RESIDENT_PATH,
            GATES["diffd.api.long-poll"].env_var: GATES["diffd.api.long-poll"].expected_value,
        }
    payload: dict[str, object] = {
        "Label": label,
        "ProgramArguments": program_arguments,
        "EnvironmentVariables": environment,
        "RunAtLoad": True,
        "KeepAlive": False,
        "WorkingDirectory": str(paths.workspace_root),
        "StandardOutPath": str(paths.log_dir / f"{service.name}-launchd.out"),
        "StandardErrorPath": str(paths.log_dir / f"{service.name}-launchd.err"),
    }
    if service.name == "diffd" and start_interval_seconds is not None:
        payload["StartInterval"] = start_interval_seconds
    return payload


def _pushd_launchd_resident_plist_report(
    args: argparse.Namespace,
    paths: RuntimePaths,
    service: ServiceDefinition,
) -> CommandReport:
    execute = bool(getattr(args, "execute", False))
    load_result = load_config(paths)
    issues = list(load_result.issues)
    report_path = getattr(args, "report_path", None)
    service_upper = service.name.upper()
    shadow_check, shadow_issues = _shadow_report_check(
        report_path,
        issue_key=f"PCLOUD_TOOLS_{service_upper}_LAUNCHD_RESIDENT_PLIST_SHADOW_REPORT",
    )
    issues.extend(shadow_issues)
    entrypoint = _command_v("pcloud-manager") or "pcloud-manager"
    fswatch_bin = _command_v("fswatch")
    api_credential = _pcloud_api_credential(load_result.config) if service.name == "diffd" else None
    label = _service_public_launchd_label(service)
    plist_path = _service_public_launchd_plist_path(label)
    expected_root = Path.home() / "Library" / "LaunchAgents"
    resident_plist_gate_name = (
        "pushd.launchd.resident-plist"
        if service.name == "pushd"
        else "diffd.launchd.long-poll-plist"
    )
    resident_gate = validate_gate(GATES[resident_plist_gate_name], args, os.environ)
    resident_gate_env = resident_gate.spec.env_var
    resident_gate_value = resident_gate.spec.expected_value
    resident_gate_open = resident_gate.env_ok
    start_interval_seconds = getattr(args, "start_interval_seconds", None)
    if start_interval_seconds is not None and start_interval_seconds <= 0:
        issues.append(
            ConfigIssue(
                key=f"PCLOUD_TOOLS_{service_upper}_LAUNCHD_START_INTERVAL",
                level="error",
                message="launchd StartInterval must be a positive number of seconds",
            )
        )
    if service.name == "pushd" and start_interval_seconds is not None:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_PUSHD_LAUNCHD_START_INTERVAL",
                level="error",
                message="pushd fswatch resident plist does not support StartInterval; fswatch is event-driven",
            )
        )
    effective_start_interval = (
        start_interval_seconds
        if service.name == "diffd" and start_interval_seconds is not None and start_interval_seconds > 0
        else None
    )
    payload = _service_launchd_operational_plist_payload(
        paths,
        service,
        label=label,
        entrypoint=entrypoint,
        report_path=report_path,
        start_interval_seconds=effective_start_interval,
    )
    service_specific_checks = (
        [
            {
                "name": "fswatch binary in resident PATH",
                "status": "ok" if fswatch_bin else "pending",
                "detail": fswatch_bin or "fswatch not found by command -v",
            },
            {
                "name": "resident command review",
                "status": "ok" if resident_gate.flag_ok("--operator-reviewed-resident-command") else "pending",
                "detail": "operator reviewed resident ProgramArguments with --execute and approval flags",
            },
            {
                "name": "resident environment approval",
                "status": "ok" if resident_gate.flag_ok("--reviewer-approved-resident-environment") else "pending",
                "detail": "reviewer approved PATH and PCLOUD_TOOLS_PUSHD_FSWATCH_RESIDENT_GATE in plist EnvironmentVariables",
            },
        ]
        if service.name == "pushd"
        else [
            {
                "name": "API credential available",
                "status": "ok" if api_credential and api_credential.token else "pending",
                "detail": api_credential.source_detail if api_credential else "missing",
            },
            {
                "name": "long-poll command review",
                "status": "ok" if resident_gate.flag_ok("--operator-reviewed-resident-command") else "pending",
                "detail": "operator reviewed one-shot live API ProgramArguments with --live-api --max-iterations 1 --execute",
            },
            {
                "name": "long-poll environment approval",
                "status": "ok" if resident_gate.flag_ok("--reviewer-approved-resident-environment") else "pending",
                "detail": "reviewer approved PATH and PCLOUD_TOOLS_DIFFD_API_LONG_POLL_GATE in plist EnvironmentVariables",
            },
        ]
    )
    checks = [
        shadow_check,
        {
            "name": "public pcloud-manager wrapper",
            "status": "ok" if entrypoint != "pcloud-manager" else "pending",
            "detail": entrypoint,
        },
        *service_specific_checks,
        {
            "name": "no-bootstrap approval",
            "status": "ok" if resident_gate.flag_ok("--reviewer-approved-no-bootstrap") else "pending",
            "detail": "reviewer approved writing plist only; no bootout/bootstrap in this gate",
        },
        {
            "name": "resident plist gate env",
            "status": "ok" if resident_gate_open else "pending",
            "detail": f"{resident_gate_env}={resident_gate_value}",
        },
        {
            "name": "parallel dangerous gates",
            "status": "ok",
            "detail": "upload/download transfer, normal sync/resync, listing cache operations, and launchctl changes stay out of scope",
        },
    ]
    approval_status = "complete-read-only" if all(check["status"] == "ok" for check in checks) else "pending"
    if execute and paths.dev_mode:
        issues.append(
            ConfigIssue(
                key=f"PCLOUD_TOOLS_{service_upper}_LAUNCHD_RESIDENT_PLIST_RUNTIME",
                level="error",
                message=f"{service.name} launchd resident-plist --execute must use the public non-dev pcloud-manager runtime",
            )
        )
    if execute and approval_status != "complete-read-only":
        issues.append(
            ConfigIssue(
                key=f"PCLOUD_TOOLS_{service_upper}_LAUNCHD_RESIDENT_PLIST_APPROVAL",
                level="error",
                message=f"{service.name} launchd resident-plist requires shadow report, approvals, dependencies, and gate env",
            )
        )
    if execute and not _path_is_relative_to(plist_path, expected_root):
        issues.append(
            ConfigIssue(
                key=f"PCLOUD_TOOLS_{service_upper}_LAUNCHD_RESIDENT_PLIST_PATH",
                level="error",
                message=f"refusing to write {service.name} operational plist outside {expected_root}: {plist_path}",
            )
        )
    issues = sort_issues(issues)
    details: dict[str, object] = {
        "execute": "yes" if execute else "no",
        "planned action": f"{'write' if execute else 'preview'} {service.name} operational LaunchAgent plist",
        "implementation status": "operational launchd plist gate; launchctl is not executed",
        "resident plist gate status": "open" if approval_status == "complete-read-only" else "closed",
        "resident plist can write": "yes" if approval_status == "complete-read-only" else "no",
        "service label": label,
        "plist path": str(plist_path),
        "plist status": "present" if plist_path.exists() else "missing",
        "plist payload": payload,
        "resident program arguments": payload.get("ProgramArguments", []),
        "environment variables": payload.get("EnvironmentVariables", {}),
        "working directory": payload.get("WorkingDirectory", "-"),
        "standard out path": payload.get("StandardOutPath", "-"),
        "standard error path": payload.get("StandardErrorPath", "-"),
        "run at load": payload.get("RunAtLoad", False),
        "keep alive": payload.get("KeepAlive", False),
        "start interval seconds": payload.get("StartInterval", "-"),
        "preflight checks": checks,
        "approval status": approval_status,
        "state writes": "public launchd resident plist only" if execute and not has_errors(issues) else "none",
        "launchctl execution": "no",
        "persistent daemon start": "no",
        "automatic transfer execution": "no",
        "normal sync/resync": "no",
        "listing cache operations": "no",
        "blocked operations": [
            "launchctl bootout",
            "launchctl bootstrap",
            "starting persistent daemon",
            "automatic upload/download transfer execution",
            "normal sync/resync",
            "listing cache operations",
        ],
        "next human check trigger": "explicit request to reload launchd with operational plist",
    }
    if execute and not has_errors(issues):
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        plist_path.write_bytes(plistlib.dumps(payload, sort_keys=True))
        details["plist status"] = "written"
    return CommandReport(
        command=f"{service.name} launchd resident-plist",
        status=status_from_issues(issues),
        summary=(
            f"{service.name} launchd resident plist written"
            if execute and not has_errors(issues)
            else f"{service.name} launchd resident plist is gated"
        ),
        details=details,
        issues=report_issues(issues),
        actions=_service_actions(paths, service),
    )


def _resident_plist_operational_status(plist_path: Path, service: ServiceDefinition) -> tuple[str, str]:
    if not plist_path.exists():
        return "missing", str(plist_path)
    try:
        payload = plistlib.loads(plist_path.read_bytes())
    except (OSError, plistlib.InvalidFileException) as exc:
        return "invalid", str(exc)
    args = payload.get("ProgramArguments", [])
    env = payload.get("EnvironmentVariables", {})
    if service.name == "pushd":
        required_args = {
            "--operator-reviewed-probe",
            "--reviewer-approved-queue-policy",
            "--reviewer-approved-process-policy",
            "--execute",
        }
        if not isinstance(args, list) or not required_args.issubset(set(map(str, args))):
            return "not-operational", "resident ProgramArguments are missing required execution/approval flags"
        resident_spec = GATES["pushd.fswatch.resident"]
        if not isinstance(env, dict) or env.get(resident_spec.env_var) != resident_spec.expected_value:
            return "not-operational", "resident EnvironmentVariables are missing the fswatch resident gate"
        if "/opt/homebrew/bin" not in str(env.get("PATH", "")):
            return "not-operational", "resident PATH does not include /opt/homebrew/bin"
        return "operational", "resident execution args and environment are present"
    required_args = {
        "--operator-reviewed-preview",
        "--reviewer-approved-response-policy",
        "--reviewer-approved-credential-policy",
        "--reviewer-approved-process-policy",
        "--live-api",
        "--execute",
    }
    if not isinstance(args, list) or not required_args.issubset(set(map(str, args))):
        return "not-operational", "long-poll ProgramArguments are missing required execution/approval flags"
    if "--max-iterations" not in args or "1" not in args:
        return "not-operational", "long-poll ProgramArguments must bound live API execution to one iteration"
    long_poll_spec = GATES["diffd.api.long-poll"]
    if not isinstance(env, dict) or env.get(long_poll_spec.env_var) != long_poll_spec.expected_value:
        return "not-operational", "long-poll EnvironmentVariables are missing the API long-poll gate"
    if "/opt/homebrew/bin" not in str(env.get("PATH", "")):
        return "not-operational", "long-poll PATH does not include /opt/homebrew/bin"
    return "operational", "long-poll one-shot execution args and environment are present"


def _public_executor_plist_operational_status(plist_path: Path, service: ServiceDefinition) -> tuple[str, str]:
    if not plist_path.exists():
        return "missing", str(plist_path)
    try:
        payload = plistlib.loads(plist_path.read_bytes())
    except (OSError, plistlib.InvalidFileException) as exc:
        return "invalid", str(exc)
    args = payload.get("ProgramArguments", [])
    env = payload.get("EnvironmentVariables", {})
    required_args = {
        service.name,
        "transfer",
        "automation-run",
        "--execute",
        "--consume-on-success",
        "--json",
    }
    if not isinstance(args, list) or not required_args.issubset(set(map(str, args))):
        return "not-operational", "automation ProgramArguments are missing required execution args"
    if "--report-path" not in args:
        return "not-operational", "automation ProgramArguments are missing --report-path"
    if "--max-records" not in args:
        return "not-operational", "automation ProgramArguments are missing --max-records"
    try:
        max_records = int(str(args[args.index("--max-records") + 1]))
    except (ValueError, IndexError):
        return "not-operational", "automation ProgramArguments have invalid --max-records"
    if max_records <= 0:
        return "not-operational", "automation ProgramArguments must use positive --max-records"
    if not isinstance(env, dict):
        return "not-operational", "automation EnvironmentVariables are missing"
    if env.get("PCLOUD_TOOLS_REAL_TRANSFER_EXECUTION_GATE") != _REAL_TRANSFER_EXECUTION_GATE_VALUE:
        return "not-operational", "automation EnvironmentVariables are missing the real transfer gate"
    if env.get("PCLOUD_TOOLS_REAL_TRANSFER_AUTOMATION_GATE") != _REAL_TRANSFER_AUTOMATION_GATE_VALUE:
        return "not-operational", "automation EnvironmentVariables are missing the automation gate"
    if env.get("PCLOUD_TOOLS_REAL_TRANSFER_AUTOMATION_RUN_GATE") != _REAL_TRANSFER_AUTOMATION_RUN_GATE_VALUE:
        return "not-operational", "automation EnvironmentVariables are missing the automation-run gate"
    if "/opt/homebrew/bin" not in str(env.get("PATH", "")):
        return "not-operational", "automation PATH does not include /opt/homebrew/bin"
    return "operational", "automation execution args and environment are present"


def _pushd_launchd_reload_report(
    args: argparse.Namespace,
    paths: RuntimePaths,
    service: ServiceDefinition,
) -> CommandReport:
    execute = bool(getattr(args, "execute", False))
    load_result = load_config(paths)
    issues = list(load_result.issues)
    service_upper = service.name.upper()
    shadow_check, shadow_issues = _shadow_report_check(
        getattr(args, "report_path", None),
        issue_key=f"PCLOUD_TOOLS_{service_upper}_LAUNCHD_RELOAD_SHADOW_REPORT",
    )
    issues.extend(shadow_issues)
    label = _service_public_launchd_label(service)
    plist_path = _service_public_launchd_plist_path(label)
    target = f"gui/{os.getuid()}/{label}"
    launchctl_bin = _command_v("launchctl")
    launchctl = launchctl_bin or "launchctl"
    planned_commands = [
        [launchctl, "bootout", target],
        [launchctl, "bootstrap", f"gui/{os.getuid()}", str(plist_path)],
    ]
    rollback_commands = [
        [launchctl, "bootout", target],
        [launchctl, "disable", target],
    ]
    operational_status, operational_detail = _resident_plist_operational_status(plist_path, service)
    reload_gate = validate_gate(GATES[f"{service.name}.launchd.reload"], args, os.environ)
    reload_gate_env = reload_gate.spec.env_var
    reload_gate_value = reload_gate.spec.expected_value
    bootout_bootstrap_approved = reload_gate.flag_ok("--reviewer-approved-bootout-bootstrap")
    rollback_policy_approved = reload_gate.flag_ok("--reviewer-approved-rollback-policy")
    reload_gate_open = reload_gate.env_ok
    checks = [
        shadow_check,
        {
            "name": "launchctl binary",
            "status": "ok" if launchctl_bin else "pending",
            "detail": launchctl_bin or "launchctl not found by command -v",
        },
        {
            "name": "operational resident plist",
            "status": "ok" if operational_status == "operational" else "pending",
            "detail": operational_detail,
        },
        {
            "name": "resident plist review",
            "status": "ok" if getattr(args, "operator_reviewed_resident_plist", False) else "pending",
            "detail": "operator reviewed operational resident plist before launchd reload",
        },
        {
            "name": "bootout/bootstrap approval",
            "status": "ok" if bootout_bootstrap_approved else "pending",
            "detail": "reviewer approved bootout then bootstrap for this service only",
        },
        {
            "name": "rollback policy approval",
            "status": "ok" if rollback_policy_approved else "pending",
            "detail": "reviewer approved rollback command order",
        },
        {
            "name": "reload gate env",
            "status": "ok" if reload_gate_open else "pending",
            "detail": f"{reload_gate_env}={reload_gate_value}",
        },
        {
            "name": "parallel dangerous gates",
            "status": "ok",
            "detail": "upload/download transfer, normal sync/resync, listing cache operations, and autosync launchd changes stay out of scope",
        },
    ]
    approval_status = "complete" if all(check["status"] == "ok" for check in checks) else "pending"
    if execute and paths.dev_mode:
        issues.append(
            ConfigIssue(
                key=f"PCLOUD_TOOLS_{service_upper}_LAUNCHD_RELOAD_RUNTIME",
                level="error",
                message=f"{service.name} launchd reload --execute must use the public non-dev pcloud-manager runtime",
            )
        )
    if execute and approval_status != "complete":
        issues.append(
            ConfigIssue(
                key=f"PCLOUD_TOOLS_{service_upper}_LAUNCHD_RELOAD_APPROVAL",
                level="error",
                message=f"{service.name} launchd reload requires operational plist, shadow report, approvals, launchctl, and reload gate env",
            )
        )
    issues = sort_issues(issues)
    launchctl_results: list[dict[str, object]] = []
    details: dict[str, object] = {
        "execute": "yes" if execute else "no",
        "planned action": f"{'run' if execute else 'preview'} {service.name} launchd bootout/bootstrap reload",
        "implementation status": (
            "guarded launchd reload path"
            if execute
            else "launchd reload preview only; launchctl is not executed"
        ),
        "reload gate status": "open" if approval_status == "complete" else "closed",
        "launchd can reload": "yes" if approval_status == "complete" else "no",
        "service label": label,
        "plist path": str(plist_path),
        "resident plist status": operational_status,
        "launchctl availability": "available" if launchctl_bin else "missing",
        "launchctl binary": launchctl_bin or "-",
        "planned launchctl commands": planned_commands,
        "rollback command examples": rollback_commands,
        "preflight checks": checks,
        "approval status": approval_status,
        "state writes": "launchctl reload only" if execute and not has_errors(issues) else "none",
        "launchctl execution": "yes" if execute and not has_errors(issues) else "no",
        "persistent daemon start": "yes-if-bootstrap-succeeds" if execute and not has_errors(issues) else "no",
        "automatic transfer execution": "no",
        "normal sync/resync": "no",
        "listing cache operations": "no",
        "success policy": "verify launchd status, logs, queued records/cursor behavior, and no transfer execution",
        "failure policy": "stop on first launchctl failure and keep queue records for manual review",
        "blocked operations": [
            "automatic upload/download transfer execution",
            "normal sync/resync",
            "listing cache operations",
            "autosync launchd changes",
        ],
    }
    if execute and not has_errors(issues):
        launchctl_results = _run_launchctl_commands(planned_commands, retry_bootstrap_io_error=True)
        details["launchctl results"] = launchctl_results
        failed = [
            result for result in launchctl_results
            if result.get("returncode") != 0 and not result.get("tolerated")
        ]
        if failed:
            issues.append(
                ConfigIssue(
                    key=f"PCLOUD_TOOLS_{service_upper}_LAUNCHD_RELOAD_LAUNCHCTL",
                    level="error",
                    message=f"{service.name} launchd reload failed: {failed[0].get('stderr') or failed[0].get('stdout')}",
                )
            )
            issues = sort_issues(issues)
    return CommandReport(
        command=f"{service.name} launchd reload",
        status=status_from_issues(issues),
        summary=(
            f"{service.name} launchd reload completed"
            if execute and not has_errors(issues)
            else f"{service.name} launchd reload is gated"
        ),
        details=details,
        issues=report_issues(issues),
        actions=_service_actions(paths, service),
    )


def _service_launchd_gate_report(
    args: argparse.Namespace,
    paths: RuntimePaths,
    service: ServiceDefinition,
) -> CommandReport:
    load_result = load_config(paths)
    config = load_result.config
    issues = list(load_result.issues)
    shadow_check, shadow_issues = _shadow_report_check(
        getattr(args, "report_path", None),
        issue_key=f"PCLOUD_TOOLS_{service.name.upper()}_LAUNCHD_SHADOW_REPORT",
    )
    issues.extend(shadow_issues)
    launchctl_bin = _command_v("launchctl")
    if not launchctl_bin:
        issues.append(
            ConfigIssue(
                key=f"PCLOUD_TOOLS_{service.name.upper()}_LAUNCHCTL",
                level="warning",
                message=f"launchctl was not found by command -v; {service.name} launchd gate remains closed",
            )
        )
    entrypoint = action_entrypoint_command(paths)
    label = _service_launchd_label(paths, service)
    plist_path = _service_launchd_plist_path(paths, label)
    target = f"gui/<uid>/{label}"
    launchctl = launchctl_bin or "launchctl"
    payload = _service_launchd_plist_payload(paths, service, label=label, entrypoint=entrypoint)
    launchd_gate = validate_gate(_service_launchd_gate_spec(service), args, os.environ)
    daemon_preview = (
        [entrypoint, "pushd", "fswatch", "resident-run", "--json"]
        if service.name == "pushd"
        else [entrypoint, "diffd", "api-poll", "long-poll-run", "--json"]
    )
    checks = [
        shadow_check,
        {
            "name": "launchctl binary",
            "status": "ok" if launchctl_bin else "pending",
            "detail": launchctl_bin or "launchctl not found by command -v; verify before launchd changes",
        },
        {
            "name": "daemon command preview",
            "status": "ok" if launchd_gate.flag_ok("--operator-reviewed-daemon-command") else "pending",
            "detail": "operator reviewed the foreground daemon command before any launchd wrapper",
        },
        {
            "name": "plist policy approval",
            "status": "ok" if launchd_gate.flag_ok("--reviewer-approved-plist-policy") else "pending",
            "detail": "reviewer approved label, plist path, logs, working directory, and ProgramArguments",
        },
        {
            "name": "launchctl policy approval",
            "status": "ok" if launchd_gate.flag_ok("--reviewer-approved-launchctl-policy") else "pending",
            "detail": "reviewer approved bootstrap/bootout/enable/disable behavior before registration",
        },
        {
            "name": "rollback policy approval",
            "status": "ok" if launchd_gate.flag_ok("--reviewer-approved-rollback-policy") else "pending",
            "detail": "reviewer approved rollback command order and stop conditions",
        },
        {
            "name": "parallel dangerous gates",
            "status": "ok",
            "detail": "real transfer execution, normal sync/resync, listing cache operations, and autosync launchd changes stay out of scope",
        },
    ]
    approval_status = "complete-read-only" if all(check["status"] == "ok" for check in checks) else "pending"
    issues.append(
        ConfigIssue(
            key=f"PCLOUD_TOOLS_{service.name.upper()}_LAUNCHD_GATE",
            level="warning",
            message=f"{service.name} launchd registration remains gated; this command is read-only",
        )
    )
    details: dict[str, object] = {
        "planned action": f"check {service.name} launchd registration prerequisites",
        "implementation status": "read-only launchd gate scaffold; plist is not written and launchctl is not executed",
        "launchd gate status": "closed",
        "launchd can register": "no",
        "operator verification required": "yes-before-launchd-registration",
        "human gate status": "required-before-launchd-registration",
        "human gate reason": "launchd registration would make daemon execution persistent",
        "state writes": "none",
        "dev mode": "on" if paths.dev_mode else "off",
        "workspace root": str(paths.workspace_root),
        "state dir": str(config.state_dir / service.name),
        "service label": label,
        "plist path": str(plist_path),
        "plist status": "draft-only; not written by this command",
        "plist payload draft": payload,
        "launchctl availability": "available" if launchctl_bin else "missing",
        "launchctl binary": launchctl_bin or "-",
        "daemon command preview": daemon_preview,
        "bootstrap command examples": [
            [launchctl, "enable", target],
            [launchctl, "bootstrap", "gui/<uid>", str(plist_path)],
        ],
        "rollback command examples": [
            [launchctl, "bootout", target],
            [launchctl, "disable", target],
        ],
        "future launchd gate env var": launchd_gate.spec.env_var,
        "future launchd gate accepted value": launchd_gate.spec.expected_value,
        "approval status": approval_status,
        "preflight checks": checks,
        "success policy": "register only after an explicit operator command and verify foreground daemon behavior first",
        "failure policy": "stop on launchctl/plist error and retain existing queue/change records for manual review",
        "rollback policy": "use displayed bootout/disable commands; do not run sync/resync or transfer cleanup automatically",
        "blocked operations": [
            "writing LaunchAgent plist",
            "launchctl enable",
            "launchctl bootstrap",
            "launchctl bootout",
            "launchctl disable",
            "starting persistent daemon",
            "automatic upload/download transfer execution",
            "normal sync/resync",
            "listing cache operations",
        ],
        "next human check trigger": "explicit request to write plist or register launchd service",
    }
    issues = sort_issues(issues)
    return CommandReport(
        command=f"{service.name} launchd gate",
        status=status_from_issues(issues),
        summary=f"{service.name} launchd gate is closed",
        details=details,
        issues=report_issues(issues),
        actions=_service_actions(paths, service),
    )


def _launchctl_print_result(launchctl_bin: str | None, target: str) -> dict[str, object]:
    if not launchctl_bin:
        return {
            "launchd loaded": "unknown",
            "registration status": "unknown-launchctl-missing",
            "launchctl print returncode": None,
            "launchctl print stdout": "",
            "launchctl print stderr": "",
        }
    result = subprocess.run(
        [launchctl_bin, "print", target],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    stdout = result.stdout.strip()
    stderr = result.stderr.strip()
    combined = f"{stdout}\n{stderr}".lower()
    if result.returncode == 0:
        loaded = "yes"
        registration_status = "loaded"
    elif "could not find service" in combined or "service is not loaded" in combined:
        loaded = "no"
        registration_status = "not_loaded"
    else:
        loaded = "unknown"
        registration_status = "unknown-launchctl-print-failed"
    return {
        "launchd loaded": loaded,
        "registration status": registration_status,
        "launchctl print returncode": result.returncode,
        "launchctl print stdout": stdout[:2000],
        "launchctl print stderr": stderr[:2000],
    }


def _service_launchd_status_report(
    paths: RuntimePaths,
    service: ServiceDefinition,
) -> CommandReport:
    load_result = load_config(paths)
    issues = list(load_result.issues)
    launchctl_bin = _command_v("launchctl")
    if not launchctl_bin:
        issues.append(
            ConfigIssue(
                key=f"PCLOUD_TOOLS_{service.name.upper()}_LAUNCHCTL",
                level="warning",
                message=f"launchctl was not found by command -v; {service.name} launchd status is limited",
            )
        )
    label = _service_launchd_label(paths, service)
    plist_path = _service_launchd_plist_path(paths, label)
    target = f"gui/{os.getuid()}/{label}"
    print_result = _launchctl_print_result(launchctl_bin, target)
    details: dict[str, object] = {
        "planned action": f"inspect {service.name} launchd registration status",
        "implementation status": "read-only launchd status surface; launchctl print only",
        "state writes": "none",
        "launchctl execution": "print-only" if launchctl_bin else "none",
        "launchd gate status": "closed",
        "launchd can register": "no",
        "operator verification required": "no",
        "human gate status": "required-before-launchd-registration",
        "dev mode": "on" if paths.dev_mode else "off",
        "workspace root": str(paths.workspace_root),
        "service label": label,
        "plist path": str(plist_path),
        "plist status": "present" if plist_path.exists() else "missing",
        "launchctl availability": "available" if launchctl_bin else "missing",
        "launchctl binary": launchctl_bin or "-",
        "launchctl print target": target,
        "launchctl print command": [launchctl_bin or "launchctl", "print", target],
        "blocked operations": [
            "writing LaunchAgent plist",
            "launchctl enable",
            "launchctl bootstrap",
            "launchctl bootout",
            "launchctl disable",
            "starting persistent daemon",
            "automatic upload/download transfer execution",
            "normal sync/resync",
            "listing cache operations",
        ],
        "next human check trigger": "explicit request to write plist or register launchd service",
    }
    details.update(print_result)
    issues = sort_issues(issues)
    return CommandReport(
        command=f"{service.name} launchd status",
        status=status_from_issues(issues),
        summary=f"{service.name} launchd status is {details['registration status']}",
        details=details,
        issues=report_issues(issues),
        actions=_service_actions(paths, service),
    )


def cmd_service_launchd(
    args: argparse.Namespace,
    paths: RuntimePaths,
    service: ServiceDefinition,
) -> int | None:
    if args.launchd_command == "status":
        report = _service_launchd_status_report(paths, service)
        _print_service_launchd_status_report(report, args)
        return exit_code_for_report(report)
    if args.launchd_command == "review":
        report = _service_launchd_review_report(paths, service)
        _print_service_launchd_review_report(report, args)
        return exit_code_for_report(report)
    if args.launchd_command == "register":
        report = _service_launchd_register_report(args, paths, service)
        _print_service_launchd_register_report(report, args)
        return exit_code_for_report(report)
    if args.launchd_command == "reload":
        report = _pushd_launchd_reload_report(args, paths, service)
        _print_service_launchd_reload_report(report, args)
        return exit_code_for_report(report)
    if args.launchd_command == "resident-plist":
        report = _pushd_launchd_resident_plist_report(args, paths, service)
        _print_service_launchd_resident_plist_report(report, args)
        return exit_code_for_report(report)
    if args.launchd_command == "executor-plist":
        report = _service_launchd_executor_plist_report(args, paths, service)
        _print_service_launchd_plist_report(report, args)
        return exit_code_for_report(report)
    if args.launchd_command == "automation-plist":
        report = _service_launchd_automation_plist_report(args, paths, service)
        _print_service_launchd_plist_report(report, args)
        return exit_code_for_report(report)
    if args.launchd_command == "automation-reload":
        report = _service_launchd_automation_reload_report(args, paths, service)
        _print_service_launchd_reload_report(report, args)
        return exit_code_for_report(report)
    if args.launchd_command == "plist":
        report = _service_launchd_plist_report(args, paths, service)
        _print_service_launchd_plist_report(report, args)
        return exit_code_for_report(report)
    if args.launchd_command == "gate":
        report = _service_launchd_gate_report(args, paths, service)
        _print_service_launchd_gate_report(report, args)
        return exit_code_for_report(report)
    return None


def _pushd_run_report(args: argparse.Namespace, paths: RuntimePaths) -> CommandReport:
    load_result = load_config(paths)
    state = read_service_daemon_state(load_result.config, "pushd")
    plan, scope = build_pushd_plan(load_result.config, state)
    execute = getattr(args, "execute", False)
    issues = list(load_result.issues) + list(state.issues) + list(plan.issues) + scope_issues(scope)
    details: dict[str, object] = {
        "planned action": "record pushd dry-run state" if execute else "preview pushd dry run",
        "run mode": "dry-run",
        "plan summary": _pushd_plan_summary(plan),
        "planned uploads": plan.upload_count,
        "missing local upload records": len(
            _split_missing_local_upload_records(load_result.config, plan.upload_records)[1]
        ),
        "excluded queue items": plan.excluded_count,
        "invalid queue items": plan.invalid_count,
        "last plan file": str(state.last_plan_file),
        "last event file": str(state.last_event_file),
        "cursor file": str(state.cursor_file),
    }

    if execute:
        dev_issue = _dev_execute_issue(paths, load_result.config, "pushd run")
        if dev_issue:
            issues.append(dev_issue)
        if not has_errors(issues):
            result = record_dry_run_state(
                state=state,
                service_name="pushd",
                plan_summary=_pushd_plan_summary(plan),
                counts={
                    "planned_uploads": plan.upload_count,
                    "excluded_queue_items": plan.excluded_count,
                    "invalid_queue_items": plan.invalid_count,
                },
                records={
                    "planned_uploads": record_payloads(plan.upload_records),
                    "excluded_queue": record_payloads(plan.excluded_records),
                    "invalid_queue": record_payloads(plan.invalid_records),
                },
            )
            details["recorded cursor"] = result.cursor

    summary = "pushd dry-run recorded" if execute and not has_errors(issues) else "pushd run preview is ready"
    if has_errors(issues):
        summary = "pushd run cannot be recorded until issues are resolved"
    issues = sort_issues(issues)
    return CommandReport(
        command="pushd run",
        status=status_from_issues(issues),
        summary=summary,
        details=details,
        issues=report_issues(issues),
        actions=_service_actions(paths, _SERVICES["pushd"]),
    )


def _diffd_run_report(args: argparse.Namespace, paths: RuntimePaths) -> CommandReport:
    load_result = load_config(paths)
    state = read_service_daemon_state(load_result.config, "diffd")
    daemon_state = read_daemon_state(load_result.config)
    plan = build_diffd_plan(load_result.config, state, daemon_state)
    execute = getattr(args, "execute", False)
    issues = list(load_result.issues) + list(state.issues) + list(daemon_state.issues) + list(plan.issues)
    details: dict[str, object] = {
        "planned action": "record diffd dry-run state" if execute else "preview diffd dry run",
        "run mode": "dry-run",
        "plan summary": _diffd_plan_summary(plan),
        "remote changes": plan.remote_change_count,
        "pending downloads": plan.pending_download_count,
        "planned downloads": plan.download_count,
        "skipped download records": plan.skipped_count,
        "daemon diffid": daemon_state.diffid,
        "last plan file": str(state.last_plan_file),
        "last event file": str(state.last_event_file),
        "cursor file": str(state.cursor_file),
    }

    if execute:
        dev_issue = _dev_execute_issue(paths, load_result.config, "diffd run")
        if dev_issue:
            issues.append(dev_issue)
        if not has_errors(issues):
            result = record_dry_run_state(
                state=state,
                service_name="diffd",
                plan_summary=_diffd_plan_summary(plan),
                counts={
                    "remote_changes": plan.remote_change_count,
                    "pending_downloads": plan.pending_download_count,
                    "planned_downloads": plan.download_count,
                    "skipped_download_records": plan.skipped_count,
                },
                records={
                    "remote_changes": record_payloads(plan.remote_change_records),
                    "pending_downloads": record_payloads(plan.pending_download_records),
                },
            )
            details["recorded cursor"] = result.cursor

    summary = "diffd dry-run recorded" if execute and not has_errors(issues) else "diffd run preview is ready"
    if has_errors(issues):
        summary = "diffd run cannot be recorded until issues are resolved"
    issues = sort_issues(issues)
    return CommandReport(
        command="diffd run",
        status=status_from_issues(issues),
        summary=summary,
        details=details,
        issues=report_issues(issues),
        actions=_service_actions(paths, _SERVICES["diffd"]),
    )


def cmd_service_run(
    args: argparse.Namespace, paths: RuntimePaths, service: ServiceDefinition
) -> int:
    report = _pushd_run_report(args, paths) if service.name == "pushd" else _diffd_run_report(args, paths)
    print_report(report, args)
    return exit_code_for_report(report)


def _invalid_fswatch_records(invalid_events) -> tuple[PlanRecord, ...]:
    return tuple(
        PlanRecord(path="", action="upload", reason=f"fswatch fixture: {event.reason}")
        for event in invalid_events
        if event.reason != "blank or comment"
    )


def _invalid_fswatch_details(invalid_events) -> list[dict[str, str]]:
    return [
        {"raw": event.raw, "reason": event.reason}
        for event in invalid_events
        if event.reason != "blank or comment"
    ]


def _pushd_fswatch_report(args: argparse.Namespace, paths: RuntimePaths) -> CommandReport:
    load_result = load_config(paths)
    fixture = Path(args.fixture)
    scope = sync_allowlist_info(load_result.config)
    issues = list(load_result.issues) + scope_issues(scope)
    details: dict[str, object] = {
        "planned action": "preview pushd fswatch fixture",
        "implementation status": "fixture parser only; fswatch process is not started",
        "fixture file": str(fixture),
        "gate status": "closed",
    }

    try:
        parsed = parse_fswatch_fixture(fixture)
    except OSError as exc:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_PUSHD_FSWATCH_FIXTURE",
                level="error",
                message=f"cannot read fswatch fixture {fixture}: {exc}",
            )
        )
        issues = sort_issues(issues)
        return CommandReport(
            command="pushd fswatch preview",
            status=status_from_issues(issues),
            summary="pushd fswatch fixture cannot be previewed",
            details=details,
            issues=report_issues(issues),
            actions=_service_actions(paths, _SERVICES["pushd"]),
        )

    event_records = fswatch_events_to_records(parsed.events)
    invalid_records = _invalid_fswatch_records(parsed.invalid)
    plan = build_pushd_plan_from_records(
        load_result.config,
        parsed.source,
        (*event_records, *invalid_records),
        total=len(parsed.events) + len(invalid_records),
    )
    issues.extend(plan.issues)
    issues = sort_issues(issues)
    details.update(
        {
            "parsed fswatch events": len(parsed.events),
            "invalid fswatch events": len(invalid_records),
            "invalid fswatch records": _invalid_fswatch_details(parsed.invalid),
            **_pushd_plan_details(load_result.config, plan, scope),
        }
    )
    return CommandReport(
        command="pushd fswatch preview",
        status=status_from_issues(issues),
        summary="pushd fswatch fixture preview is ready",
        details=details,
        issues=report_issues(issues),
        actions=_service_actions(paths, _SERVICES["pushd"]),
    )


def _pushd_fswatch_probe_command(config: AppConfig, fswatch_bin: str) -> tuple[str, ...]:
    return (
        fswatch_bin,
        "--one-event",
        "--recursive",
        "--event-flag-separator",
        ",",
        str(config.core_dir),
    )


def _pushd_fswatch_resident_command(config: AppConfig, fswatch_bin: str) -> tuple[str, ...]:
    return (
        fswatch_bin,
        "--recursive",
        "--event-flag-separator",
        ",",
        str(config.core_dir),
    )


def _pushd_fswatch_probe_report(paths: RuntimePaths) -> CommandReport:
    load_result = load_config(paths)
    issues = list(load_result.issues)
    fswatch_bin = shutil.which("fswatch")
    if fswatch_bin:
        command = _pushd_fswatch_probe_command(load_result.config, fswatch_bin)
        availability = "available"
    else:
        command = _pushd_fswatch_probe_command(load_result.config, "fswatch")
        availability = "missing"
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_PUSHD_FSWATCH_BIN",
                level="warning",
                message="fswatch was not found on PATH; probe remains preview-only",
            )
        )

    details: dict[str, object] = {
        "planned action": "preview pushd one-shot fswatch probe",
        "implementation status": "probe preview only; fswatch process is not started",
        "gate status": "closed",
        "allowed work": "command preview only",
        "watch root": str(load_result.config.core_dir),
        "fswatch availability": availability,
        "fswatch command": list(command),
        "state writes": "none",
    }
    issues = sort_issues(issues)
    return CommandReport(
        command="pushd fswatch probe",
        status=status_from_issues(issues),
        summary="pushd fswatch one-shot probe preview is ready",
        details=details,
        issues=report_issues(issues),
        actions=_service_actions(paths, _SERVICES["pushd"]),
    )


def _command_v(command_name: str) -> str | None:
    result = subprocess.run(
        ["/bin/sh", "-c", f"command -v {shlex.quote(command_name)}"],
        check=False,
        capture_output=True,
        text=True,
    )
    found = result.stdout.strip()
    return found or None


def _pushd_fswatch_resident_gate_report(args: argparse.Namespace, paths: RuntimePaths) -> CommandReport:
    load_result = load_config(paths)
    scope = sync_allowlist_info(load_result.config)
    baseline_label = f"{scope.baseline.mode} ({scope.baseline.status})"
    issues = list(load_result.issues) + scope_issues(scope)
    shadow_check, shadow_issues = _shadow_report_check(
        getattr(args, "report_path", None),
        issue_key="PCLOUD_TOOLS_PUSHD_FSWATCH_SHADOW_REPORT",
    )
    issues.extend(shadow_issues)
    fswatch_bin = _command_v("fswatch")
    fswatch_check = {
        "name": "fswatch binary",
        "status": "ok" if fswatch_bin else "pending",
        "detail": fswatch_bin or "fswatch not found by command -v; install/verify before resident mode",
    }
    if not fswatch_bin:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_PUSHD_FSWATCH_BIN",
                level="warning",
                message="fswatch was not found by command -v; resident gate remains closed",
            )
        )
    resident_command = _pushd_fswatch_resident_command(load_result.config, fswatch_bin or "fswatch")
    resident_gate = validate_gate(GATES["pushd.fswatch.resident"], args, os.environ)
    checks = [
        shadow_check,
        fswatch_check,
        {
            "name": "watch scope",
            "status": "ok" if scope.allowlist_status == "loaded" else "pending",
            "detail": f"{baseline_label}; entries={scope.allowlist_count}; root={load_result.config.core_dir}",
        },
        {
            "name": "operator probe review",
            "status": "ok" if resident_gate.flag_ok("--operator-reviewed-probe") else "pending",
            "detail": "operator reviewed pushd fswatch probe output and watch root",
        },
        {
            "name": "queue policy approval",
            "status": "ok" if resident_gate.flag_ok("--reviewer-approved-queue-policy") else "pending",
            "detail": "reviewer approved event-to-queue append policy and manual review handling",
        },
        {
            "name": "process lifecycle approval",
            "status": "ok" if resident_gate.flag_ok("--reviewer-approved-process-policy") else "pending",
            "detail": "reviewer approved timeout, restart, log, and cleanup behavior before resident start",
        },
        {
            "name": "parallel dangerous gates",
            "status": "ok",
            "detail": "launchd registration, pCloud API long-poll, normal sync/resync, and archive work stay out of scope",
        },
    ]
    approval_status = "complete-read-only" if all(check["status"] == "ok" for check in checks) else "pending"
    details: dict[str, object] = {
        "planned action": "check pushd fswatch resident gate prerequisites",
        "implementation status": "read-only checklist; fswatch resident process is not started",
        "resident gate status": "closed",
        "resident can start": "no",
        "operator verification required": "yes-before-resident-gate",
        "human gate status": "required-before-resident-start",
        "human gate reason": "fswatch resident mode would start a long-running local watcher",
        "state writes": "none",
        "watch root": str(load_result.config.core_dir),
        "allowlist": str(load_result.config.allowlist_file),
        "scope status": scope.allowlist_status,
        "scope baseline": baseline_label,
        "scope entries": scope.allowlist_count,
        "fswatch availability": "available" if fswatch_bin else "missing",
        "fswatch binary": fswatch_bin or "-",
        "resident command preview": list(resident_command),
        "resident approval status": approval_status,
        "preflight checks": checks,
        "blocked operations": [
            "starting fswatch resident process",
            "writing pushd queue records from live fswatch events",
            "launchd registration",
            "real upload execution from resident events",
        ],
        "next human check trigger": "explicit resident start implementation or launchd integration",
    }
    issues.append(
        ConfigIssue(
            key="PCLOUD_TOOLS_PUSHD_FSWATCH_RESIDENT_GATE",
            level="warning",
            message="pushd fswatch resident mode remains gated; this command is read-only",
        )
    )
    issues = sort_issues(issues)
    return CommandReport(
        command="pushd fswatch resident-gate",
        status=status_from_issues(issues),
        summary="pushd fswatch resident gate is closed",
        details=details,
        issues=report_issues(issues),
        actions=_service_actions(paths, _SERVICES["pushd"]),
    )


def _resident_run_state_file(config: AppConfig) -> Path:
    return config.state_dir / "pushd" / "fswatch-resident-last-run.json"


def _write_resident_run_state(state_file: Path, payload: dict[str, object]) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(state_file, payload, sort_keys=True)


def _normalize_resident_fswatch_line(line: str, root: Path) -> str:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return line
    if "\t" in stripped:
        path_part, flags_part = stripped.split("\t", 1)
    else:
        path_part, flags_part = stripped, ""
    candidate = Path(path_part).expanduser()
    if candidate.is_absolute():
        try:
            relative = candidate.resolve().relative_to(root.resolve())
        except OSError:
            return line
        except ValueError:
            return line
        normalized = relative.as_posix()
    else:
        normalized = normalize_plan_path(path_part)
    if not normalized:
        return line
    return f"{normalized}\t{flags_part}" if flags_part else normalized


def _resident_gate_open(config: AppConfig) -> bool:
    return config.pushd_fswatch_resident_gate == GATES["pushd.fswatch.resident"].expected_value


def _recent_resident_debounce_keys(state_file: Path, *, debounce_seconds: int, now: datetime) -> set[tuple[str, str]]:
    if debounce_seconds <= 0 or not state_file.exists():
        return set()
    try:
        payload = json.loads(state_file.read_text())
    except (OSError, json.JSONDecodeError):
        return set()
    if not isinstance(payload, dict):
        return set()
    raw_finished_at = payload.get("finished_at")
    if not isinstance(raw_finished_at, str):
        return set()
    try:
        finished_at = datetime.fromisoformat(raw_finished_at)
    except ValueError:
        return set()
    if finished_at.tzinfo is None:
        finished_at = finished_at.replace(tzinfo=timezone.utc)
    if (now - finished_at).total_seconds() > debounce_seconds:
        return set()
    records = payload.get("appended_records")
    if not isinstance(records, list):
        return set()
    keys: set[tuple[str, str]] = set()
    for item in records:
        if not isinstance(item, dict):
            continue
        path = normalize_plan_path(item.get("path", ""))
        action = str(item.get("action", "") or "")
        if path and action:
            keys.add((path, action))
    return keys


def _pushd_fswatch_resident_run_report(args: argparse.Namespace, paths: RuntimePaths) -> CommandReport:
    gate_report = _pushd_fswatch_resident_gate_report(args, paths)
    load_result = load_config(paths)
    config = load_result.config
    execute = bool(getattr(args, "execute", False))
    max_events = getattr(args, "max_events", None)
    details = dict(gate_report.details)
    issues = [
        ConfigIssue(key=issue.key, level=issue.level, message=issue.message)
        for issue in gate_report.issues
        if issue.key != "PCLOUD_TOOLS_PUSHD_FSWATCH_RESIDENT_GATE"
    ]
    resident_command = [str(part) for part in details.get("resident command preview", [])]
    approval_status = str(details.get("resident approval status", "pending"))
    gate_open = _resident_gate_open(config)
    resident_spec = GATES["pushd.fswatch.resident"]
    state_file = _resident_run_state_file(config)

    details.update(
        {
            "planned action": "run pushd fswatch resident loop" if execute else "preview pushd fswatch resident run",
            "implementation status": (
                "foreground resident loop; fswatch events append pushd queue records"
                if execute
                else "resident run preview only; fswatch process is not started"
            ),
            "resident run gate status": (
                f"open: {resident_spec.expected_value}"
                if gate_open
                else f"closed: requires {resident_spec.env_var}={resident_spec.expected_value}"
            ),
            "resident can start": "yes" if gate_open and approval_status == "complete-read-only" else "no",
            "execute requested": "yes" if execute else "no",
            "state writes": "pushd queue and resident run state" if execute else "none",
            "resident state file": str(state_file),
            "future gate env": f"{resident_spec.env_var}={resident_spec.expected_value}",
            "max events": max_events if max_events is not None else "-",
            "events processed": 0,
            "queue records appended": 0,
            "duplicate events skipped": 0,
            "debounce events skipped": 0,
            "queue limit skips": 0,
            "excluded events": 0,
            "invalid events": 0,
        }
    )
    if max_events is not None and max_events < 1:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_PUSHD_FSWATCH_MAX_EVENTS",
                level="error",
                message="--max-events must be >= 1",
            )
        )
    if approval_status != "complete-read-only":
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_PUSHD_FSWATCH_RESIDENT_APPROVAL",
                level="error" if execute else "warning",
                message="resident execution requires complete read-only fswatch approvals",
            )
        )
    if not gate_open:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_PUSHD_FSWATCH_RESIDENT_EXECUTION_GATE",
                level="error" if execute else "warning",
                message=(
                    "resident execution requires "
                    f"{resident_spec.env_var}={resident_spec.expected_value!r}"
                ),
            )
        )
    if execute and not resident_command:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_PUSHD_FSWATCH_COMMAND",
                level="error",
                message="resident command is unavailable",
            )
        )

    if not execute or has_errors(issues):
        if has_errors(issues):
            details["state writes"] = "none"
        issues = sort_issues(issues)
        return CommandReport(
            command="pushd fswatch resident-run",
            status=status_from_issues(issues),
            summary=(
                "pushd fswatch resident execution is gated"
                if has_errors(issues) or not gate_open
                else "pushd fswatch resident run is ready"
            ),
            details=details,
            issues=report_issues(issues),
            actions=_service_actions(paths, _SERVICES["pushd"]),
        )

    started_at = datetime.now(timezone.utc).isoformat()
    cleanup: dict[str, object] = {"process group cleanup": "not-needed"}
    results: dict[str, object] = {
        "command": resident_command,
        "started_at": started_at,
        "max_events": max_events,
    }
    appended_records: list[dict[str, str]] = []
    duplicate_records: list[dict[str, str]] = []
    debounce_records: list[dict[str, str]] = []
    queue_limit_records: list[dict[str, str]] = []
    excluded_records: list[dict[str, str]] = []
    invalid_records: list[dict[str, str]] = []
    process: subprocess.Popen[str] | None = None
    try:
        debounce_keys = _recent_resident_debounce_keys(
            state_file,
            debounce_seconds=config.pushd_debounce_seconds,
            now=datetime.now(timezone.utc),
        )
        process = subprocess.Popen(
            resident_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        results["pid"] = process.pid
        results["status"] = "running"
        results["updated_at"] = datetime.now(timezone.utc).isoformat()
        _write_resident_run_state(state_file, results)
        events_processed = 0
        assert process.stdout is not None
        for raw_line in process.stdout:
            events_processed += 1
            normalized_line = _normalize_resident_fswatch_line(raw_line, config.core_dir)
            parsed = parse_fswatch_event_line(normalized_line)
            if isinstance(parsed, InvalidPushdEvent):
                invalid_records.append({"raw": parsed.raw, "reason": parsed.reason})
            else:
                record = fswatch_events_to_records((parsed,))[0]
                plan = build_pushd_plan_from_records(
                    config,
                    read_service_daemon_state(config, "pushd").queue_file,
                    (record,),
                    total=1,
                )
                if plan.upload_records:
                    debounce_key = (record.path, record.action)
                    if record.action == "upload" and debounce_key in debounce_keys:
                        debounce_records.append(
                            {"path": record.path, "action": record.action, "reason": "recent resident append"}
                        )
                    else:
                        update = append_plan_record_with_policy(
                            plan.queue_file,
                            "PCLOUD_TOOLS_PUSHD_QUEUE",
                            record,
                            max_records=config.pushd_queue_limit,
                        )
                        if update.issue:
                            issues.append(update.issue)
                        if update.appended:
                            appended_records.append(
                                {"path": record.path, "action": record.action, "reason": record.reason}
                            )
                            debounce_keys.add(debounce_key)
                        elif update.skipped_reason == "duplicate path/action":
                            duplicate_records.append(
                                {"path": record.path, "action": record.action, "reason": update.skipped_reason}
                            )
                        elif update.skipped_reason == "queue limit reached":
                            queue_limit_records.append(
                                {"path": record.path, "action": record.action, "reason": update.skipped_reason}
                            )
                elif plan.excluded_records:
                    excluded = plan.excluded_records[0]
                    excluded_records.append({"path": excluded.path, "action": excluded.action, "reason": excluded.reason})
                elif plan.invalid_records:
                    invalid = plan.invalid_records[0]
                    invalid_records.append({"raw": raw_line.strip(), "reason": invalid.reason})
                issues.extend(plan.issues)
            results.update(
                {
                    "status": "running",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "last_raw_event": raw_line.strip(),
                    "last_normalized_event": normalized_line.strip(),
                    "appended_records": appended_records,
                    "duplicate_records": duplicate_records,
                    "debounce_records": debounce_records,
                    "queue_limit_records": queue_limit_records,
                    "excluded_records": excluded_records,
                    "invalid_records": invalid_records,
                }
            )
            _write_resident_run_state(state_file, results)
            if max_events is not None and events_processed >= max_events:
                break
        if max_events is not None and process.poll() is None:
            cleanup = _cleanup_transfer_process_group(process)
        returncode = process.wait(timeout=_TRANSFER_CLEANUP_WAIT_SECONDS) if process.poll() is None else process.returncode
        stderr = process.stderr.read().strip() if process.stderr is not None else ""
        results["returncode"] = returncode
        results["stderr"] = stderr
    except subprocess.TimeoutExpired:
        if process is not None:
            cleanup = _cleanup_transfer_process_group(process)
            results["returncode"] = process.returncode
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_PUSHD_FSWATCH_RESIDENT_TIMEOUT",
                level="error",
                message="resident process did not stop after requested max-events cleanup",
            )
        )
    except OSError as exc:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_PUSHD_FSWATCH_RESIDENT_EXEC",
                level="error",
                message=f"resident process could not start: {exc}",
            )
        )
    finished_at = datetime.now(timezone.utc).isoformat()
    results.update(
        {
            "finished_at": finished_at,
            "cleanup": cleanup,
            "status": "failed" if has_errors(issues) else "success",
            "appended_records": appended_records,
            "duplicate_records": duplicate_records,
            "debounce_records": debounce_records,
            "queue_limit_records": queue_limit_records,
            "excluded_records": excluded_records,
            "invalid_records": invalid_records,
        }
    )
    if not has_errors(issues):
        _write_resident_run_state(state_file, results)

    details.update(
        {
            "events processed": (
                len(appended_records)
                + len(duplicate_records)
                + len(debounce_records)
                + len(queue_limit_records)
                + len(excluded_records)
                + len(invalid_records)
            ),
            "queue records appended": len(appended_records),
            "duplicate events skipped": len(duplicate_records),
            "debounce events skipped": len(debounce_records),
            "queue limit skips": len(queue_limit_records),
            "excluded events": len(excluded_records),
            "invalid events": len(invalid_records),
            "appended record details": appended_records,
            "duplicate event details": duplicate_records,
            "debounce event details": debounce_records,
            "queue limit skip details": queue_limit_records,
            "excluded event details": excluded_records,
            "invalid event details": invalid_records,
            "process result": results,
            "process group cleanup": cleanup.get("process group cleanup", "-"),
            "state writes": "pushd queue and resident run state" if not has_errors(issues) else "none",
        }
    )
    issues = sort_issues(issues)
    return CommandReport(
        command="pushd fswatch resident-run",
        status=status_from_issues(issues),
        summary="pushd fswatch resident run completed" if not has_errors(issues) else "pushd fswatch resident run failed",
        details=details,
        issues=report_issues(issues),
        actions=_service_actions(paths, _SERVICES["pushd"]),
    )


def cmd_pushd_fswatch(args: argparse.Namespace, paths: RuntimePaths) -> int | None:
    if args.fswatch_command == "preview":
        report = _pushd_fswatch_report(args, paths)
        print_report(report, args)
        return exit_code_for_report(report)
    if args.fswatch_command == "probe":
        report = _pushd_fswatch_probe_report(paths)
        print_report(report, args)
        return exit_code_for_report(report)
    if args.fswatch_command == "resident-gate":
        report = _pushd_fswatch_resident_gate_report(args, paths)
        _print_fswatch_resident_gate_report(report, args)
        return exit_code_for_report(report)
    if args.fswatch_command == "resident-run":
        report = _pushd_fswatch_resident_run_report(args, paths)
        _print_fswatch_resident_run_report(report, args)
        return exit_code_for_report(report)
    return None


def _invalid_diff_records(invalid_changes) -> tuple[PlanRecord, ...]:
    return tuple(
        PlanRecord(path="", action="download", reason=f"diff fixture: {change.reason}")
        for change in invalid_changes
    )


def _invalid_diff_details(invalid_changes) -> list[dict[str, str]]:
    return [{"raw": change.raw, "reason": change.reason} for change in invalid_changes]


def _diffd_diff_report(args: argparse.Namespace, paths: RuntimePaths) -> CommandReport:
    load_result = load_config(paths)
    fixture = Path(args.fixture)
    issues = list(load_result.issues)
    details: dict[str, object] = {
        "planned action": "preview diffd pCloud diff fixture",
        "implementation status": "fixture parser only; pCloud API is not called",
        "fixture file": str(fixture),
        "gate status": "closed",
    }

    try:
        parsed = parse_diff_response_fixture(fixture)
    except OSError as exc:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_DIFFD_DIFF_FIXTURE",
                level="error",
                message=f"cannot read pCloud diff fixture {fixture}: {exc}",
            )
        )
        issues = sort_issues(issues)
        return CommandReport(
            command="diffd diff preview",
            status=status_from_issues(issues),
            summary="diffd pCloud diff fixture cannot be previewed",
            details=details,
            issues=report_issues(issues),
            actions=_service_actions(paths, _SERVICES["diffd"]),
        )

    remote_records = diff_changes_to_records(parsed.changes)
    invalid_records = _invalid_diff_records(parsed.invalid)
    plan = build_diffd_plan_from_records(
        config=load_result.config,
        remote_changes_file=parsed.source,
        pending_downloads_file=load_result.config.state_dir / "daemon" / "pending-downloads.json",
        remote_records=(*remote_records, *invalid_records),
    )
    issues.extend(plan.issues)
    issues = sort_issues(issues)
    details.update(
        {
            "fixture diffid": parsed.diffid,
            "parsed diff changes": len(parsed.changes),
            "invalid diff changes": len(parsed.invalid),
            "invalid diff records": _invalid_diff_details(parsed.invalid),
            **_diffd_plan_details(plan),
        }
    )
    return CommandReport(
        command="diffd diff preview",
        status=status_from_issues(issues),
        summary="diffd pCloud diff fixture preview is ready",
        details=details,
        issues=report_issues(issues),
        actions=_service_actions(paths, _SERVICES["diffd"]),
    )


def cmd_diffd_diff(args: argparse.Namespace, paths: RuntimePaths) -> int | None:
    if args.diff_command == "preview":
        report = _diffd_diff_report(args, paths)
        print_report(report, args)
        return exit_code_for_report(report)
    return None


def _diffd_api_poll_report(paths: RuntimePaths) -> CommandReport:
    load_result = load_config(paths)
    daemon_state = read_daemon_state(load_result.config)
    issues = sort_issues(list(load_result.issues) + list(daemon_state.issues))
    details: dict[str, object] = {
        "planned action": "preview diffd one-shot pCloud API poll",
        "implementation status": "API poll preview only; pCloud API is not called",
        "gate status": "closed",
        "allowed work": "request-shape preview only",
        "remote root": load_result.config.core_remote,
        "current diffid": daemon_state.diffid,
        "request method": "GET",
        "request path": "/diff",
        "request query": {
            "diffid": daemon_state.diffid,
            "limit": load_result.config.diffd_batch_limit,
        },
        "required before execution": [
            "explicit operator/reviewer API gate",
            "configured pCloud API base URL",
            "configured least-privilege pCloud API credential",
            "fixture coverage for expected response shapes",
        ],
        "state writes": "none",
    }
    return CommandReport(
        command="diffd api-poll preview",
        status=status_from_issues(issues),
        summary="diffd pCloud API poll preview is ready",
        details=details,
        issues=report_issues(issues),
        actions=_service_actions(paths, _SERVICES["diffd"]),
    )


def _diffd_api_long_poll_gate_report(args: argparse.Namespace, paths: RuntimePaths) -> CommandReport:
    load_result = load_config(paths)
    daemon_state = read_daemon_state(load_result.config)
    scope = sync_allowlist_info(load_result.config)
    baseline_label = f"{scope.baseline.mode} ({scope.baseline.status})"
    issues = list(load_result.issues) + list(daemon_state.issues) + scope_issues(scope)
    shadow_check, shadow_issues = _shadow_report_check(
        getattr(args, "report_path", None),
        issue_key="PCLOUD_TOOLS_DIFFD_API_POLL_SHADOW_REPORT",
    )
    issues.extend(shadow_issues)
    entrypoint = action_entrypoint_command(paths)
    preview_command = [entrypoint, "diffd", "api-poll", "preview", "--json"]
    request_query = {
        "diffid": daemon_state.diffid,
        "limit": load_result.config.diffd_batch_limit,
    }
    long_poll_gate = validate_gate(GATES["diffd.api.long-poll"], args, os.environ)
    checks = [
        shadow_check,
        {
            "name": "API preview command",
            "status": "ok",
            "detail": " ".join(preview_command),
        },
        {
            "name": "diff cursor state",
            "status": "ok",
            "detail": f"current diffid={daemon_state.diffid}",
        },
        {
            "name": "download scope",
            "status": "ok" if scope.allowlist_status == "loaded" else "pending",
            "detail": f"{baseline_label}; entries={scope.allowlist_count}; root={load_result.config.core_dir}",
        },
        {
            "name": "operator preview review",
            "status": "ok" if long_poll_gate.flag_ok("--operator-reviewed-preview") else "pending",
            "detail": "operator reviewed diffd api-poll preview request shape",
        },
        {
            "name": "response policy approval",
            "status": "ok" if long_poll_gate.flag_ok("--reviewer-approved-response-policy") else "pending",
            "detail": "reviewer approved response parsing, skipped records, and cursor mutation policy",
        },
        {
            "name": "credential policy approval",
            "status": "ok" if long_poll_gate.flag_ok("--reviewer-approved-credential-policy") else "pending",
            "detail": "reviewer approved least-privilege credential handling before any API call",
        },
        {
            "name": "process lifecycle approval",
            "status": "ok" if long_poll_gate.flag_ok("--reviewer-approved-process-policy") else "pending",
            "detail": "reviewer approved timeout, retry, backoff, logs, and cleanup behavior before long-poll",
        },
        {
            "name": "parallel dangerous gates",
            "status": "ok",
            "detail": "fswatch resident mode, launchd registration, normal sync/resync, and archive work stay out of scope",
        },
    ]
    approval_status = "complete-read-only" if all(check["status"] == "ok" for check in checks) else "pending"
    details: dict[str, object] = {
        "planned action": "check diffd pCloud API long-poll prerequisites",
        "implementation status": "read-only checklist; pCloud API long-poll is not started",
        "long-poll gate status": "closed",
        "long-poll can start": "no",
        "operator verification required": "yes-before-api-long-poll-gate",
        "human gate status": "required-before-api-long-poll",
        "human gate reason": "pCloud API long-poll would call the live pCloud API and mutate diff cursor state",
        "state writes": "none",
        "remote root": load_result.config.core_remote,
        "current diffid": daemon_state.diffid,
        "poll interval seconds": load_result.config.diffd_poll_interval_seconds,
        "batch limit": load_result.config.diffd_batch_limit,
        "request method": "GET",
        "request path": "/diff",
        "request query": request_query,
        "preview command": preview_command,
        "scope status": scope.allowlist_status,
        "scope baseline": baseline_label,
        "scope entries": scope.allowlist_count,
        "long-poll approval status": approval_status,
        "preflight checks": checks,
        "success policy": "update diff cursor and append remote-change records only after an accepted API response",
        "failure policy": "retain current diff cursor and existing remote-change records; record failure state for retry/manual review after a gated live API attempt",
        "retry policy": "manual retry or future scheduler retry only; no automatic retry loop in this command",
        "backoff policy": f"wait at least {load_result.config.diffd_poll_interval_seconds}s before retrying a failed API attempt",
        "rollback policy": "no automatic local/remote delete or cursor rollback beyond retaining the previous cursor",
        "blocked operations": [
            "calling pCloud API /diff",
            "starting pCloud API long-poll loop",
            "mutating diff cursor from live API responses",
            "writing diffd remote-change records from live API responses",
            "launchd registration",
            "real download execution from API events",
        ],
        "next human check trigger": "explicit API long-poll implementation or launchd integration",
    }
    issues.append(
        ConfigIssue(
            key="PCLOUD_TOOLS_DIFFD_API_LONG_POLL_GATE",
            level="warning",
            message="diffd pCloud API long-poll remains gated; this command is read-only",
        )
    )
    issues = sort_issues(issues)
    return CommandReport(
        command="diffd api-poll long-poll-gate",
        status=status_from_issues(issues),
        summary="diffd pCloud API long-poll gate is closed",
        details=details,
        issues=report_issues(issues),
        actions=_service_actions(paths, _SERVICES["diffd"]),
    )


def _diffd_api_long_poll_run_state_file(config: AppConfig) -> Path:
    return config.state_dir / "diffd" / "api-long-poll-last-run.json"


def _diffd_api_checkpoint_state_file(config: AppConfig) -> Path:
    return config.state_dir / "diffd" / "api-checkpoint-last-run.json"


def _diffd_api_long_poll_lock_dir(config: AppConfig) -> Path:
    return config.state_dir / "diffd" / "api-long-poll.lock"


def _diffd_api_lock_stale_seconds(config: AppConfig) -> int:
    return max(120, int(config.pcloud_api_timeout_seconds) * 4)


def _acquire_diffd_api_lock(
    lock_dir: Path,
    *,
    issue_key: str,
    description: str,
    stale_after_seconds: int,
) -> tuple[bool, str, ConfigIssue | None]:
    try:
        lock_dir.mkdir(parents=True)
        return True, "acquired", None
    except FileExistsError:
        try:
            age_seconds = max(0, int(time.time() - lock_dir.stat().st_mtime))
        except OSError:
            age_seconds = -1
        if age_seconds >= stale_after_seconds:
            try:
                lock_dir.rmdir()
                lock_dir.mkdir(parents=True)
                return True, "acquired-after-stale-release", None
            except FileExistsError:
                pass
            except OSError as exc:
                return (
                    False,
                    "held-stale",
                    ConfigIssue(
                        key=issue_key,
                        level="error",
                        message=f"cannot release stale {description} lock {lock_dir}: {exc}",
                    ),
                )
        age_detail = (
            "unknown age"
            if age_seconds < 0
            else f"age {age_seconds}s; stale threshold {stale_after_seconds}s"
        )
        return (
            False,
            "held",
            ConfigIssue(
                key=issue_key,
                level="error",
                message=f"{description} is already running or lock is present: {lock_dir} ({age_detail})",
            ),
        )
    except OSError as exc:
        return (
            False,
            "error",
            ConfigIssue(
                key=issue_key,
                level="error",
                message=f"cannot create {description} lock {lock_dir}: {exc}",
            ),
        )


def _diffd_folder_cache_file(config: AppConfig) -> Path:
    return config.state_dir / "diffd" / "folder-cache.json"


def _core_remote_path(config: AppConfig) -> str:
    remote = str(config.core_remote)
    if ":" not in remote:
        return ""
    return normalize_plan_path(remote.split(":", 1)[1])


def _diffd_relative_remote_path(config: AppConfig, path: str) -> str:
    normalized = normalize_plan_path(path)
    root = _core_remote_path(config)
    if root and normalized == root:
        return ""
    if root and normalized.startswith(f"{root}/"):
        return normalize_plan_path(normalized[len(root) + 1 :])
    return normalized


def _read_diffd_folder_cache(config: AppConfig) -> dict[str, str]:
    path = _diffd_folder_cache_file(config)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        str(key): _diffd_relative_remote_path(config, str(value))
        for key, value in payload.items()
        if str(key) and value is not None
    }


def _write_diffd_folder_cache(config: AppConfig, folder_paths: dict[str, str]) -> None:
    path = _diffd_folder_cache_file(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, folder_paths, sort_keys=True)


def _diffd_folder_cache_entries(folder_paths: dict[str, str]) -> list[dict[str, str]]:
    return [
        {"folder_id": folder_id, "path": path}
        for folder_id, path in sorted(folder_paths.items(), key=lambda item: item[0])
    ]


def _diffd_folder_cache_report(args: argparse.Namespace, paths: RuntimePaths) -> CommandReport:
    load_result = load_config(paths)
    config = load_result.config
    cache_file = _diffd_folder_cache_file(config)
    before = _read_diffd_folder_cache(config)
    after = dict(before)
    execute = bool(getattr(args, "execute", False))
    command = str(getattr(args, "folder_cache_command", "") or "")
    issues = list(load_result.issues)
    state_writes = "none"
    details: dict[str, object] = {
        "folder cache file": str(cache_file),
        "folder cache entries before": len(before),
        "folder cache entries after": len(after),
        "state writes": state_writes,
        "entries": _diffd_folder_cache_entries(after),
    }

    if command == "status":
        summary = "diffd folder cache status is available"
        details["planned action"] = "inspect diffd folder cache"
    elif command == "add":
        folder_id = str(getattr(args, "folder_id", "") or "").strip()
        path = normalize_plan_path(getattr(args, "path", ""))
        details.update(
            {
                "planned action": "add diffd folder cache mapping" if execute else "preview add diffd folder cache mapping",
                "folder id": folder_id,
                "path": path,
                "previous path": before.get(folder_id, "-"),
            }
        )
        if not folder_id or not folder_id.isdigit():
            issues.append(
                ConfigIssue(
                    key="PCLOUD_TOOLS_DIFFD_FOLDER_CACHE_ID",
                    level="error",
                    message="folder-cache add requires a numeric pCloud folder id",
                )
            )
        if not path:
            issues.append(
                ConfigIssue(
                    key="PCLOUD_TOOLS_DIFFD_FOLDER_CACHE_PATH",
                    level="error",
                    message="folder-cache add requires a safe relative folder path",
                )
            )
        if not has_errors(issues):
            after[folder_id] = path
        summary = "diffd folder cache mapping added" if execute and not has_errors(issues) else "diffd folder cache add preview is ready"
    elif command == "remove":
        folder_id = str(getattr(args, "folder_id", "") or "").strip()
        details.update(
            {
                "planned action": (
                    "remove diffd folder cache mapping" if execute else "preview remove diffd folder cache mapping"
                ),
                "folder id": folder_id,
                "previous path": before.get(folder_id, "-"),
            }
        )
        if not folder_id or not folder_id.isdigit():
            issues.append(
                ConfigIssue(
                    key="PCLOUD_TOOLS_DIFFD_FOLDER_CACHE_ID",
                    level="error",
                    message="folder-cache remove requires a numeric pCloud folder id",
                )
            )
        if not has_errors(issues):
            after.pop(folder_id, None)
        details["folder cache entries removed"] = len(before) - len(after)
        summary = (
            "diffd folder cache mapping removed"
            if execute and not has_errors(issues)
            else "diffd folder cache remove preview is ready"
        )
    elif command == "clear":
        details["planned action"] = "clear diffd folder cache" if execute else "preview clear diffd folder cache"
        after = {}
        details["folder cache entries removed"] = len(before)
        summary = "diffd folder cache cleared" if execute and not has_errors(issues) else "diffd folder cache clear preview is ready"
    else:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_DIFFD_FOLDER_CACHE_COMMAND",
                level="error",
                message="folder-cache command must be status, add, remove, or clear",
            )
        )
        details["planned action"] = "none"
        summary = "diffd folder cache command is invalid"

    details["folder cache entries after"] = len(after)
    details["entries"] = _diffd_folder_cache_entries(after)

    if execute:
        dev_issue = _dev_execute_issue(paths, config, f"diffd folder-cache {command}")
        if dev_issue:
            issues.append(dev_issue)
        if not has_errors(issues):
            try:
                _write_diffd_folder_cache(config, after)
                state_writes = "diffd folder cache"
            except OSError as exc:
                issues.append(
                    ConfigIssue(
                        key="PCLOUD_TOOLS_DIFFD_FOLDER_CACHE",
                        level="error",
                        message=f"cannot write diffd folder cache {cache_file}: {exc}",
                    )
                )
    if has_errors(issues):
        state_writes = "none"
        if command != "status":
            summary = "diffd folder cache cannot be updated until issues are resolved"
    details["state writes"] = state_writes

    issues = sort_issues(issues)
    return CommandReport(
        command=f"diffd folder-cache {command}".strip(),
        status=status_from_issues(issues),
        summary=summary,
        details=details,
        issues=report_issues(issues),
        actions=_service_actions(paths, _SERVICES["diffd"]),
    )


def cmd_diffd_folder_cache(args: argparse.Namespace, paths: RuntimePaths) -> int:
    report = _diffd_folder_cache_report(args, paths)
    _print_diffd_folder_cache_report(report, args)
    return exit_code_for_report(report)


@dataclass(frozen=True)
class PcloudApiCredential:
    base_url: str
    auth_param: str
    token: str
    source: str
    source_detail: str


def _pcloud_api_credential(config: AppConfig) -> PcloudApiCredential:
    if config.pcloud_api_token:
        return PcloudApiCredential(
            base_url=config.pcloud_api_base_url,
            auth_param=config.pcloud_api_auth_param,
            token=config.pcloud_api_token,
            source="env/config",
            source_detail="PCLOUD_TOOLS_PCLOUD_API_TOKEN",
        )
    credentials = load_rclone_pcloud_credentials(config)
    if credentials is not None:
        return PcloudApiCredential(
            base_url=(
                credentials.hostname
                if credentials.hostname.startswith(("http://", "https://"))
                else f"https://{credentials.hostname}"
            ),
            auth_param="access_token",
            token=credentials.access_token,
            source="rclone config",
            source_detail=f"{credentials.source_path} [{credentials.remote_name}]",
        )
    return PcloudApiCredential(
        base_url=config.pcloud_api_base_url,
        auth_param=config.pcloud_api_auth_param,
        token="",
        source="missing",
        source_detail=f"PCLOUD_TOOLS_PCLOUD_API_TOKEN or {rclone_config_path()} [{config.core_remote.split(':', 1)[0]}]",
    )


def _pcloud_diff_request_url(
    config: AppConfig, credential: PcloudApiCredential, diffid: str, *, block: bool
) -> tuple[str, str]:
    query = {
        "diffid": diffid,
        "limit": str(config.diffd_batch_limit),
        credential.auth_param: credential.token,
    }
    if block:
        query["block"] = "1"
    endpoint = credential.base_url.rstrip("/") + "/diff"
    url = endpoint + "?" + urllib.parse.urlencode(query)
    redacted_query = dict(query)
    redacted_query[credential.auth_param] = "<redacted>"
    redacted_url = endpoint + "?" + urllib.parse.urlencode(redacted_query)
    return url, redacted_url


def _pcloud_diff_checkpoint_request_url(config: AppConfig, credential: PcloudApiCredential) -> tuple[str, str]:
    query = {
        "last": "0",
        credential.auth_param: credential.token,
    }
    base_url = config.pcloud_api_base_url.rstrip("/")
    encoded = urllib.parse.urlencode(query)
    url = f"{base_url}/diff?{encoded}"
    redacted = dict(query)
    redacted[credential.auth_param] = "<redacted>"
    redacted_url = f"{base_url}/diff?{urllib.parse.urlencode(redacted)}"
    return url, redacted_url


def _fetch_pcloud_diff_response(
    config: AppConfig, credential: PcloudApiCredential, diffid: str, *, block: bool
) -> tuple[str, str]:
    url, redacted_url = _pcloud_diff_request_url(config, credential, diffid, block=block)
    request = urllib.request.Request(url, headers={"User-Agent": "pcloud-tools/diffd"})
    with urllib.request.urlopen(request, timeout=config.pcloud_api_timeout_seconds) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset), redacted_url


def _fetch_pcloud_diff_checkpoint(config: AppConfig, credential: PcloudApiCredential) -> tuple[str, str]:
    url, redacted_url = _pcloud_diff_checkpoint_request_url(config, credential)
    request = urllib.request.Request(url, headers={"User-Agent": "pcloud-tools/diffd"})
    with urllib.request.urlopen(request, timeout=config.pcloud_api_timeout_seconds) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset), redacted_url


def _pcloud_listfolder_metadata_request_url(
    config: AppConfig, credential: PcloudApiCredential, folder_id: str
) -> tuple[str, str]:
    query = {
        "folderid": folder_id,
        "nofiles": "1",
        credential.auth_param: credential.token,
    }
    base_url = config.pcloud_api_base_url.rstrip("/")
    url = f"{base_url}/listfolder?{urllib.parse.urlencode(query)}"
    redacted = dict(query)
    redacted[credential.auth_param] = "<redacted>"
    return url, f"{base_url}/listfolder?{urllib.parse.urlencode(redacted)}"


def _fetch_pcloud_listfolder_metadata(
    config: AppConfig, credential: PcloudApiCredential, folder_id: str
) -> tuple[dict[str, object], str]:
    url, redacted_url = _pcloud_listfolder_metadata_request_url(config, credential, folder_id)
    request = urllib.request.Request(url, headers={"User-Agent": "pcloud-tools/diffd"})
    with urllib.request.urlopen(request, timeout=config.pcloud_api_timeout_seconds) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        payload = json.loads(response.read().decode(charset))
    metadata = payload.get("metadata") if isinstance(payload, dict) else None
    if not isinstance(metadata, dict):
        raise ValueError(f"pCloud listfolder metadata is missing for folderid {folder_id}")
    return metadata, redacted_url


def _folder_id_from_metadata(metadata: dict[str, object]) -> str:
    folder_id = metadata.get("folderid")
    if folder_id is None:
        raw_id = str(metadata.get("id", "")).strip()
        folder_id = raw_id[1:] if raw_id.startswith("d") else raw_id
    return str(folder_id if folder_id is not None else "").strip()


def _resolve_pcloud_folder_path(
    config: AppConfig,
    credential: PcloudApiCredential,
    folder_id: str,
    folder_paths: dict[str, str],
    fetched_urls: list[str],
    resolving: set[str],
) -> str:
    if folder_id in folder_paths:
        return folder_paths[folder_id]
    if not folder_id or not folder_id.isdigit() or folder_id in resolving:
        return ""
    resolving.add(folder_id)
    metadata, redacted_url = _fetch_pcloud_listfolder_metadata(config, credential, folder_id)
    fetched_urls.append(redacted_url)
    name = str(metadata.get("name", "")).strip()
    parent_id = str(metadata.get("parentfolderid", "0")).strip()
    path = ""
    if name:
        if parent_id and parent_id != "0":
            parent_path = _resolve_pcloud_folder_path(
                config, credential, parent_id, folder_paths, fetched_urls, resolving
            )
            path = normalize_plan_path(f"{parent_path}/{name}") if parent_path else normalize_plan_path(name)
        else:
            path = normalize_plan_path(name)
    path = _diffd_relative_remote_path(config, path)
    actual_folder_id = _folder_id_from_metadata(metadata) or folder_id
    folder_paths[actual_folder_id] = path
    resolving.discard(folder_id)
    return path


def _diff_parent_folder_ids(response_text: str) -> set[str]:
    try:
        payload = json.loads(response_text)
    except json.JSONDecodeError:
        return set()
    entries = payload.get("entries", payload.get("changes", payload.get("diff", []))) if isinstance(payload, dict) else []
    if not isinstance(entries, list):
        return set()
    folder_ids: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        metadata = entry.get("metadata")
        if not isinstance(metadata, dict):
            continue
        parent_id = str(metadata.get("parentfolderid", "")).strip()
        if parent_id and parent_id != "0" and parent_id.isdigit():
            folder_ids.add(parent_id)
    return folder_ids


def _resolve_diff_response_parent_folders(
    config: AppConfig,
    credential: PcloudApiCredential,
    response_text: str,
    folder_paths: dict[str, str],
) -> tuple[dict[str, str], list[str]]:
    resolved = dict(folder_paths)
    fetched_urls: list[str] = []
    for folder_id in sorted(_diff_parent_folder_ids(response_text)):
        if folder_id not in resolved:
            _resolve_pcloud_folder_path(config, credential, folder_id, resolved, fetched_urls, set())
    return resolved, fetched_urls


def _api_long_poll_gate_open(config: AppConfig) -> bool:
    return config.diffd_api_long_poll_gate == GATES["diffd.api.long-poll"].expected_value


def _diffd_api_long_poll_run_report(args: argparse.Namespace, paths: RuntimePaths) -> CommandReport:
    gate_report = _diffd_api_long_poll_gate_report(args, paths)
    load_result = load_config(paths)
    config = load_result.config
    daemon_state = read_daemon_state(config)
    state = read_service_daemon_state(config, "diffd")
    execute = bool(getattr(args, "execute", False))
    fixture = getattr(args, "fixture", None)
    live_api = bool(getattr(args, "live_api", False))
    block = bool(getattr(args, "block", False))
    max_iterations = getattr(args, "max_iterations", None)
    details = dict(gate_report.details)
    issues = [
        ConfigIssue(key=issue.key, level=issue.level, message=issue.message)
        for issue in gate_report.issues
        if issue.key != "PCLOUD_TOOLS_DIFFD_API_LONG_POLL_GATE"
    ]
    approval_status = str(details.get("long-poll approval status", "pending"))
    gate_open = _api_long_poll_gate_open(config)
    long_poll_spec = GATES["diffd.api.long-poll"]
    state_file = _diffd_api_long_poll_run_state_file(config)
    fixture_path = Path(fixture) if fixture is not None else None
    requested_iterations = 1 if max_iterations is None else max_iterations
    parsed = None
    plan: DiffdPlan | None = None
    live_request_url = "-"
    api_response_source = str(fixture_path) if fixture_path else "-"
    api_failure: str | None = None
    api_credential = _pcloud_api_credential(config)
    initial_folder_cache = _read_diffd_folder_cache(config)
    folder_cache_file = _diffd_folder_cache_file(config)
    lock_dir = _diffd_api_long_poll_lock_dir(config)
    lock_stale_seconds = _diffd_api_lock_stale_seconds(config)
    lock_acquired = False
    catchup_gate_env = "PCLOUD_TOOLS_DIFFD_API_CATCHUP_GATE"
    catchup_gate_open = os.environ.get(catchup_gate_env) == _DIFFD_API_CATCHUP_GATE_VALUE
    catchup_policy_approved = bool(getattr(args, "reviewer_approved_catchup_policy", False))
    catchup_requested = bool(live_api and requested_iterations != 1)

    details.update(
        {
            "planned action": (
                "run diffd live pCloud API long-poll"
                if execute and live_api
                else "run diffd pCloud API long-poll fixture"
                if execute
                else "preview diffd API long-poll run"
            ),
            "implementation status": (
                "live pCloud API /diff call; guarded by explicit API gate and --live-api"
                if execute and live_api
                else "fixture-backed long-poll loop; live pCloud API is not called"
                if execute
                else "long-poll run preview only; live pCloud API is not called"
            ),
            "long-poll run gate status": (
                f"open: {long_poll_spec.expected_value}"
                if gate_open
                else f"closed: requires {long_poll_spec.env_var}={long_poll_spec.expected_value}"
            ),
            "long-poll gate status": (
                f"open: {long_poll_spec.expected_value}"
                if gate_open
                else f"closed: requires {long_poll_spec.env_var}={long_poll_spec.expected_value}"
            ),
            "long-poll can start": "yes" if gate_open and approval_status == "complete-read-only" else "no",
            "execute requested": "yes" if execute else "no",
            "state writes": "diffd remote-change records, diff cursor, and long-poll run state" if execute else "none",
            "live API requested": "yes" if live_api else "no",
            "API block requested": "yes" if block else "no",
            "API base URL": api_credential.base_url,
            "API auth parameter": api_credential.auth_param,
            "API credential source": api_credential.source,
            "API credential source detail": api_credential.source_detail,
            "API token provided": "yes" if api_credential.token else "no",
            "API timeout seconds": config.pcloud_api_timeout_seconds,
            "fixture file": str(fixture_path) if fixture_path else "-",
            "API response source": api_response_source,
            "API request URL": "-",
            "retry policy": "manual retry or future scheduler retry only; no automatic retry loop",
            "backoff seconds": config.diffd_poll_interval_seconds,
            "failure state writes": "diffd long-poll failure state only after gated live API execution failure",
            "long-poll state file": str(state_file),
            "long-poll lock": str(lock_dir),
            "long-poll lock status": "-",
            "long-poll lock stale threshold seconds": lock_stale_seconds,
            "folder cache file": str(folder_cache_file),
            "folder cache entries before": len(initial_folder_cache),
            "folder cache entries after": len(initial_folder_cache),
            "folder metadata requests": [],
            "folder metadata requests count": 0,
            "future gate env": f"{long_poll_spec.env_var}={long_poll_spec.expected_value}",
            "max iterations": requested_iterations,
            "catch-up requested": "yes" if catchup_requested else "no",
            "catch-up gate env var": catchup_gate_env,
            "catch-up gate accepted value": _DIFFD_API_CATCHUP_GATE_VALUE,
            "catch-up gate env honored": "yes" if catchup_gate_open else "no",
            "catch-up policy approval": "yes" if catchup_policy_approved else "no",
            "catch-up max iterations limit": 100,
            "new diffid": "-",
            "iterations processed": 0,
            "parsed diff changes": 0,
            "invalid diff changes": 0,
            "download records appended": 0,
            "skipped download records": 0,
            "invalid diff records": [],
            "appended record details": [],
            "skipped record details": [],
        }
    )

    if requested_iterations < 1:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_DIFFD_API_LONG_POLL_MAX_ITERATIONS",
                level="error",
                message="--max-iterations must be >= 1",
            )
        )
    if approval_status != "complete-read-only":
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_DIFFD_API_LONG_POLL_APPROVAL",
                level="error" if execute else "warning",
                message="long-poll execution requires complete read-only API approvals",
            )
        )
    if not gate_open:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_DIFFD_API_LONG_POLL_EXECUTION_GATE",
                level="error" if execute else "warning",
                message=(
                    "long-poll execution requires "
                    f"{long_poll_spec.env_var}={long_poll_spec.expected_value!r}"
                ),
            )
        )
    if execute and fixture_path is None:
        if not live_api:
            issues.append(
                ConfigIssue(
                    key="PCLOUD_TOOLS_DIFFD_API_LONG_POLL_FIXTURE",
                    level="error",
                    message="--fixture is required unless --live-api is explicitly requested",
                )
            )
    if execute and live_api and fixture_path is not None:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_DIFFD_API_LONG_POLL_FIXTURE",
                level="error",
                message="--fixture and --live-api cannot be used together",
            )
        )
    if execute and live_api and not api_credential.token:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_PCLOUD_API_TOKEN",
                level="error",
                message="PCLOUD_TOOLS_PCLOUD_API_TOKEN or rclone pCloud access_token is required for live pCloud API long-poll",
            )
        )
    if execute and catchup_requested:
        if requested_iterations > 100:
            issues.append(
                ConfigIssue(
                    key="PCLOUD_TOOLS_DIFFD_API_CATCHUP_MAX_ITERATIONS",
                    level="error",
                    message="live pCloud API catch-up is limited to --max-iterations <= 100",
                )
            )
        if not catchup_policy_approved:
            issues.append(
                ConfigIssue(
                    key="PCLOUD_TOOLS_DIFFD_API_CATCHUP_APPROVAL",
                    level="error",
                    message="live pCloud API catch-up requires --reviewer-approved-catchup-policy",
                )
            )
        if not catchup_gate_open:
            issues.append(
                ConfigIssue(
                    key=catchup_gate_env,
                    level="error",
                    message=f"live pCloud API catch-up requires {catchup_gate_env}={_DIFFD_API_CATCHUP_GATE_VALUE}",
                )
            )

    if fixture_path is not None:
        try:
            parsed = parse_diff_response_fixture(fixture_path, initial_folder_cache)
            api_response_source = str(fixture_path)
        except OSError as exc:
            issues.append(
                ConfigIssue(
                    key="PCLOUD_TOOLS_DIFFD_API_LONG_POLL_FIXTURE",
                    level="error",
                    message=f"cannot read pCloud diff fixture {fixture_path}: {exc}",
                )
            )

    if execute and live_api and not has_errors(issues):
        lock_acquired, lock_status, lock_issue = _acquire_diffd_api_lock(
            lock_dir,
            issue_key="PCLOUD_TOOLS_DIFFD_API_LONG_POLL_LOCK",
            description="diffd API long-poll",
            stale_after_seconds=lock_stale_seconds,
        )
        details["long-poll lock status"] = lock_status
        if lock_issue is not None:
            issues.append(lock_issue)

    if execute and live_api and not has_errors(issues):
        try:
            current_diffid = daemon_state.diffid
            folder_cache = dict(initial_folder_cache)
            folder_metadata_requests: list[str] = []
            combined_changes = []
            combined_invalid = []
            iterations_processed = 0
            for _ in range(requested_iterations):
                response_text, live_request_url = _fetch_pcloud_diff_response(
                    config, api_credential, current_diffid, block=block
                )
                api_response_source = live_request_url
                folder_cache, fetched_folder_urls = _resolve_diff_response_parent_folders(
                    config, api_credential, response_text, folder_cache
                )
                folder_metadata_requests.extend(fetched_folder_urls)
                iteration = parse_diff_response_text(response_text, live_request_url, folder_cache)
                if not iteration.diffid.isdigit():
                    parsed = iteration
                    break
                combined_changes.extend(iteration.changes)
                combined_invalid.extend(iteration.invalid)
                folder_cache = dict(iteration.folder_paths)
                iterations_processed += 1
                previous_diffid = current_diffid
                current_diffid = iteration.diffid
                if current_diffid == previous_diffid:
                    break
            parsed = DiffdResponseParseResult(
                source=api_response_source,
                diffid=current_diffid,
                changes=tuple(combined_changes),
                invalid=tuple(combined_invalid),
                folder_paths=folder_cache,
            )
            details["iterations processed"] = iterations_processed
            details["folder metadata requests"] = folder_metadata_requests
            details["folder metadata requests count"] = len(folder_metadata_requests)
        except (OSError, urllib.error.URLError, TimeoutError, ValueError) as exc:
            api_failure = str(exc)
            issues.append(
                ConfigIssue(
                    key="PCLOUD_TOOLS_DIFFD_API_LONG_POLL_HTTP",
                    level="error",
                    message=f"live pCloud API /diff request failed: {exc}",
                )
            )

    if parsed is not None:
        remote_records = diff_changes_to_records(parsed.changes)
        plan = build_diffd_plan_from_records(
            config=config,
            remote_changes_file=state.state_dir / "remote-changes.json",
            pending_downloads_file=daemon_state.pending_downloads_file,
            remote_records=remote_records,
        )
        issues.extend(plan.issues)
        details.update(
            {
                "API response source": api_response_source,
                "API request URL": live_request_url,
                "fixture diffid": parsed.diffid if fixture_path else "-",
                "new diffid": parsed.diffid,
                "parsed diff changes": len(parsed.changes),
                "invalid diff changes": len(parsed.invalid),
                "folder cache entries after": len(parsed.folder_paths),
                "invalid diff records": _invalid_diff_details(parsed.invalid),
                **_diffd_plan_details(plan),
            }
        )
        if execute and not parsed.diffid.isdigit():
            label = "API" if live_api else "fixture"
            issues.append(
                ConfigIssue(
                    key="PCLOUD_TOOLS_DIFFD_API_LONG_POLL_DIFFID",
                    level="error",
                    message=f"{label} diffid must be a non-negative integer before cursor mutation: {parsed.diffid!r}",
                )
            )

    failure_state_written = False
    if execute and live_api and api_failure and gate_open and approval_status == "complete-read-only":
        finished_at = datetime.now(timezone.utc).isoformat()
        failure_state = {
            "source": api_response_source,
            "fixture": "-",
            "live_api": True,
            "api_request_url": live_request_url,
            "api_block_requested": block,
            "finished_at": finished_at,
            "iterations_processed": 0,
            "previous_diffid": daemon_state.diffid,
            "written_diffid": "-",
            "failure": api_failure,
            "failure_policy": "retain current diffid and existing remote-change records",
            "retry_policy": "manual retry or future scheduler retry only",
            "backoff_seconds": config.diffd_poll_interval_seconds,
            "appended_records": [],
            "skipped_records": [],
            "invalid_records": [],
        }
        try:
            state_file.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(state_file, failure_state, sort_keys=True)
            failure_state_written = True
            details["process result"] = failure_state
        except OSError as exc:
            issues.append(
                ConfigIssue(
                    key="PCLOUD_TOOLS_DIFFD_API_LONG_POLL_STATE",
                    level="error",
                    message=f"cannot write long-poll failure state {state_file}: {exc}",
                )
            )

    if not execute or has_errors(issues):
        if lock_acquired:
            try:
                lock_dir.rmdir()
                details["long-poll lock status"] = "released"
                lock_acquired = False
            except OSError as exc:
                issues.append(
                    ConfigIssue(
                        key="PCLOUD_TOOLS_DIFFD_API_LONG_POLL_LOCK",
                        level="error",
                        message=f"cannot release diffd API long-poll lock {lock_dir}: {exc}",
                    )
                )
        if has_errors(issues) and not failure_state_written:
            details["state writes"] = "none"
        elif failure_state_written:
            details["state writes"] = "diffd long-poll failure state"
            details["failure state written"] = "yes"
            details["written diffid"] = "-"
            details["iterations processed"] = 0
        issues = sort_issues(issues)
        return CommandReport(
            command="diffd api-poll long-poll-run",
            status=status_from_issues(issues),
            summary=(
                "diffd pCloud API long-poll execution is gated"
                if has_errors(issues) or not gate_open
                else "diffd pCloud API long-poll run is ready"
            ),
            details=details,
            issues=report_issues(issues),
            actions=_service_actions(paths, _SERVICES["diffd"]),
        )

    assert parsed is not None
    assert plan is not None
    iterations_processed = int(details.get("iterations processed", requested_iterations) or 0)
    started_at = datetime.now(timezone.utc).isoformat()
    appended_records: list[dict[str, str]] = []
    skipped_records = record_payloads(plan.skipped_records)
    for record in plan.download_records:
        update = append_plan_record(plan.remote_changes_file, "PCLOUD_TOOLS_DIFFD_REMOTE_CHANGES", record)
        if update.issue:
            issues.append(update.issue)
        else:
            appended_records.append({"path": record.path, "action": record.action, "reason": record.reason})
    written_diffid = "-"
    if not has_errors(issues):
        try:
            written_diffid = write_diffid(config, parsed.diffid)
        except ValueError as exc:
            issues.append(
                ConfigIssue(
                    key="PCLOUD_TOOLS_DIFFD_API_LONG_POLL_DIFFID",
                    level="error",
                    message=str(exc),
                )
            )
    finished_at = datetime.now(timezone.utc).isoformat()
    run_state = {
        "source": api_response_source,
        "fixture": str(fixture_path) if fixture_path else "-",
        "live_api": live_api,
        "api_request_url": live_request_url,
        "api_block_requested": block,
        "started_at": started_at,
        "finished_at": finished_at,
        "iterations_processed": iterations_processed,
        "previous_diffid": daemon_state.diffid,
        "written_diffid": written_diffid,
        "folder_cache_file": str(folder_cache_file),
        "folder_cache_entries_before": len(initial_folder_cache),
        "folder_cache_entries_after": len(parsed.folder_paths),
        "parsed_diff_changes": len(parsed.changes),
        "invalid_diff_changes": len(parsed.invalid),
        "appended_records": appended_records,
        "skipped_records": skipped_records,
        "invalid_records": _invalid_diff_details(parsed.invalid),
    }
    if not has_errors(issues):
        state_file.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(state_file, run_state, sort_keys=True)
        _write_diffd_folder_cache(config, parsed.folder_paths)
    if lock_acquired:
        try:
            lock_dir.rmdir()
            details["long-poll lock status"] = "released"
            lock_acquired = False
        except OSError as exc:
            issues.append(
                ConfigIssue(
                    key="PCLOUD_TOOLS_DIFFD_API_LONG_POLL_LOCK",
                    level="error",
                    message=f"cannot release diffd API long-poll lock {lock_dir}: {exc}",
                )
            )

    details.update(
        {
            "iterations processed": iterations_processed,
            "download records appended": len(appended_records),
            "skipped download records": len(skipped_records),
            "appended record details": appended_records,
            "skipped record details": skipped_records,
            "written diffid": written_diffid,
            "process result": run_state,
            "state writes": (
                "diffd remote-change records, diff cursor, and long-poll run state"
                if not has_errors(issues)
                else "none"
            ),
        }
    )
    issues = sort_issues(issues)
    return CommandReport(
        command="diffd api-poll long-poll-run",
        status=status_from_issues(issues),
        summary=(
            "diffd pCloud API long-poll run completed"
            if not has_errors(issues)
            else "diffd pCloud API long-poll run failed"
        ),
        details=details,
        issues=report_issues(issues),
        actions=_service_actions(paths, _SERVICES["diffd"]),
    )


def _diffd_api_checkpoint_report(args: argparse.Namespace, paths: RuntimePaths) -> CommandReport:
    load_result = load_config(paths)
    config = load_result.config
    daemon_state = read_daemon_state(config)
    execute = bool(getattr(args, "execute", False))
    checkpoint_gate_env = "PCLOUD_TOOLS_DIFFD_API_CHECKPOINT_GATE"
    checkpoint_gate_open = os.environ.get(checkpoint_gate_env) == _DIFFD_API_CHECKPOINT_GATE_VALUE
    operator_reviewed = bool(getattr(args, "operator_reviewed_checkpoint", False))
    policy_approved = bool(getattr(args, "reviewer_approved_checkpoint_policy", False))
    state_file = _diffd_api_checkpoint_state_file(config)
    lock_dir = _diffd_api_long_poll_lock_dir(config)
    lock_stale_seconds = _diffd_api_lock_stale_seconds(config)
    lock_acquired = False
    api_credential = _pcloud_api_credential(config)
    shadow_check, shadow_issues = _shadow_report_check(
        getattr(args, "report_path", None),
        issue_key="PCLOUD_TOOLS_DIFFD_API_CHECKPOINT_SHADOW_REPORT",
    )
    issues = list(load_result.issues)
    if execute:
        issues.extend(ConfigIssue(issue.key, "error", issue.message) for issue in shadow_issues)
    else:
        issues.extend(shadow_issues)
    request_url = "-"
    checkpoint_diffid = "-"
    previous_diffid = daemon_state.diffid
    details: dict[str, object] = {
        "planned action": "set diffd API checkpoint" if execute else "preview diffd API checkpoint",
        "implementation status": (
            "live pCloud /diff last=0 checkpoint; no events are processed"
            if execute
            else "checkpoint preview only; live pCloud API is not called"
        ),
        "execute requested": "yes" if execute else "no",
        "state writes": "diff cursor and checkpoint state only" if execute else "none",
        "current diffid": previous_diffid,
        "checkpoint diffid": checkpoint_diffid,
        "checkpoint state file": str(state_file),
        "diffd API lock": str(lock_dir),
        "diffd API lock status": "-",
        "diffd API lock stale threshold seconds": lock_stale_seconds,
        "checkpoint gate env var": checkpoint_gate_env,
        "checkpoint gate accepted value": _DIFFD_API_CHECKPOINT_GATE_VALUE,
        "checkpoint gate env honored": "yes" if checkpoint_gate_open else "no",
        "operator checkpoint review": "yes" if operator_reviewed else "no",
        "checkpoint policy approval": "yes" if policy_approved else "no",
        "saved shadow validation report": shadow_check,
        "API base URL": api_credential.base_url,
        "API auth parameter": api_credential.auth_param,
        "API credential source": api_credential.source,
        "API credential source detail": api_credential.source_detail,
        "API token provided": "yes" if api_credential.token else "no",
        "API request URL": request_url,
        "request method": "GET",
        "request path": "/diff",
        "request query": {"last": 0},
        "events processed": 0,
        "remote-change records appended": 0,
        "automatic upload/download transfer execution": "no",
        "normal sync/resync": "no",
        "listing cache operations": "no",
        "policy": "set checkpoint to pCloud's current diffid and ignore earlier account-wide history",
        "rollback policy": "manual checkpoint reset only; no local/remote delete or transfer execution",
    }
    if execute and not operator_reviewed:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_DIFFD_API_CHECKPOINT_OPERATOR_REVIEW",
                level="error",
                message="diffd API checkpoint requires --operator-reviewed-checkpoint",
            )
        )
    if execute and not policy_approved:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_DIFFD_API_CHECKPOINT_POLICY",
                level="error",
                message="diffd API checkpoint requires --reviewer-approved-checkpoint-policy",
            )
        )
    if execute and not checkpoint_gate_open:
        issues.append(
            ConfigIssue(
                key=checkpoint_gate_env,
                level="error",
                message=f"diffd API checkpoint requires {checkpoint_gate_env}={_DIFFD_API_CHECKPOINT_GATE_VALUE}",
            )
        )
    if execute and not api_credential.token:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_PCLOUD_API_TOKEN",
                level="error",
                message="PCLOUD_TOOLS_PCLOUD_API_TOKEN or rclone pCloud access_token is required for live pCloud API checkpoint",
            )
        )

    run_state: dict[str, object] | None = None
    if execute and not has_errors(issues):
        lock_acquired, lock_status, lock_issue = _acquire_diffd_api_lock(
            lock_dir,
            issue_key="PCLOUD_TOOLS_DIFFD_API_CHECKPOINT_LOCK",
            description="diffd API checkpoint",
            stale_after_seconds=lock_stale_seconds,
        )
        details["diffd API lock status"] = lock_status
        if lock_issue is not None:
            issues.append(lock_issue)

    if execute and not has_errors(issues):
        try:
            response_text, request_url = _fetch_pcloud_diff_checkpoint(config, api_credential)
            parsed = parse_diff_response_text(response_text, request_url, _read_diffd_folder_cache(config))
            if not parsed.diffid.isdigit():
                issues.append(
                    ConfigIssue(
                        key="PCLOUD_TOOLS_DIFFD_API_CHECKPOINT_DIFFID",
                        level="error",
                        message=f"checkpoint diffid must be a non-negative integer: {parsed.diffid!r}",
                    )
                )
            else:
                checkpoint_diffid = parsed.diffid
                written_diffid = write_diffid(config, checkpoint_diffid)
                finished_at = datetime.now(timezone.utc).isoformat()
                run_state = {
                    "source": request_url,
                    "api_request_url": request_url,
                    "finished_at": finished_at,
                    "previous_diffid": previous_diffid,
                    "checkpoint_diffid": checkpoint_diffid,
                    "written_diffid": written_diffid,
                    "events_processed": 0,
                    "remote_change_records_appended": 0,
                    "policy": "checkpoint current pCloud account diffid; ignore earlier history",
                }
                state_file.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_json(state_file, run_state, sort_keys=True)
        except (OSError, urllib.error.URLError, TimeoutError, ValueError) as exc:
            issues.append(
                ConfigIssue(
                    key="PCLOUD_TOOLS_DIFFD_API_CHECKPOINT_EXECUTION",
                    level="error",
                    message=f"diffd API checkpoint failed: {exc}",
                )
            )
    if lock_acquired:
        try:
            lock_dir.rmdir()
            details["diffd API lock status"] = "released"
            lock_acquired = False
        except OSError as exc:
            issues.append(
                ConfigIssue(
                    key="PCLOUD_TOOLS_DIFFD_API_CHECKPOINT_LOCK",
                    level="error",
                    message=f"cannot release diffd API checkpoint lock {lock_dir}: {exc}",
                )
            )

    if has_errors(issues):
        details["state writes"] = "none"
    details.update(
        {
            "API request URL": request_url,
            "checkpoint diffid": checkpoint_diffid,
            "process result": run_state or {},
        }
    )
    issues = sort_issues(issues)
    return CommandReport(
        command="diffd api-poll checkpoint",
        status=status_from_issues(issues),
        summary=(
            "diffd API checkpoint completed"
            if execute and not has_errors(issues)
            else "diffd API checkpoint is gated"
            if execute and has_errors(issues)
            else "diffd API checkpoint preview is ready"
        ),
        details=details,
        issues=report_issues(issues),
        actions=_service_actions(paths, _SERVICES["diffd"]),
    )


def cmd_diffd_api_poll(args: argparse.Namespace, paths: RuntimePaths) -> int | None:
    if args.api_poll_command == "preview":
        report = _diffd_api_poll_report(paths)
        print_report(report, args)
        return exit_code_for_report(report)
    if args.api_poll_command == "long-poll-gate":
        report = _diffd_api_long_poll_gate_report(args, paths)
        _print_api_long_poll_gate_report(report, args)
        return exit_code_for_report(report)
    if args.api_poll_command == "long-poll-run":
        report = _diffd_api_long_poll_run_report(args, paths)
        _print_api_long_poll_run_report(report, args)
        return exit_code_for_report(report)
    if args.api_poll_command == "checkpoint":
        report = _diffd_api_checkpoint_report(args, paths)
        print_report(report, args)
        return exit_code_for_report(report)
    return None


def _remote_path(remote: str, path: str) -> str:
    return f"{remote.rstrip('/')}/{path.lstrip('/')}"


_TRANSFER_EXECUTION_GATE_VALUE = "dev-fake-rclone"
_REAL_TRANSFER_EXECUTION_GATE_VALUE = "operator-approved-real-transfer-v1"
_TRANSFER_CLEANUP_WAIT_SECONDS = 1
_PUBLIC_REAL_TRANSFER_TIMEOUT_SECONDS = 3600
_REAL_TRANSFER_REQUIRED_SHADOW_CHECKS = {
    "temporary workspace guard",
    "temporary state dir guard",
    "unsafe state dir guard",
}


def _preview_rclone_bin(config: AppConfig) -> str:
    configured = config.rclone_bin.strip()
    if configured and configured != "rclone":
        return configured
    return shutil.which("rclone") or "rclone"


def _transfer_command_records(
    config: AppConfig,
    service: ServiceDefinition,
    records: tuple[PlanRecord, ...],
    *,
    rclone_bin: str | None = None,
) -> list[dict[str, object]]:
    command_bin = rclone_bin or _preview_rclone_bin(config)
    planned: list[dict[str, object]] = []
    for record in records:
        local_path = str(config.core_dir / record.path)
        remote_path = _remote_path(config.core_remote, record.path)
        if service.name == "pushd":
            command = [command_bin, "copyto", local_path, remote_path]
            direction = "upload"
            planned.append(
                {
                    "path": record.path,
                    "direction": direction,
                    "reason": record.reason,
                    "local_path": local_path,
                    "remote_path": remote_path,
                    "command": command,
                }
            )
        else:
            staging_path = download_staging_dir(config) / f"{record.path}.download"
            command = [command_bin, "copyto", remote_path, local_path]
            actual_command = [command_bin, "copyto", remote_path, str(staging_path)]
            direction = "download"
            planned.append(
                {
                    "path": record.path,
                    "direction": direction,
                    "reason": record.reason,
                    "local_path": local_path,
                    "remote_path": remote_path,
                    "final_path": local_path,
                    "staging_path": str(staging_path),
                    "pre_transfer_fingerprint": local_fingerprint(Path(local_path)).as_dict(),
                    "command": command,
                    "actual_command": actual_command,
                }
            )
    return planned


def _transfer_fake_rclone_issue(paths: RuntimePaths, config: AppConfig, command: str) -> ConfigIssue | None:
    dev_issue = _dev_execute_issue(paths, config, command)
    if dev_issue:
        return dev_issue
    if config.transfer_execution_gate != _TRANSFER_EXECUTION_GATE_VALUE:
        return ConfigIssue(
            key="PCLOUD_TOOLS_TRANSFER_EXECUTION_GATE",
            level="error",
            message=(
                "refusing transfer execution until PCLOUD_TOOLS_TRANSFER_EXECUTION_GATE="
                f"{_TRANSFER_EXECUTION_GATE_VALUE!r}"
            ),
        )
    raw_bin = config.rclone_bin.strip()
    if not raw_bin:
        return ConfigIssue(
            key="PCLOUD_TOOLS_RCLONE_BIN",
            level="error",
            message="PCLOUD_TOOLS_RCLONE_BIN must point to a fake-rclone executable for dev transfer execution",
        )
    configured = Path(raw_bin).expanduser()
    if not configured.is_absolute():
        return ConfigIssue(
            key="PCLOUD_TOOLS_RCLONE_BIN",
            level="error",
            message="PCLOUD_TOOLS_RCLONE_BIN must be an absolute fake-rclone path for dev transfer execution",
        )
    try:
        resolved = configured.resolve(strict=True)
    except OSError as exc:
        return ConfigIssue(
            key="PCLOUD_TOOLS_RCLONE_BIN",
            level="error",
            message=f"cannot resolve fake-rclone executable {configured}: {exc}",
        )
    fake_root = (paths.workspace_root / ".dev-state").resolve()
    if resolved.name != "fake-rclone" or not resolved.is_relative_to(fake_root):
        return ConfigIssue(
            key="PCLOUD_TOOLS_RCLONE_BIN",
            level="error",
            message=(
                "refusing transfer execution unless PCLOUD_TOOLS_RCLONE_BIN resolves to "
                f"a fake-rclone executable under {fake_root}"
            ),
        )
    if not resolved.is_file() or not resolved.stat().st_mode & 0o111:
        return ConfigIssue(
            key="PCLOUD_TOOLS_RCLONE_BIN",
            level="error",
            message=f"fake-rclone path is not executable: {resolved}",
        )
    return None


def _resolve_real_rclone_bin(config: AppConfig) -> tuple[str | None, ConfigIssue | None]:
    raw_bin = config.rclone_bin.strip() or "rclone"
    if raw_bin == "rclone":
        found = shutil.which("rclone")
        if not found:
            return None, ConfigIssue(
                key="PCLOUD_TOOLS_RCLONE_BIN",
                level="error",
                message="cannot find rclone in PATH for real transfer execution",
            )
        configured = Path(found)
    else:
        configured = Path(raw_bin).expanduser()
        if not configured.is_absolute():
            return None, ConfigIssue(
                key="PCLOUD_TOOLS_RCLONE_BIN",
                level="error",
                message="PCLOUD_TOOLS_RCLONE_BIN must be absolute for real transfer execution",
            )
    try:
        resolved = configured.resolve(strict=True)
    except OSError as exc:
        return None, ConfigIssue(
            key="PCLOUD_TOOLS_RCLONE_BIN",
            level="error",
            message=f"cannot resolve rclone executable {configured}: {exc}",
        )
    if resolved.name == "fake-rclone":
        return None, ConfigIssue(
            key="PCLOUD_TOOLS_RCLONE_BIN",
            level="error",
            message="fake-rclone is forbidden for real transfer execution",
        )
    if not resolved.is_file() or not resolved.stat().st_mode & 0o111:
        return None, ConfigIssue(
            key="PCLOUD_TOOLS_RCLONE_BIN",
            level="error",
            message=f"rclone path is not executable: {resolved}",
        )
    return str(resolved), None


def _subprocess_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace").strip()
    return value.strip()


def _cleanup_transfer_process_group(process: subprocess.Popen[str]) -> dict[str, object]:
    cleanup: dict[str, object] = {
        "process group cleanup": "attempted",
        "terminate attempted": False,
        "kill attempted": False,
        "terminated": False,
    }
    try:
        pgid = os.getpgid(process.pid)
        cleanup["process group id"] = pgid
    except ProcessLookupError:
        cleanup["process group cleanup"] = "already-exited"
        cleanup["terminated"] = True
        return cleanup
    except OSError as exc:
        cleanup["process group cleanup"] = "pgid-unavailable"
        cleanup["cleanup error"] = str(exc)
        try:
            process.terminate()
            cleanup["terminate attempted"] = True
        except OSError as terminate_exc:
            cleanup["cleanup error"] = f"{cleanup['cleanup error']}; terminate failed: {terminate_exc}"
        pgid = None

    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGTERM)
            cleanup["terminate attempted"] = True
        except ProcessLookupError:
            cleanup["process group cleanup"] = "already-exited"
            cleanup["terminated"] = True
            return cleanup
        except OSError as exc:
            cleanup["process group cleanup"] = "terminate-failed"
            cleanup["cleanup error"] = str(exc)

    try:
        process.wait(timeout=_TRANSFER_CLEANUP_WAIT_SECONDS)
        cleanup["terminated"] = True
        return cleanup
    except subprocess.TimeoutExpired:
        cleanup["process group cleanup"] = "terminate-timeout"

    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGKILL)
            cleanup["kill attempted"] = True
        except ProcessLookupError:
            cleanup["terminated"] = True
            return cleanup
        except OSError as exc:
            cleanup["process group cleanup"] = "kill-failed"
            cleanup["cleanup error"] = str(exc)
    else:
        try:
            process.kill()
            cleanup["kill attempted"] = True
        except OSError as exc:
            cleanup["process group cleanup"] = "kill-failed"
            cleanup["cleanup error"] = str(exc)

    try:
        process.wait(timeout=_TRANSFER_CLEANUP_WAIT_SECONDS)
        cleanup["terminated"] = True
        if cleanup.get("process group cleanup") in {"terminate-timeout", "kill-failed"}:
            cleanup["process group cleanup"] = "killed"
    except subprocess.TimeoutExpired:
        cleanup["process group cleanup"] = "kill-timeout"
    return cleanup


def _fingerprint_payload(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _finalize_download_transfer(
    config: AppConfig,
    item: dict[str, object],
) -> tuple[dict[str, object], list[ConfigIssue]]:
    if item.get("direction") != "download":
        return {}, []
    path = normalize_plan_path(item.get("path", ""))
    final_path = Path(str(item.get("final_path") or item.get("local_path") or ""))
    staging_path = Path(str(item.get("staging_path") or ""))
    issues: list[ConfigIssue] = []
    details: dict[str, object] = {
        "download finalized": False,
        "download conflict": False,
        "download final path": str(final_path),
        "download staging path": str(staging_path),
    }
    if not path or not str(final_path) or not str(staging_path):
        return details, issues
    if not staging_path.exists():
        clear_download_suppression_record(config, path)
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_DIFFD_DOWNLOAD_STAGING",
                level="error",
                message=f"download staging file was not produced for {path}: {staging_path}",
            )
        )
        details["download finalize error"] = "staging file missing"
        return details, issues

    before = _fingerprint_payload(item.get("pre_transfer_fingerprint"))
    current = local_fingerprint(final_path).as_dict()
    if current == before:
        final_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging_path, final_path)
        fingerprint = local_fingerprint(final_path)
        journal_path = mark_download_completed(config, path, fingerprint)
        details.update(
            {
                "download finalized": True,
                "download finalization": "replaced destination from staging",
                "download journal state": "completed",
                "download journal file": str(journal_path),
                "post_transfer_fingerprint": fingerprint.as_dict(),
            }
        )
        return details, issues

    conflict_path = conflict_copy_path(final_path)
    conflict_path.parent.mkdir(parents=True, exist_ok=True)
    os.replace(staging_path, conflict_path)
    fingerprint = local_fingerprint(conflict_path)
    journal_path = mark_download_conflict(
        config,
        path,
        conflict_path=conflict_path.relative_to(config.core_dir).as_posix()
        if conflict_path.is_relative_to(config.core_dir)
        else str(conflict_path),
        fingerprint=fingerprint,
    )
    details.update(
        {
            "download finalized": True,
            "download conflict": True,
            "download finalization": "created conflict copy and retained existing destination",
            "download conflict path": str(conflict_path),
            "download journal state": "conflict",
            "download journal file": str(journal_path),
            "post_transfer_fingerprint": current,
            "conflict_fingerprint": fingerprint.as_dict(),
        }
    )
    issues.append(
        ConfigIssue(
            key="PCLOUD_TOOLS_DIFFD_DOWNLOAD_CONFLICT",
            level="warning",
            message=f"download conflict copy created for {path}: {conflict_path}",
        )
    )
    return details, issues


def _execute_transfer_commands(
    commands: list[dict[str, object]], *, timeout_seconds: int, config: AppConfig | None = None
) -> tuple[list[dict[str, object]], list[ConfigIssue]]:
    results: list[dict[str, object]] = []
    issues: list[ConfigIssue] = []
    for item in commands:
        raw_command = item.get("actual_command", item["command"])
        command = [str(part) for part in raw_command]  # command records are built internally.
        try:
            if config is not None and item.get("direction") == "download":
                mark_download_started(config, str(item.get("path", "")))
                staging_path = Path(str(item.get("staging_path") or ""))
                if str(staging_path):
                    staging_path.parent.mkdir(parents=True, exist_ok=True)
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            stdout, stderr = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            cleanup = _cleanup_transfer_process_group(process)
            try:
                stdout, stderr = process.communicate(timeout=_TRANSFER_CLEANUP_WAIT_SECONDS)
            except subprocess.TimeoutExpired as exc:
                stdout = _subprocess_output(exc.stdout)
                stderr = _subprocess_output(exc.stderr)
                cleanup["communicate timeout"] = True
            results.append(
                {
                    **item,
                    "executed_command": command,
                    "returncode": None,
                    "timed_out": True,
                    "timeout seconds": timeout_seconds,
                    "cleanup": cleanup,
                    "stdout": _subprocess_output(stdout),
                    "stderr": _subprocess_output(stderr),
                }
            )
            if config is not None and item.get("direction") == "download":
                clear_download_suppression_record(config, str(item.get("path", "")))
            issues.append(
                ConfigIssue(
                    key="PCLOUD_TOOLS_TRANSFER_EXEC_TIMEOUT",
                    level="error",
                    message=f"transfer command timed out for {item['path']} after {timeout_seconds}s",
                )
            )
            continue
        except OSError as exc:
            results.append(
                {**item, "executed_command": command, "returncode": None, "timed_out": False, "stdout": "", "stderr": str(exc)}
            )
            if config is not None and item.get("direction") == "download":
                clear_download_suppression_record(config, str(item.get("path", "")))
            issues.append(
                ConfigIssue(
                    key="PCLOUD_TOOLS_TRANSFER_EXEC",
                    level="error",
                    message=f"transfer command could not start for {item['path']}: {exc}",
                )
            )
            continue
        result = {
            **item,
            "executed_command": command,
            "returncode": process.returncode,
            "timed_out": False,
            "stdout": stdout.strip(),
            "stderr": stderr.strip(),
        }
        if process.returncode == 0 and config is not None and item.get("direction") == "upload":
            path = normalize_plan_path(item.get("path", ""))
            fingerprint = local_fingerprint(Path(str(item.get("local_path") or "")))
            journal_path = mark_upload_completed(config, path, fingerprint)
            result.update(
                {
                    "upload origin journal state": "completed",
                    "upload origin journal file": str(journal_path),
                    "post_transfer_fingerprint": fingerprint.as_dict(),
                }
            )
        elif process.returncode == 0 and config is not None and item.get("direction") == "download":
            finalize_details, finalize_issues = _finalize_download_transfer(config, item)
            result.update(finalize_details)
            issues.extend(finalize_issues)
            if finalize_details.get("download conflict"):
                result["manual_review"] = True
                result["conflict"] = True
        elif process.returncode != 0 and config is not None and item.get("direction") == "download":
            clear_download_suppression_record(config, str(item.get("path", "")))
        results.append(result)
        if process.returncode != 0:
            issues.append(
                ConfigIssue(
                    key="PCLOUD_TOOLS_TRANSFER_EXEC",
                    level="error",
                    message=f"transfer command failed for {item['path']} with exit {process.returncode}",
                )
            )
    return results, issues


def _notify_abnormal_transfer_results(
    config: AppConfig,
    service: ServiceDefinition,
    transfer_results: list[dict[str, object]],
) -> tuple[list[dict[str, object]], list[ConfigIssue]]:
    notifications: list[dict[str, object]] = []
    issues: list[ConfigIssue] = []
    journal_path = config.state_dir / service.name / "chat-notify-journal.json"
    journal = _read_chat_notify_journal(journal_path)
    dedupe_seconds = _chat_notify_dedupe_seconds()
    journal_changed = False
    for result in transfer_results:
        if not isinstance(result, dict):
            continue
        path = normalize_plan_path(result.get("path", ""))
        message = ""
        kind = ""
        if result.get("conflict"):
            kind = "conflict"
            message = (
                f"pcloud-manager {service.name}: download conflict for {path}; "
                f"conflict copy={result.get('download conflict path', '-')}"
            )
        elif result.get("timed_out"):
            kind = "timeout"
            message = f"pcloud-manager {service.name}: transfer timed out for {path}"
        elif result.get("returncode") not in {0, None}:
            kind = "failure"
            message = (
                f"pcloud-manager {service.name}: transfer failed for {path} "
                f"exit={result.get('returncode')}"
            )
        if not message:
            continue
        dedupe_key = _chat_notify_dedupe_key(service, kind, path, result)
        suppressed = False
        if config.chat_notify_enabled:
            suppressed = _update_chat_notify_journal(
                journal,
                dedupe_key,
                message,
                dedupe_seconds=dedupe_seconds,
            )
            journal_changed = True
        if suppressed:
            notifications.append(
                {
                    "path": path,
                    "message": message,
                    "attempted": False,
                    "enabled": config.chat_notify_enabled,
                    "command": list(build_chat_notify_command(config, message)),
                    "returncode": "-",
                    "stdout": "",
                    "stderr": "",
                    "issue": "-",
                    "suppressed": True,
                    "suppression reason": "dedupe cooldown",
                    "dedupe seconds": dedupe_seconds,
                    "dedupe key": dedupe_key,
                }
            )
            continue
        notify_result = send_chat_notification(config, message)
        notifications.append(
            {
                "path": path,
                "message": message,
                "suppressed": False,
                "dedupe seconds": dedupe_seconds,
                "dedupe key": dedupe_key,
                **notify_result.as_dict(),
            }
        )
        if notify_result.issue:
            issues.append(notify_result.issue)
    if journal_changed:
        _write_chat_notify_journal(journal_path, journal)
    return notifications, issues


def _chat_notify_dedupe_seconds() -> int:
    raw = os.environ.get("PCLOUD_TOOLS_CHAT_NOTIFY_DEDUPE_SECONDS", "3600")
    try:
        return max(0, int(raw))
    except ValueError:
        return 3600


def _read_chat_notify_journal(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_chat_notify_journal(path: Path, journal: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, journal, ensure_ascii=True, sort_keys=True)


def _chat_notify_dedupe_key(
    service: ServiceDefinition,
    kind: str,
    path: str,
    result: dict[str, object],
) -> str:
    if kind == "conflict":
        conflict_path = normalize_plan_path(result.get("download conflict path", ""))
        return f"{service.name}:{kind}:{path}:{conflict_path}"
    if kind == "failure":
        return f"{service.name}:{kind}:{path}:{result.get('returncode')}"
    return f"{service.name}:{kind}:{path}"


def _update_chat_notify_journal(
    journal: dict[str, object],
    key: str,
    message: str,
    *,
    dedupe_seconds: int,
) -> bool:
    now = datetime.now(timezone.utc)
    entry = journal.get(key)
    if dedupe_seconds > 0 and isinstance(entry, dict):
        last_notified_at = entry.get("last_notified_at")
        if isinstance(last_notified_at, str):
            try:
                last = datetime.fromisoformat(last_notified_at)
            except ValueError:
                last = None
            if last is not None and (now - last).total_seconds() < dedupe_seconds:
                entry["suppressed_count"] = int(entry.get("suppressed_count", 0)) + 1
                entry["last_suppressed_at"] = now.isoformat()
                entry["last_message"] = message
                journal[key] = entry
                return True
    journal[key] = {
        "last_notified_at": now.isoformat(),
        "last_message": message,
        "suppressed_count": 0,
    }
    return False


def _record_transfer_execution_state(
    state: ServiceDaemonState,
    service: ServiceDefinition,
    commands: list[dict[str, object]],
    results: list[dict[str, object]],
    *,
    mode: str = "dev-fake-rclone-transfer",
) -> Path:
    generated_at = datetime.now(timezone.utc).isoformat()
    payload = {
        "service": service.name,
        "mode": mode,
        "generated_at": generated_at,
        "planned_transfer_commands": commands,
        "results": results,
    }
    path = state.state_dir / "last-transfer.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, payload)
    return path


def _successful_transfer_results(
    payload: dict[str, object] | None,
    direction: str,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        return [], []
    successful: list[dict[str, object]] = []
    retained: list[dict[str, object]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        item_direction = str(item.get("direction", ""))
        normalized = normalize_plan_path(item.get("path", ""))
        result = {**item, "path": normalized}
        if (
            normalized
            and item_direction == direction
            and item.get("returncode") == 0
            and not item.get("timed_out")
            and not item.get("manual_review")
            and not item.get("conflict")
        ):
            successful.append(result)
        else:
            retained.append(result)
    return successful, retained


def _consume_source_records(
    config: AppConfig,
    state: ServiceDaemonState,
    service: ServiceDefinition,
) -> tuple[Path, tuple[PlanRecord, ...], list[ConfigIssue]]:
    if service.name == "pushd":
        plan, scope = build_pushd_plan(config, state)
        issues = list(plan.issues) + scope_issues(scope)
        return (
            state.queue_file,
            (*plan.upload_records, *plan.excluded_records, *plan.invalid_records),
            issues,
        )

    daemon_state = read_daemon_state(config)
    plan = build_diffd_plan(config, state, daemon_state)
    return state.state_dir / "remote-changes.json", plan.remote_change_records, [
        *daemon_state.issues,
        *plan.issues,
    ]


def _consume_preview_report(paths: RuntimePaths, service: ServiceDefinition) -> CommandReport:
    load_result = load_config(paths)
    state = read_service_daemon_state(load_result.config, service.name)
    issues = list(load_result.issues) + list(state.issues)
    source_file, source_records, source_issues = _consume_source_records(
        load_result.config, state, service
    )
    issues.extend(source_issues)
    direction = "upload" if service.name == "pushd" else "download"
    successful, retained = _successful_transfer_results(state.last_transfer, direction)
    success_paths = {str(item.get("path", "")) for item in successful if item.get("path")}
    planned_removals = tuple(record for record in source_records if record.path in success_paths)
    matched_paths = {record.path for record in planned_removals}
    unmatched_successes = [
        item for item in successful
        if str(item.get("path", "")) not in matched_paths
    ]
    details: dict[str, object] = {
        "planned action": f"preview {service.name} transfer consume policy",
        "implementation status": "read-only consume preview; queue/change records are not removed",
        "consume gate status": "preview-only",
        "real execution readiness": "not-transfer-execution",
        "real execution blocked reason": "consume commands only inspect or remove dev-state records",
        "real execution can run": "no",
        "state writes": "none",
        "source file": str(source_file),
        "last transfer file": str(state.last_transfer_file),
        "last transfer status": "available" if state.last_transfer else "missing",
        "successful transfer results": len(successful),
        "retained transfer results": len(retained),
        "planned record removals": len(planned_removals),
        "unmatched successful transfers": len(unmatched_successes),
        "planned removal record details": _plan_records(planned_removals),
        "unmatched successful transfer details": unmatched_successes,
        "retained transfer result details": retained,
    }
    issues = sort_issues(issues)
    return CommandReport(
        command=f"{service.name} transfer consume preview",
        status=status_from_issues(issues),
        summary=f"{service.name} transfer consume policy preview is ready",
        details=details,
        issues=report_issues(issues),
        actions=_service_actions(paths, service),
    )


def _consume_run_report(
    args: argparse.Namespace,
    paths: RuntimePaths,
    service: ServiceDefinition,
) -> CommandReport:
    execute = getattr(args, "execute", False)
    report = _consume_preview_report(paths, service)
    details = dict(report.details)
    issues = [
        ConfigIssue(level=issue.level, key=issue.key, message=issue.message)
        for issue in report.issues
    ]
    source_file = Path(str(details["source file"]))
    removals = details.get("planned removal record details")
    removal_paths = [
        str(item.get("path", ""))
        for item in removals
        if isinstance(removals, list) and isinstance(item, dict) and item.get("path")
    ]

    details["planned action"] = (
        f"remove {service.name} consumed transfer records"
        if execute
        else f"preview {service.name} transfer consume run"
    )
    details["consume gate status"] = "open: dev-state" if execute else "closed: preview-only"
    details["records to remove"] = len(removal_paths)

    if execute:
        load_result = load_config(paths)
        dev_issue = _dev_execute_issue(paths, load_result.config, f"{service.name} transfer consume run")
        if dev_issue:
            issues.append(dev_issue)
        if not has_errors(issues):
            before_count: int | None = None
            after_count: int | None = None
            for path in removal_paths:
                result = remove_plan_records(
                    source_file,
                    f"PCLOUD_TOOLS_{service.name.upper()}_TRANSFER_CONSUME",
                    path,
                    write=True,
                )
                if result.issue:
                    issues.append(result.issue)
                    break
                if before_count is None:
                    before_count = result.before_count
                after_count = result.after_count
            details["records before"] = before_count if before_count is not None else 0
            details["records after"] = after_count if after_count is not None else 0
            details["state writes"] = str(source_file) if removal_paths else "none"
        else:
            details["state writes"] = "none"
    else:
        details["state writes"] = "none"

    if has_errors(issues):
        summary = f"{service.name} transfer consume cannot run until issues are resolved"
    elif execute:
        summary = f"{service.name} transfer consumed records"
    else:
        summary = f"{service.name} transfer consume run preview is ready"
    issues = sort_issues(issues)
    return CommandReport(
        command=f"{service.name} transfer consume run",
        status=status_from_issues(issues),
        summary=summary,
        details=details,
        issues=report_issues(issues),
        actions=_service_actions(paths, service),
    )


def _validation_matrix_cases(service: ServiceDefinition) -> list[dict[str, object]]:
    shared = [
        {
            "id": "small-txt",
            "path": "dev-fixtures/Documents/validation/small.txt",
            "purpose": "small text file",
        },
        {
            "id": "japanese-name",
            "path": "dev-fixtures/Documents/validation/\u65e5\u672c\u8a9e\u30d5\u30a1\u30a4\u30eb.txt",
            "purpose": "Japanese filename",
        },
        {
            "id": "space-name",
            "path": "dev-fixtures/Documents/validation/space name.txt",
            "purpose": "filename with spaces",
        },
        {
            "id": "subdirectory",
            "path": "dev-fixtures/Documents/validation/subdir/nested.txt",
            "purpose": "nested allowlisted path",
        },
        {
            "id": "overwrite-existing",
            "path": "dev-fixtures/Documents/validation/overwrite.txt",
            "purpose": "existing target overwrite review",
        },
    ]
    if service.name == "pushd":
        return [{**case, "direction": "upload"} for case in shared]
    return [
        *({**case, "direction": "download"} for case in shared),
        {
            "id": "remote-only-download",
            "path": "dev-fixtures/Documents/validation/remote-only.txt",
            "purpose": "remote-only object download review",
            "direction": "download",
        },
    ]


def _validation_matrix_commands(entrypoint: str, service: ServiceDefinition, path: str) -> dict[str, list[str]]:
    if service.name == "pushd":
        setup = [entrypoint, "pushd", "queue", "add", path, "--reason", "validation-matrix", "--execute", "--json"]
        cleanup = [entrypoint, "pushd", "queue", "remove", path, "--execute", "--json"]
        direction = "upload"
    else:
        setup = [
            entrypoint,
            "diffd",
            "remote-change",
            "add",
            path,
            "--reason",
            "validation-matrix",
            "--execute",
            "--json",
        ]
        cleanup = [entrypoint, "diffd", "remote-change", "remove", path, "--execute", "--json"]
        direction = "download"
    return {
        "setup": setup,
        "preview": [entrypoint, service.name, "transfer", "preview", "--json"],
        "check": [
            entrypoint,
            service.name,
            "transfer",
            "check",
            "--sample-path",
            path,
            "--confirm-path",
            path,
            "--confirm-direction",
            direction,
            "--consume-policy",
            "remove-on-success-retain-on-failure",
            "--timeout-policy",
            "reuse-fake-rclone-cleanup",
            "--final-review",
            "--json",
        ],
        "cleanup": cleanup,
    }


def _validation_matrix_report(paths: RuntimePaths, service: ServiceDefinition) -> CommandReport:
    load_result = load_config(paths)
    entrypoint = action_entrypoint_command(paths)
    issues = list(load_result.issues)
    cases: list[dict[str, object]] = []
    for case in _validation_matrix_cases(service):
        path = str(case["path"])
        cases.append(
            {
                **case,
                "commands": _validation_matrix_commands(entrypoint, service, path),
                "gate": "dedicated real-transfer gate with human confirmation required",
            }
        )
    details: dict[str, object] = {
        "planned action": f"review {service.name} real transfer validation matrix",
        "implementation status": "read-only matrix; no setup, transfer, consume, or cleanup command is executed",
        "real execution can run": "no",
        "state writes": "none",
        "case count": len(cases),
        "cases": cases,
        "blocked operations": [
            "running setup commands",
            "running rclone copyto",
            "consuming queue/change records",
            "launchd registration",
            "normal sync/resync",
            "listing cache operations",
        ],
        "human confirmation required": [
            "choose exactly one case",
            "review setup/preview/check output",
            "open dedicated real-transfer gate",
            "run real transfer only with explicit operator approval",
        ],
    }
    issues = sort_issues(issues)
    return CommandReport(
        command=f"{service.name} transfer validation-matrix",
        status=status_from_issues(issues),
        summary=f"{service.name} real transfer validation matrix is ready",
        details=details,
        issues=report_issues(issues),
        actions=_service_actions(paths, service),
    )


def _shadow_report_check(
    report_path: Path | None,
    *,
    issue_key: str = "PCLOUD_TOOLS_REAL_TRANSFER_SHADOW_REPORT",
) -> tuple[dict[str, object], list[ConfigIssue]]:
    if report_path is None:
        return (
            {
                "name": "saved shadow validation report",
                "status": "pending",
                "detail": "pass --report-path after saving scripts/pcloud-shadow-validation.py --report-path",
            },
            [
                ConfigIssue(
                    key=issue_key,
                    level="warning",
                    message="saved shadow validation report was not provided",
                )
            ],
        )

    path = report_path.expanduser()
    if not path.exists() or not path.is_file():
        return (
            {
                "name": "saved shadow validation report",
                "status": "missing",
                "detail": str(path),
            },
            [
                ConfigIssue(
                    key=issue_key,
                    level="warning",
                    message=f"saved shadow validation report is missing: {path}",
                )
            ],
        )

    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return (
            {
                "name": "saved shadow validation report",
                "status": "invalid",
                "detail": str(exc),
            },
            [
                ConfigIssue(
                    key=issue_key,
                    level="warning",
                    message=f"saved shadow validation report could not be read: {exc}",
                )
            ],
        )

    checks = payload.get("checks")
    check_names = {
        str(check.get("name", ""))
        for check in checks
        if isinstance(check, dict)
    } if isinstance(checks, list) else set()
    failed = [
        str(check.get("name", "unknown"))
        for check in checks
        if isinstance(check, dict) and check.get("status") != "ok"
    ] if isinstance(checks, list) else ["checks missing"]
    missing_required = sorted(_REAL_TRANSFER_REQUIRED_SHADOW_CHECKS - check_names)
    workspace = str(payload.get("workspace", ""))
    state_dir = str(payload.get("state_dir", ""))
    temp_workspace_ok = "/pcloud-shadow-validation-" in workspace and workspace.endswith("/workspace")
    temp_state_ok = state_dir == f"{workspace}/.dev-state/state" if workspace else False
    report_status = payload.get("status")
    if report_status == "ok" and not failed and not missing_required and temp_workspace_ok and temp_state_ok:
        return (
            {
                "name": "saved shadow validation report",
                "status": "ok",
                "detail": f"{path}; required checks present; temp state guard verified",
            },
            [],
        )

    detail = (
        f"top-level status={report_status!r}; failed checks={failed}; "
        f"missing required checks={missing_required}; temp workspace ok={temp_workspace_ok}; "
        f"temp state dir ok={temp_state_ok}"
    )
    return (
        {
            "name": "saved shadow validation report",
            "status": "not-ok",
            "detail": detail,
        },
        [
            ConfigIssue(
                key=issue_key,
                level="warning",
                message=f"saved shadow validation report is not ok: {detail}",
            )
        ],
    )


def _real_transfer_check_report(
    args: argparse.Namespace,
    paths: RuntimePaths,
    service: ServiceDefinition,
) -> CommandReport:
    load_result = load_config(paths)
    state = read_service_daemon_state(load_result.config, service.name)
    issues = list(load_result.issues) + list(state.issues)
    if service.name == "pushd":
        plan, scope = build_pushd_plan(load_result.config, state)
        issues.extend(plan.issues)
        issues.extend(scope_issues(scope))
        present_upload_records, missing_local_records = _split_missing_local_upload_records(
            load_result.config, plan.upload_records
        )
        records, manual_review_records = _filter_manual_review_transfers(
            present_upload_records,
            _opposite_transfer_candidates(load_result.config, service),
        )
        counts = {
            "planned uploads": len(records),
            "missing local upload records": len(missing_local_records),
            "missing local upload record details": _plan_records(missing_local_records),
            "manual review transfer records": len(manual_review_records),
            "excluded queue items": plan.excluded_count,
            "invalid queue items": plan.invalid_count,
        }
        direction = "upload"
    else:
        daemon_state = read_daemon_state(load_result.config)
        issues.extend(daemon_state.issues)
        plan = build_diffd_plan(load_result.config, state, daemon_state)
        issues.extend(plan.issues)
        records, manual_review_records = _filter_manual_review_transfers(
            plan.download_records,
            _opposite_transfer_candidates(load_result.config, service),
        )
        counts = {
            "planned downloads": len(records),
            "manual review transfer records": len(manual_review_records),
            "remote changes": plan.remote_change_count,
            "pending downloads": plan.pending_download_count,
            "skipped download records": plan.skipped_count,
        }
        direction = "download"
    plan_summary = _transfer_plan_summary(service, counts)
    manual_review_issue = _manual_review_issue(service, len(manual_review_records))
    if manual_review_issue:
        issues.append(manual_review_issue)

    commands = _transfer_command_records(load_result.config, service, records)
    shadow_check, shadow_issues = _shadow_report_check(getattr(args, "report_path", None))
    issues.extend(shadow_issues)
    issues.append(
        ConfigIssue(
            key="PCLOUD_TOOLS_REAL_TRANSFER_GATE",
            level="warning",
            message="real rclone/pCloud transfer gate remains closed; this command is a read-only checklist",
        )
    )
    if not commands:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_REAL_TRANSFER_TARGET",
                level="warning",
                message=f"no planned {direction} transfer is available for first-run review",
            )
        )

    entrypoint = action_entrypoint_command(paths)
    preview_command = [entrypoint, service.name, "transfer", "preview", "--json"]
    scope = sync_allowlist_info(load_result.config)
    sample_root = "Documents/"
    if scope.allowlist_status == "loaded" and scope.entries:
        first_entry = scope.entries[0]
        sample_root = first_entry if first_entry.endswith("/") else f"{first_entry}/"
    default_sample_path = f"{sample_root}{service.name}-transfer-gate-sample.txt"
    sample_path = normalize_plan_path(getattr(args, "sample_path", None) or default_sample_path)
    check_command = [entrypoint, service.name, "transfer", "check", "--sample-path", sample_path]
    report_path = getattr(args, "report_path", None)
    if report_path is not None:
        check_command.extend(["--report-path", str(report_path)])
    confirmed_path_raw = getattr(args, "confirm_path", None)
    if confirmed_path_raw:
        check_command.extend(["--confirm-path", normalize_plan_path(confirmed_path_raw)])
    confirmed_direction = getattr(args, "confirm_direction", None)
    if confirmed_direction:
        check_command.extend(["--confirm-direction", confirmed_direction])
    consume_policy = getattr(args, "consume_policy", None)
    if consume_policy:
        check_command.extend(["--consume-policy", consume_policy])
    timeout_policy = getattr(args, "timeout_policy", None)
    if timeout_policy:
        check_command.extend(["--timeout-policy", timeout_policy])
    if getattr(args, "final_review", False):
        check_command.append("--final-review")
    check_command.append("--json")
    sample_record = PlanRecord(sample_path, direction, "real-transfer-gate-sample")
    if service.name == "pushd":
        sample_plan = build_pushd_plan_from_records(
            load_result.config,
            state.queue_file,
            (sample_record,),
        )
        sample_ready = sample_plan.upload_count == 1
        sample_skip_detail = (
            sample_plan.excluded_records[0].reason
            if sample_plan.excluded_records
            else "invalid path" if sample_plan.invalid_records else ""
        )
    else:
        sample_plan = build_diffd_plan_from_records(
            config=load_result.config,
            remote_changes_file=state.state_dir / "remote-changes.json",
            pending_downloads_file=load_result.config.state_dir / "daemon" / "pending-downloads.json",
            remote_records=(sample_record,),
        )
        sample_ready = sample_plan.download_count == 1
        sample_skip_detail = sample_plan.skipped_records[0].reason if sample_plan.skipped_records else ""
    if not sample_ready:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_REAL_TRANSFER_SAMPLE_PATH",
                level="warning",
                message=(
                    "sample path will not become a planned transfer; "
                    f"use a relative allowlisted path: {sample_path or '(empty)'}"
                ),
            )
        )
    if service.name == "pushd":
        setup_command = [
            entrypoint,
            "pushd",
            "queue",
            "add",
            sample_path,
            "--reason",
            "real-transfer-gate-sample",
            "--execute",
            "--json",
        ]
        cleanup_command = [
            entrypoint,
            "pushd",
            "queue",
            "remove",
            sample_path,
            "--execute",
            "--json",
        ]
    else:
        setup_command = [
            entrypoint,
            "diffd",
            "remote-change",
            "add",
            sample_path,
            "--reason",
            "real-transfer-gate-sample",
            "--execute",
            "--json",
        ]
        cleanup_command = [
            entrypoint,
            "diffd",
            "remote-change",
            "remove",
            sample_path,
            "--execute",
            "--json",
        ]
    first_command = commands[0] if commands else {}
    first_target_status = "ready" if commands else "missing"
    target_check, target_issues, confirmed_commands = _real_transfer_target_confirmation(args, service, commands)
    issues.extend(target_issues)
    review_commands = (
        confirmed_commands
        if confirmed_commands and getattr(args, "allow_confirmed_subset", False)
        else commands
    )
    selected_command = confirmed_commands[0] if len(confirmed_commands) == 1 else {}
    consume_check = _real_transfer_policy_check(
        args,
        "consume_policy",
        "queue/change consumption policy",
        "reviewer must approve whether records are consumed, retained, or rolled back on failure",
    )
    timeout_check = _real_transfer_policy_check(
        args,
        "timeout_policy",
        "timeout/process cleanup policy",
        "fake-rclone timeout cleanup exists; real transfer behavior still needs explicit approval",
    )
    checklist = [
        shadow_check,
        {
            "name": "real transfer preview command",
            "status": "ok" if commands else "pending",
            "detail": " ".join(preview_command),
        },
        target_check,
        consume_check,
        timeout_check,
        {
            "name": "parallel dangerous gates",
            "status": "ok",
            "detail": "fswatch resident mode, pCloud API long-poll, launchd changes, and archive work stay out of scope",
        },
    ]
    final_review = _final_real_transfer_review_details(
        requested=getattr(args, "final_review", False),
        checklist=checklist,
        commands=review_commands,
        manual_review_count=len(manual_review_records),
        total_command_count=len(commands),
    )
    details: dict[str, object] = {
        "planned action": f"check {service.name} real {direction} transfer gate prerequisites",
        "implementation status": "read-only checklist; rclone is not executed",
        "real transfer gate status": "closed",
        "real execution readiness": "blocked-final-review",
        "real execution blocked reason": "transfer check is read-only and cannot execute real rclone or pCloud transfer",
        "real execution can run": "no",
        "state writes": "none",
        "dev mode": "on" if paths.dev_mode else "off",
        "plan summary": plan_summary,
        "core dir": str(load_result.config.core_dir),
        "core remote": load_result.config.core_remote,
        "preview command": preview_command,
        "check command": check_command,
        "sample path": sample_path,
        "sample path status": "ready" if sample_ready else "not planned",
        "sample path detail": sample_skip_detail or "will be planned after setup",
        "dev-state sample setup command": setup_command,
        "dev-state sample cleanup command": cleanup_command,
        "review command sequence": [setup_command, preview_command, check_command, cleanup_command],
        "expected after sample setup": {
            "first planned transfer status": "ready" if sample_ready else "missing",
            f"planned {direction}s": 1 if sample_ready else 0,
            "real transfer gate status": "closed",
            "state writes": "none",
        },
        "first planned transfer status": first_target_status,
        "first planned transfer": first_command,
        "selected transfer status": "ready" if selected_command else "missing",
        "selected transfer": selected_command,
        "operator confirmed path": target_check.get("confirmed path", "-"),
        "operator confirmed direction": target_check.get("confirmed direction", "-"),
        "operator target confirmation status": target_check.get("status", "-"),
        "consume policy": consume_policy or "-",
        "consume policy status": consume_check.get("status", "-"),
        "timeout policy": timeout_policy or "-",
        "timeout policy status": timeout_check.get("status", "-"),
        "planned transfer commands": commands,
        "manual review transfer record details": _plan_records(manual_review_records),
        "preflight checks": checklist,
        **final_review,
        **counts,
    }
    issues = sort_issues(issues)
    return CommandReport(
        command=f"{service.name} transfer check",
        status=status_from_issues(issues),
        summary=f"{service.name} real transfer gate checklist is not open",
        details=details,
        issues=report_issues(issues),
        actions=_service_actions(paths, service),
    )


def _real_transfer_gate_report(
    args: argparse.Namespace,
    paths: RuntimePaths,
    service: ServiceDefinition,
) -> CommandReport:
    report = _real_transfer_check_report(
        _real_gate_args(args, allow_confirmed_subset=True),
        paths,
        service,
    )
    details = dict(report.details)
    approval_details = _real_gate_approval_details(args, details.get("final review status"))
    approval_status = approval_details.get("separate real gate approval status")
    verification_details = _operator_verification_details(
        details.get("final review status"),
        approval_status,
    )
    readiness_details = _real_execution_readiness_details(details.get("final review status"), approval_status)
    policy_details = _future_real_run_policy_details(service)
    details.update(
        {
            "planned action": f"inspect {service.name} separate real transfer execution gate",
            "implementation status": (
                "read-only real execution gate scaffold; guarded real-run implementation exists but is not executed"
            ),
            "real transfer gate command status": "read-only",
            "real transfer execution gate status": (
                f"closed: requires PCLOUD_TOOLS_REAL_TRANSFER_EXECUTION_GATE={_REAL_TRANSFER_EXECUTION_GATE_VALUE}"
            ),
            "future real gate env var": "PCLOUD_TOOLS_REAL_TRANSFER_EXECUTION_GATE",
            "future real gate accepted value": _REAL_TRANSFER_EXECUTION_GATE_VALUE,
            "fake-rclone gate reuse": "forbidden",
            "state writes": "none",
            **approval_details,
            **verification_details,
            **readiness_details,
            **policy_details,
        }
    )
    issues = [
        ConfigIssue(
            key="PCLOUD_TOOLS_REAL_TRANSFER_EXECUTION_GATE",
            level="warning",
            message=(
                "real rclone/pCloud transfer execution gate remains closed; "
                "this command is a read-only scaffold"
            ),
        )
    ]
    issues.extend(ConfigIssue(level=issue.level, key=issue.key, message=issue.message) for issue in report.issues)
    issues = sort_issues(issues)
    return CommandReport(
        command=f"{service.name} transfer real-gate",
        status=status_from_issues(issues),
        summary=f"{service.name} real transfer execution gate is closed",
        details=details,
        issues=report_issues(issues),
        actions=_service_actions(paths, service),
    )


def _transfer_automation_gate_report(
    args: argparse.Namespace,
    paths: RuntimePaths,
    service: ServiceDefinition,
) -> CommandReport:
    report = _real_transfer_check_report(_real_gate_args(args), paths, service)
    details = dict(report.details)
    load_result = load_config(paths)
    state = read_service_daemon_state(load_result.config, service.name)
    prior_validation = _prior_real_transfer_validation_details(state, service)
    approval_details = _real_gate_approval_details(args, details.get("final review status"))
    approval_status = approval_details.get("separate real gate approval status")
    planned_transfer_count = int(details.get("planned uploads" if service.name == "pushd" else "planned downloads", 0))
    manual_review_count = int(details.get("manual review transfer records", 0))
    prior_validation_ok = (
        prior_validation.get("prior real transfer validation status") == "ok"
        and planned_transfer_count == 0
        and manual_review_count == 0
    )
    approval_checks_ok = all(
        isinstance(check, dict) and check.get("status") == "ok"
        for check in approval_details.get("separate real gate approval checks", [])
    )
    selected_tick_approval_ok = (
        details.get("selected transfer status") == "ready"
        and details.get("operator target confirmation status") == "ok"
        and details.get("consume policy status") == "ok"
        and details.get("timeout policy status") == "ok"
        and manual_review_count == 0
        and approval_checks_ok
    )
    real_transfer_approvals_ok = (
        approval_status == "complete-read-only"
        or prior_validation_ok
        or selected_tick_approval_ok
    )
    interval = int(getattr(args, "start_interval_seconds", _QUEUE_EXECUTOR_START_INTERVAL_SECONDS))
    max_records = int(getattr(args, "max_records", _PUBLIC_QUEUE_EXECUTOR_MAX_RECORDS))
    public_entrypoint = _command_v("pcloud-manager") or "pcloud-manager"
    label = _service_public_executor_launchd_label(service)
    plist_path = _service_public_launchd_plist_path(label)
    automation_gate_env = "PCLOUD_TOOLS_REAL_TRANSFER_AUTOMATION_GATE"
    automation_gate_provided = os.environ.get(automation_gate_env)
    automation_gate_open = automation_gate_provided == _REAL_TRANSFER_AUTOMATION_GATE_VALUE
    command_status = "implemented-gated"
    automation_checks = [
        {
            "name": "first real-transfer gate review",
            "status": "ok" if getattr(args, "operator_reviewed_real_transfer_gate", False) else "pending",
            "detail": "operator reviewed transfer real-gate output and first target policy",
        },
        {
            "name": "real transfer approvals",
            "status": "ok" if real_transfer_approvals_ok else "pending",
            "detail": (
                f"separate real gate approval status={approval_status}; "
                f"prior validation={prior_validation.get('prior real transfer validation status')}"
            ),
        },
        {
            "name": "prior real-transfer validation",
            "status": "ok" if real_transfer_approvals_ok else "pending",
            "detail": (
                "not required; current selected final-review approved"
                if approval_status == "complete-read-only" or selected_tick_approval_ok
                else str(prior_validation.get("prior real transfer validation detail", "-"))
            ),
        },
        {
            "name": "automation command implementation",
            "status": "ok",
            "detail": "public real transfer automation-run command exists and is gated",
        },
        {
            "name": "automation command approval",
            "status": "ok" if getattr(args, "reviewer_approved_automation_command", False) else "pending",
            "detail": "reviewer approved the future automation command argv, environment, and command ownership",
        },
        {
            "name": "automation consume policy approval",
            "status": "ok" if getattr(args, "reviewer_approved_consume_policy", False) else "pending",
            "detail": "reviewer approved remove-on-success/retain-on-failure behavior for automatic queue drain",
        },
        {
            "name": "automation launchd policy approval",
            "status": "ok" if getattr(args, "reviewer_approved_launchd_policy", False) else "pending",
            "detail": f"reviewer approved public label, plist path, StartInterval={interval}, logs, and rollback scope",
        },
        {
            "name": "automation rollback policy approval",
            "status": "ok" if getattr(args, "reviewer_approved_rollback_policy", False) else "pending",
            "detail": "reviewer approved disabling only this executor service on failure",
        },
        {
            "name": "automation gate env",
            "status": "ok" if automation_gate_open else "pending",
            "detail": f"{automation_gate_env}={_REAL_TRANSFER_AUTOMATION_GATE_VALUE}",
        },
        {
            "name": "automation interval",
            "status": "ok" if interval > 0 else "pending",
            "detail": f"StartInterval={interval}",
        },
        {
            "name": "parallel dangerous gates",
            "status": "ok",
            "detail": "normal sync/resync, listing cache operations, autosync launchd changes, and immediate launchctl execution stay out of scope",
        },
    ]
    issues = [
        ConfigIssue(
            key="PCLOUD_TOOLS_REAL_TRANSFER_AUTOMATION_GATE",
            level="warning",
            message="automatic real upload/download transfer execution remains closed; this command is read-only",
        ),
    ]
    if interval <= 0:
        issues.append(
            ConfigIssue(
                key=f"PCLOUD_TOOLS_{service.name.upper()}_TRANSFER_AUTOMATION_INTERVAL",
                level="error",
                message="transfer automation StartInterval must be a positive number of seconds",
            )
        )
    if max_records <= 0:
        issues.append(
            ConfigIssue(
                key=f"PCLOUD_TOOLS_{service.name.upper()}_TRANSFER_AUTOMATION_BATCH_LIMIT",
                level="error",
                message="transfer automation --max-records must be a positive integer",
            )
        )
    issues.extend(ConfigIssue(level=issue.level, key=issue.key, message=issue.message) for issue in report.issues)
    issues = sort_issues(issues)
    details.update(
        {
            "planned action": f"inspect {service.name} automatic real transfer queue executor gate",
            "implementation status": "read-only automation gate; public executor plist and automation command are not written or run",
            "automation gate status": "closed",
            "automation can run": "no",
            "automation command status": command_status,
            "automation gate env var": automation_gate_env,
            "automation gate accepted value": _REAL_TRANSFER_AUTOMATION_GATE_VALUE,
            "automation gate env provided": "yes" if automation_gate_provided else "no",
            "automation gate env honored": "no",
            "planned public executor service label": label,
            "planned public executor plist path": str(plist_path),
            "planned public executor StartInterval": interval,
            "planned public executor max records": max_records,
            "future automation command": [
                public_entrypoint,
                service.name,
                "transfer",
                "automation-run",
                "--execute",
                "--consume-on-success",
                "--max-records",
                str(max_records),
                "--json",
            ],
            "future real-run command": [
                public_entrypoint,
                service.name,
                "transfer",
                "real-run",
                "--execute",
                "--json",
            ],
            "future consume policy": "remove-on-success-retain-on-failure only after exact successful real transfer result",
            "state writes": "none",
            "launchctl execution": "no",
            "public plist writes": "no",
            "automatic real transfer execution": "no",
            "normal sync/resync": "no",
            "listing cache operations": "no",
            "automation approval status": "ready-for-launchd-review" if all(
                check["status"] == "ok" for check in automation_checks
            ) else "pending",
            "automation approval checks": automation_checks,
            "separate real gate approval status": approval_status,
            "real transfer approvals source": (
                "current selected final-review" if approval_status == "complete-read-only"
                else "current selected bounded automation tick" if selected_tick_approval_ok
                else "prior successful real-run" if prior_validation_ok
                else "pending"
            ),
            "automation batch limit": max_records,
            "deferred transfer command count": max(planned_transfer_count - max_records, 0),
            **approval_details,
            **prior_validation,
            **_future_real_run_policy_details(service),
            "minimum terminal review commands": [
                [public_entrypoint, service.name, "transfer", "check", "--json"],
                [public_entrypoint, service.name, "transfer", "real-gate", "--json"],
                [public_entrypoint, service.name, "transfer", "automation-gate", "--json"],
            ],
            "blocked operations": [
                "writing public queue executor LaunchAgent plist",
                "launchctl enable",
                "launchctl bootstrap",
                "launchctl bootout",
                "launchctl disable",
                "automatic real upload/download execution",
                "automatic queue/change record consumption after real transfer",
                "normal sync/resync",
                "listing cache operations",
                "autosync launchd changes",
            ],
            "next human check trigger": "explicit request to implement or write public real-transfer automation executor",
        }
    )
    return CommandReport(
        command=f"{service.name} transfer automation-gate",
        status=status_from_issues(issues),
        summary=f"{service.name} real transfer automation gate is closed",
        details=details,
        issues=report_issues(issues),
        actions=_service_actions(paths, service),
    )


def _consume_successful_transfer_results(
    config: AppConfig,
    state: ServiceDaemonState,
    service: ServiceDefinition,
    transfer_results: list[dict[str, object]],
) -> tuple[dict[str, object], list[ConfigIssue]]:
    source_file, source_records, source_issues = _consume_source_records(config, state, service)
    success_paths = {
        str(item.get("path", ""))
        for item in transfer_results
        if (
            item.get("returncode") == 0
            and not item.get("timed_out")
            and not item.get("manual_review")
            and not item.get("conflict")
            and item.get("path")
        )
    }
    planned_removals = tuple(record for record in source_records if record.path in success_paths)
    issues = list(source_issues)
    before_count: int | None = None
    after_count: int | None = None
    for record in planned_removals:
        result = remove_plan_records(
            source_file,
            f"PCLOUD_TOOLS_{service.name.upper()}_TRANSFER_AUTOMATION_CONSUME",
            record.path,
            write=True,
        )
        if result.issue:
            issues.append(result.issue)
            break
        if before_count is None:
            before_count = result.before_count
        after_count = result.after_count
    details: dict[str, object] = {
        "consume source file": str(source_file),
        "successful transfer paths": sorted(success_paths),
        "records consumed": len(planned_removals) if not has_errors(issues) else 0,
        "consumed record details": _plan_records(planned_removals),
        "consume records before": before_count if before_count is not None else 0,
        "consume records after": after_count if after_count is not None else 0,
        "consume state writes": str(source_file) if planned_removals and not has_errors(issues) else "none",
    }
    return details, issues


def _transfer_automation_run_report(
    args: argparse.Namespace,
    paths: RuntimePaths,
    service: ServiceDefinition,
) -> CommandReport:
    load_result = load_config(paths)
    state = read_service_daemon_state(load_result.config, service.name)
    real_gate_env = os.environ.get("PCLOUD_TOOLS_REAL_TRANSFER_EXECUTION_GATE")
    automation_gate_env = os.environ.get("PCLOUD_TOOLS_REAL_TRANSFER_AUTOMATION_GATE")
    automation_run_gate_env = os.environ.get("PCLOUD_TOOLS_REAL_TRANSFER_AUTOMATION_RUN_GATE")
    execute = getattr(args, "execute", False)
    consume_on_success = getattr(args, "consume_on_success", False)
    max_records = int(getattr(args, "max_records", 1))
    issues = list(load_result.issues) + list(state.issues)
    shadow_check, shadow_issues = _shadow_report_check(
        getattr(args, "report_path", None),
        issue_key="PCLOUD_TOOLS_REAL_TRANSFER_AUTOMATION_SHADOW_REPORT",
    )
    issues.extend(shadow_issues)
    if service.name == "pushd":
        plan, scope = build_pushd_plan(load_result.config, state)
        issues.extend(plan.issues)
        issues.extend(scope_issues(scope))
        present_upload_records, missing_local_records = _split_missing_local_upload_records(
            load_result.config, plan.upload_records
        )
        records, manual_review_records = _filter_manual_review_transfers(
            present_upload_records,
            _opposite_transfer_candidates(load_result.config, service),
        )
        count_details = {
            "planned uploads": len(records),
            "missing local upload records": len(missing_local_records),
            "missing local upload record details": _plan_records(missing_local_records),
            "manual review transfer records": len(manual_review_records),
            "excluded queue items": plan.excluded_count,
            "invalid queue items": plan.invalid_count,
        }
    else:
        daemon_state = read_daemon_state(load_result.config)
        issues.extend(daemon_state.issues)
        plan = build_diffd_plan(load_result.config, state, daemon_state)
        issues.extend(plan.issues)
        records, manual_review_records = _filter_manual_review_transfers(
            plan.download_records,
            _opposite_transfer_candidates(load_result.config, service),
        )
        count_details = {
            "planned downloads": len(records),
            "manual review transfer records": len(manual_review_records),
            "remote changes": plan.remote_change_count,
            "pending downloads": plan.pending_download_count,
            "skipped download records": plan.skipped_count,
        }
    planned_command_count = len(records)
    executable_records = records[:max_records] if max_records > 0 else []
    deferred_records = records[max_records:] if max_records > 0 else records
    execution_command_count = len(executable_records)
    manual_review_issue = _manual_review_issue(service, len(manual_review_records))
    if manual_review_issue:
        issues.append(manual_review_issue)
    if execute and manual_review_records:
        issues.append(
            ConfigIssue(
                key=f"PCLOUD_TOOLS_{service.name.upper()}_AUTOMATION_MANUAL_REVIEW",
                level="error",
                message="automation-run refuses to execute while manual-review transfer records are present",
            )
        )
    real_gate_open = real_gate_env == _REAL_TRANSFER_EXECUTION_GATE_VALUE
    automation_gate_open = automation_gate_env == _REAL_TRANSFER_AUTOMATION_GATE_VALUE
    automation_run_gate_open = automation_run_gate_env == _REAL_TRANSFER_AUTOMATION_RUN_GATE_VALUE
    if not real_gate_open:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_REAL_TRANSFER_EXECUTION_GATE",
                level="error" if execute else "warning",
                message=(
                    "automation-run requires PCLOUD_TOOLS_REAL_TRANSFER_EXECUTION_GATE="
                    f"{_REAL_TRANSFER_EXECUTION_GATE_VALUE!r}"
                ),
            )
        )
    if not automation_gate_open:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_REAL_TRANSFER_AUTOMATION_GATE",
                level="error" if execute else "warning",
                message=(
                    "automation-run requires PCLOUD_TOOLS_REAL_TRANSFER_AUTOMATION_GATE="
                    f"{_REAL_TRANSFER_AUTOMATION_GATE_VALUE!r}"
                ),
            )
        )
    if not automation_run_gate_open:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_REAL_TRANSFER_AUTOMATION_RUN_GATE",
                level="error" if execute else "warning",
                message=(
                    "automation-run requires PCLOUD_TOOLS_REAL_TRANSFER_AUTOMATION_RUN_GATE="
                    f"{_REAL_TRANSFER_AUTOMATION_RUN_GATE_VALUE!r}"
                ),
            )
        )
    if execute and shadow_check.get("status") != "ok":
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_REAL_TRANSFER_AUTOMATION_SHADOW_REPORT",
                level="error",
                message="automation-run execution requires a saved ok shadow validation report",
            )
        )
    if execute and not consume_on_success:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_REAL_TRANSFER_AUTOMATION_CONSUME_POLICY",
                level="error",
                message="automation-run requires --consume-on-success so successful records are not retried",
            )
        )
    if execute and max_records <= 0:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_REAL_TRANSFER_AUTOMATION_BATCH_LIMIT",
                level="error",
                message="automation-run requires --max-records to be a positive integer",
            )
        )
    rclone_bin = None
    rclone_issue: ConfigIssue | None = None
    if real_gate_open and automation_gate_open and automation_run_gate_open:
        rclone_bin, rclone_issue = _resolve_real_rclone_bin(load_result.config)
        if rclone_issue:
            issues.append(rclone_issue)
    planned_commands = _transfer_command_records(load_result.config, service, records, rclone_bin=rclone_bin)
    commands = _transfer_command_records(load_result.config, service, executable_records, rclone_bin=rclone_bin)
    transfer_results: list[dict[str, object]] = []
    transfer_state_file: Path | None = None
    consume_details: dict[str, object] = {
        "records consumed": 0,
        "consume state writes": "none",
    }
    notify_details: list[dict[str, object]] = []
    runnable = (
        real_gate_open
        and automation_gate_open
        and automation_run_gate_open
        and shadow_check.get("status") == "ok"
        and consume_on_success
        and max_records > 0
        and rclone_bin is not None
        and not rclone_issue
        and not manual_review_records
    )
    if execute and runnable and execution_command_count > 0 and not has_errors(issues):
        transfer_results, execution_issues = _execute_transfer_commands(
            commands,
            timeout_seconds=load_result.config.transfer_exec_timeout_seconds,
            config=load_result.config,
        )
        transfer_state_file = _record_transfer_execution_state(
            state,
            service,
            commands,
            transfer_results,
            mode="real-rclone-automation-transfer",
        )
        consume_details, consume_issues = _consume_successful_transfer_results(
            load_result.config,
            state,
            service,
            transfer_results,
        )
        issues.extend(execution_issues)
        notify_details, notify_issues = _notify_abnormal_transfer_results(
            load_result.config,
            service,
            transfer_results,
        )
        issues.extend(notify_issues)
        issues.extend(consume_issues)
    elif execute and runnable and planned_command_count == 0 and not has_errors(issues):
        transfer_results = []
    elif execute and manual_review_records:
        notify_result = send_chat_notification(
            load_result.config,
            f"pcloud-manager {service.name}: automation blocked by manual-review records ({len(manual_review_records)})",
        )
        notify_details.append({"message": "manual-review records present", **notify_result.as_dict()})
        if notify_result.issue:
            issues.append(notify_result.issue)
    state_writes: list[str] = []
    if transfer_state_file is not None:
        state_writes.append(str(transfer_state_file))
    consume_state_write = str(consume_details.get("consume state writes", "none"))
    if consume_state_write != "none":
        state_writes.append(consume_state_write)
    if execute and runnable and planned_command_count == 0 and not has_errors(issues):
        summary = f"{service.name} transfer automation-run had no records"
        implementation_status = "guarded automatic real-transfer executor tick; no transfer records were pending"
        automation_can_run = "yes"
    elif execute and runnable and not has_errors(issues):
        summary = f"{service.name} transfer automation-run completed"
        implementation_status = "guarded automatic real-transfer executor tick"
        automation_can_run = "yes"
    elif execute:
        summary = f"{service.name} transfer automation-run refused"
        implementation_status = "guarded automatic real-transfer executor tick; blocked by gate checks"
        automation_can_run = "no"
    elif runnable:
        summary = f"{service.name} transfer automation-run is ready"
        implementation_status = "guarded automatic real-transfer executor tick; not executed without --execute"
        automation_can_run = "yes"
    else:
        summary = f"{service.name} transfer automation-run is gated"
        implementation_status = "guarded automatic real-transfer executor tick; blocked by gate checks"
        automation_can_run = "no"
    issues = sort_issues(issues)
    details: dict[str, object] = {
        "planned action": f"{'execute' if execute else 'preview'} {service.name} automatic real-transfer queue executor",
        "implementation status": implementation_status,
        "automation command status": "implemented-gated",
        "automation can run": automation_can_run,
        "execute requested": "yes" if execute else "no",
        "consume on success requested": "yes" if consume_on_success else "no",
        "saved shadow validation report": shadow_check,
        "real transfer gate env provided": "yes" if real_gate_env else "no",
        "real transfer gate env honored": "yes" if real_gate_open else "no",
        "automation gate env provided": "yes" if automation_gate_env else "no",
        "automation gate env honored": "yes" if automation_gate_open else "no",
        "automation run gate env provided": "yes" if automation_run_gate_env else "no",
        "automation run gate env honored": "yes" if automation_run_gate_open else "no",
        "automation run gate env var": "PCLOUD_TOOLS_REAL_TRANSFER_AUTOMATION_RUN_GATE",
        "automation run gate accepted value": _REAL_TRANSFER_AUTOMATION_RUN_GATE_VALUE,
        "rclone binary": rclone_bin or _preview_rclone_bin(load_result.config),
        "state writes": state_writes if state_writes else "none",
        "public plist writes": "no",
        "launchctl execution": "no",
        "automatic real transfer execution": "yes" if execute and transfer_results else "no",
        "automatic queue/change consumption": "yes" if consume_details.get("records consumed") else "no",
        "automation batch limit": max_records,
        "planned transfer command count": planned_command_count,
        "execution transfer command count": execution_command_count,
        "deferred transfer command count": len(deferred_records),
        "planned transfer commands": planned_commands,
        "execution transfer commands": commands,
        "deferred transfer record details": _plan_records(deferred_records),
        "manual review transfer record details": _plan_records(manual_review_records),
        "transfer results": transfer_results,
        "chat notify results": notify_details,
        **consume_details,
        **chat_notify_status(load_result.config),
        **count_details,
        "normal sync/resync": "no",
        "listing cache operations": "no",
        "blocked operations": [
            "public LaunchAgent plist write",
            "launchctl execution",
            "normal sync/resync",
            "listing cache operations",
        ],
        "next human check trigger": "public automation plist write/reload or any changed automation policy",
    }
    return CommandReport(
        command=f"{service.name} transfer automation-run",
        status=status_from_issues(issues),
        summary=summary,
        details=details,
        issues=report_issues(issues),
        actions=_service_actions(paths, service),
    )


def _real_transfer_run_report(
    args: argparse.Namespace,
    paths: RuntimePaths,
    service: ServiceDefinition,
) -> CommandReport:
    check_report = _real_transfer_check_report(
        _real_gate_args(args, allow_confirmed_subset=True),
        paths,
        service,
    )
    load_result = load_config(paths)
    state = read_service_daemon_state(load_result.config, service.name)
    real_gate_env = os.environ.get("PCLOUD_TOOLS_REAL_TRANSFER_EXECUTION_GATE")
    fake_gate_env = os.environ.get("PCLOUD_TOOLS_TRANSFER_EXECUTION_GATE")
    details = dict(check_report.details)
    approval_details = _real_gate_approval_details(args, details.get("final review status"))
    approval_status = approval_details.get("separate real gate approval status")
    commands = details.get("planned transfer commands")
    all_planned_commands = commands if isinstance(commands, list) else []
    selected_command = details.get("selected transfer")
    planned_commands = (
        [selected_command]
        if isinstance(selected_command, dict) and selected_command
        else all_planned_commands
    )
    execute = getattr(args, "execute", False)
    gate_open = real_gate_env == _REAL_TRANSFER_EXECUTION_GATE_VALUE

    issues = [
        ConfigIssue(level=issue.level, key=issue.key, message=issue.message)
        for issue in check_report.issues
        if issue.key != "PCLOUD_TOOLS_REAL_TRANSFER_GATE"
    ]
    if not gate_open:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_REAL_TRANSFER_EXECUTION_GATE",
                level="error" if execute else "warning",
                message=(
                    "real transfer execution requires PCLOUD_TOOLS_REAL_TRANSFER_EXECUTION_GATE="
                    f"{_REAL_TRANSFER_EXECUTION_GATE_VALUE!r}"
                ),
            )
        )
    if details.get("final review status") != "ready":
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_REAL_TRANSFER_FINAL_REVIEW",
                level="error" if execute else "warning",
                message="real transfer execution requires a ready final-review checklist",
            )
        )
    if approval_status != "complete-read-only":
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_REAL_TRANSFER_APPROVAL",
                level="error" if execute else "warning",
                message="real transfer execution requires all read-only approval flags",
            )
        )
    rclone_bin = None
    rclone_issue: ConfigIssue | None = None
    if gate_open and approval_status == "complete-read-only" and details.get("final review status") == "ready":
        rclone_bin, rclone_issue = _resolve_real_rclone_bin(load_result.config)
        if rclone_issue:
            issues.append(rclone_issue)

    runnable = (
        gate_open
        and approval_status == "complete-read-only"
        and details.get("final review status") == "ready"
        and rclone_bin is not None
        and not rclone_issue
    )
    entrypoint = action_entrypoint_command(paths)
    real_commands = _transfer_command_records(
        load_result.config,
        service,
        tuple(
            PlanRecord(
                path=str(item.get("path", "")),
                action=str(item.get("direction", "")),
                reason=str(item.get("reason", "")),
            )
            for item in planned_commands
            if isinstance(item, dict) and item.get("path")
        ),
        rclone_bin=rclone_bin,
    ) if rclone_bin else planned_commands
    transfer_results: list[dict[str, object]] = []
    transfer_state_file: Path | None = None
    consume_details: dict[str, object] = {
        "records consumed": 0,
        "consume state writes": "none",
    }
    notify_details: list[dict[str, object]] = []
    if execute and runnable and not has_errors(issues):
        transfer_results, execution_issues = _execute_transfer_commands(
            real_commands,
            timeout_seconds=load_result.config.transfer_exec_timeout_seconds,
            config=load_result.config,
        )
        issues.extend(execution_issues)
        notify_details, notify_issues = _notify_abnormal_transfer_results(
            load_result.config,
            service,
            transfer_results,
        )
        issues.extend(notify_issues)
        transfer_state_file = _record_transfer_execution_state(
            state,
            service,
            real_commands,
            transfer_results,
            mode="real-rclone-transfer",
        )
        if getattr(args, "consume_policy", None) == "remove-on-success-retain-on-failure" and not has_errors(issues):
            consume_details, consume_issues = _consume_successful_transfer_results(
                load_result.config,
                state,
                service,
                transfer_results,
            )
            issues.extend(consume_issues)

    state_writes: list[str] = []
    if transfer_state_file is not None:
        state_writes.append(str(transfer_state_file))
    consume_state_write = str(consume_details.get("consume state writes", "none"))
    if consume_state_write != "none":
        state_writes.append(consume_state_write)

    if execute and transfer_state_file and not has_errors(issues):
        summary = f"{service.name} real transfer executed"
        implementation_status = "guarded real rclone transfer execution path"
        readiness = "executed"
        can_run = "yes"
    elif runnable:
        summary = f"{service.name} real transfer run is ready"
        implementation_status = "guarded real rclone transfer execution path; not executed without --execute"
        readiness = "ready"
        can_run = "yes"
    else:
        summary = f"{service.name} real transfer execution is gated"
        implementation_status = "guarded real rclone transfer execution path; blocked by gate checks"
        readiness = "blocked-gate"
        can_run = "no"
    if execute and has_errors(issues):
        summary = f"{service.name} real transfer execution refused"
    details.update(
        {
            "planned action": f"{'execute' if execute else 'preview'} {service.name} real transfer execution",
            "implementation status": implementation_status,
            "real transfer gate status": "closed",
            "real transfer execution gate status": (
                f"open: {_REAL_TRANSFER_EXECUTION_GATE_VALUE}"
                if gate_open and runnable
                else f"closed: requires PCLOUD_TOOLS_REAL_TRANSFER_EXECUTION_GATE={_REAL_TRANSFER_EXECUTION_GATE_VALUE}"
            ),
            "real execution readiness": readiness,
            "real execution blocked reason": (
                "-" if runnable else "final review, approval flags, real gate env, or rclone binary are not ready"
            ),
            "real execution can run": can_run,
            "execute requested": "yes" if execute else "no",
            "state writes": ", ".join(state_writes) if state_writes else "none",
            "core dir": str(load_result.config.core_dir),
            "core remote": load_result.config.core_remote,
            "future real gate env var": "PCLOUD_TOOLS_REAL_TRANSFER_EXECUTION_GATE",
            "future real gate accepted value": _REAL_TRANSFER_EXECUTION_GATE_VALUE,
            "real gate env provided": "yes" if real_gate_env else "no",
            "real gate env honored": "yes" if gate_open else "no",
            "fake-rclone gate reuse": "forbidden",
            "fake-rclone gate env provided": "yes" if fake_gate_env else "no",
            "fake-rclone gate env honored": "no",
            "rclone binary": rclone_bin or _preview_rclone_bin(load_result.config),
            "all planned transfer commands": all_planned_commands,
            "planned transfer commands": real_commands,
            "transfer results": transfer_results,
            "chat notify results": notify_details,
            "automatic queue/change consumption": (
                "yes" if consume_details.get("consume state writes") != "none" else "no"
            ),
            "safe alternative command": [
                entrypoint,
                service.name,
                "transfer",
                "real-gate",
                "--json",
            ],
            **consume_details,
            **chat_notify_status(load_result.config),
            **approval_details,
        }
    )
    issues = sort_issues(issues)
    return CommandReport(
        command=f"{service.name} transfer real-run",
        status=status_from_issues(issues),
        summary=summary,
        details=details,
        issues=report_issues(issues),
        actions=_service_actions(paths, service),
    )


def _service_transfer_report(
    paths: RuntimePaths,
    service: ServiceDefinition,
    *,
    transfer_command: str,
    execute: bool = False,
) -> CommandReport:
    load_result = load_config(paths)
    state = read_service_daemon_state(load_result.config, service.name)
    issues = list(load_result.issues) + list(state.issues)
    if service.name == "pushd":
        plan, scope = build_pushd_plan(load_result.config, state)
        issues.extend(plan.issues)
        issues.extend(scope_issues(scope))
        present_upload_records, missing_local_records = _split_missing_local_upload_records(
            load_result.config, plan.upload_records
        )
        records, manual_review_records = _filter_manual_review_transfers(
            present_upload_records,
            _opposite_transfer_candidates(load_result.config, service),
        )
        counts = {
            "planned uploads": len(records),
            "missing local upload records": len(missing_local_records),
            "missing local upload record details": _plan_records(missing_local_records),
            "manual review transfer records": len(manual_review_records),
            "excluded queue items": plan.excluded_count,
            "invalid queue items": plan.invalid_count,
        }
        preview_summary = "pushd upload transfer preview is ready"
        run_preview_summary = "pushd upload transfer run preview is ready"
        executed_summary = "pushd upload transfer executed with fake-rclone"
        planned_action = "preview pushd upload executor commands"
    else:
        daemon_state = read_daemon_state(load_result.config)
        issues.extend(daemon_state.issues)
        plan = build_diffd_plan(load_result.config, state, daemon_state)
        issues.extend(plan.issues)
        records, manual_review_records = _filter_manual_review_transfers(
            plan.download_records,
            _opposite_transfer_candidates(load_result.config, service),
        )
        counts = {
            "planned downloads": len(records),
            "manual review transfer records": len(manual_review_records),
            "remote changes": plan.remote_change_count,
            "pending downloads": plan.pending_download_count,
            "skipped download records": plan.skipped_count,
        }
        preview_summary = "diffd download transfer preview is ready"
        run_preview_summary = "diffd download transfer run preview is ready"
        executed_summary = "diffd download transfer executed with fake-rclone"
        planned_action = "preview diffd download executor commands"

    manual_review_issue = _manual_review_issue(service, len(manual_review_records))
    if manual_review_issue:
        issues.append(manual_review_issue)

    execution_issue: ConfigIssue | None = None
    rclone_bin: str | None = None
    transfer_results: list[dict[str, object]] = []
    transfer_state_file: Path | None = None
    notify_details: list[dict[str, object]] = []
    if execute:
        execution_issue = _transfer_fake_rclone_issue(
            paths, load_result.config, f"{service.name} transfer run"
        )
        if execution_issue:
            issues.append(execution_issue)
        else:
            rclone_bin = str(Path(load_result.config.rclone_bin).expanduser().resolve(strict=True))

    commands = _transfer_command_records(load_result.config, service, records, rclone_bin=rclone_bin)
    if execute and not execution_issue and not has_errors(issues):
        transfer_results, execution_issues = _execute_transfer_commands(
            commands,
            timeout_seconds=load_result.config.transfer_exec_timeout_seconds,
            config=load_result.config,
        )
        issues.extend(execution_issues)
        notify_details, notify_issues = _notify_abnormal_transfer_results(
            load_result.config,
            service,
            transfer_results,
        )
        issues.extend(notify_issues)
        transfer_state_file = _record_transfer_execution_state(state, service, commands, transfer_results)

    if transfer_command == "preview":
        implementation_status = "transfer command preview only; rclone is not executed"
        summary = preview_summary
    elif execute and not has_errors(issues):
        implementation_status = (
            "dev-mode fake-rclone transfer execution only; real rclone and pCloud transfer are not permitted"
        )
        summary = executed_summary
    elif execute and transfer_results:
        implementation_status = (
            "dev-mode fake-rclone transfer execution failed; real rclone and pCloud transfer are not permitted"
        )
        summary = f"{service.name} transfer execution failed"
    elif execute:
        implementation_status = "transfer execution refused before rclone start"
        summary = f"{service.name} transfer execution refused"
    else:
        implementation_status = "transfer run preview only; rclone is not executed"
        summary = run_preview_summary

    if transfer_state_file:
        state_writes: object = str(transfer_state_file)
    else:
        state_writes = "none"

    details: dict[str, object] = {
        "planned action": planned_action,
        "implementation status": implementation_status,
        "real transfer gate status": "closed",
        "real execution readiness": "blocked-preview" if not execute else "blocked-fake-rclone-only",
        "real execution blocked reason": (
            "transfer preview/run paths do not permit real rclone or pCloud transfer"
        ),
        "real execution can run": "no",
        "execution gate": (
            "open: dev-fake-rclone"
            if execute and not execution_issue
            else "closed: requires PCLOUD_TOOLS_TRANSFER_EXECUTION_GATE=dev-fake-rclone"
        ),
        "state writes": state_writes,
        "transfer timeout seconds": load_result.config.transfer_exec_timeout_seconds,
        "core dir": str(load_result.config.core_dir),
        "core remote": load_result.config.core_remote,
        "planned transfer commands": commands,
        "manual review transfer record details": _plan_records(manual_review_records),
        "chat notify results": notify_details,
        **chat_notify_status(load_result.config),
        **counts,
    }
    if transfer_command == "preview":
        details["gate status"] = "closed"
    if execute:
        details["transfer results"] = transfer_results
    issues = sort_issues(issues)
    return CommandReport(
        command=f"{service.name} transfer {transfer_command}",
        status=status_from_issues(issues),
        summary=summary,
        details=details,
        issues=report_issues(issues),
        actions=_service_actions(paths, service),
    )


def _config_issues_from_report(report: CommandReport) -> list[ConfigIssue]:
    return [
        ConfigIssue(level=issue.level, key=issue.key, message=issue.message)
        for issue in report.issues
    ]


def _transfer_executor_run_report(
    args: argparse.Namespace,
    paths: RuntimePaths,
    service: ServiceDefinition,
) -> CommandReport:
    execute = getattr(args, "execute", False)
    consume_on_success = getattr(args, "consume_on_success", False)
    preview_report = _service_transfer_report(paths, service, transfer_command="run", execute=False)
    preview_details = dict(preview_report.details)
    manual_review_count = int(preview_details.get("manual review transfer records") or 0)
    planned_commands = preview_details.get("planned transfer commands")
    planned_command_count = len(planned_commands) if isinstance(planned_commands, list) else 0
    issues = _config_issues_from_report(preview_report)

    manual_review_blocked = execute and manual_review_count > 0
    if manual_review_blocked:
        issues.append(
            ConfigIssue(
                key=f"PCLOUD_TOOLS_{service.name.upper()}_EXECUTOR_MANUAL_REVIEW",
                level="error",
                message=(
                    f"{service.name} transfer executor refuses to run while "
                    f"{manual_review_count} manual-review record(s) are present"
                ),
            )
        )

    transfer_report = preview_report
    if execute and not manual_review_blocked:
        transfer_report = _service_transfer_report(paths, service, transfer_command="run", execute=True)
        issues = _config_issues_from_report(transfer_report)

    transfer_errors = any(issue.level == "error" for issue in transfer_report.issues)
    consume_report: CommandReport | None = None
    if execute and consume_on_success and not transfer_errors and planned_command_count > 0:
        consume_args = argparse.Namespace(execute=True, json=True, xbar=False)
        consume_report = _consume_run_report(consume_args, paths, service)
        issues.extend(_config_issues_from_report(consume_report))

    transfer_details = dict(transfer_report.details)
    consume_details = dict(consume_report.details) if consume_report else {}
    state_writes: list[str] = []
    transfer_state_writes = str(transfer_details.get("state writes", "none"))
    consume_state_writes = str(consume_details.get("state writes", "none"))
    if transfer_state_writes != "none":
        state_writes.append(transfer_state_writes)
    if consume_state_writes != "none":
        state_writes.append(consume_state_writes)

    if execute and not any(issue.level == "error" for issue in issues):
        summary = f"{service.name} transfer executor tick completed"
        implementation_status = "dev fake-rclone transfer executor tick; real transfer automation remains closed"
    elif execute:
        summary = f"{service.name} transfer executor tick refused"
        implementation_status = "transfer executor blocked before automatic queue drain"
    else:
        summary = f"{service.name} transfer executor tick preview is ready"
        implementation_status = "transfer executor preview only; no rclone or consume command is executed"

    details: dict[str, object] = {
        "planned action": f"{'execute' if execute else 'preview'} {service.name} transfer executor tick",
        "implementation status": implementation_status,
        "executor gate status": "open: dev-fake-rclone" if execute and not has_errors(issues) else "preview/dev-only",
        "real transfer automation gate status": "closed",
        "real execution can run": "no",
        "execute requested": "yes" if execute else "no",
        "consume on success requested": "yes" if consume_on_success else "no",
        "state writes": state_writes if state_writes else "none",
        "planned transfer command count": planned_command_count,
        "manual review transfer records": manual_review_count,
        "transfer summary": transfer_report.summary,
        "transfer status": transfer_report.status,
        "transfer state writes": transfer_state_writes,
        "consume summary": consume_report.summary if consume_report else "-",
        "consume status": consume_report.status if consume_report else "-",
        "consume state writes": consume_state_writes,
        "records consumed": consume_details.get("records to remove", 0) if consume_report else 0,
        "blocked operations": [
            "real rclone/pCloud transfer automation",
            "normal sync/resync",
            "listing cache operations",
            "autosync launchd changes",
        ],
    }
    details.update(
        {
            key: value
            for key, value in transfer_details.items()
            if key
            in {
                "planned uploads",
                "planned downloads",
                "remote changes",
                "pending downloads",
                "queued items",
                "execution gate",
                "transfer results",
            }
        }
    )
    issues = sort_issues(issues)
    return CommandReport(
        command=f"{service.name} transfer executor-run",
        status=status_from_issues(issues),
        summary=summary,
        details=details,
        issues=report_issues(issues),
        actions=_service_actions(paths, service),
    )


def cmd_service_transfer(
    args: argparse.Namespace, paths: RuntimePaths, service: ServiceDefinition
) -> int | None:
    if args.transfer_command == "preview":
        report = _service_transfer_report(paths, service, transfer_command="preview")
        _print_transfer_preview_report(report, args)
        return exit_code_for_report(report)
    if args.transfer_command == "validation-matrix":
        report = _validation_matrix_report(paths, service)
        _print_validation_matrix_report(report, args)
        return exit_code_for_report(report)
    if args.transfer_command == "check":
        report = _real_transfer_check_report(args, paths, service)
        _print_transfer_check_report(report, args)
        return exit_code_for_report(report)
    if args.transfer_command == "real-gate":
        report = _real_transfer_gate_report(args, paths, service)
        _print_transfer_check_report(report, args)
        return exit_code_for_report(report)
    if args.transfer_command == "automation-gate":
        report = _transfer_automation_gate_report(args, paths, service)
        print_report(report, args)
        return exit_code_for_report(report)
    if args.transfer_command == "automation-run":
        report = _transfer_automation_run_report(args, paths, service)
        print_report(report, args)
        return exit_code_for_report(report)
    if args.transfer_command == "real-run":
        report = _real_transfer_run_report(args, paths, service)
        _print_real_transfer_run_report(report, args)
        return exit_code_for_report(report)
    if args.transfer_command == "run":
        report = _service_transfer_report(
            paths,
            service,
            transfer_command="run",
            execute=getattr(args, "execute", False),
        )
        print_report(report, args)
        return exit_code_for_report(report)
    if args.transfer_command == "executor-run":
        report = _transfer_executor_run_report(args, paths, service)
        print_report(report, args)
        return exit_code_for_report(report)
    if args.transfer_command == "consume" and getattr(args, "consume_command", None) == "preview":
        report = _consume_preview_report(paths, service)
        _print_transfer_consume_report(report, args)
        return exit_code_for_report(report)
    if args.transfer_command == "consume" and getattr(args, "consume_command", None) == "run":
        report = _consume_run_report(args, paths, service)
        _print_transfer_consume_report(report, args)
        return exit_code_for_report(report)
    return None


def _dev_execute_issue(paths: RuntimePaths, config: AppConfig, command: str) -> ConfigIssue | None:
    if not paths.dev_mode:
        return ConfigIssue(
            key="PCLOUD_TOOLS_DEV_EXECUTION",
            level="error",
            message=f"refusing --execute for `{command}` outside pcloud-tools dev mode",
        )
    expected_state_root = (paths.workspace_root / ".dev-state" / "state").resolve()
    actual_state_dir = config.state_dir.resolve()
    if actual_state_dir != expected_state_root and not actual_state_dir.is_relative_to(
        expected_state_root
    ):
        return ConfigIssue(
            key="PCLOUD_TOOLS_DEV_STATE_DIR",
            level="error",
            message=(
                f"refusing --execute for `{command}` outside dev state dir: "
                f"{actual_state_dir} is not under {expected_state_root}"
            ),
        )
    return None


def _state_update_issue(issue: ConfigIssue | None) -> ConfigIssue | None:
    if not issue:
        return None
    return ConfigIssue(key=issue.key, level="error", message=issue.message)


def _plan_record_from_args(args: argparse.Namespace, default_action: str, key: str) -> tuple[PlanRecord, list[ConfigIssue]]:
    issues: list[ConfigIssue] = []
    path = normalize_plan_path(getattr(args, "path", ""))
    action = str(getattr(args, "action", default_action) or default_action).strip()
    reason = str(getattr(args, "reason", "manual") or "manual").strip()
    if not path:
        issues.append(ConfigIssue(key=key, level="error", message="path must be a relative path"))
    if not action:
        issues.append(ConfigIssue(key=f"{key}_ACTION", level="error", message="action is required"))
    if not reason:
        reason = "manual"
    return PlanRecord(path=path, action=action or default_action, reason=reason), issues


def _pushd_queue_report(args: argparse.Namespace, paths: RuntimePaths) -> CommandReport:
    load_result = load_config(paths)
    state = read_service_daemon_state(load_result.config, "pushd")
    plan, scope = build_pushd_plan(load_result.config, state)
    execute = getattr(args, "execute", False)
    issues = list(load_result.issues) + list(state.issues) + list(plan.issues) + scope_issues(scope)

    if args.queue_command == "add":
        record, record_issues = _plan_record_from_args(args, "upload", "PCLOUD_TOOLS_PUSHD_QUEUE_PATH")
        issues.extend(record_issues)
        planned_action = "append pushd queue record" if execute else "preview append pushd queue record"
        after_count = plan.total + 1
        details: dict[str, object] = {
            "planned action": planned_action,
            "queue file": str(state.queue_file),
            "queue items before": plan.total,
            "queue items after": after_count,
            "path": record.path,
            "action": record.action,
            "reason": record.reason,
        }
        if execute:
            dev_issue = _dev_execute_issue(paths, load_result.config, "pushd queue add")
            if dev_issue:
                issues.append(dev_issue)
            if not has_errors(issues):
                result = append_plan_record(state.queue_file, "PCLOUD_TOOLS_PUSHD_QUEUE", record)
                update_issue = _state_update_issue(result.issue)
                if update_issue:
                    issues.append(update_issue)
                details["queue items before"] = result.before_count
                details["queue items after"] = result.after_count
        summary = "pushd queue record appended" if execute and not has_errors(issues) else "pushd queue add preview is ready"
    elif args.queue_command == "clear":
        planned_action = "clear pushd queue" if execute else "preview clear pushd queue"
        details = {
            "planned action": planned_action,
            "queue file": str(state.queue_file),
            "queue items before": plan.total,
            "queue items after": 0,
        }
        if execute:
            dev_issue = _dev_execute_issue(paths, load_result.config, "pushd queue clear")
            if dev_issue:
                issues.append(dev_issue)
            if not has_errors(issues):
                result = clear_plan_records(state.queue_file, "PCLOUD_TOOLS_PUSHD_QUEUE")
                update_issue = _state_update_issue(result.issue)
                if update_issue:
                    issues.append(update_issue)
                details["queue items before"] = result.before_count
                details["queue items after"] = result.after_count
        summary = "pushd queue cleared" if execute and not has_errors(issues) else "pushd queue clear preview is ready"
    elif args.queue_command == "remove":
        record, record_issues = _plan_record_from_args(args, "upload", "PCLOUD_TOOLS_PUSHD_QUEUE_PATH")
        issues.extend(record_issues)
        planned_action = "remove pushd queue records" if execute else "preview remove pushd queue records"
        remove_gate_env = "PCLOUD_TOOLS_PUSHD_QUEUE_REMOVE_GATE"
        remove_gate_open = os.environ.get(remove_gate_env) == _PUSHD_QUEUE_REMOVE_GATE_VALUE
        remove_approval = bool(getattr(args, "reviewer_approved_queue_record_removal", False))
        result = (
            remove_plan_records(state.queue_file, "PCLOUD_TOOLS_PUSHD_QUEUE", record.path, write=False)
            if not record_issues
            else None
        )
        if result and result.issue:
            update_issue = _state_update_issue(result.issue)
            if update_issue:
                issues.append(update_issue)
        details = {
            "planned action": planned_action,
            "queue file": str(state.queue_file),
            "queue items before": result.before_count if result else plan.total,
            "queue items after": result.after_count if result else plan.total,
            "queue items removed": (result.before_count - result.after_count) if result else 0,
            "path": record.path,
            "cleanup scope": "matching pushd queue path only",
            "queue remove gate env var": remove_gate_env,
            "queue remove gate accepted value": _PUSHD_QUEUE_REMOVE_GATE_VALUE,
            "queue remove gate env honored": "yes" if remove_gate_open else "no",
            "queue record removal approval": "yes" if remove_approval else "no",
            "state writes": "pushd queue only" if execute else "none",
            "automatic real transfer execution": "no",
            "normal sync/resync": "no",
            "listing cache operations": "no",
        }
        if execute:
            public_remove_gate = remove_gate_open and remove_approval
            if paths.dev_mode:
                dev_issue = _dev_execute_issue(paths, load_result.config, "pushd queue remove")
                if dev_issue:
                    issues.append(dev_issue)
            elif not public_remove_gate:
                if not remove_approval:
                    issues.append(
                        ConfigIssue(
                            key="PCLOUD_TOOLS_PUSHD_QUEUE_REMOVE_APPROVAL",
                            level="error",
                            message="pushd queue remove requires --reviewer-approved-queue-record-removal",
                        )
                    )
                if not remove_gate_open:
                    issues.append(
                        ConfigIssue(
                            key="PCLOUD_TOOLS_PUSHD_QUEUE_REMOVE_GATE",
                            level="error",
                            message=f"pushd queue remove requires {remove_gate_env}={_PUSHD_QUEUE_REMOVE_GATE_VALUE}",
                        )
                    )
            if not has_errors(issues):
                result = remove_plan_records(state.queue_file, "PCLOUD_TOOLS_PUSHD_QUEUE", record.path)
                update_issue = _state_update_issue(result.issue)
                if update_issue:
                    issues.append(update_issue)
                details["queue items before"] = result.before_count
                details["queue items after"] = result.after_count
                details["queue items removed"] = result.before_count - result.after_count
        if has_errors(issues):
            details["state writes"] = "none"
        summary = (
            "pushd queue records removed"
            if execute and not has_errors(issues)
            else "pushd queue remove preview is ready"
        )
    elif args.queue_command == "prune-excluded":
        excluded_records = plan.excluded_records
        planned_action = "prune excluded pushd queue records" if execute else "preview prune excluded pushd queue records"
        prune_gate_env = "PCLOUD_TOOLS_PUSHD_QUEUE_PRUNE_EXCLUDED_GATE"
        prune_gate_open = os.environ.get(prune_gate_env) == _PUSHD_QUEUE_PRUNE_EXCLUDED_GATE_VALUE
        cleanup_approved = bool(getattr(args, "reviewer_approved_excluded_record_cleanup", False))
        details = {
            "planned action": planned_action,
            "queue file": str(state.queue_file),
            "queue items before": plan.total,
            "queue items after": plan.total - len(excluded_records),
            "queue items removed": len(excluded_records),
            "excluded queue items": len(excluded_records),
            "excluded queue record details": _plan_records(excluded_records),
            "cleanup scope": "excluded records only; planned upload and invalid records are retained",
            "prune gate env var": prune_gate_env,
            "prune gate accepted value": _PUSHD_QUEUE_PRUNE_EXCLUDED_GATE_VALUE,
            "prune gate env honored": "yes" if prune_gate_open else "no",
            "cleanup approval": "yes" if cleanup_approved else "no",
            "state writes": "none",
            "automatic real transfer execution": "no",
            "normal sync/resync": "no",
            "listing cache operations": "no",
        }
        if execute:
            if paths.dev_mode:
                dev_issue = _dev_execute_issue(paths, load_result.config, "pushd queue prune-excluded")
                if dev_issue:
                    issues.append(dev_issue)
            else:
                if not cleanup_approved:
                    issues.append(
                        ConfigIssue(
                            key="PCLOUD_TOOLS_PUSHD_QUEUE_PRUNE_EXCLUDED_APPROVAL",
                            level="error",
                            message="pushd queue prune-excluded requires reviewer approval before public queue cleanup",
                        )
                    )
                if not prune_gate_open:
                    issues.append(
                        ConfigIssue(
                            key=prune_gate_env,
                            level="error",
                            message=(
                                "pushd queue prune-excluded requires "
                                f"{prune_gate_env}={_PUSHD_QUEUE_PRUNE_EXCLUDED_GATE_VALUE}"
                            ),
                        )
                    )
            if not has_errors(issues):
                before_count = plan.total
                after_count = before_count
                for record in excluded_records:
                    result = remove_plan_records(
                        state.queue_file,
                        "PCLOUD_TOOLS_PUSHD_QUEUE",
                        record.path,
                    )
                    update_issue = _state_update_issue(result.issue)
                    if update_issue:
                        issues.append(update_issue)
                        break
                    after_count = result.after_count
                details["queue items before"] = before_count
                details["queue items after"] = after_count
                details["queue items removed"] = before_count - after_count
                details["state writes"] = "pushd queue only" if not has_errors(issues) else "none"
        summary = (
            "pushd queue excluded records pruned"
            if execute and not has_errors(issues)
            else "pushd queue prune-excluded preview is ready"
        )
    elif args.queue_command == "prune-missing-local":
        _present_records, missing_local_records = _split_missing_local_upload_records(
            load_result.config, plan.upload_records
        )
        planned_action = (
            "prune missing local pushd upload records"
            if execute
            else "preview prune missing local pushd upload records"
        )
        cleanup_approved = bool(getattr(args, "reviewer_approved_missing_local_cleanup", False))
        details = {
            "planned action": planned_action,
            "queue file": str(state.queue_file),
            "queue items before": plan.total,
            "queue items after": plan.total - len(missing_local_records),
            "queue items removed": len(missing_local_records),
            "missing local upload records": len(missing_local_records),
            "missing local upload record details": _plan_records(missing_local_records),
            "cleanup scope": "missing local upload records only; existing files, excluded records, and invalid records are retained",
            "cleanup approval": "yes" if cleanup_approved else "no",
            "state writes": "none",
            "automatic real transfer execution": "no",
            "normal sync/resync": "no",
            "listing cache operations": "no",
        }
        if execute:
            if paths.dev_mode:
                dev_issue = _dev_execute_issue(paths, load_result.config, "pushd queue prune-missing-local")
                if dev_issue:
                    issues.append(dev_issue)
            elif not cleanup_approved:
                issues.append(
                    ConfigIssue(
                        key="PCLOUD_TOOLS_PUSHD_QUEUE_PRUNE_MISSING_LOCAL_APPROVAL",
                        level="error",
                        message=(
                            "pushd queue prune-missing-local requires "
                            "--reviewer-approved-missing-local-cleanup"
                        ),
                    )
                )
            if not has_errors(issues):
                before_count = plan.total
                after_count = before_count
                for record in missing_local_records:
                    result = remove_plan_records(
                        state.queue_file,
                        "PCLOUD_TOOLS_PUSHD_QUEUE",
                        record.path,
                    )
                    update_issue = _state_update_issue(result.issue)
                    if update_issue:
                        issues.append(update_issue)
                        break
                    after_count = result.after_count
                details["queue items before"] = before_count
                details["queue items after"] = after_count
                details["queue items removed"] = before_count - after_count
                details["state writes"] = "pushd queue only" if not has_errors(issues) else "none"
        summary = (
            "pushd queue missing local records pruned"
            if execute and not has_errors(issues)
            else "pushd queue prune-missing-local preview is ready"
        )
    else:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_PUSHD_QUEUE_COMMAND",
                level="error",
                message="queue command must be add, clear, remove, prune-excluded, or prune-missing-local",
            )
        )
        details = {"planned action": "none", "queue file": str(state.queue_file)}
        summary = "pushd queue command is invalid"

    if has_errors(issues):
        summary = "pushd queue cannot be updated until issues are resolved"
    issues = sort_issues(issues)
    return CommandReport(
        command=f"pushd queue {args.queue_command or ''}".strip(),
        status=status_from_issues(issues),
        summary=summary,
        details=details,
        issues=report_issues(issues),
        actions=_service_actions(paths, _SERVICES["pushd"]),
    )


def cmd_pushd_queue(args: argparse.Namespace, paths: RuntimePaths) -> int:
    report = _pushd_queue_report(args, paths)
    print_report(report, args)
    return exit_code_for_report(report)


def _diffd_remote_change_report(args: argparse.Namespace, paths: RuntimePaths) -> CommandReport:
    load_result = load_config(paths)
    state = read_service_daemon_state(load_result.config, "diffd")
    daemon_state = read_daemon_state(load_result.config)
    plan = build_diffd_plan(load_result.config, state, daemon_state)
    execute = getattr(args, "execute", False)
    issues = list(load_result.issues) + list(state.issues) + list(daemon_state.issues) + list(plan.issues)

    if args.remote_change_command == "add":
        record, record_issues = _plan_record_from_args(
            args, "download", "PCLOUD_TOOLS_DIFFD_REMOTE_CHANGE_PATH"
        )
        issues.extend(record_issues)
        planned_action = (
            "append diffd remote-change record" if execute else "preview append diffd remote-change record"
        )
        details: dict[str, object] = {
            "planned action": planned_action,
            "remote changes file": str(plan.remote_changes_file),
            "remote changes before": plan.remote_change_count,
            "remote changes after": plan.remote_change_count + 1,
            "path": record.path,
            "action": record.action,
            "reason": record.reason,
        }
        if execute:
            dev_issue = _dev_execute_issue(paths, load_result.config, "diffd remote-change add")
            if dev_issue:
                issues.append(dev_issue)
            if not has_errors(issues):
                result = append_plan_record(
                    plan.remote_changes_file, "PCLOUD_TOOLS_DIFFD_REMOTE_CHANGES", record
                )
                update_issue = _state_update_issue(result.issue)
                if update_issue:
                    issues.append(update_issue)
                details["remote changes before"] = result.before_count
                details["remote changes after"] = result.after_count
        summary = (
            "diffd remote-change record appended"
            if execute and not has_errors(issues)
            else "diffd remote-change add preview is ready"
        )
    elif args.remote_change_command == "clear":
        planned_action = "clear diffd remote changes" if execute else "preview clear diffd remote changes"
        details = {
            "planned action": planned_action,
            "remote changes file": str(plan.remote_changes_file),
            "remote changes before": plan.remote_change_count,
            "remote changes after": 0,
        }
        if execute:
            dev_issue = _dev_execute_issue(paths, load_result.config, "diffd remote-change clear")
            if dev_issue:
                issues.append(dev_issue)
            if not has_errors(issues):
                result = clear_plan_records(plan.remote_changes_file, "PCLOUD_TOOLS_DIFFD_REMOTE_CHANGES")
                update_issue = _state_update_issue(result.issue)
                if update_issue:
                    issues.append(update_issue)
                details["remote changes before"] = result.before_count
                details["remote changes after"] = result.after_count
        summary = (
            "diffd remote changes cleared"
            if execute and not has_errors(issues)
            else "diffd remote-change clear preview is ready"
        )
    elif args.remote_change_command == "remove":
        record, record_issues = _plan_record_from_args(
            args, "download", "PCLOUD_TOOLS_DIFFD_REMOTE_CHANGE_PATH"
        )
        issues.extend(record_issues)
        planned_action = (
            "remove diffd remote-change records"
            if execute
            else "preview remove diffd remote-change records"
        )
        result = (
            remove_plan_records(
                plan.remote_changes_file,
                "PCLOUD_TOOLS_DIFFD_REMOTE_CHANGES",
                record.path,
                write=False,
            )
            if not record_issues
            else None
        )
        if result and result.issue:
            update_issue = _state_update_issue(result.issue)
            if update_issue:
                issues.append(update_issue)
        details = {
            "planned action": planned_action,
            "remote changes file": str(plan.remote_changes_file),
            "remote changes before": result.before_count if result else plan.remote_change_count,
            "remote changes after": result.after_count if result else plan.remote_change_count,
            "remote changes removed": (result.before_count - result.after_count) if result else 0,
            "path": record.path,
        }
        if execute:
            dev_issue = _dev_execute_issue(paths, load_result.config, "diffd remote-change remove")
            if dev_issue:
                issues.append(dev_issue)
            if not has_errors(issues):
                result = remove_plan_records(
                    plan.remote_changes_file,
                    "PCLOUD_TOOLS_DIFFD_REMOTE_CHANGES",
                    record.path,
                )
                update_issue = _state_update_issue(result.issue)
                if update_issue:
                    issues.append(update_issue)
                details["remote changes before"] = result.before_count
                details["remote changes after"] = result.after_count
                details["remote changes removed"] = result.before_count - result.after_count
        summary = (
            "diffd remote-change records removed"
            if execute and not has_errors(issues)
            else "diffd remote-change remove preview is ready"
        )
    else:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_DIFFD_REMOTE_CHANGE_COMMAND",
                level="error",
                message="remote-change command must be add, clear, or remove",
            )
        )
        details = {"planned action": "none", "remote changes file": str(plan.remote_changes_file)}
        summary = "diffd remote-change command is invalid"

    if has_errors(issues):
        summary = "diffd remote changes cannot be updated until issues are resolved"
    issues = sort_issues(issues)
    return CommandReport(
        command=f"diffd remote-change {args.remote_change_command or ''}".strip(),
        status=status_from_issues(issues),
        summary=summary,
        details=details,
        issues=report_issues(issues),
        actions=_service_actions(paths, _SERVICES["diffd"]),
    )


def cmd_diffd_remote_change(args: argparse.Namespace, paths: RuntimePaths) -> int:
    report = _diffd_remote_change_report(args, paths)
    print_report(report, args)
    return exit_code_for_report(report)


def _standalone_main(service_name: str, argv: list[str] | None = None) -> int:
    service = _SERVICES[service_name]
    parser = argparse.ArgumentParser(
        prog=f"pcloud-{service_name}",
        description=f"Development scaffold for {service.summary_name}.",
    )
    subparsers = parser.add_subparsers(dest="service_command")

    status_parser = subparsers.add_parser("status", help=service.status_help)
    status_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    status_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")

    preview_parser = subparsers.add_parser("preview", help=service.preview_help)
    preview_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    preview_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")

    run_parser = subparsers.add_parser("run", help=f"Preview a {service.name} one-shot dry run.")
    run_parser.add_argument("--execute", action="store_true")
    run_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    run_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")

    gate_parser = subparsers.add_parser(
        "gate", help=f"Check the read-only gate before real {service.name} implementation work."
    )
    gate_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
    gate_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")

    _add_service_launchd_parser(subparsers, service)

    if service.name == "pushd":
        fswatch_parser = subparsers.add_parser(
            "fswatch", help="Preview pushd fswatch fixture events without starting fswatch."
        )
        fswatch_subparsers = fswatch_parser.add_subparsers(dest="fswatch_command")
        fswatch_preview_parser = fswatch_subparsers.add_parser(
            "preview", help="Parse a fixture and preview the resulting upload plan."
        )
        fswatch_preview_parser.add_argument("--fixture", required=True, type=Path)
        fswatch_preview_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        fswatch_preview_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
        fswatch_probe_parser = fswatch_subparsers.add_parser(
            "probe", help="Preview the one-shot fswatch probe command without running it."
        )
        fswatch_probe_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        fswatch_probe_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
        fswatch_resident_gate_parser = fswatch_subparsers.add_parser(
            "resident-gate", help="Read-only checklist before starting a resident fswatch watcher."
        )
        fswatch_resident_gate_parser.add_argument("--report-path", type=Path)
        fswatch_resident_gate_parser.add_argument("--operator-reviewed-probe", action="store_true")
        fswatch_resident_gate_parser.add_argument("--reviewer-approved-queue-policy", action="store_true")
        fswatch_resident_gate_parser.add_argument("--reviewer-approved-process-policy", action="store_true")
        fswatch_resident_gate_parser.add_argument(
            "--json", action="store_true", help="Emit structured JSON output."
        )
        fswatch_resident_gate_parser.add_argument(
            "--xbar", action="store_true", help="Emit xbar menu output."
        )
        fswatch_resident_run_parser = fswatch_subparsers.add_parser(
            "resident-run", help="Run the foreground fswatch resident loop after the dedicated gate opens."
        )
        fswatch_resident_run_parser.add_argument("--report-path", type=Path)
        fswatch_resident_run_parser.add_argument("--operator-reviewed-probe", action="store_true")
        fswatch_resident_run_parser.add_argument("--reviewer-approved-queue-policy", action="store_true")
        fswatch_resident_run_parser.add_argument("--reviewer-approved-process-policy", action="store_true")
        fswatch_resident_run_parser.add_argument("--max-events", type=int)
        fswatch_resident_run_parser.add_argument("--execute", action="store_true")
        fswatch_resident_run_parser.add_argument(
            "--json", action="store_true", help="Emit structured JSON output."
        )
        fswatch_resident_run_parser.add_argument(
            "--xbar", action="store_true", help="Emit xbar menu output."
        )

        transfer_parser = subparsers.add_parser(
            "transfer", help="Preview upload executor commands without running transfers."
        )
        transfer_subparsers = transfer_parser.add_subparsers(dest="transfer_command")
        transfer_preview_parser = transfer_subparsers.add_parser(
            "preview", help="Emit planned upload commands from the current pushd plan."
        )
        transfer_preview_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        transfer_preview_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
        transfer_matrix_parser = transfer_subparsers.add_parser(
            "validation-matrix", help="Show read-only real upload validation matrix command examples."
        )
        transfer_matrix_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        transfer_matrix_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
        transfer_check_parser = transfer_subparsers.add_parser(
            "check", help="Read-only checklist for the real upload transfer gate."
        )
        transfer_check_parser.add_argument("--report-path", type=Path)
        transfer_check_parser.add_argument("--sample-path")
        transfer_check_parser.add_argument("--confirm-path")
        transfer_check_parser.add_argument("--confirm-direction", choices=("upload", "download"))
        transfer_check_parser.add_argument("--consume-policy", choices=_CONSUME_POLICIES)
        transfer_check_parser.add_argument("--timeout-policy", choices=_TIMEOUT_POLICIES)
        transfer_check_parser.add_argument("--final-review", action="store_true")
        transfer_check_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        transfer_check_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
        transfer_real_gate_parser = transfer_subparsers.add_parser(
            "real-gate", help="Read-only scaffold for the separate real upload execution gate."
        )
        transfer_real_gate_parser.add_argument("--report-path", type=Path)
        transfer_real_gate_parser.add_argument("--sample-path")
        transfer_real_gate_parser.add_argument("--confirm-path")
        transfer_real_gate_parser.add_argument("--confirm-direction", choices=("upload", "download"))
        transfer_real_gate_parser.add_argument("--consume-policy", choices=_CONSUME_POLICIES)
        transfer_real_gate_parser.add_argument("--timeout-policy", choices=_TIMEOUT_POLICIES)
        transfer_real_gate_parser.add_argument("--operator-reviewed-dry-run", action="store_true")
        transfer_real_gate_parser.add_argument("--reviewer-approved-real-command", action="store_true")
        transfer_real_gate_parser.add_argument("--reviewer-approved-consume-policy", action="store_true")
        transfer_real_gate_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        transfer_real_gate_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
        _add_transfer_automation_gate_parser(transfer_subparsers, direction="upload")
        transfer_real_run_parser = transfer_subparsers.add_parser(
            "real-run", help="Run guarded real upload execution only after the real-transfer gate is open."
        )
        transfer_real_run_parser.add_argument("--report-path", type=Path)
        transfer_real_run_parser.add_argument("--sample-path")
        transfer_real_run_parser.add_argument("--confirm-path")
        transfer_real_run_parser.add_argument("--confirm-direction", choices=("upload", "download"))
        transfer_real_run_parser.add_argument("--consume-policy", choices=_CONSUME_POLICIES)
        transfer_real_run_parser.add_argument("--timeout-policy", choices=_TIMEOUT_POLICIES)
        transfer_real_run_parser.add_argument("--operator-reviewed-dry-run", action="store_true")
        transfer_real_run_parser.add_argument("--reviewer-approved-real-command", action="store_true")
        transfer_real_run_parser.add_argument("--reviewer-approved-consume-policy", action="store_true")
        transfer_real_run_parser.add_argument("--execute", action="store_true")
        transfer_real_run_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        transfer_real_run_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
        transfer_run_parser = transfer_subparsers.add_parser(
            "run", help="Run dev-mode fake-rclone upload executor commands behind the transfer gate."
        )
        transfer_run_parser.add_argument("--execute", action="store_true")
        transfer_run_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        transfer_run_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
        transfer_executor_parser = transfer_subparsers.add_parser(
            "executor-run",
            help="Run one queue executor tick with fake-rclone and optional dev-state consume.",
        )
        transfer_executor_parser.add_argument("--execute", action="store_true")
        transfer_executor_parser.add_argument("--consume-on-success", action="store_true")
        transfer_executor_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        transfer_executor_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
        transfer_consume_parser = transfer_subparsers.add_parser(
            "consume", help="Preview queue consumption after successful transfer records."
        )
        transfer_consume_subparsers = transfer_consume_parser.add_subparsers(dest="consume_command")
        transfer_consume_preview_parser = transfer_consume_subparsers.add_parser("preview")
        transfer_consume_preview_parser.add_argument(
            "--json", action="store_true", help="Emit structured JSON output."
        )
        transfer_consume_preview_parser.add_argument(
            "--xbar", action="store_true", help="Emit xbar menu output."
        )

        queue_parser = subparsers.add_parser("queue", help="Preview or update pushd queue state.")
        queue_subparsers = queue_parser.add_subparsers(dest="queue_command")
        queue_add_parser = queue_subparsers.add_parser("add")
        queue_add_parser.add_argument("path")
        queue_add_parser.add_argument("--action", default="upload")
        queue_add_parser.add_argument("--reason", default="manual")
        queue_add_parser.add_argument("--execute", action="store_true")
        queue_add_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        queue_clear_parser = queue_subparsers.add_parser("clear")
        queue_clear_parser.add_argument("--execute", action="store_true")
        queue_clear_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        queue_remove_parser = queue_subparsers.add_parser("remove")
        queue_remove_parser.add_argument("path")
        queue_remove_parser.add_argument("--execute", action="store_true")
        queue_remove_parser.add_argument("--reviewer-approved-queue-record-removal", action="store_true")
        queue_remove_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        queue_prune_parser = queue_subparsers.add_parser("prune-excluded")
        queue_prune_parser.add_argument("--execute", action="store_true")
        queue_prune_parser.add_argument("--reviewer-approved-excluded-record-cleanup", action="store_true")
        queue_prune_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        queue_prune_missing_parser = queue_subparsers.add_parser("prune-missing-local")
        queue_prune_missing_parser.add_argument("--execute", action="store_true")
        queue_prune_missing_parser.add_argument("--reviewer-approved-missing-local-cleanup", action="store_true")
        queue_prune_missing_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        queue_prune_missing_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")

    if service.name == "diffd":
        diff_parser = subparsers.add_parser(
            "diff", help="Preview diffd pCloud diff fixture responses without calling the API."
        )
        diff_subparsers = diff_parser.add_subparsers(dest="diff_command")
        diff_preview_parser = diff_subparsers.add_parser(
            "preview", help="Parse a fixture and preview the resulting download plan."
        )
        diff_preview_parser.add_argument("--fixture", required=True, type=Path)
        diff_preview_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        diff_preview_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")

        api_poll_parser = subparsers.add_parser(
            "api-poll", help="Preview a one-shot pCloud API poll without calling the API."
        )
        api_poll_subparsers = api_poll_parser.add_subparsers(dest="api_poll_command")
        api_poll_preview_parser = api_poll_subparsers.add_parser(
            "preview", help="Report the intended one-shot API poll request shape."
        )
        api_poll_preview_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        api_poll_preview_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
        api_poll_long_poll_gate_parser = api_poll_subparsers.add_parser(
            "long-poll-gate", help="Read-only checklist before enabling diffd pCloud API long-poll."
        )
        api_poll_long_poll_gate_parser.add_argument("--report-path", type=Path)
        api_poll_long_poll_gate_parser.add_argument("--operator-reviewed-preview", action="store_true")
        api_poll_long_poll_gate_parser.add_argument("--reviewer-approved-response-policy", action="store_true")
        api_poll_long_poll_gate_parser.add_argument("--reviewer-approved-credential-policy", action="store_true")
        api_poll_long_poll_gate_parser.add_argument("--reviewer-approved-process-policy", action="store_true")
        api_poll_long_poll_gate_parser.add_argument(
            "--json", action="store_true", help="Emit structured JSON output."
        )
        api_poll_long_poll_gate_parser.add_argument(
            "--xbar", action="store_true", help="Emit xbar menu output."
        )
        api_poll_long_poll_run_parser = api_poll_subparsers.add_parser(
            "long-poll-run",
            help="Run guarded fixture-backed API long-poll processing after the dedicated gate opens.",
        )
        api_poll_long_poll_run_parser.add_argument("--report-path", type=Path)
        api_poll_long_poll_run_parser.add_argument("--operator-reviewed-preview", action="store_true")
        api_poll_long_poll_run_parser.add_argument("--reviewer-approved-response-policy", action="store_true")
        api_poll_long_poll_run_parser.add_argument("--reviewer-approved-credential-policy", action="store_true")
        api_poll_long_poll_run_parser.add_argument("--reviewer-approved-process-policy", action="store_true")
        api_poll_long_poll_run_parser.add_argument("--fixture", type=Path)
        api_poll_long_poll_run_parser.add_argument("--max-iterations", type=int)
        api_poll_long_poll_run_parser.add_argument("--execute", action="store_true")
        api_poll_long_poll_run_parser.add_argument(
            "--json", action="store_true", help="Emit structured JSON output."
        )
        api_poll_long_poll_run_parser.add_argument(
            "--xbar", action="store_true", help="Emit xbar menu output."
        )
        api_poll_checkpoint_parser = api_poll_subparsers.add_parser(
            "checkpoint",
            help="Set the diffd cursor to the current pCloud diffid after the dedicated gate opens.",
        )
        api_poll_checkpoint_parser.add_argument("--report-path", type=Path)
        api_poll_checkpoint_parser.add_argument("--operator-reviewed-checkpoint", action="store_true")
        api_poll_checkpoint_parser.add_argument("--reviewer-approved-checkpoint-policy", action="store_true")
        api_poll_checkpoint_parser.add_argument("--execute", action="store_true")
        api_poll_checkpoint_parser.add_argument(
            "--json", action="store_true", help="Emit structured JSON output."
        )
        api_poll_checkpoint_parser.add_argument(
            "--xbar", action="store_true", help="Emit xbar menu output."
        )

        transfer_parser = subparsers.add_parser(
            "transfer", help="Preview download executor commands without running transfers."
        )
        transfer_subparsers = transfer_parser.add_subparsers(dest="transfer_command")
        transfer_preview_parser = transfer_subparsers.add_parser(
            "preview", help="Emit planned download commands from the current diffd plan."
        )
        transfer_preview_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        transfer_preview_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
        transfer_matrix_parser = transfer_subparsers.add_parser(
            "validation-matrix", help="Show read-only real download validation matrix command examples."
        )
        transfer_matrix_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        transfer_matrix_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
        transfer_check_parser = transfer_subparsers.add_parser(
            "check", help="Read-only checklist for the real download transfer gate."
        )
        transfer_check_parser.add_argument("--report-path", type=Path)
        transfer_check_parser.add_argument("--sample-path")
        transfer_check_parser.add_argument("--confirm-path")
        transfer_check_parser.add_argument("--confirm-direction", choices=("upload", "download"))
        transfer_check_parser.add_argument("--consume-policy", choices=_CONSUME_POLICIES)
        transfer_check_parser.add_argument("--timeout-policy", choices=_TIMEOUT_POLICIES)
        transfer_check_parser.add_argument("--final-review", action="store_true")
        transfer_check_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        transfer_check_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
        transfer_real_gate_parser = transfer_subparsers.add_parser(
            "real-gate", help="Read-only scaffold for the separate real download execution gate."
        )
        transfer_real_gate_parser.add_argument("--report-path", type=Path)
        transfer_real_gate_parser.add_argument("--sample-path")
        transfer_real_gate_parser.add_argument("--confirm-path")
        transfer_real_gate_parser.add_argument("--confirm-direction", choices=("upload", "download"))
        transfer_real_gate_parser.add_argument("--consume-policy", choices=_CONSUME_POLICIES)
        transfer_real_gate_parser.add_argument("--timeout-policy", choices=_TIMEOUT_POLICIES)
        transfer_real_gate_parser.add_argument("--operator-reviewed-dry-run", action="store_true")
        transfer_real_gate_parser.add_argument("--reviewer-approved-real-command", action="store_true")
        transfer_real_gate_parser.add_argument("--reviewer-approved-consume-policy", action="store_true")
        transfer_real_gate_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        transfer_real_gate_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
        _add_transfer_automation_gate_parser(transfer_subparsers, direction="download")
        transfer_real_run_parser = transfer_subparsers.add_parser(
            "real-run", help="Run guarded real download execution only after the real-transfer gate is open."
        )
        transfer_real_run_parser.add_argument("--report-path", type=Path)
        transfer_real_run_parser.add_argument("--sample-path")
        transfer_real_run_parser.add_argument("--confirm-path")
        transfer_real_run_parser.add_argument("--confirm-direction", choices=("upload", "download"))
        transfer_real_run_parser.add_argument("--consume-policy", choices=_CONSUME_POLICIES)
        transfer_real_run_parser.add_argument("--timeout-policy", choices=_TIMEOUT_POLICIES)
        transfer_real_run_parser.add_argument("--operator-reviewed-dry-run", action="store_true")
        transfer_real_run_parser.add_argument("--reviewer-approved-real-command", action="store_true")
        transfer_real_run_parser.add_argument("--reviewer-approved-consume-policy", action="store_true")
        transfer_real_run_parser.add_argument("--execute", action="store_true")
        transfer_real_run_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        transfer_real_run_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
        transfer_run_parser = transfer_subparsers.add_parser(
            "run", help="Run dev-mode fake-rclone download executor commands behind the transfer gate."
        )
        transfer_run_parser.add_argument("--execute", action="store_true")
        transfer_run_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        transfer_run_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
        transfer_executor_parser = transfer_subparsers.add_parser(
            "executor-run",
            help="Run one queue executor tick with fake-rclone and optional dev-state consume.",
        )
        transfer_executor_parser.add_argument("--execute", action="store_true")
        transfer_executor_parser.add_argument("--consume-on-success", action="store_true")
        transfer_executor_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        transfer_executor_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
        transfer_consume_parser = transfer_subparsers.add_parser(
            "consume", help="Preview remote-change consumption after successful transfer records."
        )
        transfer_consume_subparsers = transfer_consume_parser.add_subparsers(dest="consume_command")
        transfer_consume_preview_parser = transfer_consume_subparsers.add_parser("preview")
        transfer_consume_preview_parser.add_argument(
            "--json", action="store_true", help="Emit structured JSON output."
        )
        transfer_consume_preview_parser.add_argument(
            "--xbar", action="store_true", help="Emit xbar menu output."
        )
        transfer_consume_run_parser = transfer_consume_subparsers.add_parser("run")
        transfer_consume_run_parser.add_argument("--execute", action="store_true")
        transfer_consume_run_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        transfer_consume_run_parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")

        remote_parser = subparsers.add_parser(
            "remote-change", help="Preview or update diffd remote change state."
        )
        remote_subparsers = remote_parser.add_subparsers(dest="remote_change_command")
        remote_add_parser = remote_subparsers.add_parser("add")
        remote_add_parser.add_argument("path")
        remote_add_parser.add_argument("--action", default="download")
        remote_add_parser.add_argument("--reason", default="manual")
        remote_add_parser.add_argument("--execute", action="store_true")
        remote_add_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        remote_clear_parser = remote_subparsers.add_parser("clear")
        remote_clear_parser.add_argument("--execute", action="store_true")
        remote_clear_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")
        remote_remove_parser = remote_subparsers.add_parser("remove")
        remote_remove_parser.add_argument("path")
        remote_remove_parser.add_argument("--execute", action="store_true")
        remote_remove_parser.add_argument("--json", action="store_true", help="Emit structured JSON output.")

    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    args.command = service.name
    args.service_name = service.name
    paths = detect_runtime_paths()
    result = cmd_service_daemon(args, paths)
    if result is not None:
        return result
    parser.print_help()
    return 1


def main_pushd(argv: list[str] | None = None) -> int:
    return _standalone_main("pushd", argv)


def main_diffd(argv: list[str] | None = None) -> int:
    return _standalone_main("diffd", argv)
