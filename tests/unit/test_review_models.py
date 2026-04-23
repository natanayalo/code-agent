"""Unit tests for shared structured review schema models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from workers import ReviewFinding, ReviewResult
from workers.review import SuppressedReviewFinding


def test_review_result_supports_explicit_no_findings_payload() -> None:
    """No-findings review results should be represented explicitly."""
    result = ReviewResult(
        reviewer_kind="worker_self_review",
        summary="No actionable issues were identified.",
        confidence=0.91,
        outcome="no_findings",
        findings=[],
    )
    assert result.outcome == "no_findings"
    assert result.findings == []


def test_review_result_requires_findings_when_outcome_is_findings() -> None:
    """The explicit findings outcome must include at least one finding."""
    with pytest.raises(ValidationError, match="requires at least one finding"):
        ReviewResult(
            reviewer_kind="independent_reviewer",
            summary="Found issues.",
            confidence=0.8,
            outcome="findings",
            findings=[],
        )


def test_review_finding_rejects_invalid_line_ranges() -> None:
    """Line-end must be greater than or equal to line-start."""
    with pytest.raises(ValidationError, match="greater than or equal to line_start"):
        ReviewFinding(
            severity="high",
            category="logic",
            confidence=0.85,
            file_path="workers/codex_cli_worker.py",
            line_start=20,
            line_end=19,
            title="Condition is inverted",
            why_it_matters="The worker can skip a required check.",
        )


def test_review_result_allows_no_findings_when_only_suppressed_findings_exist() -> None:
    """Suppressed findings should not force an exposed findings outcome."""
    finding = ReviewFinding(
        severity="low",
        category="style",
        confidence=0.9,
        file_path="workers/prompt.py",
        line_start=1,
        title="Whitespace cleanup",
        why_it_matters="Consistency matters.",
    )
    result = ReviewResult(
        reviewer_kind="independent_reviewer",
        summary="Suppressed style-only notes.",
        confidence=0.8,
        outcome="no_findings",
        findings=[],
        suppressed_findings=[
            SuppressedReviewFinding(
                finding=finding,
                reasons=["style category suppressed by policy (style)"],
            )
        ],
    )
    assert result.outcome == "no_findings"
    assert len(result.suppressed_findings) == 1
