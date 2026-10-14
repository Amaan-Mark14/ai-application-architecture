# Architecture Delivery Roadmap: Phases 0 to 14

This document defines the sequential engineering and delivery lifecycle for the **`ai-application-architecture`** repository.

To maintain architectural integrity, phases are executed sequentially. Advancement to subsequent phases requires meeting defined exit criteria and satisfying applicable quality gates defined in [QUALITY-GATES.md](QUALITY-GATES.md).

> [!IMPORTANT]
> **The Early Cross-Cutting Capability Principle**  
> Later platform phases do **not** mean that evaluation, security, observability, and provider abstractions are absent before then.  
> Minimal contracts and capabilities begin early (Phase 2). Later phases provide consolidation, dashboards, enterprise policies, advanced tooling, scale, and cross-application governance.

> [!NOTE]
> **Normative Authority for Phase Scope**  
> `ROADMAP.md` is the single authoritative source for phase scope, dependencies, and exit criteria across this repository. Other documents may reference or summarize roadmap phases, but normative ownership resides exclusively herein.  
> *A concept may be explained in multiple documents, but its normative ownership must have one authoritative source.*

---

## Roadmap Overview

```
Phase 0 ──► Phase 1 ──► Phase 2 ──► Phase 3 ──► Phase 4 ──► Phase 5 ──► Phase 6
Governance  Engineering Core       Tiered      AI          Knowledge   Agentic
& Vision    Standards   Contracts  Templates   Foundations Intel & RAG Task Exec
   │
   ▼
Phase 7 ──► Phase 8 ──► Phase 9 ──► Phase 10 ─► Phase 11 ─► Phase 12 ─► Phase 13 ─► Phase 14
Agentic     Document    Multimodal  Voice &     Research &  Decision    Hardening &  v1.0
Workflows   Intel       Intel       Real-Time   Synthesis   Intel & Plat Audit       Release
```

---

## Phase 0: Vision, Scope & Architecture Governance (COMPLETED)
* **Objective**: Establish the architectural vision, operational scope, core principles, application taxonomy, local-first execution policy, reference tiers, quality gates, and AI coding agent constitution.
* **Scope**: Foundational markdown governance documentation, standards in `docs/architecture/`, and ADR framework.
* **Expected Deliverables**: Governance constitution; zero application code or premature infrastructure.
* **Exit Criteria**:
  * All Phase 0 governance documents created, verified, and cross-referenced.
  * Self-audit completed verifying compliance with all Phase 0 architectural constraints.

---

## Phase 1: Engineering Standards & Governance (COMPLETED)
* **Objective**: Establish the repository's engineering constitution covering coding, testing, AI evaluation, security, reliability, observability, data, APIs, documentation, Git workflows, and Definition of Done.
* **Scope**:
  * General engineering standards and the Architecture Complexity Rule.
  * Primary language standards: Python (`mypy --strict`, `ruff`, `pytest`), .NET 8 LTS / modern C# (`dotnet format`, analyzers, nullable references), and TypeScript (`tsc --strict`, `eslint`, `vitest`).
  * Secondary enterprise language standard: Java 21 LTS / Spring Boot (conventions, JPA/transactions, JUnit 5).
  * Testing standards (risk-based portfolio, deterministic vs. probabilistic eval separation, test doubles policy, mature coverage policy).
  * AI evaluation standards (eval sets, metrics, regression prevention, synthetic data governance).
  * Security standards (AI security, prompt injection defense, data privacy, dependency management).
  * Reliability, resilience, and rate-limiting standards.
  * Observability & telemetry standards (OpenTelemetry GenAI semantic conventions, logging, metrics).
  * Data management, vector store, and persistence standards.
  * Configuration and secrets management standards.
  * API design standards (REST, streaming/SSE, MCP).
  * Documentation and diagramming standards (Mermaid C4, ADRs).
  * Definition of Done (DoD) checklist.
  * Git workflows, branching, and commit conventions.
* **Dependencies**: Phase 0.
* **Exit Criteria**: Authoritative engineering standards documented in `docs/engineering/`; executable documentation validation script operational with passing verification.

---

## Phase 2: Repository Foundation & Minimum Core Contracts (COMPLETED)
* **Objective**: Establish monorepo structure, dependency boundaries, and minimal viable architectural contracts.
* **Scope**:
  * Modular monorepo dependency boundaries and folder layouts (`apps/`, `building-blocks/`, `platform/`).
  * **Mandatory Minimum Contracts**:
    1. **Model/LLM Client Port**: Standardized interface for text and structured generation (`ILlmClient`).
    2. **AI Operation / Telemetry Context**: OpenTelemetry context propagation and trace attributes.
    3. **Base Evaluation Result Contract**: Shared schema for recording evaluation scores and benchmark metadata (`EvaluationScenarioResult`, `EvaluationSummary`).
    4. **Structured Schema Validation & Error Contract**: Shared representation for parsing errors and domain validation failures.
  * **Conditional Early Contract**:
    * Embedding client abstraction (`IEmbeddingClient`) may be established if Phase 2 demonstrates a concrete requirement to support the immediately upcoming Knowledge Intelligence/RAG architecture. If no concrete Phase 2 requirement exists, the interface is deferred to Phase 5.
  * **Explicitly NOT Mandatory in Phase 2**:
    * Do **not** mandate a generic `IGuardrail` interface merely for architectural symmetry; guardrails represent multiple disparate concerns (input sanitization, schema validation, policy checks) and must not be reduced prematurely to one speculative interface.
    * Do **not** invent speculative agent interfaces, workflow interfaces, memory interfaces, tool interfaces, vector-store abstractions, or agent-loop abstractions before concrete reference implementations establish their requirements.
* **Exit Criteria**: Mandatory minimum contracts defined and verified with in-memory test doubles; zero external vendor SDK dependencies in domain interfaces.

---

## Phase 3: Tiered Reference Implementation Templates (COMPLETED — READY FOR REVIEW)
* **Objective**: Build reusable, unpopulated scaffolding archetypes for each reference tier and establish an evidence-driven governance system.
* **Scope**:
  * **Canonical Templates Directory**: Four lean reference archetypes in `templates/`:
    * Tier 1 Reference Application (`templates/reference-application/`) with consolidated documentation (`requirements.md`, `architecture.md`, `quality.md`).
    * Tier 2 Pattern Example (`templates/pattern-example/`) with self-contained architecture and run guide.
    * Tier 3 Platform Component (`templates/platform-component/`) with contract, failure mode, and integration guide.
    * Tier 4 Template Artifact (`templates/template-artifact/`) with archetype guide and non-production notice.
  * **Reference Artifact Manifest (`artifact.json`)**: Machine-readable schema declaring tier, status, language, mode, and Phase 0 taxonomy references.
  * **Automated Tier Validation**: Automated structural and manifest validation integrated into `scripts/validate.py`.
  * **Quality Gate Applicability Matrix**: Explicit mapping from Gate A through Gate J across the four tiers.
* **Dependencies**: Phase 2.
* **Exit Criteria**: All four tier templates created; manifest validation enforced; automated validator tests passing; documentation links verified.

---

## Phase 4: AI Foundations
* **Objective**: Implement the first functional foundational capabilities and adapters.
* **Scope**:
  * Concrete local runtime adapter for **Ollama**.
  * Structured output generation with schema validation (Pydantic / Zod).
  * Model name, latency, token usage and correlation attributes recorded as response metadata. This is metadata collection, not OpenTelemetry span emission or export.
  * Minimal evaluation harness executing benchmark scoring against local models.
  * Minimal input validation guardrail.
* **Dependencies**: Phase 3.
* **Exit Criteria**: Working local Ollama integration executing structured generation with automated test coverage and recorded telemetry metadata.
* **Explicit Telemetry Deferral**: Per [ADR-0012](adr/0012-phase-4-telemetry-deferral-and-rag-preparation-cache.md), OpenTelemetry span implementation and local export verification belong to Phase 12. Phase 4 delivery does not satisfy Gate F for the Tier 3 adapter; that gate remains unmet until spans are implemented and tested. This phase-scope deferral does not weaken [QUALITY-GATES.md](QUALITY-GATES.md).

---

## Phase 5: Knowledge Intelligence & RAG (IMPLEMENTED — PENDING INDEPENDENT REVIEW)
* **Objective**: Establish foundational knowledge intelligence and retrieval-augmented generation architectures per ADR-0004 and ADR-0005.
* **Scope**:
  * **Core Baseline Reference Implementation** (Tier 2 Pattern Example & Tier 3 Platform Component):
    * Provider-neutral embedding boundary (`EmbeddingPort`) and local Ollama embedding adapter (`platform/ollama-embedding-adapter`).
    * Markdown document ingestion, frontmatter parsing, and heading-aware structural chunking with deterministic identifiers.
    * In-memory cosine vector index with metadata filtering and deterministic tie-breaking.
    * Local SQLite preparation cache with document fingerprints, content-addressed embedding reuse, configuration compatibility checks and atomic incremental updates/deletions, per [ADR-0012](adr/0012-phase-4-telemetry-deferral-and-rag-preparation-cache.md). Exact search still runs in memory.
    * Decoupled semantic retriever with two-tier evidence-sufficiency threshold policy.
    * Defensive context construction with character budgeting and untrusted delimiter framing.
    * Grounded generation consuming frozen Phase 4 `TextGenerationPort`.
    * Application-level citation validation, spoofing rejection, and source attribution mapping.
    * Automated reference evaluation harness and versioned 32-scenario ground-truth dataset (`eval_dataset.jsonl`).
  * **Decision-Gated Enterprise Enhancements** (Progression Triggers documented in ADR-0005):
    * Lexical search (BM25) and Reciprocal Rank Fusion (RRF) triggered upon demonstrated dense keyword retrieval failure.
    * Persistent vector database storage (e.g., pgvector, Qdrant) triggered when corpus exceeds memory bounds.
    * Cross-encoder reranking triggered upon large candidate set ranking inversions.
    * Tier 1 Enterprise Knowledge Application packaging asynchronous ingestion, hybrid search, and persistent storage.
* **Dependencies**: Phase 4.
* **Exit Criteria**: Fully runnable local RAG pipeline with hermetic test suite; reference evaluation achieving groundedness $\ge 0.85$, context relevance $\ge 0.80$, schema adherence $\ge 0.98$, and zero citation spoofing.

---

## Phase 6: Agentic Task Execution (COMPLETED)
* **Objective**: Implement bounded, tool-using autonomous agents with explicit authorization controls, capability sandboxing, and Human-in-the-Loop safety boundaries.
* **Scope**:
  * **Core Baseline Reference Implementation** (Tier 2 Pattern Example & Tier 3 Platform Component, documented in ADR-0006):
    * Bounded reasoning control loop with strict iteration ceilings (`max_steps`) and cycle detection.
    * Strongly typed action proposals (`AgentDecision`) parsed defensively from model output.
    * Capability registry with parameter JSON schema validation and allowlist enforcement.
    * Application-controlled authorization policy (`CustomerSupportAuthorizationPolicy`) enforcing RBAC, transaction ceilings, and security hold rules.
    * Mandatory Human-in-the-Loop (HITL) approval gate (`ApprovalPort`) for state-mutating operations with interactive CLI and deterministic test doubles.
    * Centralized tool executor (`ToolExecutor`) enforcing asynchronous timeouts, exception sandboxing, and idempotency caching on `action_id`.
    * Auditable execution traces (`AgentTrajectoryTrace`) recording verified execution receipts without persisting unredacted chain-of-thought monologues.
    * Standard-library JSON-RPC 2.0 Model Context Protocol adapter (`platform/mcp-adapter`) over stdio with capability allowlisting.
    * Versioned 32-scenario evaluation dataset (`eval_dataset.jsonl`) measuring zero-tolerance safety invariants.
  * **Decision-Gated Enterprise Enhancements** (Progression Triggers documented in ADR-0006):
    * Multi-agent collaborative swarms and negotiation protocols (deferred to Phase 7+).
    * Durable workflow engine integration (Temporal/Camunda) with persistent state machines (deferred to Phase 7+).
    * Long-term persistent vector memory stores across sessions (deferred to Phase 7+).
    * Tier 1 Enterprise Agent Application integrating persistent CRM and enterprise identity providers.
* **Dependencies**: Phase 5.
* **Exit Criteria**: Fully runnable local agentic execution engine with hermetic test suite; standalone verification script; zero-tolerance safety invariants passed (0 unauthorized mutations, 0 unapproved required mutations, 0 unknown capabilities, 0 post-rejection executions, 0 max-step violations); complete audit trajectory recorded.

---

## Phase 7: Agentic Workflow Orchestration
* **Objective**: Coordinate one bounded, read-only agent within a deterministic workflow with durable approval checkpoints, following [ADR-0009](adr/0009-agentic-workflow-orchestration-and-roadmap-reconciliation.md).
* **Scope**:
  * Customer account remediation pattern example (Tier 2), using standard-library Python and SQLite.
  * Application-controlled state transitions, bounded read-only investigation, durable mutation receipts, and local compensation handlers.
  * Human-in-the-loop asynchronous approval pause/resume checkpoints with current authorization and approval binding checks.
  * Multi-agent collaboration, distributed workflow engines, and production identity-provider integration remain deferred as recorded in ADR-0009.
* **Dependencies**: Phase 6.
* **Exit Criteria**: Demonstrate approval suspension and resumption across separate local processes; meet Tier 2 Gates B, C, H, and I. Local interrupted-execution recovery, durable action-bound business operations, and the note-then-credit compensation sequence are implemented with deterministic restart/failure regressions; independent acceptance and live-model quality remain unverified.

---

## Phase 8: Document Intelligence
* **Objective**: Implement structured document processing (IDP) over complex enterprise documents.
* **Scope**:
  * Intelligent document processing reference application (Tier 1).
  * Layout analysis, multi-page extraction, table extraction, and optical confidence scoring.
  * Human review routing for low-confidence fields.
* **Dependencies**: Phase 7.
* **Exit Criteria**: End-to-end extraction accuracy $\ge 95\%$ on standardized document benchmark test sets.

---

## Phase 9: Multimodal Intelligence
* **Objective**: Implement joint reasoning across visual assets, documents, and textual context.
* **Scope**:
  * Multimodal inspection reference application (Tier 1).
  * Vision-language model adapters (local VLM / cloud).
  * Image tiling, resolution management, and spatial grounding.
* **Dependencies**: Phase 8.
* **Exit Criteria**: Multimodal reference application runs locally against supported vision models with automated evaluation.

---

## Phase 10: Voice & Real-Time Interaction
* **Objective**: Architect ultra-low-latency real-time voice and streaming conversational systems.
* **Scope**:
  * Real-time voice interaction reference application (Tier 1).
  * Full-duplex WebSocket streaming, Voice Activity Detection (VAD), and streaming TTS.
  * Barge-in interruption handling and sub-second latency optimization.
* **Dependencies**: Phase 9.
* **Exit Criteria**: End-to-end round-trip audio latency $< 800\text{ms}$ locally; barge-in cancels audio stream correctly.

---

## Phase 11: Research & Synthesis Systems
* **Objective**: Architect evidence-driven deep research, multi-source investigation, and report synthesis.
* **Scope**:
  * Autonomous deep research reference application (Tier 1).
  * Recursive query decomposition, citation graph generation, and contradictory evidence resolution.
* **Dependencies**: Phase 10.
* **Exit Criteria**: 100% of synthesized claims map to verifiable source citations in the output graph.

---

## Phase 12: Decision Intelligence & Advanced Platform Capabilities
* **Objective**: Build high-stakes decision support architectures and consolidate platform capabilities.
* **Scope**:
  * Decision intelligence reference application combining deterministic rules, ML scores, and LLM reasoning.
  * Advanced platform capabilities: consolidated evaluation dashboards, centralized model gateway with dynamic routing, and cloud deployment blueprints.
  * Implement OpenTelemetry spans at model boundaries deferred from Phase 4, including model, duration, token usage and success/error state; verify local export and redaction without requiring a hosted observability service.
* **Dependencies**: Phase 11.
* **Exit Criteria**: Auditable decision traces generated; centralized model gateway demonstrates fallback routing and rate limiting; deferred model spans emitted and verified through a local or in-memory exporter against Gate F.

---

## Phase 13: Architecture Hardening & Independent Audit
* **Objective**: Conduct comprehensive architectural review, security penetration audits, and performance profiling.
* **Scope**:
  * Full-suite vulnerability scans, OWASP Top 10 for LLMs audit, secret sweeps.
  * Concurrency load testing and resilience fault injection across all applications.
* **Dependencies**: Phase 12.
* **Exit Criteria**: Zero Critical or High vulnerabilities; audit report published in `docs/decisions/`.

---

## Phase 14: v1.0 Reference Architecture Release
* **Objective**: Publish the stable v1.0 enterprise reference architecture release.
* **Scope**:
  * Documentation audit, verified quick-start developer experience, and release tagging.
* **Dependencies**: Phase 13.
* **Exit Criteria**: All applications runnable, observable, evaluated, and compliant with Quality Gates A–J.
