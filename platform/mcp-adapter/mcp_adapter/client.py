"""Bounded MCP stdio client with a serialized JSON-RPC request channel."""

from __future__ import annotations

import json
import math
import os
import queue
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

from .protocol import JsonRpcError, JsonRpcNotification, JsonRpcRequest, JsonRpcResponse
from .server import McpServer

PROTOCOL_VERSION = "2024-11-05"
MAX_MESSAGE_CHARS = 1_048_576


class McpClientError(Exception):
    """MCP transport, protocol or remote request failure."""


class McpClient:
    """One outstanding request at a time; notifications never satisfy a request.

    Stdio deadlines cover initialization, discovery and calls. A failed channel
    is closed so late responses cannot leak into subsequent requests.
    """

    def __init__(
        self,
        server_instance: McpServer | None = None,
        command: Sequence[str] | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        if (server_instance is None) == (command is None):
            raise ValueError("McpClient requires exactly one of server_instance or command")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive and finite")
        self._server_instance = server_instance
        self._command = command
        self.timeout_seconds = timeout_seconds
        self._process: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._writer: threading.Thread | None = None
        self._messages: queue.Queue[str | McpClientError] = queue.Queue(maxsize=256)
        self._stopped = threading.Event()
        self._request_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._initialize_lock = threading.Lock()
        self._initialization: dict[str, Any] | None = None
        self._next_id = 1
        if command is not None:
            self._start_process()

    def _start_process(self) -> None:
        assert self._command is not None
        env = os.environ.copy()
        root = Path(__file__).resolve().parents[3]
        paths = [str(root / "building-blocks" / "python"), str(Path(__file__).resolve().parents[1])]
        if env.get("PYTHONPATH"):
            paths.append(env["PYTHONPATH"])
        env["PYTHONPATH"] = os.pathsep.join(paths)
        self._process = subprocess.Popen(
            list(self._command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        assert self._process.stdout is not None
        self._reader = threading.Thread(
            target=self._read_stdout, args=(self._process.stdout,), daemon=True
        )
        self._reader.start()

    def _read_stdout(self, stream: TextIO) -> None:
        try:
            while not self._stopped.is_set():
                line = stream.readline(MAX_MESSAGE_CHARS + 1)
                if not line:
                    message: str | McpClientError = McpClientError(
                        "MCP server process closed stream without responding"
                    )
                elif len(line) > MAX_MESSAGE_CHARS or not line.endswith("\n"):
                    message = McpClientError("MCP message exceeds framing limit or has no newline")
                else:
                    message = line
                self._enqueue(message)
                if isinstance(message, McpClientError):
                    return
        except (OSError, ValueError, UnicodeError) as exc:
            self._enqueue(McpClientError(f"MCP stdout read failed: {exc}"))

    def _enqueue(self, message: str | McpClientError) -> None:
        while not self._stopped.is_set():
            try:
                self._messages.put(message, timeout=0.1)
                return
            except queue.Full:
                continue

    def _write(self, payload: str, deadline: float | None = None) -> None:
        process = self._process
        if not process or not process.stdin:
            raise McpClientError("MCP server process is not running")
        if len(payload) + 1 > MAX_MESSAGE_CHARS:
            raise McpClientError("Outgoing MCP message exceeds framing limit")
        stream = process.stdin
        completion: queue.Queue[Exception | None] = queue.Queue(maxsize=1)

        def write_message() -> None:
            try:
                stream.write(payload + "\n")
                stream.flush()
                completion.put(None)
            except (OSError, ValueError) as exc:
                completion.put(exc)

        self._writer = threading.Thread(target=write_message, daemon=True)
        self._writer.start()
        remaining = (deadline - time.monotonic()) if deadline is not None else self.timeout_seconds
        try:
            error = completion.get(timeout=max(0.0, remaining))
        except queue.Empty as exc:
            raise McpClientError("MCP request timed out writing to the server") from exc
        if error is not None:
            raise McpClientError(f"MCP stdin write failed: {error}") from error
        self._writer.join()

    def _send_request(
        self, method: str, params: Mapping[str, Any] | None = None
    ) -> JsonRpcResponse:
        deadline = time.monotonic() + self.timeout_seconds
        if not self._request_lock.acquire(timeout=self.timeout_seconds):
            raise McpClientError(f"MCP request {method} timed out waiting for the request channel")
        try:
            if self._stopped.is_set():
                raise McpClientError("MCP client is closed")
            req = JsonRpcRequest(method=method, id=self._next_id, params=params)
            self._next_id += 1
            if self._server_instance is not None:
                response = self._server_instance.handle_request(req)
            else:
                self._write(req.to_json(), deadline)
                response = self._wait_response(req.id, deadline)
            if type(response.id) is not type(req.id) or response.id != req.id:
                raise McpClientError("MCP response ID does not match the outstanding request")
            return response
        except (McpClientError, OSError, ValueError, TypeError) as exc:
            self.close()
            if isinstance(exc, McpClientError):
                raise
            raise McpClientError(f"Failed to communicate with MCP server: {exc}") from exc
        finally:
            self._request_lock.release()

    def _wait_response(self, request_id: str | int, deadline: float) -> JsonRpcResponse:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise McpClientError("MCP request timed out")
            try:
                message = self._messages.get(timeout=remaining)
            except queue.Empty as exc:
                raise McpClientError("MCP request timed out") from exc
            if isinstance(message, McpClientError):
                raise message
            data = json.loads(message)
            if not isinstance(data, dict):
                raise McpClientError("MCP message must be an object")
            if "method" in data:
                if "id" not in data:
                    JsonRpcNotification.from_dict(data)
                    continue
                incoming = JsonRpcRequest.from_dict(data)
                # No client sampling/roots capabilities were advertised. Ping is supported.
                response = (
                    JsonRpcResponse(id=incoming.id, result={})
                    if incoming.method == "ping"
                    else JsonRpcResponse(
                        id=incoming.id,
                        error=JsonRpcError(-32601, "Client method not supported"),
                    )
                )
                self._write(json.dumps(response.to_dict()), deadline)
                continue
            response = JsonRpcResponse.from_dict(data)
            if type(response.id) is not type(request_id) or response.id != request_id:
                raise McpClientError("MCP response ID does not match the outstanding request")
            return response

    def initialize(self) -> Mapping[str, Any]:
        """Negotiate the supported revision, then send the initialized notification."""
        with self._initialize_lock:
            if self._initialization is not None and not self._stopped.is_set():
                return dict(self._initialization)
            result = self._request_result(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "ai-architecture-mcp-client", "version": "1.0.0"},
                },
            )
            if (
                result.get("protocolVersion") != PROTOCOL_VERSION
                or not isinstance(result.get("capabilities"), dict)
                or not isinstance(result.get("serverInfo"), dict)
            ):
                self.close()
                raise McpClientError(
                    "Unsupported protocol version or invalid initialization response"
                )
            notification = JsonRpcNotification("notifications/initialized")
            try:
                if self._server_instance is not None:
                    self._server_instance.handle_notification(notification)
                else:
                    self._write(notification.to_json())
            except (OSError, McpClientError) as exc:
                self.close()
                raise McpClientError(f"Failed to send initialized notification: {exc}") from exc
            self._initialization = result
            return dict(result)

    def _request_result(
        self, method: str, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        response = self._send_request(method, params)
        if response.error:
            raise McpClientError(
                f"{method} error ({response.error.code}): {response.error.message}"
            )
        if not isinstance(response.result, dict):
            self.close()
            raise McpClientError(f"{method} result must be an object")
        return dict(response.result)

    def list_tools(self) -> Sequence[Mapping[str, Any]]:
        initialization = self.initialize()
        if "tools" not in initialization["capabilities"]:
            raise McpClientError("Server did not negotiate the tools capability")
        result = self._request_result("tools/list")
        tools = result.get("tools")
        if (
            not isinstance(tools, list)
            or any(not isinstance(tool, dict) for tool in tools)
            or result.get("nextCursor")
        ):
            raise McpClientError("Invalid or unsupported paginated tools/list result")
        return tools

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        self.initialize()
        return self._request_result("tools/call", {"name": name, "arguments": dict(arguments)})

    def close(self) -> None:
        """Terminate the child before closing pipes, releasing blocked readers."""
        with self._close_lock:
            self._stopped.set()
            process, self._process = self._process, None
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=0.5)
                if self._writer is not None:
                    self._writer.join(timeout=0.5)
                for pipe in (process.stdin, process.stdout):
                    if pipe is not None:
                        pipe.close()
            if self._reader is not None:
                self._reader.join(timeout=0.5)
