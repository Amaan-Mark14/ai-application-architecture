#!/usr/bin/env python3
"""Evaluation runner for Knowledge Intelligence & RAG.

Meets Tier 2 Reference Evaluation requirements:
- Executes versioned dataset (32 scenarios across factual, semantic, distractor, metadata, insufficient evidence, adversarial).
- Measures retrieval metrics (Recall@K, Hit Rate, MRR, Context Relevance).
- Measures schema, expected-outcome heuristics, citation identity and abstention accuracy.
- Assesses metrics against Tier 1 reference thresholds in QUALITY-GATES.md:
  * Claim-level faithfulness >= 0.85 remains NOT VERIFIED (not measured by the heuristic)
  * Context Relevance >= 0.80
  * Schema Adherence >= 0.98
- Supports offline deterministic evaluation (--mode fake) and live model evaluation (--mode live).
- Distinguishes evaluation harness execution, real AI model quality, and Quality Gate tier applicability.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BUILDING_BLOCKS_DIR = REPO_ROOT / "building-blocks" / "python"
PLATFORM_OLLAMA_DIR = REPO_ROOT / "platform" / "ollama-adapter"
PLATFORM_EMBEDDING_DIR = REPO_ROOT / "platform" / "ollama-embedding-adapter"
RAG_DIR = Path(__file__).resolve().parent

for p in (BUILDING_BLOCKS_DIR, PLATFORM_OLLAMA_DIR, PLATFORM_EMBEDDING_DIR, RAG_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from contracts.models import EmbeddingRequest
from contracts.ports import EmbeddingPort, TextGenerationPort
from ollama_adapter.adapter import OllamaAdapter
from ollama_embedding_adapter.adapter import OllamaEmbeddingAdapter
from rag.chunker import HeadingAwareChunker
from rag.citation_validator import CitationValidator
from rag.document import Chunk, Document
from rag.ingestion import load_corpus_directory
from rag.retriever import Retriever
from rag.service import RAGResponse, RAGService
from rag.test_doubles import DeterministicEmbeddingStub, DeterministicRAGGenerationStub
from rag.vector_index import InMemoryVectorIndex

# Authoritative Gate D Thresholds from QUALITY-GATES.md
GATE_D_MIN_SCENARIOS = 30
GATE_D_GROUNDEDNESS_THRESHOLD = 0.85
GATE_D_CONTEXT_RELEVANCE_THRESHOLD = 0.80
GATE_D_SCHEMA_ADHERENCE_THRESHOLD = 0.98


@dataclass(frozen=True)
class EvalScenario:
    scenario_id: str
    scenario_type: str
    query: str
    expected_chunk_ids: list[str]
    expected_document_ids: list[str]
    expected_facts: list[str]
    expected_abstention: bool
    metadata_filter: dict[str, Any] | None
    description: str
    adversarial: bool = False
    forbidden_answer_fragments: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ScenarioEvalResult:
    scenario_id: str
    scenario_type: str
    query: str
    retrieved_chunk_ids: list[str]
    recall_at_k: float
    hit: bool
    reciprocal_rank: float
    context_relevance: float
    schema_valid: bool
    expected_outcome_passed: bool
    citation_accuracy: float
    abstention_accurate: bool
    latency_ms: float
    answer: str
    error: str | None = None
    adversarial_passed: bool | None = None


@dataclass
class RAGEvalSummary:
    total_scenarios: int
    mode: str
    timestamp: str
    # Retrieval Metrics
    hit_rate: float
    mean_recall_at_k: float
    mrr: float
    mean_context_relevance: float
    # Generation Metrics
    schema_adherence_rate: float
    expected_outcome_rate: float
    citation_accuracy_rate: float
    abstention_accuracy_rate: float
    avg_latency_ms: float
    # Evaluation Harness & Governance Assessment
    harness_passed: bool
    reference_thresholds_met: bool
    reference_threshold_details: dict[str, Any]
    real_ai_quality_verified: bool
    software_checks_passed: bool = False
    gate_d_applicable: bool = False
    gate_d_status: str = "NOT_APPLICABLE"
    type_breakdown: dict[str, dict[str, float]] = field(default_factory=dict)


def load_dataset(dataset_path: Path) -> list[EvalScenario]:
    """Load and validate the versioned eval_dataset.jsonl file."""
    if not dataset_path.is_file():
        raise FileNotFoundError(f"Evaluation dataset not found at: {dataset_path}")

    scenarios: list[EvalScenario] = []
    with open(dataset_path, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            data = json.loads(line)
            if (
                type(data.get("expected_abstention", False)) is not bool
                or type(data.get("adversarial", False)) is not bool
            ):
                raise ValueError(f"Scenario booleans must be actual booleans at line {line_num}")
            forbidden = data.get("forbidden_answer_fragments", [])
            if not isinstance(forbidden, list) or any(
                not isinstance(item, str) or not item for item in forbidden
            ):
                raise ValueError(f"Invalid forbidden_answer_fragments at line {line_num}")
            if data.get("adversarial") and not data.get("scenario_type", "").startswith(
                "adversarial"
            ):
                raise ValueError(
                    f"Adversarial scenarios require an adversarial scenario_type at line {line_num}"
                )
            scenarios.append(
                EvalScenario(
                    scenario_id=data["scenario_id"],
                    scenario_type=data["scenario_type"],
                    query=data["query"],
                    expected_chunk_ids=data.get("expected_chunk_ids", []),
                    expected_document_ids=data.get("expected_document_ids", []),
                    expected_facts=data.get("expected_facts", []),
                    expected_abstention=data.get("expected_abstention", False),
                    metadata_filter=data.get("metadata_filter"),
                    description=data.get("description", ""),
                    adversarial=data.get("adversarial", False),
                    forbidden_answer_fragments=forbidden,
                )
            )

    if len(scenarios) < GATE_D_MIN_SCENARIOS:
        raise ValueError(
            f"Gate D requires >= {GATE_D_MIN_SCENARIOS} scenarios, but dataset only contains {len(scenarios)}"
        )
    return scenarios


async def setup_knowledge_index(
    corpus_dir: Path,
    embedding_port: EmbeddingPort,
    embedding_model: str,
) -> tuple[InMemoryVectorIndex, list[Document], list[Chunk]]:
    """Ingest, chunk, embed, and index the reference corpus."""
    docs = load_corpus_directory(corpus_dir)
    chunker = HeadingAwareChunker(max_chunk_chars=1000)

    all_chunks: list[Chunk] = []

    for doc in docs:
        chunks = chunker.chunk_document(doc)
        all_chunks.extend(chunks)

    # Batch embedding
    chunk_texts = [c.text for c in all_chunks]
    req = EmbeddingRequest(inputs=chunk_texts, model=embedding_model)
    res = await embedding_port.embed(req)

    index = InMemoryVectorIndex(dimensions=res.dimensions)
    index.add_batch(all_chunks, res.embeddings)

    return index, docs, all_chunks


def evaluate_single_scenario(
    scenario: EvalScenario,
    response: RAGResponse,
) -> ScenarioEvalResult:
    """Score retrieval and grounded generation for a single scenario."""
    retrieved_cids = [r.chunk_id for r in response.retrieved_results]
    expected_cids = set(scenario.expected_chunk_ids)

    # 1. Retrieval Metrics
    if scenario.expected_abstention or not expected_cids:
        # For unanswerable queries, retrieval metrics don't penalize if no chunks were relevant
        recall = 1.0 if not expected_cids else 0.0
        hit = response.insufficient_evidence
        rr = 1.0 if response.insufficient_evidence else 0.0
        context_relevance = 1.0 if response.insufficient_evidence else 0.5
    else:
        found_expected = set(retrieved_cids) & expected_cids
        recall = len(found_expected) / len(expected_cids)
        hit = len(found_expected) > 0

        # Reciprocal rank (1 / position of first relevant chunk)
        rr = 0.0
        for rank, cid in enumerate(retrieved_cids, start=1):
            if cid in expected_cids:
                rr = 1.0 / rank
                break

        # Context relevance: ratio of retrieved chunks that belong to expected documents or chunks
        relevant_count = sum(
            1
            for r in response.retrieved_results
            if r.chunk_id in expected_cids or r.document_id in scenario.expected_document_ids
        )
        context_relevance = (
            relevant_count / len(response.retrieved_results) if response.retrieved_results else 0.0
        )

    # 2. Generation Metrics
    # Schema adherence: check if model output satisfied the schema without fallback and has no schema_error
    schema_valid = (
        response.schema_valid
        and response.citation_validation.is_valid
        and not response.metadata.get("schema_error")
    )

    # Abstention accuracy
    if scenario.expected_abstention:
        abstention_accurate = response.accepted and response.insufficient_evidence
    else:
        abstention_accurate = response.accepted and not response.insufficient_evidence

    # Narrow outcome heuristic: expected-fact substrings and citation identity, not entailment.
    if scenario.expected_abstention:
        expected_outcome_passed = schema_valid and abstention_accurate
    else:
        # Check that verified citations exist
        has_verified_citations = len(response.citations) > 0
        # Check expected facts presence in answer
        facts_present = all(
            fact.lower() in response.answer.lower() for fact in scenario.expected_facts
        )
        expected_outcome_passed = (
            schema_valid
            and response.accepted
            and has_verified_citations
            and facts_present
            and not response.insufficient_evidence
        )

    # Citation accuracy: verified citations / total citations produced
    if scenario.expected_abstention:
        citation_acc = 1.0 if response.accepted and len(response.citations) == 0 else 0.0
    else:
        total_cited = len(response.citation_validation.verified_citations) + len(
            response.citation_validation.rejected_citation_ids
        )
        if total_cited == 0:
            citation_acc = 0.0
        else:
            citation_acc = len(response.citation_validation.verified_citations) / total_cited

    return ScenarioEvalResult(
        scenario_id=scenario.scenario_id,
        scenario_type=scenario.scenario_type,
        query=scenario.query,
        retrieved_chunk_ids=retrieved_cids,
        recall_at_k=recall,
        hit=hit,
        reciprocal_rank=rr,
        context_relevance=context_relevance,
        schema_valid=schema_valid,
        expected_outcome_passed=expected_outcome_passed,
        citation_accuracy=citation_acc,
        abstention_accurate=abstention_accurate,
        latency_ms=response.latency_ms,
        answer=response.answer,
        error=response.validation_error,
        adversarial_passed=(
            expected_outcome_passed
            and not any(
                fragment.casefold() in response.answer.casefold()
                for fragment in scenario.forbidden_answer_fragments
            )
        )
        if scenario.adversarial
        else None,
    )


async def run_evaluation(
    dataset_path: Path,
    corpus_dir: Path,
    mode: str = "fake",
    embedding_model: str = "nomic-embed-text",
    generation_model: str = "llama3.2",
    endpoint: str = "http://localhost:11434",
    top_k: int = 2,
    allow_unverified: bool = False,
) -> tuple[RAGEvalSummary | None, list[ScenarioEvalResult]]:
    """Run full evaluation suite over dataset scenarios."""
    scenarios = load_dataset(dataset_path)

    if mode == "live":
        print(f"Connecting to live Ollama runtime at: {endpoint}")
        embed_adapter = OllamaEmbeddingAdapter(endpoint=endpoint, default_model=embedding_model)
        gen_adapter = OllamaAdapter(endpoint=endpoint, default_model=generation_model)

        is_healthy = await embed_adapter.check_health()
        if not is_healthy:
            print("\n[LIVE EVALUATION STATUS: NOT VERIFIED]")
            print(f"Reason: Ollama daemon is unreachable at '{endpoint}'.")
            if allow_unverified:
                return None, []
            sys.exit(1)

        installed = await embed_adapter.get_installed_models()
        # Verify models are installed
        has_embed_model = any(embedding_model.split(":")[0] in m for m in installed)
        has_gen_model = any(generation_model.split(":")[0] in m for m in installed)

        if not has_embed_model or not has_gen_model:
            print("\n[LIVE EVALUATION STATUS: NOT VERIFIED]")
            print(f"Installed models: {installed}")
            if not has_embed_model:
                print(
                    f"Missing required embedding model: '{embedding_model}'. Run `ollama pull {embedding_model}`."
                )
            if not has_gen_model:
                print(
                    f"Missing required generation model: '{generation_model}'. Run `ollama pull {generation_model}`."
                )
            if allow_unverified:
                return None, []
            sys.exit(1)

        embedding_port: EmbeddingPort = embed_adapter
        llm_port: TextGenerationPort = gen_adapter
    else:
        embedding_port = DeterministicEmbeddingStub(dimensions=512)
        # Create deterministic canned responses for the scenarios
        canned: dict[str, dict[str, Any]] = {}
        for sc in scenarios:
            if sc.expected_abstention:
                canned[sc.query] = {
                    "answer": "I do not have sufficient evidence in the knowledge base to answer this question.",
                    "citations": [],
                    "insufficient_evidence": True,
                }
            else:
                facts_str = " ".join(sc.expected_facts)
                canned[sc.query] = {
                    "answer": f"According to verified enterprise policy, {facts_str}.",
                    "citations": list(sc.expected_chunk_ids),
                    "insufficient_evidence": False,
                }
        llm_port = DeterministicRAGGenerationStub(canned_answers=canned)

    # Index corpus
    print(f"Indexing knowledge corpus from '{corpus_dir.name}'...")
    index, docs, chunks = await setup_knowledge_index(corpus_dir, embedding_port, embedding_model)
    print(f"Indexed {len(docs)} documents ({len(chunks)} chunks) into in-memory vector index.")

    retriever = Retriever(
        embedding_port=embedding_port,
        vector_index=index,
        embedding_model=embedding_model,
        default_top_k=top_k,
    )
    validator = CitationValidator(document_catalog={d.document_id: d for d in docs})
    service = RAGService(
        retriever=retriever,
        llm_client=llm_port,
        citation_validator=validator,
        model=generation_model,
    )

    print(
        f"\nExecuting evaluation across {len(scenarios)} scenarios (mode={mode}, top_k={top_k})..."
    )
    results: list[ScenarioEvalResult] = []

    for i, sc in enumerate(scenarios, start=1):
        res = await service.answer(
            query=sc.query,
            top_k=top_k,
            filters=sc.metadata_filter,
        )

        eval_res = evaluate_single_scenario(sc, res)
        results.append(eval_res)

        passed = (
            eval_res.expected_outcome_passed
            and eval_res.hit
            and eval_res.adversarial_passed is not False
        )
        status_symbol = "PASS" if passed else "FAIL"
        if sc.scenario_type.startswith("adversarial") and eval_res.adversarial_passed is None:
            status_symbol = "NOT VERIFIED"
        print(
            f"[{i:02d}/{len(scenarios):02d}] {status_symbol} {sc.scenario_id} ({sc.scenario_type}): {sc.query[:50]}..."
        )

    return summarize_results(results, mode), results


def summarize_results(results: list[ScenarioEvalResult], mode: str) -> RAGEvalSummary:
    """Aggregate observed outcomes; unavailable evidence can never become a pass."""
    if not results:
        raise ValueError("Cannot summarize an empty evaluation")
    if len({result.scenario_id for result in results}) != len(results):
        raise ValueError("Evaluation scenario IDs must be unique")
    # Aggregate metrics
    total = len(results)
    mean_recall = sum(r.recall_at_k for r in results) / total
    hit_rate = sum(1 for r in results if r.hit) / total
    mrr = sum(r.reciprocal_rank for r in results) / total
    mean_context_rel = sum(r.context_relevance for r in results) / total
    schema_adh = sum(1 for r in results if r.schema_valid) / total
    expected_outcome_rate = sum(1 for r in results if r.expected_outcome_passed) / total
    citation_acc = sum(r.citation_accuracy for r in results) / total
    abstention_acc = sum(1 for r in results if r.abstention_accurate) / total
    avg_latency = sum(r.latency_ms for r in results) / total

    adversarial = [result for result in results if result.scenario_type.startswith("adversarial")]
    adversarial_measured = bool(adversarial) and all(
        result.adversarial_passed is not None for result in adversarial
    )
    adversarial_passed = (
        all(result.adversarial_passed is True for result in adversarial)
        if adversarial_measured
        else None
    )
    adversarial_detail = {
        "measured": sum(result.adversarial_passed is True for result in adversarial)
        / len(adversarial)
        if adversarial_measured
        else None,
        "required": 1.0,
        "passed": adversarial_passed,
        "scenario_count": len(adversarial),
        "status": "MEASURED" if adversarial_measured else "NOT_VERIFIED",
        "scope": "Declared schema, expected-outcome and forbidden-fragment checks only",
    }
    # Faithfulness requires reviewed claim-level assessment, which this harness lacks.
    reference_threshold_details: dict[str, dict[str, Any]] = {
        "scenario_count": {
            "measured": total,
            "required": GATE_D_MIN_SCENARIOS,
            "passed": total >= GATE_D_MIN_SCENARIOS,
        },
        "faithfulness": {
            "measured": None,
            "required": GATE_D_GROUNDEDNESS_THRESHOLD,
            "passed": None,
            "status": "NOT_VERIFIED",
        },
        "context_relevance": {
            "measured": round(mean_context_rel, 4),
            "required": GATE_D_CONTEXT_RELEVANCE_THRESHOLD,
            "passed": mean_context_rel >= GATE_D_CONTEXT_RELEVANCE_THRESHOLD,
        },
        "schema_adherence": {
            "measured": round(schema_adh, 4),
            "required": GATE_D_SCHEMA_ADHERENCE_THRESHOLD,
            "passed": schema_adh >= GATE_D_SCHEMA_ADHERENCE_THRESHOLD,
        },
        "adversarial_handling": adversarial_detail,
    }
    reference_thresholds_met = all(
        check["passed"] is True for check in reference_threshold_details.values()
    )
    harness_passed = True  # Evaluation harness executed completely without unhandled errors
    real_ai_quality_verified = False  # Live execution alone cannot verify factual support.
    gate_d_applicable = (
        False  # Per QUALITY-GATES.md, Gate D applies to Tier 1 Reference Applications
    )
    gate_d_status = "NOT_APPLICABLE"

    # Type breakdown
    types = sorted({result.scenario_type for result in results})
    type_breakdown: dict[str, dict[str, float]] = {}
    for st in types:
        st_res = [r for r in results if r.scenario_type == st]
        type_breakdown[st] = {
            "count": len(st_res),
            "hit_rate": round(sum(1 for r in st_res if r.hit) / len(st_res), 3),
            "expected_outcome_rate": round(
                sum(1 for r in st_res if r.expected_outcome_passed) / len(st_res), 3
            ),
        }

    summary = RAGEvalSummary(
        total_scenarios=total,
        mode=mode,
        timestamp=datetime.now(UTC).isoformat(),
        hit_rate=round(hit_rate, 4),
        mean_recall_at_k=round(mean_recall, 4),
        mrr=round(mrr, 4),
        mean_context_relevance=round(mean_context_rel, 4),
        schema_adherence_rate=round(schema_adh, 4),
        expected_outcome_rate=round(expected_outcome_rate, 4),
        citation_accuracy_rate=round(citation_acc, 4),
        abstention_accuracy_rate=round(abstention_acc, 4),
        avg_latency_ms=round(avg_latency, 2),
        harness_passed=harness_passed,
        reference_thresholds_met=reference_thresholds_met,
        reference_threshold_details=reference_threshold_details,
        real_ai_quality_verified=real_ai_quality_verified,
        software_checks_passed=expected_outcome_rate == 1.0
        and all(
            check["passed"] is True
            for name, check in reference_threshold_details.items()
            if name != "faithfulness"
        ),
        gate_d_applicable=gate_d_applicable,
        gate_d_status=gate_d_status,
        type_breakdown=type_breakdown,
    )

    return summary


def print_report(summary: RAGEvalSummary) -> None:
    """Print clean terminal report adhering to repository verification standards."""
    print("\n" + "=" * 70)
    print("ai-application-architecture — Knowledge Intelligence & RAG Evaluation")
    print("=" * 70)
    print(f"Timestamp:          {summary.timestamp}")
    print(f"Evaluation Mode:    {summary.mode.upper()}")
    print(f"Total Scenarios:    {summary.total_scenarios}")
    print(f"Average Latency:    {summary.avg_latency_ms:.1f}ms")
    print("-" * 70)
    print("RETRIEVAL METRICS:")
    print(f"  Hit Rate:                 {summary.hit_rate * 100:.1f}%")
    print(f"  Recall@K:                 {summary.mean_recall_at_k * 100:.1f}%")
    print(f"  MRR (Mean Reciprocal Rank): {summary.mrr:.3f}")
    print(
        f"  Context Relevance:        {summary.mean_context_relevance * 100:.1f}% (Threshold: >= {GATE_D_CONTEXT_RELEVANCE_THRESHOLD * 100:.0f}%)"
    )
    print("-" * 70)
    print("GENERATION CONTRACTS & NARROW OUTCOME HEURISTICS:")
    print(
        f"  Schema Adherence:         {summary.schema_adherence_rate * 100:.1f}% (Threshold: >= {GATE_D_SCHEMA_ADHERENCE_THRESHOLD * 100:.0f}%)"
    )
    print(
        f"  Expected Outcome Heuristic: {summary.expected_outcome_rate * 100:.1f}% (facts/citations or abstention)"
    )
    print("  Claim-level Faithfulness:   [NOT VERIFIED]")
    print("  Citation accuracy measures supplied-source identity, not factual entailment.")
    adversarial = summary.reference_threshold_details.get("adversarial_handling", {})
    print(
        f"  Adversarial Declared Checks: {adversarial.get('passed')} (n={adversarial.get('scenario_count', 0)})"
    )
    print(f"  Citation Accuracy:        {summary.citation_accuracy_rate * 100:.1f}%")
    print(f"  Abstention Accuracy:      {summary.abstention_accuracy_rate * 100:.1f}%")
    print("-" * 70)
    print("SCENARIO TYPE BREAKDOWN:")
    for st, data in summary.type_breakdown.items():
        print(
            f"  {st:<28} (n={data['count']:<2}) Hit: {data['hit_rate'] * 100:>5.1f}% | Outcome: {data['expected_outcome_rate'] * 100:>5.1f}%"
        )
    print("-" * 70)
    print("QUALITY GOVERNANCE & EVALUATION ASSESSMENT:")
    harness_status = "PASS" if summary.harness_passed else "FAIL"
    software_status = "PASS" if summary.software_checks_passed else "FAIL"
    print(f"  Evaluation Harness Validation: [{harness_status}]")
    print(f"  Observed Software Checks:      [{software_status}]")
    print("  Real AI RAG Quality:          [NOT VERIFIED]")
    print("    -> Expected-fact matching does not establish support for every generated claim.")
    if summary.mode == "fake":
        print("    -> Deterministic test doubles validate software mechanics only.")
    print("  Gate D Applicability:         [NOT APPLICABLE TO TIER 2]")
    print("    -> Full reference thresholds remain unverified without claim-level assessment.")
    print("=" * 70)


def save_report(
    summary: RAGEvalSummary, results: list[ScenarioEvalResult], output_dir: Path
) -> Path:
    """Save structured evaluation report JSON file."""
    output_dir.mkdir(parents=True, exist_ok=True)
    report_file = output_dir / f"eval_report_{int(time.time())}.json"
    report_data = {
        "summary": asdict(summary),
        "scenarios": [asdict(r) for r in results],
    }
    report_file.write_text(json.dumps(report_data, indent=2), encoding="utf-8")
    return report_file


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate Knowledge Intelligence and RAG capability."
    )
    parser.add_argument(
        "--mode", choices=["fake", "live"], default="fake", help="Execution mode (default: fake)"
    )
    parser.add_argument(
        "--dataset", default=str(RAG_DIR / "eval_dataset.jsonl"), help="Path to eval_dataset.jsonl"
    )
    parser.add_argument(
        "--corpus", default=str(RAG_DIR / "corpus"), help="Path to corpus directory"
    )
    parser.add_argument(
        "--endpoint", default="http://localhost:11434", help="Ollama API base URL for live mode"
    )
    parser.add_argument("--embed-model", default="nomic-embed-text", help="Embedding model tag")
    parser.add_argument("--gen-model", default="llama3.2", help="Generation model tag")
    parser.add_argument("--top-k", type=int, default=2, help="Retrieval top-k depth (default: 2)")
    parser.add_argument("--output-dir", default=str(RAG_DIR / "results"), help="Results directory")
    parser.add_argument(
        "--allow-unverified",
        action="store_true",
        help="Exit 0 when live infrastructure is unavailable",
    )
    args = parser.parse_args()

    dataset_path = Path(args.dataset)
    corpus_dir = Path(args.corpus)
    output_dir = Path(args.output_dir)

    summary, results = asyncio.run(
        run_evaluation(
            dataset_path=dataset_path,
            corpus_dir=corpus_dir,
            mode=args.mode,
            embedding_model=args.embed_model,
            generation_model=args.gen_model,
            endpoint=args.endpoint,
            top_k=args.top_k,
            allow_unverified=args.allow_unverified,
        )
    )

    if summary is None:
        return 0 if args.allow_unverified else 1

    print_report(summary)
    report_path = save_report(summary, results, output_dir)
    try:
        display_path = report_path.relative_to(REPO_ROOT)
    except ValueError:
        display_path = report_path
    print(f"Report saved to: {display_path}")

    passed = (
        summary.software_checks_passed if args.mode == "fake" else summary.reference_thresholds_met
    )
    return 0 if summary.harness_passed and passed else 1


if __name__ == "__main__":
    sys.exit(main())
