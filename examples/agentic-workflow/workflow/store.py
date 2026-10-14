"""Durable SQLite and in-memory persistence stores for checkpoints, approvals, and mutations."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import sqlite3
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from contracts.workflow import (
    CheckpointStorePort,
    WorkflowStatus,
)

from .approval import DurableApprovalRecord
from .audit import WorkflowAuditEvent
from .errors import ConcurrentResumeConflictError, CorruptedCheckpointError
from .ledger import DurableMutationRecord
from .state import WorkflowState


class InMemoryWorkflowStore(CheckpointStorePort):
    """Hermetic in-memory store for unit tests and rapid evaluation."""

    def __init__(self) -> None:
        self.checkpoints: dict[str, WorkflowState] = {}
        self.approvals: dict[str, DurableApprovalRecord] = {}
        self.approvals_by_action: dict[str, str] = {}
        self.mutations: dict[str, DurableMutationRecord] = {}
        self.audit_events: list[WorkflowAuditEvent] = []

    async def save_checkpoint(
        self,
        workflow_id: str,
        state_payload: Mapping[str, Any],
        checkpoint_version: int,
    ) -> None:
        state = WorkflowState.from_dict(state_payload)
        self.save_state(state)

    async def load_checkpoint(
        self,
        workflow_id: str,
    ) -> Mapping[str, Any] | None:
        state = self.load_state(workflow_id)
        return state.to_dict() if state else None

    def save_state(self, state: WorkflowState) -> None:
        state_with_checksum = state.with_updated_checksum()
        self.checkpoints[state.workflow_id] = state_with_checksum

    def load_state(self, workflow_id: str) -> WorkflowState | None:
        state = self.checkpoints.get(workflow_id)
        if state is None:
            return None
        if state.compute_checksum() != state.checksum:
            raise CorruptedCheckpointError(
                f"Integrity checksum verification failed for workflow '{workflow_id}'."
            )
        return state

    def compare_and_swap_resume(
        self,
        workflow_id: str,
        expected_version: int,
        new_version: int,
    ) -> bool:
        state = self.checkpoints.get(workflow_id)
        if not state:
            return False
        if (
            state.checkpoint_version == expected_version
            and state.status == WorkflowStatus.AWAITING_APPROVAL
        ):
            updated = WorkflowState(
                workflow_id=state.workflow_id,
                definition=state.definition,
                status=WorkflowStatus.RUNNING,
                current_step=state.current_step,
                initiator_actor=state.initiator_actor,
                history=state.history,
                pending_approval_id=state.pending_approval_id,
                step_retries=state.step_retries,
                domain_context=state.domain_context,
                checkpoint_version=new_version,
                checksum="",
                created_at=state.created_at,
                updated_at=time.time(),
            )
            self.save_state(updated)
            return True
        return False

    def save_approval(self, record: DurableApprovalRecord) -> None:
        self.approvals[record.approval_id] = record
        self.approvals_by_action[record.action_id] = record.approval_id

    def load_approval(self, approval_id: str) -> DurableApprovalRecord | None:
        return self.approvals.get(approval_id)

    def load_approval_by_action_id(self, action_id: str) -> DurableApprovalRecord | None:
        app_id = self.approvals_by_action.get(action_id)
        return self.approvals.get(app_id) if app_id else None

    def save_mutation(self, record: DurableMutationRecord) -> None:
        self.mutations[record.action_id] = record

    def load_mutation(self, action_id: str) -> DurableMutationRecord | None:
        return self.mutations.get(action_id)

    def atomic_commit_mutation_and_approval(
        self,
        mutation: DurableMutationRecord,
        approval: DurableApprovalRecord,
    ) -> None:
        self.save_mutation(mutation)
        self.save_approval(approval)

    def commit_approval_transition(
        self, previous: WorkflowState, updated: WorkflowState, approval: DurableApprovalRecord
    ) -> None:
        current = self.load_state(previous.workflow_id)
        if (
            current is None
            or current.checkpoint_version != previous.checkpoint_version
            or current.status != previous.status
        ):
            raise ConcurrentResumeConflictError("Approval checkpoint changed before commit.")
        if approval.approval_id not in self.approvals:
            raise ValueError("Pending approval not found.")
        prepared = updated.with_updated_checksum()
        self.approvals[approval.approval_id] = approval
        self.checkpoints[updated.workflow_id] = prepared

    def append_audit_event(self, event: WorkflowAuditEvent) -> None:
        self.audit_events.append(event)

    def load_audit_events(self, workflow_id: str | None = None) -> list[WorkflowAuditEvent]:
        if workflow_id is None:
            return list(self.audit_events)
        return [e for e in self.audit_events if e.workflow_id == workflow_id]


class SQLiteWorkflowStore(CheckpointStorePort):
    """Persistent standard-library SQLite store with WAL mode and optimistic concurrency."""

    def __init__(self, db_path: str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _get_connection(self) -> Iterator[sqlite3.Connection]:
        """Commit or roll back the transaction, then always release its handle."""
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=5000;")
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS workflow_checkpoints (
                    workflow_id TEXT PRIMARY KEY,
                    definition_id TEXT NOT NULL,
                    definition_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_step TEXT NOT NULL,
                    checkpoint_version INTEGER NOT NULL,
                    state_json TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS workflow_approvals (
                    approval_id TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL,
                    action_id TEXT NOT NULL,
                    capability_name TEXT NOT NULL,
                    canonical_args TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    status TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS workflow_mutation_ledger (
                    action_id TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL,
                    step_name TEXT NOT NULL,
                    capability_name TEXT NOT NULL,
                    canonical_args TEXT NOT NULL,
                    status TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS workflow_audit_log (
                    event_id TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    step_name TEXT,
                    details_json TEXT NOT NULL,
                    timestamp REAL NOT NULL
                );
            """)
            conn.commit()

    async def save_checkpoint(
        self,
        workflow_id: str,
        state_payload: Mapping[str, Any],
        checkpoint_version: int,
    ) -> None:
        state = WorkflowState.from_dict(state_payload)
        self.save_state(state)

    async def load_checkpoint(
        self,
        workflow_id: str,
    ) -> Mapping[str, Any] | None:
        state = self.load_state(workflow_id)
        return state.to_dict() if state else None

    def save_state(self, state: WorkflowState) -> None:
        state_with_checksum = state.with_updated_checksum()
        state_json = json.dumps(state_with_checksum.to_dict())

        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO workflow_checkpoints (
                    workflow_id, definition_id, definition_version, status,
                    current_step, checkpoint_version, state_json, checksum,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(workflow_id) DO UPDATE SET
                    status = excluded.status,
                    current_step = excluded.current_step,
                    checkpoint_version = excluded.checkpoint_version,
                    state_json = excluded.state_json,
                    checksum = excluded.checksum,
                    updated_at = excluded.updated_at;
                """,
                (
                    state_with_checksum.workflow_id,
                    state_with_checksum.definition.definition_id,
                    state_with_checksum.definition.version,
                    state_with_checksum.status.value,
                    state_with_checksum.current_step,
                    state_with_checksum.checkpoint_version,
                    state_json,
                    state_with_checksum.checksum,
                    state_with_checksum.created_at,
                    state_with_checksum.updated_at,
                ),
            )
            conn.commit()

    def load_state(self, workflow_id: str) -> WorkflowState | None:
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT state_json, checksum FROM workflow_checkpoints WHERE workflow_id = ?;",
                (workflow_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None

            try:
                data = json.loads(row["state_json"])
                state = WorkflowState.from_dict(data)
            except Exception as exc:
                raise CorruptedCheckpointError(
                    f"Integrity check failed: corrupted state JSON for workflow '{workflow_id}': {exc}"
                ) from exc

            # Checksum integrity validation
            computed = state.compute_checksum()
            if computed != row["checksum"] or computed != state.checksum:
                raise CorruptedCheckpointError(
                    f"Integrity checksum mismatch for workflow '{workflow_id}'. File may be corrupted."
                )
            return state

    def commit_approval_transition(
        self, previous: WorkflowState, updated: WorkflowState, approval: DurableApprovalRecord
    ) -> None:
        prepared = updated.with_updated_checksum()
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute(
                "UPDATE workflow_checkpoints SET status=?, current_step=?, checkpoint_version=?, state_json=?, checksum=?, updated_at=? WHERE workflow_id=? AND checkpoint_version=? AND status=?",
                (
                    prepared.status.value,
                    prepared.current_step,
                    prepared.checkpoint_version,
                    json.dumps(prepared.to_dict()),
                    prepared.checksum,
                    prepared.updated_at,
                    previous.workflow_id,
                    previous.checkpoint_version,
                    previous.status.value,
                ),
            )
            if changed.rowcount != 1:
                raise ConcurrentResumeConflictError("Approval checkpoint changed before commit.")
            changed = conn.execute(
                "UPDATE workflow_approvals SET status=?, record_json=?, updated_at=? WHERE approval_id=? AND workflow_id=? AND status='pending'",
                (
                    approval.status.value,
                    json.dumps(approval.to_dict()),
                    time.time(),
                    approval.approval_id,
                    previous.workflow_id,
                ),
            )
            if changed.rowcount != 1:
                raise ConcurrentResumeConflictError("Pending approval changed before commit.")

    def compare_and_swap_resume(
        self,
        workflow_id: str,
        expected_version: int,
        new_version: int,
    ) -> bool:
        """Atomic compare-and-swap on checkpoint version and AWAITING_APPROVAL status."""
        now = time.time()
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT state_json FROM workflow_checkpoints WHERE workflow_id = ? AND checkpoint_version = ? AND status = ?;",
                (workflow_id, expected_version, WorkflowStatus.AWAITING_APPROVAL.value),
            )
            row = cursor.fetchone()
            if not row:
                return False

            data = json.loads(row["state_json"])
            data["status"] = WorkflowStatus.RUNNING.value
            data["checkpoint_version"] = new_version
            data["updated_at"] = now

            # Reconstruct state to compute canonical checksum over updated payload
            updated_state = WorkflowState.from_dict(data)
            new_checksum = updated_state.compute_checksum()
            data["checksum"] = new_checksum
            updated_json = json.dumps(data)

            # Atomic single-statement update of columns, state JSON, and checksum
            update_cur = conn.execute(
                """
                UPDATE workflow_checkpoints
                SET checkpoint_version = ?,
                    status = ?,
                    state_json = ?,
                    checksum = ?,
                    updated_at = ?
                WHERE workflow_id = ?
                  AND checkpoint_version = ?
                  AND status = ?;
                """,
                (
                    new_version,
                    WorkflowStatus.RUNNING.value,
                    updated_json,
                    new_checksum,
                    now,
                    workflow_id,
                    expected_version,
                    WorkflowStatus.AWAITING_APPROVAL.value,
                ),
            )
            conn.commit()
            return update_cur.rowcount == 1

    def save_approval(self, record: DurableApprovalRecord) -> None:
        rec_json = json.dumps(record.to_dict())
        now = time.time()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO workflow_approvals (
                    approval_id, workflow_id, action_id, capability_name,
                    canonical_args, actor_id, actor_role, status, record_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(approval_id) DO UPDATE SET
                    status = excluded.status,
                    record_json = excluded.record_json,
                    updated_at = excluded.updated_at;
                """,
                (
                    record.approval_id,
                    record.workflow_id,
                    record.action_id,
                    record.capability_name,
                    record.canonical_arguments_json,
                    record.actor_id,
                    record.actor_role,
                    record.status.value,
                    rec_json,
                    record.requested_at,
                    now,
                ),
            )
            conn.commit()

    def load_approval(self, approval_id: str) -> DurableApprovalRecord | None:
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT record_json FROM workflow_approvals WHERE approval_id = ?;",
                (approval_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return DurableApprovalRecord.from_dict(json.loads(row["record_json"]))

    def load_approval_by_action_id(self, action_id: str) -> DurableApprovalRecord | None:
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT record_json FROM workflow_approvals WHERE action_id = ?;",
                (action_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return DurableApprovalRecord.from_dict(json.loads(row["record_json"]))

    def save_mutation(self, record: DurableMutationRecord) -> None:
        rec_json = json.dumps(record.to_dict())
        now = time.time()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO workflow_mutation_ledger (
                    action_id, workflow_id, step_name, capability_name,
                    canonical_args, status, record_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(action_id) DO UPDATE SET
                    status = excluded.status,
                    record_json = excluded.record_json,
                    updated_at = excluded.updated_at;
                """,
                (
                    record.action_id,
                    record.workflow_id,
                    record.step_name,
                    record.capability_name,
                    record.canonical_args,
                    record.status.value,
                    rec_json,
                    record.created_at,
                    now,
                ),
            )
            conn.commit()

    def load_mutation(self, action_id: str) -> DurableMutationRecord | None:
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT record_json FROM workflow_mutation_ledger WHERE action_id = ?;",
                (action_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return DurableMutationRecord.from_dict(json.loads(row["record_json"]))

    def atomic_commit_mutation_and_approval(
        self,
        mutation: DurableMutationRecord,
        approval: DurableApprovalRecord,
    ) -> None:
        """Atomically update mutation ledger to EXECUTED and approval record to CONSUMED."""
        mut_json = json.dumps(mutation.to_dict())
        app_json = json.dumps(approval.to_dict())
        now = time.time()

        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            conn.execute(
                """
                INSERT INTO workflow_mutation_ledger (
                    action_id, workflow_id, step_name, capability_name,
                    canonical_args, status, record_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(action_id) DO UPDATE SET
                    status = excluded.status,
                    record_json = excluded.record_json,
                    updated_at = excluded.updated_at;
                """,
                (
                    mutation.action_id,
                    mutation.workflow_id,
                    mutation.step_name,
                    mutation.capability_name,
                    mutation.canonical_args,
                    mutation.status.value,
                    mut_json,
                    mutation.created_at,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO workflow_approvals (
                    approval_id, workflow_id, action_id, capability_name,
                    canonical_args, actor_id, actor_role, status, record_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(approval_id) DO UPDATE SET
                    status = excluded.status,
                    record_json = excluded.record_json,
                    updated_at = excluded.updated_at;
                """,
                (
                    approval.approval_id,
                    approval.workflow_id,
                    approval.action_id,
                    approval.capability_name,
                    approval.canonical_arguments_json,
                    approval.actor_id,
                    approval.actor_role,
                    approval.status.value,
                    app_json,
                    approval.requested_at,
                    now,
                ),
            )
            conn.commit()

    def append_audit_event(self, event: WorkflowAuditEvent) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO workflow_audit_log (
                    event_id, workflow_id, event_type, step_name, details_json, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?);
                """,
                (
                    event.event_id,
                    event.workflow_id,
                    event.event_type,
                    event.step_name,
                    json.dumps(event.details),
                    event.timestamp,
                ),
            )
            conn.commit()

    def load_audit_events(self, workflow_id: str | None = None) -> list[WorkflowAuditEvent]:
        with self._get_connection() as conn:
            if workflow_id:
                cursor = conn.execute(
                    "SELECT event_id, workflow_id, event_type, step_name, details_json, timestamp "
                    "FROM workflow_audit_log WHERE workflow_id = ? ORDER BY timestamp ASC;",
                    (workflow_id,),
                )
            else:
                cursor = conn.execute(
                    "SELECT event_id, workflow_id, event_type, step_name, details_json, timestamp "
                    "FROM workflow_audit_log ORDER BY timestamp ASC;"
                )
            results = []
            for row in cursor.fetchall():
                results.append(
                    WorkflowAuditEvent(
                        event_id=row["event_id"],
                        workflow_id=row["workflow_id"],
                        event_type=row["event_type"],
                        step_name=row["step_name"],
                        details=json.loads(row["details_json"]),
                        timestamp=float(row["timestamp"]),
                    )
                )
            return results


@contextmanager
def execution_guard(store: Any, workflow_id: str) -> Iterator[None]:
    """Local OS ownership survives coroutine waits and is released on process death."""
    db_path = getattr(store, "db_path", None)
    if db_path is None:
        active: set[str] = getattr(store, "_active_executions", set())
        store._active_executions = active
        if workflow_id in active:
            raise ConcurrentResumeConflictError("Workflow already has an active executor.")
        active.add(workflow_id)
        try:
            yield
        finally:
            active.remove(workflow_id)
        return
    lock_dir = Path(db_path).parent / ".workflow-locks"
    lock_dir.mkdir(exist_ok=True)
    identity = hashlib.sha256(
        (str(Path(db_path).resolve()) + ":" + workflow_id).encode()
    ).hexdigest()
    with (lock_dir / identity).open("a+b") as handle:
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise ConcurrentResumeConflictError(
                    "Workflow already has an active executor."
                ) from exc
        else:
            fcntl = importlib.import_module("fcntl")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise ConcurrentResumeConflictError(
                    "Workflow already has an active executor."
                ) from exc
        try:
            yield
        finally:
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
