"""Durable Human-in-the-Loop (HITL) approval records, verification, and adapter."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from contracts.agent import (
    AgentActor,
    ApprovalDecision,
    ApprovalPort,
    CapabilityMetadata,
)
from contracts.workflow import ApprovalStatus, WorkflowDefinitionRef

from .errors import (
    ApprovalMismatchError,
    ApprovalReplayError,
    ApprovalVerificationError,
)


def canonicalize_arguments(arguments: Mapping[str, Any]) -> str:
    """Produce deterministic JSON representation with sorted keys and normalized separators."""
    return json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def resolve_current_actor(
    actor_id: str,
    resolver: Callable[[str], AgentActor | None] | None,
) -> AgentActor:
    """Resolve privileges through host-owned identity data, never a saved snapshot."""
    if resolver is None:
        raise PermissionError("No trusted current-actor lookup configured.")
    actor = resolver(actor_id)
    if not isinstance(actor, AgentActor) or actor.actor_id != actor_id:
        raise PermissionError(f"Current actor '{actor_id}' is unavailable or mismatched.")
    return actor


@dataclass(frozen=True)
class DurableApprovalRecord:
    """Durable approval authorization record stored in SQLite across process restarts."""

    approval_id: str
    workflow_id: str
    workflow_definition_id: str
    workflow_definition_version: str
    action_id: str
    capability_name: str
    canonical_arguments_json: str
    actor_id: str
    actor_role: str
    status: ApprovalStatus = ApprovalStatus.PENDING
    approver_id: str | None = None
    approver_role: str | None = None
    reason: str | None = None
    requested_at: float = field(default_factory=time.time)
    decided_at: float | None = None
    consumed_at: float | None = None
    execution_receipt_id: str | None = None
    expires_at: float | None = None

    def __post_init__(self) -> None:
        if isinstance(self.status, str):
            object.__setattr__(self, "status", ApprovalStatus(self.status))

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "workflow_id": self.workflow_id,
            "workflow_definition_id": self.workflow_definition_id,
            "workflow_definition_version": self.workflow_definition_version,
            "action_id": self.action_id,
            "capability_name": self.capability_name,
            "canonical_arguments_json": self.canonical_arguments_json,
            "actor_id": self.actor_id,
            "actor_role": self.actor_role,
            "status": self.status.value,
            "approver_id": self.approver_id,
            "approver_role": self.approver_role,
            "reason": self.reason,
            "requested_at": self.requested_at,
            "decided_at": self.decided_at,
            "consumed_at": self.consumed_at,
            "execution_receipt_id": self.execution_receipt_id,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DurableApprovalRecord:
        return cls(
            approval_id=str(data["approval_id"]),
            workflow_id=str(data["workflow_id"]),
            workflow_definition_id=str(data["workflow_definition_id"]),
            workflow_definition_version=str(data["workflow_definition_version"]),
            action_id=str(data["action_id"]),
            capability_name=str(data["capability_name"]),
            canonical_arguments_json=str(data["canonical_arguments_json"]),
            actor_id=str(data["actor_id"]),
            actor_role=str(data["actor_role"]),
            status=ApprovalStatus(str(data["status"])),
            approver_id=data.get("approver_id"),
            approver_role=data.get("approver_role"),
            reason=data.get("reason"),
            requested_at=float(data.get("requested_at", time.time())),
            decided_at=float(data["decided_at"]) if data.get("decided_at") is not None else None,
            consumed_at=float(data["consumed_at"]) if data.get("consumed_at") is not None else None,
            execution_receipt_id=data.get("execution_receipt_id"),
            expires_at=float(data["expires_at"]) if data.get("expires_at") is not None else None,
        )


class WorkflowApprovalVerifier:
    """Pre-execution verification of lifecycle, definition and execution bindings."""

    @staticmethod
    def verify(
        record: DurableApprovalRecord,
        expected_workflow_id: str,
        expected_action_id: str,
        capability: CapabilityMetadata,
        arguments: Mapping[str, Any],
        actor: AgentActor,
        expected_definition: WorkflowDefinitionRef,
    ) -> None:
        """Verify that a durable approval record authorizes the intended capability execution."""
        # 1. Check approval lifecycle status
        if record.status == ApprovalStatus.CONSUMED:
            raise ApprovalReplayError(
                f"Approval '{record.approval_id}' has already been consumed at timestamp {record.consumed_at}."
            )
        if record.status != ApprovalStatus.APPROVED:
            raise ApprovalVerificationError(
                f"Approval '{record.approval_id}' is in status '{record.status.value}', expected 'approved'."
            )

        # 2. Check workflow binding
        if record.workflow_id != expected_workflow_id:
            raise ApprovalMismatchError(
                f"Approval workflow mismatch: record is for '{record.workflow_id}', expected '{expected_workflow_id}'."
            )

        if (
            record.workflow_definition_id != expected_definition.definition_id
            or record.workflow_definition_version != expected_definition.version
        ):
            raise ApprovalMismatchError(
                "Approval workflow definition identity or version mismatch."
            )

        # 3. Check action ID binding
        if record.action_id != expected_action_id:
            raise ApprovalMismatchError(
                f"Approval action ID mismatch: record is for '{record.action_id}', expected '{expected_action_id}'."
            )

        # 4. Check capability name binding
        if record.capability_name != capability.name:
            raise ApprovalMismatchError(
                f"Approval capability mismatch: record is for '{record.capability_name}', target is '{capability.name}'."
            )

        # 5. Check canonical arguments binding
        expected_canonical_args = canonicalize_arguments(arguments)
        if record.canonical_arguments_json != expected_canonical_args:
            raise ApprovalMismatchError(
                f"Approval arguments mismatch for capability '{capability.name}'. "
                f"Approved: {record.canonical_arguments_json}, Attempted: {expected_canonical_args}."
            )

        # 6. Check initiating actor binding
        if record.actor_id != actor.actor_id or record.actor_role != actor.role:
            raise ApprovalVerificationError(
                f"Approval actor mismatch: record for actor '{record.actor_id}' (role: {record.actor_role}), "
                f"invoked by '{actor.actor_id}' (role: {actor.role})."
            )

        # 7. Check expiration
        if record.expires_at is not None and time.time() >= record.expires_at:
            raise ApprovalVerificationError(
                f"Approval '{record.approval_id}' expired at timestamp {record.expires_at}."
            )


class WorkflowApprovalAdapter(ApprovalPort):
    """Adapter exposing durable SQLite approvals through the frozen Phase 6 ApprovalPort."""

    def __init__(
        self,
        approval_record: DurableApprovalRecord,
        expected_workflow_id: str,
        expected_action_id: str,
        expected_definition: WorkflowDefinitionRef,
    ) -> None:
        self.approval_record = approval_record
        self.expected_workflow_id = expected_workflow_id
        self.expected_action_id = expected_action_id
        self.expected_definition = expected_definition

    async def request_approval(
        self,
        actor: AgentActor,
        capability: CapabilityMetadata,
        arguments: Mapping[str, Any],
    ) -> ApprovalDecision:
        """Evaluate durable approval record against the incoming execution proposal."""
        try:
            WorkflowApprovalVerifier.verify(
                record=self.approval_record,
                expected_workflow_id=self.expected_workflow_id,
                expected_action_id=self.expected_action_id,
                capability=capability,
                arguments=arguments,
                actor=actor,
                expected_definition=self.expected_definition,
            )
            return ApprovalDecision(
                approved=True,
                approver=self.approval_record.approver_id or "workflow_durable_approver",
                reason=self.approval_record.reason or "Durable workflow approval verified.",
            )
        except Exception as exc:  # noqa: BLE001 -- finalize failures from external execution boundaries.
            return ApprovalDecision(
                approved=False,
                approver="workflow_approval_verifier",
                reason=f"Approval verification rejected: {exc!s}",
            )
