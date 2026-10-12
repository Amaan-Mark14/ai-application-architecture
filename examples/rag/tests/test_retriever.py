"""Unit tests for Retriever component."""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
BUILDING_BLOCKS_DIR = REPO_ROOT / "building-blocks" / "python"
RAG_DIR = Path(__file__).resolve().parent.parent

for p in (BUILDING_BLOCKS_DIR, RAG_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from contracts.models import EmbeddingRequest
from rag.document import Chunk
from rag.retriever import Retriever
from rag.test_doubles import DeterministicEmbeddingStub
from rag.vector_index import InMemoryVectorIndex


class TestRetriever(unittest.TestCase):
    """Test suite verifying semantic retrieval decoupled from LLM generation."""

    def setUp(self) -> None:
        self.embedding_stub = DeterministicEmbeddingStub(dimensions=16)
        self.index = InMemoryVectorIndex(dimensions=16)
        self.retriever = Retriever(
            embedding_port=self.embedding_stub,
            vector_index=self.index,
            default_top_k=2,
        )

    def test_empty_query_returns_empty_list(self) -> None:
        res = asyncio.run(self.retriever.retrieve("   "))
        self.assertEqual(res, [])
        self.assertEqual(len(self.embedding_stub.recorded_requests), 0)

    def test_retrieval_flow_and_ranking(self) -> None:
        # Create test chunks
        chunk1 = Chunk(chunk_id="chunk-retention", document_id="doc1", chunk_index=0, text="audit log retention for 7 years")
        chunk2 = Chunk(chunk_id="chunk-password", document_id="doc2", chunk_index=0, text="password requirements 16 chars")
        chunk3 = Chunk(chunk_id="chunk-vacation", document_id="doc3", chunk_index=0, text="annual leave policy 25 days")

        # Index chunks with deterministic embeddings
        req = asyncio.run(
            self.embedding_stub.embed(
                EmbeddingRequest(
                    inputs=[chunk1.text, chunk2.text, chunk3.text],
                    model="test-embed",
                )
            )
        )
        self.index.add_batch([chunk1, chunk2, chunk3], req.embeddings)

        # Retrieve with query closely matching chunk1
        results = asyncio.run(self.retriever.retrieve("what is audit log retention period?", top_k=2))

        self.assertEqual(len(results), 2)
        # Top rank must be chunk1
        self.assertEqual(results[0].rank, 1)
        self.assertEqual(results[0].chunk_id, "chunk-retention")
        self.assertEqual(results[0].document_id, "doc1")
        self.assertGreater(results[0].score, 0.5)

        # Second rank must be rank 2
        self.assertEqual(results[1].rank, 2)


if __name__ == "__main__":
    unittest.main()
