"""Unit tests for agent decision extraction and schema parsing."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
BUILDING_BLOCKS_DIR = REPO_ROOT / "building-blocks" / "python"
AGENT_EXEC_DIR = Path(__file__).resolve().parent.parent

for p in (BUILDING_BLOCKS_DIR, AGENT_EXEC_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from agent.engine import AgentExecutionEngine
from agent.policy import CustomerSupportAuthorizationPolicy
from agent.registry import CapabilityRegistry
from agent.test_doubles import ScriptedGenerationStub
from contracts.agent import AgentDecisionType


class TestDecisionParser(unittest.TestCase):
    """Test suite verifying robust JSON decision parsing from untrusted model outputs."""

    def setUp(self) -> None:
        self.engine = AgentExecutionEngine(
            llm_client=ScriptedGenerationStub(),
            registry=CapabilityRegistry(),
            policy=CustomerSupportAuthorizationPolicy(),
            approval_handler=None,  # type: ignore[arg-type]
        )

    def test_parse_valid_action_decision(self) -> None:
        raw = '{"type": "action", "action_name": "get_customer", "arguments": {"customer_id": "cust-001"}}'
        decision, err = self.engine._parse_decision(raw)
        self.assertIsNone(err)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.decision_type, AgentDecisionType.ACTION)
        self.assertEqual(decision.action_name, "get_customer")
        self.assertEqual(decision.arguments, {"customer_id": "cust-001"})

    def test_decision_fields_require_declared_types(self) -> None:
        for decision_type, field in (
            ("final", "final_answer"),
            ("clarification", "clarification_question"),
        ):
            for value in (42, True, {}, [], None, " "):
                with self.subTest(decision_type=decision_type, value=value):
                    decision, error = self.engine._parse_decision(
                        json.dumps({"type": decision_type, field: value})
                    )
                    self.assertIsNone(decision)
                    self.assertIsNotNone(error)
        for payload in ({"type": 1}, {"type": "final", "final_answer": "ok", "explanation": 4}):
            decision, error = self.engine._parse_decision(json.dumps(payload))
            self.assertIsNone(decision)
            self.assertIsNotNone(error)

    def test_parse_action_with_markdown_fences(self) -> None:
        raw = """Here is the next step:
```json
{
  "type": "action",
  "action_name": "apply_fee_credit",
  "arguments": {"customer_id": "cust-001", "amount_cents": 1500, "reason": "Billing error", "action_id": "act-1"}
}
```
Hope this helps!"""
        decision, err = self.engine._parse_decision(raw)
        self.assertIsNone(err)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.decision_type, AgentDecisionType.ACTION)
        self.assertEqual(decision.action_name, "apply_fee_credit")
        self.assertEqual(decision.arguments["amount_cents"], 1500)

    def test_parse_valid_final_decision(self) -> None:
        raw = '{"type": "final", "final_answer": "Account balance is $150.00."}'
        decision, err = self.engine._parse_decision(raw)
        self.assertIsNone(err)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.decision_type, AgentDecisionType.FINAL)
        self.assertEqual(decision.final_answer, "Account balance is $150.00.")

    def test_parse_valid_clarification_decision(self) -> None:
        raw = '{"type": "clarification", "clarification_question": "Please supply the customer identifier."}'
        decision, err = self.engine._parse_decision(raw)
        self.assertIsNone(err)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.decision_type, AgentDecisionType.CLARIFICATION)
        self.assertEqual(decision.clarification_question, "Please supply the customer identifier.")

    def test_parse_malformed_json_returns_error(self) -> None:
        raw = '{"type": "action", "action_name": "incomplete'
        decision, err = self.engine._parse_decision(raw)
        self.assertIsNone(decision)
        self.assertIsNotNone(err)
        self.assertIn("Failed to parse model JSON", err)

    def test_parse_unknown_decision_type_returns_error(self) -> None:
        raw = '{"type": "execute_code", "code": "import os"}'
        decision, err = self.engine._parse_decision(raw)
        self.assertIsNone(decision)
        self.assertIsNotNone(err)
        self.assertIn("Unknown decision type", err)

    def test_parse_rejects_admin_override_field(self) -> None:
        raw = '{"type": "action", "action_name": "get_customer", "arguments": {}, "admin_override": true}'
        decision, err = self.engine._parse_decision(raw)
        self.assertIsNone(decision)
        self.assertIsNotNone(err)
        self.assertIn("unexpected or conflicting field(s)", err or "")
        self.assertIn("admin_override", err or "")

    def test_parse_rejects_approval_not_required_field(self) -> None:
        raw = '{"type": "action", "action_name": "apply_fee_credit", "arguments": {}, "approval_not_required": true}'
        decision, err = self.engine._parse_decision(raw)
        self.assertIsNone(decision)
        self.assertIsNotNone(err)
        self.assertIn("approval_not_required", err or "")

    def test_parse_rejects_authorized_field(self) -> None:
        raw = '{"type": "action", "action_name": "apply_fee_credit", "arguments": {}, "authorized": true}'
        decision, err = self.engine._parse_decision(raw)
        self.assertIsNone(decision)
        self.assertIsNotNone(err)
        self.assertIn("authorized", err or "")

    def test_parse_rejects_unexpected_field(self) -> None:
        raw = '{"type": "action", "action_name": "get_customer", "arguments": {}, "unexpected": "payload"}'
        decision, err = self.engine._parse_decision(raw)
        self.assertIsNone(decision)
        self.assertIsNotNone(err)
        self.assertIn("unexpected", err or "")

    def test_parse_rejects_conflicting_final_answer_on_action(self) -> None:
        raw = '{"type": "action", "action_name": "get_customer", "arguments": {}, "final_answer": "Done"}'
        decision, err = self.engine._parse_decision(raw)
        self.assertIsNone(decision)
        self.assertIsNotNone(err)
        self.assertIn("final_answer", err or "")

    def test_parse_rejects_action_fields_on_final_decision(self) -> None:
        raw = '{"type": "final", "final_answer": "Done with task", "action_name": "get_customer"}'
        decision, err = self.engine._parse_decision(raw)
        self.assertIsNone(decision)
        self.assertIsNotNone(err)
        self.assertIn("action_name", err or "")

    def test_parse_rejects_action_fields_on_clarification(self) -> None:
        raw = '{"type": "clarification", "clarification_question": "Which ID?", "arguments": {}}'
        decision, err = self.engine._parse_decision(raw)
        self.assertIsNone(decision)
        self.assertIsNotNone(err)
        self.assertIn("arguments", err or "")


if __name__ == "__main__":
    unittest.main()
