"""Typed shared review schema for worker self-review and independent review flows."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ReviewModel(BaseModel):
    """Base model for review payload boundaries."""

    model_config = ConfigDict(extra="forbid")


ReviewSeverity = Literal["low", "medium", "high", "critical"]
ReviewerKind = Literal["worker_self_review", "independent_reviewer"]


class ReviewFinding(ReviewModel):
    """One actionable review finding correlated to code context."""

    severity: ReviewSeverity
    category: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    file_path: str = Field(min_length=1)
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)
    title: str = Field(min_length=1)
    why_it_matters: str = Field(min_length=1)
    evidence: str | None = None
    suggested_fix: str | None = None

    @model_validator(mode="after")
    def _validate_line_range(self) -> ReviewFinding:
        """Ensure finding line ranges are coherent when provided."""
        if self.line_end is not None and self.line_start is None:
            raise ValueError("line_start is required when line_end is provided.")
        if (
            self.line_start is not None
            and self.line_end is not None
            and self.line_end < self.line_start
        ):
            raise ValueError("line_end must be greater than or equal to line_start.")
        return self


class SuppressedReviewFinding(ReviewModel):
    """A review finding that was intentionally suppressed from surfaced output."""

    finding: ReviewFinding
    reasons: list[str] = Field(min_length=1)


class ReviewResult(ReviewModel):
    """Shared structured review payload used across review stages."""

    reviewer_kind: ReviewerKind
    summary: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    outcome: Literal["no_findings", "findings"]
    findings: list[ReviewFinding] = Field(default_factory=list)
    suppressed_findings: list[SuppressedReviewFinding] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_outcome_consistency(self) -> ReviewResult:
        """Ensure the explicit outcome flag matches finding presence."""
        has_findings = bool(self.findings)
        if self.outcome == "no_findings" and has_findings:
            raise ValueError("no_findings outcome must not include findings.")
        if self.outcome == "findings" and not has_findings:
            raise ValueError("findings outcome requires at least one finding.")
        return self
