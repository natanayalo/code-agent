"""Integration coverage for memory retrieval evaluation through load_memory."""

from __future__ import annotations

from evaluation import evaluate_memory_retrieval, load_memory_retrieval_suite


def test_memory_retrieval_evaluation_captures_memory_loaded_payload(session_factory) -> None:
    """The retrieval evaluator should exercise build_load_memory_node output."""
    suite = load_memory_retrieval_suite()

    report = evaluate_memory_retrieval(
        suite=suite,
        session_factory=session_factory,
        search_limit=3,
    )

    direct_project = next(
        result for result in report.results if result.case_id == "direct-project-pytest"
    )

    assert direct_project.memory_loaded_payload.retrieval_mode == "full_text"
    assert direct_project.memory_loaded_payload.search_query == "pytest"
    assert direct_project.memory_loaded_payload.search_limit == 3
    assert "pytest_matrix" in direct_project.memory_loaded_payload.project_keys
    assert report.regression_misses == ()
    assert report.known_semantic_gap_misses == (
        "known-semantic-gap-coverage:project:coverage_gate",
        "known-semantic-gap-style:personal:communication_style",
        "mixed-direct-and-gap:project:coverage_gate",
    )
