"""Read only inspection and explicit recovery for interrupted transfer attempts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import ConfigIssue
from .transfer_state import (
    AttemptResult,
    StateIssue,
    unresolved_attempts,
    update_attempt,
)


@dataclass(frozen=True)
class RecoveryCandidate:
    attempt_id: str
    service: str
    phase: str
    status: str
    reason: str
    child_pids: tuple[int, ...]
    requires_child_exit_confirmation: bool
    payload: dict[str, Any]


@dataclass(frozen=True)
class RecoveryReport:
    service: str
    state_file: Path
    candidates: tuple[RecoveryCandidate, ...]
    issues: tuple[ConfigIssue, ...] = ()


def _candidate(item: dict[str, Any], service: str) -> RecoveryCandidate:
    child_pids = tuple(value for value in item.get("child_pids", []) if isinstance(value, int))
    phase = str(item.get("phase", "unknown"))
    status = str(item.get("status", "unknown"))
    reason = str(item.get("hold_reason") or item.get("failure_reason") or "attempt is incomplete")
    return RecoveryCandidate(
        attempt_id=str(item.get("attempt_id", "")),
        service=str(item.get("service", service)),
        phase=phase,
        status=status,
        reason=reason,
        child_pids=child_pids,
        requires_child_exit_confirmation=bool(item.get("requires_child_exit_confirmation", True)),
        payload=dict(item),
    )


def inspect_recovery(state_dir: Path, service: str) -> RecoveryReport:
    """Return unresolved attempts without changing any state."""

    state_file = state_dir / service / "transfer-attempts.json"
    candidates = tuple(_candidate(item, service) for item in unresolved_attempts(state_dir, service))
    return RecoveryReport(service=service, state_file=state_file, candidates=candidates)


def preview_recovery(state_dir: Path, service: str, attempt_id: str | None = None) -> RecoveryReport:
    report = inspect_recovery(state_dir, service)
    if not attempt_id:
        return report
    return RecoveryReport(
        service=service,
        state_file=report.state_file,
        candidates=tuple(item for item in report.candidates if item.attempt_id == attempt_id),
        issues=report.issues,
    )


def recover_attempt(
    state_dir: Path,
    service: str,
    attempt_id: str,
    *,
    child_exit_confirmed: bool,
    writers_stopped: bool,
    latest_event_ids_rechecked: bool,
    local_fingerprints_rechecked: bool,
) -> AttemptResult:
    """Move one held attempt back to the scheduler only after explicit checks.

    No successful receipt is generated and no queue record is consumed here.
    """

    missing: list[str] = []
    if not child_exit_confirmed:
        missing.append("child exit confirmation")
    if not writers_stopped:
        missing.append("all queue/journal writers stopped")
    if not latest_event_ids_rechecked:
        missing.append("latest event_id recheck")
    if not local_fingerprints_rechecked:
        missing.append("local fingerprint recheck")
    if missing:
        return AttemptResult(
            attempt_id=attempt_id,
            file=state_dir / service / "transfer-attempts.json",
            phase="needs-recovery",
            status="blocked",
            issue=StateIssue(
                key="PCLOUD_TOOLS_TRANSFER_RECOVERY_CONFIRMATION",
                message="recovery requires: " + ", ".join(missing),
            ),
        )
    return update_attempt(
        state_dir,
        service,
        attempt_id,
        phase="released",
        status="released",
        hold_reason="explicit recovery checks completed; candidate must be re-evaluated",
        child_exit_confirmed=True,
        writers_stopped=True,
        latest_event_ids_rechecked=True,
        local_fingerprints_rechecked=True,
    )


def recovery_issues(report: RecoveryReport) -> list[ConfigIssue]:
    issues: list[ConfigIssue] = list(report.issues)
    for candidate in report.candidates:
        issues.append(
            ConfigIssue(
                key="PCLOUD_TOOLS_TRANSFER_ATTEMPT_PENDING",
                level="warning",
                message=(
                    f"attempt {candidate.attempt_id} is held at phase {candidate.phase}: "
                    f"{candidate.reason}; child exit confirmation required="
                    f"{'yes' if candidate.requires_child_exit_confirmation else 'no'}"
                ),
            )
        )
    return issues


__all__ = [
    "RecoveryCandidate",
    "RecoveryReport",
    "inspect_recovery",
    "preview_recovery",
    "recover_attempt",
    "recovery_issues",
]
