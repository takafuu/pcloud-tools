from __future__ import annotations

import argparse
import shlex

from .chat_notify import chat_notify_status, send_chat_notification, set_chat_notify_enabled
from .config import ConfigIssue, load_config
from .output import CommandReport, ReportAction, ReportIssue, render_report
from .runtime import RuntimePaths, action_entrypoint_command


def add_notify_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("notify", help="Inspect or toggle abnormal chat notifications.")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")
    notify_subparsers = parser.add_subparsers(dest="notify_command")
    for name, help_text in (
        ("status", "Show chat notification state."),
        ("enable", "Enable abnormal chat notifications."),
        ("disable", "Disable all chat notifications."),
        ("test", "Send one explicit test notification."),
    ):
        sub = notify_subparsers.add_parser(name, help=help_text)
        sub.add_argument("--json", action="store_true")
        sub.add_argument("--xbar", action="store_true", help="Emit xbar menu output.")


def _action_command(paths: RuntimePaths, action_id: str) -> list[str]:
    return [action_entrypoint_command(paths), "action", action_id]


def _actions(paths: RuntimePaths) -> list[ReportAction]:
    return [
        ReportAction(
            id="notify.chat.status",
            label="Refresh Discord notify status",
            command=tuple(_action_command(paths, "notify.chat.status")),
            terminal=False,
            refresh=True,
        ),
        ReportAction(
            id="notify.chat.enable",
            label="Discord notify ON",
            command=tuple(_action_command(paths, "notify.chat.enable")),
            terminal=False,
            refresh=True,
        ),
        ReportAction(
            id="notify.chat.disable",
            label="Discord notify OFF",
            command=tuple(_action_command(paths, "notify.chat.disable")),
            terminal=False,
            refresh=True,
        ),
        ReportAction(
            id="notify.chat.test",
            label="Send Discord notify test",
            command=tuple(_action_command(paths, "notify.chat.test")),
            terminal=False,
            refresh=False,
        ),
    ]


def _report(args: argparse.Namespace, paths: RuntimePaths) -> CommandReport:
    command = getattr(args, "notify_command", None) or "status"
    if command in {"enable", "disable"}:
        set_chat_notify_enabled(paths.env_file, command == "enable")
    load_result = load_config(paths)
    config = load_result.config
    issues = list(load_result.issues)
    notify_result = None
    if command == "test":
        notify_result = send_chat_notification(
            config,
            "pcloud-manager notify test",
            force=True,
        )
        if notify_result.issue:
            issues.append(notify_result.issue)
    elif command not in {"status", "enable", "disable"}:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_NOTIFY_COMMAND",
                level="error",
                message=f"unknown notify command: {command}",
            )
        )
    details = {
        "planned action": f"{command} chat notification",
        "state writes": str(paths.env_file) if command in {"enable", "disable"} else "none",
        "normal success notifications": "no",
        "abnormal event notifications": "yes" if config.chat_notify_enabled else "no",
        **chat_notify_status(config),
    }
    if notify_result:
        details["chat notify test result"] = notify_result.as_dict()
    if command == "enable":
        summary = "chat notifications enabled for abnormal events"
    elif command == "disable":
        summary = "chat notifications disabled"
    elif command == "test":
        summary = "chat notification test attempted"
    else:
        summary = "chat notification status is ready"
    return CommandReport(
        command=f"notify {command}",
        status="error" if any(issue.level == "error" for issue in issues) else "ok",
        summary=summary,
        details=details,
        issues=[ReportIssue(level=issue.level, key=issue.key, message=issue.message) for issue in issues],
        actions=_actions(paths),
    )


def _xbar_escape(value: object) -> str:
    return str(value).replace("\n", " ").replace("|", "/")


def _xbar_action(action: ReportAction) -> str:
    fields = [
        f"bash={shlex.quote(action.command[0])}",
        f"terminal={'true' if action.terminal else 'false'}",
        f"refresh={'true' if action.refresh else 'false'}",
    ]
    for index, arg in enumerate(action.command[1:], start=1):
        fields.append(f"param{index}={shlex.quote(arg)}")
    return f"{_xbar_escape(action.label)} | {' '.join(fields)}"


def _print_report(report: CommandReport, args: argparse.Namespace) -> None:
    if getattr(args, "xbar", False):
        lines = [
            f"Discord notify: {report.details.get('chat notify mode', '-')}",
            "---",
            _xbar_escape(report.summary),
        ]
        for action in report.actions:
            lines.append(_xbar_action(action))
        print("\n".join(lines))
        return
    output_format = "json" if getattr(args, "json", False) else "human"
    print(render_report(report, output_format=output_format))


def cmd_notify(args: argparse.Namespace, paths: RuntimePaths) -> int:
    report = _report(args, paths)
    _print_report(report, args)
    return 1 if report.status == "error" else 0
