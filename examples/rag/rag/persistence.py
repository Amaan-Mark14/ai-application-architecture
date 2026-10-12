"""Local SQLite preparation cache; exact search remains in memory.

Publish complete snapshots atomically after embedding succeeds. Optimistic generation
checks reject competing writers rather than silently overwriting newer preparation.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import sqlite3
import time
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path

from contracts.models import EmbeddingRequest
from contracts.ports import EmbeddingPort

from .chunker import HeadingAwareChunker
from .document import Chunk, Document
from .ingestion import load_markdown_text
from .vector_index import InMemoryVectorIndex


@dataclass(frozen=True)
class EmbeddingConfiguration:
    """Explicit embedding identity; revision must change when model weights change."""

    provider: str
    model: str
    revision: str
    dimensions: int | None = None

    def __post_init__(self) -> None:
        if not all(value.strip() for value in (self.provider, self.model, self.revision)):
            raise ValueError("Embedding provider, model and immutable revision are required")
        if self.dimensions is not None and self.dimensions <= 0:
            raise ValueError("Embedding dimensions must be positive")


@dataclass(frozen=True)
class PreparationStats:
    loaded_documents: int
    reused_documents: int
    deleted_documents: int
    embedded_texts: int
    reused_chunks: int
    elapsed_ms: float


@dataclass(frozen=True)
class PreparedCorpus:
    documents: list[Document]
    chunks: list[Chunk]
    index: InMemoryVectorIndex
    stats: PreparationStats


StoredDocument = tuple[str, str, str]  # fingerprint, document JSON, chunks JSON


class CorpusCache:
    """Single local corpus snapshot; synchronizing a directory includes deletions.

    Hash file bytes on every synchronization to detect same-size/same-mtime edits.
    Unchanged files bypass parsing/chunking, and identical embedding inputs reuse
    vectors even when sections move and their positional citation IDs change.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5.0)
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                tables = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
                if tables:
                    raise ValueError("Refusing to initialize an unrelated SQLite database")
                schema = Path(__file__).with_name("cache_schema.sql").read_text(encoding="utf-8")
                connection.executescript("BEGIN IMMEDIATE;\n" + schema + "\nCOMMIT;")
            elif version != 1:
                raise ValueError(f"Unsupported corpus cache schema version: {version}")
            return connection
        except BaseException:
            connection.close()
            raise

    def _snapshot(self) -> tuple[str, int, dict[str, StoredDocument], dict[str, list[float]]]:
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN")
            state = connection.execute(
                "SELECT configuration, generation FROM cache_state WHERE singleton = 1"
            ).fetchone()
            documents = {
                path: (fingerprint, document_json, chunks_json)
                for path, fingerprint, document_json, chunks_json in connection.execute(
                    "SELECT path, fingerprint, document_json, chunks_json FROM documents"
                )
            }
            embeddings = {
                content_hash: [float(value) for value in json.loads(vector_json)]
                for content_hash, vector_json in connection.execute(
                    "SELECT content_hash, vector_json FROM embeddings"
                )
            }
            return (state[0], state[1], documents, embeddings) if state else ("", 0, {}, {})

    def _commit(
        self,
        configuration: str,
        generation: int,
        documents: dict[str, StoredDocument],
        embeddings: dict[str, list[float]],
    ) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT generation FROM cache_state WHERE singleton = 1"
            ).fetchone()
            if (current[0] if current else 0) != generation:
                raise RuntimeError("Corpus cache changed during preparation; retry synchronization")
            existing_paths = {row[0] for row in connection.execute("SELECT path FROM documents")}
            connection.executemany(
                "DELETE FROM documents WHERE path = ?",
                [(path,) for path in existing_paths - documents.keys()],
            )
            connection.executemany(
                "INSERT INTO documents VALUES (?, ?, ?, ?) ON CONFLICT(path) DO UPDATE SET "
                "fingerprint=excluded.fingerprint, document_json=excluded.document_json, "
                "chunks_json=excluded.chunks_json WHERE fingerprint != excluded.fingerprint "
                "OR document_json != excluded.document_json OR chunks_json != excluded.chunks_json",
                [(path, *record) for path, record in documents.items()],
            )
            existing_hashes = {row[0] for row in connection.execute("SELECT content_hash FROM embeddings")}
            connection.executemany(
                "DELETE FROM embeddings WHERE content_hash = ?",
                [(key,) for key in existing_hashes - embeddings.keys()],
            )
            connection.executemany(
                "INSERT INTO embeddings VALUES (?, ?) ON CONFLICT(content_hash) DO UPDATE SET "
                "vector_json=excluded.vector_json WHERE vector_json != excluded.vector_json",
                [(key, json.dumps(vector, allow_nan=False)) for key, vector in embeddings.items()],
            )
            connection.execute(
                "INSERT OR REPLACE INTO cache_state VALUES (1, ?, ?)",
                (configuration, generation + 1),
            )

    @staticmethod
    def _read_documents(
        corpus_dir: Path,
        old_documents: dict[str, StoredDocument],
        chunker: HeadingAwareChunker,
    ) -> tuple[dict[str, StoredDocument], list[Document], list[Chunk], int]:
        if not corpus_dir.is_dir():
            raise NotADirectoryError(f"Corpus directory not found: {corpus_dir}")
        records: dict[str, StoredDocument] = {}
        documents: list[Document] = []
        chunks: list[Chunk] = []
        seen_ids: set[str] = set()
        reused = 0
        for path in sorted(corpus_dir.glob("*.md")):
            if path.name.startswith((".", "_")):
                continue
            raw = path.read_bytes()
            fingerprint = hashlib.sha256(raw).hexdigest()
            previous = old_documents.get(path.name)
            if previous and previous[0] == fingerprint:
                document = Document(**json.loads(previous[1]))
                document_chunks = [Chunk(**item) for item in json.loads(previous[2])]
                record = previous
                reused += 1
            else:
                document = load_markdown_text(raw.decode("utf-8"), path)
                document_chunks = chunker.chunk_document(document)
                record = (
                    fingerprint,
                    json.dumps(asdict(document), sort_keys=True, allow_nan=False),
                    json.dumps([asdict(chunk) for chunk in document_chunks], sort_keys=True, allow_nan=False),
                )
            if document.document_id in seen_ids:
                raise ValueError(f"Duplicate document ID: {document.document_id}")
            seen_ids.add(document.document_id)
            records[path.name] = record
            documents.append(document)
            chunks.extend(document_chunks)
        return records, documents, chunks, reused

    async def prepare(
        self,
        corpus_dir: Path,
        embedding_port: EmbeddingPort,
        embedding: EmbeddingConfiguration,
        chunker: HeadingAwareChunker | None = None,
        batch_size: int = 32,
    ) -> PreparedCorpus:
        """Synchronize additions, edits and deletions, then hydrate an exact index."""
        if batch_size <= 0:
            raise ValueError("Embedding batch_size must be positive")
        started = time.monotonic()
        corpus_dir = corpus_dir.resolve()
        chunker = chunker or HeadingAwareChunker(max_chunk_chars=1000)
        configuration = json.dumps({
            "corpus": str(corpus_dir),
            "ingestion": "markdown-v1",
            "chunking": chunker.configuration(),
            "embedding": asdict(embedding),
        }, sort_keys=True)
        old_config, generation, old_documents, vectors = await asyncio.to_thread(self._snapshot)
        previous_paths = set(old_documents) if old_config == configuration else set()
        if old_config != configuration:
            old_documents, vectors = {}, {}
        records, documents, chunks, reused_docs = await asyncio.to_thread(
            self._read_documents, corpus_dir, old_documents, chunker
        )
        unique_texts = {chunk.content_hash: chunk.text for chunk in chunks}
        missing = [key for key in unique_texts if key not in vectors]
        reused_chunks = sum(chunk.content_hash in vectors for chunk in chunks)
        dimensions = embedding.dimensions
        for offset in range(0, len(missing), batch_size):
            keys = missing[offset:offset + batch_size]
            response = await embedding_port.embed(EmbeddingRequest(
                inputs=[unique_texts[key] for key in keys], model=embedding.model
            ))
            if len(response.embeddings) != len(keys):
                raise ValueError("Embedding response count does not match requested input count")
            if dimensions is None:
                dimensions = response.dimensions
            if response.dimensions != dimensions:
                raise ValueError("Embedding dimensions changed within the configured identity")
            for key, vector in zip(keys, response.embeddings):
                vectors[key] = [float(value) for value in vector]

        active_vectors = {key: vectors[key] for key in unique_texts}
        index = InMemoryVectorIndex(dimensions=dimensions)
        for chunk in chunks:
            vector = active_vectors[chunk.content_hash]
            if not vector or not all(math.isfinite(value) for value in vector):
                raise ValueError("Embeddings must contain finite, non-empty vectors")
            index.add(chunk, vector)
        # A failed embedding, invalid vector, or duplicate ID never replaces the old snapshot.
        await asyncio.to_thread(self._commit, configuration, generation, records, active_vectors)
        stats = PreparationStats(
            loaded_documents=len(documents) - reused_docs,
            reused_documents=reused_docs,
            deleted_documents=len(previous_paths - records.keys()),
            embedded_texts=len(missing),
            reused_chunks=reused_chunks,
            elapsed_ms=round((time.monotonic() - started) * 1000, 2),
        )
        return PreparedCorpus(documents, chunks, index, stats)
