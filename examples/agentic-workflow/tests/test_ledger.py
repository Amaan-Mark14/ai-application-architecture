"""Hermetic unit tests for durable mutation ledger, stable action ID, and replay suppression."""

import sys
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
BUILDING_BLOCKS_DIR = REPO_ROOT / "building-blocks" / "python"
AGENT_EXEC_DIR = REPO_ROOT / "examples" / "agent-execution"
WORKFLOW_DIR = Path(__file__).resolve().parent.parent

for p in (BUILDING_BLOCKS_DIR, AGENT_EXEC_DIR, WORKFLOW_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from agent.capabilities import ApplyFeeCreditCapability
from agent.domain import InMemoryCustomerStore
from agent.executor import ToolExecutor
from agent.policy import CustomerSupportAuthorizationPolicy
from contracts.agent import AgentActor, ExecutionReceipt
from contracts.workflow import (
    ApprovalStatus,
    MutationStatus,
    StepExecutionStatus,
    WorkflowDefinitionRef,
    WorkflowStatus,
)
from workflow.approval import DurableApprovalRecord, canonicalize_arguments
from workflow.ledger import DurableMutationRecord, generate_stable_action_id
from workflow.state import WorkflowActorSnapshot, WorkflowState
from workflow.steps.mutation import MutationExecutionStep
from workflow.store import InMemoryWorkflowStore


class TestDurableMutationLedger(unittest.TestCase):
    """Verify stable action ID generation, ledger serialization, and idempotency semantics."""

    def test_generate_stable_action_id_determinism(self) -> None:
        """Same workflow, step, capability, and canonical arguments yield identical action_id."""
        wf_id = "wf-1001"
        step = "mutation_execution"
        cap = "apply_fee_credit"
        args = canonicalize_arguments({"amount": 2500, "customer_id": "cust-42"})

        id_1 = generate_stable_action_id(wf_id, step, cap, args)
        id_2 = generate_stable_action_id(wf_id, step, cap, args)

        self.assertEqual(id_1, id_2)
        self.assertTrue(id_1.startswith(f"act-{wf_id}-{step}-"))

    def test_generate_stable_action_id_argument_sensitivity(self) -> None:
        """Changed arguments yield a different action_id."""
        wf_id = "wf-1001"
        step = "mutation_execution"
        cap = "apply_fee_credit"
        args_1 = canonicalize_arguments({"amount": 2500, "customer_id": "cust-42"})
        args_2 = canonicalize_arguments({"amount": 5000, "customer_id": "cust-42"})

        id_1 = generate_stable_action_id(wf_id, step, cap, args_1)
        id_2 = generate_stable_action_id(wf_id, step, cap, args_2)

        self.assertNotEqual(id_1, id_2)

    def test_generate_stable_action_id_workflow_isolation(self) -> None:
        """Different workflow instances yield distinct action IDs for identical arguments."""
        step = "mutation_execution"
        cap = "apply_fee_credit"
        args = canonicalize_arguments({"amount": 2500, "customer_id": "cust-42"})

        id_wf1 = generate_stable_action_id("wf-001", step, cap, args)
        id_wf2 = generate_stable_action_id("wf-002", step, cap, args)

        self.assertNotEqual(id_wf1, id_wf2)

    def test_durable_mutation_record_serialization_roundtrip(self) -> None:
        """Verify full round-trip serialization of DurableMutationRecord with ExecutionReceipt."""
        receipt = ExecutionReceipt(
            action_id="act-wf-1001-mutation_execution-abcdef1234567890",
            capability_name="apply_fee_credit",
            status="succeeded",
            executed_at=1700000000.0,
            result_summary="Credited $25.00 to account cust-42",
        )
        record = DurableMutationRecord(
            action_id=receipt.action_id,
            workflow_id="wf-1001",
            step_name="mutation_execution",
            capability_name="apply_fee_credit",
            canonical_args='{"amount":2500,"customer_id":"cust-42"}',
            status=MutationStatus.EXECUTED,
            execution_receipt=receipt,
            created_at=1700000000.0,
            updated_at=1700000005.0,
        )

        d = record.to_dict()
        restored = DurableMutationRecord.from_dict(d)

        self.assertEqual(restored.action_id, record.action_id)
        self.assertEqual(restored.workflow_id, record.workflow_id)
        self.assertEqual(restored.status, MutationStatus.EXECUTED)
        self.assertIsNotNone(restored.execution_receipt)
        assert restored.execution_receipt is not None
        self.assertEqual(restored.execution_receipt.status, "succeeded")
        self.assertEqual(restored.execution_receipt.result_summary, receipt.result_summary)


class TestAmbiguousMutationRecovery(unittest.IsolatedAsyncioTestCase):
    """Hermetic verification of in-flight EXECUTION_STARTED crash recovery and domain reconciliation."""

    def setUp(self) -> None:
        self.customer_store = InMemoryCustomerStore()
        self.auth_policy = CustomerSupportAuthorizationPolicy(store=self.customer_store)
        self.current_actors = {"operator_bob": AgentActor("operator_bob", "senior_agent")}
        self.workflow_store = InMemoryWorkflowStore()
        self.tool_executor = ToolExecutor()
        self.credit_cap = ApplyFeeCreditCapability(self.customer_store)

    async def test_in_flight_recovery_reconciled_as_executed_suppresses_duplicate(self) -> None:
        """Probe reproduction: 15000 -> 16000 with EXECUTION_STARTED must NOT become 17000 on recovery."""
        wf_id = "wf-ambig-001"
        action_id = generate_stable_action_id(
            workflow_id=wf_id,
            step_name="mutation_execution",
            capability_name="apply_fee_credit",
            canonical_args=canonicalize_arguments(
                {"amount_cents": 1000, "customer_id": "cust-001", "reason": "overdraft concession"}
            ),
        )

        # Baseline: cust-001 balance is 15000 cents
        self.assertEqual(self.customer_store.get_account("cust-001").balance_cents, 15000)

        # 1. Simulate physical execution already occurred (balance became 16000)
        self.customer_store.apply_credit("cust-001", 1000, "overdraft concession")
        self.assertEqual(self.customer_store.get_account("cust-001").balance_cents, 16000)

        # 2. Simulate process crash before committing EXECUTED: ledger has EXECUTION_STARTED
        in_flight = DurableMutationRecord(
            action_id=action_id,
            workflow_id=wf_id,
            step_name="mutation_execution",
            capability_name="apply_fee_credit",
            canonical_args=canonicalize_arguments(
                {"amount_cents": 1000, "customer_id": "cust-001", "reason": "overdraft concession"}
            ),
            status=MutationStatus.EXECUTION_STARTED,
            created_at=time.time() - 10,
            updated_at=time.time() - 10,
        )
        self.workflow_store.save_mutation(in_flight)

        # 3. Simulate pending approval record exists in APPROVED status
        approval = DurableApprovalRecord(
            approval_id="app-ambig-1",
            workflow_id=wf_id,
            workflow_definition_id="customer_account_remediation",
            workflow_definition_version="1.0.0",
            action_id=action_id,
            capability_name="apply_fee_credit",
            canonical_arguments_json=canonicalize_arguments(
                {"amount_cents": 1000, "customer_id": "cust-001", "reason": "overdraft concession"}
            ),
            actor_id="operator_bob",
            actor_role="senior_agent",
            status=ApprovalStatus.APPROVED,
            approver_id="manager_jane",
            approver_role="manager",
            reason="Approved concession",
            requested_at=time.time() - 20,
            decided_at=time.time() - 15,
        )
        self.workflow_store.save_approval(approval)

        # 4. Define domain reconciler
        def reconcile_credit(act_id: str, cap_name: str, args: dict) -> str:
            acc = self.customer_store.get_account("cust-001")
            expected_log = "Credit applied: +$10.00 (Reason: overdraft concession)"
            if expected_log in acc.activity_log:
                return "executed"
            return "not_executed"

        mutation_step = MutationExecutionStep(
            capability=self.credit_cap,
            tool_executor=self.tool_executor,
            store=self.workflow_store,
            policy=self.auth_policy,
            actor_resolver=self.current_actors.get,
            reconciler=reconcile_credit,
        )

        state = WorkflowState(
            workflow_id=wf_id,
            definition=WorkflowDefinitionRef("customer_account_remediation", "1.0.0"),
            status=WorkflowStatus.RUNNING,
            current_step="mutation_execution",
            initiator_actor=WorkflowActorSnapshot("operator_bob", "senior_agent"),
            domain_context={
                "customer_id": "cust-001",
                "target_arguments": {
                    "amount_cents": 1000,
                    "customer_id": "cust-001",
                    "reason": "overdraft concession",
                },
                "stable_action_id": action_id,
                "pending_approval_id": "app-ambig-1",
            },
        )

        # 5. Execute recovery: must reconcile, suppress duplicate, and NOT double-credit
        outcome = await mutation_step.execute(state)

        self.assertEqual(outcome.status, StepExecutionStatus.SUCCEEDED)
        self.assertTrue(outcome.context_updates.get("duplicate_suppressed"))
        self.assertTrue(outcome.context_updates.get("reconciled_after_restart"))

        # CRITICAL REPRODUCTION CHECK: Balance must remain 16000, NOT 17000!
        self.assertEqual(self.customer_store.get_account("cust-001").balance_cents, 16000)

        # Durable ledger must now be EXECUTED
        persisted_mutation = self.workflow_store.load_mutation(action_id)
        self.assertIsNotNone(persisted_mutation)
        self.assertEqual(persisted_mutation.status, MutationStatus.EXECUTED)

        # Approval must now be CONSUMED
        persisted_approval = self.workflow_store.load_approval("app-ambig-1")
        self.assertIsNotNone(persisted_approval)
        self.assertEqual(persisted_approval.status, ApprovalStatus.CONSUMED)

    async def test_in_flight_recovery_reconciled_as_not_executed_safely_resumes(self) -> None:
        """When domain state confirms tool was not executed, recovery safely performs first-time execution."""
        wf_id = "wf-ambig-002"
        action_id = generate_stable_action_id(
            workflow_id=wf_id,
            step_name="mutation_execution",
            capability_name="apply_fee_credit",
            canonical_args=canonicalize_arguments(
                {"amount_cents": 1000, "customer_id": "cust-001", "reason": "unapplied concession"}
            ),
        )

        # Initial balance: 15000 cents
        self.assertEqual(self.customer_store.get_account("cust-001").balance_cents, 15000)

        # Simulate crash before physical tool execution: ledger has EXECUTION_STARTED
        in_flight = DurableMutationRecord(
            action_id=action_id,
            workflow_id=wf_id,
            step_name="mutation_execution",
            capability_name="apply_fee_credit",
            canonical_args=canonicalize_arguments(
                {"amount_cents": 1000, "customer_id": "cust-001", "reason": "unapplied concession"}
            ),
            status=MutationStatus.EXECUTION_STARTED,
            created_at=time.time() - 10,
            updated_at=time.time() - 10,
        )
        self.workflow_store.save_mutation(in_flight)

        approval = DurableApprovalRecord(
            approval_id="app-ambig-2",
            workflow_id=wf_id,
            workflow_definition_id="customer_account_remediation",
            workflow_definition_version="1.0.0",
            action_id=action_id,
            capability_name="apply_fee_credit",
            canonical_arguments_json=canonicalize_arguments(
                {"amount_cents": 1000, "customer_id": "cust-001", "reason": "unapplied concession"}
            ),
            actor_id="operator_bob",
            actor_role="senior_agent",
            status=ApprovalStatus.APPROVED,
            approver_id="manager_jane",
            approver_role="manager",
            reason="Approved concession",
            requested_at=time.time() - 20,
            decided_at=time.time() - 15,
        )
        self.workflow_store.save_approval(approval)

        def reconcile_credit(act_id: str, cap_name: str, args: dict) -> str:
            return "not_executed"

        mutation_step = MutationExecutionStep(
            capability=self.credit_cap,
            tool_executor=self.tool_executor,
            store=self.workflow_store,
            policy=self.auth_policy,
            actor_resolver=self.current_actors.get,
            reconciler=reconcile_credit,
        )

        state = WorkflowState(
            workflow_id=wf_id,
            definition=WorkflowDefinitionRef("customer_account_remediation", "1.0.0"),
            status=WorkflowStatus.RUNNING,
            current_step="mutation_execution",
            initiator_actor=WorkflowActorSnapshot("operator_bob", "senior_agent"),
            domain_context={
                "customer_id": "cust-001",
                "target_arguments": {
                    "amount_cents": 1000,
                    "customer_id": "cust-001",
                    "reason": "unapplied concession",
                },
                "stable_action_id": action_id,
                "pending_approval_id": "app-ambig-2",
            },
        )

        outcome = await mutation_step.execute(state)

        self.assertEqual(outcome.status, StepExecutionStatus.SUCCEEDED)
        # Executed safely once
        self.assertEqual(self.customer_store.get_account("cust-001").balance_cents, 16000)
        self.assertEqual(
            self.workflow_store.load_mutation(action_id).status, MutationStatus.EXECUTED
        )
        self.assertEqual(
            self.workflow_store.load_approval("app-ambig-2").status, ApprovalStatus.CONSUMED
        )

    async def test_in_flight_recovery_inconclusive_halts_safely_without_mutation(self) -> None:
        """When domain state reconciliation is inconclusive, recovery marks AMBIGUOUS and halts safely."""
        wf_id = "wf-ambig-003"
        action_id = generate_stable_action_id(
            workflow_id=wf_id,
            step_name="mutation_execution",
            capability_name="apply_fee_credit",
            canonical_args=canonicalize_arguments(
                {"amount_cents": 1000, "customer_id": "cust-001", "reason": "inconclusive test"}
            ),
        )

        self.assertEqual(self.customer_store.get_account("cust-001").balance_cents, 15000)

        in_flight = DurableMutationRecord(
            action_id=action_id,
            workflow_id=wf_id,
            step_name="mutation_execution",
            capability_name="apply_fee_credit",
            canonical_args=canonicalize_arguments(
                {"amount_cents": 1000, "customer_id": "cust-001", "reason": "inconclusive test"}
            ),
            status=MutationStatus.EXECUTION_STARTED,
            created_at=time.time() - 10,
            updated_at=time.time() - 10,
        )
        self.workflow_store.save_mutation(in_flight)

        def reconcile_inconclusive(act_id: str, cap_name: str, args: dict) -> str:
            return "inconclusive"

        mutation_step = MutationExecutionStep(
            capability=self.credit_cap,
            tool_executor=self.tool_executor,
            store=self.workflow_store,
            policy=self.auth_policy,
            actor_resolver=self.current_actors.get,
            reconciler=reconcile_inconclusive,
        )

        state = WorkflowState(
            workflow_id=wf_id,
            definition=WorkflowDefinitionRef("customer_account_remediation", "1.0.0"),
            status=WorkflowStatus.RUNNING,
            current_step="mutation_execution",
            initiator_actor=WorkflowActorSnapshot("operator_bob", "senior_agent"),
            domain_context={
                "customer_id": "cust-001",
                "target_arguments": {
                    "amount_cents": 1000,
                    "customer_id": "cust-001",
                    "reason": "inconclusive test",
                },
                "stable_action_id": action_id,
            },
        )

        outcome = await mutation_step.execute(state)

        # Halts closed with FAILED, outcome_type="ambiguous_execution", manual_intervention_required=True
        self.assertEqual(outcome.status, StepExecutionStatus.FAILED)
        self.assertEqual(outcome.outcome_type, "ambiguous_execution")
        self.assertTrue(outcome.context_updates.get("manual_intervention_required"))

        # Balance remains unmodified! Zero duplicate mutation!
        self.assertEqual(self.customer_store.get_account("cust-001").balance_cents, 15000)

        # Ledger marked AMBIGUOUS
        self.assertEqual(
            self.workflow_store.load_mutation(action_id).status, MutationStatus.AMBIGUOUS
        )


if __name__ == "__main__":
    unittest.main()
