#!/usr/bin/env python3
"""Verification CLI for Knowledge Intelligence & RAG pattern implementation."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

# Ensure repository paths are importable
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
BUILDING_BLOCKS_DIR = REPO_ROOT / "building-blocks" / "python"
PLATFORM_OLLAMA_DIR = REPO_ROOT / "platform" / "ollama-adapter"
PLATFORM_EMBEDDING_DIR = REPO_ROOT / "platform" / "ollama-embedding-adapter"

for p in (BUILDING_BLOCKS_DIR, PLATFORM_OLLAMA_DIR, PLATFORM_EMBEDDING_DIR, SCRIPT_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from contracts.models import EmbeddingRequest
from contracts.ports import EmbeddingPort, TextGenerationPort
from ollama_adapter.adapter import OllamaAdapter
from ollama_embedding_adapter.adapter import OllamaEmbeddingAdapter
from rag.chunker import HeadingAwareChunker
from rag.citation_validator import CitationValidator
from rag.context_builder import ContextBuilder
from rag.ingestion import load_corpus_directory
from rag.retriever import Retriever
from rag.service import RAGService
from rag.test_doubles import DeterministicEmbeddingStub, DeterministicRAGGenerationStub
from rag.vector_index import InMemoryVectorIndex


async def verify_rag_pipeline(
    mode: str = "fake",
    endpoint: str = "http://localhost:11434",
    embed_model: str = "nomic-embed-text",
    gen_model: str = "llama3.2",
    allow_unverified: bool = False,
) -> int:
    print("=" * 60)
    print("ai-application-architecture — RAG Component Verification")
    print("=" * 60)
    print(f"Mode: {mode.upper()}")
    print(f"Corpus Path: {SCRIPT_DIR / 'corpus'}")

    corpus_dir = SCRIPT_DIR / "corpus"
    if not corpus_dir.is_dir():
        print(f"FAIL: Corpus directory not found at: {corpus_dir}")
        return 1

    docs = load_corpus_directory(corpus_dir)
    print(f"Corpus Ingestion: OK ({len(docs)} documents loaded)")

    chunker = HeadingAwareChunker(max_chunk_chars=1000)
    all_chunks = []
    for d in docs:
        all_chunks.extend(chunker.chunk_document(d))
    print(f"Heading-Aware Chunking: OK ({len(all_chunks)} chunks generated)")

    if mode == "live":
        print(f"Checking Ollama live runtime at: {endpoint}")
        embed_adapter = OllamaEmbeddingAdapter(endpoint=endpoint, default_model=embed_model)
        gen_adapter = OllamaAdapter(endpoint=endpoint, default_model=gen_model)

        is_healthy = await embed_adapter.check_health()
        if not is_healthy:
            print("\nStatus: NOT VERIFIED")
            print(f"Reason: Ollama daemon is unreachable at '{endpoint}'.")
            return 0 if allow_unverified else 1

        installed = await embed_adapter.get_installed_models()
        has_embed = any(embed_model.split(":")[0] in m for m in installed)
        has_gen = any(gen_model.split(":")[0] in m for m in installed)

        if not has_embed or not has_gen:
            print("\nStatus: NOT VERIFIED")
            print(f"Installed models: {installed}")
            if not has_embed:
                print(f"Missing embedding model: '{embed_model}'. Run `ollama pull {embed_model}`.")
            if not has_gen:
                print(f"Missing generation model: '{gen_model}'. Run `ollama pull {gen_model}`.")
            return 0 if allow_unverified else 1

        embedding_port: EmbeddingPort = embed_adapter
        llm_port: TextGenerationPort = gen_adapter
    else:
        embedding_port = DeterministicEmbeddingStub(dimensions=512)
        llm_port = DeterministicRAGGenerationStub(
            canned_answers={
                "audit": {
                    "answer": "Security audit logs must be kept for exactly 7 years.",
                    "citations": ["sec-01#c1"],
                    "insufficient_evidence": False,
                }
            }
        )

    # Embed and index
    req = EmbeddingRequest(inputs=[c.text for c in all_chunks], model=embed_model)
    emb_resp = await embedding_port.embed(req)
    index = InMemoryVectorIndex(dimensions=emb_resp.dimensions)
    index.add_batch(all_chunks, emb_resp.embeddings)
    print(f"Vector Indexing: OK ({index.count()} chunks indexed into InMemoryVectorIndex)")

    retriever = Retriever(embedding_port=embedding_port, vector_index=index, embedding_model=embed_model, default_top_k=2)
    validator = CitationValidator(document_catalog={d.document_id: d for d in docs})
    service = RAGService(
        retriever=retriever,
        llm_client=llm_port,
        context_builder=ContextBuilder(max_chunks=2, max_chars=3000),
        citation_validator=validator,
        model=gen_model,
    )

    # Smoke query verification
    test_query = "What is the mandatory retention period for security audit logs?"
    resp = await service.answer(test_query, top_k=2)

    print(f"\nSmoke Query: '{test_query}'")
    print(f"Retrieved Chunks: {[r.chunk_id for r in resp.retrieved_results]}")
    print(f"Answer: '{resp.answer}'")
    print(f"Citations Verified: {[c.chunk_id for c in resp.citations]}")
    print(f"Latency: {resp.latency_ms:.1f}ms")

    if not resp.accepted or not resp.citations or resp.insufficient_evidence:
        print("FAIL: Smoke query failed to return verified citations.")
        return 1

    print("\nRESULT: PASS — RAG pipeline verification successfully passed.")
    print("=" * 60)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify RAG pipeline execution.")
    parser.add_argument("--mode", choices=["fake", "live"], default="fake", help="Execution mode")
    parser.add_argument("--endpoint", default=os.getenv("OLLAMA_ENDPOINT", "http://localhost:11434"))
    parser.add_argument("--embed-model", default=os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text"))
    parser.add_argument("--gen-model", default=os.getenv("OLLAMA_MODEL", "llama3.2"))
    parser.add_argument("--allow-unverified", action="store_true", help="Exit 0 when live infrastructure is unavailable")
    args = parser.parse_args()

    return asyncio.run(
        verify_rag_pipeline(
            mode=args.mode,
            endpoint=args.endpoint,
            embed_model=args.embed_model,
            gen_model=args.gen_model,
            allow_unverified=args.allow_unverified,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
