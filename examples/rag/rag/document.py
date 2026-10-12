"""Domain entities and data structures for documents and chunks."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Document:
    """Represents a discrete source knowledge document ingested into the system."""

    document_id: str
    title: str
    content: str
    source_uri: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.document_id or not self.document_id.strip():
            raise ValueError("Document document_id must be a non-empty string")
        if not isinstance(self.content, str):
            raise TypeError("Document content must be a string")



@dataclass(frozen=True)
class Chunk:
    """Represents a bounded textual passage extracted from a Document.

    Maintains source document traceability and stable chunk identity.
    """

    chunk_id: str
    document_id: str
    chunk_index: int
    text: str
    section_title: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        """Identity of exact embedding input, independent of positional citation IDs."""
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    def __post_init__(self) -> None:
        if not self.chunk_id or not self.chunk_id.strip():
            raise ValueError("Chunk chunk_id must be a non-empty string")
        if not self.document_id or not self.document_id.strip():
            raise ValueError("Chunk document_id must be a non-empty string")
        if self.chunk_index < 0:
            raise ValueError("Chunk chunk_index must be non-negative")
        if not self.text or not self.text.strip():
            raise ValueError("Chunk text must be a non-empty string")


def make_chunk_id(document_id: str, chunk_index: int) -> str:
    """Generate a deterministic, traceable chunk identifier."""
    return f"{document_id}#c{chunk_index}"
