"""Small, enforced JSON Schema subset for capability arguments.

Schema declarations fail closed at registration; values are checked without
coercion before authorization. This is deliberately not a full JSON Schema engine.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

ANNOTATIONS = {"title", "description", "default", "examples"}
TYPE_KEYWORDS = {
    "object": {"properties", "required", "additionalProperties"},
    "array": {"items", "minItems", "maxItems"},
    "string": {"minLength", "maxLength", "enum"},
    "integer": {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "enum"},
    "number": {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "enum"},
    "boolean": {"enum"},
    "null": {"enum"},
}
MAX_DEPTH = 64


def _matches_type(value: Any, kind: str) -> bool:
    if kind == "number":
        return type(value) is int or (type(value) is float and math.isfinite(value))
    types = {
        "object": dict,
        "array": list,
        "string": str,
        "integer": int,
        "boolean": bool,
        "null": type(None),
    }
    return type(value) is types[kind]


def _check_json(value: Any, path: str, depth: int = 0) -> None:
    """Reject Python-only values, nonfinite numbers and circular/deep structures."""
    if depth > MAX_DEPTH:
        raise ValueError(f"{path}: JSON nesting exceeds {MAX_DEPTH}")
    if type(value) is dict:
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path}: JSON object keys must be strings")  # noqa: TRY004 -- invalid JSON data.
            _check_json(item, f"{path}.{key}", depth + 1)
    elif type(value) is list:
        for index, item in enumerate(value):
            _check_json(item, f"{path}[{index}]", depth + 1)
    elif not (value is None or type(value) in (str, bool) or _matches_type(value, "number")):
        raise ValueError(f"{path}: value must be finite JSON data")


def validate_schema(schema: Mapping[str, Any]) -> None:
    """Reject unsupported, misplaced or malformed declarations, including nested ones."""
    _check_json(schema, "schema")
    if schema.get("type", "object") != "object":
        raise ValueError("schema: capability arguments must have an object root")
    _check_schema(schema, "schema")


def _check_schema(schema: Any, path: str) -> None:
    if not isinstance(schema, dict):
        raise ValueError(f"{path}: must be a schema object")  # noqa: TRY004 -- invalid schema declaration.
    kind = schema.get("type")
    if kind is not None and (not isinstance(kind, str) or kind not in TYPE_KEYWORDS):
        raise ValueError(f"{path}.type: unsupported type")
    allowed = ANNOTATIONS | {"type"} | (TYPE_KEYWORDS[kind] if kind else set())
    unsupported = set(schema) - allowed
    if unsupported:
        raise ValueError(f"{path}: unsupported or misplaced keywords: {sorted(unsupported)}")
    for key in ("title", "description"):
        if key in schema and not isinstance(schema[key], str):
            raise ValueError(f"{path}.{key}: must be a string")
    if "type" in schema and kind is None:
        raise ValueError(f"{path}.type: unsupported type")
    if kind == "object":
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            raise ValueError(f"{path}.properties: must be an object")
        required = schema.get("required", [])
        if (
            not isinstance(required, list)
            or any(not isinstance(key, str) for key in required)
            or len(set(required)) != len(required)
            or set(required) - set(properties)
        ):
            raise ValueError(f"{path}.required: must name unique declared properties")
        if type(schema.get("additionalProperties", True)) is not bool:
            raise ValueError(f"{path}.additionalProperties: must be a boolean")
        for name, child in properties.items():
            _check_schema(child, f"{path}.properties.{name}")
    if kind == "array" and "items" in schema:
        _check_schema(schema["items"], f"{path}.items")
    for low, high in (("minLength", "maxLength"), ("minItems", "maxItems")):
        for key in (low, high):
            if key in schema and (type(schema[key]) is not int or schema[key] < 0):
                raise ValueError(f"{path}.{key}: must be a nonnegative integer")
        if low in schema and high in schema and schema[low] > schema[high]:
            raise ValueError(f"{path}: {low} exceeds {high}")
    for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
        if key in schema and not _matches_type(schema[key], "number"):
            raise ValueError(f"{path}.{key}: must be a finite number")
    if "enum" in schema:
        values = schema["enum"]
        assert isinstance(kind, str)  # Scalar enums require a declared type above.
        if (
            not isinstance(values, list)
            or not values
            or any(not _matches_type(value, kind) for value in values)
        ):
            raise ValueError(f"{path}.enum: must contain values of the declared scalar type")
        if any(value in values[:index] for index, value in enumerate(values)):
            raise ValueError(f"{path}.enum: must contain unique values")


def validate_value(schema: Mapping[str, Any], value: Any) -> list[str]:
    """Return actionable paths for violations of an already validated schema."""
    try:
        _check_json(value, "arguments")
    except ValueError as exc:
        return [str(exc)]
    if not isinstance(value, dict):
        return ["arguments must be an object"]
    errors: list[str] = []
    _check_value(schema, value, "arguments", errors)
    return errors


def _check_value(schema: Mapping[str, Any], value: Any, path: str, errors: list[str]) -> None:
    kind = schema.get("type")
    if kind is None:
        return
    if not _matches_type(value, kind):
        article = "an" if kind in ("integer", "object", "array") else "a"
        errors.append(f"{path} must be {article} {kind}, got {type(value).__name__}")
        return
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path} must be one of {schema['enum']}")
    if kind == "object":
        properties = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"Missing required parameter '{key}' at {path}")
        for key, item in value.items():
            if key in properties:
                _check_value(properties[key], item, f"{path}.{key}", errors)
            elif schema.get("additionalProperties") is False:
                errors.append(f"Unexpected parameter '{key}' at {path}")
    if kind == "array" and "items" in schema:
        for index, item in enumerate(value):
            _check_value(schema["items"], item, f"{path}[{index}]", errors)
    if kind in ("string", "array"):
        low, high = ("minLength", "maxLength") if kind == "string" else ("minItems", "maxItems")
        if low in schema and len(value) < schema[low]:
            errors.append(f"{path} violates {low}={schema[low]}")
        if high in schema and len(value) > schema[high]:
            errors.append(f"{path} violates {high}={schema[high]}")
    if kind in ("number", "integer"):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path} violates minimum={schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path} violates maximum={schema['maximum']}")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            errors.append(f"{path} violates exclusiveMinimum={schema['exclusiveMinimum']}")
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            errors.append(f"{path} violates exclusiveMaximum={schema['exclusiveMaximum']}")
