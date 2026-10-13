"""Hermetic unit tests for AgentExecutionEngine bounded reasoning loop."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
BUILDING_BLOCKS_DIR = REPO_ROOT / "building-blocks" / "python"
AGENT_EXEC_DIR = Path(__file__).resolve().parent.parent

for p in (BUILDING_BLOCKS_DIR, AGENT_EXEC_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from agent.approval import DeterministicApprovalHandler
from agent.capabilities import (
    ApplyFeeCreditCapability,
    GetAccountStatusCapability,
    GetCustomerCapability,
    SearchPolicyCapability,
    UpdateCustomerNoteCapability,
)
from agent.domain import InMemoryCustomerStore
from agent.engine import AgentExecutionEngine
from agent.executor import ToolExecutor
from agent.policy import CustomerSupportAuthorizationPolicy
from agent.registry import CapabilityRegistry
from agent.test_doubles import ScriptedGenerationStub
from contracts.agent import AgentActor


class TestAgentExecutionEngine(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.store = InMemoryCustomerStore()
        self.registry = CapabilityRegistry()
        self.registry.register(GetCustomerCapability(self.store))
        self.registry.register(GetAccountStatusCapability(self.store))
        self.registry.register(SearchPolicyCapability())
        self.registry.register(UpdateCustomerNoteCapability(self.store))
        self.registry.register(ApplyFeeCreditCapability(self.store))

        self.policy = CustomerSupportAuthorizationPolicy(self.store)
        self.approval = DeterministicApprovalHandler(default_approved=True)
        self.executor = ToolExecutor()

        self.junior_agent = AgentActor(actor_id="agent-junior-01", role="junior_agent")
        self.senior_agent = AgentActor(actor_id="agent-senior-01", role="senior_agent")

    async def test_read_only_success(self) -> None:
        canned_responses = [
            json.dumps(
                {
                    "type": "action",
                    "action_name": "get_customer",
                    "arguments": {"customer_id": "cust-001"},
                    "explanation": "Looking up Alice Smith",
                }
            ),
            json.dumps(
                {
                    "type": "final",
                    "final_answer": "Customer Alice Smith is active with low risk tier.",
                    "explanation": "Verified records",
                }
            ),
        ]
        stub = ScriptedGenerationStub(canned_responses)
        engine = AgentExecutionEngine(
            llm_client=stub,
            registry=self.registry,
            policy=self.policy,
            approval_handler=self.approval,
            executor=self.executor,
            max_steps=5,
        )

        result = await engine.run("Check customer cust-001 status", actor=self.junior_agent)
        self.assertEqual(result.status, "COMPLETED")
        self.assertEqual(result.step_count, 2)
        self.assertIn("Alice Smith", result.final_answer)
        self.assertEqual(len(result.execution_receipts), 1)
        self.assertEqual(result.execution_receipts[0].status, "succeeded")
        self.assertEqual(result.trace.final_status, "COMPLETED")

    async def test_replay_finishes_trace_without_a_second_mutation(self) -> None:
        proposal = {
            "type": "action",
            "action_name": "update_customer_note",
            "arguments": {
                "customer_id": "cust-001",
                "note": "Reviewed request",
                "action_id": "note-once",
            },
        }
        responses = [
            proposal,
            {
                "type": "action",
                "action_name": "get_customer",
                "arguments": {"customer_id": "cust-001"},
            },
            proposal,
            {"type": "final", "final_answer": "Note recorded."},
        ]
        engine = AgentExecutionEngine(
            ScriptedGenerationStub([json.dumps(item) for item in responses]),
            self.registry,
            self.policy,
            self.approval,
            self.executor,
        )
        initial_notes = len(self.store.get_account("cust-001").notes)
        result = await engine.run("Record one note", self.senior_agent)
        self.assertEqual(result.status, "COMPLETED")
        self.assertEqual(result.trace.final_status, "COMPLETED")
        self.assertEqual(len(self.store.get_account("cust-001").notes), initial_notes + 1)
        self.assertEqual(len(self.executor.execution_history), 2)  # One mutation and one read.
        self.assertEqual(result.trace.steps[2].execution_kind, "replayed")
        self.assertEqual(len(result.execution_receipts), 2)
        json.dumps(result.trace.to_audit_summary())

    async def test_invalid_arguments_never_reach_authorization_or_approval(self) -> None:
        for arguments in (
            {"customer_id": "cust-001", "amount_cents": True, "reason": "test", "action_id": "a"},
            {"customer_id": "cust-001", "amount_cents": 10, "reason": "test", "action_id": " "},
        ):
            with (
                self.subTest(arguments=arguments),
                patch.object(self.policy, "authorize") as authorize,
                patch.object(self.approval, "request_approval") as approve,
            ):
                responses = [
                    {"type": "action", "action_name": "apply_fee_credit", "arguments": arguments},
                    {"type": "final", "final_answer": "Invalid request."},
                ]
                engine = AgentExecutionEngine(
                    ScriptedGenerationStub([json.dumps(item) for item in responses]),
                    self.registry,
                    self.policy,
                    self.approval,
                    self.executor,
                )
                result = await engine.run("Apply credit", self.senior_agent)
                self.assertFalse(result.trace.steps[0].validation_result.is_valid)
                self.assertEqual(result.execution_receipts, [])
                authorize.assert_not_called()
                approve.assert_not_called()

    async def test_multi_step_read_success(self) -> None:
        canned = [
            json.dumps(
                {
                    "type": "action",
                    "action_name": "get_customer",
                    "arguments": {"customer_id": "cust-001"},
                }
            ),
            json.dumps(
                {
                    "type": "action",
                    "action_name": "get_account_status",
                    "arguments": {"customer_id": "cust-001"},
                }
            ),
            json.dumps(
                {
                    "type": "final",
                    "final_answer": "Customer cust-001 has account with balance $150.00.",
                }
            ),
        ]
        stub = ScriptedGenerationStub(canned)
        engine = AgentExecutionEngine(
            llm_client=stub,
            registry=self.registry,
            policy=self.policy,
            approval_handler=self.approval,
            executor=self.executor,
        )

        result = await engine.run(
            "Retrieve full account profile for cust-001", actor=self.junior_agent
        )
        self.assertEqual(result.status, "COMPLETED")
        self.assertEqual(result.step_count, 3)
        self.assertEqual(len(result.execution_receipts), 2)
        self.assertIn("150.00", result.final_answer)

    async def test_clarification_path(self) -> None:
        canned = [
            json.dumps(
                {
                    "type": "clarification",
                    "clarification_question": "Please provide the customer ID or account number.",
                    "explanation": "Goal did not specify customer identifier.",
                }
            )
        ]
        stub = ScriptedGenerationStub(canned)
        engine = AgentExecutionEngine(
            llm_client=stub,
            registry=self.registry,
            policy=self.policy,
            approval_handler=self.approval,
            executor=self.executor,
        )

        result = await engine.run("Can you check customer balance?", actor=self.junior_agent)
        self.assertEqual(result.status, "NEEDS_CLARIFICATION")
        self.assertEqual(result.step_count, 1)
        self.assertIn("customer ID", result.final_answer)
        self.assertEqual(len(result.execution_receipts), 0)

    async def test_mutation_approved_path(self) -> None:
        approval_handler = DeterministicApprovalHandler(default_approved=True)
        canned = [
            json.dumps(
                {
                    "type": "action",
                    "action_name": "apply_fee_credit",
                    "arguments": {
                        "customer_id": "cust-001",
                        "amount_cents": 1500,
                        "reason": "Waive monthly maintenance fee",
                        "action_id": "act-fee-001",
                    },
                }
            ),
            json.dumps(
                {
                    "type": "final",
                    "final_answer": "A fee credit of $15.00 has been successfully applied to cust-001.",
                }
            ),
        ]
        stub = ScriptedGenerationStub(canned)
        engine = AgentExecutionEngine(
            llm_client=stub,
            registry=self.registry,
            policy=self.policy,
            approval_handler=approval_handler,
            executor=self.executor,
        )

        result = await engine.run("Waive $15 fee for cust-001", actor=self.junior_agent)
        self.assertEqual(result.status, "COMPLETED")
        self.assertEqual(len(result.execution_receipts), 1)
        self.assertEqual(result.execution_receipts[0].status, "succeeded")
        # Verify customer account balance increased from 15000 to 16500 cents
        acc = self.store.get_account("cust-001")
        self.assertIsNotNone(acc)
        self.assertEqual(acc.balance_cents, 16500)

    async def test_mutation_rejected_by_approver(self) -> None:
        # Explicit rejection in approval handler
        approval_handler = DeterministicApprovalHandler(default_approved=False)
        canned = [
            json.dumps(
                {
                    "type": "action",
                    "action_name": "apply_fee_credit",
                    "arguments": {
                        "customer_id": "cust-001",
                        "amount_cents": 1500,
                        "reason": "Waive fee",
                        "action_id": "act-fee-002",
                    },
                }
            )
        ]
        stub = ScriptedGenerationStub(canned)
        engine = AgentExecutionEngine(
            llm_client=stub,
            registry=self.registry,
            policy=self.policy,
            approval_handler=approval_handler,
            executor=self.executor,
        )

        result = await engine.run("Apply $15 credit", actor=self.junior_agent)
        self.assertEqual(result.status, "REJECTED")
        self.assertIn("rejected", result.final_answer.lower())
        # Tool must NOT have been executed
        self.assertEqual(len(result.execution_receipts), 0)
        # Account balance must remain unchanged
        acc = self.store.get_account("cust-001")
        self.assertIsNotNone(acc)
        self.assertEqual(acc.balance_cents, 15000)

    async def test_authorization_denied_limit_exceeded(self) -> None:
        # Junior agent has limit of $20 (2000 cents). Proposing 3500 cents should be blocked by policy.
        canned = [
            json.dumps(
                {
                    "type": "action",
                    "action_name": "apply_fee_credit",
                    "arguments": {
                        "customer_id": "cust-001",
                        "amount_cents": 3500,
                        "reason": "Customer loyalty",
                        "action_id": "act-fee-003",
                    },
                }
            )
        ]
        stub = ScriptedGenerationStub(canned)
        engine = AgentExecutionEngine(
            llm_client=stub,
            registry=self.registry,
            policy=self.policy,
            approval_handler=self.approval,
            executor=self.executor,
        )

        result = await engine.run("Apply $35 credit", actor=self.junior_agent)
        self.assertEqual(result.status, "DENIED")
        self.assertIn("not permitted", result.final_answer.lower())
        self.assertEqual(len(result.execution_receipts), 0)
        # Ensure approval handler was NEVER called because policy denied authorization upfront
        self.assertEqual(len(self.approval.recorded_requests), 0)

    async def test_authorization_denied_frozen_account(self) -> None:
        # cust-003 is frozen; mutations must be denied
        canned = [
            json.dumps(
                {
                    "type": "action",
                    "action_name": "update_customer_note",
                    "arguments": {
                        "customer_id": "cust-003",
                        "note": "Spoke with customer",
                        "action_id": "act-note-001",
                    },
                }
            )
        ]
        stub = ScriptedGenerationStub(canned)
        engine = AgentExecutionEngine(
            llm_client=stub,
            registry=self.registry,
            policy=self.policy,
            approval_handler=self.approval,
            executor=self.executor,
        )

        result = await engine.run("Add note for cust-003", actor=self.senior_agent)
        self.assertEqual(result.status, "DENIED")
        self.assertIn("frozen", result.final_answer.lower())
        self.assertEqual(len(result.execution_receipts), 0)

    async def test_parameter_validation_error_and_recovery(self) -> None:
        # Step 1: Missing required customer_id -> validation error returned in observation
        # Step 2: Corrects parameters -> succeeds
        # Step 3: Final answer
        canned = [
            json.dumps(
                {
                    "type": "action",
                    "action_name": "get_customer",
                    "arguments": {"invalid_param": "123"},
                }
            ),
            json.dumps(
                {
                    "type": "action",
                    "action_name": "get_customer",
                    "arguments": {"customer_id": "cust-001"},
                }
            ),
            json.dumps(
                {
                    "type": "final",
                    "final_answer": "Alice Smith is active.",
                }
            ),
        ]
        stub = ScriptedGenerationStub(canned)
        engine = AgentExecutionEngine(
            llm_client=stub,
            registry=self.registry,
            policy=self.policy,
            approval_handler=self.approval,
            executor=self.executor,
        )

        result = await engine.run("Inspect cust-001", actor=self.junior_agent)
        self.assertEqual(result.status, "COMPLETED")
        self.assertEqual(result.step_count, 3)
        # Step 1 should have recorded a validation error in trace
        step1 = result.trace.steps[0]
        self.assertIsNotNone(step1.validation_result)
        self.assertFalse(step1.validation_result.is_valid)
        self.assertIn("customer_id", step1.validation_result.errors[0])

    async def test_cycle_detection(self) -> None:
        # Same action and identical args consecutively -> cycle detection aborts
        identical_action = json.dumps(
            {
                "type": "action",
                "action_name": "get_customer",
                "arguments": {"customer_id": "cust-001"},
            }
        )
        stub = ScriptedGenerationStub([identical_action, identical_action])
        engine = AgentExecutionEngine(
            llm_client=stub,
            registry=self.registry,
            policy=self.policy,
            approval_handler=self.approval,
            executor=self.executor,
            max_steps=5,
        )

        result = await engine.run("Look up cust-001", actor=self.junior_agent)
        self.assertEqual(result.status, "MAX_STEPS_REACHED")
        self.assertIn("Cycle detected", result.final_answer)

    async def test_max_steps_reached(self) -> None:
        # 3 different read actions without ever emitting final answer
        # Note: use non-frozen customers cust-001, cust-002, cust-004 to avoid policy hold
        canned = [
            json.dumps(
                {
                    "type": "action",
                    "action_name": "get_customer",
                    "arguments": {"customer_id": "cust-001"},
                }
            ),
            json.dumps(
                {
                    "type": "action",
                    "action_name": "get_customer",
                    "arguments": {"customer_id": "cust-002"},
                }
            ),
            json.dumps(
                {
                    "type": "action",
                    "action_name": "get_customer",
                    "arguments": {"customer_id": "cust-004"},
                }
            ),
        ]
        stub = ScriptedGenerationStub(canned)
        engine = AgentExecutionEngine(
            llm_client=stub,
            registry=self.registry,
            policy=self.policy,
            approval_handler=self.approval,
            executor=self.executor,
            max_steps=3,
        )

        result = await engine.run("Check multiple customers", actor=self.junior_agent)
        self.assertEqual(result.status, "MAX_STEPS_REACHED")
        self.assertEqual(result.step_count, 3)

    async def test_repeated_malformed_json_fails(self) -> None:
        # 2 consecutive unparsable responses
        stub = ScriptedGenerationStub(["not json at all", "still not {valid json"])
        engine = AgentExecutionEngine(
            llm_client=stub,
            registry=self.registry,
            policy=self.policy,
            approval_handler=self.approval,
            executor=self.executor,
            max_steps=5,
        )

        result = await engine.run("Do something", actor=self.junior_agent)
        self.assertEqual(result.status, "FAILED")
        self.assertIn("repeatedly emitted malformed", result.final_answer)

    async def test_update_customer_note_requires_approval_and_succeeds_when_approved(self) -> None:
        """Verify update_customer_note requires approval and executes when approved."""
        approval_handler = DeterministicApprovalHandler(default_approved=True)
        canned = [
            json.dumps(
                {
                    "type": "action",
                    "action_name": "update_customer_note",
                    "arguments": {
                        "customer_id": "cust-001",
                        "note": "Verified customer identity via passport",
                        "action_id": "act-note-001",
                    },
                }
            ),
            json.dumps(
                {
                    "type": "final",
                    "final_answer": "Note added to customer record.",
                }
            ),
        ]
        stub = ScriptedGenerationStub(canned)
        engine = AgentExecutionEngine(
            llm_client=stub,
            registry=self.registry,
            policy=self.policy,
            approval_handler=approval_handler,
            executor=self.executor,
        )

        result = await engine.run("Add passport note for cust-001", actor=self.junior_agent)
        self.assertEqual(result.status, "COMPLETED")
        self.assertEqual(len(result.execution_receipts), 1)
        self.assertEqual(result.execution_receipts[0].status, "succeeded")
        self.assertEqual(len(approval_handler.recorded_requests), 1)
        self.assertEqual(
            approval_handler.recorded_requests[0]["capability_name"], "update_customer_note"
        )

        acc = self.store.get_account("cust-001")
        self.assertIsNotNone(acc)
        self.assertIn("Verified customer identity via passport", acc.notes)

    async def test_update_customer_note_blocked_when_approval_rejected(self) -> None:
        """Verify update_customer_note halts with REJECTED and zero mutation when human declines."""
        approval_handler = DeterministicApprovalHandler(default_approved=False)
        canned = [
            json.dumps(
                {
                    "type": "action",
                    "action_name": "update_customer_note",
                    "arguments": {
                        "customer_id": "cust-001",
                        "note": "Unverified note",
                        "action_id": "act-note-002",
                    },
                }
            )
        ]
        stub = ScriptedGenerationStub(canned)
        engine = AgentExecutionEngine(
            llm_client=stub,
            registry=self.registry,
            policy=self.policy,
            approval_handler=approval_handler,
            executor=self.executor,
        )

        result = await engine.run("Add unverified note for cust-001", actor=self.junior_agent)
        self.assertEqual(result.status, "REJECTED")
        self.assertEqual(len(result.execution_receipts), 0)
        self.assertEqual(len(approval_handler.recorded_requests), 1)

        acc = self.store.get_account("cust-001")
        self.assertIsNotNone(acc)
        self.assertNotIn("Unverified note", acc.notes)

    async def test_misconfigured_state_mutating_capability_forces_approval(self) -> None:
        """Finding 14: A capability declaring STATE_MUTATING with requires_approval=False must force approval."""
        from contracts.agent import (
            AuthorizationDecision,
            AuthorizationPort,
            CapabilityMetadata,
            CapabilityPort,
            SideEffectLevel,
        )

        class DummyMutatingTool(CapabilityPort):
            def __init__(self) -> None:
                self.executed = False
                self._meta = CapabilityMetadata(
                    name="unsafe_mutation",
                    description="Misconfigured tool",
                    input_schema={"type": "object", "properties": {"target": {"type": "string"}}},
                    side_effect_level=SideEffectLevel.STATE_MUTATING,
                    requires_approval=False,  # Intentionally misconfigured!
                )

            @property
            def metadata(self) -> CapabilityMetadata:
                return self._meta

            async def execute(self, arguments, context=None):
                self.executed = True
                return {"status": "mutated"}

        class AllowPolicy(AuthorizationPort):
            def authorize(self, actor, capability, arguments):
                return AuthorizationDecision(allowed=True, reason="Permitted for test")

        tool = DummyMutatingTool()
        self.registry.register(tool)

        # Approver will reject
        approval_handler = DeterministicApprovalHandler(default_approved=False)
        canned = [
            json.dumps(
                {
                    "type": "action",
                    "action_name": "unsafe_mutation",
                    "arguments": {"target": "data", "action_id": "unsafe-once"},
                    "explanation": "Attempt unapproved mutation",
                }
            )
        ]
        stub = ScriptedGenerationStub(canned)
        engine = AgentExecutionEngine(
            llm_client=stub,
            registry=self.registry,
            policy=AllowPolicy(),
            approval_handler=approval_handler,
            executor=self.executor,
        )

        result = await engine.run("Run unsafe mutation", actor=self.senior_agent)
        self.assertEqual(result.status, "REJECTED")
        self.assertFalse(
            tool.executed, "Misconfigured state-mutating tool must NOT execute without approval!"
        )
        self.assertEqual(len(result.execution_receipts), 0)
        # Verify approval handler WAS invoked despite requires_approval=False in metadata
        self.assertEqual(len(approval_handler.recorded_requests), 1)
        self.assertEqual(
            approval_handler.recorded_requests[0]["capability_name"], "unsafe_mutation"
        )


if __name__ == "__main__":
    unittest.main()
