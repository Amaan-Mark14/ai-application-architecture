"""Local durable customer accounts and atomically recorded business action identities."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from agent.domain import Account, Customer, InMemoryCustomerStore

from .store import SQLiteWorkflowStore


class SQLiteCustomerStore(InMemoryCustomerStore):
    """The synthetic domain baseline with persistent, idempotent account operations."""

    def __init__(self, workflow_store: SQLiteWorkflowStore) -> None:
        self.workflow_store = workflow_store
        baseline = InMemoryCustomerStore()
        with self.workflow_store._get_connection() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS business_customers (customer_id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS business_accounts (customer_id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS business_operations (action_id TEXT PRIMARY KEY, capability TEXT NOT NULL, canonical_args TEXT NOT NULL)"
            )
            for customer in baseline._customers.values():
                conn.execute(
                    "INSERT OR IGNORE INTO business_customers VALUES (?, ?)",
                    (customer.customer_id, json.dumps(asdict(customer))),
                )
            for account in baseline._accounts.values():
                conn.execute(
                    "INSERT OR IGNORE INTO business_accounts VALUES (?, ?)",
                    (account.customer_id, json.dumps(asdict(account))),
                )

    def reset(self) -> None:
        raise RuntimeError("Persistent business state cannot be reset implicitly.")

    def get_customer(self, customer_id: str) -> Customer | None:
        with self.workflow_store._get_connection() as conn:
            row = conn.execute(
                "SELECT payload FROM business_customers WHERE customer_id=?", (customer_id,)
            ).fetchone()
            return Customer(**json.loads(row[0])) if row else None

    def get_account(self, customer_id: str) -> Account | None:
        with self.workflow_store._get_connection() as conn:
            row = conn.execute(
                "SELECT payload FROM business_accounts WHERE customer_id=?", (customer_id,)
            ).fetchone()
            return Account(**json.loads(row[0])) if row else None

    def operation_status(
        self, action_id: str, capability: str, arguments: dict[str, object]
    ) -> str:
        if not self.get_account(str(arguments.get("customer_id", ""))):
            return "inconclusive"
        with self.workflow_store._get_connection() as conn:
            row = conn.execute(
                "SELECT capability, canonical_args FROM business_operations WHERE action_id=?",
                (action_id,),
            ).fetchone()
            if not row:
                return "not_executed"
            return (
                "executed"
                if (row[0], row[1]) == (capability, json.dumps(arguments, sort_keys=True))
                else "inconclusive"
            )

    def _apply(self, action_id: str, capability: str, arguments: dict[str, object]) -> bool:
        if not isinstance(action_id, str) or not action_id.strip():
            raise ValueError("Persistent account operations require a non-empty action ID.")
        canonical = json.dumps(arguments, sort_keys=True)
        customer_id = str(arguments["customer_id"])
        with self.workflow_store._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT capability, canonical_args FROM business_operations WHERE action_id=?",
                (action_id,),
            ).fetchone()
            if existing:
                if (existing[0], existing[1]) != (capability, canonical):
                    raise ValueError("Conflicting business action ID")
                return True
            row = conn.execute(
                "SELECT payload FROM business_accounts WHERE customer_id=?", (customer_id,)
            ).fetchone()
            if not row:
                return False
            account: dict[str, Any] = json.loads(row[0])
            if capability == "apply_fee_credit":
                amount = arguments["amount_cents"]
                if type(amount) is not int or amount <= 0:
                    raise ValueError("Credit must be a positive integer number of cents.")
                account["balance_cents"] += amount
                account["credits_applied_cents"] += amount
                account["activity_log"].append(
                    f"Credit applied: +${amount / 100:.2f} (Reason: {arguments['reason']})"
                )
            else:
                note = str(arguments["note"]).strip()
                if not note:
                    raise ValueError("Note cannot be empty")
                account["notes"].append(note)
                account["activity_log"].append(f"Note added: {note}")
            conn.execute(
                "UPDATE business_accounts SET payload=? WHERE customer_id=?",
                (json.dumps(account), customer_id),
            )
            conn.execute(
                "INSERT INTO business_operations VALUES (?, ?, ?)",
                (action_id, capability, canonical),
            )
            return True

    def add_note(self, customer_id: str, note: str, *, action_id: str = "") -> bool:
        return self._apply(
            action_id, "update_customer_note", {"customer_id": customer_id, "note": note}
        )

    def apply_credit(
        self, customer_id: str, amount_cents: int, reason: str, *, action_id: str = ""
    ) -> bool:
        return self._apply(
            action_id,
            "apply_fee_credit",
            {"customer_id": customer_id, "amount_cents": amount_cents, "reason": reason},
        )
