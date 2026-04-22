from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class ReportIssue:
    level: str
    key: str
    message: str


@dataclass(frozen=True)
class CommandReport:
    command: str
    status: str
    summary: str
    details: dict[str, Any] = field(default_factory=dict)
    issues: list[ReportIssue] = field(default_factory=list)
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
        }


def render_report(report: CommandReport, as_json: bool = False) -> str:
    if as_json:
        return json.dumps(report.to_payload(), indent=2, ensure_ascii=False, sort_keys=True)

    lines = [f"{report.command}: {report.status}", report.summary]
    for key, value in report.details.items():
        lines.append(f"{key}: {value}")
    if report.issues:
        lines.append("issues:")
        for issue in report.issues:
            lines.append(f"- {issue.level}: {issue.key}: {issue.message}")
    return "\n".join(lines)
