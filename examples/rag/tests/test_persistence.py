"""Integration checks using real temporary SQLite files and model boundary doubles."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
RAG_DIR = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT / "building-blocks" / "python", RAG_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from contracts.models import EmbeddingResponse
from rag.chunker import HeadingAwareChunker
from rag.persistence import CorpusCache, EmbeddingConfiguration, PreparedCorpus
from rag.test_doubles import DeterministicEmbeddingStub


class TestCorpusCache(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.corpus = self.root / "corpus"
        self.corpus.mkdir()
        self.file = self.corpus / "policy.md"
        self.file.write_text("## Retention\n\nAudit records stay seven years.\n\n## Access\n\nMFA is mandatory.", encoding="utf-8")
        self.cache_path = self.root / "cache.sqlite3"
        self.embedding = EmbeddingConfiguration("deterministic-test", "test", "v1", 16)
        self.stub = DeterministicEmbeddingStub(dimensions=16)

    def prepare(self, chunker: HeadingAwareChunker | None = None, batch_size: int = 32) -> PreparedCorpus:
        # New cache instance and SQLite connection each time simulates process restart.
        return asyncio.run(CorpusCache(self.cache_path).prepare(
            self.corpus, self.stub, self.embedding, chunker=chunker, batch_size=batch_size
        ))

    def test_restart_skips_parsing_chunking_and_embedding(self) -> None:
        first = self.prepare()
        new_stub = DeterministicEmbeddingStub(dimensions=16)
        with patch("rag.persistence.load_markdown_text", side_effect=AssertionError("reparsed")), patch.object(
            HeadingAwareChunker, "chunk_document", side_effect=AssertionError("rechunked")
        ):
            second = asyncio.run(CorpusCache(self.cache_path).prepare(self.corpus, new_stub, self.embedding))
        self.assertEqual(new_stub.recorded_requests, [])
        self.assertEqual(second.chunks, first.chunks)
        self.assertEqual(second.stats.reused_documents, 1)
        self.assertEqual(second.stats.embedded_texts, 0)
        self.assertEqual(second.stats.reused_chunks, 2)
        query = self.stub._compute_vector("Audit records")
        self.assertEqual(first.index.search(query), second.index.search(query))

    def test_moved_sections_reuse_vectors_independent_of_citation_position(self) -> None:
        first = self.prepare()
        self.file.write_text("## Access\n\nMFA is mandatory.\n\n## Retention\n\nAudit records stay seven years.", encoding="utf-8")
        second = self.prepare()
        self.assertEqual(second.stats.embedded_texts, 0)
        self.assertEqual(second.stats.reused_chunks, 2)
        self.assertEqual(second.chunks[1].content_hash, first.chunks[0].content_hash)
        self.assertNotEqual(second.chunks[1].chunk_id, first.chunks[0].chunk_id)

    def test_edit_only_embeds_changed_text_and_prunes_obsolete_vectors(self) -> None:
        first = self.prepare()
        previous_hash = first.chunks[0].content_hash
        stat = self.file.stat()
        # Same size and mtime: only hashing the bytes detects this edit reliably.
        self.file.write_text(self.file.read_text(encoding="utf-8").replace("seven", "eight"), encoding="utf-8")
        os.utime(self.file, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        second = self.prepare(batch_size=1)
        self.assertEqual(second.stats.embedded_texts, 1)
        self.assertEqual(second.stats.reused_chunks, 1)
        with closing(sqlite3.connect(self.cache_path)) as connection:
            hashes = {row[0] for row in connection.execute("SELECT content_hash FROM embeddings")}
        self.assertNotIn(previous_hash, hashes)

    def test_metadata_only_change_updates_sources_without_reembedding(self) -> None:
        self.prepare()
        self.file.write_text("---\ntitle: Updated Policy\ndepartment: security\n---\n" + self.file.read_text(encoding="utf-8"), encoding="utf-8")
        updated = self.prepare()
        self.assertEqual(updated.stats.embedded_texts, 0)
        self.assertEqual(updated.documents[0].title, "Updated Policy")
        self.assertEqual(updated.chunks[0].metadata["department"], "security")

    def test_add_delete_and_empty_corpus(self) -> None:
        first = self.prepare()
        other = self.corpus / "other.md"
        other.write_text("New knowledge", encoding="utf-8")
        added = self.prepare()
        self.assertEqual(added.stats.embedded_texts, 1)
        self.assertEqual(added.index.count(), first.index.count() + 1)
        other.unlink()
        deleted = self.prepare()
        self.assertEqual(deleted.stats.deleted_documents, 1)
        self.assertEqual(deleted.stats.embedded_texts, 0)
        self.file.unlink()
        empty = self.prepare()
        self.assertEqual(empty.index.count(), 0)
        with closing(sqlite3.connect(self.cache_path)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM embeddings").fetchone()[0], 0)

    def test_identical_inputs_share_embedding_but_keep_distinct_sources(self) -> None:
        (self.corpus / "copy.md").write_text(self.file.read_text(encoding="utf-8"), encoding="utf-8")
        prepared = self.prepare()
        self.assertEqual(prepared.stats.embedded_texts, 2)
        self.assertEqual(len(prepared.chunks), 4)
        self.assertEqual(len({chunk.chunk_id for chunk in prepared.chunks}), 4)

    def test_embedding_and_chunking_configuration_invalidate_cache(self) -> None:
        self.prepare()
        for identity in (
            replace(self.embedding, model="another"),
            replace(self.embedding, provider="other-provider"),
            replace(self.embedding, revision="v2"),
        ):
            result = asyncio.run(CorpusCache(self.cache_path).prepare(self.corpus, self.stub, identity))
            self.assertEqual(result.stats.reused_documents, 0)
            self.assertEqual(result.stats.reused_chunks, 0)
            self.assertEqual(result.stats.embedded_texts, 2)
        changed = self.prepare(chunker=HeadingAwareChunker(max_chunk_chars=90))
        self.assertEqual(changed.stats.reused_chunks, 0)
        self.embedding = replace(self.embedding, dimensions=32)
        self.stub = DeterministicEmbeddingStub(dimensions=32)
        resized = self.prepare()
        self.assertEqual(resized.index.dimensions, 32)
        self.assertEqual(resized.stats.reused_chunks, 0)

    def test_duplicate_document_ids_leave_previous_snapshot_intact(self) -> None:
        first = self.prepare()
        (self.corpus / "duplicate.md").write_text("---\nid: policy\n---\nDuplicate", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Duplicate document ID"):
            self.prepare()
        (self.corpus / "duplicate.md").unlink()
        self.assertEqual(self.prepare().chunks, first.chunks)

    def test_embedding_failure_preserves_previous_snapshot(self) -> None:
        first = self.prepare()
        original = self.file.read_text(encoding="utf-8")
        self.file.write_text(original + "\n\nNew facts", encoding="utf-8")
        with patch.object(self.stub, "embed", side_effect=RuntimeError("provider failure")), self.assertRaisesRegex(RuntimeError, "provider failure"):
            self.prepare()
        self.file.write_text(original, encoding="utf-8")
        restored = self.prepare()
        self.assertEqual(restored.stats.embedded_texts, 0)
        self.assertEqual(restored.chunks, first.chunks)

    def test_competing_preparations_cannot_overwrite_newer_generation(self) -> None:
        self.prepare()
        cache = CorpusCache(self.cache_path)
        configuration, generation, documents, vectors = cache._snapshot()
        cache._commit(configuration, generation, documents, vectors)
        with self.assertRaisesRegex(RuntimeError, "changed during preparation"):
            cache._commit(configuration, generation, documents, vectors)

    def test_invalid_embedding_payloads_preserve_snapshot(self) -> None:
        self.prepare()
        cache = CorpusCache(self.cache_path)
        original = cache._snapshot()
        self.file.write_text("## Changed\n\nFirst new fact.\n\n## More\n\nSecond new fact.", encoding="utf-8")
        invalid_responses = [
            EmbeddingResponse([[0.0] * 16], "test", 16),  # One vector for two inputs.
            EmbeddingResponse([[0.0] * 8] * 2, "test", 8),  # Wrong configured dimensions.
            EmbeddingResponse([[float("nan")] * 16] * 2, "test", 16),
        ]
        for response in invalid_responses:
            with self.subTest(response=response), patch.object(self.stub, "embed", return_value=response), self.assertRaises(ValueError):
                self.prepare()
            self.assertEqual(cache._snapshot(), original)

    def test_inferred_dimensions_cannot_change_between_embedding_batches(self) -> None:
        self.embedding = replace(self.embedding, dimensions=None)
        responses = [EmbeddingResponse([[0.0] * 16], "test", 16), EmbeddingResponse([[0.0] * 8], "test", 8)]
        with patch.object(self.stub, "embed", side_effect=responses), self.assertRaisesRegex(ValueError, "dimensions changed"):
            self.prepare(batch_size=1)

    def test_unknown_schema_and_unrelated_database_are_preserved(self) -> None:
        for version in (0, 99):
            path = self.root / f"unrelated-{version}.sqlite3"
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("CREATE TABLE important (value TEXT)")
                connection.execute(f"PRAGMA user_version = {version}")
            with self.assertRaises(ValueError):
                CorpusCache(path)._snapshot()
            with closing(sqlite3.connect(path)) as connection:
                self.assertIsNotNone(connection.execute("SELECT name FROM sqlite_master WHERE name='important'").fetchone())

    def test_invalid_cache_configuration_and_missing_corpus(self) -> None:
        with self.assertRaises(ValueError):
            EmbeddingConfiguration("provider", "model", "")
        with self.assertRaises(ValueError):
            EmbeddingConfiguration("provider", "model", "rev", 0)
        with self.assertRaises(ValueError):
            self.prepare(batch_size=0)
        with self.assertRaises(NotADirectoryError):
            asyncio.run(CorpusCache(self.cache_path).prepare(self.root / "absent", self.stub, self.embedding))
