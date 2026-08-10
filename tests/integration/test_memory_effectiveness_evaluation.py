"""Integration coverage for the M28 paired memory-effectiveness baseline."""

from __future__ import annotations

from evaluation.memory_effectiveness import (
    evaluate_memory_effectiveness,
    load_memory_effectiveness_suite,
)


def test_memory_effectiveness_evaluation_uses_real_load_memory_path(session_factory) -> None:
    report = evaluate_memory_effectiveness(
        suite=load_memory_effectiveness_suite(),
        session_factory=session_factory,
        search_limit=20,
    )

    assert report.status == "passed"
    assert report.total_cases == 4
    stale = next(result for result in report.results if result.case_id == "stale-reverification")
    stale_candidate = stale.assisted.candidates[0]
    assert stale_candidate.retrieved is True
    assert stale_candidate.context_disposition == "suppressed"
    assert stale_candidate.reason_codes == ["high_risk_unverified_or_stale"]
    assert report.retrieval_mode == "sqlite_substring_fallback"
    assert stale.assisted.retrieval_mode == "sqlite_substring_fallback"
    assert stale.assisted.timeline_retrieval_mode == "full_text"
    assert stale.assisted.search_query == "release deployment policy"
    conflict = next(result for result in report.results if result.case_id == "conflict-handling")
    assert conflict.assisted.project_keys == ["m28_test_execution_preference"]
    assert conflict.assisted.personal_keys == []
    assert conflict.session_continuity.actual == conflict.session_continuity.expected.model_dump()
