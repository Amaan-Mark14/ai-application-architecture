"""Exercise evaluator verdicts with real workflows and intentionally wrong expectations."""

import importlib.util
import json
import sys
import unittest
from pathlib import Path

WORKFLOW_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = WORKFLOW_DIR.parent.parent
for directory in (
    REPO_ROOT / "building-blocks/python",
    REPO_ROOT / "examples/agent-execution",
    WORKFLOW_DIR,
):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

spec = importlib.util.spec_from_file_location(
    "workflow_eval_runner", WORKFLOW_DIR / "eval_runner.py"
)
assert spec is not None and spec.loader is not None
runner = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = runner
spec.loader.exec_module(runner)


class TestWorkflowEvaluationVerdicts(unittest.IsolatedAsyncioTestCase):
    def _row(self, **changes: object) -> dict[str, object]:
        return {
            "id": "eval-verdict-check",
            "customer_id": "cust-001",
            "actor_id": "operator_bob",
            "actor_role": "senior_agent",
            "inquiry": "Dispute duplicate fee",
            "canned_agent_answer": json.dumps(
                {
                    "proposal_type": "fee_credit",
                    "summary": "Duplicate fee",
                    "suggested_arguments": {"amount_cents": 1000, "reason": "Duplicate fee"},
                }
            ),
            "resume_decision": "approved",
            "expected_status": "COMPLETED",
            "expected_mutations": 2,
            **changes,
        }

    async def test_correct_terminal_status_with_wrong_mutation_count_fails(self) -> None:
        evaluator = runner.DeterministicWorkflowEvaluator(WORKFLOW_DIR / "eval_dataset.jsonl")
        passed = await evaluator.evaluate_scenario(self._row())
        self.assertTrue(passed.passed)
        self.assertEqual(passed.mutations_executed, 2)
        for incorrect_count in (0, 1, 3):
            with self.subTest(incorrect_count=incorrect_count):
                failed = await evaluator.evaluate_scenario(
                    self._row(expected_mutations=incorrect_count)
                )
                self.assertEqual(failed.actual_status, "COMPLETED")
                self.assertEqual(failed.mutations_executed, 2)
                self.assertFalse(failed.passed)

    async def test_unexpected_resume_exception_is_not_expected_workflow_failure(self) -> None:
        evaluator = runner.DeterministicWorkflowEvaluator(WORKFLOW_DIR / "eval_dataset.jsonl")
        result = await evaluator.evaluate_scenario(
            self._row(
                resume_decision=1,
                expected_status="FAILED",
                expected_mutations=0,
            )
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.actual_status, "EXCEPTION")
        self.assertIsNotNone(result.error_message)
