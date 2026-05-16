from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class GateSpec:
    name: str
    env_var: str
    expected_value: str
    approval_flags: tuple[str, ...]
    summary: str


@dataclass(frozen=True)
class GateFlagStatus:
    flag: str
    attr: str
    approved: bool


@dataclass(frozen=True)
class GateValidation:
    spec: GateSpec
    env_value: str | None
    env_ok: bool
    flag_statuses: tuple[GateFlagStatus, ...]

    @property
    def flags_ok(self) -> bool:
        return all(status.approved for status in self.flag_statuses)

    @property
    def complete(self) -> bool:
        return self.env_ok and self.flags_ok

    @property
    def missing_flags(self) -> tuple[str, ...]:
        return tuple(status.flag for status in self.flag_statuses if not status.approved)

    def flag_ok(self, flag: str) -> bool:
        for status in self.flag_statuses:
            if status.flag == flag:
                return status.approved
        raise KeyError(flag)


GATES: dict[str, GateSpec] = {
    "pushd.launchd.gate": GateSpec(
        name="pushd.launchd.gate",
        env_var="PCLOUD_TOOLS_PUSHD_LAUNCHD_GATE",
        expected_value="operator-approved-pushd-launchd-v1",
        approval_flags=(
            "--operator-reviewed-daemon-command",
            "--reviewer-approved-plist-policy",
            "--reviewer-approved-launchctl-policy",
            "--reviewer-approved-rollback-policy",
        ),
        summary="launchd registration",
    ),
    "diffd.launchd.gate": GateSpec(
        name="diffd.launchd.gate",
        env_var="PCLOUD_TOOLS_DIFFD_LAUNCHD_GATE",
        expected_value="operator-approved-diffd-launchd-v1",
        approval_flags=(
            "--operator-reviewed-daemon-command",
            "--reviewer-approved-plist-policy",
            "--reviewer-approved-launchctl-policy",
            "--reviewer-approved-rollback-policy",
        ),
        summary="launchd registration",
    ),
    "pushd.launchd.plist": GateSpec(
        name="pushd.launchd.plist",
        env_var="PCLOUD_TOOLS_PUSHD_LAUNCHD_PLIST_GATE",
        expected_value="operator-approved-pushd-launchd-plist-v1",
        approval_flags=(
            "--operator-reviewed-plist",
            "--reviewer-approved-public-target",
            "--reviewer-approved-no-bootstrap",
        ),
        summary="public launchd plist write",
    ),
    "diffd.launchd.plist": GateSpec(
        name="diffd.launchd.plist",
        env_var="PCLOUD_TOOLS_DIFFD_LAUNCHD_PLIST_GATE",
        expected_value="operator-approved-diffd-launchd-plist-v1",
        approval_flags=(
            "--operator-reviewed-plist",
            "--reviewer-approved-public-target",
            "--reviewer-approved-no-bootstrap",
        ),
        summary="public launchd plist write",
    ),
    "pushd.launchd.reload": GateSpec(
        name="pushd.launchd.reload",
        env_var="PCLOUD_TOOLS_PUSHD_LAUNCHD_RELOAD_GATE",
        expected_value="operator-approved-pushd-launchd-reload-v1",
        approval_flags=(
            "--reviewer-approved-bootout-bootstrap",
            "--reviewer-approved-rollback-policy",
        ),
        summary="launchd bootout/bootstrap reload",
    ),
    "diffd.launchd.reload": GateSpec(
        name="diffd.launchd.reload",
        env_var="PCLOUD_TOOLS_DIFFD_LAUNCHD_RELOAD_GATE",
        expected_value="operator-approved-diffd-launchd-reload-v1",
        approval_flags=(
            "--reviewer-approved-bootout-bootstrap",
            "--reviewer-approved-rollback-policy",
        ),
        summary="launchd bootout/bootstrap reload",
    ),
    "pushd.launchd.automation-plist": GateSpec(
        name="pushd.launchd.automation-plist",
        env_var="PCLOUD_TOOLS_PUSHD_LAUNCHD_AUTOMATION_PLIST_GATE",
        expected_value="operator-approved-pushd-launchd-automation-plist-v1",
        approval_flags=(
            "--operator-reviewed-automation-command",
            "--reviewer-approved-automation-environment",
            "--reviewer-approved-no-bootstrap",
        ),
        summary="public real-transfer automation plist write",
    ),
    "diffd.launchd.automation-plist": GateSpec(
        name="diffd.launchd.automation-plist",
        env_var="PCLOUD_TOOLS_DIFFD_LAUNCHD_AUTOMATION_PLIST_GATE",
        expected_value="operator-approved-diffd-launchd-automation-plist-v1",
        approval_flags=(
            "--operator-reviewed-automation-command",
            "--reviewer-approved-automation-environment",
            "--reviewer-approved-no-bootstrap",
        ),
        summary="public real-transfer automation plist write",
    ),
    "pushd.launchd.automation-reload": GateSpec(
        name="pushd.launchd.automation-reload",
        env_var="PCLOUD_TOOLS_PUSHD_LAUNCHD_AUTOMATION_RELOAD_GATE",
        expected_value="operator-approved-pushd-launchd-automation-reload-v1",
        approval_flags=(
            "--operator-reviewed-automation-plist",
            "--reviewer-approved-bootout-bootstrap",
        ),
        summary="public real-transfer automation launchd reload",
    ),
    "diffd.launchd.automation-reload": GateSpec(
        name="diffd.launchd.automation-reload",
        env_var="PCLOUD_TOOLS_DIFFD_LAUNCHD_AUTOMATION_RELOAD_GATE",
        expected_value="operator-approved-diffd-launchd-automation-reload-v1",
        approval_flags=(
            "--operator-reviewed-automation-plist",
            "--reviewer-approved-bootout-bootstrap",
        ),
        summary="public real-transfer automation launchd reload",
    ),
    "pushd.fswatch.resident": GateSpec(
        name="pushd.fswatch.resident",
        env_var="PCLOUD_TOOLS_PUSHD_FSWATCH_RESIDENT_GATE",
        expected_value="operator-approved-fswatch-resident-v1",
        approval_flags=(
            "--operator-reviewed-probe",
            "--reviewer-approved-queue-policy",
            "--reviewer-approved-process-policy",
        ),
        summary="fswatch resident watcher",
    ),
    "pushd.queue.remove": GateSpec(
        name="pushd.queue.remove",
        env_var="PCLOUD_TOOLS_PUSHD_QUEUE_REMOVE_GATE",
        expected_value="operator-approved-pushd-queue-remove-v1",
        approval_flags=(
            "--reviewer-approved-queue-record-removal",
        ),
        summary="pushd queue record removal",
    ),
    "pushd.queue.prune-excluded": GateSpec(
        name="pushd.queue.prune-excluded",
        env_var="PCLOUD_TOOLS_PUSHD_QUEUE_PRUNE_EXCLUDED_GATE",
        expected_value="operator-approved-pushd-queue-prune-excluded-v1",
        approval_flags=(
            "--reviewer-approved-excluded-record-cleanup",
        ),
        summary="pushd excluded queue record cleanup",
    ),
    "transfer.execution": GateSpec(
        name="transfer.execution",
        env_var="PCLOUD_TOOLS_TRANSFER_EXECUTION_GATE",
        expected_value="dev-fake-rclone",
        approval_flags=(),
        summary="dev fake-rclone transfer execution",
    ),
    "real_transfer.execution": GateSpec(
        name="real_transfer.execution",
        env_var="PCLOUD_TOOLS_REAL_TRANSFER_EXECUTION_GATE",
        expected_value="operator-approved-real-transfer-v1",
        approval_flags=(
            "--operator-reviewed-dry-run",
            "--reviewer-approved-real-command",
            "--reviewer-approved-consume-policy",
        ),
        summary="real transfer execution",
    ),
    "real_transfer.automation": GateSpec(
        name="real_transfer.automation",
        env_var="PCLOUD_TOOLS_REAL_TRANSFER_AUTOMATION_GATE",
        expected_value="operator-approved-real-transfer-automation-v1",
        approval_flags=(
            "--operator-reviewed-real-transfer-gate",
            "--reviewer-approved-automation-command",
            "--reviewer-approved-launchd-policy",
            "--reviewer-approved-rollback-policy",
        ),
        summary="real transfer automation gate",
    ),
    "real_transfer.automation-run": GateSpec(
        name="real_transfer.automation-run",
        env_var="PCLOUD_TOOLS_REAL_TRANSFER_AUTOMATION_RUN_GATE",
        expected_value="operator-approved-real-transfer-automation-run-v1",
        approval_flags=(),
        summary="real transfer automation run",
    ),
    "diffd.api.long-poll": GateSpec(
        name="diffd.api.long-poll",
        env_var="PCLOUD_TOOLS_DIFFD_API_LONG_POLL_GATE",
        expected_value="operator-approved-api-long-poll-v1",
        approval_flags=(
            "--operator-reviewed-preview",
            "--reviewer-approved-response-policy",
            "--reviewer-approved-credential-policy",
            "--reviewer-approved-process-policy",
        ),
        summary="diffd live API long-poll",
    ),
    "diffd.api.catchup": GateSpec(
        name="diffd.api.catchup",
        env_var="PCLOUD_TOOLS_DIFFD_API_CATCHUP_GATE",
        expected_value="operator-approved-api-catchup-v1",
        approval_flags=(
            "--reviewer-approved-catchup-policy",
        ),
        summary="diffd live API catch-up",
    ),
    "diffd.api.checkpoint": GateSpec(
        name="diffd.api.checkpoint",
        env_var="PCLOUD_TOOLS_DIFFD_API_CHECKPOINT_GATE",
        expected_value="operator-approved-api-checkpoint-v1",
        approval_flags=(
            "--operator-reviewed-checkpoint",
            "--reviewer-approved-checkpoint-policy",
        ),
        summary="diffd API checkpoint",
    ),
    "pushd.launchd.resident-plist": GateSpec(
        name="pushd.launchd.resident-plist",
        env_var="PCLOUD_TOOLS_PUSHD_LAUNCHD_RESIDENT_PLIST_GATE",
        expected_value="operator-approved-pushd-launchd-resident-plist-v1",
        approval_flags=(
            "--operator-reviewed-resident-command",
            "--reviewer-approved-resident-environment",
            "--reviewer-approved-no-bootstrap",
        ),
        summary="launchd resident plist write",
    ),
    "diffd.launchd.long-poll-plist": GateSpec(
        name="diffd.launchd.long-poll-plist",
        env_var="PCLOUD_TOOLS_DIFFD_LAUNCHD_LONG_POLL_PLIST_GATE",
        expected_value="operator-approved-diffd-launchd-long-poll-plist-v1",
        approval_flags=(
            "--operator-reviewed-resident-command",
            "--reviewer-approved-resident-environment",
            "--reviewer-approved-no-bootstrap",
        ),
        summary="launchd long-poll plist write",
    ),
}


def add_gate_review_args(parser: argparse.ArgumentParser, spec: GateSpec) -> None:
    for flag in spec.approval_flags:
        parser.add_argument(
            flag,
            action="store_true",
            help=f"Reviewer approval for {spec.summary} ({flag.lstrip('-')}).",
        )


def validate_gate(spec: GateSpec, args: argparse.Namespace, env: Mapping[str, str]) -> GateValidation:
    env_value = env.get(spec.env_var)
    flag_statuses = tuple(
        GateFlagStatus(
            flag=flag,
            attr=_flag_attr(flag),
            approved=bool(getattr(args, _flag_attr(flag), False)),
        )
        for flag in spec.approval_flags
    )
    return GateValidation(
        spec=spec,
        env_value=env_value,
        env_ok=env_value == spec.expected_value,
        flag_statuses=flag_statuses,
    )


def _flag_attr(flag: str) -> str:
    return flag.lstrip("-").replace("-", "_")
