# Enterprise AI Architecture: Hands-On Learning Curriculum

Welcome to the hands-on learning curriculum for **`ai-application-architecture`**.

This repository is simultaneously:
1. An **Enterprise AI Reference Architecture** establishing architectural patterns, taxonomy, and boundary governance.
2. A **Production-Grade Reference Implementation Monorepo** delivering verifiable code, hermetic test suites, and quantitative AI evaluation harnesses.
3. A **Structured Hands-On Learning Curriculum** designed to guide engineers, solution architects, and technical leaders from foundational governance through advanced enterprise AI patterns.

---

## 1. Learning Philosophy & Pedagogical Approach

Most AI tutorials teach prompt scripting, single-vendor SDK usage, or ungrounded toy demos. This curriculum teaches **Enterprise AI Architecture**—the discipline of building reliable, observable, secure, and provider-neutral AI capabilities integrated into production software systems.

Every guide in this curriculum follows the strict principle:

> **Repository → Evidence → Explanation**  
> Every architectural concept is grounded in actual committed code, deterministic tests, quantitative evaluations, and Architecture Decision Records (ADRs). Nothing is speculative or imagined.

### The 8-Step Study Method

When studying each phase, progress through this exact sequence:
1. **WHY**: Understand the enterprise problem that necessitated this phase.
2. **WHAT**: Review the architectural decisions and rejected alternatives (ADRs).
3. **WHERE**: Navigate the physical repository structure and component boundaries.
4. **HOW**: Follow the code execution path and dependency directions.
5. **RUN IT**: Execute hermetic test suites and standalone verification tools.
6. **BREAK IT SAFELY**: Induce controlled failures (timeouts, schema violations, service outages) to observe recovery and error classification.
7. **OBSERVE IT**: Inspect latency distributions, token usage metrics, and trace contexts.
8. **EXPLAIN IT**: Defend the architecture using the Architect Interview Checkpoints.

---

## 2. Curriculum Roadmap & Progression

The curriculum is structured around the repository's phased engineering roadmap. Detailed learning guides are produced **only after a phase has been independently reviewed, accepted, and frozen**.

```
FOUNDATION (PHASES 0–3) — FROZEN & VERIFIED
──────────────────────────────────────────────────────────────────────────
Phase 0: Architecture Governance
  "What are we building, why, and what rules govern it?"
  → [Phase 00 Learning Guide](phase-00-governance.md)
        ↓
Phase 1: Engineering Standards & Quality Gates
  "How should engineers build, test, and document it?"
  → [Phase 01 Learning Guide](phase-01-engineering-standards.md)
        ↓
Phase 2: Repository Foundation & Minimum Core Contracts
  "What is the smallest reusable foundation needed before writing AI code?"
  → [Phase 02 Learning Guide](phase-02-repository-foundation.md)
        ↓
Phase 3: Tiered Reference Implementation Templates
  "How do we structure reference examples without bureaucracy or boilerplate?"
  → [Phase 03 Learning Guide](phase-03-reference-templates.md)


AI ENGINEERING & CAPABILITIES (PHASES 4–14)
──────────────────────────────────────────────────────────────────────────
Phase 4: AI Foundations [IMPLEMENTED; SPANS DEFERRED TO PHASE 12, GATE F UNMET]
  "How does an application safely communicate with an LLM?"
  → [Phase 04 Learning Guide](phase-04-ai-foundations.md)
        ↓
Phase 5: Knowledge Intelligence & RAG [IMPLEMENTED; CORRECTIONS PENDING INDEPENDENT REVIEW]
  "How will the LLM securely ground reasoning in private enterprise knowledge?"
  → [Phase 05 Learning Guide](phase-05-knowledge-intelligence-rag.md)
        ↓
Phase 6: Agentic Task Execution [ACCEPTED & FREEZE-READY]
  "How will AI safely invoke enterprise tools and execute stateful actions?"
  → [Phase 06 Learning Guide](phase-06-agentic-task-execution.md)
        ↓
Phase 7: Agentic Workflow Orchestration [ACCEPTED & FREEZE-READY]
  "How will multi-step, human-in-the-loop workflows be coordinated?"
  → [Phase 07 Learning Guide](phase-07-agentic-workflow-orchestration.md)
        ↓
Phases 8–14: Advanced Enterprise Systems [PLANNED — GUIDES NOT YET AVAILABLE]
  Multi-Agent, Observability, Gateways, Multimodal, Edge, and Production Apps
```

---

## 3. Curriculum Phase Index

| Phase | Title | Focus Area | Status | Learning Guide |
| :---: | :--- | :--- | :--- | :---: |
| **00** | **Architecture Governance** | Vision, Scope, Taxonomy, 10 Quality Gates, Local-First Policy | `FROZEN` | [Phase 00 Guide](phase-00-governance.md) |
| **01** | **Engineering Standards** | Definition of Done, Testing Strategy, AI Eval Standards, Security | `FROZEN` | [Phase 01 Guide](phase-01-engineering-standards.md) |
| **02** | **Repository Foundation** | Monorepo Structure, Core Contracts (`models.py`, `ports.py`, `errors.py`) | `FROZEN` | [Phase 02 Guide](phase-02-repository-foundation.md) |
| **03** | **Reference Templates** | Four Reference Tiers, Manifests, Taxonomy Validation, Governance | `FROZEN` | [Phase 03 Guide](phase-03-reference-templates.md) |
| **04** | **AI Foundations** | Provider-Neutral Ports, `OllamaAdapter`, Structured Generation, Gate D Eval | `FROZEN` | [Phase 04 Guide](phase-04-ai-foundations.md) |
| **05** | **Knowledge Intelligence & RAG** | Chunking, Embeddings, Vector Index, Evidence Sufficiency, Citations | `ACCEPTED` | [Phase 05 Guide](phase-05-knowledge-intelligence-rag.md) |
| **06** | **Agentic Task Execution** | Tool Proposals, Capability Registry, Policy Enforcement, Mandatory HITL, MCP | `ACCEPTED` | [Phase 06 Guide](phase-06-agentic-task-execution.md) |
| **07** | **Workflow Orchestration** | Deterministic Graph Workflows, Human-in-the-Loop, State Machines | `ACCEPTED` | [Phase 07 Guide](phase-07-agentic-workflow-orchestration.md) |
| **08–14** | **Enterprise AI Systems** | Multi-Agent, Gateways, Telemetry, Multimodal, Edge & Reference Apps | `PLANNED` | *Available after respective phase freeze* |

---

## 4. Audience & Recommended Prerequisites

This curriculum is intended for:
* **Senior Software Engineers & AI Engineers**: Transitioning from ad-hoc script prototyping to robust, enterprise-grade AI architecture.
* **Enterprise & Solution Architects**: Designing scalable AI platforms, selecting boundary abstractions, and defining governance standards.
* **Technical Leaders & Platform Engineers**: Evaluating local-first vs cloud AI infrastructure, quality gates, and AI testability.

### Knowledge Prerequisites
* **Software Architecture**: Familiarity with Hexagonal / Clean Architecture (Ports and Adapters), Dependency Inversion Principle, and API boundary design.
* **Programming**: Proficiency in Python 3.11+ (dataclasses, typing protocols, asynchronous programming with `asyncio`).
* **Systems Engineering**: Basic HTTP REST interaction, JSON schema validation, and unit testing concepts.

### Environment Setup
* **For Deterministic Study (Phases 0–3)**: Python 3.11+ and standard Unix command-line tools. Zero external AI runtimes or cloud credentials required.
* **For Live AI Execution (Phase 4)**: A running local Ollama instance with a pulled model (e.g. `llama3.2:3b`). Follow the comprehensive workstation setup in [`docs/setup/local-ai-environment.md`](../setup/local-ai-environment.md).

---

## 5. Post-Freeze Learning Maintenance Rule

To ensure documentation remains an authoritative educational asset:
> **Post-Freeze Learning Rule**: Whenever a roadmap phase achieves independent acceptance and is frozen, its dedicated learning guide must be authored directly from the verified repository state before work begins on the subsequent phase.
