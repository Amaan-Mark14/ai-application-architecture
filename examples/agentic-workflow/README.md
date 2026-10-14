# Agentic Workflow Orchestration (Tier 2 Pattern Example)

## 1. Problem Statement

Modern enterprise applications cannot rely solely on autonomous agent loops for end-to-end business operations. While bounded agent loops (such as Phase 6 ReAct) excel at dynamic information gathering, multi-step diagnostics, and unstructured reasoning, enterprise processes demand:
1. **Deterministic Process Sovereignty**: Business routing rules, compliance boundaries, and state transitions must be strictly application-controlled, never delegated to model hallucinations.
2. **Durable Asynchronous Suspension (HITL)**: High-consequence mutations (e.g. monetary concessions, account modifications) require human operator approval. The runtime must persist state to durable storage, cleanly terminate processes, and safely resume days later upon operator review.
3. **Crash Consistency & Ambiguous Execution Recovery**: If a server crashes mid-mutation, restarting must never blindly retry mutations or cause duplicate executions.
4. **Local Backward Compensation (Saga)**: Multi-step mutations that fail downstream must execute explicit inverse compensating steps to maintain ledger consistency.

The central thesis of Phase 7 is:
> **Workflow orchestration coordinates agentic execution; it does not replace the Phase 6 agent runtime.**  
> The agent may reason about the task, but the application owns the process.

---

## 2. Architectural Overview & Component Structure

This Tier 2 pattern example models a **Customer Account Remediation Workflow** using pure Python standard library and an ACID SQLite checkpoint store (Mode A: Offline Local).

```text
examples/agentic-workflow/
├── artifact.json                  # Tier 2 Pattern Example manifest
├── README.md                      # Architecture and operational guide
├── threat-model.md                # STRIDE and security analysis
├── eval_dataset.jsonl             # 30 deterministic test scenarios
├── eval_runner.py                 # Deterministic evaluation runner with self-tests
├── live_smoke.py                  # Live local Ollama smoke runner
├── demo.py                        # Interactive CLI demonstrating multi-terminal restart
├── verify.py                      # Standalone deterministic verification script
├── workflow/
│   ├── __init__.py                # Package exports
│   ├── errors.py                  # Domain failure hierarchy
│   ├── state.py                   # WorkflowState, RemediationProposal, ActorSnapshot
│   ├── definition.py              # WorkflowDefinition, StepDefinition, TransitionRule
│   ├── engine.py                  # WorkflowEngine with CAS concurrency
│   ├── store.py                   # Checkpoints, atomic approvals, and OS ownership
│   ├── business_store.py          # Persistent accounts and unique business action IDs
│   ├── ledger.py                  # DurableMutationLedger and stable action ID generator
│   ├── approval.py                # DurableApprovalRecord, WorkflowApprovalVerifier, Adapter
│   ├── audit.py                   # Structured append-only audit event recorder
│   ├── retry.py                   # StepRetryPolicy (mutation non-amplification)
│   ├── saga.py                    # Local Saga backward compensation coordinator
│   ├── remediation_workflow.py    # Canonical Customer Account Remediation graph builder
│   └── steps/
│       ├── __init__.py
│       ├── deterministic.py       # IntakeValidation, ContextGathering, Finalization
│       ├── agentic.py             # AgenticInvestigationStep (strictly read-only)
│       ├── human.py               # HumanApprovalStep (durable suspension)
│       └── mutation.py            # MutationExecutionStep (pre-verified tool execution)
└── tests/                         # 56 hermetic unit and integration tests
```

---

## 3. Sovereign Authority Matrix

| Concern | Workflow Engine | Phase 6 Agent | Language Model | Human Operator | Tool Executor |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Process Progression** | **SOVEREIGN** (Defines graph, evaluates transitions) | **NONE** (Cannot select next graph node) | **NONE** (Cannot emit transition commands) | **INDIRECT** (Supplies approval/rejection) | **NONE** |
| **Investigation Actions** | **BOUNDED** (Supplies read-only allowlist) | **BOUNDED** (Proposes tools from allowlist) | **UNTRUSTED** (Proposes JSON action payload) | **NONE** | **SANDBOXED** (Executes read tools) |
| **Authorization** | **SOVEREIGN** (Evaluates actor permissions) | **NONE** (Subject to `AuthorizationPolicy`) | **NONE** (Cannot elevate privileges) | **GOVERNS** (Must hold required role) | **NONE** |
| **Approval Verification** | **SOVEREIGN** (`WorkflowApprovalVerifier` checks SQLite) | **ADAPTER** (Wraps verifier if agent loop used) | **NONE** | **SOVEREIGN** (Authorizes/rejects mutation) | **NONE** (Does not check approvals) |
| **Physical Mutation** | **CONTROLS** (Routes to `MUTATION_EXECUTION`) | **NONE** (Agent does not mutate) | **NONE** | **NONE** | **SOVEREIGN** (Executes mutating tool) |
| **Step Retries** | **SOVEREIGN** (Governs bounded backoff) | **LOCAL** (Max 2 schema repairs) | **NONE** | **NONE** | **NONE** (Zero tool retries) |
| **Checkpoints** | **SOVEREIGN** (Commits to SQLite) | **NONE** (In-memory trace only) | **NONE** | **NONE** | **NONE** |
| **Saga Compensation** | **SOVEREIGN** (Routes to compensating step) | **NONE** | **NONE** | **NONE** | **NONE** |
| **Terminal State** | **SOVEREIGN** (Enforces terminal bounds) | **NONE** | **NONE** | **NONE** | **NONE** |

---

## 4. State Machine Definition & Transitions

The canonical Customer Account Remediation workflow graph is code-defined:

```mermaid
stateDiagram-v2
    [*] --> INTAKE_VALIDATION
    INTAKE_VALIDATION --> CONTEXT_GATHERING: Valid Input
    INTAKE_VALIDATION --> TERMINAL_FAILED: Invalid Customer ID

    CONTEXT_GATHERING --> AGENTIC_INVESTIGATION: Context Loaded
    CONTEXT_GATHERING --> TERMINAL_FAILED: Data Load Error

    AGENTIC_INVESTIGATION --> FINALIZATION: Proposal = INFORMATIONAL
    AGENTIC_INVESTIGATION --> TERMINAL_ESCALATED: Proposal = SECURITY_ESCALATION
    AGENTIC_INVESTIGATION --> MUTATE_NOTE: Proposal = CUSTOMER_NOTE
    AGENTIC_INVESTIGATION --> HUMAN_APPROVAL: Proposal = FEE_CREDIT

    HUMAN_APPROVAL --> CONCESSION_NOTE: Operator Approved
    CONCESSION_NOTE --> MUTATION_EXECUTION: Note Persisted
    CONCESSION_NOTE --> TERMINAL_FAILED: Note Failed
    HUMAN_APPROVAL --> TERMINAL_REJECTED: Operator Rejected

    MUTATE_NOTE --> FINALIZATION: Note Appended

    MUTATION_EXECUTION --> FINALIZATION: Credit Succeeded
    MUTATION_EXECUTION --> COMPENSATE_MUTATION: Credit Failed (Recorded Prior Note)
    MUTATION_EXECUTION --> TERMINAL_FAILED: Credit Failed (No Recorded Note)

    COMPENSATE_MUTATION --> TERMINAL_FAILED: Compensation Completed

    FINALIZATION --> TERMINAL_COMPLETED: Done

    TERMINAL_COMPLETED --> [*]
    TERMINAL_FAILED --> [*]
    TERMINAL_REJECTED --> [*]
    TERMINAL_ESCALATED --> [*]
```

### Safety Invariants
1. **Model Output Cannot Route**: The model emits a typed `RemediationProposal`. The state machine engine strictly evaluates declarative transition rules against the proposal type. Unexpected proposal fields such as `"goto": "finalization"` are rejected before routing.
2. **Cycle Ceiling**: A maximum transition count (`max_transitions = 15`) guarantees bounded execution and prevents infinite loops.
3. **Terminal Immobility**: Once a workflow reaches `COMPLETED`, `FAILED`, `REJECTED`, or `ESCALATED`, no further transitions or physical mutations are permitted.

---

## 5. Read-Only Agentic Investigation Invariant

Workflow input requires `customer_id` and a non-empty string `inquiry`. Intake rejects missing or mistyped complaints before investigation. The submitted `inquiry` is passed to the agent; there is no default complaint.

Only an agent result with status `COMPLETED` can supply a proposal. One JSON object must contain `proposal_type`, a non-empty `summary`, and `suggested_arguments`. Credit arguments require positive integer `amount_cents` and a non-empty `reason`; note arguments require a non-empty `note`; informational arguments must be empty; escalation arguments may contain a reason. Unknown fields, duplicate JSON keys, coercions, and malformed or incomplete output fail explicitly. Prose outside the object never selects an action. Optional boolean `requires_approval` and the legacy `requires_human_approval` alias are accepted, but credit approval is always required by the application.


To guarantee that autonomous model reasoning cannot cause side-effects:
* The `CapabilityRegistry` supplied to `AgenticInvestigationStep` contains **exclusively read-only tools**:
  - `get_customer` (`SideEffectLevel.READ_ONLY`)
  - `get_account_status` (`SideEffectLevel.READ_ONLY`)
  - `search_policy` (`SideEffectLevel.READ_ONLY`)
* The constructor explicitly inspects the registry; detecting any `STATE_MUTATING` tool raises an immediate `ValueError`.
* **Zero mutating capabilities can be executed during investigation.**

---

## 6. Durable Human-in-the-Loop (HITL) Lifecycle

The host must supply `actor_resolver(actor_id)` to the workflow builder. Immediately before each new mutation or compensation, the runtime resolves current identity, role, and permissions through this trusted lookup and then applies account-hold and role-limit policy. Missing, failed, unknown, or mismatched lookup results fail closed; the saved initiating actor is audit context, not current authority. The CLI uses a local host-owned directory (`operator_bob`, `operator_junior`, `operator_admin`); `--role` can only assert its configured role. Production identity-provider integration remains deferred.


When an agentic investigation proposes a state-mutating fee credit:
1. **Suspension**: The workflow enters `HUMAN_APPROVAL`. A `DurableApprovalRecord` is generated and saved to SQLite with status `PENDING`.
2. **Persistence**: The workflow state transitions to `AWAITING_APPROVAL`. Checkpoint version increments, SHA-256 integrity checksum is committed to disk, and the execution process cleanly exits.
3. **External Operator Action**: An independent process loads the pending approval, reviews the proposal, and registers an approval decision (`APPROVED` or `REJECTED`).
4. **Atomic resumption and ownership**: A local OS lock excludes live competing executors; the approval decision and checkpoint transition commit atomically, checking `checkpoint_version` and status in SQLite.
5. **Pre-Execution 7-Point Binding Verification**: Before executing the physical tool, `WorkflowApprovalVerifier` confirms:
   - Approval status is `APPROVED` and not `CONSUMED`.
   - Workflow ID, workflow-definition identity, and definition version match.
   - Stable action ID matches.
   - Target capability matches.
   - Canonical arguments JSON matches byte-for-byte.
   - Initiator actor ID and role match.
   - Expiration timestamp has not elapsed.
6. **Atomic Consumption**: Following verified tool execution, a single local database transaction marks `ledger.status = EXECUTED` and `approval.status = CONSUMED`.

---

## 7. Stable Action Identity & Mutation Ledger

Every state mutation derives a deterministic, stable action ID:
$$\text{digest} = \text{SHA-256}(\text{workflow\_id} \mathbin{\Vert} \text{step\_name} \mathbin{\Vert} \text{capability} \mathbin{\Vert} \text{canonical\_args})[0:16]$$
$$\text{action\_id} = \text{"act-" } \mathbin{\Vert} \text{workflow\_id} \mathbin{\Vert} \text{step\_name} \mathbin{\Vert} \text{digest}$$

* **Same logical mutation across restart** $\rightarrow$ **Identical `action_id`**.
* **Altered arguments** $\rightarrow$ **Different `action_id`** (rejects approval reuse).
* **Duplicate Execution Suppression**: Before invoking `ToolExecutor`, `MutationExecutionStep` queries the ledger. If `status == EXECUTED`, it replays the existing receipt without invoking the tool.
* **Ambiguous In-Flight Recovery (`EXECUTION_STARTED`)**: If a process crashes after `EXECUTION_STARTED` is recorded, the engine must never blindly re-execute the tool. On resumption, `MutationExecutionStep` triggers domain state reconciliation:
  - If domain state confirms physical execution completed: ledger is committed as `EXECUTED`, approval is consumed, and duplicate tool invocation is suppressed.
  - If domain state confirms physical execution did not take place: safe execution proceeds under the original stable `action_id`.
  - If reconciliation is inconclusive or unconfigured: ledger transitions to `AMBIGUOUS`, workflow halts safely in `FAILED`, and `manual_intervention_required` is flagged.

---

## 8. Local Saga Backward Compensation

In multi-step remediation flows where an operational note is logged prior to credit disbursement:
* If the credit disbursement encounters permanent downstream failure (e.g. gateway error or policy cap violation), the workflow transitions to `COMPENSATE_MUTATION`.
* The compensating step invokes `update_customer_note` appending an inverse compensating record:
  `"COMPENSATION: Fee credit disbursement failed for workflow ... Concession note voided."`
* If compensation succeeds, the workflow completes in `FAILED` status with `compensation_applied = True`.
* If compensation itself fails, the system logs `manual_intervention_required` and halts safely.
* **Compensation Crash Recovery & Reconciliation**: Compensating actions derive stable action IDs (`act-{wf_id}-compensate_mutation-...`) and enforce the same in-flight reconciliation semantics as primary mutations. If a crash occurs after `EXECUTION_STARTED`, recovery reconciles the specific business operation ID: if the compensating operation is already recorded, duplicate invocation is suppressed (`duplicate_suppressed = True`) and ledger is committed to `EXECUTED`; if unexecuted, it executes safely; if inconclusive, it fails closed to `AMBIGUOUS` with `manual_intervention_required = True`.

---

## 9. Verification & Execution Instructions

Local recovery now uses `WorkflowEngine.recover_workflow()` or `demo.py --db <database> recover --workflow-id <id>` for abandoned `RUNNING` executions. The initiating, resuming and recovering process holds an OS file lock for the entire run; a live owner blocks competitors, and process death releases ownership. Approval decisions and their checkpoint transition commit in one SQLite transaction. A mutation can be re-entered only when its host-declared recovery contract reconciles the durable action record; arbitrary mutating steps fail closed.

The CLI now persists customer accounts and action identities in the workflow SQLite database. Each account update and its unique action ID commit together. Identical retries reuse that operation; conflicting reuse is rejected. Reconciliation checks the exact action ID, capability and canonical arguments, so unrelated identical text or amounts do not count as completion. The in-memory test store retains the same operation identity semantics within its lifetime.

An approved credit now records a concession note first, then attempts the credit. Compensation eligibility comes from the successful preceding note's durable ledger record, never `has_prior_note` supplied by a caller. A downstream failure appends a voiding note and records which earlier action it compensates. Current definition version is **1.1.0**; old 1.0.0 checkpoints require their original compatible definition or an explicit migration, which is not implemented. This remains a local SQLite pattern; distributed ownership and production identity-provider integration are deferred.

The deterministic evaluator requires both the expected terminal status and the exact expected number of account mutations. Unexpected exceptions fail the scenario. This is software evidence; live-model quality and independent acceptance remain unverified.


### Run Unit and Integration Test Suite
```bash
python3 -m unittest discover -s examples/agentic-workflow/tests
```
Executes the hermetic workflow unit and integration suite, including negative boundary and evaluator-verdict tests.

### Run 30-Scenario Deterministic Evaluation
```bash
python3 examples/agentic-workflow/eval_runner.py
```
Runs all 30 versioned scenarios from `eval_dataset.jsonl`, asserting zero-tolerance safety invariants:
* `unauthorized_transitions == 0`
* `unapproved_mutations == 0`
* `duplicate_mutations == 0`
* `post_terminal_executions == 0`
* `approval_replays == 0`
* `max_transition_violations == 0`

To verify that the evaluator detects injected safety violations:
```bash
python3 examples/agentic-workflow/eval_runner.py --self-test
```

### Run Live Local Ollama Smoke Suite
```bash
python3 examples/agentic-workflow/live_smoke.py --model llama3.2
```
Connects to local Ollama (`http://localhost:11434`), verifies provider reachability, executes live reasoning with real `llama3.2`, and confirms read-only investigation invariants. If Ollama is offline:
```bash
python3 examples/agentic-workflow/live_smoke.py --allow-unverified
```
Outputs `STATUS: NOT VERIFIED` and exits cleanly.

### Run Interactive Multi-Terminal Process-Restart Demo
```bash
# Terminal 1: Start workflow and pause at approval gate
python3 examples/agentic-workflow/demo.py start --workflow-id wf-demo-1 --customer cust-001 --inquiry "Dispute $25 late fee"

# Terminal 2: Inspect durable state from independent process
python3 examples/agentic-workflow/demo.py status --workflow-id wf-demo-1

# Terminal 2: Approve and complete execution
python3 examples/agentic-workflow/demo.py approve --workflow-id wf-demo-1 --approver manager_alice
```

### Run Standalone Pattern Verifier
```bash
python3 examples/agentic-workflow/verify.py
```
Runs artifact manifest validation, the complete workflow test suite, and the 30-scenario evaluation harness, confirming:
`PHASE 7 DETERMINISTIC VERIFICATION PASSED`.
