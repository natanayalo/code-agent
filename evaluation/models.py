"""Shared evaluation data models and serialisation helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class ReviewExpectation:
    """Optional reviewer-quality expectations for one frozen evaluation case."""

    expected_outcome: Literal["no_findings", "findings"] | None = None
    expect_fix_after_review: bool | None = None


@dataclass(frozen=True, slots=True)
class TaskExpectation:
    """Expected output constraints for one frozen evaluation case."""

    require_success: bool = True
    require_tests_passed: bool = False
    required_files_changed: tuple[str, ...] = ()
    required_summary_substrings: tuple[str, ...] = ()
    review: ReviewExpectation | None = None


@dataclass(frozen=True, slots=True)
class FrozenTaskCase:
    """One deterministic task input from the frozen benchmark suite."""

    case_id: str
    repo_fixture: str
    task_text: str
    expectation: TaskExpectation
    task_class: str | None = None


@dataclass(frozen=True, slots=True)
class ReliabilityMetrics:
    """M20.0 per-case reliability signal extracted from orchestrator state.

    All fields default to safe sentinel values so replay mode remains valid.
    Fields marked "state estimate" are derived from available orchestrator state
    and may be 0 / False / empty in replay mode where live interaction history
    is not present.
    """

    human_interaction_count: int = 0
    repeated_question_count: int = 0

    validation_evidence_present: bool = False
    manual_log_inspection_needed: bool = False
    worker_status: str | None = None
    worker_failure_kind: str | None = None
    next_action_hint: str | None = None
    friction_report_count: int = 0
    files_changed_count: int = 0
    commands_run_count: int = 0
    test_results_count: int = 0

    approval_required: bool = False
    approval_status: str | None = None

    stage_latency_seconds: tuple[tuple[str, float], ...] = ()
    stage_latency_available: bool = False

    attempt_count: int = 0

    def stage_latency_dict(self) -> dict[str, float]:
        """Return stage latency as a plain dict for serialisation."""
        return dict(self.stage_latency_seconds)

    def to_dict(self) -> dict[str, Any]:
        return {
            "human_interaction_count": self.human_interaction_count,
            "repeated_question_count": self.repeated_question_count,
            "validation_evidence_present": self.validation_evidence_present,
            "manual_log_inspection_needed": self.manual_log_inspection_needed,
            "worker_status": self.worker_status,
            "worker_failure_kind": self.worker_failure_kind,
            "next_action_hint": self.next_action_hint,
            "friction_report_count": self.friction_report_count,
            "files_changed_count": self.files_changed_count,
            "commands_run_count": self.commands_run_count,
            "test_results_count": self.test_results_count,
            "approval_required": self.approval_required,
            "approval_status": self.approval_status,
            "stage_latency_seconds": self.stage_latency_dict(),
            "stage_latency_available": self.stage_latency_available,
            "attempt_count": self.attempt_count,
        }


@dataclass(frozen=True, slots=True)
class ReliabilityReport:
    """M20.0 aggregate reliability signal across all cases in a suite run."""

    total_cases: int
    cases_needing_approval: int
    cases_with_validation_evidence: int
    cases_needing_manual_log_inspection: int
    cases_with_worker_failure: int
    worker_failure_kind_counts: tuple[tuple[str, int], ...]
    mean_commands_run: float | None
    mean_files_changed: float | None
    mean_friction_reports: float | None
    stage_latency_available: bool
    mean_stage_latency_seconds: tuple[tuple[str, float], ...]

    def worker_failure_kind_counts_dict(self) -> dict[str, int]:
        return dict(self.worker_failure_kind_counts)

    def mean_stage_latency_dict(self) -> dict[str, float]:
        return dict(self.mean_stage_latency_seconds)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_cases": self.total_cases,
            "cases_needing_approval": self.cases_needing_approval,
            "cases_with_validation_evidence": self.cases_with_validation_evidence,
            "cases_needing_manual_log_inspection": self.cases_needing_manual_log_inspection,
            "cases_with_worker_failure": self.cases_with_worker_failure,
            "worker_failure_kind_counts": self.worker_failure_kind_counts_dict(),
            "mean_commands_run": self.mean_commands_run,
            "mean_files_changed": self.mean_files_changed,
            "mean_friction_reports": self.mean_friction_reports,
            "stage_latency_available": self.stage_latency_available,
            "mean_stage_latency_seconds": self.mean_stage_latency_dict(),
        }


@dataclass(frozen=True, slots=True)
class ReviewOutcome:
    """Optional normalized reviewer-quality outcome data for one case."""

    findings_count: int = 0
    actionable_findings_count: int = 0
    false_positive_findings_count: int = 0
    fix_after_review_attempted: bool | None = None
    fix_after_review_succeeded: bool | None = None


@dataclass(frozen=True, slots=True)
class WorkerOutcome:
    """Normalized execution output used by the local evaluation harness."""

    status: Literal["success", "failure", "error"]
    summary: str
    files_changed: tuple[str, ...] = ()
    tests_passed: bool | None = None
    review: ReviewOutcome | None = None
    reliability: ReliabilityMetrics | None = None


@dataclass(frozen=True, slots=True)
class ReviewMetrics:
    """Aggregate reviewer quality metrics for one report.

    Note: `false_discovery_rate` and the compatibility alias `false_positive_rate`
    both use the denominator `total_findings` (reported findings count), not `TN`.
    """

    reviewed_cases: int
    precision: float | None
    actionable_rate: float | None
    false_discovery_rate: float | None
    false_positive_rate: float | None
    fix_after_review_success: float | None
    empty_review_correctness: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "reviewed_cases": self.reviewed_cases,
            "precision": self.precision,
            "actionable_rate": self.actionable_rate,
            "false_discovery_rate": self.false_discovery_rate,
            "false_positive_rate": self.false_positive_rate,
            "fix_after_review_success": self.fix_after_review_success,
            "empty_review_correctness": self.empty_review_correctness,
        }


@dataclass(frozen=True, slots=True)
class EvaluationProfile:
    """Optional run metadata to support A/B comparisons across reviewer variants."""

    variant_label: str | None = None
    review_prompt_profile: str | None = None
    reviewer_model_profile: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "variant_label": self.variant_label,
            "review_prompt_profile": self.review_prompt_profile,
            "reviewer_model_profile": self.reviewer_model_profile,
        }


@dataclass(frozen=True, slots=True)
class EvaluationComparison:
    """Structured comparison between two evaluation report variants."""

    baseline_variant_label: str | None
    candidate_variant_label: str | None
    delta_passed_cases: int
    delta_total_score: int
    delta_reviewed_cases: int
    delta_precision: float | None
    delta_actionable_rate: float | None
    delta_false_discovery_rate: float | None
    delta_false_positive_rate: float | None
    delta_fix_after_review_success: float | None
    delta_empty_review_correctness: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline_variant_label": self.baseline_variant_label,
            "candidate_variant_label": self.candidate_variant_label,
            "delta_passed_cases": self.delta_passed_cases,
            "delta_total_score": self.delta_total_score,
            "delta_reviewed_cases": self.delta_reviewed_cases,
            "delta_precision": self.delta_precision,
            "delta_actionable_rate": self.delta_actionable_rate,
            "delta_false_discovery_rate": self.delta_false_discovery_rate,
            "delta_false_positive_rate": self.delta_false_positive_rate,
            "delta_fix_after_review_success": self.delta_fix_after_review_success,
            "delta_empty_review_correctness": self.delta_empty_review_correctness,
        }
