#!/usr/bin/env python3
"""Interactive CLI demonstration for Phase 7 Agentic Workflow Orchestration.

Demonstrates durable Human-in-the-Loop (HITL) pause, process exit, and independent
process resumption from SQLite across multiple terminal sessions.

Commands:
  start     Start a new remediation workflow instance (pauses at human approval if credit proposed)
  status    Inspect durable state, current step, pending approval, and mutation ledger
  approve   Approve pending action and resume workflow execution to completion
  reject    Reject pending action and route workflow to terminal rejected state
  resume    Resume workflow with explicit approval decision

Usage examples:
  # Terminal 1: Start workflow and pause at approval gate
  python3 demo.py start --customer cust-001 --inquiry "Dispute accidental overdraft fee"

  # Terminal 2: Inspect status from independent process
  python3 demo.py status --workflow-id wf-demo-1

  # Terminal 2: Approve and resume execution
  python3 demo.py approve --workflow-id wf-demo-1 --approver manager_alice
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
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
from contracts.agent import AgentActor
from contracts.workflow import WorkflowStatus
from workflow.business_store import SQLiteCustomerStore
from workflow.engine import WorkflowEngine
from workflow.remediation_workflow import build_customer_remediation_workflow
from workflow.state import WorkflowActorSnapshot, WorkflowState
from workflow.store import SQLiteWorkflowStore

DEFAULT_DB_PATH = str(WORKFLOW_DIR / "workflow_demo.db")
DEMO_ACTORS = {
    "operator_bob": AgentActor("operator_bob", "senior_agent"),
    "operator_junior": AgentActor("operator_junior", "junior_agent"),
    "operator_admin": AgentActor("operator_admin", "admin"),
}


class DemoAgentEngine:
    """Agent reasoning stub for CLI demo producing realistic structured proposals."""

    def __init__(self, inquiry: str) -> None:
        self.inquiry = inquiry

    async def run(self, task: str, actor: AgentActor) -> Any:
        inquiry_lower = self.inquiry.lower()

        if "fraud" in inquiry_lower or "frozen" in inquiry_lower or "security" in inquiry_lower:
            answer = (
                "Customer inquiry concerns potential fraud or security hold.\n"
                '{"proposal_type": "security_escalation", "summary": "Escalate to fraud investigations team", '
                '"suggested_arguments": {"reason": "Customer mentions potential unauthorized account activity"}, '
                '"requires_human_approval": false}'
            )
        elif "note" in inquiry_lower or "address" in inquiry_lower or "travel" in inquiry_lower:
            answer = (
                "Administrative record update requested.\n"
                '{"proposal_type": "customer_note", "summary": "Record operational account note", '
                f'"suggested_arguments": {json.dumps({"note": "Customer inquiry noted: " + self.inquiry})}, '
                '"requires_human_approval": false}'
            )
        elif (
            "credit" in inquiry_lower
            or "fee" in inquiry_lower
            or "dispute" in inquiry_lower
            or "refund" in inquiry_lower
        ):
            answer = (
                "Verified billing discrepancy. Outage fee adjustment recommended.\n"
                '{"proposal_type": "fee_credit", "summary": "Refund $25.00 dispute concession", '
                '"suggested_arguments": {"amount_cents": 2500, "reason": "Disputed fee concession"}, '
                '"requires_human_approval": true}'
            )
        else:
            answer = (
                "Standard account inquiry. No adjustment necessary.\n"
                '{"proposal_type": "informational", "summary": "General inquiry answered", '
                '"suggested_arguments": {}, "requires_human_approval": false}'
            )

        class Result:
            def __init__(self, text: str) -> None:
                self.final_answer = text
                self.status = "COMPLETED"
                self.execution_receipts: list[Any] = []
                self.run_id = f"demo-run-{time.time_ns()}"
                self.step_count = 2

        return Result(answer)


def get_components(db_path: str, inquiry: str = "") -> tuple[WorkflowEngine, InMemoryCustomerStore]:
    sqlite_store = SQLiteWorkflowStore(db_path)
    customer_store = SQLiteCustomerStore(sqlite_store)
    auth_policy = CustomerSupportAuthorizationPolicy(store=customer_store)

    read_only_caps = [
        GetCustomerCapability(customer_store),
        GetAccountStatusCapability(customer_store),
        SearchPolicyCapability(),
    ]
    credit_cap = ApplyFeeCreditCapability(customer_store)
    note_cap = UpdateCustomerNoteCapability(customer_store)
    executor = ToolExecutor()
    agent_engine = DemoAgentEngine(inquiry)

    workflow_def = build_customer_remediation_workflow(
        customer_store=customer_store,
        agent_engine=agent_engine,
        read_only_registry=CapabilityRegistry(read_only_caps),
        credit_capability=credit_cap,
        note_capability=note_cap,
        tool_executor=executor,
        workflow_store=sqlite_store,
        auth_policy=auth_policy,
        actor_resolver=DEMO_ACTORS.get,
    )

    engine = WorkflowEngine(definition=workflow_def, store=sqlite_store)
    return engine, customer_store


def format_state_banner(state: WorkflowState) -> str:
    lines = [
        "=" * 70,
        f"WORKFLOW INSTANCE: {state.workflow_id}",
        "=" * 70,
        f"  Definition:         {state.definition.definition_id} (v{state.definition.version})",
        f"  Status:             {state.status.value.upper()}",
        f"  Current Step:       {state.current_step}",
        f"  Checkpoint Version: {state.checkpoint_version}",
        f"  Initiator Actor:    {state.initiator_actor.actor_id} ({state.initiator_actor.role})",
        f"  Integrity Checksum: {(state.checksum or state.compute_checksum())[:16]}...",
        f"  Step History Count: {len(state.history)}",
    ]
    lines.append("-" * 70)
    return "\n".join(lines)


async def cmd_start(args: argparse.Namespace) -> int:
    wf_id = args.workflow_id or f"wf-demo-{int(time.time()) % 10000}"
    db_path = args.db or DEFAULT_DB_PATH
    customer_id = args.customer
    inquiry = args.inquiry
    current_actor = DEMO_ACTORS.get(args.actor)
    if current_actor is None or (args.role is not None and args.role != current_actor.role):
        print("Actor or role does not match the host's local demo actor directory.")
        return 1
    engine, _ = get_components(db_path, inquiry)
    actor = WorkflowActorSnapshot(actor_id=current_actor.actor_id, role=current_actor.role)

    print(f"\n[DEMO START] Initializing workflow '{wf_id}' against '{db_path}'...")
    print(f"Customer ID:  {customer_id}")
    print(f"Actor ID:     {actor.actor_id} (Role: {actor.role})")
    print(f"Inquiry:      {inquiry}\n")

    state = await engine.start_workflow(
        workflow_id=wf_id,
        actor=actor,
        domain_context={"customer_id": customer_id, "inquiry": inquiry},
    )

    print(format_state_banner(state))

    if state.status == WorkflowStatus.AWAITING_APPROVAL:
        approval_id = state.domain_context.get("pending_approval_id")
        action_id = state.domain_context.get("stable_action_id")
        args_dict = state.domain_context.get("target_arguments", {})
        print(">>> WORKFLOW SUSPENDED: Awaiting Human Authorization <<<")
        print(f"  Pending Approval ID: {approval_id}")
        print(f"  Stable Action ID:    {action_id}")
        print(f"  Target Capability:   {state.domain_context.get('target_capability')}")
        print(f"  Proposed Arguments:  {args_dict}")
        print("\nProcess is now cleanly terminating. State is safely persisted in SQLite.")
        print("To inspect or resume in another terminal:")
        print(f"  python3 demo.py status --workflow-id {wf_id}")
        print(f"  python3 demo.py approve --workflow-id {wf_id} --approver manager_jane")
    else:
        print(f"Workflow reached terminal status: {state.status.value.upper()}")

    return 0


async def cmd_status(args: argparse.Namespace) -> int:
    wf_id = args.workflow_id
    db_path = args.db or DEFAULT_DB_PATH

    store = SQLiteWorkflowStore(db_path)
    state = store.load_state(wf_id)
    if not state:
        print(f"Error: Workflow '{wf_id}' not found in '{db_path}'.")
        return 1

    print("\n" + format_state_banner(state))

    # Inspect Pending Approval if present
    approval_id = state.domain_context.get("pending_approval_id")
    if approval_id:
        approval = store.load_approval(approval_id)
        if approval:
            print("HUMAN APPROVAL RECORD:")
            print(f"  Approval ID:       {approval.approval_id}")
            print(f"  Status:            {approval.status.value.upper()}")
            print(f"  Action ID:         {approval.action_id}")
            print(f"  Capability:        {approval.capability_name}")
            print(f"  Canonical Args:    {approval.canonical_arguments_json}")
            print(f"  Reason:            {approval.reason}")
            print(f"  Decided At:        {approval.decided_at}")
            print(f"  Consumed At:       {approval.consumed_at}")
            print("-" * 70)

    # Inspect Mutation Ledger
    action_id = state.domain_context.get("stable_action_id")
    if action_id:
        mutation = store.load_mutation(action_id)
        if mutation:
            print("DURABLE MUTATION LEDGER:")
            print(f"  Action ID:         {mutation.action_id}")
            print(f"  Mutation Status:   {mutation.status.value.upper()}")
            print(f"  Capability:        {mutation.capability_name}")
            print(f"  Canonical Args:    {mutation.canonical_args}")
            if mutation.execution_receipt:
                print(f"  Receipt Status:    {mutation.execution_receipt.status}")
                print(f"  Receipt Summary:   {mutation.execution_receipt.result_summary}")
            print("-" * 70)

    print("STEP EXECUTION HISTORY:")
    for h in state.history:
        print(f"  - Step: {h.step_name:<25} Status: {h.status.value:<10} Outcome: {h.outcome_type}")
    print("=" * 70)

    return 0


async def cmd_decision(args: argparse.Namespace, decision: str) -> int:
    wf_id = args.workflow_id
    db_path = args.db or DEFAULT_DB_PATH
    approver = args.approver
    reason = args.reason or f"CLI {decision.upper()} action"

    engine, _customer_store = get_components(db_path)

    print(f"\n[DEMO RESUME] Resuming workflow '{wf_id}' from '{db_path}'...")
    print(f"Decision: {decision.upper()}")
    print(f"Approver: {approver}")
    print(f"Reason:   {reason}\n")

    try:
        final_state = await engine.resume_workflow(
            workflow_id=wf_id,
            decision=decision,
            approver_id=approver,
            reason=reason,
        )

        print(format_state_banner(final_state))
        print(f">>> WORKFLOW EXECUTION COMPLETE: {final_state.status.value.upper()} <<<")

        action_id = final_state.domain_context.get("stable_action_id")
        if action_id:
            mutation = engine.store.load_mutation(action_id)
            if mutation and mutation.execution_receipt:
                print(f"Physical Execution Receipt: {mutation.execution_receipt.result_summary}")

        return 0
    except Exception as exc:  # noqa: BLE001 -- finalize failures from external execution boundaries.
        print(f"\nExecution halted with error: {exc}")
        return 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 7 Agentic Workflow Orchestration CLI Demo")
    parser.add_argument(
        "--db",
        "--db-path",
        dest="db",
        type=str,
        default=DEFAULT_DB_PATH,
        help="Path to SQLite database file",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # start subcommand
    p_start = subparsers.add_parser("start", help="Start new remediation workflow instance")
    p_start.add_argument("--workflow-id", type=str, default=None, help="Custom workflow identifier")
    p_start.add_argument(
        "--customer", type=str, default="cust-001", help="Customer ID (cust-001, cust-002, etc.)"
    )
    p_start.add_argument(
        "--inquiry",
        type=str,
        default="Dispute accidental $25 overdraft fee during platform outage",
        help="Customer inquiry description",
    )
    p_start.add_argument("--actor", type=str, default="operator_bob", help="Initiating actor ID")
    p_start.add_argument(
        "--role",
        type=str,
        default=None,
        help="Optional role assertion; must match the local actor directory",
    )

    # status subcommand
    p_status = subparsers.add_parser(
        "status", help="Inspect status and durable records of a workflow"
    )
    p_status.add_argument("--workflow-id", type=str, required=True, help="Workflow ID to inspect")

    # approve subcommand
    p_app = subparsers.add_parser("approve", help="Approve pending action and resume workflow")
    p_app.add_argument("--workflow-id", type=str, required=True, help="Workflow ID to resume")
    p_app.add_argument("--approver", type=str, default="manager_jane", help="Approver ID")
    p_app.add_argument("--reason", type=str, default=None, help="Approval justification")

    # reject subcommand
    p_rej = subparsers.add_parser("reject", help="Reject pending action and resume workflow")
    p_rej.add_argument("--workflow-id", type=str, required=True, help="Workflow ID to reject")
    p_rej.add_argument("--approver", type=str, default="manager_jane", help="Approver ID")
    p_rej.add_argument("--reason", type=str, default=None, help="Rejection justification")

    p_recover = subparsers.add_parser("recover", help="Recover an abandoned RUNNING workflow")
    p_recover.add_argument("--workflow-id", type=str, required=True)

    args = parser.parse_args()

    if args.command == "start":
        sys.exit(asyncio.run(cmd_start(args)))
    elif args.command == "status":
        sys.exit(asyncio.run(cmd_status(args)))
    elif args.command == "approve":
        sys.exit(asyncio.run(cmd_decision(args, "approved")))
    elif args.command == "reject":
        sys.exit(asyncio.run(cmd_decision(args, "rejected")))
    elif args.command == "recover":
        engine, _ = get_components(args.db or DEFAULT_DB_PATH)
        recovered = asyncio.run(engine.recover_workflow(args.workflow_id))
        print(format_state_banner(recovered))


if __name__ == "__main__":
    main()
