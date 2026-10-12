#!/usr/bin/env python3
"""Synthetic software-capacity diagnostic, using deterministic model doubles.

Reports preparation reuse, update/delete work, exact-search latency and peak traced
Python allocations. Does not measure real embedding throughput or model quality.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import tempfile
import time
import tracemalloc
from dataclasses import asdict
from pathlib import Path
from typing import Any

RAG_DIR = Path(__file__).resolve().parent
REPO_ROOT = RAG_DIR.parent.parent
for path in (REPO_ROOT / "building-blocks" / "python", RAG_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from contracts.models import EmbeddingRequest
from rag.persistence import CorpusCache, EmbeddingConfiguration
from rag.test_doubles import DeterministicEmbeddingStub


async def measure(document_count: int, dimensions: int, searches: int) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        corpus = root / "corpus"
        corpus.mkdir()
        for number in range(document_count):
            (corpus / f"policy-{number:06d}.md").write_text(
                f"## Policy {number}\n\nSecurity audit records for department {number} "
                "must be retained for seven years. Access requires MFA and peer review.",
                encoding="utf-8",
            )
        identity = EmbeddingConfiguration("synthetic-hashing-stub", "test", "v1", dimensions)
        path = root / "cache.sqlite3"
        tracemalloc.start()
        try:
            cold = await CorpusCache(path).prepare(corpus, DeterministicEmbeddingStub(dimensions), identity)
            warm = await CorpusCache(path).prepare(corpus, DeterministicEmbeddingStub(dimensions), identity)
            edited = corpus / "policy-000000.md"
            edited.write_text(edited.read_text(encoding="utf-8").replace("seven", "eight"), encoding="utf-8")
            update = await CorpusCache(path).prepare(corpus, DeterministicEmbeddingStub(dimensions), identity)
            edited.unlink()
            deletion = await CorpusCache(path).prepare(corpus, DeterministicEmbeddingStub(dimensions), identity)
            _, peak_bytes = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        query = await DeterministicEmbeddingStub(dimensions).embed(
            EmbeddingRequest(inputs=["Security audit record retention"], model="test")
        )
        samples: list[float] = []
        for _ in range(searches):
            start = time.perf_counter()
            warm.index.search(query.embeddings[0], top_k=5)
            samples.append((time.perf_counter() - start) * 1000)
        return {
            "measurement": "synthetic software only; deterministic embeddings; local SQLite; no model quality claim",
            "documents": document_count,
            "chunks": len(cold.chunks),
            "dimensions": dimensions,
            "cold": asdict(cold.stats),
            "warm": asdict(warm.stats),
            "one_document_update": asdict(update.stats),
            "one_document_delete": asdict(deletion.stats),
            "search_samples": searches,
            "exact_search_median_ms": round(statistics.median(samples), 3),
            "exact_search_max_ms": round(max(samples), 3),
            "peak_traced_python_mib": round(peak_bytes / (1024 * 1024), 3),
            "sqlite_bytes": path.stat().st_size,
            "python": sys.version.split()[0],
        }


async def main_async(args: argparse.Namespace) -> None:
    for count in args.documents:
        print(json.dumps(await measure(count, args.dimensions, args.searches), sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--documents", type=int, nargs="+", default=[100, 1000])
    parser.add_argument("--dimensions", type=int, default=64)
    parser.add_argument("--searches", type=int, default=10)
    args = parser.parse_args()
    if min(args.documents) <= 0 or args.dimensions <= 0 or args.searches <= 0:
        parser.error("Document counts, dimensions and search count must be positive")
    asyncio.run(main_async(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
