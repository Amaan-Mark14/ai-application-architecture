"""Local capability (tool) implementations conforming to CapabilityPort."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from contracts.agent import (
    CapabilityMetadata,
    CapabilityPort,
    SideEffectLevel,
)
from contracts.telemetry import AiOperationContext

from .domain import InMemoryCustomerStore

POLICIES = {
    "credit_limit": (
        "Customer fee credits are capped at a maximum of $50.00 (5000 cents) per incident. "
        "Credits require verified customer identity and operational justification. "
        "Junior agents are authorized for credits up to $20.00 (2000 cents); senior agents up to $50.00. "
        "Accounts marked with 'high' risk or 'frozen' status require formal supervisor approval."
    ),
    "frozen_account": (
        "Accounts under 'frozen' status are subject to security holds. "
        "No state mutations, fee credits, or funds disbursements may be executed on a frozen account "
        "until identity verification is completed and the hold is formally released by the security team."
    ),
    "customer_notes": (
        "Operational customer notes must document the business reason for inquiries, disputes, or concessions. "
        "Notes must never contain unencrypted card numbers, passwords, or personal credentials."
    ),
    "dispute_resolution": (
        "Billing disputes must be cross-referenced with recent transaction logs. "
        "If a duplicate or erroneous fee is verified, a fee credit may be proposed subject to approval."
    ),
}


class GetCustomerCapability(CapabilityPort):
    """Retrieves customer identity, status, and risk tier."""

    def __init__(self, store: InMemoryCustomerStore) -> None:
        self.store = store
        self._metadata = CapabilityMetadata(
            name="get_customer",
            description="Retrieve customer identity profile, risk level, and account status.",
            input_schema={
                "type": "object",
                "properties": {
                    "customer_id": {
                        "type": "string",
                        "description": "Unique customer identifier (e.g. cust-001)",
                    }
                },
                "required": ["customer_id"],
                "additionalProperties": False,
            },
            side_effect_level=SideEffectLevel.READ_ONLY,
            requires_approval=False,
        )

    @property
    def metadata(self) -> CapabilityMetadata:
        return self._metadata

    async def execute(
        self,
        arguments: Mapping[str, Any],
        context: AiOperationContext | None = None,
    ) -> Any:
        customer_id = str(arguments.get("customer_id", ""))
        cust = self.store.get_customer(customer_id)
        if not cust:
            return {"found": False, "error": f"Customer '{customer_id}' does not exist"}
        return {
            "found": True,
            "customer_id": cust.customer_id,
            "name": cust.name,
            "email": cust.email,
            "risk_level": cust.risk_level,
            "status": cust.status,
        }


class GetAccountStatusCapability(CapabilityPort):
    """Retrieves financial balance, notes, and recent transaction history."""

    def __init__(self, store: InMemoryCustomerStore) -> None:
        self.store = store
        self._metadata = CapabilityMetadata(
            name="get_account_status",
            description="Retrieve customer account balance in cents, currency, operational notes, and recent activity log.",
            input_schema={
                "type": "object",
                "properties": {
                    "customer_id": {"type": "string", "description": "Unique customer identifier"}
                },
                "required": ["customer_id"],
                "additionalProperties": False,
            },
            side_effect_level=SideEffectLevel.READ_ONLY,
            requires_approval=False,
        )

    @property
    def metadata(self) -> CapabilityMetadata:
        return self._metadata

    async def execute(
        self,
        arguments: Mapping[str, Any],
        context: AiOperationContext | None = None,
    ) -> Any:
        customer_id = str(arguments.get("customer_id", ""))
        acc = self.store.get_account(customer_id)
        if not acc:
            return {"found": False, "error": f"Account for customer '{customer_id}' does not exist"}
        return {
            "found": True,
            "customer_id": acc.customer_id,
            "balance_cents": acc.balance_cents,
            "balance_usd": f"${acc.balance_cents / 100:.2f}",
            "notes": list(acc.notes),
            "recent_activity": list(acc.activity_log[-5:]),
            "credits_applied_cents": acc.credits_applied_cents,
        }


class SearchPolicyCapability(CapabilityPort):
    """Searches customer support operational policy documentation."""

    def __init__(self) -> None:
        self._metadata = CapabilityMetadata(
            name="search_policy",
            description="Search operational guidance on fee credits, frozen accounts, dispute handling, and documentation notes.",
            input_schema={
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "Policy query keyword or topic (e.g. credit_limit, frozen_account, customer_notes, dispute_resolution)",
                    }
                },
                "required": ["topic"],
                "additionalProperties": False,
            },
            side_effect_level=SideEffectLevel.READ_ONLY,
            requires_approval=False,
        )

    @property
    def metadata(self) -> CapabilityMetadata:
        return self._metadata

    async def execute(
        self,
        arguments: Mapping[str, Any],
        context: AiOperationContext | None = None,
    ) -> Any:
        topic = str(arguments.get("topic", "")).lower()
        matched = []
        for key, text in POLICIES.items():
            if key in topic or topic in key or any(word in text.lower() for word in topic.split()):
                matched.append({"policy_topic": key, "guideline": text})

        if not matched:
            return {
                "found": False,
                "message": f"No specific operational policy matched '{topic}'. Available topics: {list(POLICIES.keys())}",
            }
        return {"found": True, "results": matched}


class UpdateCustomerNoteCapability(CapabilityPort):
    """Appends an operational note to customer account history (state-mutating)."""

    def __init__(self, store: InMemoryCustomerStore) -> None:
        self.store = store
        self._metadata = CapabilityMetadata(
            name="update_customer_note",
            description="Append an official administrative note to the customer account history. Mutates state.",
            input_schema={
                "type": "object",
                "properties": {
                    "customer_id": {"type": "string", "description": "Customer identifier"},
                    "note": {
                        "type": "string",
                        "description": "Note content explaining action or context",
                    },
                    "action_id": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Idempotency key uniquely identifying this action proposal",
                    },
                },
                "required": ["customer_id", "note", "action_id"],
                "additionalProperties": False,
            },
            side_effect_level=SideEffectLevel.STATE_MUTATING,
            requires_approval=True,  # Mandatory Phase 6 HITL governance: all mutations require approval
        )

    @property
    def metadata(self) -> CapabilityMetadata:
        return self._metadata

    async def execute(
        self,
        arguments: Mapping[str, Any],
        context: AiOperationContext | None = None,
    ) -> Any:
        customer_id = str(arguments.get("customer_id", ""))
        note = str(arguments.get("note", ""))
        action_id = str(arguments.get("action_id", ""))

        if not note.strip():
            return {"success": False, "error": "Note cannot be empty"}

        ok = self.store.add_note(customer_id, note)
        if not ok:
            return {"success": False, "error": f"Customer '{customer_id}' not found"}
        return {
            "success": True,
            "customer_id": customer_id,
            "action_id": action_id,
            "added_note": note,
            "status": "persisted",
        }


class ApplyFeeCreditCapability(CapabilityPort):
    """Credits customer balance (financial state mutation, always requires HITL approval)."""

    def __init__(self, store: InMemoryCustomerStore) -> None:
        self.store = store
        self._metadata = CapabilityMetadata(
            name="apply_fee_credit",
            description="Disburse a fee concession or billing adjustment credit to customer balance. Financial mutation, always requires approval.",
            input_schema={
                "type": "object",
                "properties": {
                    "customer_id": {"type": "string", "description": "Customer identifier"},
                    "amount_cents": {
                        "type": "integer",
                        "description": "Credit amount in integer cents (e.g. 1500 for $15.00)",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Justification for the credit adjustment",
                    },
                    "action_id": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Idempotency key uniquely identifying this transaction",
                    },
                },
                "required": ["customer_id", "amount_cents", "reason", "action_id"],
                "additionalProperties": False,
            },
            side_effect_level=SideEffectLevel.STATE_MUTATING,
            requires_approval=True,  # Mandatory human-in-the-loop approval gate
        )

    @property
    def metadata(self) -> CapabilityMetadata:
        return self._metadata

    async def execute(
        self,
        arguments: Mapping[str, Any],
        context: AiOperationContext | None = None,
    ) -> Any:
        customer_id = str(arguments.get("customer_id", ""))
        amount_cents = int(arguments.get("amount_cents", 0))
        reason = str(arguments.get("reason", ""))
        action_id = str(arguments.get("action_id", ""))

        if amount_cents <= 0:
            return {"success": False, "error": "Credit amount must be greater than zero"}

        try:
            ok = self.store.apply_credit(customer_id, amount_cents, reason)
            if not ok:
                return {"success": False, "error": f"Customer '{customer_id}' not found"}

            acc = self.store.get_account(customer_id)
            new_balance = acc.balance_cents if acc else 0
            return {
                "success": True,
                "action_id": action_id,
                "customer_id": customer_id,
                "credited_amount_cents": amount_cents,
                "credited_amount_usd": f"${amount_cents / 100:.2f}",
                "new_balance_cents": new_balance,
                "new_balance_usd": f"${new_balance / 100:.2f}",
                "reason": reason,
                "status": "applied",
            }
        except Exception as exc:  # noqa: BLE001 -- tool boundary returns a structured failure.
            return {"success": False, "error": str(exc)}
