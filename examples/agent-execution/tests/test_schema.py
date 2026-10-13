"""Boundary regressions for supported declarations and recursive value validation."""

from __future__ import annotations

import sys
import unittest
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for directory in (ROOT / "building-blocks/python", Path(__file__).resolve().parents[1]):
    sys.path.insert(0, str(directory))

from agent.registry import CapabilityRegistry
from agent.schema import validate_schema, validate_value
from contracts.agent import CapabilityMetadata, SideEffectLevel

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["order"],
    "properties": {
        "order": {
            "type": "object",
            "required": ["items"],
            "additionalProperties": False,
            "properties": {
                "items": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 2,
                    "items": {
                        "type": "object",
                        "required": ["amount", "kind"],
                        "additionalProperties": False,
                        "properties": {
                            "amount": {"type": "number", "minimum": 1, "maximum": 50},
                            "kind": {"type": "string", "enum": ["credit", "note"]},
                        },
                    },
                },
            },
        },
        "label": {"type": "string", "minLength": 1, "maxLength": 8},
    },
}


class TestSchema(unittest.TestCase):
    def test_valid_nested_value_and_boundaries(self) -> None:
        validate_schema(SCHEMA)
        for amount in (1, 50, 1.5):
            self.assertEqual(
                validate_value(
                    SCHEMA, {"order": {"items": [{"amount": amount, "kind": "credit"}]}}
                ),
                [],
            )

    def test_nested_values_enforce_all_declared_constraints(self) -> None:
        cases = [
            ({}, "order"),
            ({"order": {}}, "items"),
            ({"order": {"items": []}}, "minItems"),
            ({"order": {"items": [1]}}, "arguments.order.items[0]"),
            ({"order": {"items": [{"amount": True, "kind": "credit"}]}}, "number"),
            ({"order": {"items": [{"amount": 0, "kind": "credit"}]}}, "minimum"),
            ({"order": {"items": [{"amount": 51, "kind": "credit"}]}}, "maximum"),
            ({"order": {"items": [{"amount": 1, "kind": "delete"}]}}, "one of"),
            ({"order": {"items": [{"amount": 1, "kind": "credit", "override": True}]}}, "override"),
        ]
        for value, error in cases:
            with self.subTest(value=value):
                self.assertIn(error, " ".join(validate_value(SCHEMA, value)))

    def test_scalar_array_and_length_rules(self) -> None:
        cases = [
            ({"type": "integer"}, True),
            ({"type": "integer"}, 1.0),
            ({"type": "boolean"}, 1),
            ({"type": "null"}, "null"),
            ({"type": "string", "minLength": 1}, ""),
            ({"type": "string", "maxLength": 2}, "abc"),
            ({"type": "array", "maxItems": 1}, [1, 2]),
            ({"type": "array"}, (1,)),
            ({"type": "number", "exclusiveMinimum": 1}, 1),
            ({"type": "number", "exclusiveMaximum": 1}, 1),
        ]
        for child, value in cases:
            schema = {"type": "object", "properties": {"x": child}}
            validate_schema(schema)
            with self.subTest(child=child, value=value):
                self.assertTrue(validate_value(schema, {"x": value}))

    def test_malformed_and_unsupported_declarations_fail_registration(self) -> None:
        cases = [
            {"type": "object", "$ref": "#/anything"},
            {"type": "object", "properties": {"x": {"type": "string", "pattern": "x"}}},
            {"type": "object", "required": "x"},
            {"type": "object", "required": ["undeclared"]},
            {"type": "object", "additionalProperties": {}},
            {"type": "array"},
            {"type": ["object", "null"]},
            {"type": "object", "properties": []},
            {"type": "object", "properties": {"x": False}},
            {"type": "object", "properties": {"x": {"type": "number", "minimum": True}}},
            {"type": "object", "properties": {"x": {"type": "string", "enum": [1]}}},
            {"type": "object", "properties": {"x": {"type": "string", "minLength": -1}}},
            {
                "type": "object",
                "properties": {"x": {"type": "string", "minLength": 4, "maxLength": 1}},
            },
            {"type": "object", "properties": {"x": {"type": "number", "enum": [1, 1]}}},
            {"type": "object", "properties": {"x": {"type": None}}},
        ]
        for schema in cases:
            with self.subTest(schema=schema), self.assertRaises(ValueError):
                CapabilityRegistry([SchemaTool(schema)])

    def test_non_json_deep_and_circular_values_rejected(self) -> None:
        circular = {}
        circular["self"] = circular
        for value in (
            {"x": float("nan")},
            {"x": float("inf")},
            {1: "value"},
            {"x": object()},
            circular,
        ):
            with self.subTest(value=type(value)):
                self.assertTrue(validate_value({}, value))
        with self.assertRaises(ValueError):
            validate_schema(circular)

    def test_registered_schema_is_a_snapshot(self) -> None:
        schema = deepcopy(SCHEMA)
        registry = CapabilityRegistry([SchemaTool(schema)])
        schema["required"] = []
        self.assertFalse(registry.validate_arguments("schema_tool", {}).is_valid)


class SchemaTool:
    def __init__(self, schema):
        self.metadata = CapabilityMetadata(
            "schema_tool", "Schema test tool", schema, SideEffectLevel.READ_ONLY
        )

    async def execute(self, arguments, context=None):
        return arguments
