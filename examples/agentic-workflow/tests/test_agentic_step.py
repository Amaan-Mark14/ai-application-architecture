"""Hermetic unit tests for AgenticInvestigationStep and read-only registry safety."""

import json
import sys
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

from agent.registry import CapabilityRegistry
from contracts.agent import (
    AgentActor,
    CapabilityMetadata,
    CapabilityPort,
    SideEffectLevel,
)
from contracts.workflow import StepExecutionStatus, WorkflowDefinitionRef, WorkflowStatus
from workflow.state import (
    RemediationType,
    WorkflowActorSnapshot,
    WorkflowState,
)
from workflow.steps.agentic import AgenticInvestigationStep


class DummyReadOnlyCapability(CapabilityPort):
    def __init__(self, name: str = "get_customer") -> None:
        self._metadata = CapabilityMetadata(
            name=name,
            description="Read-only query",
            input_schema={"type": "object"},
            side_effect_level=SideEffectLevel.READ_ONLY,
        )

    @property
    def metadata(self) -> CapabilityMetadata:
        return self._metadata

    async def execute(self, arguments: Mapping[str, Any], context: Any | None = None) -> Any:
        return {"customer_id": "cust-1", "name": "Alice"}


class DummyMutatingCapability(CapabilityPort):
    def __init__(self, name: str = "apply_fee_credit") -> None:
        self._metadata = CapabilityMetadata(
            name=name,
            description="Mutating credit action",
            input_schema={"type": "object"},
            side_effect_level=SideEffectLevel.STATE_MUTATING,
            requires_approval=True,
        )

    @property
    def metadata(self) -> CapabilityMetadata:
        return self._metadata

    async def execute(self, arguments: Mapping[str, Any], context: Any | None = None) -> Any:
        return {"credited": True}


class MockAgentEngine:
    def __init__(self, answer_text: Any, status: str = "COMPLETED") -> None:
        self.answer_text = answer_text
        self.status = status
        self.tasks: list[str] = []

    async def run(self, task: str, actor: AgentActor) -> Any:
        self.tasks.append(task)

        class MockResult:
            def __init__(self, answer: Any, status: str) -> None:
                self.final_answer = answer
                self.total_steps = 1
                self.execution_receipts = []
                self.status = status
                self.run_id = "run-mock-123"
                self.episodes = ()

        return MockResult(self.answer_text, self.status)


class TestAgenticInvestigationStep(unittest.IsolatedAsyncioTestCase):
    """Verify read-only invariant and structured proposal parsing."""

    def _state(self, **context: Any) -> WorkflowState:
        return WorkflowState(
            workflow_id="wf-investigation",
            definition=WorkflowDefinitionRef("customer_account_remediation", "1.0.0"),
            status=WorkflowStatus.RUNNING,
            current_step="agentic_investigation",
            initiator_actor=WorkflowActorSnapshot("operator_bob", "senior_agent"),
            domain_context={
                "customer_id": "cust-001",
                "inquiry": "Dispute duplicate wire fee",
                **context,
            },
        )

    def _step(self, answer: Any, status: str = "COMPLETED") -> AgenticInvestigationStep:
        return AgenticInvestigationStep(
            MockAgentEngine(answer, status), CapabilityRegistry([DummyReadOnlyCapability()])
        )

    def test_enforces_read_only_registry_invariant(self) -> None:
        """Investigation step MUST reject any registry containing STATE_MUTATING tools."""
        mutating_registry = CapabilityRegistry(
            [DummyReadOnlyCapability(), DummyMutatingCapability()]
        )
        with self.assertRaises(ValueError) as ctx:
            AgenticInvestigationStep(
                agent_engine=MockAgentEngine(""),
                read_only_registry=mutating_registry,
            )
        self.assertIn("Security Invariant Violation", str(ctx.exception))
        self.assertIn("Mutating capability 'apply_fee_credit' detected", str(ctx.exception))

    async def test_successful_proposal_parsing_fee_credit(self) -> None:
        """Parses structured JSON proposal block into typed RemediationProposal."""
        read_only_registry = CapabilityRegistry([DummyReadOnlyCapability()])
        model_output = (
            "Based on the transaction history, the customer was double-charged due to system timeout.\n"
            "```json\n"
            "{\n"
            '  "proposal_type": "fee_credit",\n'
            '  "summary": "Refund accidental overdraft fee",\n'
            '  "suggested_arguments": {"amount_cents": 3500, "reason": "Duplicate fee"},\n'
            '  "requires_human_approval": true\n'
            "}\n"
            "```\n"
            "Please review."
        )

        step = AgenticInvestigationStep(
            agent_engine=MockAgentEngine(model_output),
            read_only_registry=read_only_registry,
        )

        state = WorkflowState(
            workflow_id="wf-inv-001",
            definition=WorkflowDefinitionRef("customer_account_remediation", "1.0.0"),
            status=WorkflowStatus.RUNNING,
            current_step="agentic_investigation",
            initiator_actor=WorkflowActorSnapshot("user-1", "operator"),
            history=(),
            domain_context={"customer_id": "cust-123", "inquiry": "Dispute duplicate wire fee"},
        )

        outcome = await step.execute(state)

        self.assertEqual(outcome.status, StepExecutionStatus.SUCCEEDED)
        self.assertEqual(outcome.outcome_type, RemediationType.FEE_CREDIT.value)
        proposal_dict = outcome.context_updates.get("remediation_proposal")
        self.assertIsNotNone(proposal_dict)
        assert proposal_dict is not None
        self.assertEqual(proposal_dict["proposal_type"], "fee_credit")
        self.assertEqual(proposal_dict["suggested_arguments"]["amount_cents"], 3500)

    async def test_unstructured_output_fails_instead_of_inventing_proposal(self) -> None:
        """Unparseable model answers must not become successful recommendations."""
        read_only_registry = CapabilityRegistry([DummyReadOnlyCapability()])
        model_output = "The customer account is in good standing. No financial discrepancy found."

        step = AgenticInvestigationStep(
            agent_engine=MockAgentEngine(model_output),
            read_only_registry=read_only_registry,
        )

        state = WorkflowState(
            workflow_id="wf-inv-002",
            definition=WorkflowDefinitionRef("customer_account_remediation", "1.0.0"),
            status=WorkflowStatus.RUNNING,
            current_step="agentic_investigation",
            initiator_actor=WorkflowActorSnapshot("user-1", "operator"),
            history=(),
            domain_context={"customer_id": "cust-123", "inquiry": "Check account"},
        )

        outcome = await step.execute(state)

        self.assertEqual(outcome.status, StepExecutionStatus.FAILED)
        self.assertEqual(outcome.outcome_type, "invalid_proposal")
        self.assertIsNone(outcome.proposal)

    async def test_submitted_inquiry_is_in_agent_goal(self) -> None:
        inquiry = 'Dispute "wire fee" of $35 on 14 October; account suffix 9876'
        step = self._step(
            '{"proposal_type":"informational","summary":"Reviewed","suggested_arguments":{}}'
        )
        outcome = await step.execute(self._state(inquiry=inquiry))
        self.assertEqual(outcome.status, StepExecutionStatus.SUCCEEDED)
        self.assertIn(inquiry, step.agent_engine.tasks[0])

    async def test_missing_or_invalid_inquiry_never_calls_agent(self) -> None:
        for inquiry in (None, "", " \t", 7, {"text": "Dispute fee"}):
            with self.subTest(inquiry=inquiry):
                step = self._step("Should never run")
                outcome = await step.execute(self._state(inquiry=inquiry))
                self.assertEqual(outcome.outcome_type, "invalid_input")
                self.assertEqual(step.agent_engine.tasks, [])

    async def test_incomplete_agent_cannot_supply_proposal(self) -> None:
        valid = '{"proposal_type":"fee_credit","summary":"Refund","suggested_arguments":{"amount_cents":1000,"reason":"Duplicate"}}'
        for status in (
            "FAILED",
            "NEEDS_CLARIFICATION",
            "DENIED",
            "REJECTED",
            "MAX_STEPS_REACHED",
            "",
            "UNKNOWN",
        ):
            with self.subTest(status=status):
                outcome = await self._step(valid, status).execute(self._state())
                self.assertEqual(outcome.status, StepExecutionStatus.FAILED)
                self.assertIsNone(outcome.proposal)
                self.assertNotIn("remediation_proposal", outcome.context_updates)

    async def test_malformed_proposals_never_produce_mutations(self) -> None:
        valid = {
            "proposal_type": "fee_credit",
            "summary": "Refund",
            "suggested_arguments": {"amount_cents": 1000, "reason": "Duplicate"},
        }
        invalid: list[Any] = [
            "Do not issue a refund",
            "No credit, waive, or note is appropriate",
            "{broken refund}",
            None,
            {"proposal_type": "fee_credit"},
            {**valid, "proposal_type": 1},
            {**valid, "proposal_type": "goto_finalization"},
            {**valid, "summary": False},
            {**valid, "summary": "  "},
            {**valid, "suggested_arguments": []},
            {**valid, "suggested_arguments": {"amount_cents": True, "reason": "Duplicate"}},
            {**valid, "suggested_arguments": {"amount_cents": "1000", "reason": "Duplicate"}},
            {**valid, "suggested_arguments": {"amount_cents": -1, "reason": "Duplicate"}},
            {**valid, "suggested_arguments": {"amount_cents": 1.5, "reason": "Duplicate"}},
            {**valid, "suggested_arguments": {"amount_cents": 1000, "reason": " "}},
            {
                **valid,
                "suggested_arguments": {
                    "amount_cents": 1000,
                    "reason": "Duplicate",
                    "customer_id": "cust-002",
                },
            },
            {**valid, "requires_approval": "false"},
            {**valid, "goto": "finalization"},
            {**valid, "requires_approval": False, "requires_human_approval": True},
            {
                "proposal_type": "customer_note",
                "summary": "Record",
                "suggested_arguments": {"note": 123},
            },
            {
                "proposal_type": "informational",
                "summary": "Info",
                "suggested_arguments": {"amount_cents": 1000},
            },
            '{"proposal_type":"fee_credit","proposal_type":"informational","summary":"X","suggested_arguments":{}}',
            json.dumps(valid) + json.dumps(valid),
        ]
        for value in invalid:
            answer = json.dumps(value) if isinstance(value, dict) else value
            with self.subTest(answer=answer):
                outcome = await self._step(answer).execute(self._state())
                self.assertEqual(outcome.status, StepExecutionStatus.FAILED)
                self.assertEqual(outcome.outcome_type, "invalid_proposal")
                self.assertIsNone(outcome.proposal)

    async def test_model_cannot_disable_required_credit_approval(self) -> None:
        outcome = await self._step(
            '{"proposal_type":"fee_credit","summary":"Refund","suggested_arguments":{"amount_cents":1000,"reason":"Duplicate"},"requires_approval":false}'
        ).execute(self._state())
        self.assertIsNotNone(outcome.proposal)
        assert outcome.proposal is not None
        self.assertTrue(outcome.proposal.requires_approval)


if __name__ == "__main__":
    unittest.main()
