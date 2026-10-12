"""Vector index protocol and in-memory deterministic implementation."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from .document import Chunk


@dataclass(frozen=True)
class ScoredChunk:
    """Retrieved chunk associated with a similarity score."""

    chunk: Chunk
    score: float


@runtime_checkable
class VectorIndexPort(Protocol):
    """Protocol for vector indexing and similarity search."""

    def add(self, chunk: Chunk, vector: Sequence[float]) -> None:
        """Insert a single chunk and its dense embedding vector into the index."""
        ...

    def add_batch(self, chunks: Sequence[Chunk], vectors: Sequence[Sequence[float]]) -> None:
        """Insert a batch of chunks and corresponding vectors."""
        ...

    def search(
        self,
        query_vector: Sequence[float],
        top_k: int = 5,
        filters: Mapping[str, Any] | None = None,
    ) -> list[ScoredChunk]:
        """Perform similarity search returning top-K scored chunks adhering to filters."""
        ...

    def count(self) -> int:
        """Return the number of indexed entries."""
        ...

    def clear(self) -> None:
        """Remove all indexed entries."""
        ...


def cosine_similarity(v1: Sequence[float], v2: Sequence[float]) -> float:
    """Calculate cosine similarity between two float vectors with zero-norm safety."""
    if len(v1) != len(v2):
        raise ValueError(f"Vector dimension mismatch: {len(v1)} != {len(v2)}")

    dot_product = 0.0
    norm1_sq = 0.0
    norm2_sq = 0.0

    for a, b in zip(v1, v2):
        dot_product += a * b
        norm1_sq += a * a
        norm2_sq += b * b

    norm1 = math.sqrt(norm1_sq)
    norm2 = math.sqrt(norm2_sq)

    if norm1 <= 1e-9 or norm2 <= 1e-9:
        return 0.0

    sim = dot_product / (norm1 * norm2)
    # Clamp numerical precision artifacts to [-1.0, 1.0]
    return max(-1.0, min(1.0, sim))


class InMemoryVectorIndex(VectorIndexPort):
    """Deterministic, pure-Python in-memory vector index.

    Implements exact cosine similarity search with deterministic tie-breaking.
    Provides complete visibility into retrieval mechanics without external database dependencies.
    """

    def __init__(self, dimensions: int | None = None) -> None:
        self.dimensions = dimensions
        self._entries: list[tuple[Chunk, list[float]]] = []

    def add(self, chunk: Chunk, vector: Sequence[float]) -> None:
        vec_list = [float(x) for x in vector]
        if self.dimensions is None:
            self.dimensions = len(vec_list)
        elif len(vec_list) != self.dimensions:
            raise ValueError(
                f"Dimension mismatch: chunk vector has {len(vec_list)} dimensions, index requires {self.dimensions}"
            )

        # Disallow duplicate chunk_ids
        for existing_chunk, _ in self._entries:
            if existing_chunk.chunk_id == chunk.chunk_id:
                raise ValueError(f"Chunk with ID '{chunk.chunk_id}' is already indexed")

        self._entries.append((chunk, vec_list))

    def add_batch(self, chunks: Sequence[Chunk], vectors: Sequence[Sequence[float]]) -> None:
        if len(chunks) != len(vectors):
            raise ValueError(f"Chunk count ({len(chunks)}) does not match vector count ({len(vectors)})")
        for chunk, vec in zip(chunks, vectors):
            self.add(chunk, vec)

    def _matches_filters(self, chunk: Chunk, filters: Mapping[str, Any] | None) -> bool:
        """Check if chunk metadata satisfies all key-value filter conditions."""
        if not filters:
            return True
        for key, expected_val in filters.items():
            actual_val = chunk.metadata.get(key)
            if actual_val != expected_val:
                return False
        return True

    def search(
        self,
        query_vector: Sequence[float],
        top_k: int = 5,
        filters: Mapping[str, Any] | None = None,
    ) -> list[ScoredChunk]:
        """Perform exact cosine similarity search with deterministic tie-breaking."""
        if top_k <= 0:
            raise ValueError(f"top_k must be positive, got {top_k}")
        if self.dimensions is not None and len(query_vector) != self.dimensions:
            raise ValueError(
                f"Query vector dimension ({len(query_vector)}) does not match index dimension ({self.dimensions})"
            )

        candidates: list[ScoredChunk] = []
        for chunk, vec in self._entries:
            if not self._matches_filters(chunk, filters):
                continue
            sim = cosine_similarity(query_vector, vec)
            candidates.append(ScoredChunk(chunk=chunk, score=sim))

        # Deterministic tie-breaking: sort descending by score, ascending by chunk_id
        candidates.sort(key=lambda sc: (-sc.score, sc.chunk.chunk_id))
        return candidates[:top_k]

    def count(self) -> int:
        return len(self._entries)

    def clear(self) -> None:
        self._entries.clear()
