"""Heading-aware markdown chunking with a hard character limit."""

from __future__ import annotations

import re
from re import Match
from typing import Any

from .document import Chunk, Document, make_chunk_id

HEADING_PATTERN = re.compile(r"^ {0,3}(#{1,6})[ \t]+([^\r\n]+)$", re.MULTILINE)
FENCE_PATTERN = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


class HeadingAwareChunker:
    """Preserve sections and short text; split long paragraphs when necessary.

    min_chunk_chars is nonbinding: short chunks are retained, never discarded.
    Overlap is deliberately unsupported and rejected rather than silently ignored.
    """

    VERSION = "heading-aware-v2"

    def __init__(
        self,
        max_chunk_chars: int = 800,
        min_chunk_chars: int = 50,
        overlap_chars: int = 0,
    ) -> None:
        if min_chunk_chars < 0 or max_chunk_chars <= min_chunk_chars:
            raise ValueError("Require 0 <= min_chunk_chars < max_chunk_chars")
        if overlap_chars != 0:
            raise ValueError("HeadingAwareChunker does not support overlap_chars != 0")
        self.max_chunk_chars = max_chunk_chars
        self.min_chunk_chars = min_chunk_chars
        self.overlap_chars = overlap_chars

    def configuration(self) -> dict[str, Any]:
        """Versioned cache identity for every chunking option."""
        return {
            "version": self.VERSION,
            "max_chunk_chars": self.max_chunk_chars,
            "min_chunk_chars": self.min_chunk_chars,
            "overlap_chars": self.overlap_chars,
        }

    @staticmethod
    def _headings(content: str) -> list[Match[str]]:
        headings: list[Match[str]] = []
        fence_char = ""
        fence_length = 0
        offset = 0
        for line in content.splitlines(keepends=True):
            fence = FENCE_PATTERN.match(line.rstrip("\r\n"))
            if fence:
                marker, suffix = fence.groups()
                if not fence_char:
                    if marker[0] != "`" or "`" not in suffix:
                        fence_char, fence_length = marker[0], len(marker)
                elif marker[0] == fence_char and len(marker) >= fence_length and not suffix.strip():
                    fence_char = ""
            elif not fence_char:
                heading = HEADING_PATTERN.match(content, offset)
                if heading:
                    headings.append(heading)
            offset += len(line)
        return headings

    def chunk_document(self, document: Document) -> list[Chunk]:
        content = document.content.strip()
        if not content:
            return []
        matches = self._headings(content)
        if not matches:
            return self._chunk_paragraphs(document, document.title, content, 0)

        chunks: list[Chunk] = []
        preamble = content[:matches[0].start()].strip()
        if preamble:
            chunks.extend(self._chunk_paragraphs(
                document, f"{document.title} (Overview)", preamble, len(chunks)
            ))

        for i, match in enumerate(matches):
            title = match.group(2).strip()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
            body = content[match.end():end].strip()
            if not body and len(match.group(1)) == 1 and title == document.title and i + 1 < len(matches):
                continue  # Document title is already retained as source metadata.
            prefix = f"## {title}\n\n"
            if not body or len(prefix) >= self.max_chunk_chars:
                # Oversized headings must not consume the entire body budget or be lost.
                body, prefix = f"## {title}\n\n{body}".strip(), ""
            chunks.extend(self._chunk_paragraphs(document, title, body, len(chunks), prefix))
        return chunks

    def _chunk_paragraphs(
        self,
        document: Document,
        section_title: str,
        text: str,
        start_index: int,
        section_prefix: str = "",
    ) -> list[Chunk]:
        capacity = self.max_chunk_chars - len(section_prefix)
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        bodies: list[str] = []
        buffer = ""
        for paragraph in paragraphs:
            candidate = f"{buffer}\n\n{paragraph}" if buffer else paragraph
            if len(candidate) <= capacity:
                buffer = candidate
                continue
            if buffer:
                bodies.append(buffer)
            while len(paragraph) > capacity:
                boundary = max(paragraph.rfind(" ", 0, capacity), paragraph.rfind("\n", 0, capacity))
                cut = boundary + 1 if boundary >= capacity // 2 else capacity
                bodies.append(paragraph[:cut].rstrip())
                paragraph = paragraph[cut:].lstrip()
            buffer = paragraph
        if buffer:
            bodies.append(buffer)
        return [
            Chunk(
                chunk_id=make_chunk_id(document.document_id, start_index + i),
                document_id=document.document_id,
                chunk_index=start_index + i,
                text=section_prefix + body,
                section_title=section_title,
                metadata=dict(document.metadata),
            )
            for i, body in enumerate(bodies)
        ]
