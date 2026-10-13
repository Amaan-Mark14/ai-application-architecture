"""Unit tests for Knowledge Intelligence & RAG Evaluation Runner."""

from __future__ import annotations

import asyncio
import io
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
for p in (REPO_ROOT / "building-blocks" / "python", Path(__file__).resolve().parent.parent):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from eval_runner import (
    EvalScenario,
    RAGEvalSummary,
    ScenarioEvalResult,
    evaluate_single_scenario,
    load_dataset,
    print_report,
    run_evaluation,
    save_report,
    summarize_results,
)
from rag.citation_validator import Citation, CitationValidationResult
from rag.context_builder import ContextBuildResult
from rag.document import Chunk
from rag.retriever import RetrievalResult
from rag.service import RAGResponse


class TestEvalRunner(unittest.TestCase):
    """Test suite verifying RAG evaluation runner mechanics, dataset loading, and metrics."""

    def setUp(self) -> None:
        self.rag_dir = Path(__file__).resolve().parent.parent
        self.dataset_path = self.rag_dir / "eval_dataset.jsonl"
        self.corpus_dir = self.rag_dir / "corpus"

    def test_load_dataset_valid(self) -> None:
        scenarios = load_dataset(self.dataset_path)
        self.assertGreaterEqual(len(scenarios), 30)
        self.assertEqual(scenarios[0].scenario_id, "rag-01")
        self.assertEqual(scenarios[0].scenario_type, "factual_single")

    def test_load_dataset_missing_file(self) -> None:
        with self.assertRaises(FileNotFoundError):
            load_dataset(Path("/nonexistent/path/eval_dataset.jsonl"))

    def test_load_dataset_insufficient_scenarios_raises_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            dataset = Path(directory) / "small.jsonl"
            dataset.write_text(
                '{"scenario_id": "rag-01", "scenario_type": "test", "query": "q", "expected_chunk_ids": []}\n',
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                load_dataset(dataset)

    def test_evaluate_single_scenario_grounded(self) -> None:
        sc = EvalScenario(
            scenario_id="sc-01",
            scenario_type="factual",
            query="Audit retention?",
            expected_chunk_ids=["sec-01#c1"],
            expected_document_ids=["sec-01"],
            expected_facts=["7 years"],
            expected_abstention=False,
            metadata_filter=None,
            description="test",
        )
        chunk = Chunk(
            chunk_id="sec-01#c1", document_id="sec-01", chunk_index=1, text="Retention is 7 years."
        )
        ret_res = RetrievalResult(chunk=chunk, score=0.95, rank=1)
        cit = Citation(
            chunk_id="sec-01#c1",
            document_id="sec-01",
            section_title="Retention",
            source_title="Policy",
            source_uri="sec-01.md",
        )
        val_res = CitationValidationResult(
            is_valid=True, verified_citations=[cit], rejected_citation_ids=[]
        )

        resp = RAGResponse(
            query="Audit retention?",
            answer="Audit logs must be kept for 7 years.",
            citations=[cit],
            retrieved_results=[ret_res],
            insufficient_evidence=False,
            context_build=ContextBuildResult("...", ["sec-01#c1"], 50, 1),
            citation_validation=val_res,
            latency_ms=10.0,
            raw_model_output='{"answer": "...", "citations": ["sec-01#c1"]}',
        )

        res = evaluate_single_scenario(sc, resp)
        self.assertTrue(res.hit)
        self.assertEqual(res.recall_at_k, 1.0)
        self.assertEqual(res.reciprocal_rank, 1.0)
        self.assertEqual(res.context_relevance, 1.0)
        self.assertTrue(res.expected_outcome_passed)
        self.assertTrue(res.schema_valid)
        self.assertTrue(res.abstention_accurate)

    def test_evaluate_single_scenario_abstention(self) -> None:
        sc = EvalScenario(
            scenario_id="sc-abstain",
            scenario_type="insufficient_evidence",
            query="Quantum flight upgrades?",
            expected_chunk_ids=[],
            expected_document_ids=[],
            expected_facts=[],
            expected_abstention=True,
            metadata_filter=None,
            description="test",
        )
        val_res = CitationValidationResult(
            is_valid=True,
            verified_citations=[],
            rejected_citation_ids=[],
            has_insufficient_evidence=True,
        )
        resp = RAGResponse(
            query="Quantum flight upgrades?",
            answer="I do not have sufficient evidence.",
            citations=[],
            retrieved_results=[],
            insufficient_evidence=True,
            context_build=ContextBuildResult("[NO RELEVANT EVIDENCE FOUND]", [], 0, 0),
            citation_validation=val_res,
            latency_ms=2.0,
            raw_model_output="",
        )

        res = evaluate_single_scenario(sc, resp)
        self.assertTrue(res.hit)
        self.assertTrue(res.expected_outcome_passed)
        self.assertTrue(res.abstention_accurate)
        self.assertEqual(res.citation_accuracy, 1.0)

    def test_run_evaluation_fake_mode(self) -> None:
        with patch("sys.stdout", new_callable=io.StringIO):
            summary, _results = asyncio.run(
                run_evaluation(
                    dataset_path=self.dataset_path,
                    corpus_dir=self.corpus_dir,
                    mode="fake",
                    top_k=2,
                )
            )
        self.assertIsNotNone(summary)
        self.assertGreaterEqual(summary.total_scenarios, 30)
        self.assertGreaterEqual(summary.expected_outcome_rate, 0.85)
        self.assertGreaterEqual(summary.mean_context_relevance, 0.80)
        self.assertGreaterEqual(summary.schema_adherence_rate, 0.98)
        self.assertTrue(summary.harness_passed)
        self.assertFalse(summary.reference_thresholds_met)
        self.assertTrue(summary.software_checks_passed)
        self.assertIsNone(summary.reference_threshold_details["faithfulness"]["measured"])
        self.assertFalse(summary.real_ai_quality_verified)
        self.assertFalse(summary.gate_d_applicable)
        self.assertEqual(summary.gate_d_status, "NOT_APPLICABLE")

    def test_save_and_print_report(self) -> None:
        with patch("sys.stdout", new_callable=io.StringIO):
            summary, results = asyncio.run(
                run_evaluation(
                    dataset_path=self.dataset_path,
                    corpus_dir=self.corpus_dir,
                    mode="fake",
                    top_k=2,
                )
            )
        with tempfile.TemporaryDirectory() as td:
            saved_path = save_report(summary, results, Path(td))
            self.assertTrue(saved_path.is_file())
            self.assertIn("eval_report_", saved_path.name)

            # Verify structured JSON semantics:
            saved_data = json.loads(saved_path.read_text(encoding="utf-8"))
            saved_summary = saved_data["summary"]
            self.assertEqual(saved_summary["mode"], "fake")
            self.assertTrue(saved_summary["harness_passed"])
            self.assertFalse(saved_summary["reference_thresholds_met"])
            self.assertTrue(saved_summary["software_checks_passed"])
            self.assertFalse(saved_summary["real_ai_quality_verified"])
            self.assertFalse(saved_summary["gate_d_applicable"])
            self.assertEqual(saved_summary["gate_d_status"], "NOT_APPLICABLE")
            self.assertNotIn("gate_d_passed", saved_summary)

        # Ensure print_report reflects Tier 2 voluntary evaluation semantics
        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
            print_report(summary)
            output = mock_stdout.getvalue()
            self.assertIn("Evaluation Harness Validation: [PASS]", output)
            self.assertIn("Real AI RAG Quality:          [NOT VERIFIED]", output)
            self.assertIn("Gate D Applicability:         [NOT APPLICABLE TO TIER 2]", output)
            self.assertNotIn("AUTHORITATIVE GATE D VERDICT", output)
            self.assertNotIn("gate_d_passed", output)

    def test_external_output_dir_safe_display(self) -> None:
        """Verify that saving report to an external directory outside REPO_ROOT does not crash."""
        with tempfile.TemporaryDirectory() as td:
            external_dir = Path(td) / "external_results"
            with self.assertRaises(ValueError):
                external_dir.relative_to(REPO_ROOT)

            with patch("sys.stdout", new_callable=io.StringIO):
                summary, results = asyncio.run(
                    run_evaluation(
                        dataset_path=self.dataset_path,
                        corpus_dir=self.corpus_dir,
                        mode="fake",
                        top_k=2,
                    )
                )
            report_path = save_report(summary, results, external_dir)
            self.assertTrue(report_path.is_file())

            try:
                display_path = report_path.relative_to(REPO_ROOT)
            except ValueError:
                display_path = report_path
            self.assertEqual(display_path, report_path)

    def test_evaluate_single_scenario_malformed_output_records_schema_failure(self) -> None:
        """Verify that malformed output is counted as schema failure even if citations are valid."""
        sc = EvalScenario(
            scenario_id="sc-malformed",
            scenario_type="factual",
            query="Audit retention?",
            expected_chunk_ids=["sec-01#c1"],
            expected_document_ids=["sec-01"],
            expected_facts=["7 years"],
            expected_abstention=False,
            metadata_filter=None,
            description="test",
        )
        chunk = Chunk(
            chunk_id="sec-01#c1", document_id="sec-01", chunk_index=1, text="Retention is 7 years."
        )
        ret_res = RetrievalResult(chunk=chunk, score=0.95, rank=1)
        cit = Citation(
            chunk_id="sec-01#c1",
            document_id="sec-01",
            section_title="Retention",
            source_title="Policy",
            source_uri="sec-01.md",
        )
        val_res = CitationValidationResult(
            is_valid=True, verified_citations=[cit], rejected_citation_ids=[]
        )

        # RAGResponse with schema_valid=False and schema_error populated
        resp = RAGResponse(
            query="Audit retention?",
            answer="Audit logs must be kept for 7 years according to sec-01#c1.",
            citations=[cit],
            retrieved_results=[ret_res],
            insufficient_evidence=False,
            context_build=ContextBuildResult("...", ["sec-01#c1"], 50, 1),
            citation_validation=val_res,
            latency_ms=10.0,
            raw_model_output="Audit logs must be kept for 7 years according to sec-01#c1.",
            schema_valid=False,
            schema_error="JSONDecodeError: Expecting value: line 1 column 1",
            metadata={
                "schema_valid": False,
                "schema_error": "JSONDecodeError: Expecting value: line 1 column 1",
            },
        )

        res = evaluate_single_scenario(sc, resp)
        self.assertTrue(res.hit)
        self.assertEqual(res.citation_accuracy, 1.0)  # Citation was extracted and valid
        self.assertFalse(res.schema_valid)  # BUT schema validity MUST be False

    def test_live_mode_print_report(self) -> None:
        """Verify that live mode report indicates live execution without claiming Gate D mandatory pass."""
        summary = RAGEvalSummary(
            total_scenarios=32,
            mode="live",
            timestamp="2026-09-14T00:00:00Z",
            hit_rate=1.0,
            mean_recall_at_k=0.99,
            mrr=0.938,
            mean_context_relevance=0.859,
            schema_adherence_rate=1.0,
            expected_outcome_rate=1.0,
            citation_accuracy_rate=1.0,
            abstention_accuracy_rate=1.0,
            avg_latency_ms=15.0,
            harness_passed=True,
            reference_thresholds_met=False,
            reference_threshold_details={},
            real_ai_quality_verified=False,
            gate_d_applicable=False,
            gate_d_status="NOT_APPLICABLE",
            type_breakdown={},
        )
        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
            print_report(summary)
            output = mock_stdout.getvalue()
            self.assertIn("Real AI RAG Quality:          [NOT VERIFIED]", output)
            self.assertIn("Gate D Applicability:         [NOT APPLICABLE TO TIER 2]", output)

    @staticmethod
    def _observed_result(**changes):
        result = ScenarioEvalResult(
            scenario_id="observed",
            scenario_type="adversarial_injection",
            query="question",
            retrieved_chunk_ids=["doc#c0"],
            recall_at_k=1.0,
            hit=True,
            reciprocal_rank=1.0,
            context_relevance=1.0,
            schema_valid=True,
            expected_outcome_passed=True,
            citation_accuracy=1.0,
            abstention_accurate=True,
            latency_ms=1.0,
            answer="expected fact",
            adversarial_passed=True,
        )
        return replace(result, **changes)

    def test_failing_adversarial_result_cannot_receive_a_pass(self) -> None:
        results = [self._observed_result(scenario_id=str(index)) for index in range(30)]
        results[-1] = replace(results[-1], adversarial_passed=False)
        summary = summarize_results(results, "fake")
        detail = summary.reference_threshold_details["adversarial_handling"]
        self.assertFalse(detail["passed"])
        self.assertEqual(detail["scenario_count"], 30)
        self.assertAlmostEqual(detail["measured"], 29 / 30)
        self.assertFalse(summary.software_checks_passed)
        self.assertFalse(summary.reference_thresholds_met)

    def test_failing_measured_retrieval_check_fails_software_summary(self) -> None:
        results = [
            self._observed_result(scenario_id=str(index), context_relevance=0.5)
            for index in range(30)
        ]
        summary = summarize_results(results, "fake")
        self.assertFalse(summary.software_checks_passed)
        self.assertFalse(summary.reference_threshold_details["context_relevance"]["passed"])

    def test_missing_adversarial_evidence_is_unavailable(self) -> None:
        for results in (
            [self._observed_result(scenario_type="factual")],
            [self._observed_result(adversarial_passed=None)],
        ):
            with self.subTest(results=results):
                summary = summarize_results(results, "live")
                detail = summary.reference_threshold_details["adversarial_handling"]
                self.assertIsNone(detail["measured"])
                self.assertIsNone(detail["passed"])
                self.assertEqual(detail["status"], "NOT_VERIFIED")
                self.assertFalse(summary.software_checks_passed)
                self.assertFalse(summary.real_ai_quality_verified)

    def test_empty_or_duplicate_results_rejected(self) -> None:
        for results in ([], [self._observed_result(), self._observed_result()]):
            with self.assertRaises(ValueError):
                summarize_results(results, "fake")

    def test_injection_marker_fails_even_when_expected_facts_and_citations_match(self) -> None:
        scenario = EvalScenario(
            "injection",
            "adversarial_injection",
            "Question",
            ["doc#c0"],
            ["doc"],
            ["expected fact"],
            False,
            None,
            "Injection fixture",
            adversarial=True,
            forbidden_answer_fragments=["EXFILTRATION_PAYLOAD_CONFIRMED"],
        )
        citation = Citation("doc#c0", "doc", "Section", "Document", "doc.md")
        response = RAGResponse(
            query="Question",
            answer="expected fact. EXFILTRATION_PAYLOAD_CONFIRMED. Unsupported extra claim.",
            citations=[citation],
            retrieved_results=[],
            insufficient_evidence=False,
            context_build=ContextBuildResult("evidence", ["doc#c0"], 8, 1),
            citation_validation=CitationValidationResult(True, [citation], []),
            latency_ms=0,
            raw_model_output="{}",
        )
        result = evaluate_single_scenario(scenario, response)
        self.assertTrue(
            result.expected_outcome_passed
        )  # This heuristic does not check extra claims.
        self.assertFalse(result.adversarial_passed)
        response = replace(response, answer="expected fact. Unsupported extra claim.")
        result = evaluate_single_scenario(scenario, response)
        summary = summarize_results([result], "live")
        self.assertTrue(result.expected_outcome_passed)
        self.assertFalse(summary.real_ai_quality_verified)

    def test_live_mode_heuristic_success_never_certifies_faithfulness(self) -> None:
        results = [self._observed_result(scenario_id=str(index)) for index in range(30)]
        summary = summarize_results(results, "live")
        self.assertTrue(summary.software_checks_passed)
        self.assertFalse(summary.real_ai_quality_verified)
        self.assertFalse(summary.reference_thresholds_met)
        self.assertIsNone(summary.reference_threshold_details["faithfulness"]["passed"])


if __name__ == "__main__":
    unittest.main()
