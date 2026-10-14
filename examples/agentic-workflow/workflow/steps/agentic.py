"""Agentic investigation step integrating frozen Phase 6 AgentExecutionEngine."""

from __future__ import annotations

import json
import re
from typing import Any

from agent.schema import validate_value
from contracts.agent import AgentActor, SideEffectLevel
from contracts.workflow import StepExecutionStatus

from ..state import RemediationProposal, RemediationType, StepOutcome, WorkflowState

PROPOSAL_JSON_PATTERN = re.compile(r"\{.*\}", re.DOTALL)

PROPOSAL_SCHEMA = {
    "type": "object",
    "properties": {
        "proposal_type": {"type": "string", "enum": [t.value for t in RemediationType]},
        "summary": {"type": "string", "minLength": 1},
        "suggested_arguments": {"type": "object"},
        "requires_approval": {"type": "boolean"},
        "requires_human_approval": {"type": "boolean"},
    },
    "required": ["proposal_type", "summary", "suggested_arguments"],
    "additionalProperties": False,
}
ARGUMENT_SCHEMAS = {
    RemediationType.FEE_CREDIT: {
        "type": "object",
        "properties": {
            "amount_cents": {"type": "integer", "minimum": 1},
            "reason": {"type": "string", "minLength": 1},
        },
        "required": ["amount_cents", "reason"],
        "additionalProperties": False,
    },
    RemediationType.CUSTOMER_NOTE: {
        "type": "object",
        "properties": {"note": {"type": "string", "minLength": 1}},
        "required": ["note"],
        "additionalProperties": False,
    },
    RemediationType.INFORMATIONAL: {"type": "object", "additionalProperties": False},
    RemediationType.SECURITY_ESCALATION: {
        "type": "object",
        "properties": {"reason": {"type": "string", "minLength": 1}},
        "additionalProperties": False,
    },
}


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate proposal field: {key}")
        result[key] = value
    return result


class AgenticInvestigationStep:
    """Delegates bounded diagnosis to Phase 6 Agent with strictly read-only capabilities."""

    def __init__(
        self,
        agent_engine: Any,
        read_only_registry: Any,
    ) -> None:
        self.agent_engine = agent_engine
        self.registry = read_only_registry
        self._verify_read_only_registry()

    def _verify_read_only_registry(self) -> None:
        """Enforce critical safety invariant: Zero mutating tools in investigation registry."""
        for item in self.registry.list_capabilities():
            if isinstance(item, str):
                cap = self.registry.get(item)
                meta = cap.metadata if cap else None
            else:
                meta = item
            if meta and meta.side_effect_level == SideEffectLevel.STATE_MUTATING:
                raise ValueError(
                    f"Security Invariant Violation: Mutating capability '{meta.name}' detected "
                    "in AgenticInvestigationStep registry. Investigation must be strictly read-only."
                )

    def _parse_proposal_from_answer(self, raw_text: str) -> RemediationProposal:
        """Validate one JSON proposal; prose never determines a business action."""
        if not isinstance(raw_text, str):
            raise TypeError("Agent final answer must be a string.")
        match = PROPOSAL_JSON_PATTERN.search(raw_text)
        if not match:
            raise ValueError("Completed investigation must contain a JSON proposal.")
        data = json.loads(match.group(0), object_pairs_hook=_unique_json_object)
        errors = validate_value(PROPOSAL_SCHEMA, data)
        if errors:
            raise ValueError("Invalid proposal: " + "; ".join(errors))
        proposal_type = RemediationType(data["proposal_type"])
        arguments = data["suggested_arguments"]
        errors = validate_value(ARGUMENT_SCHEMAS[proposal_type], arguments)
        if errors:
            raise ValueError("Invalid proposal arguments: " + "; ".join(errors))
        if not data["summary"].strip() or any(
            isinstance(value, str) and not value.strip() for value in arguments.values()
        ):
            raise ValueError("Proposal text fields must not be blank.")
        if (
            "requires_approval" in data
            and "requires_human_approval" in data
            and data["requires_approval"] != data["requires_human_approval"]
        ):
            raise ValueError("Conflicting proposal approval flags.")
        return RemediationProposal(
            proposal_type=proposal_type,
            summary=data["summary"],
            suggested_arguments=arguments,
            requires_approval=proposal_type == RemediationType.FEE_CREDIT,
        )

    async def execute(self, state: WorkflowState) -> StepOutcome:
        customer_id = state.domain_context.get("customer_id", "")
        inquiry = state.domain_context.get("inquiry")
        if not isinstance(inquiry, str) or not inquiry.strip():
            return StepOutcome(
                status=StepExecutionStatus.FAILED,
                outcome_type="invalid_input",
                error_message="Required field 'inquiry' must be a non-empty string.",
            )

        goal = (
            f"Investigate customer inquiry for '{customer_id}': {inquiry}. "
            "Inspect customer status, account balance, and dispute policies. "
            "Determine the appropriate remediation proposal: INFORMATIONAL, CUSTOMER_NOTE, "
            "FEE_CREDIT, or SECURITY_ESCALATION. Return one JSON object with exactly "
            "proposal_type (lowercase), summary (non-empty string), and suggested_arguments (object). "
            "For fee_credit supply amount_cents (positive integer) and reason (non-empty string); "
            "for customer_note supply note (non-empty string); for informational supply {}; "
            "for security_escalation supply an optional reason. Do not include customer_id, "
            "workflow routes, or execution commands in the proposal. The application controls approval."
        )

        actor = AgentActor(
            actor_id=state.initiator_actor.actor_id,
            role=state.initiator_actor.role,
            permissions=list(state.initiator_actor.permissions),
        )

        # Run bounded Phase 6 agent
        result = await self.agent_engine.run(goal, actor=actor)

        # Invariant check: Ensure NO mutating capabilities were executed
        execution_receipts = getattr(result, "execution_receipts", ()) or ()
        for receipt in execution_receipts:
            cap = self.registry.get(receipt.capability_name)
            if cap and cap.metadata.side_effect_level == SideEffectLevel.STATE_MUTATING:
                raise RuntimeError(
                    f"Fatal Security Breach: Mutating capability '{receipt.capability_name}' was executed "
                    "during read-only agentic investigation."
                )

        result_status = getattr(result, "status", "")
        if result_status != "COMPLETED":
            return StepOutcome(
                status=StepExecutionStatus.FAILED,
                outcome_type="agent_failed" if result_status == "FAILED" else "agent_incomplete",
                error_message=f"Investigation did not complete: {result_status!r}.",
                receipts=tuple(execution_receipts),
            )

        try:
            proposal = self._parse_proposal_from_answer(getattr(result, "final_answer", ""))
        except (ValueError, TypeError, RecursionError) as exc:
            return StepOutcome(
                status=StepExecutionStatus.FAILED,
                outcome_type="invalid_proposal",
                error_message=str(exc),
                receipts=tuple(execution_receipts),
            )

        return StepOutcome(
            status=StepExecutionStatus.SUCCEEDED,
            outcome_type=proposal.proposal_type.value,
            proposal=proposal,
            context_updates={
                "agent_run_id": getattr(result, "run_id", "run-unknown"),
                "remediation_proposal": proposal.to_dict(),
            },
            receipts=tuple(execution_receipts),
        )
