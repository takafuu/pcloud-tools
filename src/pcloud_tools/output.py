from __future__ import annotations

import json
import shlex
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class ReportIssue:
    level: str
    key: str
    message: str


@dataclass(frozen=True)
class ReportAction:
    id: str
    label: str
    command: tuple[str, ...]
    refresh: bool = True
    terminal: bool = False


@dataclass(frozen=True)
class CommandReport:
    command: str
    status: str
    summary: str
    details: dict[str, Any] = field(default_factory=dict)
    issues: list[ReportIssue] = field(default_factory=list)
    actions: list[ReportAction] = field(default_factory=list)
    schema_version: str = "pcloud-tools-report.v1"

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "command": self.command,
            "status": self.status,
            "summary": self.summary,
            "details": self.details,
            "issues": [asdict(issue) for issue in self.issues],
            "actions": [asdict(action) for action in self.actions],
        }


def render_report(report: CommandReport, as_json: bool = False, output_format: str = "human") -> str:
    if as_json:
        output_format = "json"

    if output_format == "json":
        return json.dumps(report.to_payload(), indent=2, ensure_ascii=False, sort_keys=True)
    if output_format == "xbar":
        return _render_xbar(report)

    lines = [f"{report.command}: {report.status}", report.summary]
    for key, value in report.details.items():
        if isinstance(value, list):
            if value:
                lines.append(f"{key}:")
                for item in value:
                    lines.append(f"- {item}")
            else:
                lines.append(f"{key}: []")
            continue
        lines.append(f"{key}: {value}")
    if report.issues:
        lines.append("issues:")
        for issue in report.issues:
            lines.append(f"- {issue.level}: {issue.key}: {issue.message}")
    return "\n".join(lines)


def _xbar_escape(value: object) -> str:
    return str(value).replace("\n", " ").replace("|", "/")


def _xbar_status_label(status: str) -> str:
    if status == "ok":
        return "OK"
    if status == "warning":
        return "WARN"
    return "ERR"


def _render_xbar_action(action: ReportAction) -> str:
    fields = [
        f"bash={shlex.quote(action.command[0])}",
        f"terminal={'true' if action.terminal else 'false'}",
        f"refresh={'true' if action.refresh else 'false'}",
    ]
    for index, arg in enumerate(action.command[1:], start=1):
        fields.append(f"param{index}={shlex.quote(arg)}")
    return f"{_xbar_escape(action.label)} | {' '.join(fields)}"


def _render_xbar(report: CommandReport) -> str:
    lines = [f"pCloud {_xbar_status_label(report.status)}", "---"]
    lines.append(_xbar_escape(report.summary))
    if report.issues:
        lines.append("---")
        for issue in report.issues:
            lines.append(f"{issue.level}: {_xbar_escape(issue.message)}")
    if report.details:
        lines.append("---")
        for key, value in report.details.items():
            if isinstance(value, list):
                lines.append(f"{_xbar_escape(key)}: {len(value)}")
            else:
                lines.append(f"{_xbar_escape(key)}: {_xbar_escape(value)}")
    if report.actions:
        lines.append("---")
        for action in report.actions:
            lines.append(_render_xbar_action(action))
    return "\n".join(lines)
