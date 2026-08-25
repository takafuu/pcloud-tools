from __future__ import annotations

import argparse
import shlex

from ..cli_common import output_format, print_report
from ..output import CommandReport, ReportAction


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


def _service_status_xbar_action_ids(service_name: str) -> set[str]:
    common = {
        f"{service_name}.status.refresh",
        f"{service_name}.preview",
        f"{service_name}.launchd.status",
        f"{service_name}.launchd.gate",
        f"{service_name}.transfer.check",
    }
    if service_name == "pushd":
        common.add("pushd.fswatch.resident-gate")
    else:
        common.add("diffd.api-poll.long-poll-gate")
    return common


def render_service_status_xbar(report: CommandReport, service_name: str) -> str:
    details = report.details
    conflict_line = (
        f"conflicts={details.get('download conflict count', '-')}; "
        f"latest={details.get('download latest conflict', '-')}"
    )
    if service_name == "pushd":
        plan_line = (
            f"plan: uploads={details.get('planned uploads', '-')}; "
            f"vanished={details.get('vanished local candidates', details.get('missing local upload records', '-'))}; "
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
    allowed_actions = _service_status_xbar_action_ids(service_name)
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
        _xbar_escape(
            f"vanished local candidates: "
            f"{details.get('vanished local candidates', details.get('missing local upload records', '-'))}"
        )
        if service_name == "pushd"
        else _xbar_escape("vanished local candidates: -"),
        _xbar_escape(f"upload echo: suppressed={details.get('upload origin completed', '-')}"),
        _xbar_escape(notify_line),
    ]
    if service_name == "pushd":
        missing_records = details.get("missing local upload record details", [])
        if isinstance(missing_records, list) and missing_records:
            lines.append("---")
            lines.append(_xbar_escape("Vanished local candidates (automatic cleanup)"))
            for record in missing_records[:5]:
                if not isinstance(record, dict):
                    continue
                reason = record.get("reason", "-")
                lines.append(_xbar_escape(f"{record.get('path', '-')} ({reason})"))
            if len(missing_records) > 5:
                lines.append(_xbar_escape(f"... and {len(missing_records) - 5} more"))
    manual_records = details.get("manual review transfer record details", [])
    if isinstance(manual_records, list) and manual_records:
        review_action = next(
            (action for action in report.actions if action.id == f"{service_name}.transfer.preview"),
            None,
        )
        lines.append("---")
        if review_action is not None:
            lines.append(
                _xbar_action(
                    ReportAction(
                        id=review_action.id,
                        label=f"Review pending {service_name} items ({len(manual_records)})",
                        command=review_action.command,
                        refresh=False,
                        terminal=True,
                    )
                )
            )
        else:
            lines.append(_xbar_escape(f"Manual review records: {len(manual_records)}"))
        for record in manual_records[:5]:
            if not isinstance(record, dict):
                continue
            lines.append(_xbar_escape(f"--{record.get('path', '-')} ({record.get('action', '-')})"))
        if len(manual_records) > 5:
            lines.append(_xbar_escape(f"--... and {len(manual_records) - 5} more"))
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


def print_service_status_report(report: CommandReport, args: argparse.Namespace, service_name: str) -> None:
    if output_format(args) == "xbar":
        print(render_service_status_xbar(report, service_name))
        return
    print_report(report, args)
