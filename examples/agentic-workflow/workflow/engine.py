"""Deterministic workflow execution engine with checkpointing and CAS resumption."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Mapping
from dataclasses import replace
from typing import Any, cast

from contracts.workflow import (
    ApprovalStatus,
    StepExecutionStatus,
    WorkflowStatus,
)

from .audit import WorkflowAuditRecorder
from .definition import WorkflowDefinition
from .errors import (
    ConcurrentResumeConflictError,
    IncompatibleWorkflowDefinitionError,
    InvalidTransitionError,
    MaxTransitionsExceededError,
)
from .retry import StepRetryPolicy
from .state import StepExecutionRecord, StepOutcome, WorkflowActorSnapshot, WorkflowState
from .store import execution_guard


class WorkflowEngine:
    """Core orchestrator executing code-defined workflows with durability and HITL."""

    def __init__(
        self,
        definition: WorkflowDefinition,
        store: Any,
        retry_policy: StepRetryPolicy | None = None,
        audit_recorder: WorkflowAuditRecorder | None = None,
    ) -> None:
        self.definition = definition
        self.store = store
        self.retry_policy = retry_policy or StepRetryPolicy()
        self.audit_recorder = audit_recorder or WorkflowAuditRecorder()

    def _record_audit(
        self,
        workflow_id: str,
        event_type: str,
        step_name: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> Any:
        event = self.audit_recorder.record(
            workflow_id=workflow_id,
            event_type=event_type,
            step_name=step_name,
            details=details,
        )
        if hasattr(self.store, "append_audit_event"):
            try:
                self.store.append_audit_event(event)
            except Exception:  # noqa: BLE001 -- audit persistence remains best effort.
                return event
        return event

    async def _start_workflow(
        self,
        workflow_id: str,
        actor: WorkflowActorSnapshot,
        domain_context: Mapping[str, Any] | None = None,
    ) -> WorkflowState:
        """Initialize and run a new durable workflow instance."""
        existing = self.store.load_state(workflow_id)
        if existing:
            raise ValueError(f"Workflow with ID '{workflow_id}' already exists.")

        initial_state = WorkflowState(
            workflow_id=workflow_id,
            definition=self.definition.definition_ref,
            status=WorkflowStatus.RUNNING,
            current_step=self.definition.start_step,
            initiator_actor=actor,
            history=(),
            domain_context=dict(domain_context or {}),
            checkpoint_version=1,
            created_at=time.time(),
            updated_at=time.time(),
        )

        self.store.save_state(initial_state)
        self._record_audit(
            workflow_id=workflow_id,
            event_type="workflow_started",
            step_name=self.definition.start_step,
            details={
                "definition": self.definition.definition_id,
                "version": self.definition.version,
            },
        )

        return await self._run_loop(initial_state)

    async def start_workflow(
        self,
        workflow_id: str,
        actor: WorkflowActorSnapshot,
        domain_context: Mapping[str, Any] | None = None,
    ) -> WorkflowState:
        with execution_guard(self.store, workflow_id):
            return await self._start_workflow(workflow_id, actor, domain_context)

    def _check_definition(self, state: WorkflowState) -> None:
        if state.definition != self.definition.definition_ref:
            raise IncompatibleWorkflowDefinitionError(
                f"Workflow '{state.workflow_id}' was persisted using definition "
                f"'{state.definition.definition_id}' v{state.definition.version}, "
                f"but runtime engine supports '{self.definition.definition_id}' v{self.definition.version}."
            )

    async def resume_workflow(
        self,
        workflow_id: str,
        decision: str,
        approver_id: str,
        approver_role: str = "manager",
        reason: str | None = None,
    ) -> WorkflowState:
        with execution_guard(self.store, workflow_id):
            state = self.store.load_state(workflow_id)
            if state is None:
                raise ValueError(f"Workflow '{workflow_id}' not found in persistence store.")
            self._check_definition(state)
            if state.status != WorkflowStatus.AWAITING_APPROVAL:
                raise ConcurrentResumeConflictError(
                    f"Workflow '{workflow_id}' cannot be resumed from status '{state.status.value}'."
                )
            if not isinstance(decision, str) or decision.strip().lower() not in (
                "approve",
                "approved",
                "yes",
                "y",
                "reject",
                "rejected",
                "no",
                "n",
            ):
                raise ValueError("Unrecognized approval decision.")
            approved = decision.strip().lower() in ("approve", "approved", "yes", "y")
            approval = self.store.load_approval(state.domain_context.get("pending_approval_id"))
            if approval is None or approval.status != ApprovalStatus.PENDING:
                raise ConcurrentResumeConflictError("No pending approval is available.")
            decided = replace(
                approval,
                status=ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED,
                approver_id=approver_id,
                approver_role=approver_role,
                reason=reason,
                decided_at=time.time(),
            )
            outcome = StepOutcome(
                status=StepExecutionStatus.SUCCEEDED,
                outcome_type="approval_granted" if approved else "approval_rejected",
                context_updates={
                    "approval_decision": "approved" if approved else "rejected",
                    "approver_id": approver_id,
                },
            )
            running = replace(state, status=WorkflowStatus.RUNNING)
            next_step, terminal = self.definition.resolve_next_step(running, outcome)
            updated = self._apply_transition(running, next_step, terminal, outcome)
            self.store.commit_approval_transition(state, updated, decided)
            self._record_audit(
                workflow_id,
                "approval_decided",
                details={
                    "decision": "approved" if approved else "rejected",
                    "approver": approver_id,
                },
            )
            return await self._run_loop(updated)

    async def recover_workflow(self, workflow_id: str) -> WorkflowState:
        """Re-enter an abandoned RUNNING checkpoint under exclusive local ownership."""
        with execution_guard(self.store, workflow_id):
            state = self.store.load_state(workflow_id)
            if state is None:
                raise ValueError(f"Workflow '{workflow_id}' not found in persistence store.")
            self._check_definition(state)
            if state.status != WorkflowStatus.RUNNING:
                raise ConcurrentResumeConflictError(
                    "Recovery requires an abandoned RUNNING checkpoint."
                )
            step = self.definition.steps.get(state.current_step)
            if step is None or (step.is_mutating and not step.supports_recovery):
                raise InvalidTransitionError("Step has no safe mutation recovery contract.")
            return await self._run_loop(state)

    async def _execute_step_handler(self, step_def: Any, state: WorkflowState) -> StepOutcome:
        handler = step_def.handler
        if hasattr(handler, "execute"):
            fn = handler.execute
        else:
            fn = handler

        if inspect.iscoroutinefunction(fn):
            return cast(StepOutcome, await fn(state))
        res = fn(state)
        if inspect.iscoroutine(res):
            return cast(StepOutcome, await res)
        return cast(StepOutcome, res)

    async def _run_loop(self, state: WorkflowState) -> WorkflowState:
        """Internal execution loop advancing steps according to declared transitions."""
        transitions_count = 0

        while not state.status.is_terminal and state.status != WorkflowStatus.AWAITING_APPROVAL:
            step_def = self.definition.steps.get(state.current_step)
            if not step_def:
                raise InvalidTransitionError(
                    f"Current step '{state.current_step}' not declared in definition."
                )

            # Terminal Step Handling
            if step_def.is_terminal:
                state = WorkflowState(
                    workflow_id=state.workflow_id,
                    definition=state.definition,
                    status=step_def.terminal_status or WorkflowStatus.COMPLETED,
                    current_step=state.current_step,
                    initiator_actor=state.initiator_actor,
                    history=state.history,
                    pending_approval_id=state.pending_approval_id,
                    step_retries=state.step_retries,
                    domain_context=state.domain_context,
                    checkpoint_version=state.checkpoint_version + 1,
                    checksum="",
                    created_at=state.created_at,
                    updated_at=time.time(),
                )
                self.store.save_state(state)
                self._record_audit(
                    workflow_id=state.workflow_id,
                    event_type="workflow_terminal",
                    step_name=state.current_step,
                    details={"status": state.status.value},
                )
                break

            # Transition Ceiling Invariant
            transitions_count += 1
            if transitions_count > self.definition.max_transitions:
                state = WorkflowState(
                    workflow_id=state.workflow_id,
                    definition=state.definition,
                    status=WorkflowStatus.FAILED,
                    current_step=state.current_step,
                    initiator_actor=state.initiator_actor,
                    history=state.history,
                    pending_approval_id=state.pending_approval_id,
                    step_retries=state.step_retries,
                    domain_context=dict(state.domain_context),
                    checkpoint_version=state.checkpoint_version + 1,
                    checksum="",
                    created_at=state.created_at,
                    updated_at=time.time(),
                )
                self.store.save_state(state)
                self._record_audit(
                    workflow_id=state.workflow_id,
                    event_type="max_transitions_exceeded",
                    step_name=state.current_step,
                )
                raise MaxTransitionsExceededError(
                    f"Workflow '{state.workflow_id}' exceeded transition ceiling of {self.definition.max_transitions}."
                )

            # Step Execution with Retry Policy
            attempt = 0
            outcome: StepOutcome | None = None

            while True:
                attempt += 1
                self._record_audit(
                    workflow_id=state.workflow_id,
                    event_type="step_started",
                    step_name=step_def.name,
                    details={"attempt": attempt},
                )

                try:
                    outcome = await self._execute_step_handler(step_def, state)
                    break
                except Exception as exc:  # noqa: BLE001 -- step failures become structured outcomes.
                    if self.retry_policy.is_retryable(
                        exc, attempt, is_state_mutating=step_def.is_mutating
                    ):
                        delay = self.retry_policy.compute_delay_seconds(attempt)
                        self._record_audit(
                            workflow_id=state.workflow_id,
                            event_type="step_retry",
                            step_name=step_def.name,
                            details={"attempt": attempt, "error": str(exc), "delay_seconds": delay},
                        )
                        await asyncio.sleep(delay)
                        continue
                    else:
                        outcome = StepOutcome(
                            status=StepExecutionStatus.FAILED,
                            outcome_type="step_exception",
                            error_message=str(exc),
                        )
                        break

            assert outcome is not None

            # Handle Suspension (e.g. Human Approval Checkpoint)
            if outcome.status == StepExecutionStatus.SUSPENDED:
                updated_context = dict(state.domain_context)
                updated_context.update(outcome.context_updates)

                state = WorkflowState(
                    workflow_id=state.workflow_id,
                    definition=state.definition,
                    status=WorkflowStatus.AWAITING_APPROVAL,
                    current_step=step_def.name,
                    initiator_actor=state.initiator_actor,
                    history=state.history,
                    pending_approval_id=updated_context.get("pending_approval_id"),
                    step_retries=state.step_retries,
                    domain_context=updated_context,
                    checkpoint_version=state.checkpoint_version + 1,
                    checksum="",
                    created_at=state.created_at,
                    updated_at=time.time(),
                )
                self.store.save_state(state)
                self._record_audit(
                    workflow_id=state.workflow_id,
                    event_type="workflow_suspended",
                    step_name=step_def.name,
                    details={"pending_approval_id": state.pending_approval_id},
                )
                break

            # Resolve next transition
            try:
                next_step, terminal_status = self.definition.resolve_next_step(state, outcome)
            except InvalidTransitionError:
                # If step failed and no recovery transition, terminate to FAILED
                state = WorkflowState(
                    workflow_id=state.workflow_id,
                    definition=state.definition,
                    status=WorkflowStatus.FAILED,
                    current_step=step_def.name,
                    initiator_actor=state.initiator_actor,
                    history=state.history,
                    pending_approval_id=state.pending_approval_id,
                    step_retries=state.step_retries,
                    domain_context=dict(state.domain_context),
                    checkpoint_version=state.checkpoint_version + 1,
                    checksum="",
                    created_at=state.created_at,
                    updated_at=time.time(),
                )
                self.store.save_state(state)
                break

            # Apply transition
            state = self._apply_transition(state, next_step, terminal_status, outcome)
            self.store.save_state(state)

        return state

    def _apply_transition(
        self,
        state: WorkflowState,
        next_step: str,
        terminal_status: WorkflowStatus | None,
        outcome: StepOutcome,
    ) -> WorkflowState:
        now = time.time()
        receipt_ids = tuple(r.action_id for r in outcome.receipts)

        exec_record = StepExecutionRecord(
            step_name=state.current_step,
            attempt=1,
            status=outcome.status,
            started_at=now,
            completed_at=now,
            outcome_type=outcome.outcome_type,
            execution_receipt_ids=receipt_ids,
            error_message=outcome.error_message,
        )

        new_history = list(state.history)
        new_history.append(exec_record)

        updated_context = dict(state.domain_context)
        updated_context.update(outcome.context_updates)

        new_status = terminal_status if terminal_status is not None else state.status

        self._record_audit(
            workflow_id=state.workflow_id,
            event_type="transition_selected",
            step_name=state.current_step,
            details={
                "to_step": next_step,
                "outcome_type": outcome.outcome_type,
                "status": new_status.value,
            },
        )

        return WorkflowState(
            workflow_id=state.workflow_id,
            definition=state.definition,
            status=new_status,
            current_step=next_step,
            initiator_actor=state.initiator_actor,
            history=tuple(new_history),
            pending_approval_id=state.pending_approval_id,
            step_retries=state.step_retries,
            domain_context=updated_context,
            checkpoint_version=state.checkpoint_version + 1,
            checksum="",
            created_at=state.created_at,
            updated_at=now,
        )
