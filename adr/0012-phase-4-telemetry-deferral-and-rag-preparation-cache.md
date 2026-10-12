# ADR-0012: Phase 4 Telemetry Deferral and Local RAG Preparation Persistence

* **Status**: Accepted for correction scope; implementation pending independent review
* **Date**: 2026-10-12
* **Technical Story**: Phase 4 telemetry reconciliation and Phase 5 preparation reuse
* **Amends**: [ADR-0003](0003-provider-neutral-model-adapter-and-ai-foundations.md), [ADR-0004](0004-knowledge-intelligence-and-rag-architecture.md), [ADR-0005](0005-roadmap-reconciliation-knowledge-intelligence-and-rag.md)

## 1. Context

Phase 4 records model timing, token usage and correlation metadata but never emits OpenTelemetry spans. Its roadmap exit criteria previously required spans. The agreed correction is a formal deferral to Phase 12, rather than new observability infrastructure now.

The Phase 5 in-memory baseline deliberately rebuilds preparation after restart. A requested scope extension now requires reuse of document parsing, chunks and embeddings, plus document updates and deletions. This requirement does not itself justify a managed vector database or approximate search.

## 2. Decisions

### Telemetry ownership and completion criteria

Phase 4 delivers recorded metadata only. Phase 12 owns implementation of model spans and verification through a local or in-memory exporter, with required model/duration/token/error attributes and redaction. [ROADMAP.md](../ROADMAP.md) reflects this ownership. [Gate F](../QUALITY-GATES.md#gate-f--observability--telemetry) remains unchanged and unmet for the Tier 3 adapter until those spans exist and are verified. Phase delivery and full gate acceptance are separate claims.

### Local preparation cache

Use standard-library SQLite in [CorpusCache](../examples/rag/rag/persistence.py), with a versioned [schema](../examples/rag/rag/cache_schema.sql). This is a local preparation cache, distinct from the in-memory exact cosine search index. A directory synchronization discovers additions and deletions, hashes source bytes, skips parsing/chunking unchanged files, and reuses embeddings by SHA-256 of the exact chunk text. Positional citation IDs remain snapshot locators for compatibility with the reference dataset; they are not embedding cache identities.

Compatibility includes corpus root, ingestion/chunker algorithm versions, all chunker options, embedding provider, model, immutable model revision and optional expected dimensions. Live CLI persistence requires an explicit model digest/revision; a mutable model tag alone is insufficient. Existing embeddings must be invalidated by changing that revision when weights or provider-side embedding behavior changes.

Embedding runs in bounded batches. Only after all inputs and vectors validate does one transaction publish changed documents/chunks/vectors and remove obsolete records. Failed preparation retains the previous complete snapshot. An optimistic generation check rejects a competing writer; callers retry synchronization. Unknown schemas and unrelated databases are rejected without replacement. This is local developer cache initialization, not a production database migration service.

### Response and evidence boundary

Use strict standard-library JSON validation, consistent with ADR-0003's dependency-free boundary. Require exact fields and types, reject duplicate keys, and accept only a complete JSON object or a complete JSON fence. Invalid schema or citation membership produces `status="validation_failed"`, `accepted=False`, an empty answer/citation list, and a separate diagnostic raw output. Normal answers require citations; abstentions require none. Validate membership against passages supplied to the model, not all retrieved candidates.

Budget the complete evidence block and the combined system/user input text. Output has a separate token cap. Character limits are not a tokenizer-based guarantee of fitting every model's context window. Citation membership proves source identity only; factual entailment requires separate quality evaluation.

## 3. Consequences and Deferred Work

No external vector service, hosted telemetry service, ORM or AI framework is introduced. Source bytes still must be read to detect edits; hydrated vectors and exact search still consume memory and search remains linear. Preparation counters/timing and a separate deterministic capacity benchmark provide a measured path to deciding on a larger backend. These measurements are software/capacity diagnostics, not real-model quality evidence. Representative real-model validation remains separate work.

The cache contains source text and vectors in plaintext and belongs in an ignored local directory. It provides neither encryption nor multi-tenant access control. A caller holding a long-lived service must synchronize and rebuild its retriever and citation catalog together to publish a new corpus snapshot; the CLI does this at startup. Independent acceptance remains pending.
