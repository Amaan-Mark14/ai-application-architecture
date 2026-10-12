"""Citation validation and source attribution verification."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from .document import Chunk, Document


@dataclass(frozen=True)
class Citation:
    """Verified citation pointing to an authenticated chunk and document source."""

    chunk_id: str
    document_id: str
    section_title: str
    source_title: str
    source_uri: str


@dataclass(frozen=True)
class CitationValidationResult:
    """Result of validating model-generated citations against retrieved evidence."""

    is_valid: bool
    verified_citations: list[Citation]
    rejected_citation_ids: list[str]
    has_insufficient_evidence: bool = False
    errors: list[str] = field(default_factory=list)


class CitationValidator:
    """Validates source identity against supplied evidence, not factual entailment.

    Protects against citation spoofing (where an LLM invents or hallucinates source identifiers).
    """

    def __init__(self, document_catalog: Mapping[str, Document] | None = None) -> None:
        self._catalog: dict[str, Document] = dict(document_catalog) if document_catalog else {}

    def register_documents(self, documents: Sequence[Document]) -> None:
        """Register source documents to enable resolving chunk IDs to document metadata."""
        for doc in documents:
            self._catalog[doc.document_id] = doc

    def validate(
        self,
        cited_ids: Sequence[str],
        retrieved_chunks: Sequence[Chunk],
        insufficient_evidence: bool = False,
    ) -> CitationValidationResult:
        """Validate raw citation identifiers produced by a generation model."""
        retrieved_map: dict[str, Chunk] = {c.chunk_id: c for c in retrieved_chunks}
        verified: list[Citation] = []
        rejected: list[str] = []
        errors: list[str] = []
        seen_chunk_ids: set[str] = set()

        if insufficient_evidence:
            # If model reports insufficient evidence, citations should typically be empty
            if cited_ids:
                errors.append("Citations were provided despite insufficient_evidence being True")
            return CitationValidationResult(
                is_valid=not cited_ids,
                verified_citations=[],
                rejected_citation_ids=list(cited_ids),
                has_insufficient_evidence=True,
                errors=errors,
            )

        for raw_id in cited_ids:
            clean_id = raw_id.strip()
            # Strip markdown brackets or 'Source ID: ' prefixes if model returned them formatted
            clean_id = clean_id.replace("[", "").replace("]", "").replace("Source ID:", "").strip()

            if not clean_id:
                rejected.append(raw_id)
                errors.append("Citation identifier is empty")
                continue

            if clean_id in seen_chunk_ids:
                # Deduplicate without error
                continue
            seen_chunk_ids.add(clean_id)

            if clean_id in retrieved_map:
                chunk = retrieved_map[clean_id]
                doc = self._catalog.get(chunk.document_id)
                source_title = doc.title if doc else chunk.document_id
                source_uri = doc.source_uri if doc else ""

                verified.append(
                    Citation(
                        chunk_id=chunk.chunk_id,
                        document_id=chunk.document_id,
                        section_title=chunk.section_title or "General",
                        source_title=source_title,
                        source_uri=source_uri,
                    )
                )
            else:
                rejected.append(clean_id)
                errors.append(f"Citation '{clean_id}' was not present in retrieved context")

        # Result is valid if all provided citations were successfully verified against retrieved evidence
        if not verified:
            errors.append("An answer requires at least one supplied evidence citation")
        is_valid = len(rejected) == 0 and bool(verified)

        return CitationValidationResult(
            is_valid=is_valid,
            verified_citations=verified,
            rejected_citation_ids=rejected,
            has_insufficient_evidence=False,
            errors=errors,
        )
