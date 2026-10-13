"""Hermetic unit tests verifying that evaluation safety invariant counters are evidence-derived.

Proves Finding 3 / Finding 25 / Finding 73:
1. Counters are NOT hardcoded assumptions.
2. Invariant status accurately switches to FAIL when controlled violations are injected.
3. Every zero-tolerance safety invariant is individually measured.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
BUILDING_BLOCKS_DIR = REPO_ROOT / "building-blocks" / "python"
AGENT_EXEC_DIR = Path(__file__).resolve().parent.parent

for p in (BUILDING_BLOCKS_DIR, AGENT_EXEC_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from agent.engine import AgentExecutionResult
from agent.trace import AgentStepRecord, AgentTrajectoryTrace
from contracts.agent import (
    AgentActor,
    AgentDecision,
    AgentDecisionType,
    ApprovalDecision,
    AuthorizationDecision,
    ExecutionReceipt,
)
from eval_runner import EvalScenario, compute_safety_metrics


class TestEvalInvariants(unittest.TestCase):
    """Test suite proving evaluation safety invariants are dynamically measured from evidence."""

    def setUp(self) -> None:
        self.scenario = EvalScenario(
            id="test-sc-001",
            category="mutation_test",
            goal="Test invariant measurement",
            actor_id="agent-001",
            actor_role="junior_agent",
            approval_response=True,
            canned_responses=[],
            expected_status="COMPLETED",
            expected_tools=["apply_fee_credit"],
            prohibited_tools=[],
            must_contain=[],
            max_steps=3,
        )
        self.registered_tools = [
            "get_customer",
            "get_account_status",
            "search_policy",
            "update_customer_note",
            "apply_fee_credit",
        ]
        self.mutating_tools = ["apply_fee_credit", "update_customer_note"]

    def _create_base_result(
        self,
        status: str = "COMPLETED",
        step_count: int = 1,
        receipts: list[ExecutionReceipt] | None = None,
        steps: list[AgentStepRecord] | None = None,
    ) -> AgentExecutionResult:
        actor = AgentActor(actor_id="agent-001", role="junior_agent")
        trace = AgentTrajectoryTrace(run_id="run-test", goal="Test goal", actor=actor)
        if steps:
            for s in steps:
                trace.record_step(s)
            # Test fixtures bind each physical receipt by exact action ID, never by tool name.
            for receipt in receipts or []:
                match = next(
                    (
                        step
                        for step in steps
                        if step.execution_receipt is None
                        and step.decision is not None
                        and step.decision.action_name == receipt.capability_name
                        and step.decision.arguments.get("action_id") == receipt.action_id
                    ),
                    None,
                )
                if match is not None:
                    match.execution_receipt = receipt
                    match.execution_kind = "executed"
        trace.complete(status, "Done")

        return AgentExecutionResult(
            run_id="run-test",
            goal="Test goal",
            status=status,
            final_answer="Done",
            step_count=step_count,
            execution_receipts=receipts or [],
            trace=trace,
            total_latency_ms=10.0,
        )

    def test_clean_execution_invariants_pass(self) -> None:
        receipt = ExecutionReceipt("approved", "apply_fee_credit", "succeeded")
        step = self._mutation_step(1, "approved", receipt, kind="executed")
        metrics = self._metrics([receipt], [step])
        self.assertTrue(metrics.passed)
        self.assertEqual(metrics.total_violations, 0)

    def test_unauthorized_mutation_detected(self) -> None:
        receipt = ExecutionReceipt("denied", "apply_fee_credit", "succeeded")
        step = self._mutation_step(1, "denied", receipt, allowed=False, kind="executed")
        metrics = self._metrics([receipt], [step], "DENIED")
        self.assertFalse(metrics.passed)
        self.assertEqual(metrics.unauthorized_mutation_executions, 1)

    def test_unapproved_mutation_and_execution_after_rejection_detected(self) -> None:
        receipt = ExecutionReceipt("declined", "apply_fee_credit", "succeeded")
        step = self._mutation_step(1, "declined", receipt, approved=False, kind="executed")
        metrics = self._metrics([receipt], [step], "REJECTED")
        self.assertFalse(metrics.passed)
        self.assertEqual(metrics.unapproved_required_mutation_executions, 1)
        self.assertEqual(metrics.executions_after_rejection, 1)

    def test_unknown_capability_execution_detected(self) -> None:
        """Inject execution receipt for unregistered tool and verify unknown_capability_executions > 0."""
        receipt = ExecutionReceipt(
            action_id="act-unknown-01",
            capability_name="unregistered_malicious_tool",
            status="succeeded",
        )
        result = self._create_base_result(
            status="COMPLETED",
            step_count=1,
            receipts=[receipt],
        )

        metrics = compute_safety_metrics(
            result=result,
            scenario=self.scenario,
            registered_tools=self.registered_tools,
            mutating_tools=self.mutating_tools,
        )

        self.assertFalse(metrics.passed)
        self.assertEqual(metrics.unknown_capability_executions, 1)

    def test_max_step_termination_violation_detected(self) -> None:
        """Inject step count exceeding scenario.max_steps and verify max_step violation is counted."""
        result = self._create_base_result(
            status="MAX_STEPS_REACHED",
            step_count=5,  # scenario.max_steps is 3
        )

        metrics = compute_safety_metrics(
            result=result,
            scenario=self.scenario,
            registered_tools=self.registered_tools,
            mutating_tools=self.mutating_tools,
        )

        self.assertFalse(metrics.passed)
        self.assertEqual(metrics.executions_after_max_step_termination, 2)

    @staticmethod
    def _mutation_step(number, action_id, receipt=None, allowed=True, approved=True, kind=None):
        return AgentStepRecord(
            step_number=number,
            decision=AgentDecision(
                AgentDecisionType.ACTION,
                action_name="apply_fee_credit",
                arguments={"action_id": action_id},
            ),
            authorization_result=AuthorizationDecision(allowed, "Test policy"),
            approval_result=ApprovalDecision(approved, "test") if approved is not None else None,
            execution_receipt=receipt,
            execution_kind=kind,
        )

    def _metrics(self, receipts, steps, status="COMPLETED"):
        result = self._create_base_result(status, len(steps), receipts, steps)
        return compute_safety_metrics(
            result, self.scenario, self.registered_tools, self.mutating_tools
        )

    def test_earlier_approved_execution_does_not_become_execution_after_rejection(self) -> None:
        receipt = ExecutionReceipt("approved", "apply_fee_credit", "succeeded")
        steps = [
            self._mutation_step(1, "approved", receipt, kind="executed"),
            self._mutation_step(2, "rejected", approved=False),
        ]
        metrics = self._metrics([receipt], steps, "REJECTED")
        self.assertTrue(metrics.passed)
        self.assertEqual(metrics.executions_after_rejection, 0)

    def test_authorization_and_approval_are_checked_for_each_call(self) -> None:
        first = ExecutionReceipt("first", "apply_fee_credit", "succeeded")
        second = ExecutionReceipt("second", "apply_fee_credit", "succeeded")
        steps = [
            self._mutation_step(1, "first", first, kind="executed"),
            self._mutation_step(2, "second", second, allowed=False, approved=None, kind="executed"),
        ]
        metrics = self._metrics([first, second], steps, "DENIED")
        self.assertEqual(metrics.unauthorized_mutation_executions, 1)
        self.assertEqual(metrics.unapproved_required_mutation_executions, 1)
        self.assertEqual(metrics.execution_evidence_errors, 0)

    def test_earlier_denied_call_does_not_authorize_or_taint_another_call(self) -> None:
        receipt = ExecutionReceipt("allowed", "apply_fee_credit", "succeeded")
        steps = [
            self._mutation_step(1, "denied", allowed=False, approved=None),
            self._mutation_step(2, "allowed", receipt, kind="executed"),
        ]
        self.assertTrue(self._metrics([receipt], steps).passed)

    def test_replays_are_not_duplicate_effects_or_post_rejection_executions(self) -> None:
        receipt = ExecutionReceipt("once", "apply_fee_credit", "succeeded")
        steps = [
            self._mutation_step(1, "once", receipt, kind="executed"),
            self._mutation_step(2, "declined", approved=False),
            self._mutation_step(3, "once", receipt, kind="replayed"),
        ]
        metrics = self._metrics([receipt], steps)
        self.assertTrue(metrics.passed)
        self.assertEqual(metrics.duplicate_mutation_incidents, 0)
        self.assertEqual(metrics.executions_after_rejection, 0)

    def test_genuine_physical_duplicate_is_still_detected(self) -> None:
        first = ExecutionReceipt("duplicate", "apply_fee_credit", "succeeded", executed_at=1)
        second = ExecutionReceipt("duplicate", "apply_fee_credit", "succeeded", executed_at=2)
        steps = [
            self._mutation_step(1, "duplicate", first, kind="executed"),
            self._mutation_step(2, "duplicate", second, kind="executed"),
        ]
        metrics = self._metrics([first, second], steps)
        self.assertEqual(metrics.duplicate_mutation_incidents, 1)
        self.assertEqual(metrics.unauthorized_mutation_executions, 0)
        self.assertEqual(metrics.unapproved_required_mutation_executions, 0)

    def test_only_actual_later_dispatches_count_after_rejection(self) -> None:
        first = ExecutionReceipt("before", "apply_fee_credit", "succeeded")
        last = ExecutionReceipt("after", "apply_fee_credit", "succeeded")
        steps = [
            self._mutation_step(1, "before", first, kind="executed"),
            self._mutation_step(2, "rejected", approved=False),
            self._mutation_step(3, "after", last, kind="executed"),
        ]
        metrics = self._metrics([first, last], steps)
        self.assertEqual(metrics.executions_after_rejection, 1)

    def test_wrong_action_identity_cannot_inherit_an_approval(self) -> None:
        receipt = ExecutionReceipt("different-action", "apply_fee_credit", "succeeded")
        step = self._mutation_step(1, "approved-action", receipt, kind="executed")
        metrics = self._metrics([receipt], [step])
        self.assertEqual(metrics.unauthorized_mutation_executions, 1)
        self.assertEqual(metrics.unapproved_required_mutation_executions, 1)
        self.assertGreater(metrics.execution_evidence_errors, 0)

    def test_missing_bindings_and_empty_ids_fail_without_timestamp_fallback(self) -> None:
        for receipt in (
            ExecutionReceipt("orphan", "apply_fee_credit", "succeeded"),
            ExecutionReceipt("", "apply_fee_credit", "succeeded"),
        ):
            metrics = self._metrics([receipt], [])
            self.assertFalse(metrics.passed)
            self.assertGreater(metrics.execution_evidence_errors, 0)

    def test_pre_dispatch_rejection_is_not_a_physical_execution(self) -> None:
        receipt = ExecutionReceipt("rejected", "apply_fee_credit", "failed")
        step = self._mutation_step(1, "rejected", receipt, approved=False, kind="rejected")
        self.assertTrue(self._metrics([], [step], "REJECTED").passed)


if __name__ == "__main__":
    unittest.main()
