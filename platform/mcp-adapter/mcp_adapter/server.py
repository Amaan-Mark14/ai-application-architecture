"""Reference Model Context Protocol (MCP) server over standard input/output (stdio).

Pure Python standard library implementation demonstrating the JSON-RPC 2.0 wire protocol.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable, Mapping
from typing import Any

from .protocol import (
    JsonRpcError,
    JsonRpcErrorCode,
    JsonRpcNotification,
    JsonRpcRequest,
    JsonRpcResponse,
)


class McpServer:
    """Standard-library MCP Server providing JSON-RPC 2.0 tool dispatch."""

    def __init__(self, server_name: str = "reference-mcp-server", version: str = "1.0.0") -> None:
        self.server_name = server_name
        self.version = version
        self._tools: dict[str, dict[str, Any]] = {}
        self._handlers: dict[str, Callable[[Mapping[str, Any]], Any]] = {}
        self._initialized = False
        self._initialize_received = False
        self._register_default_tools()

    def _register_default_tools(self) -> None:
        """Register default safe read-only diagnostic tools."""
        self.register_tool(
            name="get_system_status",
            description="Retrieve diagnostic health, uptime, and operational metrics of the service environment.",
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            handler=self._handle_system_status,
        )
        self.register_tool(
            name="ping",
            description="Send a latency ping to verify MCP server responsiveness.",
            input_schema={
                "type": "object",
                "properties": {"echo": {"type": "string", "description": "Optional echo message"}},
                "additionalProperties": False,
            },
            handler=self._handle_ping,
        )

    def register_tool(
        self,
        name: str,
        description: str,
        input_schema: Mapping[str, Any],
        handler: Callable[[Mapping[str, Any]], Any],
    ) -> None:
        """Register a model-callable capability with its input schema and execution handler."""
        self._tools[name] = {
            "name": name,
            "description": description,
            "inputSchema": dict(input_schema),
        }
        self._handlers[name] = handler

    def _handle_system_status(self, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        return {
            "status": "operational",
            "server": self.server_name,
            "version": self.version,
            "timestamp": time.time(),
            "metrics": {
                "active_connections": 1,
                "subsystem_healthy": True,
            },
        }

    def _handle_ping(self, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        return {
            "pong": True,
            "echo": arguments.get("echo", ""),
            "timestamp": time.time(),
        }

    def handle_request(self, request: JsonRpcRequest) -> JsonRpcResponse:
        """Process an individual JSON-RPC 2.0 request and formulate a response."""
        method = request.method
        params = request.params or {}

        if method == "initialize":
            if (
                not isinstance(params.get("capabilities"), dict)
                or not isinstance(params.get("protocolVersion"), str)
                or not isinstance(params.get("clientInfo"), dict)
            ):
                return JsonRpcResponse(
                    id=request.id,
                    error=JsonRpcError(
                        JsonRpcErrorCode.INVALID_PARAMS,
                        "Invalid initialize parameters",
                    ),
                )
            self._initialize_received = True
            self._initialized = False
            return JsonRpcResponse(
                id=request.id,
                result={
                    "protocolVersion": "2024-11-05",
                    "serverInfo": {
                        "name": self.server_name,
                        "version": self.version,
                    },
                    "capabilities": {
                        "tools": {"listChanged": False},
                    },
                },
            )

        if method == "ping":
            return JsonRpcResponse(id=request.id, result={})

        if not self._initialized:
            return JsonRpcResponse(
                id=request.id,
                error=JsonRpcError(
                    JsonRpcErrorCode.INVALID_REQUEST,
                    "MCP initialization is not complete",
                ),
            )

        if method == "tools/list":
            tools_list = list(self._tools.values())
            return JsonRpcResponse(
                id=request.id,
                result={"tools": tools_list},
            )

        if method == "tools/call":
            tool_name = params.get("name")
            tool_args = params.get("arguments", {})

            if not tool_name or not isinstance(tool_name, str):
                return JsonRpcResponse(
                    id=request.id,
                    error=JsonRpcError(
                        code=JsonRpcErrorCode.INVALID_PARAMS,
                        message="Missing or invalid 'name' in tools/call parameters",
                    ),
                )

            if tool_name not in self._handlers:
                return JsonRpcResponse(
                    id=request.id,
                    error=JsonRpcError(
                        code=JsonRpcErrorCode.METHOD_NOT_FOUND,
                        message=f"Tool '{tool_name}' is not registered on this MCP server",
                    ),
                )

            try:
                handler = self._handlers[tool_name]
                result_content = handler(tool_args)
                return JsonRpcResponse(
                    id=request.id,
                    result={
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(result_content)
                                if not isinstance(result_content, str)
                                else result_content,
                            }
                        ],
                        "isError": False,
                    },
                )
            except Exception as exc:  # noqa: BLE001 -- contain external tool handler failures.
                return JsonRpcResponse(
                    id=request.id,
                    result={
                        "content": [
                            {
                                "type": "text",
                                "text": f"Tool execution failed: {type(exc).__name__}: {exc!s}",
                            }
                        ],
                        "isError": True,
                    },
                )

        return JsonRpcResponse(
            id=request.id,
            error=JsonRpcError(
                code=JsonRpcErrorCode.METHOD_NOT_FOUND,
                message=f"Unsupported MCP method: '{method}'",
            ),
        )

    def handle_notification(self, notification: JsonRpcNotification) -> None:
        """Notifications have no response, including unknown methods."""
        if notification.method == "notifications/initialized" and self._initialize_received:
            self._initialized = True

    def process_message(self, line: str) -> str:
        """Process a single JSON-RPC line and return the response line."""
        clean = line.strip()
        if not clean:
            return ""

        try:
            data = json.loads(clean)
            if not isinstance(data, dict):
                raise ValueError("JSON-RPC message must be an object")  # noqa: TRY004 -- malformed wire message.
            if "method" in data and "id" not in data:
                self.handle_notification(JsonRpcNotification.from_dict(data))
                return ""
            request = JsonRpcRequest.from_dict(data)
            response = self.handle_request(request)
            return response.to_json()
        except (ValueError, TypeError, KeyError) as exc:
            err_resp = JsonRpcResponse(
                id=0,
                error=JsonRpcError(
                    code=JsonRpcErrorCode.PARSE_ERROR,
                    message=f"Parse error: {type(exc).__name__}: {exc!s}",
                ),
            )
            return err_resp.to_json()

    def run_stdio(self) -> None:
        """Run standard I/O communication loop reading from stdin and writing to stdout."""
        for line in sys.stdin:
            response_json = self.process_message(line)
            if response_json:
                sys.stdout.write(response_json + "\n")
                sys.stdout.flush()


if __name__ == "__main__":
    server = McpServer()
    server.run_stdio()
