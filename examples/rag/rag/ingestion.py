"""Knowledge document loader and normalizer."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .document import Document

FRONTMATTER_PATTERN = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def normalize_text(text: str) -> str:
    """Normalize line endings, trim excessive blank lines and whitespace."""
    # Convert Windows CRLF to Unix LF
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    # Normalize multiple consecutive blank lines to at most 2
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip()


def parse_frontmatter(raw_text: str) -> tuple[dict[str, Any], str]:
    """Extract YAML-like key-value frontmatter from document header."""
    match = FRONTMATTER_PATTERN.match(raw_text)
    if not match:
        return {}, raw_text

    frontmatter_str = match.group(1)
    body = raw_text[match.end() :]
    metadata: dict[str, Any] = {}

    for line in frontmatter_str.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            key, val = line.split(":", 1)
            key = key.strip()
            val = val.strip()
            # Try parsing numeric or boolean types
            if val.lower() == "true":
                metadata[key] = True
            elif val.lower() == "false":
                metadata[key] = False
            else:
                try:
                    if "." in val:
                        metadata[key] = float(val)
                    else:
                        metadata[key] = int(val)
                except ValueError:
                    # Strip quotation marks if present
                    if (val.startswith('"') and val.endswith('"')) or (
                        val.startswith("'") and val.endswith("'")
                    ):
                        val = val[1:-1]
                    metadata[key] = val

    return metadata, body


def load_markdown_document(file_path: Path) -> Document:
    """Load and normalize a single markdown document file."""
    if not file_path.is_file():
        raise FileNotFoundError(f"Document file not found: {file_path}")

    raw_text = file_path.read_text(encoding="utf-8")
    return load_markdown_text(raw_text, file_path)


def load_markdown_text(raw_text: str, file_path: Path) -> Document:
    """Parse a previously read snapshot so its fingerprint and content agree."""
    metadata, body = parse_frontmatter(raw_text)
    normalized_body = normalize_text(body)

    # Derive document ID from metadata or filename stem
    doc_id = str(metadata.get("id") or file_path.stem)
    title = str(metadata.get("title") or doc_id)

    # If title not in metadata, attempt to extract first H1 heading
    if title == doc_id:
        for line in normalized_body.split("\n"):
            if line.startswith("# "):
                title = line[2:].strip()
                break

    return Document(
        document_id=doc_id,
        title=title,
        content=normalized_body,
        source_uri=str(file_path),
        metadata=metadata,
    )


def load_corpus_directory(corpus_dir: Path) -> list[Document]:
    """Load all markdown documents from a corpus directory sorted by filename."""
    if not corpus_dir.is_dir():
        raise NotADirectoryError(f"Corpus directory not found: {corpus_dir}")

    docs: list[Document] = []
    for file_path in sorted(corpus_dir.glob("*.md")):
        if file_path.name.startswith((".", "_")):
            continue
        docs.append(load_markdown_document(file_path))

    return docs
