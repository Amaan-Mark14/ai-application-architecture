"""Deterministic workflow steps for intake validation, context retrieval, and finalization."""

from __future__ import annotations

from typing import Any

from contracts.workflow import StepExecutionStatus

from ..state import StepOutcome, WorkflowState


class IntakeValidationStep:
    """Validates initial customer inquiry payload and verifies customer identity."""

    def __init__(self, customer_store: Any) -> None:
        self.customer_store = customer_store

    async def execute(self, state: WorkflowState) -> StepOutcome:
        inquiry = state.domain_context.get("inquiry")
        if not isinstance(inquiry, str) or not inquiry.strip():
            return StepOutcome(
                status=StepExecutionStatus.FAILED,
                outcome_type="invalid_input",
                error_message="Required field 'inquiry' must be a non-empty string.",
            )
        customer_id = state.domain_context.get("customer_id")
        if not customer_id or not str(customer_id).strip():
            return StepOutcome(
                status=StepExecutionStatus.FAILED,
                outcome_type="invalid_input",
                error_message="Missing required field 'customer_id' in workflow input.",
            )

        customer = self.customer_store.get_customer(str(customer_id))
        if not customer:
            return StepOutcome(
                status=StepExecutionStatus.FAILED,
                outcome_type="customer_not_found",
                error_message=f"Customer '{customer_id}' does not exist in domain store.",
            )

        return StepOutcome(
            status=StepExecutionStatus.SUCCEEDED,
            outcome_type="valid_input",
            context_updates={
                "customer_name": customer.name,
                "customer_email": customer.email,
                "customer_risk_level": customer.risk_level,
                "customer_status": customer.status,
            },
        )


class ContextGatheringStep:
    """Retrieves account balance, ledger history, and operational notes into workflow context."""

    def __init__(self, customer_store: Any) -> None:
        self.customer_store = customer_store

    async def execute(self, state: WorkflowState) -> StepOutcome:
        customer_id = str(state.domain_context["customer_id"])
        account = self.customer_store.get_account(customer_id)
        if not account:
            return StepOutcome(
                status=StepExecutionStatus.FAILED,
                outcome_type="account_not_found",
                error_message=f"Account for customer '{customer_id}' does not exist.",
            )

        return StepOutcome(
            status=StepExecutionStatus.SUCCEEDED,
            outcome_type="context_loaded",
            context_updates={
                "account_balance_cents": account.balance_cents,
                "account_balance_usd": f"${account.balance_cents / 100:.2f}",
                "account_notes": list(account.notes),
                "recent_activity": list(account.activity_log[-5:]),
            },
        )


class FinalizationStep:
    """Concludes workflow, emits audit summary, and transitions to terminal state."""

    async def execute(self, state: WorkflowState) -> StepOutcome:
        resolution_summary = state.domain_context.get(
            "resolution_summary", "Workflow completed successfully."
        )
        return StepOutcome(
            status=StepExecutionStatus.SUCCEEDED,
            outcome_type="finalized",
            context_updates={"final_resolution": resolution_summary},
        )
