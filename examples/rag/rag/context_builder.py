"""Exact character budgeting for framed, untrusted evidence."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .retriever import RetrievalResult

EVIDENCE_HEADER = "=== BEGIN RETRIEVED EVIDENCE (UNTRUSTED DATA) ==="
EVIDENCE_FOOTER = "=== END RETRIEVED EVIDENCE ==="
TRUNCATION_MARKER = "... [TRUNCATED DUE TO BUDGET]"
NO_EVIDENCE = "[NO RELEVANT EVIDENCE FOUND]"


@dataclass(frozen=True)
class ContextBuildResult:
    formatted_context: str
    included_chunk_ids: list[str]
    total_chars: int
    chunks_count: int
    truncated: bool = False


class ContextBuilder:
    """Budget delimiters, source headers, separators and truncation markers too."""

    def __init__(self, max_chunks: int = 5, max_chars: int = 4000) -> None:
        if max_chunks <= 0 or max_chars <= 0:
            raise ValueError("max_chunks and max_chars must be positive")
        self.max_chunks = max_chunks
        self.max_chars = max_chars

    def build_context(
        self,
        retrieval_results: Sequence[RetrievalResult],
        max_chars: int | None = None,
    ) -> ContextBuildResult:
        budget = min(self.max_chars, max_chars) if max_chars is not None else self.max_chars
        if budget < 0:
            raise ValueError("Available context budget cannot be negative")
        if not retrieval_results:
            text = NO_EVIDENCE if len(NO_EVIDENCE) <= budget else ""
            return ContextBuildResult(text, [], len(text), 0)

        included: list[str] = []
        blocks: list[str] = []
        passage_truncated = False
        wrapper_size = len(EVIDENCE_HEADER) + len(EVIDENCE_FOOTER) + 4
        used = wrapper_size
        for result in retrieval_results[:self.max_chunks]:
            chunk = result.chunk
            header = f"[Source ID: {chunk.chunk_id}] (Section: {chunk.section_title or 'General'})\n"
            text = chunk.text.strip()
            separator_size = 2 if blocks else 0
            available = budget - used - separator_size - len(header)
            if len(text) > available:
                if blocks or available <= len(TRUNCATION_MARKER):
                    break
                text = text[:available - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER
                passage_truncated = True
            blocks.append(header + text)
            included.append(chunk.chunk_id)
            used += separator_size + len(header) + len(text)
            if passage_truncated:
                break

        formatted = (
            EVIDENCE_HEADER + "\n\n" + "\n\n".join(blocks) + "\n\n" + EVIDENCE_FOOTER
            if blocks else ""
        )
        truncated = len(included) < len(retrieval_results) or passage_truncated
        return ContextBuildResult(formatted, included, len(formatted), len(blocks), truncated)
