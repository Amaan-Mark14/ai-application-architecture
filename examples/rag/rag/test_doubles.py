"""Deterministic in-memory test doubles for hermetic CI testing and evaluation."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from typing import Any

from contracts.models import (
    CompletionRequest,
    CompletionResponse,
    EmbeddingRequest,
    EmbeddingResponse,
    FinishReason,
    UsageMetrics,
)
from contracts.ports import EmbeddingPort, TextGenerationPort
from contracts.telemetry import AiOperationContext

STOP_WORDS = {
    "what", "is", "the", "for", "in", "to", "how", "a", "an", "and",
    "of", "on", "are", "be", "do", "does", "with", "this", "that",
    "it", "from", "by", "as", "at", "all", "must", "many", "we", "can",
    "before", "after", "during", "which", "when", "where", "there", "their",
}


class DeterministicEmbeddingStub(EmbeddingPort):
    """Deterministic in-memory embedding double based on term hashing.

    Produces fixed-dimension dense vectors that yield high cosine similarity
    for overlapping semantic keywords and low similarity for disjoint content.
    Enables hermetic CI testing and evaluation without Ollama or external models.
    """

    def __init__(self, dimensions: int = 512) -> None:
        self.dimensions = dimensions
        self.recorded_requests: list[EmbeddingRequest] = []

    def _compute_vector(self, text: str) -> list[float]:
        """Convert text into a normalized term and n-gram frequency vector."""
        words = [w for w in re.findall(r"\w+", text.lower()) if w not in STOP_WORDS]
        vec = [0.0] * self.dimensions

        for word in words:
            # Word token feature
            h = int(hashlib.md5(word.encode("utf-8")).hexdigest(), 16)
            vec[h % self.dimensions] += 2.0

            # Subword character n-grams (3 to 5 chars) to match plurals/stems
            for n in range(3, min(6, len(word) + 1)):
                for i in range(len(word) - n + 1):
                    ngram = word[i : i + n]
                    hn = int(hashlib.md5(ngram.encode("utf-8")).hexdigest(), 16)
                    vec[hn % self.dimensions] += 0.5

        # Normalize vector to unit L2 length
        norm = math.sqrt(sum(x * x for x in vec))
        if norm > 1e-9:
            vec = [x / norm for x in vec]
        return vec


    async def embed(
        self,
        request: EmbeddingRequest,
        context: AiOperationContext | None = None,
    ) -> EmbeddingResponse:
        self.recorded_requests.append(request)
        embeddings = [self._compute_vector(t) for t in request.inputs]
        return EmbeddingResponse(
            embeddings=embeddings,
            model=request.model,
            dimensions=self.dimensions,
            latency_ms=1.0,
            usage=UsageMetrics(input_tokens=sum(len(t.split()) for t in request.inputs)),
        )


class DeterministicRAGGenerationStub(TextGenerationPort):
    """Deterministic model double for evaluating grounded generation and citations."""

    def __init__(
        self,
        canned_answers: Mapping[str, dict[str, Any]] | None = None,
        default_answer: dict[str, Any] | None = None,
    ) -> None:
        self.canned_answers = dict(canned_answers) if canned_answers else {}
        self.default_answer = default_answer or {
            "answer": "Grounded answer from retrieved evidence.",
            "citations": [],
            "insufficient_evidence": False,
        }
        self.recorded_requests: list[CompletionRequest] = []

    async def generate(
        self,
        request: CompletionRequest,
        context: AiOperationContext | None = None,
    ) -> CompletionResponse:
        self.recorded_requests.append(request)

        # Extract user question from prompt if present
        user_question = ""
        if "USER QUESTION:" in request.prompt:
            parts = request.prompt.split("USER QUESTION:", 1)[1]
            user_question = parts.split("\n\n")[0].strip().lower()
        search_target = user_question if user_question else request.prompt.lower()

        # Look for matching query or scenario keywords
        selected = None
        for key, ans in self.canned_answers.items():
            if key.lower() in search_target:
                selected = dict(ans)
                evidence_ids = set(re.findall(r"\[Source ID:\s*([^\]]+)\]", request.prompt))
                if "citations" in selected and evidence_ids:
                    selected["citations"] = [cid for cid in selected["citations"] if cid in evidence_ids]
                break



        if not selected:
            # Check if evidence contains source IDs
            found_ids = re.findall(r"\[Source ID:\s*([^\]]+)\]", request.prompt)
            if not found_ids or "[NO RELEVANT EVIDENCE FOUND]" in request.prompt:
                selected = {
                    "answer": "I do not have sufficient evidence to answer this question.",
                    "citations": [],
                    "insufficient_evidence": True,
                }
            else:
                # Grounded default with first found source ID
                selected = {
                    "answer": f"According to enterprise records, this requirement is defined in {found_ids[0]}.",
                    "citations": [found_ids[0]],
                    "insufficient_evidence": False,
                }

        if selected and "raw_override" in selected:
            output_text = str(selected["raw_override"])
        else:
            output_text = json.dumps(selected)

        return CompletionResponse(
            text=output_text,
            model=request.model,
            finish_reason=FinishReason.STOP,
            usage=UsageMetrics(input_tokens=100, output_tokens=30, total_tokens=130),
            latency_ms=5.0,
        )
