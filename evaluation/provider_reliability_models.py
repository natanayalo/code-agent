"""Versioned contracts for M29 Provider Reliability Advisory Reports."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ReportStatus = Literal["complete", "partial", "insufficient_data"]
MutationMode = Literal["read_only", "mutation"]

DEFAULT_ENABLED_PROFILES: tuple[str, ...] = (
    "codex-native-executor",
    "codex-native-executor-read-only",
    "antigravity-native-executor",
    "antigravity-native-executor-read-only",
)


class StrictModel(BaseModel):
    """Reject unknown fields in provider reliability contracts."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class WilsonConfidenceInterval(StrictModel):
    """Two-sided Wilson score confidence interval."""

    lower: float = Field(ge=0.0, le=1.0)
    center: float = Field(ge=0.0, le=1.0)
    upper: float = Field(ge=0.0, le=1.0)


class StageOutcomeRates(StrictModel):
    """Stage-specific outcome rates; optional stages are None when not applicable."""

    dispatch_rate: float = Field(ge=0.0, le=1.0)
    execution_success_rate: float = Field(ge=0.0, le=1.0)
    verification_pass_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    review_pass_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    delivery_pass_rate: float | None = Field(default=None, ge=0.0, le=1.0)


class RepairMetrics(StrictModel):
    """Repair frequency across verifier and independent review loops."""

    verifier_repairs_count: int = Field(ge=0)
    review_repairs_count: int = Field(ge=0)
    total_repaired_tasks: int = Field(ge=0)
    repair_rate: float = Field(ge=0.0, le=1.0)


class InterventionMetrics(StrictModel):
    """Human checkpoints and questions encountered."""

    human_interventions_count: int = Field(ge=0)
    clarification_questions_count: int = Field(ge=0)
    approvals_count: int = Field(ge=0)
    intervention_rate: float = Field(ge=0.0, le=1.0)


class LatencyMetrics(StrictModel):
    """Task terminal latency distribution in seconds."""

    median_seconds: float | None = Field(default=None, ge=0.0)
    mean_seconds: float | None = Field(default=None, ge=0.0)
    min_seconds: float | None = Field(default=None, ge=0.0)
    max_seconds: float | None = Field(default=None, ge=0.0)
    p90_seconds: float | None = Field(default=None, ge=0.0)


class BudgetCoverageMetrics(StrictModel):
    """Reporting completeness of budget usage metrics."""

    budget_reported_count: int = Field(ge=0)
    budget_coverage_rate: float = Field(ge=0.0, le=1.0)


class ProviderReliabilityEvidenceCell(StrictModel):
    """Aggregated empirical evidence for one task-class, profile, and mode cell."""

    task_class: str = Field(min_length=1)
    profile: str = Field(min_length=1)
    mutation_mode: MutationMode
    sample_size: int = Field(ge=0)
    oldest_evidence_timestamp: datetime | None = None
    newest_evidence_timestamp: datetime | None = None
    accepted_count: int = Field(ge=0)
    accepted_task_rate: float = Field(ge=0.0, le=1.0)
    accepted_task_rate_ci: WilsonConfidenceInterval
    failure_count: int = Field(ge=0)
    failure_rate: float = Field(ge=0.0, le=1.0)
    stage_outcome_rates: StageOutcomeRates
    typed_failures: dict[str, int] = Field(default_factory=dict)
    repairs: RepairMetrics
    interventions: InterventionMetrics
    manual_overrides_count: int = Field(ge=0)
    manual_override_rate: float = Field(ge=0.0, le=1.0)
    terminal_latency: LatencyMetrics
    budget_field_coverage: BudgetCoverageMetrics
    is_eligible: bool = False
    insufficiency_reasons: list[str] = Field(default_factory=list)


class CandidateRanking(StrictModel):
    """Ranking metadata for one candidate profile in a recommendation decision."""

    profile: str = Field(min_length=1)
    is_eligible: bool
    accepted_rate_wilson_lower: float = Field(ge=0.0, le=1.0)
    median_latency_seconds: float | None = Field(default=None, ge=0.0)
    rank: int | None = Field(default=None, ge=1)
    insufficiency_reasons: list[str] = Field(default_factory=list)
    sample_size: int = Field(default=0, ge=0)
    accepted_count: int = Field(default=0, ge=0)
    oldest_evidence_timestamp: datetime | None = None
    newest_evidence_timestamp: datetime | None = None
    evidence_age_days: float | None = Field(default=None, ge=0.0)


class TaskClassRecommendation(StrictModel):
    """Recommendation decision for one task class and mutation mode."""

    task_class: str = Field(min_length=1)
    mutation_mode: MutationMode
    recommended_profile: str | None = None
    rankings: list[CandidateRanking] = Field(default_factory=list)
    fallback_reason: str | None = None


class TaskExclusionSummary(StrictModel):
    """Aggregate accounting of excluded records and reasons."""

    total_tasks_scanned: int = Field(ge=0)
    included_tasks_count: int = Field(ge=0)
    excluded_tasks_count: int = Field(ge=0)
    by_reason: dict[str, int] = Field(default_factory=dict)


class ReliabilityReportPolicy(StrictModel):
    """Sampling and statistical policy used to generate the report."""

    schema_version: Literal[1] = 1
    lookback_days: int = Field(default=90, ge=1)
    min_samples: int = Field(default=10, ge=1)
    confidence_level: float = Field(default=0.95, gt=0.0, lt=1.0)
    as_of: datetime
    window_start_at: datetime
    window_end_at: datetime
    enabled_profiles: list[str] = Field(default_factory=lambda: list(DEFAULT_ENABLED_PROFILES))


class ProviderReliabilityReport(StrictModel):
    """Versioned aggregate provider reliability advisory report (schema v1)."""

    schema_version: Literal[1] = 1
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    status: ReportStatus
    policy: ReliabilityReportPolicy
    exclusions: TaskExclusionSummary
    evidence_cells: list[ProviderReliabilityEvidenceCell]
    recommendations: list[TaskClassRecommendation]
