"""Retriever component decoupling knowledge search from language model generation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from contracts.models import EmbeddingRequest
from contracts.ports import EmbeddingPort
from contracts.telemetry import AiOperationContext

from .document import Chunk
from .vector_index import VectorIndexPort


@dataclass(frozen=True)
class RetrievalResult:
    """Represents an individual retrieved chunk with its rank and similarity score."""

    chunk: Chunk
    score: float
    rank: int

    @property
    def chunk_id(self) -> str:
        return self.chunk.chunk_id

    @property
    def document_id(self) -> str:
        return self.chunk.document_id

    @property
    def text(self) -> str:
        return self.chunk.text


class Retriever:
    """Knowledge retriever coordinating query vectorization and similarity retrieval."""

    def __init__(
        self,
        embedding_port: EmbeddingPort,
        vector_index: VectorIndexPort,
        embedding_model: str = "nomic-embed-text",
        default_top_k: int = 3,
        min_relevance_score: float | None = None,
    ) -> None:
        self.embedding_port = embedding_port
        self.vector_index = vector_index
        self.embedding_model = embedding_model
        self.default_top_k = default_top_k
        self.min_relevance_score = min_relevance_score

    async def retrieve(
        self,
        query: str,
        top_k: int | None = None,
        filters: Mapping[str, Any] | None = None,
        min_score: float | None = None,
        context: AiOperationContext | None = None,
    ) -> list[RetrievalResult]:
        """Execute semantic retrieval for a user query.

        Does NOT invoke any text generation model or LLM.
        """
        clean_query = query.strip()
        if not clean_query:
            return []

        k = top_k if top_k is not None else self.default_top_k

        # 1. Embed query text using the configured EmbeddingPort
        request = EmbeddingRequest(inputs=[clean_query], model=self.embedding_model)
        response = await self.embedding_port.embed(request, context=context)

        query_vector = response.embeddings[0]

        # 2. Search the vector index
        scored_chunks = self.vector_index.search(
            query_vector=query_vector,
            top_k=k,
            filters=filters,
        )

        # 3. Format into ranked results (1-indexed)
        results: list[RetrievalResult] = []
        for rank, sc in enumerate(scored_chunks, start=1):
            results.append(
                RetrievalResult(
                    chunk=sc.chunk,
                    score=sc.score,
                    rank=rank,
                )
            )

        # 4. Apply evidence sufficiency policy if threshold is configured
        threshold = min_score if min_score is not None else self.min_relevance_score
        if threshold is not None:
            results = [r for r in results if r.score >= threshold]

        return results
