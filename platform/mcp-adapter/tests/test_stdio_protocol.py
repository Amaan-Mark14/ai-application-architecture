"""Exercise wire behavior against independent stdio fixtures, without a model."""

from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for directory in (ROOT / "building-blocks/python", Path(__file__).resolve().parents[1]):
    sys.path.insert(0, str(directory))

from contracts.agent import SideEffectLevel
from mcp_adapter.adapter import McpCapabilityAdapter, create_mcp_capabilities
from mcp_adapter.client import McpClient, McpClientError
from mcp_adapter.protocol import JsonRpcNotification, JsonRpcRequest, JsonRpcResponse
from mcp_adapter.server import McpServer

SERVER_FIXTURE = r"""
import json, sys, time
mode = sys.argv[1]
def send(value):
    print(json.dumps(value), flush=True)
request = json.loads(sys.stdin.readline())
assert request['method'] == 'initialize'
assert request['params']['capabilities'] == {}
assert request['params']['clientInfo']['name']
if mode == 'stall_initialize':
    time.sleep(10)
    sys.exit()
result = {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'fixture', 'version': '1'}}
if mode == 'wrong_version':
    result['protocolVersion'] = '2099-01-01'
if mode == 'invalid_capabilities':
    result['capabilities'] = []
send({'jsonrpc': '2.0', 'id': request['id'], 'result': result})
notification = json.loads(sys.stdin.readline())
assert notification == {'jsonrpc': '2.0', 'method': 'notifications/initialized'}
request = json.loads(sys.stdin.readline())
assert request['method'] == 'tools/list'
if mode == 'stall_discovery':
    time.sleep(10)
    sys.exit()
if mode == 'partial_line':
    sys.stdout.write('{"jsonrpc":'); sys.stdout.flush()
    time.sleep(10)
    sys.exit()
if mode == 'notification_flood':
    while True:
        send({'jsonrpc': '2.0', 'method': 'notifications/message', 'params': {'data': 'waiting'}})
if mode == 'notifications_and_ping':
    send({'jsonrpc': '2.0', 'method': 'notifications/message', 'params': {'data': 'hello'}})
    send({'jsonrpc': '2.0', 'id': 'server-ping', 'method': 'ping'})
    assert json.loads(sys.stdin.readline()) == {'jsonrpc': '2.0', 'id': 'server-ping', 'result': {}}
if mode == 'malformed':
    send([])
elif mode == 'wrong_id':
    send({'jsonrpc': '2.0', 'id': request['id'] + 100, 'result': {'tools': []}})
elif mode == 'string_id':
    send({'jsonrpc': '2.0', 'id': str(request['id']), 'result': {'tools': []}})
else:
    send({'jsonrpc': '2.0', 'id': request['id'], 'result': {'tools': [{'name': 'fixture', 'description': 'fixture tool', 'inputSchema': {'type': 'object'}}]}})
for line in sys.stdin:
    request = json.loads(line)
    send({'jsonrpc': '2.0', 'id': request['id'], 'result': {'content': [{'type': 'text', 'text': request['params']['arguments']['echo']}], 'isError': False}})
"""


class TestStdioProtocol(unittest.TestCase):
    def client(self, mode, timeout=1.5):
        client = McpClient(
            command=[sys.executable, "-c", SERVER_FIXTURE, mode], timeout_seconds=timeout
        )
        self.addCleanup(client.close)
        return client

    def test_handshake_notifications_ping_and_utf8_roundtrip(self) -> None:
        client = self.client("notifications_and_ping")
        self.assertEqual(client.list_tools()[0]["name"], "fixture")
        result = client.call_tool("fixture", {"echo": "اردو ✓"})
        self.assertEqual(result["content"][0]["text"], "اردو ✓")
        process = client._process
        client.close()
        self.assertIsNotNone(process.poll())
        self.assertFalse(client._reader.is_alive())
        self.assertFalse(client._writer.is_alive())
        client.close()  # Cleanup is idempotent.

    def test_initialization_and_discovery_stalls_close_child(self) -> None:
        for mode in ("stall_initialize", "stall_discovery", "partial_line", "notification_flood"):
            with self.subTest(mode=mode):
                client = self.client(mode, timeout=0.3)
                process = client._process
                started = time.monotonic()
                with self.assertRaisesRegex(McpClientError, "timed out"):
                    client.list_tools()
                self.assertLess(time.monotonic() - started, 1.5)
                self.assertIsNotNone(process.poll())
                self.assertFalse(client._reader.is_alive())

    def test_incompatible_or_malformed_server_cannot_connect(self) -> None:
        for mode in ("wrong_version", "invalid_capabilities", "wrong_id", "string_id", "malformed"):
            with self.subTest(mode=mode):
                client = self.client(mode)
                process = client._process
                with self.assertRaises(McpClientError):
                    client.list_tools()
                self.assertIsNotNone(process.poll())

    def test_nonreading_child_cannot_block_request_write(self) -> None:
        client = McpClient(
            command=[sys.executable, "-c", "import time; time.sleep(10)"], timeout_seconds=0.2
        )
        self.addCleanup(client.close)
        started = time.monotonic()
        with self.assertRaisesRegex(McpClientError, "timed out"):
            client._send_request("large", {"text": "x" * 500_000})
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertFalse(client._writer.is_alive())

    def test_invalid_timeout_and_transport_configuration(self) -> None:
        for timeout in (0, -1, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                McpClient(server_instance=McpServer(), timeout_seconds=timeout)
        for kwargs in ({}, {"server_instance": McpServer(), "command": []}):
            with self.assertRaises(ValueError):
                McpClient(**kwargs)

    def test_closed_client_and_channel_contention_fail_predictably(self) -> None:
        client = McpClient(server_instance=McpServer(), timeout_seconds=0.05)
        self.addCleanup(client.close)
        client._request_lock.acquire()
        try:
            with self.assertRaisesRegex(McpClientError, "waiting for the request channel"):
                client._send_request("initialize")
        finally:
            client._request_lock.release()
        client.close()
        with self.assertRaisesRegex(McpClientError, "closed"):
            client.list_tools()

    def test_oversized_outgoing_message_closes_channel(self) -> None:
        client = self.client("normal")
        with self.assertRaisesRegex(McpClientError, "framing limit"):
            client._send_request("large", {"text": "x" * 1_048_576})
        self.assertIsNone(client._process)


class TestClassification(unittest.TestCase):
    def setUp(self) -> None:
        self.server = McpServer()
        self.client = McpClient(server_instance=self.server)
        self.addCleanup(self.client.close)

    def test_allowlisting_is_separate_from_side_effect_classification(self) -> None:
        for classification in (None, "read_only"):
            with self.assertRaisesRegex(ValueError, "classification"):
                McpCapabilityAdapter(
                    self.client, "ping", side_effect_level=classification, allowlist=["ping"]
                )
        with self.assertRaisesRegex(ValueError, "allowlist"):
            McpCapabilityAdapter(self.client, "ping", side_effect_level=SideEffectLevel.READ_ONLY)
        with self.assertRaisesRegex(ValueError, "classification"):
            create_mcp_capabilities(
                self.client, ["ping", "get_system_status"], {"ping": SideEffectLevel.READ_ONLY}
            )

    def test_mutation_always_requires_approval(self) -> None:
        adapter = McpCapabilityAdapter(
            self.client,
            "ping",
            side_effect_level=SideEffectLevel.STATE_MUTATING,
            allowlist=["ping"],
        )
        self.assertTrue(adapter.metadata.requires_approval)
        with self.assertRaisesRegex(ValueError, "must require approval"):
            McpCapabilityAdapter(
                self.client,
                "ping",
                side_effect_level=SideEffectLevel.STATE_MUTATING,
                requires_approval=False,
                allowlist=["ping"],
            )
        with self.assertRaises(ValueError):
            McpCapabilityAdapter(
                self.client,
                "ping",
                side_effect_level=SideEffectLevel.READ_ONLY,
                requires_approval=1,
                allowlist=["ping"],
            )

    def test_server_annotations_do_not_override_host_classification(self) -> None:
        self.server._tools["ping"]["annotations"] = {"readOnlyHint": True}
        capabilities = create_mcp_capabilities(
            self.client, ["ping"], {"ping": SideEffectLevel.STATE_MUTATING}
        )
        self.assertEqual(capabilities[0].metadata.side_effect_level, SideEffectLevel.STATE_MUTATING)
        self.assertTrue(capabilities[0].metadata.requires_approval)


class TestWireContracts(unittest.TestCase):
    def test_malformed_requests_and_notifications_are_rejected(self) -> None:
        for payload in (
            {"jsonrpc": "1", "id": 1, "method": "ping"},
            {"jsonrpc": "2.0", "id": 1, "method": None},
            {"jsonrpc": "2.0", "method": "ping"},
            {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": []},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                JsonRpcRequest.from_dict(payload)
        for payload in (
            {"jsonrpc": "1", "method": "notifications/message"},
            {"jsonrpc": "2.0", "id": 1, "method": "notifications/message"},
            {"jsonrpc": "2.0", "method": "notifications/message", "params": []},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                JsonRpcNotification.from_dict(payload)
        notification = JsonRpcNotification("notifications/message", {"data": "text"})
        self.assertEqual(json.loads(notification.to_json())["params"], {"data": "text"})

    def test_unnegotiated_or_unsupported_discovery_is_rejected(self) -> None:
        class DiscoveryServer(McpServer):
            def handle_request(self, request):
                response = super().handle_request(request)
                if request.method == "initialize" and mode == "no_tools":
                    response.result["capabilities"] = {}
                if request.method == "tools/list":
                    results = {
                        "paginated": {"tools": [], "nextCursor": "more"},
                        "invalid_tools": {"tools": "invalid"},
                        "invalid_result": [],
                    }
                    return JsonRpcResponse(request.id, result=results[mode])
                return response

        for mode in ("no_tools", "paginated", "invalid_tools", "invalid_result"):
            with self.subTest(mode=mode):
                client = McpClient(server_instance=DiscoveryServer())
                self.addCleanup(client.close)
                with self.assertRaises(McpClientError):
                    client.list_tools()

    def test_reference_server_rejects_invalid_requests_and_contains_tool_failure(self) -> None:
        server = McpServer()
        before_init = json.loads(
            server.process_message('{"jsonrpc":"2.0","id":1,"method":"tools/list"}')
        )
        self.assertIn("error", before_init)
        bad_init = json.loads(
            server.process_message('{"jsonrpc":"2.0","id":2,"method":"initialize"}')
        )
        self.assertIn("error", bad_init)
        client = McpClient(server_instance=server)
        self.addCleanup(client.close)
        client.initialize()
        self.assertEqual(client._request_result("ping"), {})
        with self.assertRaises(McpClientError):
            client._request_result("tools/call", {"arguments": {}})
        with self.assertRaises(McpClientError):
            client._request_result("unsupported")

        def fail(arguments):
            raise RuntimeError("fixture failure")

        server.register_tool("fails", "Fails intentionally", {"type": "object"}, fail)
        result = client.call_tool("fails", {})
        self.assertTrue(result["isError"])
        self.assertIn("fixture failure", result["content"][0]["text"])
        for text in ("", "[]", "bad json"):
            response = server.process_message(text)
            if text:
                self.assertIn("error", json.loads(response))
            else:
                self.assertEqual(response, "")

    def test_notification_produces_no_response(self) -> None:
        server = McpServer()
        init = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test"},
            },
        }
        self.assertIn("result", json.loads(server.process_message(json.dumps(init))))
        self.assertEqual(
            server.process_message('{"jsonrpc":"2.0","method":"notifications/initialized"}'), ""
        )
        self.assertEqual(
            server.process_message('{"jsonrpc":"2.0","method":"notifications/unknown"}'), ""
        )
        self.assertIn(
            "tools",
            json.loads(server.process_message('{"jsonrpc":"2.0","id":2,"method":"tools/list"}'))[
                "result"
            ],
        )

    def test_malformed_responses_are_rejected(self) -> None:
        for payload in (
            {"jsonrpc": "1", "id": 1, "result": {}},
            {"jsonrpc": "2.0", "id": True, "result": {}},
            {"jsonrpc": "2.0", "id": 1},
            {"jsonrpc": "2.0", "id": 1, "result": {}, "error": {}},
            {"jsonrpc": "2.0", "id": 1, "error": {"code": "-1", "message": "error"}},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                JsonRpcResponse.from_dict(payload)
