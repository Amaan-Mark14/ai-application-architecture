"""JSON-RPC 2.0 protocol specifications and serialization for Model Context Protocol (MCP)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


class JsonRpcErrorCode:
    """Standard JSON-RPC 2.0 error codes."""

    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603


@dataclass(frozen=True)
class JsonRpcError:
    """JSON-RPC 2.0 error object."""

    code: int
    message: str
    data: Any | None = None

    def to_dict(self) -> Mapping[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            payload["data"] = self.data
        return payload


@dataclass(frozen=True)
class JsonRpcRequest:
    """JSON-RPC 2.0 request payload."""

    method: str
    id: str | int
    params: Mapping[str, Any] | None = None
    jsonrpc: str = "2.0"

    def to_dict(self) -> Mapping[str, Any]:
        payload: dict[str, Any] = {
            "jsonrpc": self.jsonrpc,
            "id": self.id,
            "method": self.method,
        }
        if self.params is not None:
            payload["params"] = dict(self.params)
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> JsonRpcRequest:
        if data.get("jsonrpc") != "2.0":
            raise ValueError(f"Invalid JSON-RPC version: {data.get('jsonrpc')}")
        if "method" not in data or not isinstance(data["method"], str):
            raise ValueError("Missing or invalid 'method' field in JSON-RPC request")
        if "id" not in data or type(data["id"]) not in (str, int):
            raise ValueError("Missing 'id' field in JSON-RPC request")
        if "params" in data and not isinstance(data["params"], dict):
            raise ValueError("Request params must be an object")
        return cls(
            method=data["method"],
            id=data["id"],
            params=data.get("params"),
        )


@dataclass(frozen=True)
class JsonRpcNotification:
    """A notification has no request ID and must never receive a response."""

    method: str
    params: Mapping[str, Any] | None = None

    def to_json(self) -> str:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": self.method}
        if self.params is not None:
            payload["params"] = dict(self.params)
        return json.dumps(payload)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> JsonRpcNotification:
        if data.get("jsonrpc") != "2.0" or "id" in data or not isinstance(data.get("method"), str):
            raise ValueError("Invalid JSON-RPC notification")
        if "params" in data and not isinstance(data["params"], dict):
            raise ValueError("Notification params must be an object")
        return cls(method=data["method"], params=data.get("params"))


@dataclass(frozen=True)
class JsonRpcResponse:
    """JSON-RPC 2.0 response payload."""

    id: str | int
    result: Any | None = None
    error: JsonRpcError | None = None
    jsonrpc: str = "2.0"

    def to_dict(self) -> Mapping[str, Any]:
        payload: dict[str, Any] = {"jsonrpc": self.jsonrpc, "id": self.id}
        if self.error is not None:
            payload["error"] = self.error.to_dict()
        else:
            payload["result"] = self.result
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> JsonRpcResponse:
        if data.get("jsonrpc") != "2.0":
            raise ValueError(f"Invalid JSON-RPC version: {data.get('jsonrpc')}")
        if "id" not in data or type(data["id"]) not in (str, int):
            raise ValueError("Missing 'id' field in JSON-RPC response")
        if ("result" in data) == ("error" in data):
            raise ValueError("Response must contain exactly one of result or error")

        err: JsonRpcError | None = None
        if "error" in data:
            err_dict = data["error"]
            if (
                not isinstance(err_dict, dict)
                or type(err_dict.get("code")) is not int
                or not isinstance(err_dict.get("message"), str)
            ):
                raise ValueError("Invalid JSON-RPC error object")
            err = JsonRpcError(
                code=err_dict.get("code", JsonRpcErrorCode.INTERNAL_ERROR),
                message=err_dict.get("message", "Unknown error"),
                data=err_dict.get("data"),
            )

        return cls(
            id=data["id"],
            result=data.get("result"),
            error=err,
        )
