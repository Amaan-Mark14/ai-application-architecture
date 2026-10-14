"""Hermetic unit tests for durable approval verification and Phase 6 adapter."""

import sys
import time
import unittest
from dataclasses import replace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
BUILDING_BLOCKS_DIR = REPO_ROOT / "building-blocks" / "python"
AGENT_EXEC_DIR = REPO_ROOT / "examples" / "agent-execution"
WORKFLOW_DIR = Path(__file__).resolve().parent.parent

for p in (BUILDING_BLOCKS_DIR, AGENT_EXEC_DIR, WORKFLOW_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from contracts.agent import AgentActor, CapabilityMetadata, SideEffectLevel
from contracts.workflow import ApprovalStatus, WorkflowDefinitionRef
from workflow.approval import (
    DurableApprovalRecord,
    WorkflowApprovalAdapter,
    WorkflowApprovalVerifier,
    canonicalize_arguments,
    resolve_current_actor,
)
from workflow.errors import (
    ApprovalMismatchError,
    ApprovalReplayError,
    ApprovalVerificationError,
)


class TestWorkflowApprovalVerifier(unittest.TestCase):
    """Verify all 7 security binding assertions of WorkflowApprovalVerifier."""

    def setUp(self) -> None:
        self.workflow_id = "wf-test-001"
        self.action_id = "act-wf-test-001-mutation-abc1234"
        self.actor = AgentActor(actor_id="operator_bob", role="support_tier_2")
        self.capability = CapabilityMetadata(
            name="apply_fee_credit",
            description="Apply monetary credit to customer account.",
            input_schema={"type": "object"},
            side_effect_level=SideEffectLevel.STATE_MUTATING,
            requires_approval=True,
        )
        self.args = {"customer_id": "cust-99", "amount": 2500}
        self.canonical_args = canonicalize_arguments(self.args)

        self.valid_record = DurableApprovalRecord(
            approval_id="app-12345",
            workflow_id=self.workflow_id,
            workflow_definition_id="customer_account_remediation",
            workflow_definition_version="1.0.0",
            action_id=self.action_id,
            capability_name="apply_fee_credit",
            canonical_arguments_json=self.canonical_args,
            actor_id=self.actor.actor_id,
            actor_role=self.actor.role,
            status=ApprovalStatus.APPROVED,
            approver_id="manager_alice",
            approver_role="manager",
            reason="Customer experienced validated platform outage.",
            requested_at=time.time() - 60,
            decided_at=time.time() - 30,
            expires_at=time.time() + 3600,
        )

    def test_verify_success_with_exact_bindings(self) -> None:
        """When all 7 bindings match exactly, verification succeeds without error."""
        WorkflowApprovalVerifier.verify(
            record=self.valid_record,
            expected_workflow_id=self.workflow_id,
            expected_action_id=self.action_id,
            expected_definition=WorkflowDefinitionRef("customer_account_remediation", "1.0.0"),
            capability=self.capability,
            arguments=self.args,
            actor=self.actor,
        )

    def test_definition_identity_and_version_must_match(self) -> None:
        for record in (
            replace(self.valid_record, workflow_definition_id="different_definition"),
            replace(self.valid_record, workflow_definition_version="2.0.0"),
        ):
            with (
                self.subTest(
                    definition=record.workflow_definition_id,
                    version=record.workflow_definition_version,
                ),
                self.assertRaises(ApprovalMismatchError),
            ):
                WorkflowApprovalVerifier.verify(
                    record,
                    self.workflow_id,
                    self.action_id,
                    self.capability,
                    self.args,
                    self.actor,
                    WorkflowDefinitionRef("customer_account_remediation", "1.0.0"),
                )

    def test_verify_rejects_already_consumed_approval(self) -> None:
        """Replay check: A CONSUMED approval must raise ApprovalReplayError."""
        consumed_record = DurableApprovalRecord(
            **{
                **self.valid_record.to_dict(),
                "status": ApprovalStatus.CONSUMED.value,
                "consumed_at": time.time(),
            }
        )
        with self.assertRaises(ApprovalReplayError):
            WorkflowApprovalVerifier.verify(
                record=consumed_record,
                expected_workflow_id=self.workflow_id,
                expected_action_id=self.action_id,
                expected_definition=WorkflowDefinitionRef("customer_account_remediation", "1.0.0"),
                capability=self.capability,
                arguments=self.args,
                actor=self.actor,
            )

    def test_verify_rejects_pending_or_rejected_status(self) -> None:
        """Status check: Non-approved status must raise ApprovalVerificationError."""
        pending_record = DurableApprovalRecord(
            **{**self.valid_record.to_dict(), "status": ApprovalStatus.PENDING.value}
        )
        with self.assertRaises(ApprovalVerificationError):
            WorkflowApprovalVerifier.verify(
                record=pending_record,
                expected_workflow_id=self.workflow_id,
                expected_action_id=self.action_id,
                expected_definition=WorkflowDefinitionRef("customer_account_remediation", "1.0.0"),
                capability=self.capability,
                arguments=self.args,
                actor=self.actor,
            )

    def test_verify_rejects_workflow_mismatch(self) -> None:
        """Workflow binding: Reusing approval in a different workflow raises ApprovalMismatchError."""
        with self.assertRaises(ApprovalMismatchError):
            WorkflowApprovalVerifier.verify(
                record=self.valid_record,
                expected_workflow_id="wf-DIFFERENT-999",
                expected_action_id=self.action_id,
                expected_definition=WorkflowDefinitionRef("customer_account_remediation", "1.0.0"),
                capability=self.capability,
                arguments=self.args,
                actor=self.actor,
            )

    def test_verify_rejects_action_id_mismatch(self) -> None:
        """Action ID binding: Reusing approval for a different action raises ApprovalMismatchError."""
        with self.assertRaises(ApprovalMismatchError):
            WorkflowApprovalVerifier.verify(
                record=self.valid_record,
                expected_workflow_id=self.workflow_id,
                expected_action_id="act-wf-test-001-mutation-OTHER",
                expected_definition=WorkflowDefinitionRef("def-1", "1.0.0"),
                capability=self.capability,
                arguments=self.args,
                actor=self.actor,
            )

    def test_verify_rejects_capability_mismatch(self) -> None:
        """Capability binding: Reusing approval for a different capability raises ApprovalMismatchError."""
        other_cap = CapabilityMetadata(
            name="close_customer_account",
            description="Close customer account permanently.",
            input_schema={"type": "object"},
            side_effect_level=SideEffectLevel.STATE_MUTATING,
        )
        with self.assertRaises(ApprovalMismatchError):
            WorkflowApprovalVerifier.verify(
                record=self.valid_record,
                expected_workflow_id=self.workflow_id,
                expected_action_id=self.action_id,
                expected_definition=WorkflowDefinitionRef("customer_account_remediation", "1.0.0"),
                capability=other_cap,
                arguments=self.args,
                actor=self.actor,
            )

    def test_verify_rejects_argument_tampering_or_substitution(self) -> None:
        """Argument binding: Changing amounts (e.g. 2500 -> 250000) raises ApprovalMismatchError."""
        tampered_args = {"customer_id": "cust-99", "amount": 250000}
        with self.assertRaises(ApprovalMismatchError):
            WorkflowApprovalVerifier.verify(
                record=self.valid_record,
                expected_workflow_id=self.workflow_id,
                expected_action_id=self.action_id,
                expected_definition=WorkflowDefinitionRef("customer_account_remediation", "1.0.0"),
                capability=self.capability,
                arguments=tampered_args,
                actor=self.actor,
            )

    def test_verify_rejects_actor_impersonation(self) -> None:
        """Actor binding: Different actor_id or role raises ApprovalVerificationError."""
        impostor = AgentActor(actor_id="operator_eve", role="support_tier_2")
        with self.assertRaises(ApprovalVerificationError):
            WorkflowApprovalVerifier.verify(
                record=self.valid_record,
                expected_workflow_id=self.workflow_id,
                expected_action_id=self.action_id,
                expected_definition=WorkflowDefinitionRef("customer_account_remediation", "1.0.0"),
                capability=self.capability,
                arguments=self.args,
                actor=impostor,
            )

    def test_verify_rejects_expired_approval(self) -> None:
        """Expiration check: Past timestamp raises ApprovalVerificationError."""
        expired_record = DurableApprovalRecord(
            **{**self.valid_record.to_dict(), "expires_at": time.time() - 10}
        )
        with self.assertRaises(ApprovalVerificationError):
            WorkflowApprovalVerifier.verify(
                record=expired_record,
                expected_workflow_id=self.workflow_id,
                expected_action_id=self.action_id,
                expected_definition=WorkflowDefinitionRef("customer_account_remediation", "1.0.0"),
                capability=self.capability,
                arguments=self.args,
                actor=self.actor,
            )


class TestWorkflowApprovalAdapter(unittest.IsolatedAsyncioTestCase):
    """Verify that WorkflowApprovalAdapter cleanly implements Phase 6 ApprovalPort."""

    def test_current_actor_lookup_fails_closed(self) -> None:
        for resolver in (None, {}.get, {"bob": AgentActor("another_actor", "admin")}.get):
            with self.subTest(resolver=resolver), self.assertRaises(PermissionError):
                resolve_current_actor("bob", resolver)

    def test_current_actor_lookup_returns_fresh_permissions(self) -> None:
        current = AgentActor("bob", "senior_agent", ["current_permission"])
        self.assertEqual(
            resolve_current_actor("bob", {"bob": current}.get).permissions, ["current_permission"]
        )

    async def test_adapter_approves_when_verifier_passes(self) -> None:
        workflow_id = "wf-adapt-001"
        action_id = "act-12345"
        actor = AgentActor(actor_id="operator_bob", role="support_tier_2")
        capability = CapabilityMetadata(
            name="apply_fee_credit",
            description="Fee credit",
            input_schema={"type": "object"},
            side_effect_level=SideEffectLevel.STATE_MUTATING,
        )
        args = {"amount": 1000}
        rec = DurableApprovalRecord(
            approval_id="app-1",
            workflow_id=workflow_id,
            workflow_definition_id="def-1",
            workflow_definition_version="1.0.0",
            action_id=action_id,
            capability_name="apply_fee_credit",
            canonical_arguments_json=canonicalize_arguments(args),
            actor_id=actor.actor_id,
            actor_role=actor.role,
            status=ApprovalStatus.APPROVED,
            approver_id="mgr-1",
        )

        adapter = WorkflowApprovalAdapter(
            approval_record=rec,
            expected_workflow_id=workflow_id,
            expected_action_id=action_id,
            expected_definition=WorkflowDefinitionRef("def-1", "1.0.0"),
        )

        decision = await adapter.request_approval(actor, capability, args)
        self.assertTrue(decision.approved)
        self.assertEqual(decision.approver, "mgr-1")

    async def test_adapter_rejects_when_verifier_fails(self) -> None:
        workflow_id = "wf-adapt-001"
        action_id = "act-12345"
        actor = AgentActor(actor_id="operator_bob", role="support_tier_2")
        capability = CapabilityMetadata(
            name="apply_fee_credit",
            description="Fee credit",
            input_schema={"type": "object"},
            side_effect_level=SideEffectLevel.STATE_MUTATING,
        )
        args = {"amount": 1000}
        rec = DurableApprovalRecord(
            approval_id="app-1",
            workflow_id=workflow_id,
            workflow_definition_id="def-1",
            workflow_definition_version="1.0.0",
            action_id=action_id,
            capability_name="apply_fee_credit",
            canonical_arguments_json=canonicalize_arguments(args),
            actor_id=actor.actor_id,
            actor_role=actor.role,
            status=ApprovalStatus.APPROVED,
            approver_id="mgr-1",
        )

        adapter = WorkflowApprovalAdapter(
            approval_record=rec,
            expected_workflow_id=workflow_id,
            expected_action_id=action_id,
            expected_definition=WorkflowDefinitionRef("def-1", "1.0.0"),
        )

        # Pass altered arguments
        decision = await adapter.request_approval(actor, capability, {"amount": 99999})
        self.assertFalse(decision.approved)
        self.assertIn("Approval verification rejected", decision.reason)


if __name__ == "__main__":
    unittest.main()
