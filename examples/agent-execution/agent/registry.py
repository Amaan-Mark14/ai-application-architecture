"""Application-controlled capability registry and argument schema validation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from contracts.agent import CapabilityMetadata, CapabilityPort, SideEffectLevel
from contracts.validation import ValidationResult

from .schema import validate_schema, validate_value


class CapabilityRegistry:
    """Application-controlled registry enforcing tool allowlisting and parameter validation.

    Protects against arbitrary code execution by ensuring the model may only select
    from explicitly registered, approved capabilities.
    """

    def __init__(self, capabilities: Sequence[CapabilityPort] | None = None) -> None:
        self._capabilities: dict[str, CapabilityPort] = {}
        self._schemas: dict[str, Mapping[str, Any]] = {}
        if capabilities:
            for cap in capabilities:
                self.register(cap)

    def register(self, capability: CapabilityPort) -> None:
        """Register an application capability. Rejects duplicate identifiers."""
        name = capability.metadata.name
        if name in self._capabilities:
            raise ValueError(f"Capability '{name}' is already registered in registry")
        validate_schema(capability.metadata.input_schema)
        schema = deepcopy(capability.metadata.input_schema)
        self._capabilities[name] = capability
        self._schemas[name] = schema

    def is_registered(self, name: str) -> bool:
        """Check if capability identifier exists in allowlisted registry."""
        return name in self._capabilities

    def get(self, name: str) -> CapabilityPort | None:
        """Resolve capability by name."""
        return self._capabilities.get(name)

    def list_capabilities(self) -> Sequence[CapabilityMetadata]:
        """List all capability metadata registered in the application."""
        return [cap.metadata for cap in self._capabilities.values()]

    def get_tool_definitions_prompt(self) -> str:
        """Generate human-readable, model-facing capability specifications for reasoning context."""
        lines: list[str] = []
        for meta in self.list_capabilities():
            schema = self._schemas[meta.name]
            lines.append(f"• Tool: {meta.name}")
            lines.append(f"  Description: {meta.description}")
            lines.append(f"  Side Effect: {meta.side_effect_level.value.upper()}")
            lines.append(f"  Approval Required: {meta.requires_approval}")
            lines.append(f"  Required Arguments: {schema.get('required', [])}")
            lines.append(f"  Parameters Schema: {schema.get('properties', {})}")
            lines.append("")
        return "\n".join(lines).strip()

    def validate_arguments(
        self, capability_name: str, arguments: Mapping[str, Any]
    ) -> ValidationResult[Mapping[str, Any]]:
        """Perform strict deterministic schema validation on model-proposed capability arguments.

        Validates required parameter presence, data types, and prohibits unknown fields.
        """
        capability = self.get(capability_name)
        if not capability:
            return ValidationResult(
                is_valid=False,
                errors=[
                    f"Unknown capability '{capability_name}'. Tool is not present in registered allowlist."
                ],
            )

        errors = validate_value(self._schemas[capability_name], arguments)
        if capability.metadata.side_effect_level == SideEffectLevel.STATE_MUTATING:
            action_id = arguments.get("action_id") if isinstance(arguments, Mapping) else None
            if not isinstance(action_id, str) or not action_id.strip():
                errors.append("Mutating capabilities require a non-empty string action_id")
        return ValidationResult(is_valid=len(errors) == 0, errors=errors)
