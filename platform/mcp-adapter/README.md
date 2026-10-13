# Model Context Protocol (MCP) Platform Adapter (`platform/mcp-adapter`)

> **Tier 3 Platform Component** — Reusable integration adapter providing standardized connectivity to Model Context Protocol (MCP) tool providers with strict capability allowlisting.

---

## 1. Executive Summary

The **Model Context Protocol (MCP)** platform component provides a pure Python standard-library implementation of the JSON-RPC 2.0 wire protocol over standard I/O (`stdio`). It decouples agent execution loops from proprietary tool hosting environments by adapting external MCP servers into provider-neutral [`CapabilityPort`](../../building-blocks/python/contracts/agent.py) abstractions.

### Architectural Boundary Directives

> [!IMPORTANT]
> **MCP IS AN INTEGRATION PROTOCOL, NOT THE AGENT ARCHITECTURE**  
> An autonomous agent loop must not depend on MCP as its internal architecture. MCP is strictly an outbound integration adapter. The core agent reasoning loop, capability registry, authorization policy, and human-in-the-loop (HITL) approval gates function identically whether capabilities are hosted locally or over MCP.

1. **Protocol Decoupling**: Core application and domain logic have zero dependencies on MCP packages or transport abstractions.
2. **Capability Allowlisting**: An agent must never automatically expose all tools discovered on an MCP server. The application maintains an explicit allowlist; only permitted capabilities are registered in the agent's capability registry.
3. **Uniform Security Governance**: Capabilities hosted across MCP are subject to the identical parameter validation, role-based authorization, and HITL approval policies as native in-memory capabilities.
4. **Untrusted Tool Outputs**: Tool outputs returned from MCP servers are treated as untrusted external text, sanitized, and bounded before being passed as observations into the reasoning context.

---

## 2. Architecture & Wire Protocol

```
┌────────────────────────────────────────────────────────┐
│ Agent Execution Engine (examples/agent-execution)      │
│ - Selects capability by canonical name                 │
└───────────────────────────┬────────────────────────────┘
                            │ Proposes Action
                            ▼
┌────────────────────────────────────────────────────────┐
│ Capability Registry                                    │
│ - Enforces application-level allowlist                 │
└───────────────────────────┬────────────────────────────┘
                            │ Resolves CapabilityPort
                            ▼
┌────────────────────────────────────────────────────────┐
│ McpCapabilityAdapter (platform/mcp-adapter)            │
│ - Wraps McpClient                                      │
│ - Implements CapabilityPort contract                   │
│ - Sanitizes JSON-RPC responses into observations       │
└───────────────────────────┬────────────────────────────┘
                            │ JSON-RPC 2.0 Request over stdio
                            ▼
┌────────────────────────────────────────────────────────┐
│ External / Subprocess MCP Server (mcp_adapter.server)  │
│ - Handles `initialize`, `tools/list`, `tools/call`     │
│ - Dispatches to tool implementation                    │
└────────────────────────────────────────────────────────┘
```

### Supported JSON-RPC 2.0 Methods

* **`initialize`**: Protocol negotiation and capability discovery handshake. Returns server identification and protocol version (`2024-11-05`).
* **`tools/list`**: Queries all tools declared by the MCP server, returning names, descriptions, and JSON Schema definitions (`inputSchema`).
* **`tools/call`**: Executes a named capability with model-provided arguments, returning structured content payloads or error indicators (`isError: true`).

---

## 3. Component Structure

```text
platform/mcp-adapter/
├── artifact.json                 # Tier 3 Platform Component manifest
├── README.md                     # Architectural documentation and usage guide
├── verify.py                     # Standalone component verification CLI
├── mcp_adapter/
│   ├── __init__.py               # Public exports
│   ├── protocol.py               # JSON-RPC 2.0 wire framing and error codes
│   ├── server.py                 # Reference stdio MCP server implementation
│   ├── client.py                 # Subprocess and in-memory JSON-RPC stdio client
│   └── adapter.py                # McpCapabilityAdapter implementing CapabilityPort
└── tests/
    └── test_mcp_adapter.py       # Hermetic unit tests verifying protocol & allowlisting
```

---

## 4. Usage & Integration Guide

### Exposing an Allowlisted MCP Capability to an Application

```python
from mcp_adapter.adapter import McpCapabilityAdapter
from mcp_adapter.client import McpClient
from contracts.agent import SideEffectLevel

# 1. Connect client to MCP server subprocess
client = McpClient(command=["python3", "-m", "mcp_adapter.server"])

# 2. Perform protocol handshake
client.initialize()

# 3. Create adapter with strict allowlist enforcement
# Attempting to register an unlisted tool will raise ValueError
adapter = McpCapabilityAdapter(
    client=client,
    tool_name="get_system_status",
    side_effect_level=SideEffectLevel.READ_ONLY,  # Trusted host configuration
    allowlist=["get_system_status"],  # Ping will be blocked
)

# 4. Execute via standard CapabilityPort interface
result = await adapter.execute({})
print("System diagnostic output:", result)

client.close()
```

Both direct adapters and `create_mcp_capabilities(client, allowlist, side_effect_levels)` require explicit host-side side-effect classification. The factory's third argument is a mapping such as `{"get_system_status": SideEffectLevel.READ_ONLY}`; an incomplete mapping fails before discovery. Server annotations and tool names do not establish classification. Missing classification or an omitted allowlist rejects registration. `STATE_MUTATING` always requires approval; explicitly disabling approval is rejected. Allowlisting remains a separate restriction.

### Supported lifecycle and transport limits

The client supports MCP revision `2024-11-05` over stdio and the in-process reference transport. Initialization sends client capabilities, checks the negotiated revision/server metadata, and sends `notifications/initialized` before tool operations. Tool methods initialize lazily if needed. The reference server consumes notifications without responding to them.

Stdio requests are serialized and responses must match the outstanding ID and its type. Intervening notifications are consumed without satisfying a request; server pings are answered and unadvertised client methods receive `METHOD_NOT_FOUND`. Notifications do not extend the request deadline. Dynamic tool-list updates and pagination are not implemented; paginated discovery is rejected explicitly.

`McpClient(..., timeout_seconds=5.0)` sets a positive, finite request deadline covering channel acquisition and stdio writes/reads, including initialization and discovery. Reader/writer workers keep blocking pipe operations bounded; failed channels terminate their child and release workers/pipes so late responses cannot be reused. Messages are limited to 1,048,576 characters, with a bounded 256-message receive queue and UTF-8 pipe encoding. Child import paths use `os.pathsep`; inherited `PYTHONPATH` is retained. Child stderr is discarded rather than accumulated. These are transport bounds, not agent observation/context limits. In-process handlers execute synchronously and are intended for trusted local tests; their Python code cannot be forcibly preempted by the stdio deadline.

---

## 5. Security & Failure Modes

| Failure Mode | Protocol Behavior | Application Impact |
| :--- | :--- | :--- |
| **Server Crash / Disconnect** | `McpClientError` raised on broken pipe. | Tool executor catches failure; wraps as an error observation for the agent loop. |
| **Tool Name Spoofing** | Server returns `METHOD_NOT_FOUND` (-32601). | Agent receives error observation; safe termination or retry. |
| **Malformed Tool Output** | Sanitizer falls back to raw string or error dictionary. | Prevents deserialization exploits or application crashes. |
| **Prompt Injection in Tool Output** | Output formatted strictly as data; model system prompt directs model to disregard embedded commands. | Prevents indirect prompt injection attacks from external data. |
| **Un-allowlisted Tool Invocation** | `McpCapabilityAdapter` validation fails fast at initialization. | Unauthorized capabilities can never be registered in the application registry. |
| **Stalled Server / Blocking stdio** | Subprocess I/O offloaded to worker thread via `asyncio.to_thread()`; cancelled task closes client and terminates child process. | Prevents event-loop starvation; tool executor timeout bounds execution reliably. |

### Subprocess I/O & Timeout Boundary Mechanics

Because Python's standard `subprocess.Popen` stdio pipes perform blocking synchronous reads (`stdout.readline()`), invoking `call_tool()` directly on the main event loop thread would block the entire asyncio event loop, defeating outer `asyncio.wait_for(...)` timeout cancellation.

To guarantee a bounded timeout boundary without external runtime dependencies:
1. **Async Worker Thread Isolation**: `McpCapabilityAdapter.execute()` dispatches the synchronous `client.call_tool(...)` invocation onto a worker thread via `asyncio.to_thread()`.
2. **Event Loop Non-Blocking**: The agent's asyncio event loop remains fully responsive while subprocess I/O is pending. Other tasks, timers, and step abort signals execute concurrently.
3. **Subprocess Termination on Cancellation**: If the calling context or executor times out (raising `asyncio.CancelledError` or `asyncio.TimeoutError`), the adapter catches the cancellation and invokes `client.close()`.
4. **Lifecycle & Pipe Cleanup**: In `McpClient.close()`, the child process is terminated via `SIGTERM` (and `kill()` if necessary) before closing pipes. This causes an immediate EOF on the read pipe, releasing the worker thread cleanly without pipe deadlocks or zombie subprocesses.

---

## 6. Verification & Quality Gates

Run the hermetic test suite:
```bash
PYTHONPATH=building-blocks/python:platform/mcp-adapter python3 -m unittest discover -s platform/mcp-adapter/tests -v
```

Run standalone component verification:
```bash
# In-memory transport verification
python3 platform/mcp-adapter/verify.py

# Subprocess stdio transport verification
python3 platform/mcp-adapter/verify.py --subprocess
```

Validate artifact structure:
```bash
python3 scripts/artifact_validator.py platform/mcp-adapter
```
