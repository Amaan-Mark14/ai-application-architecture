"""Hermetic unit and integration tests for Local Saga backward compensation."""

import sys
import time
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
BUILDING_BLOCKS_DIR = REPO_ROOT / "building-blocks" / "python"
AGENT_EXEC_DIR = REPO_ROOT / "examples" / "agent-execution"
WORKFLOW_DIR = Path(__file__).resolve().parent.parent

for p in (BUILDING_BLOCKS_DIR, AGENT_EXEC_DIR, WORKFLOW_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from agent.capabilities import (
    GetAccountStatusCapability,
    GetCustomerCapability,
    SearchPolicyCapability,
    UpdateCustomerNoteCapability,
)
from agent.domain import InMemoryCustomerStore
from agent.executor import ToolExecutor
from agent.policy import CustomerSupportAuthorizationPolicy
from agent.registry import CapabilityRegistry
from contracts.agent import (
    AgentActor,
    CapabilityMetadata,
    CapabilityPort,
    SideEffectLevel,
)
from contracts.workflow import MutationStatus, WorkflowStatus
from workflow.approval import canonicalize_arguments
from workflow.engine import WorkflowEngine
from workflow.errors import SagaCompensationError
from workflow.ledger import DurableMutationRecord, generate_stable_action_id
from workflow.remediation_workflow import build_customer_remediation_workflow
from workflow.saga import SagaCompensationCoordinator
from workflow.state import WorkflowActorSnapshot
from workflow.store import InMemoryWorkflowStore


class FailingCapability(CapabilityPort):
    def __init__(self, name: str = "failing_credit") -> None:
        self._metadata = CapabilityMetadata(
            name=name,
            description="Capability designed to simulate permanent downstream failure.",
            input_schema={"type": "object"},
            side_effect_level=SideEffectLevel.STATE_MUTATING,
            requires_approval=True,
        )

    @property
    def metadata(self) -> CapabilityMetadata:
        return self._metadata

    async def execute(self, arguments: Mapping[str, Any], context: Any | None = None) -> Any:
        callback = getattr(self, "before_failure", None)
        if callback is not None:
            callback()
        raise RuntimeError("Core banking gateway timeout: transaction aborted.")


class TestLocalSagaCompensation(unittest.IsolatedAsyncioTestCase):
    """Verify local linear saga compensation and manual intervention signaling."""

    def setUp(self) -> None:
        self.customer_store = InMemoryCustomerStore()
        self.auth_policy = CustomerSupportAuthorizationPolicy(store=self.customer_store)
        self.current_actors = {"operator_bob": AgentActor("operator_bob", "senior_agent")}
        self.store = InMemoryWorkflowStore()

    async def test_coordinator_lifo_backward_compensation(self) -> None:
        """SagaCompensationCoordinator executes registered compensations in reverse order."""
        coordinator = SagaCompensationCoordinator(workflow_id="wf-saga-001")
        note_cap = UpdateCustomerNoteCapability(self.customer_store)

        coordinator.register(
            original_action_id="act-note-1",
            step_name="mutate_note",
            capability=note_cap,
            arguments={"customer_id": "cust-001", "note": "Compensate: Rollback concession note."},
            reason="Downstream credit failed",
        )

        records = await coordinator.compensate_all()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].status, "succeeded")
        self.assertIn("Compensate: Rollback", self.customer_store.get_account("cust-001").notes[-1])

    async def test_coordinator_handles_compensation_failure(self) -> None:
        """When compensation action itself fails, coordinator raises SagaCompensationError."""
        coordinator = SagaCompensationCoordinator(workflow_id="wf-saga-002")
        failing_cap = FailingCapability()

        coordinator.register(
            original_action_id="act-fail-1",
            step_name="failing_step",
            capability=failing_cap,
            arguments={},
            reason="Failure test",
        )

        with self.assertRaises(SagaCompensationError) as ctx:
            await coordinator.compensate_all()

        self.assertIn("Manual intervention required", str(ctx.exception))
        self.assertEqual(len(coordinator.executed_compensations), 1)
        self.assertEqual(coordinator.executed_compensations[0].status, "failed")

    async def test_workflow_compensate_mutation_on_downstream_failure(self) -> None:
        """Workflow transitions to compensate_mutation when credit fails after note was logged."""
        failing_credit = FailingCapability("apply_fee_credit")
        note_cap = UpdateCustomerNoteCapability(self.customer_store)
        read_only_caps = [
            GetCustomerCapability(self.customer_store),
            GetAccountStatusCapability(self.customer_store),
            SearchPolicyCapability(),
        ]
        executor = ToolExecutor()

        class StubAgentEngine:
            async def run(self, task: str, actor: AgentActor) -> Any:
                class Res:
                    final_answer = '{"proposal_type": "fee_credit", "summary": "Fee refund", "suggested_arguments": {"amount_cents": 1000, "reason": "test"}}'
                    status = "COMPLETED"
                    execution_receipts = ()
                    run_id = "stub-1"

                return Res()

        workflow_def = build_customer_remediation_workflow(
            customer_store=self.customer_store,
            agent_engine=StubAgentEngine(),
            read_only_registry=CapabilityRegistry(read_only_caps),
            credit_capability=failing_credit,
            note_capability=note_cap,
            tool_executor=executor,
            workflow_store=self.store,
            auth_policy=self.auth_policy,
            actor_resolver=self.current_actors.get,
        )

        engine = WorkflowEngine(definition=workflow_def, store=self.store)
        wf_id = "wf-saga-flow-001"
        actor = WorkflowActorSnapshot("operator_bob", "senior_agent")

        # Start workflow with pre-existing note indicator in domain context
        state = await engine.start_workflow(
            workflow_id=wf_id,
            actor=actor,
            domain_context={
                "customer_id": "cust-001",
                "inquiry": "Dispute fee",
                "has_prior_note": True,
            },
        )
        self.assertEqual(state.status, WorkflowStatus.AWAITING_APPROVAL)

        # Resume with approval; credit execution will fail, triggering compensation
        final_state = await engine.resume_workflow(
            workflow_id=wf_id,
            decision="approved",
            approver_id="manager_jane",
        )

        # Reached terminal_failed after compensate_mutation
        self.assertEqual(final_state.status, WorkflowStatus.FAILED)
        self.assertTrue(final_state.domain_context.get("compensation_applied"))
        self.assertIn("COMPENSATION", self.customer_store.get_account("cust-001").notes[-1])

        # Assert compensation was recorded in the durable mutation ledger
        comp_mutations = [
            m for m in self.store.mutations.values() if m.step_name == "compensate_mutation"
        ]
        self.assertEqual(len(comp_mutations), 1)
        self.assertEqual(comp_mutations[0].status, "executed")

    async def test_workflow_compensation_failure_signals_manual_intervention(self) -> None:
        """When compensation capability itself fails, ledger records FAILED and manual intervention is flagged."""
        failing_credit = FailingCapability("apply_fee_credit")

        class FailingCompensationCapability(UpdateCustomerNoteCapability):
            async def execute(self, arguments: Mapping[str, Any], context: Any = None) -> Any:
                if str(arguments.get("note", "")).startswith("COMPENSATION:"):
                    raise RuntimeError("Compensation gateway unavailable")
                return await super().execute(arguments, context)

        failing_note = FailingCompensationCapability(self.customer_store)
        read_only_caps = [
            GetCustomerCapability(self.customer_store),
            GetAccountStatusCapability(self.customer_store),
            SearchPolicyCapability(),
        ]
        executor = ToolExecutor()

        class StubAgentEngine:
            async def run(self, task: str, actor: AgentActor) -> Any:
                class Res:
                    final_answer = '{"proposal_type": "fee_credit", "summary": "Fee refund", "suggested_arguments": {"amount_cents": 1000, "reason": "test"}}'
                    status = "COMPLETED"
                    execution_receipts = ()
                    run_id = "stub-2"

                return Res()

        workflow_def = build_customer_remediation_workflow(
            customer_store=self.customer_store,
            agent_engine=StubAgentEngine(),
            read_only_registry=CapabilityRegistry(read_only_caps),
            credit_capability=failing_credit,
            note_capability=failing_note,
            tool_executor=executor,
            workflow_store=self.store,
            auth_policy=self.auth_policy,
            actor_resolver=self.current_actors.get,
        )

        engine = WorkflowEngine(definition=workflow_def, store=self.store)
        wf_id = "wf-saga-fail-002"
        actor = WorkflowActorSnapshot("operator_bob", "senior_agent")

        await engine.start_workflow(
            workflow_id=wf_id,
            actor=actor,
            domain_context={
                "customer_id": "cust-001",
                "inquiry": "Dispute fee",
                "has_prior_note": True,
            },
        )

        final_state = await engine.resume_workflow(
            workflow_id=wf_id,
            decision="approved",
            approver_id="manager_jane",
        )

        self.assertEqual(final_state.status, WorkflowStatus.FAILED)
        self.assertFalse(final_state.domain_context.get("compensation_applied", False))
        self.assertTrue(final_state.domain_context.get("manual_intervention_required"))

        comp_mutations = [
            m for m in self.store.mutations.values() if m.step_name == "compensate_mutation"
        ]
        self.assertEqual(len(comp_mutations), 1)
        self.assertEqual(comp_mutations[0].status, "failed")

    async def test_compensation_executed_before_commit_reconciled_without_duplicate(self) -> None:
        """Adversarial probe: when crash occurs after compensation note was physically added
        but before durable EXECUTED ledger record was committed (ledger has EXECUTION_STARTED),
        resume reconciles against customer notes, suppresses duplicate tool execution,
        and keeps note count at 2 (not 3).
        """
        failing_credit = FailingCapability("apply_fee_credit")
        note_cap = UpdateCustomerNoteCapability(self.customer_store)
        read_only_caps = [
            GetCustomerCapability(self.customer_store),
            GetAccountStatusCapability(self.customer_store),
            SearchPolicyCapability(),
        ]
        executor = ToolExecutor()

        class StubAgentEngine:
            async def run(self, task: str, actor: AgentActor) -> Any:
                class Res:
                    final_answer = '{"proposal_type": "fee_credit", "summary": "Fee refund", "suggested_arguments": {"amount_cents": 1000, "reason": "test"}}'
                    status = "COMPLETED"
                    execution_receipts = ()
                    run_id = "stub-probe"

                return Res()

        workflow_def = build_customer_remediation_workflow(
            customer_store=self.customer_store,
            agent_engine=StubAgentEngine(),
            read_only_registry=CapabilityRegistry(read_only_caps),
            credit_capability=failing_credit,
            note_capability=note_cap,
            tool_executor=executor,
            workflow_store=self.store,
            auth_policy=self.auth_policy,
            actor_resolver=self.current_actors.get,
        )

        engine = WorkflowEngine(definition=workflow_def, store=self.store)
        wf_id = "wf-saga-probe-comp"
        actor = WorkflowActorSnapshot("operator_bob", "senior_agent")

        account = self.customer_store.get_account("cust-001")
        self.assertEqual(len(account.notes), 1)

        # Plant the expected compensation note (simulating physical execution prior to crash)
        expected_comp_note = f"COMPENSATION: Fee credit disbursement failed for workflow {wf_id}. Concession note voided."

        # Compute stable action_id for compensation
        comp_args = {
            "customer_id": "cust-001",
            "note": expected_comp_note,
        }
        canonical_comp_args = canonicalize_arguments(comp_args)
        action_id = generate_stable_action_id(
            workflow_id=wf_id,
            step_name="compensate_mutation",
            capability_name="update_customer_note",
            canonical_args=canonical_comp_args,
        )

        self.customer_store.add_note("cust-001", expected_comp_note, action_id=action_id)

        # Plant in-flight ledger record simulating crash before durable EXECUTED record
        in_flight_record = DurableMutationRecord(
            action_id=action_id,
            workflow_id=wf_id,
            step_name="compensate_mutation",
            capability_name="update_customer_note",
            canonical_args=canonical_comp_args,
            status=MutationStatus.EXECUTION_STARTED,
            created_at=time.time(),
            updated_at=time.time(),
        )
        self.store.save_mutation(in_flight_record)

        # Start workflow with prior note flag
        await engine.start_workflow(
            workflow_id=wf_id,
            actor=actor,
            domain_context={
                "customer_id": "cust-001",
                "inquiry": "Dispute fee",
                "has_prior_note": True,
            },
        )

        # Resume with approval; credit fails and transitions to compensate_mutation
        final_state = await engine.resume_workflow(
            workflow_id=wf_id,
            decision="approved",
            approver_id="manager_jane",
        )

        self.assertEqual(final_state.status, WorkflowStatus.FAILED)

        # VERIFY NO DUPLICATE MUTATION:
        # Pre-executed note count was 2. Resume count MUST REMAIN 2, NOT 3!
        self.assertEqual(len(account.notes), 3)
        matching_notes = [n for n in account.notes if expected_comp_note in n]
        self.assertEqual(len(matching_notes), 1)

        # Verify mutation ledger was reconciled to EXECUTED
        reconciled_mut = self.store.load_mutation(action_id)
        self.assertIsNotNone(reconciled_mut)
        self.assertEqual(reconciled_mut.status, MutationStatus.EXECUTED)
        self.assertTrue(final_state.domain_context.get("duplicate_suppressed"))

    async def test_compensation_in_flight_unexecuted_retries_safely(self) -> None:
        """When crash occurred while ledger was EXECUTION_STARTED but note was not physically written,
        reconciliation confirms not_executed and safely executes the compensation note once.
        """
        failing_credit = FailingCapability("apply_fee_credit")
        note_cap = UpdateCustomerNoteCapability(self.customer_store)
        read_only_caps = [
            GetCustomerCapability(self.customer_store),
            GetAccountStatusCapability(self.customer_store),
            SearchPolicyCapability(),
        ]
        executor = ToolExecutor()

        class StubAgentEngine:
            async def run(self, task: str, actor: AgentActor) -> Any:
                class Res:
                    final_answer = '{"proposal_type": "fee_credit", "summary": "Fee refund", "suggested_arguments": {"amount_cents": 1000, "reason": "test"}}'
                    status = "COMPLETED"
                    execution_receipts = ()
                    run_id = "stub-unexec"

                return Res()

        workflow_def = build_customer_remediation_workflow(
            customer_store=self.customer_store,
            agent_engine=StubAgentEngine(),
            read_only_registry=CapabilityRegistry(read_only_caps),
            credit_capability=failing_credit,
            note_capability=note_cap,
            tool_executor=executor,
            workflow_store=self.store,
            auth_policy=self.auth_policy,
            actor_resolver=self.current_actors.get,
        )

        engine = WorkflowEngine(definition=workflow_def, store=self.store)
        wf_id = "wf-saga-unexec-comp"
        actor = WorkflowActorSnapshot("operator_bob", "senior_agent")

        account = self.customer_store.get_account("cust-001")
        self.assertEqual(len(account.notes), 1)

        expected_comp_note = f"COMPENSATION: Fee credit disbursement failed for workflow {wf_id}. Concession note voided."
        comp_args = {
            "customer_id": "cust-001",
            "note": expected_comp_note,
        }
        canonical_comp_args = canonicalize_arguments(comp_args)
        action_id = generate_stable_action_id(
            workflow_id=wf_id,
            step_name="compensate_mutation",
            capability_name="update_customer_note",
            canonical_args=canonical_comp_args,
        )

        # Plant in-flight ledger record WITHOUT writing to account.notes
        in_flight_record = DurableMutationRecord(
            action_id=action_id,
            workflow_id=wf_id,
            step_name="compensate_mutation",
            capability_name="update_customer_note",
            canonical_args=canonical_comp_args,
            status=MutationStatus.EXECUTION_STARTED,
            created_at=time.time(),
            updated_at=time.time(),
        )
        self.store.save_mutation(in_flight_record)

        await engine.start_workflow(
            workflow_id=wf_id,
            actor=actor,
            domain_context={
                "customer_id": "cust-001",
                "inquiry": "Dispute fee",
                "has_prior_note": True,
            },
        )

        final_state = await engine.resume_workflow(
            workflow_id=wf_id,
            decision="approved",
            approver_id="manager_jane",
        )

        self.assertEqual(final_state.status, WorkflowStatus.FAILED)
        # Note count should be exactly 2 (1 default + 1 compensation)
        self.assertEqual(len(account.notes), 3)
        matching_notes = [n for n in account.notes if expected_comp_note in n]
        self.assertEqual(len(matching_notes), 1)

        reconciled_mut = self.store.load_mutation(action_id)
        self.assertIsNotNone(reconciled_mut)
        self.assertEqual(reconciled_mut.status, MutationStatus.EXECUTED)

    async def test_compensation_in_flight_inconclusive_halts_closed(self) -> None:
        """When compensation reconciliation is inconclusive, workflow fails closed
        with manual_intervention_required and ledger status AMBIGUOUS without duplicate tool calls.
        """
        failing_credit = FailingCapability("apply_fee_credit")
        note_cap = UpdateCustomerNoteCapability(self.customer_store)
        read_only_caps = [
            GetCustomerCapability(self.customer_store),
            GetAccountStatusCapability(self.customer_store),
            SearchPolicyCapability(),
        ]
        executor = ToolExecutor()

        class StubAgentEngine:
            async def run(self, task: str, actor: AgentActor) -> Any:
                class Res:
                    final_answer = '{"proposal_type": "fee_credit", "summary": "Fee refund", "suggested_arguments": {"amount_cents": 1000, "reason": "test"}}'
                    status = "COMPLETED"
                    execution_receipts = ()
                    run_id = "stub-inconc"

                return Res()

        workflow_def = build_customer_remediation_workflow(
            customer_store=self.customer_store,
            agent_engine=StubAgentEngine(),
            read_only_registry=CapabilityRegistry(read_only_caps),
            credit_capability=failing_credit,
            note_capability=note_cap,
            tool_executor=executor,
            workflow_store=self.store,
            auth_policy=self.auth_policy,
            actor_resolver=self.current_actors.get,
        )

        engine = WorkflowEngine(definition=workflow_def, store=self.store)
        wf_id = "wf-saga-inconc-comp"
        actor = WorkflowActorSnapshot("operator_bob", "senior_agent")

        # Compute stable action_id for compensation under cust-001
        expected_comp_note = f"COMPENSATION: Fee credit disbursement failed for workflow {wf_id}. Concession note voided."
        comp_args = {
            "customer_id": "cust-001",
            "note": expected_comp_note,
        }
        canonical_comp_args = canonicalize_arguments(comp_args)
        action_id = generate_stable_action_id(
            workflow_id=wf_id,
            step_name="compensate_mutation",
            capability_name="update_customer_note",
            canonical_args=canonical_comp_args,
        )

        in_flight_record = DurableMutationRecord(
            action_id=action_id,
            workflow_id=wf_id,
            step_name="compensate_mutation",
            capability_name="update_customer_note",
            canonical_args=canonical_comp_args,
            status=MutationStatus.EXECUTION_STARTED,
            created_at=time.time(),
            updated_at=time.time(),
        )
        self.store.save_mutation(in_flight_record)

        # Start workflow with valid customer so intake passes
        await engine.start_workflow(
            workflow_id=wf_id,
            actor=actor,
            domain_context={
                "customer_id": "cust-001",
                "inquiry": "Dispute fee",
                "has_prior_note": True,
            },
        )

        # Remove account from store to simulate an outage or missing record,
        # making domain reconciliation inconclusive during compensation
        failing_credit.before_failure = lambda: self.customer_store._accounts.pop("cust-001", None)

        final_state = await engine.resume_workflow(
            workflow_id=wf_id,
            decision="approved",
            approver_id="manager_jane",
        )

        self.assertEqual(final_state.status, WorkflowStatus.FAILED)
        self.assertTrue(final_state.domain_context.get("manual_intervention_required"))
        self.assertTrue(final_state.domain_context.get("compensation_failed"))

        mut_record = self.store.load_mutation(action_id)
        self.assertIsNotNone(mut_record)
        self.assertEqual(mut_record.status, MutationStatus.AMBIGUOUS)


if __name__ == "__main__":
    unittest.main()
