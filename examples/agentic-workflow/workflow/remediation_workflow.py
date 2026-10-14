"""Reference Customer Account Remediation workflow definition builder."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

from contracts.agent import AgentActor, ExecutionReceipt
from contracts.workflow import MutationStatus, StepExecutionStatus, WorkflowStatus

from .approval import WorkflowApprovalVerifier, canonicalize_arguments, resolve_current_actor
from .definition import StepDefinition, TransitionRule, WorkflowDefinition
from .ledger import DurableMutationRecord, generate_stable_action_id
from .state import RemediationType, StepOutcome, WorkflowState
from .steps import (
    AgenticInvestigationStep,
    ContextGatheringStep,
    FinalizationStep,
    HumanApprovalStep,
    IntakeValidationStep,
    MutationExecutionStep,
)


def build_customer_remediation_workflow(
    customer_store: Any,
    agent_engine: Any,
    read_only_registry: Any,
    credit_capability: Any,
    note_capability: Any,
    tool_executor: Any,
    workflow_store: Any,
    auth_policy: Any,
    definition_version: str = "1.1.0",
    max_transitions: int = 15,
    actor_resolver: Callable[[str], AgentActor | None] | None = None,
) -> WorkflowDefinition:
    """Construct the canonical Customer Account Remediation workflow graph."""

    # 1. Domain Reconcilers for In-Flight Recovery
    def reconcile_credit_mutation(
        action_id: str, capability_name: str, args: Mapping[str, Any]
    ) -> str:
        """Reconcile apply_fee_credit against customer account balance and activity log."""
        return str(customer_store.operation_status(action_id, capability_name, dict(args)))

    def reconcile_note_mutation(
        action_id: str, capability_name: str, args: Mapping[str, Any]
    ) -> str:
        """Reconcile the exact recorded note operation, never matching text alone."""
        return str(customer_store.operation_status(action_id, capability_name, dict(args)))

    # 2. Instantiate step handlers
    intake_step = IntakeValidationStep(customer_store=customer_store)
    context_step = ContextGatheringStep(customer_store=customer_store)
    agentic_step = AgenticInvestigationStep(
        agent_engine=agent_engine,
        read_only_registry=read_only_registry,
    )
    human_step = HumanApprovalStep(store=workflow_store)
    mutation_credit_step = MutationExecutionStep(
        capability=credit_capability,
        tool_executor=tool_executor,
        store=workflow_store,
        policy=auth_policy,
        reconciler=reconcile_credit_mutation,
        actor_resolver=actor_resolver,
    )
    mutation_note_step = MutationExecutionStep(
        capability=note_capability,
        tool_executor=tool_executor,
        store=workflow_store,
        policy=auth_policy,
        requires_approval=False,
        reconciler=reconcile_note_mutation,
        actor_resolver=actor_resolver,
    )
    finalize_step = FinalizationStep()

    concession_note_step = MutationExecutionStep(
        capability=note_capability,
        tool_executor=tool_executor,
        store=workflow_store,
        policy=auth_policy,
        requires_approval=False,
        reconciler=reconcile_note_mutation,
        actor_resolver=actor_resolver,
        step_name="concession_note",
    )

    async def record_concession_note(state: WorkflowState) -> StepOutcome:
        """Record the approved concession intent before attempting its credit."""
        actor = resolve_current_actor(state.initiator_actor.actor_id, actor_resolver)
        credit_args = dict(state.domain_context["target_arguments"])
        if not auth_policy.authorize(actor, credit_capability.metadata, credit_args).allowed:
            return StepOutcome(
                status=StepExecutionStatus.FAILED, outcome_type="authorization_denied"
            )
        approval = workflow_store.load_approval(state.domain_context["pending_approval_id"])
        WorkflowApprovalVerifier.verify(
            approval,
            state.workflow_id,
            state.domain_context["stable_action_id"],
            credit_capability.metadata,
            credit_args,
            actor,
            state.definition,
        )
        note_args = {
            "customer_id": state.domain_context["customer_id"],
            "note": f"Concession pending for workflow {state.workflow_id}: {state.domain_context['remediation_proposal']['summary']}",
        }
        action_id = generate_stable_action_id(
            state.workflow_id,
            "concession_note",
            note_capability.metadata.name,
            canonicalize_arguments(note_args),
        )
        note_state = replace(
            state,
            domain_context={
                **state.domain_context,
                "target_arguments": note_args,
                "stable_action_id": action_id,
            },
        )
        outcome = await concession_note_step.execute(note_state)
        return replace(
            outcome,
            context_updates={"prior_note_action_id": action_id}
            if outcome.status == StepExecutionStatus.SUCCEEDED
            else {},
        )

    def has_recorded_prior_note(state: WorkflowState) -> bool:
        action_id = state.domain_context.get("prior_note_action_id")
        record = workflow_store.load_mutation(action_id) if action_id else None
        return bool(
            record
            and record.workflow_id == state.workflow_id
            and record.capability_name == note_capability.metadata.name
            and record.step_name == "concession_note"
            and record.status == MutationStatus.EXECUTED
        )

    async def compensate_step_handler(state: WorkflowState) -> StepOutcome:
        """Controlled local saga backward compensation with authorization, ledger tracking, and failure safety."""
        if not has_recorded_prior_note(state):
            return StepOutcome(
                status=StepExecutionStatus.FAILED,
                outcome_type="compensation_ineligible",
                error_message="No completed preceding concession note operation.",
            )
        customer_id = state.domain_context.get("customer_id", "")
        comp_args = {
            "customer_id": customer_id,
            "note": f"COMPENSATION: Fee credit disbursement failed for workflow {state.workflow_id}. Concession note voided.",
        }
        canonical_comp_args = canonicalize_arguments(comp_args)
        action_id = generate_stable_action_id(
            workflow_id=state.workflow_id,
            step_name="compensate_mutation",
            capability_name=note_capability.metadata.name,
            canonical_args=canonical_comp_args,
        )

        # 1. Check ledger for prior execution & ambiguous in-flight recovery
        existing = workflow_store.load_mutation(action_id)
        if existing:
            if existing.status == MutationStatus.EXECUTED:
                receipt = existing.execution_receipt or ExecutionReceipt(
                    action_id=action_id,
                    capability_name=note_capability.metadata.name,
                    status="succeeded",
                    result_summary="Compensation replayed from durable mutation ledger (duplicate suppressed).",
                )
                return StepOutcome(
                    status=StepExecutionStatus.SUCCEEDED,
                    outcome_type="compensation_completed",
                    context_updates={
                        "compensation_applied": True,
                        "duplicate_suppressed": True,
                        "compensating_receipt_id": receipt.action_id,
                    },
                    receipts=(receipt,),
                )
            elif existing.status == MutationStatus.EXECUTION_STARTED:
                # Ambiguous in-flight compensation recovery after crash
                # Invariant: Never blindly re-execute! Reconcile domain state before any tool invocation.
                reconciliation_result = "inconclusive"
                try:
                    res = reconcile_note_mutation(
                        action_id, note_capability.metadata.name, comp_args
                    )
                    reconciliation_result = str(res)
                except Exception:  # noqa: BLE001 -- inconclusive reconciliation must fail closed.
                    reconciliation_result = "inconclusive"

                if reconciliation_result == "executed":
                    now = time.time()
                    receipt = ExecutionReceipt(
                        action_id=action_id,
                        capability_name=note_capability.metadata.name,
                        status="succeeded",
                        result_summary="Compensation reconciled from domain state: side effect physically occurred prior to crash.",
                    )
                    reconciled_record = DurableMutationRecord(
                        action_id=action_id,
                        workflow_id=state.workflow_id,
                        step_name="compensate_mutation",
                        capability_name=note_capability.metadata.name,
                        canonical_args=canonical_comp_args,
                        status=MutationStatus.EXECUTED,
                        execution_receipt=receipt,
                        created_at=existing.created_at,
                        updated_at=now,
                    )
                    workflow_store.save_mutation(reconciled_record)
                    return StepOutcome(
                        status=StepExecutionStatus.SUCCEEDED,
                        outcome_type="compensation_completed",
                        context_updates={
                            "compensation_applied": True,
                            "duplicate_suppressed": True,
                            "reconciled_after_restart": True,
                            "compensating_receipt_id": receipt.action_id,
                        },
                        receipts=(receipt,),
                    )
                elif reconciliation_result == "not_executed":
                    # Confirmed by domain state that physical side effect did not occur.
                    # Proceed with safe execution below under original action_id.
                    pass
                else:
                    now = time.time()
                    ambiguous_record = DurableMutationRecord(
                        action_id=action_id,
                        workflow_id=state.workflow_id,
                        step_name="compensate_mutation",
                        capability_name=note_capability.metadata.name,
                        canonical_args=canonical_comp_args,
                        status=MutationStatus.AMBIGUOUS,
                        created_at=existing.created_at,
                        updated_at=now,
                    )
                    workflow_store.save_mutation(ambiguous_record)
                    return StepOutcome(
                        status=StepExecutionStatus.FAILED,
                        outcome_type="ambiguous_compensation",
                        error_message=(
                            f"Ambiguous in-flight compensation detected for action '{action_id}'. "
                            "Domain reconciliation was inconclusive. Manual intervention required to prevent duplicate compensation side effects."
                        ),
                        context_updates={
                            "compensation_applied": False,
                            "compensation_failed": True,
                            "manual_intervention_required": True,
                        },
                    )
            elif existing.status == MutationStatus.AMBIGUOUS:
                return StepOutcome(
                    status=StepExecutionStatus.FAILED,
                    outcome_type="ambiguous_compensation",
                    error_message=f"Compensation action '{action_id}' is marked AMBIGUOUS in mutation ledger. Manual intervention required.",
                    context_updates={
                        "compensation_applied": False,
                        "compensation_failed": True,
                        "manual_intervention_required": True,
                    },
                )
            elif existing.status == MutationStatus.FAILED:
                return StepOutcome(
                    status=StepExecutionStatus.FAILED,
                    outcome_type="compensation_failed",
                    error_message=f"Compensation action '{action_id}' previously failed. Manual intervention required.",
                    context_updates={
                        "compensation_applied": False,
                        "compensation_failed": True,
                        "manual_intervention_required": True,
                    },
                )

        # 2. Re-validate authorization for compensation action
        try:
            actor = resolve_current_actor(state.initiator_actor.actor_id, actor_resolver)
        except Exception as exc:  # noqa: BLE001 -- finalize failures from external execution boundaries.
            return StepOutcome(
                status=StepExecutionStatus.FAILED,
                outcome_type="compensation_failed",
                error_message=f"Current compensation authorization unavailable: {exc}",
                context_updates={
                    "compensation_applied": False,
                    "compensation_failed": True,
                    "manual_intervention_required": True,
                },
            )
        auth_decision = auth_policy.authorize(actor, note_capability.metadata, comp_args)
        if not auth_decision.allowed:
            return StepOutcome(
                status=StepExecutionStatus.FAILED,
                outcome_type="compensation_failed",
                error_message=f"Compensation authorization revoked: {auth_decision.reason}",
                context_updates={
                    "compensation_applied": False,
                    "compensation_failed": True,
                    "manual_intervention_required": True,
                },
            )

        # 3. Record intent in durable mutation ledger: EXECUTION_STARTED
        now = time.time()
        in_flight = DurableMutationRecord(
            action_id=action_id,
            workflow_id=state.workflow_id,
            step_name="compensate_mutation",
            capability_name=note_capability.metadata.name,
            canonical_args=canonical_comp_args,
            status=MutationStatus.EXECUTION_STARTED,
            created_at=existing.created_at if existing else now,
            updated_at=now,
        )
        workflow_store.save_mutation(in_flight)

        # 4. Execute physical compensating action via tool_executor
        args_with_id = dict(comp_args)
        args_with_id["action_id"] = action_id
        try:
            receipt, _raw_output = await tool_executor.execute_tool(
                note_capability, args_with_id, action_id=action_id
            )
            if (
                receipt.status == "succeeded"
                and isinstance(_raw_output, dict)
                and _raw_output.get("success") is False
            ):
                receipt = replace(
                    receipt,
                    status="failed",
                    error_message=str(_raw_output.get("error", "Compensation rejected")),
                )
        except Exception as exc:  # noqa: BLE001 -- finalize failures from external execution boundaries.
            receipt = ExecutionReceipt(
                action_id=action_id,
                capability_name=note_capability.metadata.name,
                status="failed",
                executed_at=time.time(),
                error_message=str(exc),
            )

        now = time.time()
        # 5. Commit outcome to durable mutation ledger
        if receipt.status == "succeeded":
            executed_record = DurableMutationRecord(
                action_id=action_id,
                workflow_id=state.workflow_id,
                step_name="compensate_mutation",
                capability_name=note_capability.metadata.name,
                canonical_args=canonical_comp_args,
                status=MutationStatus.EXECUTED,
                execution_receipt=receipt,
                created_at=in_flight.created_at,
                updated_at=now,
            )
            workflow_store.save_mutation(executed_record)
            return StepOutcome(
                status=StepExecutionStatus.SUCCEEDED,
                outcome_type="compensation_completed",
                context_updates={
                    "compensation_applied": True,
                    "compensating_receipt_id": receipt.action_id,
                    "compensated_action_id": state.domain_context["prior_note_action_id"],
                },
                receipts=(receipt,),
            )
        else:
            failed_record = DurableMutationRecord(
                action_id=action_id,
                workflow_id=state.workflow_id,
                step_name="compensate_mutation",
                capability_name=note_capability.metadata.name,
                canonical_args=canonical_comp_args,
                status=MutationStatus.FAILED,
                execution_receipt=receipt,
                created_at=in_flight.created_at,
                updated_at=now,
            )
            workflow_store.save_mutation(failed_record)
            return StepOutcome(
                status=StepExecutionStatus.FAILED,
                outcome_type="compensation_failed",
                error_message=f"Compensation execution failed: {receipt.error_message}. Manual intervention required.",
                context_updates={
                    "compensation_applied": False,
                    "compensation_failed": True,
                    "manual_intervention_required": True,
                },
                receipts=(receipt,),
            )

    # 2. Define steps and declared transitions
    steps = [
        # Step: INTAKE_VALIDATION
        StepDefinition(
            name="intake_validation",
            handler=intake_step.execute,
            transitions=(
                TransitionRule(
                    target_step="context_gathering",
                    condition_fn=lambda s, o: o.status == StepExecutionStatus.SUCCEEDED,
                    description="Valid input advances to context gathering",
                ),
            ),
            default_transition="terminal_failed",
        ),
        # Step: CONTEXT_GATHERING
        StepDefinition(
            name="context_gathering",
            handler=context_step.execute,
            transitions=(
                TransitionRule(
                    target_step="agentic_investigation",
                    condition_fn=lambda s, o: o.status == StepExecutionStatus.SUCCEEDED,
                    description="Context loaded advances to agentic investigation",
                ),
            ),
            default_transition="terminal_failed",
        ),
        # Step: AGENTIC_INVESTIGATION
        StepDefinition(
            name="agentic_investigation",
            handler=agentic_step.execute,
            transitions=(
                TransitionRule(
                    target_step="finalization",
                    condition_fn=lambda s, o: o.outcome_type == RemediationType.INFORMATIONAL.value,
                    description="Informational outcome advances directly to finalization",
                ),
                TransitionRule(
                    target_step="terminal_escalated",
                    condition_fn=lambda s, o: (
                        o.outcome_type == RemediationType.SECURITY_ESCALATION.value
                    ),
                    description="Security hold or fraud detection escalates to compliance",
                ),
                TransitionRule(
                    target_step="human_approval",
                    condition_fn=lambda s, o: o.outcome_type == RemediationType.FEE_CREDIT.value,
                    description="Fee credit mutation routes to human approval checkpoint",
                ),
                TransitionRule(
                    target_step="mutate_note",
                    condition_fn=lambda s, o: o.outcome_type == RemediationType.CUSTOMER_NOTE.value,
                    description="Customer note routes to direct mutation step",
                ),
            ),
            default_transition="terminal_failed",
        ),
        # Step: HUMAN_APPROVAL (Suspension Point)
        StepDefinition(
            name="human_approval",
            handler=human_step.execute,
            transitions=(
                TransitionRule(
                    target_step="concession_note",
                    condition_fn=lambda s, o: o.outcome_type == "approval_granted",
                    description="Approved credit advances to execution",
                ),
                TransitionRule(
                    target_step="terminal_rejected",
                    condition_fn=lambda s, o: o.outcome_type == "approval_rejected",
                    description="Rejected credit routes to terminal rejected",
                ),
            ),
            default_transition="terminal_failed",
        ),
        # Step: MUTATION_EXECUTION (Fee Credit)
        StepDefinition(
            name="concession_note",
            handler=record_concession_note,
            transitions=(
                TransitionRule(
                    target_step="mutation_execution",
                    condition_fn=lambda s, o: o.status == StepExecutionStatus.SUCCEEDED,
                ),
            ),
            default_transition="terminal_failed",
            is_mutating=True,
            supports_recovery=True,
        ),
        StepDefinition(
            name="mutation_execution",
            handler=mutation_credit_step.execute,
            transitions=(
                TransitionRule(
                    target_step="finalization",
                    condition_fn=lambda s, o: o.status == StepExecutionStatus.SUCCEEDED,
                    description="Successful mutation advances to finalization",
                ),
                TransitionRule(
                    target_step="compensate_mutation",
                    condition_fn=lambda s, o: (
                        o.status == StepExecutionStatus.FAILED and has_recorded_prior_note(s)
                    ),
                    description="Multi-step failure triggers local saga backward compensation",
                ),
            ),
            default_transition="terminal_failed",
            is_mutating=True,
            supports_recovery=True,
        ),
        # Step: MUTATE_NOTE (Customer Note)
        StepDefinition(
            name="mutate_note",
            handler=mutation_note_step.execute,
            transitions=(
                TransitionRule(
                    target_step="finalization",
                    condition_fn=lambda s, o: o.status == StepExecutionStatus.SUCCEEDED,
                    description="Successful note mutation advances to finalization",
                ),
            ),
            default_transition="terminal_failed",
            is_mutating=True,
            supports_recovery=True,
        ),
        # Step: COMPENSATE_MUTATION (Local Saga Step)
        StepDefinition(
            name="compensate_mutation",
            handler=compensate_step_handler,
            transitions=(),
            default_transition="terminal_failed",
            is_mutating=True,
            supports_recovery=True,
        ),
        # Step: FINALIZATION
        StepDefinition(
            name="finalization",
            handler=finalize_step.execute,
            transitions=(),
            default_transition="terminal_completed",
        ),
        # Terminal Steps
        StepDefinition(
            name="terminal_completed",
            handler=lambda s: StepOutcome(
                status=StepExecutionStatus.SUCCEEDED, outcome_type="completed"
            ),
            is_terminal=True,
            terminal_status=WorkflowStatus.COMPLETED,
        ),
        StepDefinition(
            name="terminal_failed",
            handler=lambda s: StepOutcome(status=StepExecutionStatus.FAILED, outcome_type="failed"),
            is_terminal=True,
            terminal_status=WorkflowStatus.FAILED,
        ),
        StepDefinition(
            name="terminal_rejected",
            handler=lambda s: StepOutcome(
                status=StepExecutionStatus.SUCCEEDED, outcome_type="rejected"
            ),
            is_terminal=True,
            terminal_status=WorkflowStatus.REJECTED,
        ),
        StepDefinition(
            name="terminal_escalated",
            handler=lambda s: StepOutcome(
                status=StepExecutionStatus.SUCCEEDED, outcome_type="escalated"
            ),
            is_terminal=True,
            terminal_status=WorkflowStatus.ESCALATED,
        ),
    ]

    return WorkflowDefinition(
        definition_id="customer_account_remediation",
        version=definition_version,
        start_step="intake_validation",
        steps=steps,
        max_transitions=max_transitions,
    )
