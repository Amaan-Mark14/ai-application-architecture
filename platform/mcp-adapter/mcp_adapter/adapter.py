"""Model Context Protocol (MCP) capability adapter conforming to CapabilityPort."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from contextlib import suppress
from typing import Any

from contracts.agent import (
    CapabilityMetadata,
    CapabilityPort,
    SideEffectLevel,
)
from contracts.telemetry import AiOperationContext

from .client import McpClient, McpClientError


class McpCapabilityAdapter(CapabilityPort):
    """Adapts an MCP-hosted tool into an application CapabilityPort with allowlisting enforcement."""

    def __init__(
        self,
        client: McpClient,
        tool_name: str,
        description: str | None = None,
        input_schema: Mapping[str, Any] | None = None,
        side_effect_level: SideEffectLevel | None = None,
        requires_approval: bool | None = None,
        allowlist: Sequence[str] | None = None,
    ) -> None:
        self.client = client
        self.tool_name = tool_name
        self.allowlist = set(allowlist) if allowlist is not None else None

        # 1. Enforce allowlisting boundary: reject unapproved tools
        if self.allowlist is not None and self.tool_name not in self.allowlist:
            raise ValueError(
                f"Security violation: MCP tool '{self.tool_name}' is not present in permitted allowlist {sorted(self.allowlist)}"
            )
        if self.allowlist is None:
            raise ValueError("An explicit host-side MCP tool allowlist is required")
        if not isinstance(side_effect_level, SideEffectLevel):
            raise ValueError(  # noqa: TRY004 -- missing trusted integration configuration.
                f"MCP tool '{tool_name}' requires an explicit host-side side-effect classification"
            )
        mutating = side_effect_level == SideEffectLevel.STATE_MUTATING
        if requires_approval is not None and type(requires_approval) is not bool:
            raise ValueError("requires_approval must be a boolean")
        if mutating and requires_approval is False:
            raise ValueError("Mutating MCP tools must require approval")

        # 2. Derive metadata from server discovery if not explicitly supplied
        desc = description
        schema = input_schema

        if desc is None or schema is None:
            available_tools = self.client.list_tools()
            matching = [t for t in available_tools if t.get("name") == self.tool_name]
            if not matching:
                raise ValueError(
                    f"Tool '{self.tool_name}' was not found on the connected MCP server"
                )
            tool_spec = matching[0]
            desc = desc or tool_spec.get("description", f"MCP capability: {self.tool_name}")
            schema = schema or tool_spec.get("inputSchema", {"type": "object"})

        self._metadata = CapabilityMetadata(
            name=self.tool_name,
            description=desc,
            input_schema=schema,
            side_effect_level=side_effect_level,
            requires_approval=mutating if requires_approval is None else requires_approval,
        )

    @property
    def metadata(self) -> CapabilityMetadata:
        return self._metadata

    async def execute(
        self,
        arguments: Mapping[str, Any],
        context: AiOperationContext | None = None,
    ) -> Any:
        """Invoke tool over MCP protocol transport without blocking the asyncio event loop."""
        try:
            raw_response = await asyncio.to_thread(self.client.call_tool, self.tool_name, arguments)
            is_error = raw_response.get("isError", False)
            content_items = raw_response.get("content", [])

            extracted_texts: list[str] = []
            for item in content_items:
                if isinstance(item, dict) and item.get("type") == "text":
                    extracted_texts.append(item.get("text", ""))

            full_text = "\n".join(extracted_texts).strip()

            # Attempt to decode JSON content if structured
            if full_text.startswith(("{", "[")):
                try:
                    parsed_payload = json.loads(full_text)
                    if is_error:
                        return {"error": True, "detail": parsed_payload}
                    return parsed_payload
                except json.JSONDecodeError:
                    pass

            if is_error:
                return {"error": True, "message": full_text or "MCP tool reported execution error"}

            return {"output": full_text}
        except (TimeoutError, asyncio.CancelledError):
            # When cancelled by caller (e.g. ToolExecutor timeout), cleanly close process
            with suppress(OSError):
                self.client.close()
            raise
        except McpClientError as err:
            return {"error": True, "message": f"MCP communication failure: {err!s}"}
        except Exception as exc:  # noqa: BLE001 -- external tool boundary returns structured failures.
            return {
                "error": True,
                "message": f"Unexpected error executing MCP tool '{self.tool_name}': {exc!s}",
            }


def create_mcp_capabilities(
    client: McpClient,
    allowlist: Sequence[str],
    side_effect_levels: Mapping[str, SideEffectLevel],
) -> Sequence[CapabilityPort]:
    """Discover all tools from an MCP server and wrap permitted tools in CapabilityPort instances."""
    allowed_set = set(allowlist)
    for allowed_name in allowed_set:
        if not isinstance(side_effect_levels.get(allowed_name), SideEffectLevel):
            raise ValueError(  # noqa: TRY004 -- missing trusted integration configuration.
                f"MCP tool '{allowed_name}' requires an explicit host-side side-effect classification"
            )
    client.initialize()
    available = client.list_tools()

    adapters: list[CapabilityPort] = []
    for tool_spec in available:
        name = tool_spec.get("name")
        if not isinstance(name, str) or not name or name not in allowed_set:
            continue

        adapter = McpCapabilityAdapter(
            client=client,
            tool_name=name,
            description=tool_spec.get("description"),
            input_schema=tool_spec.get("inputSchema"),
            side_effect_level=side_effect_levels[name],
            allowlist=allowlist,
        )
        adapters.append(adapter)

    return adapters
