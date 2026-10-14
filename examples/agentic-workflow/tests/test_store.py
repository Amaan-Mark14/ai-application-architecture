"""Unit tests for SQLiteWorkflowStore persistence, concurrency, and integrity."""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
BUILDING_BLOCKS_DIR = REPO_ROOT / "building-blocks" / "python"
AGENT_EXEC_DIR = REPO_ROOT / "examples" / "agent-execution"
WORKFLOW_DIR = Path(__file__).resolve().parent.parent

for p in (BUILDING_BLOCKS_DIR, AGENT_EXEC_DIR, WORKFLOW_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from contracts.agent import ExecutionReceipt
from contracts.workflow import (
    ApprovalStatus,
    MutationStatus,
    WorkflowDefinitionRef,
    WorkflowStatus,
)
from workflow.approval import DurableApprovalRecord
from workflow.audit import WorkflowAuditEvent
from workflow.errors import CorruptedCheckpointError
from workflow.ledger import DurableMutationRecord
from workflow.state import WorkflowActorSnapshot, WorkflowState
from workflow.store import SQLiteWorkflowStore


class TestSQLiteWorkflowStore(unittest.TestCase):
    """Verify SQLite persistence, WAL journaling, optimistic CAS, and checksum enforcement."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_workflow.db")
        self.store = SQLiteWorkflowStore(self.db_path)

        self.actor = WorkflowActorSnapshot(actor_id="test_actor", role="operator")
        self.definition_ref = WorkflowDefinitionRef(definition_id="test_def", version="1.0.0")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_wal_mode_enabled(self) -> None:
        conn = sqlite3.connect(self.db_path)
        cursor = conn.execute("PRAGMA journal_mode;")
        mode = cursor.fetchone()[0]
        conn.close()
        self.assertEqual(mode.lower(), "wal")

    def test_transaction_commits_and_closes_connection(self) -> None:
        with self.store._get_connection() as connection:
            connection.execute("CREATE TABLE close_probe (value TEXT)")
            connection.execute("INSERT INTO close_probe VALUES ('committed')")
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
        with self.store._get_connection() as reopened:
            self.assertEqual(
                reopened.execute("SELECT value FROM close_probe").fetchone()[0], "committed"
            )

    def test_failed_transaction_rolls_back_and_closes_connection(self) -> None:
        with self.store._get_connection() as setup:
            setup.execute("CREATE TABLE rollback_probe (value TEXT)")
        with (
            self.assertRaisesRegex(RuntimeError, "Interrupted write"),
            self.store._get_connection() as connection,
        ):
            connection.execute("INSERT INTO rollback_probe VALUES ('must roll back')")
            raise RuntimeError("Interrupted write")
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
        with self.store._get_connection() as reopened:
            self.assertEqual(
                reopened.execute("SELECT count(*) FROM rollback_probe").fetchone()[0], 0
            )
        # Windows must be able to unlink the database while the store object is still alive.
        os.unlink(self.db_path)

    def test_save_and_load_workflow_state(self) -> None:
        state = WorkflowState(
            workflow_id="wf-100",
            definition=self.definition_ref,
            status=WorkflowStatus.RUNNING,
            current_step="step_1",
            initiator_actor=self.actor,
            domain_context={"key": "value", "count": 10},
        )
        self.store.save_state(state)

        loaded = self.store.load_state("wf-100")
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.workflow_id, "wf-100")
        self.assertEqual(loaded.status, WorkflowStatus.RUNNING)
        self.assertEqual(loaded.current_step, "step_1")
        self.assertEqual(loaded.domain_context["count"], 10)
        self.assertEqual(loaded.compute_checksum(), loaded.checksum)

    def test_checksum_corruption_detection(self) -> None:
        state = WorkflowState(
            workflow_id="wf-corrupt",
            definition=self.definition_ref,
            status=WorkflowStatus.RUNNING,
            current_step="step_1",
            initiator_actor=self.actor,
        )
        self.store.save_state(state)

        # Directly tamper with SQLite state_json without updating checksum
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            'UPDATE workflow_checkpoints SET state_json = \'{"workflow_id": "wf-corrupt", "tampered": true}\' '
            "WHERE workflow_id = 'wf-corrupt';"
        )
        conn.commit()
        conn.close()

        with self.assertRaises(CorruptedCheckpointError):
            self.store.load_state("wf-corrupt")

    def test_compare_and_swap_resume(self) -> None:
        state = WorkflowState(
            workflow_id="wf-cas",
            definition=self.definition_ref,
            status=WorkflowStatus.AWAITING_APPROVAL,
            current_step="human_approval",
            initiator_actor=self.actor,
            checkpoint_version=3,
        )
        self.store.save_state(state)

        # 1. Matching version and status -> True
        success = self.store.compare_and_swap_resume("wf-cas", expected_version=3, new_version=4)
        self.assertTrue(success)

        resumed = self.store.load_state("wf-cas")
        assert resumed is not None
        self.assertEqual(resumed.checkpoint_version, 4)
        self.assertEqual(resumed.status, WorkflowStatus.RUNNING)

        # 2. Competing CAS with old expected_version=3 -> False
        fail_old = self.store.compare_and_swap_resume("wf-cas", expected_version=3, new_version=5)
        self.assertFalse(fail_old)

        # 3. Competing CAS when status is already RUNNING -> False
        fail_status = self.store.compare_and_swap_resume(
            "wf-cas", expected_version=4, new_version=5
        )
        self.assertFalse(fail_status)

    def test_approval_record_persistence(self) -> None:
        rec = DurableApprovalRecord(
            approval_id="app-1",
            workflow_id="wf-1",
            workflow_definition_id="def-1",
            workflow_definition_version="1.0",
            action_id="act-1",
            capability_name="apply_fee_credit",
            canonical_arguments_json='{"amount": 2000}',
            actor_id="alice",
            actor_role="support",
            status=ApprovalStatus.PENDING,
        )
        self.store.save_approval(rec)

        loaded_by_id = self.store.load_approval("app-1")
        self.assertIsNotNone(loaded_by_id)
        assert loaded_by_id is not None
        self.assertEqual(loaded_by_id.approval_id, "app-1")
        self.assertEqual(loaded_by_id.status, ApprovalStatus.PENDING)

        loaded_by_action = self.store.load_approval_by_action_id("act-1")
        self.assertIsNotNone(loaded_by_action)
        assert loaded_by_action is not None
        self.assertEqual(loaded_by_action.approval_id, "app-1")

    def test_atomic_commit_mutation_and_approval(self) -> None:
        receipt = ExecutionReceipt(
            action_id="act-mut-1",
            capability_name="apply_fee_credit",
            status="succeeded",
            result_summary="Fee credited",
        )
        mutation = DurableMutationRecord(
            action_id="act-mut-1",
            workflow_id="wf-atomic",
            step_name="mutation_step",
            capability_name="apply_fee_credit",
            canonical_args='{"amount": 1500}',
            status=MutationStatus.EXECUTED,
            execution_receipt=receipt,
        )
        approval = DurableApprovalRecord(
            approval_id="app-atomic",
            workflow_id="wf-atomic",
            workflow_definition_id="def-1",
            workflow_definition_version="1.0",
            action_id="act-mut-1",
            capability_name="apply_fee_credit",
            canonical_arguments_json='{"amount": 1500}',
            actor_id="alice",
            actor_role="support",
            status=ApprovalStatus.CONSUMED,
            consumed_at=1000.0,
            execution_receipt_id="act-mut-1",
        )

        self.store.atomic_commit_mutation_and_approval(mutation, approval)

        loaded_mut = self.store.load_mutation("act-mut-1")
        loaded_app = self.store.load_approval("app-atomic")

        self.assertIsNotNone(loaded_mut)
        self.assertIsNotNone(loaded_app)
        assert loaded_mut is not None and loaded_app is not None

        self.assertEqual(loaded_mut.status, MutationStatus.EXECUTED)
        self.assertEqual(loaded_app.status, ApprovalStatus.CONSUMED)
        self.assertEqual(loaded_app.execution_receipt_id, "act-mut-1")

    def test_audit_log_append_and_retrieve(self) -> None:
        evt1 = WorkflowAuditEvent(
            event_id="evt-1",
            workflow_id="wf-audit",
            event_type="workflow_started",
            step_name="step_a",
            details={"foo": "bar"},
        )
        evt2 = WorkflowAuditEvent(
            event_id="evt-2",
            workflow_id="wf-audit",
            event_type="step_completed",
            step_name="step_a",
        )
        self.store.append_audit_event(evt1)
        self.store.append_audit_event(evt2)

        events = self.store.load_audit_events("wf-audit")
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].event_type, "workflow_started")
        self.assertEqual(events[1].event_type, "step_completed")


if __name__ == "__main__":
    unittest.main()
