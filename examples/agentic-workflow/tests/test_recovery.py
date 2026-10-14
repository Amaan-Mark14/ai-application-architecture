"""Real SQLite and process-boundary regressions for durable recovery and compensation."""

import asyncio
import runpy
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / "examples/agentic-workflow"
for folder in (ROOT / "building-blocks/python", ROOT / "examples/agent-execution", WORKFLOW):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

from agent.capabilities import ApplyFeeCreditCapability
from contracts.workflow import ApprovalStatus, MutationStatus, WorkflowStatus
from workflow.errors import ConcurrentResumeConflictError
from workflow.state import WorkflowActorSnapshot
from workflow.store import execution_guard

get_components = runpy.run_path(str(WORKFLOW / "demo.py"))["get_components"]


class SimulatedProcessDeath(BaseException):
    """Bypass ordinary application exception handling at the physical tool boundary."""


class TestDurableRecovery(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.db = str(Path(self.directory.name) / "recovery.db")
        self.engine, self.business = get_components(self.db, "Dispute duplicate fee")

    def tearDown(self) -> None:
        self.directory.cleanup()

    async def _pause(self, workflow_id: str = "recover-credit"):
        return await self.engine.start_workflow(
            workflow_id,
            WorkflowActorSnapshot("operator_bob", "senior_agent"),
            {"customer_id": "cust-001", "inquiry": "Dispute duplicate fee"},
        )

    def test_business_state_survives_restart_and_replay_is_exact(self) -> None:
        self.business.apply_credit("cust-001", 1000, "Same reason", action_id="credit-one")
        _, fresh = get_components(self.db)
        self.assertEqual(fresh.get_account("cust-001").balance_cents, 16000)
        fresh.apply_credit("cust-001", 1000, "Same reason", action_id="credit-one")
        self.assertEqual(fresh.get_account("cust-001").balance_cents, 16000)
        args = {"customer_id": "cust-001", "amount_cents": 1000, "reason": "Same reason"}
        self.assertEqual(
            fresh.operation_status("another-action", "apply_fee_credit", args), "not_executed"
        )
        with self.assertRaises(ValueError):
            fresh.apply_credit("cust-001", 2000, "Same reason", action_id="credit-one")
        fresh.add_note("cust-001", "Same note", action_id="note-one")
        _, restarted = get_components(self.db)
        self.assertEqual(restarted.get_account("cust-001").notes.count("Same note"), 1)
        self.assertEqual(
            restarted.operation_status(
                "note-two", "update_customer_note", {"customer_id": "cust-001", "note": "Same note"}
            ),
            "not_executed",
        )

    def test_business_write_and_operation_identity_roll_back_together(self) -> None:
        with self.engine.store._get_connection() as conn:
            conn.execute(
                "CREATE TRIGGER reject_operation BEFORE INSERT ON business_operations BEGIN SELECT RAISE(ABORT, 'operation fault'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.business.apply_credit("cust-001", 1000, "Rollback", action_id="rollback")
        self.assertEqual(self.business.get_account("cust-001").balance_cents, 15000)
        self.assertEqual(
            self.business.operation_status(
                "rollback",
                "apply_fee_credit",
                {"customer_id": "cust-001", "amount_cents": 1000, "reason": "Rollback"},
            ),
            "not_executed",
        )

    async def test_approval_decision_and_checkpoint_roll_back_together(self) -> None:
        state = await self._pause()
        with self.engine.store._get_connection() as conn:
            conn.execute(
                "CREATE TRIGGER reject_approval BEFORE UPDATE ON workflow_approvals BEGIN SELECT RAISE(ABORT, 'approval fault'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            await self.engine.resume_workflow(state.workflow_id, "approved", "manager")
        self.assertEqual(
            self.engine.store.load_state(state.workflow_id).status, WorkflowStatus.AWAITING_APPROVAL
        )
        self.assertEqual(
            self.engine.store.load_approval(state.domain_context["pending_approval_id"]).status,
            ApprovalStatus.PENDING,
        )
        self.assertEqual(self.business.get_account("cust-001").balance_cents, 15000)

    async def test_death_after_physical_credit_recovers_without_duplicate(self) -> None:
        state = await self._pause()

        class CrashAfterCredit(ApplyFeeCreditCapability):
            async def execute(self, arguments, context=None):
                await super().execute(arguments, context)
                raise SimulatedProcessDeath()

        step = self.engine.definition.steps["mutation_execution"].handler.__self__
        step.capability = CrashAfterCredit(self.business)
        with self.assertRaises(SimulatedProcessDeath):
            await self.engine.resume_workflow(state.workflow_id, "approved", "manager")
        stranded = self.engine.store.load_state(state.workflow_id)
        self.assertEqual(stranded.status, WorkflowStatus.RUNNING)
        self.assertEqual(self.business.get_account("cust-001").balance_cents, 17500)
        self.assertEqual(
            self.engine.store.load_mutation(state.domain_context["stable_action_id"]).status,
            MutationStatus.EXECUTION_STARTED,
        )
        fresh_engine, fresh_business = get_components(self.db)
        final = await fresh_engine.recover_workflow(state.workflow_id)
        self.assertEqual(final.status, WorkflowStatus.COMPLETED)
        self.assertTrue(final.domain_context["reconciled_after_restart"])
        self.assertEqual(fresh_business.get_account("cust-001").balance_cents, 17500)
        self.assertEqual(fresh_business.get_account("cust-001").credits_applied_cents, 2500)
        self.assertEqual(
            fresh_engine.store.load_approval(state.domain_context["pending_approval_id"]).status,
            ApprovalStatus.CONSUMED,
        )
        with self.assertRaises(ConcurrentResumeConflictError):
            await fresh_engine.recover_workflow(state.workflow_id)

    async def test_credit_failure_compensates_an_actual_preceding_note(self) -> None:
        state = await self._pause()

        class RejectCredit(ApplyFeeCreditCapability):
            async def execute(self, arguments, context=None):
                return {"success": False, "error": "Bank rejected credit"}

        self.engine.definition.steps[
            "mutation_execution"
        ].handler.__self__.capability = RejectCredit(self.business)
        final = await self.engine.resume_workflow(state.workflow_id, "approved", "manager")
        self.assertEqual(final.status, WorkflowStatus.FAILED)
        self.assertTrue(final.domain_context["compensation_applied"])
        prior = self.engine.store.load_mutation(final.domain_context["prior_note_action_id"])
        self.assertEqual(prior.step_name, "concession_note")
        self.assertEqual(prior.status, MutationStatus.EXECUTED)
        self.assertEqual(self.business.get_account("cust-001").balance_cents, 15000)
        notes = self.business.get_account("cust-001").notes
        self.assertEqual(sum(note.startswith("Concession pending") for note in notes), 1)
        self.assertEqual(sum(note.startswith("COMPENSATION:") for note in notes), 1)

    async def test_actual_process_death_after_credit_recovers_once(self) -> None:
        state = await self._pause("os-death-credit")
        paths = [
            str(ROOT / "building-blocks/python"),
            str(ROOT / "examples/agent-execution"),
            str(WORKFLOW),
        ]
        source = "import sys,os,asyncio,runpy;sys.path[:0]=" + repr(paths) + "\n"
        source += "from agent.capabilities import ApplyFeeCreditCapability\n"
        source += (
            "get_components=runpy.run_path("
            + repr(str(WORKFLOW / "demo.py"))
            + ')["get_components"]\n'
        )
        source += "class CrashCredit(ApplyFeeCreditCapability):\n async def execute(self,args,context=None):\n  await super().execute(args,context)\n  os._exit(17)\n"
        source += "engine,business=get_components(" + repr(self.db) + ")\n"
        source += "engine.definition.steps['mutation_execution'].handler.__self__.capability=CrashCredit(business)\n"
        source += "asyncio.run(engine.resume_workflow('os-death-credit','approved','manager'))\n"
        child = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-c", source],
            capture_output=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(child.returncode, 17, child.stderr)
        self.assertEqual(
            self.engine.store.load_state(state.workflow_id).status, WorkflowStatus.RUNNING
        )
        fresh, business = get_components(self.db)
        result = await fresh.recover_workflow(state.workflow_id)
        self.assertEqual(result.status, WorkflowStatus.COMPLETED)
        self.assertEqual(business.get_account("cust-001").credits_applied_cents, 2500)
        self.assertTrue(result.domain_context["reconciled_after_restart"])

    async def test_arbitrary_mutating_step_cannot_be_blindly_recovered(self) -> None:
        state = await self._pause("unsafe-recovery")
        step = self.engine.definition.steps["mutation_execution"]
        self.engine.definition.steps[step.name] = replace(step, supports_recovery=False)
        self.engine.store.save_state(
            replace(state, status=WorkflowStatus.RUNNING, current_step=step.name)
        )
        from workflow.errors import InvalidTransitionError

        with self.assertRaises(InvalidTransitionError):
            await self.engine.recover_workflow(state.workflow_id)
        self.assertEqual(self.business.get_account("cust-001").credits_applied_cents, 0)

    def test_process_ownership_blocks_live_executor_and_releases_on_process_death(self) -> None:
        source = (
            "import os,sys;sys.path[:0]="
            + repr(
                [
                    str(ROOT / "building-blocks/python"),
                    str(ROOT / "examples/agent-execution"),
                    str(WORKFLOW),
                ]
            )
            + ";"
            "from workflow.store import SQLiteWorkflowStore,execution_guard;"
            "store=SQLiteWorkflowStore(" + repr(self.db) + ");\n"
            "with execution_guard(store,'owned'):\n os._exit(0)\n"
        )
        with execution_guard(self.engine.store, "owned"):
            child = subprocess.run(
                [sys.executable, "-c", source], capture_output=True, timeout=10, check=False
            )
            self.assertNotEqual(child.returncode, 0)
            self.assertIn(b"active executor", child.stderr)
        child = subprocess.run(
            [sys.executable, "-c", source], capture_output=True, timeout=10, check=False
        )
        self.assertEqual(child.returncode, 0, child.stderr)
        with execution_guard(self.engine.store, "owned"):
            pass
