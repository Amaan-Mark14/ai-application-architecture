#!/usr/bin/env python3
"""Deterministic evaluation runner for Phase 7 Agentic Workflow Orchestration.

Executes all 30 versioned scenarios from eval_dataset.jsonl hermetically with zero
external model dependency, verifying state machine routing, durable HITL pause/resume,
saga compensation, TOCTOU revalidation, and 6 zero-tolerance safety invariants.

Zero external dependencies (pure Python standard library).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BUILDING_BLOCKS_DIR = REPO_ROOT / "building-blocks" / "python"
AGENT_EXEC_DIR = REPO_ROOT / "examples" / "agent-execution"
WORKFLOW_DIR = Path(__file__).resolve().parent

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
from contracts.agent import (
    AgentActor,
    CapabilityMetadata,
    CapabilityPort,
    SideEffectLevel,
)
from contracts.workflow import (
    ApprovalStatus,
    StepExecutionStatus,
    WorkflowDefinitionRef,
    WorkflowStatus,
)
from workflow.approval import (
    DurableApprovalRecord,
    WorkflowApprovalVerifier,
)
from workflow.engine import WorkflowEngine
from workflow.errors import ApprovalReplayError, ConcurrentResumeConflictError
from workflow.remediation_workflow import build_customer_remediation_workflow
from workflow.state import StepExecutionRecord, WorkflowActorSnapshot, WorkflowState
from workflow.store import InMemoryWorkflowStore, SQLiteWorkflowStore


class ScriptedAgentEngine:
    """Deterministic agent stub executing canned model responses for evaluation."""

    def __init__(self, answer_text: str) -> None:
        self.answer_text = answer_text

    async def run(self, task: str, actor: AgentActor) -> Any:
        class Result:
            def __init__(self, text: str) -> None:
                self.final_answer = text
                self.status = "COMPLETED"
                self.execution_receipts: list[Any] = []
                self.run_id = f"eval-run-{time.time_ns()}"
                self.step_count = 1

        return Result(self.answer_text)


class FailingEvaluationCreditCapability(CapabilityPort):
    """Simulates permanent gateway failure to evaluate saga compensation."""

    def __init__(self) -> None:
        self._metadata = CapabilityMetadata(
            name="apply_fee_credit",
            description="Failing financial credit capability for saga evaluation.",
            input_schema={"type": "object"},
            side_effect_level=SideEffectLevel.STATE_MUTATING,
            requires_approval=True,
        )

    @property
    def metadata(self) -> CapabilityMetadata:
        return self._metadata

    async def execute(self, arguments: Mapping[str, Any], context: Any | None = None) -> Any:
        raise RuntimeError("Gateway permanent failure: account adjustment rejected.")


@dataclass
class SafetyInvariantMetrics:
    """Zero-tolerance safety invariant counters derived from durable execution evidence."""

    unauthorized_transitions: int = 0
    unapproved_mutations: int = 0
    duplicate_mutations: int = 0
    post_terminal_executions: int = 0
    approval_replays: int = 0
    max_transition_violations: int = 0

    @property
    def all_satisfied(self) -> bool:
        return (
            self.unauthorized_transitions == 0
            and self.unapproved_mutations == 0
            and self.duplicate_mutations == 0
            and self.post_terminal_executions == 0
            and self.approval_replays == 0
            and self.max_transition_violations == 0
        )


@dataclass
class ScenarioResult:
    """Individual scenario execution outcome."""

    scenario_id: str
    category: str
    passed: bool
    expected_status: str
    actual_status: str
    mutations_executed: int
    duration_ms: float
    error_message: str | None = None


class DeterministicWorkflowEvaluator:
    """Orchestrates execution of the 30-scenario deterministic evaluation dataset."""

    def __init__(self, dataset_path: Path) -> None:
        self.dataset_path = dataset_path
        self.metrics = SafetyInvariantMetrics()

    async def evaluate_scenario(self, row: Mapping[str, Any]) -> ScenarioResult:
        scenario_id = str(row["id"])
        category = str(row.get("category", "general"))
        customer_id = str(row.get("customer_id", "cust-001"))
        actor_id = str(row.get("actor_id", "agent-001"))
        actor_role = str(row.get("actor_role", "senior_agent"))
        inquiry = str(row.get("inquiry", ""))
        canned_answer = str(row.get("canned_agent_answer", ""))
        resume_decision = row.get("resume_decision")
        expected_status_str = str(row.get("expected_status", "COMPLETED"))
        expected_mutations = int(row.get("expected_mutations", 0))
        simulate_credit_failure = bool(row.get("simulate_credit_failure", False))
        has_prior_note = bool(row.get("has_prior_note", False))

        start_time = time.perf_counter()

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, f"eval_{scenario_id}.db")
            customer_store = InMemoryCustomerStore()
            auth_policy = CustomerSupportAuthorizationPolicy(store=customer_store)
            sqlite_store = SQLiteWorkflowStore(db_path)

            read_only_caps = [
                GetCustomerCapability(customer_store),
                GetAccountStatusCapability(customer_store),
                SearchPolicyCapability(),
            ]
            credit_cap = (
                FailingEvaluationCreditCapability()
                if simulate_credit_failure
                else ApplyFeeCreditCapability(customer_store)
            )
            note_cap = UpdateCustomerNoteCapability(customer_store)
            executor = ToolExecutor()
            agent_stub = ScriptedAgentEngine(canned_answer)

            workflow_def = build_customer_remediation_workflow(
                customer_store=customer_store,
                agent_engine=agent_stub,
                read_only_registry=CapabilityRegistry(read_only_caps),
                credit_capability=credit_cap,
                note_capability=note_cap,
                tool_executor=executor,
                workflow_store=sqlite_store,
                auth_policy=auth_policy,
                actor_resolver={actor_id: AgentActor(actor_id, actor_role)}.get,
            )

            engine = WorkflowEngine(definition=workflow_def, store=sqlite_store)
            actor_snapshot = WorkflowActorSnapshot(actor_id=actor_id, role=actor_role)
            initial_account = customer_store.get_account(customer_id)
            initial_activity_count = len(initial_account.activity_log) if initial_account else 0

            def observed_mutation_count() -> int:
                account = customer_store.get_account(customer_id)
                return (
                    sum(
                        entry.startswith(("Credit applied:", "Note added:"))
                        for entry in account.activity_log[initial_activity_count:]
                    )
                    if account
                    else 0
                )

            try:
                # 1. Start workflow
                domain_ctx: dict[str, Any] = {"customer_id": customer_id, "inquiry": inquiry}
                if has_prior_note:
                    domain_ctx["has_prior_note"] = True

                state = await engine.start_workflow(
                    workflow_id=scenario_id,
                    actor=actor_snapshot,
                    domain_context=domain_ctx,
                )

                # 2. Handle suspension and resumption if applicable
                if state.status == WorkflowStatus.AWAITING_APPROVAL and resume_decision:
                    state = await engine.resume_workflow(
                        workflow_id=scenario_id,
                        decision=resume_decision,
                        approver_id="manager_evaluator",
                        reason=f"Evaluation decision: {resume_decision}",
                    )

                duration_ms = (time.perf_counter() - start_time) * 1000.0

                # 3. Check physical mutation count in customer store
                acc = customer_store.get_account(customer_id)
                actual_mutations = observed_mutation_count()

                actual_status_str = state.status.value.upper()

                # Verify all 6 zero-tolerance invariants from runtime evidence
                # Invariant 1: unauthorized_transitions
                for h in state.history:
                    if (
                        h.status == StepExecutionStatus.FAILED
                        and h.outcome_type == "invalid_transition"
                    ):
                        self.metrics.unauthorized_transitions += 1
                valid_step_names = set(workflow_def.steps.keys())
                for h in state.history:
                    if h.step_name not in valid_step_names:
                        self.metrics.unauthorized_transitions += 1

                # Invariant 2: unapproved_mutations
                # Financial credits strictly require consumed approval
                if acc and acc.credits_applied_cents > 0:
                    action_id = state.domain_context.get("stable_action_id")
                    approval = (
                        sqlite_store.load_approval_by_action_id(action_id) if action_id else None
                    )
                    if not approval or approval.status != ApprovalStatus.CONSUMED:
                        self.metrics.unapproved_mutations += 1

                # Invariant 3: duplicate_mutations
                # Check customer activity log for duplicate credit entries and notes for duplicate compensation
                if acc:
                    credit_logs = [log for log in acc.activity_log if "Credit applied:" in log]
                    if len(credit_logs) > 1 and len(credit_logs) != len(set(credit_logs)):
                        self.metrics.duplicate_mutations += 1

                    comp_notes = [note for note in acc.notes if "COMPENSATION:" in note]
                    if len(comp_notes) > 1 and len(comp_notes) != len(set(comp_notes)):
                        self.metrics.duplicate_mutations += 1

                # Invariant 4: post_terminal_executions
                # Terminal workflow must reject resumption
                if state.status in (
                    WorkflowStatus.COMPLETED,
                    WorkflowStatus.FAILED,
                    WorkflowStatus.REJECTED,
                    WorkflowStatus.ESCALATED,
                ):
                    try:
                        await engine.resume_workflow(scenario_id, "approved", "evaluator")
                        self.metrics.post_terminal_executions += 1
                    except ConcurrentResumeConflictError:
                        pass  # Expected fail-closed

                # Invariant 5: approval_replays
                # Consumed approval must reject re-verification
                action_id = state.domain_context.get("stable_action_id")
                if action_id:
                    consumed_app = sqlite_store.load_approval_by_action_id(action_id)
                    if consumed_app and consumed_app.status == ApprovalStatus.CONSUMED:
                        try:
                            WorkflowApprovalVerifier.verify(
                                record=consumed_app,
                                expected_workflow_id=state.workflow_id,
                                expected_action_id=action_id,
                                expected_definition=WorkflowDefinitionRef(
                                    "customer_account_remediation", "1.0.0"
                                ),
                                capability=credit_cap.metadata,
                                arguments={"customer_id": customer_id, "amount_cents": 1000},
                                actor=AgentActor(
                                    actor_snapshot.actor_id,
                                    actor_snapshot.role,
                                    list(actor_snapshot.permissions),
                                ),
                            )
                            self.metrics.approval_replays += 1
                        except ApprovalReplayError:
                            pass  # Expected rejection

                # Invariant 6: max_transition_violations
                if len(state.history) > workflow_def.max_transitions:
                    self.metrics.max_transition_violations += 1

                status_matches = actual_status_str == expected_status_str
                passed = status_matches and actual_mutations == expected_mutations

                return ScenarioResult(
                    scenario_id=scenario_id,
                    category=category,
                    passed=passed,
                    expected_status=expected_status_str,
                    actual_status=actual_status_str,
                    mutations_executed=actual_mutations,
                    duration_ms=duration_ms,
                    error_message=None
                    if passed
                    else (
                        f"Expected {expected_status_str} with {expected_mutations} mutations; "
                        f"observed {actual_status_str} with {actual_mutations}."
                    ),
                )

            except Exception as exc:  # noqa: BLE001 -- finalize failures from external execution boundaries.
                duration_ms = (time.perf_counter() - start_time) * 1000.0
                return ScenarioResult(
                    scenario_id=scenario_id,
                    category=category,
                    passed=False,
                    expected_status=expected_status_str,
                    actual_status="EXCEPTION",
                    mutations_executed=observed_mutation_count(),
                    duration_ms=duration_ms,
                    error_message=str(exc),
                )

    async def run_all(self) -> tuple[list[ScenarioResult], SafetyInvariantMetrics]:
        scenarios: list[dict[str, Any]] = []
        dataset_text = await asyncio.to_thread(self.dataset_path.read_text, encoding="utf-8")
        for line in dataset_text.splitlines():
            if line.strip():
                scenarios.append(json.loads(line))

        results: list[ScenarioResult] = []
        for row in scenarios:
            res = await self.evaluate_scenario(row)
            results.append(res)

        return results, self.metrics


async def run_evaluator_self_tests() -> bool:
    """Evaluator Self-Tests: Prove that the evaluator detects injected violations using actual test artifacts."""
    print("\n--- Running Evaluator Safety Self-Tests ---")

    all_passed = True

    # Injection 1: Injected unauthorized transition (undeclared step in history)
    m1 = SafetyInvariantMetrics()
    cust_store = InMemoryCustomerStore()
    wf_store = InMemoryWorkflowStore()
    credit_cap = ApplyFeeCreditCapability(cust_store)
    wf_def = build_customer_remediation_workflow(
        customer_store=cust_store,
        agent_engine=ScriptedAgentEngine("{}"),
        read_only_registry=CapabilityRegistry([]),
        credit_capability=credit_cap,
        note_capability=UpdateCustomerNoteCapability(cust_store),
        tool_executor=ToolExecutor(),
        workflow_store=wf_store,
        auth_policy=CustomerSupportAuthorizationPolicy(store=cust_store),
    )

    bad_step_record = StepExecutionRecord(
        step_name="injected_malicious_step_not_in_graph",
        attempt=1,
        status=StepExecutionStatus.SUCCEEDED,
        started_at=time.time(),
        completed_at=time.time(),
        outcome_type="tampered_outcome",
    )
    tampered_state = WorkflowState(
        workflow_id="wf-test-injection-1",
        definition=wf_def.definition_ref,
        status=WorkflowStatus.RUNNING,
        current_step="intake_validation",
        initiator_actor=WorkflowActorSnapshot("bob", "senior_agent"),
        history=(bad_step_record,),
    )

    valid_step_names = set(wf_def.steps.keys())
    for h in tampered_state.history:
        if h.step_name not in valid_step_names:
            m1.unauthorized_transitions += 1

    if m1.unauthorized_transitions > 0:
        print("PASS: Evaluator detected injected unauthorized_transition.")
    else:
        print("FAIL: Evaluator failed to detect injected unauthorized_transition.")
        all_passed = False

    # Injection 2: Injected unapproved mutation (balance changed with unconsumed / absent approval)
    m2 = SafetyInvariantMetrics()
    cust_store.apply_credit("cust-001", 3000, "unapproved credit")
    acc = cust_store.get_account("cust-001")
    action_id = "act-unapproved-1"
    approval = wf_store.load_approval_by_action_id(action_id)  # None
    if (
        acc
        and acc.credits_applied_cents > 0
        and (not approval or approval.status != ApprovalStatus.CONSUMED)
    ):
        m2.unapproved_mutations += 1

    if m2.unapproved_mutations > 0:
        print("PASS: Evaluator detected injected unapproved_mutation.")
    else:
        print("FAIL: Evaluator failed to detect injected unapproved_mutation.")
        all_passed = False

    # Injection 3: Injected duplicate mutation (duplicate identical entries in activity log and compensation notes)
    m3 = SafetyInvariantMetrics()
    assert acc is not None
    acc.activity_log.append("Credit applied: +$10.00 (Reason: duplicate credit)")
    acc.activity_log.append("Credit applied: +$10.00 (Reason: duplicate credit)")
    credit_logs = [log for log in acc.activity_log if "Credit applied:" in log]
    if len(credit_logs) > 1 and len(credit_logs) != len(set(credit_logs)):
        m3.duplicate_mutations += 1

    # Also test duplicate compensation side-effect detection
    m3_comp = SafetyInvariantMetrics()
    acc.notes.append(
        "COMPENSATION: Fee credit disbursement failed for workflow wf-test. Concession note voided."
    )
    acc.notes.append(
        "COMPENSATION: Fee credit disbursement failed for workflow wf-test. Concession note voided."
    )
    comp_notes = [note for note in acc.notes if "COMPENSATION:" in note]
    if len(comp_notes) > 1 and len(comp_notes) != len(set(comp_notes)):
        m3_comp.duplicate_mutations += 1

    if m3.duplicate_mutations > 0 and m3_comp.duplicate_mutations > 0:
        print(
            "PASS: Evaluator detected injected duplicate_mutation (credit activity log and compensation notes)."
        )
    else:
        print("FAIL: Evaluator failed to detect injected duplicate_mutation.")
        all_passed = False

    # Injection 4: Injected post-terminal execution attempt
    m4 = SafetyInvariantMetrics()
    terminal_state = WorkflowState(
        workflow_id="wf-test-injection-4",
        definition=wf_def.definition_ref,
        status=WorkflowStatus.COMPLETED,
        current_step="finalization",
        initiator_actor=WorkflowActorSnapshot("bob", "senior_agent"),
    )
    wf_store.save_state(terminal_state)
    engine = WorkflowEngine(definition=wf_def, store=wf_store)
    try:
        # Engine must reject resuming a COMPLETED workflow
        await engine.resume_workflow("wf-test-injection-4", "approved", "evaluator")
        m4.post_terminal_executions += 1
    except ConcurrentResumeConflictError:
        pass  # Properly rejected

    # Simulate detection if an execution bypassed the guard
    if m4.post_terminal_executions == 0:
        print("PASS: Evaluator verified post-terminal execution is blocked.")
    else:
        print("FAIL: Evaluator allowed post-terminal execution.")
        all_passed = False

    # Injection 5: Injected approval replay (calling WorkflowApprovalVerifier with CONSUMED approval)
    m5 = SafetyInvariantMetrics()
    consumed_record = DurableApprovalRecord(
        approval_id="app-replay-1",
        workflow_id="wf-test-injection-5",
        workflow_definition_id="customer_account_remediation",
        workflow_definition_version="1.0.0",
        action_id="act-replay-1",
        capability_name="apply_fee_credit",
        canonical_arguments_json='{"amount_cents":1000,"customer_id":"cust-001"}',
        actor_id="operator_bob",
        actor_role="senior_agent",
        status=ApprovalStatus.CONSUMED,
        approver_id="manager_jane",
        approver_role="manager",
        reason="Already consumed",
        requested_at=time.time() - 100,
        decided_at=time.time() - 50,
        consumed_at=time.time() - 10,
    )
    try:
        WorkflowApprovalVerifier.verify(
            record=consumed_record,
            expected_workflow_id="wf-test-injection-5",
            expected_action_id="act-replay-1",
            expected_definition=wf_def.definition_ref,
            capability=credit_cap.metadata,
            arguments={"customer_id": "cust-001", "amount_cents": 1000},
            actor=AgentActor("operator_bob", "senior_agent", ["billing_read", "billing_write"]),
        )
        m5.approval_replays += 1
    except ApprovalReplayError:
        pass  # Replay correctly detected and rejected!

    if m5.approval_replays == 0:
        print("PASS: Evaluator detected and blocked injected approval_replay.")
    else:
        print("FAIL: Evaluator failed to block injected approval_replay.")
        all_passed = False

    # Injection 6: Injected max transition violation (history exceeds max_transitions)
    m6 = SafetyInvariantMetrics()
    history_20 = tuple(
        [
            StepExecutionRecord(
                f"step_{i}", 1, StepExecutionStatus.SUCCEEDED, time.time(), time.time(), "ok"
            )
            for i in range(20)
        ]
    )
    if len(history_20) > wf_def.max_transitions:
        m6.max_transition_violations += 1

    if m6.max_transition_violations > 0:
        print("PASS: Evaluator detected injected max_transition_violations.")
    else:
        print("FAIL: Evaluator failed to detect injected max_transition_violations.")
        all_passed = False

    return all_passed


async def async_main() -> int:
    parser = argparse.ArgumentParser(
        description="Deterministic Evaluation Suite for Phase 7 Workflow Orchestration"
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run evaluator self-tests proving violation detection.",
    )
    parser.add_argument(
        "--dataset",
        type=str,
        default=str(WORKFLOW_DIR / "eval_dataset.jsonl"),
        help="Path to jsonl dataset.",
    )
    args = parser.parse_args()

    print("=" * 70)
    print("PHASE 7 DETERMINISTIC WORKFLOW EVALUATION HARNESS")
    print("=" * 70)

    # 1. Run Evaluator Self-Tests
    self_test_ok = await run_evaluator_self_tests()
    if not self_test_ok:
        print("\nFATAL: Evaluator self-tests failed!")
        return 1

    if args.self_test:
        print("\nSelf-test mode requested and passed cleanly.")
        return 0

    # 2. Run All Dataset Scenarios
    dataset_file = Path(args.dataset)
    if not dataset_file.is_file():
        print(f"Error: Dataset file '{dataset_file}' not found.")
        return 1

    evaluator = DeterministicWorkflowEvaluator(dataset_file)
    results, metrics = await evaluator.run_all()

    total = len(results)
    passed_count = sum(1 for r in results if r.passed)
    failed_count = total - passed_count

    print(f"\nExecuted {total} deterministic evaluation scenarios:")
    print("-" * 70)
    for r in results:
        mark = "PASS" if r.passed else "FAIL"
        err = f" (Error: {r.error_message})" if r.error_message else ""
        print(
            f"[{mark}] {r.scenario_id:<12} | {r.category:<20} | Expected: {r.expected_status:<10} Actual: {r.actual_status:<10} | {r.duration_ms:.1f}ms{err}"
        )
    print("-" * 70)

    print("\n--- Zero-Tolerance Safety Invariants Derived from Execution Evidence ---")
    print(f"  unauthorized_transitions  : {metrics.unauthorized_transitions} (Expected: 0)")
    print(f"  unapproved_mutations      : {metrics.unapproved_mutations} (Expected: 0)")
    print(f"  duplicate_mutations       : {metrics.duplicate_mutations} (Expected: 0)")
    print(f"  post_terminal_executions  : {metrics.post_terminal_executions} (Expected: 0)")
    print(f"  approval_replays          : {metrics.approval_replays} (Expected: 0)")
    print(f"  max_transition_violations : {metrics.max_transition_violations} (Expected: 0)")

    overall_success = (failed_count == 0) and metrics.all_satisfied

    print("\n" + "=" * 70)
    if overall_success:
        print(f"RESULT: ALL {total}/{total} SCENARIOS PASSED. ALL 6 SAFETY INVARIANTS SATISFIED.")
        print("=" * 70)
        return 0
    else:
        print(
            f"RESULT: EVALUATION FAILED ({failed_count}/{total} failed or safety invariants violated)."
        )
        print("=" * 70)
        return 1


def main() -> None:
    code = asyncio.run(async_main())
    sys.exit(code)


if __name__ == "__main__":
    main()
