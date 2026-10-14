"""Hermetic concurrency tests verifying optimistic CAS conflict detection during resume."""

import asyncio
import os
import sys
import tempfile
import unittest
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
    ApplyFeeCreditCapability,
    GetAccountStatusCapability,
    GetCustomerCapability,
    SearchPolicyCapability,
    UpdateCustomerNoteCapability,
)
from agent.domain import InMemoryCustomerStore
from agent.executor import ToolExecutor
from agent.policy import CustomerSupportAuthorizationPolicy
from agent.registry import CapabilityRegistry
from contracts.agent import AgentActor, ApprovalDecision, ApprovalPort, CapabilityMetadata
from contracts.workflow import WorkflowStatus
from workflow.engine import WorkflowEngine
from workflow.errors import ConcurrentResumeConflictError
from workflow.remediation_workflow import build_customer_remediation_workflow
from workflow.state import WorkflowActorSnapshot
from workflow.store import SQLiteWorkflowStore


class StubAgentEngine:
    async def run(self, task: str, actor: AgentActor) -> Any:
        answer = (
            '{"proposal_type": "fee_credit", "summary": "Dispute refund", '
            '"suggested_arguments": {"amount_cents": 1500, "reason": "Outage concession"}, '
            '"requires_human_approval": true}'
        )

        class StubResult:
            def __init__(self, text: str) -> None:
                self.final_answer = text
                self.status = "COMPLETED"
                self.execution_receipts = []
                self.run_id = "agent-concur-1"
                self.step_count = 1

        return StubResult(answer)


class StubApprovalHandler(ApprovalPort):
    async def request_approval(
        self, actor: AgentActor, capability: CapabilityMetadata, arguments: Any
    ) -> ApprovalDecision:
        return ApprovalDecision(approved=True, approver="test_approver")


class TestConcurrentResume(unittest.IsolatedAsyncioTestCase):
    """Verify optimistic compare-and-swap concurrency control prevents race conditions."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "concurrent_test.db")
        self.customer_store = InMemoryCustomerStore()
        self.auth_policy = CustomerSupportAuthorizationPolicy(store=self.customer_store)
        self.current_actors = {"operator_bob": AgentActor("operator_bob", "senior_agent")}

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _create_engine(self) -> WorkflowEngine:
        sqlite_store = SQLiteWorkflowStore(self.db_path)
        read_only_caps = [
            GetCustomerCapability(self.customer_store),
            GetAccountStatusCapability(self.customer_store),
            SearchPolicyCapability(),
        ]
        credit_cap = ApplyFeeCreditCapability(self.customer_store)
        note_cap = UpdateCustomerNoteCapability(self.customer_store)
        executor = ToolExecutor()

        workflow_def = build_customer_remediation_workflow(
            customer_store=self.customer_store,
            agent_engine=StubAgentEngine(),
            read_only_registry=CapabilityRegistry(read_only_caps),
            credit_capability=credit_cap,
            note_capability=note_cap,
            tool_executor=executor,
            workflow_store=sqlite_store,
            auth_policy=self.auth_policy,
            actor_resolver=self.current_actors.get,
        )

        return WorkflowEngine(definition=workflow_def, store=sqlite_store)

    async def test_competing_resumes_result_in_exactly_one_winner_and_one_conflict(self) -> None:
        """Two concurrent resume requests for the same workflow: exactly 1 succeeds, 1 fails."""
        wf_id = "wf-concur-001"
        actor = WorkflowActorSnapshot(actor_id="operator_bob", role="senior_agent")

        # Initial workflow execution up to suspension
        engine_setup = self._create_engine()
        state = await engine_setup.start_workflow(
            workflow_id=wf_id,
            actor=actor,
            domain_context={"customer_id": "cust-001", "inquiry": "Overdraft dispute"},
        )
        self.assertEqual(state.status, WorkflowStatus.AWAITING_APPROVAL)

        # Create two distinct engine instances connecting to the same SQLite database
        engine_1 = self._create_engine()
        engine_2 = self._create_engine()

        successes: list[Any] = []
        conflicts: list[Exception] = []

        async def attempt_resume(engine: WorkflowEngine, approver_name: str) -> None:
            try:
                res = await engine.resume_workflow(
                    workflow_id=wf_id,
                    decision="approved",
                    approver_id=approver_name,
                    reason=f"Concurrent approval by {approver_name}",
                )
                successes.append(res)
            except ConcurrentResumeConflictError as exc:
                conflicts.append(exc)

        # Launch concurrent resume attempts simultaneously
        await asyncio.gather(
            attempt_resume(engine_1, "approver_alice"),
            attempt_resume(engine_2, "approver_bob"),
        )

        # Verification of Concurrency Invariants
        self.assertEqual(len(successes), 1, "Expected exactly 1 successful resume owner.")
        self.assertEqual(len(conflicts), 1, "Expected exactly 1 conflicting resume rejection.")
        self.assertIsInstance(conflicts[0], ConcurrentResumeConflictError)

        # Check physical customer balance: exactly one credit of $15.00 (+1500 cents = 16500)
        acc = self.customer_store.get_account("cust-001")
        assert acc is not None
        self.assertEqual(acc.balance_cents, 16500)


if __name__ == "__main__":
    unittest.main()
