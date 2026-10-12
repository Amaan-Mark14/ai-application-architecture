# Tier 3 Platform Component: Ollama Adapter

> **Tier Classification**: **Tier 3 — Platform Component**  
> **Component Role**: Outbound Model Provider Adapter  
> **Target Ecosystem**: Python 3.9+  
> **Status**: Implemented  

---

## 1. Responsibility & Bounded Scope

* **Purpose**: Provides a concrete, local-first model runtime adapter connecting to Ollama while implementing the provider-neutral `TextGenerationPort`.
* **Supported Behavior**:
  * Direct HTTP communication with Ollama daemon (`POST /api/generate` and `POST /api/chat`).
  * Request serialization from `CompletionRequest` (prompt, messages, temperature, tokens, format).
  * Response mapping into `CompletionResponse` including token `UsageMetrics` and `latency_ms`.
  * Bounded exponential backoff retry on transient transport/server errors (`AiTransientError`).
  * HTTP status code translation into the repository `AiError` taxonomy (`AiModelNotFoundError`, `AiTimeoutError`, `AiProviderUnavailableError`, `AiInvalidRequestError`).
  * Timeout and cancellation propagation via `asyncio.CancelledError`.
  * Preflight health and installed model discovery (`check_health()`, `get_installed_models()`).
* **Unsupported Behavior (Anti-Framework Boundaries)**:
  * Does **not** implement embeddings or vector search (deferred to Phase 5).
  * Does **not** implement tool calling, function execution, or MCP (deferred to Phase 6).
  * Does **not** implement agent loops, multi-agent debate, or memory (deferred to Phase 6).
  * Does **not** implement centralized gateway routing or quotas (deferred to Phase 12).
* **Reuse Justification**: Factor the low-level Ollama HTTP wire protocol, retry policies, and error translation into an isolated platform component so application services interact purely through `TextGenerationPort`.

---

## 2. Public Contracts & Interface Specification

`OllamaAdapter` implements the canonical `TextGenerationPort` protocol defined in `building-blocks/python/contracts/ports.py`:

```python
class OllamaAdapter(TextGenerationPort):
    def __init__(
        self,
        endpoint: str = "http://localhost:11434",
        default_model: str = "llama3.2",
        timeout_seconds: float = 30.0,
        max_retries: int = 2,
        backoff_base_seconds: float = 0.5,
    ) -> None: ...

    async def generate(
        self,
        request: CompletionRequest,
        context: Optional[AiOperationContext] = None,
    ) -> CompletionResponse: ...

    async def check_health(self) -> bool: ...

    async def get_installed_models(self) -> List[str]: ...
```

---

## 3. Integration Guide & Usage Example

```python
import asyncio
from contracts.models import CompletionRequest
from ollama_adapter.adapter import OllamaAdapter

async def main():
    # Initialize adapter pointing to local Ollama
    adapter = OllamaAdapter(endpoint="http://localhost:11434", default_model="llama3.2")

    request = CompletionRequest(
        prompt="Explain hexagonal architecture in one sentence.",
        model="llama3.2",
        temperature=0.2,
    )

    response = await adapter.generate(request)
    print(f"Response: {response.text}")
    print(f"Latency: {response.latency_ms}ms")
    if response.usage:
        print(f"Tokens: {response.usage.total_tokens}")

asyncio.run(main())
```

---

## 4. Failure Modes & Error Taxonomy

All exceptions are mapped into the domain `AiError` taxonomy (`building-blocks/python/contracts/errors.py`):

| Exception Class | Error Code | Transient? | Trigger Condition | Recommended Recovery |
| :--- | :--- | :---: | :--- | :--- |
| `AiModelNotFoundError` | `AI_MODEL_NOT_FOUND` | No | HTTP 404 from Ollama; requested model is not pulled. | Verify model installation via `ollama list` / `ollama pull`. |
| `AiInvalidRequestError` | `AI_INVALID_REQUEST` | No | HTTP 400 or 422; malformed options or unparseable payload. | Validate request parameters before invoking adapter. |
| `AiTimeoutError` | `AI_TIMEOUT` | Yes | Request exceeded `timeout_seconds` waiting for response. | Retry with increased timeout or smaller prompt/tokens. |
| `AiProviderUnavailableError` | `AI_PROVIDER_UNAVAILABLE` | Yes | Connection refused or HTTP 500/502/503/504 from daemon. | Ensure `ollama serve` is running; retry with backoff. |
| `AiRateLimitError` | `AI_RATE_LIMIT` | Yes | HTTP 429 concurrency limit reached. | Backoff and retry after delay. |

---

## 5. Security & Observability Considerations

* **Security Boundaries**: Zero external credentials required for local Mode A execution. All inputs sent to Ollama are serialized as JSON; no shell or process execution is performed. Model responses are returned as untrusted text to be parsed by consuming use cases.
* **Recorded Telemetry Metadata**: Translates Ollama nanosecond timing (`total_duration`) to millisecond latencies (`latency_ms`), captures model name and prompt/completion tokens in `UsageMetrics`, and copies `AiOperationContext` correlation attributes. These response fields do not create, emit or export OpenTelemetry spans.
* **Span Deferral**: [ADR-0012](../../adr/0012-phase-4-telemetry-deferral-and-rag-preparation-cache.md) assigns span implementation and local exporter verification to Phase 12. Phase 4's reconciled delivery criteria cover metadata only; Gate F remains unmet.
* **Sensitive Data Redaction**: Adapter does not log prompt or output text to stdout in production execution paths.

---

## 6. Testing Strategy & Local Environment Setup

* **Offline Hermetic Testing**: Hermetic in-memory test suite using mocked HTTP responses testing payload serialization, response mapping, status code translation, retry logic, and health checking. No Ollama daemon or network connectivity is required for CI or monorepo validation.
* **Local Developer Setup (Mode A / Mode B)**:
  * Running live inference against Ollama is required **only** for real runtime execution and live verification; it is **not** required for deterministic repository validation or CI.
  * Installing and managing Ollama is a developer responsibility. Verify installation via `ollama --version`.
  * Start the local daemon: `ollama serve` (default endpoint: `http://localhost:11434`).
  * Models must be manually installed by the developer (e.g. `ollama pull llama3.2:3b` or `ollama pull llama3.2`). No test, script, or CI workflow executes `ollama pull`. Check installed models via `ollama list`.
* **Verification Commands**:
```bash
# 1. Offline hermetic unit tests (runnable anywhere without Ollama)
python3 -m unittest discover -s platform/ollama-adapter/tests -t platform/ollama-adapter -v

# 2. Live Ollama runtime verification (requires local Ollama daemon and installed model)
python3 platform/ollama-adapter/verify.py
```

---

## 7. Quality Gate Checklist (Scaled for Tier 3)

Per authoritative [QUALITY-GATES.md](../../QUALITY-GATES.md), Tier 3 platform components are evaluated against Gates A, B, C, F, and H:
- [x] **Gate A — Architecture & Structural Boundaries**: Inward dependency direction maintained; zero reverse dependencies on applications; ports decouple core contracts from infrastructure.
- [ ] **Gate B — Code Quality & Type Safety**: Strictly typed with standard Python type annotations; formal external linters (`mypy`, `ruff`) not installed/run in CI (*Partially Verified*).
- [ ] **Gate C — Software Testing**: 15 hermetic unit tests verifying request/response mapping, error translation, timeout classification, and retries; statement coverage measurement: *Not Verified* (coverage tooling not installed).
- [ ] **Gate F — Observability & Telemetry**: Metadata extraction implemented (`latency_ms`, token `UsageMetrics`, model name, correlation attributes). OpenTelemetry spans and export are absent; **Gate F unmet, deferred to Phase 12** under [ADR-0012](../../adr/0012-phase-4-telemetry-deferral-and-rag-preparation-cache.md).
- [x] **Gate H — Documentation & Architectural Integrity**: Stable, documented public contracts, architecture specifications, and zero broken links.
