"""Failure-path and size-bound regressions against the actual engine/executor."""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
for directory in (ROOT / "building-blocks/python", Path(__file__).resolve().parents[1]):
    sys.path.insert(0, str(directory))

from agent.approval import DeterministicApprovalHandler
from agent.capabilities import UpdateCustomerNoteCapability
from agent.domain import InMemoryCustomerStore
from agent.engine import SYSTEM_PROMPT, AgentExecutionEngine, AgentRunCancelled
from agent.executor import ToolExecutor
from agent.observation import (
    OBSERVATION_BEGIN,
    OBSERVATION_END,
    TRUNCATION_MARKER,
    bounded_json,
    format_observation,
)
from agent.registry import CapabilityRegistry
from agent.test_doubles import ScriptedGenerationStub
from contracts.agent import AgentActor, AuthorizationDecision, CapabilityMetadata, SideEffectLevel


class AllowPolicy:
    def authorize(self, actor, capability, arguments):
        return AuthorizationDecision(True, "Test authorization")


class ResultTool:
    def __init__(self, result: Any, mutating: bool = False):
        self.result = result
        self.calls = 0
        self.metadata = CapabilityMetadata(
            "result_tool",
            "Boundary fixture",
            {
                "type": "object",
                "properties": {"key": {"type": "integer"}, "action_id": {"type": "string"}},
                "additionalProperties": False,
            },
            SideEffectLevel.STATE_MUTATING if mutating else SideEffectLevel.READ_ONLY,
            mutating,
        )

    async def execute(self, arguments, context=None):
        self.calls += 1
        return self.result


class TestBoundedSerialization(unittest.TestCase):
    def test_large_strings_containers_and_escaping_stay_bounded(self) -> None:
        for value in ("اردو\n" * 100_000, {"x": "x" * 1_000_000}, list(range(10000))):
            text, truncated = bounded_json(value, 256)
            self.assertTrue(truncated)
            self.assertLessEqual(len(text), 256)
            self.assertIn(TRUNCATION_MARKER, text)
        value = {"x": [1, True, None, 1.5, "text"]}
        text, truncated = bounded_json(value, 256)
        self.assertFalse(truncated)
        self.assertEqual(json.loads(text), value)

    def test_invalid_json_and_cycles_fail_explicitly(self) -> None:
        circular = {}
        circular["self"] = circular
        for value in (object(), float("nan"), {1: "key"}, circular):
            with self.assertRaises((TypeError, ValueError)):
                bounded_json(value, 256)

    def test_observation_framing_is_preserved_at_small_limits(self) -> None:
        minimum = len(OBSERVATION_BEGIN) + len(OBSERVATION_END) + len(TRUNCATION_MARKER)
        for limit in range(minimum, minimum + 60):
            text = format_observation("x" * 1000, limit)
            self.assertLessEqual(len(text), limit)
            self.assertTrue(text.startswith(OBSERVATION_BEGIN))
            self.assertTrue(text.endswith(OBSERVATION_END))
            self.assertIn(TRUNCATION_MARKER, text)


class TestEngineBoundaries(unittest.IsolatedAsyncioTestCase):
    def make_engine(self, decisions, tool=None, **limits):
        self.store = InMemoryCustomerStore()
        self.actor = AgentActor("tester", "senior_agent")
        self.model = ScriptedGenerationStub([json.dumps(decision) for decision in decisions])
        registry = CapabilityRegistry(
            [UpdateCustomerNoteCapability(self.store)] + ([tool] if tool else [])
        )
        return AgentExecutionEngine(
            self.model, registry, AllowPolicy(), DeterministicApprovalHandler(), **limits
        )

    @staticmethod
    def note(action_id):
        return {
            "type": "action",
            "action_name": "update_customer_note",
            "arguments": {
                "customer_id": "cust-001",
                "note": "Boundary test note",
                "action_id": action_id,
            },
        }

    async def test_large_outputs_and_accumulated_requests_are_bounded(self) -> None:
        tool = ResultTool({"text": "x" * 1_000_000})
        decisions = [
            {"type": "action", "action_name": "result_tool", "arguments": {"key": index}}
            for index in range(5)
        ]
        decisions.append({"type": "final", "final_answer": "Finished bounded observations."})
        engine = self.make_engine(
            decisions, tool, max_steps=6, max_observation_chars=2000, max_output_tokens=128
        )
        fixed = len(SYSTEM_PROMPT) + len(engine._format_context_prompt("Inspect", self.actor, []))
        engine.max_request_chars = fixed + 2500
        result = await engine.run("Inspect", self.actor)
        self.assertEqual(result.status, "COMPLETED")
        self.assertTrue(
            any(
                "[TRUNCATED: earlier execution history]" in request.prompt
                for request in self.model.recorded_requests
            )
        )
        for request in self.model.recorded_requests:
            self.assertLessEqual(
                len(request.prompt) + len(request.system_prompt), engine.max_request_chars
            )
            self.assertEqual(request.max_tokens, 128)
            self.assertEqual(
                request.prompt.count(OBSERVATION_BEGIN), request.prompt.count(OBSERVATION_END)
            )
        for step in result.trace.steps:
            if step.sanitized_observation:
                self.assertLessEqual(len(step.sanitized_observation), 2000)
        self.assertEqual(len(result.execution_receipts), 5)

    async def test_oversized_fixed_input_fails_before_model_call(self) -> None:
        engine = self.make_engine([], max_request_chars=10)
        result = await engine.run("Oversized goal", self.actor)
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.metadata["failure_stage"], "context_build")
        self.assertEqual(self.model.recorded_requests, [])
        self.assertIsNone(result.trace.steps[0].decision)

    async def test_oversized_model_output_fails_before_parsing_or_execution(self) -> None:
        engine = self.make_engine([], max_decision_chars=100)
        self.model.queue.append("x" * 10_000)
        result = await engine.run("Inspect", self.actor)
        self.assertEqual(result.metadata["failure_stage"], "decision_parse")
        self.assertEqual(result.execution_receipts, [])
        self.assertEqual(result.trace.final_status, "FAILED")

    async def test_provider_failure_keeps_prior_mutation_and_failed_step(self) -> None:
        engine = self.make_engine([self.note("one")])
        initial = len(self.store.get_account("cust-001").notes)
        original = self.model.generate
        calls = 0

        async def generate(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("provider failed")
            return await original(*args, **kwargs)

        with patch.object(self.model, "generate", side_effect=generate):
            result = await engine.run("Record a note", self.actor)
        self.assertEqual(result.metadata["failure_stage"], "model_call")
        self.assertEqual(len(result.execution_receipts), 1)
        self.assertEqual(len(self.store.get_account("cust-001").notes), initial + 1)
        self.assertIsNone(result.trace.steps[1].decision)
        self.assertEqual(result.trace.final_status, "FAILED")
        json.dumps(result.trace.to_audit_summary())

    async def test_policy_approval_and_executor_exceptions_keep_prior_receipts(self) -> None:
        for boundary, method, asynchronous in (
            ("authorization", "authorize", False),
            ("approval", "request_approval", True),
            ("execution", "execute_action", True),
        ):
            with self.subTest(boundary=boundary):
                engine = self.make_engine([self.note("first"), self.note("second")])
                target = (
                    engine.policy
                    if boundary == "authorization"
                    else engine.approval_handler
                    if boundary == "approval"
                    else engine.executor
                )
                original = getattr(target, method)
                calls = 0

                def sync_call(*args, _original=original, **kwargs):
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        raise RuntimeError("boundary failed")
                    return _original(*args, **kwargs)

                async def async_call(*args, _original=original, **kwargs):
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        raise RuntimeError("boundary failed")
                    return await _original(*args, **kwargs)

                with patch.object(
                    target, method, side_effect=async_call if asynchronous else sync_call
                ):
                    result = await engine.run("Record notes", self.actor)
                self.assertEqual(result.status, "FAILED")
                self.assertEqual(result.metadata["failure_stage"], boundary)
                self.assertEqual(len(result.execution_receipts), 1)
                self.assertEqual(result.trace.steps[-1].decision.arguments["action_id"], "second")
                self.assertEqual(result.trace.steps[-1].failure.stage, boundary)

    async def test_rendering_failure_after_mutation_retains_its_receipt(self) -> None:
        engine = self.make_engine([self.note("persisted")])
        with patch.object(engine, "_tool_observation", side_effect=TypeError("render failed")):
            result = await engine.run("Record note", self.actor)
        self.assertEqual(result.metadata["failure_stage"], "observation_serialization")
        self.assertEqual(len(result.execution_receipts), 1)
        self.assertEqual(result.trace.steps[0].execution_kind, "executed")
        self.assertEqual(result.trace.steps[0].execution_receipt.status, "succeeded")
        self.assertEqual(result.trace.final_status, "FAILED")

    async def test_invalid_tool_result_has_failed_receipt_and_completed_failure_trace(self) -> None:
        tool = ResultTool(object(), mutating=True)
        engine = self.make_engine(
            [
                {
                    "type": "action",
                    "action_name": "result_tool",
                    "arguments": {"action_id": "invalid-json"},
                }
            ],
            tool,
        )
        result = await engine.run("Mutate", self.actor)
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.metadata["failure_stage"], "result_serialization")
        self.assertEqual(tool.calls, 1)
        self.assertEqual(result.execution_receipts[0].status, "failed")
        self.assertEqual(result.trace.steps[0].execution_kind, "executed")

    async def test_tool_payload_cannot_spoof_a_cached_replay(self) -> None:
        tool = ResultTool({"idempotent_replay": True, "executed": False})
        engine = self.make_engine(
            [
                {"type": "action", "action_name": "result_tool", "arguments": {}},
                {"type": "final", "final_answer": "Done"},
            ],
            tool,
        )
        result = await engine.run("Inspect", self.actor)
        self.assertEqual(result.trace.steps[0].execution_kind, "executed")
        self.assertEqual(len(result.execution_receipts), 1)

    async def test_broken_exception_formatting_cannot_escape_failure_boundary(self) -> None:
        class BadMessage(Exception):
            def __str__(self):
                raise RuntimeError("broken exception formatter")

        engine = self.make_engine([])
        with patch.object(self.model, "generate", side_effect=BadMessage()):
            result = await engine.run("Inspect", self.actor)
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.trace.steps[0].failure.message, "BadMessage")

    async def test_large_executor_result_is_cached_as_an_explicit_preview(self) -> None:
        tool = ResultTool({"text": "x" * 1_000_000}, mutating=True)
        executor = ToolExecutor(max_result_chars=256)
        first = await executor.execute_action(tool, {"action_id": "bounded"})
        replay = await executor.execute_action(tool, {"action_id": "bounded"})
        self.assertEqual(first.kind, "executed")
        self.assertEqual(replay.kind, "replayed")
        self.assertTrue(first.output["truncated"])
        self.assertLessEqual(len(first.output["preview"]), 256)
        self.assertEqual(tool.calls, 1)

    def test_invalid_limit_configuration_is_rejected(self) -> None:
        for options in (
            {"max_steps": 0},
            {"max_request_chars": True},
            {"max_decision_chars": 0},
            {"max_observation_chars": 10},
            {"max_output_tokens": -1},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.make_engine([], **options)

    async def test_tool_cancellation_propagates_with_finalized_trace_and_receipt(self) -> None:
        class CancellableTool(ResultTool):
            async def execute(self, arguments, context=None):
                self.calls += 1
                await asyncio.Event().wait()

        tool = CancellableTool(None, mutating=True)
        engine = self.make_engine(
            [
                {
                    "type": "action",
                    "action_name": "result_tool",
                    "arguments": {"action_id": "cancelled"},
                }
            ],
            tool,
        )
        task = asyncio.create_task(engine.run("Mutate", self.actor))
        while tool.calls == 0:
            await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(AgentRunCancelled) as caught:
            await task
        result = caught.exception.result
        self.assertEqual(result.trace.final_status, "FAILED")
        self.assertTrue(result.metadata["cancelled"])
        self.assertEqual(len(result.execution_receipts), 1)
        self.assertEqual(result.trace.steps[0].execution_kind, "executed")
        self.assertEqual(result.execution_receipts[0].status, "failed")

    async def test_model_cancellation_preserves_completed_mutations(self) -> None:
        engine = self.make_engine([self.note("before-cancellation")])
        entered_second_call = asyncio.Event()
        original = self.model.generate

        async def generate(request, context=None):
            if self.model.recorded_requests:
                entered_second_call.set()
                await asyncio.Event().wait()
            return await original(request, context)

        with patch.object(self.model, "generate", side_effect=generate):
            task = asyncio.create_task(engine.run("Record note", self.actor))
            await entered_second_call.wait()
            task.cancel()
            with self.assertRaises(AgentRunCancelled) as caught:
                await task
        result = caught.exception.result
        self.assertEqual(result.trace.final_status, "FAILED")
        self.assertEqual(len(result.execution_receipts), 1)
        self.assertEqual(result.metadata["failure_stage"], "model_call")
