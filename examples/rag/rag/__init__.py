"""Knowledge Intelligence and RAG reference package."""

from .chunker import HeadingAwareChunker
from .citation_validator import Citation, CitationValidationResult, CitationValidator
from .context_builder import ContextBuilder, ContextBuildResult
from .document import Chunk, Document, make_chunk_id
from .ingestion import load_corpus_directory, load_markdown_document, normalize_text
from .retriever import RetrievalResult, Retriever
from .service import RAGResponse, RAGService
from .vector_index import InMemoryVectorIndex, ScoredChunk, VectorIndexPort

__all__ = [
    "Chunk",
    "Citation",
    "CitationValidationResult",
    "CitationValidator",
    "ContextBuildResult",
    "ContextBuilder",
    "Document",
    "HeadingAwareChunker",
    "InMemoryVectorIndex",
    "RAGResponse",
    "RAGService",
    "RetrievalResult",
    "Retriever",
    "ScoredChunk",
    "VectorIndexPort",
    "load_corpus_directory",
    "load_markdown_document",
    "make_chunk_id",
    "normalize_text",
]
