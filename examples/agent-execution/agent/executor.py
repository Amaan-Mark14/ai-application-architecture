"""Centralized, bounded tool execution with request-bound in-memory replay."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, Literal

from contracts.agent import CapabilityPort, ExecutionReceipt, SideEffectLevel
from contracts.telemetry import AiOperationContext

from .observation import TRUNCATION_MARKER, bounded_json, bounded_text, exception_message
from .schema import validate_value


@dataclass(frozen=True)
class ToolExecutionOutcome:
    """Host-owned dispatch evidence; tool-returned flags cannot set its kind."""

    receipt: ExecutionReceipt
    output: Any
    kind: Literal["executed", "replayed", "rejected"]
    failure_stage: str | None = None
    error_type: str | None = None


class ToolExecutionCancelled(asyncio.CancelledError):
    """Propagate cancellation together with evidence of a dispatched tool."""

    def __init__(self, outcome: ToolExecutionOutcome) -> None:
        super().__init__("Tool execution cancelled")
        self.outcome = outcome


@dataclass(frozen=True)
class _CachedAction:
    request: str
    capability: CapabilityPort
    receipt: ExecutionReceipt
    result: Any
    failure_stage: str | None = None
    error_type: str | None = None


class ToolExecutor:
    """Serialize mutations and bind retries to their original request and scope.

    Cache lifetime is this executor's lifetime. A timed-out/failed mutation is
    retained because failure does not prove that the tool made no changes.
    """

    def __init__(self, timeout_seconds: float = 10.0, max_result_chars: int = 16_000) -> None:
        if type(max_result_chars) is not int or max_result_chars < len(TRUNCATION_MARKER):
            raise ValueError("max_result_chars must accommodate a truncation marker")
        self.timeout_seconds = timeout_seconds
        self.max_result_chars = max_result_chars
        self._idempotency_cache: dict[str, _CachedAction] = {}
        self._mutation_lock = asyncio.Lock()
        self.execution_history: list[ExecutionReceipt] = []

    async def execute_tool(
        self,
        capability: CapabilityPort,
        arguments: Mapping[str, Any],
        action_id: str | None = None,
        context: AiOperationContext | None = None,
        execution_scope: str = "executor-local",
    ) -> tuple[ExecutionReceipt, Any]:
        """Compatibility entry point for callers that only need a receipt/result pair."""
        outcome = await self.execute_action(
            capability, arguments, action_id, context, execution_scope
        )
        return outcome.receipt, outcome.output

    async def execute_action(
        self,
        capability: CapabilityPort,
        arguments: Mapping[str, Any],
        action_id: str | None = None,
        context: AiOperationContext | None = None,
        execution_scope: str = "executor-local",
    ) -> ToolExecutionOutcome:
        """Execute or replay; reject conflicting identities without dispatch."""
        meta = capability.metadata
        mutating = meta.side_effect_level == SideEffectLevel.STATE_MUTATING
        supplied_id = arguments.get("action_id")
        effective_id = action_id if action_id is not None else supplied_id
        if effective_id is None and not mutating:
            effective_id = f"act-{time.time_ns()}"
        if not isinstance(effective_id, str) or not effective_id.strip():
            return self._rejected(meta.name, "", "A non-empty string action_id is required")
        if supplied_id is not None and supplied_id != effective_id:
            return self._rejected(
                meta.name, effective_id, "Conflicting argument and executor action_id"
            )

        errors = validate_value({}, arguments)
        if errors:
            return self._rejected(meta.name, effective_id, "; ".join(errors))
        # Snapshot before awaiting: neither a caller nor a tool can change cache identity.
        arguments_json = json.dumps(
            arguments, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        request = json.dumps(
            {
                "tool": meta.name,
                "arguments": json.loads(arguments_json),
                "scope": execution_scope,
                "trace_id": context.trace_id if context else None,
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

        if mutating:
            async with self._mutation_lock:
                return await self._dispatch(
                    capability, arguments_json, effective_id, request, context
                )
        return await self._dispatch(capability, arguments_json, effective_id, request, context)

    @staticmethod
    def _rejected(name: str, action_id: str, message: str) -> ToolExecutionOutcome:
        receipt = ExecutionReceipt(
            action_id=action_id,
            capability_name=name,
            status="failed",
            result_summary="Rejected before execution",
            error_message=message,
        )
        return ToolExecutionOutcome(
            receipt, {"error": True, "message": message, "executed": False}, "rejected"
        )

    async def _dispatch(
        self,
        capability: CapabilityPort,
        arguments_json: str,
        action_id: str,
        request: str,
        context: AiOperationContext | None,
    ) -> ToolExecutionOutcome:
        meta = capability.metadata
        cached = self._idempotency_cache.get(action_id)
        if cached:
            if cached.request != request or cached.capability is not capability:
                return self._rejected(
                    meta.name, action_id, "action_id conflicts with its original request"
                )
            return ToolExecutionOutcome(
                cached.receipt,
                {
                    "idempotent_replay": True,
                    "executed": False,
                    "original_receipt": asdict(cached.receipt),
                    "cached_result": json.loads(json.dumps(cached.result)),
                },
                "replayed",
                cached.failure_stage,
                cached.error_type,
            )

        started = time.time()
        cancelled = False
        failure_stage = None
        error_type = None
        stage = "tool_execution"
        try:
            result = await asyncio.wait_for(
                capability.execute(json.loads(arguments_json), context=context),
                timeout=self.timeout_seconds,
            )
            error = None
            if isinstance(result, dict) and result.get("error"):
                detail = result.get("message") or result["error"]
                error = (
                    bounded_text(detail, 1024)
                    if isinstance(detail, str)
                    else "Tool reported an error"
                )
            stage = "result_serialization"
            preview, truncated = bounded_json(result, self.max_result_chars)
            result = (
                {"truncated": True, "preview": preview, "error": bool(error)}
                if truncated
                else json.loads(preview)
            )
        except TimeoutError:
            failure_stage, error_type = stage, "TimeoutError"
            error = "Operation timed out"
            result = {
                "error": True,
                "message": f"Execution timed out after {self.timeout_seconds} seconds",
            }
        except asyncio.CancelledError:
            failure_stage, error_type = stage, "CancelledError"
            cancelled = True
            error = "Execution cancelled; mutation outcome may be unknown"
            result = {"error": True, "message": error}
        except Exception as exc:  # noqa: BLE001 -- arbitrary tool failures become execution receipts.
            failure_stage, error_type = stage, type(exc).__name__
            error = exception_message(exc)
            result = {"error": True, "message": f"Tool execution failed: {error}"}

        receipt = ExecutionReceipt(
            action_id=action_id,
            capability_name=meta.name,
            status="failed" if error else "succeeded",
            executed_at=started,
            result_summary=f"Executed {meta.name} ({'failed' if error else 'succeeded'})",
            error_message=error,
        )
        if meta.side_effect_level == SideEffectLevel.STATE_MUTATING:
            self._idempotency_cache[action_id] = _CachedAction(
                request,
                capability,
                receipt,
                json.loads(json.dumps(result)),
                failure_stage,
                error_type,
            )
        self.execution_history.append(receipt)
        outcome = ToolExecutionOutcome(receipt, result, "executed", failure_stage, error_type)
        if cancelled:
            raise ToolExecutionCancelled(outcome)
        return outcome
