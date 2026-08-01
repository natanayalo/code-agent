"""Contracts for the M25.6 Temporal reliability evidence bundle."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CaseCategory = Literal[
    "read_only_monolithic",
    "mutation",
    "sequential_dag",
    "read_only_fanout",
    "hitl",
    "recovery",
    "draft_pr",
]
ExpectedMode = Literal["read_only", "mutation"]
ExpectedTerminalStatus = Literal["completed", "failed", "cancelled"]
ProofType = Literal[
    "validation",
    "sequential_dag",
    "fanout_overlap",
    "verifier_repair",
    "independent_review_repair",
    "clarification",
    "approval",
    "permission_escalation",
    "cancellation",
    "worker_restart",
    "draft_pr",
]
CaseId = Annotated[str, Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9-]*$")]
CaptureFilePath = Annotated[
    str,
    Field(pattern=r"^cases/[a-z0-9][a-z0-9-]*\.json$"),
]

CODEX_PROFILES = frozenset({"codex-native-executor", "codex-native-executor-read-only"})
ANTIGRAVITY_PROFILES = frozenset(
    {"antigravity-native-executor", "antigravity-native-executor-read-only"}
)
ALLOWED_PROFILES = CODEX_PROFILES | ANTIGRAVITY_PROFILES
CATEGORY_COUNTS: dict[str, int] = {
    "read_only_monolithic": 4,
    "mutation": 4,
    "sequential_dag": 3,
    "read_only_fanout": 2,
    "hitl": 3,
    "recovery": 2,
    "draft_pr": 2,
}
REQUIRED_PROOFS = frozenset(
    {
        "verifier_repair",
        "independent_review_repair",
        "clarification",
        "approval",
        "permission_escalation",
        "cancellation",
        "worker_restart",
    }
)


class StrictModel(BaseModel):
    """Reject unknown fields in frozen and generated evidence contracts."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ReliabilitySuiteCase(StrictModel):
    """One frozen case slot; task text and repository details stay out of git."""

    case_id: CaseId
    category: CaseCategory
    expected_profile: str
    expected_mode: ExpectedMode
    expected_terminal_status: ExpectedTerminalStatus = "completed"
    required_proofs: list[ProofType] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_profile_and_mode(self) -> ReliabilitySuiteCase:
        if self.expected_profile not in ALLOWED_PROFILES:
            raise ValueError(f"unsupported native profile: {self.expected_profile}")
        read_only_profile = self.expected_profile.endswith("-read-only")
        if read_only_profile != (self.expected_mode == "read_only"):
            raise ValueError("expected profile and execution mode disagree")
        if self.category in {"read_only_monolithic", "read_only_fanout"} and not read_only_profile:
            raise ValueError(f"{self.category} requires a read-only profile")
        if self.category == "read_only_fanout" and "fanout_overlap" not in self.required_proofs:
            raise ValueError("read-only fan-out cases require fanout_overlap proof")
        if self.category == "sequential_dag" and "sequential_dag" not in self.required_proofs:
            raise ValueError("sequential DAG cases require sequential_dag proof")
        if self.category == "draft_pr" and "draft_pr" not in self.required_proofs:
            raise ValueError("draft PR cases require draft_pr proof")
        if (
            self.expected_mode == "mutation"
            and self.expected_terminal_status == "completed"
            and "validation" not in self.required_proofs
        ):
            raise ValueError("completed mutation cases require validation proof")
        return self


class ReliabilitySuite(StrictModel):
    """Checked-in 20-case collection contract."""

    suite_name: Literal["m25.6-temporal-reliability-baseline"]
    schema_version: Literal[1]
    cases: list[ReliabilitySuiteCase]

    @model_validator(mode="after")
    def validate_matrix(self) -> ReliabilitySuite:
        if len(self.cases) != 20:
            raise ValueError("suite must contain exactly 20 cases")
        case_ids = [case.case_id for case in self.cases]
        if len(set(case_ids)) != len(case_ids):
            raise ValueError("suite case IDs must be unique")
        actual_counts = {
            category: sum(case.category == category for case in self.cases)
            for category in CATEGORY_COUNTS
        }
        if actual_counts != CATEGORY_COUNTS:
            raise ValueError(f"category counts must be {CATEGORY_COUNTS}, got {actual_counts}")
        codex = sum(case.expected_profile in CODEX_PROFILES for case in self.cases)
        antigravity = sum(case.expected_profile in ANTIGRAVITY_PROFILES for case in self.cases)
        if (codex, antigravity) != (10, 10):
            raise ValueError(
                f"profile allocation must be 10 Codex / 10 Antigravity, got {codex}/{antigravity}"
            )
        proofs = {proof for case in self.cases for proof in case.required_proofs}
        missing = REQUIRED_PROOFS - proofs
        if missing:
            raise ValueError(f"suite is missing required proof coverage: {sorted(missing)}")
        return self


class BundleIdentity(StrictModel):
    """Deployment identity that every capture must match."""

    build_sha: str = Field(pattern=r"^[0-9a-f]{7,64}$")
    environment: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    operator: str = Field(min_length=1)
    temporal_address: str = Field(min_length=1)
    temporal_namespace: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    database_url_env: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    initialized_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class OperatorAnnotations(StrictModel):
    """Private operator observations that cannot be derived from persisted state."""

    manual_log_inspection: bool
    ci_rejection_count: int = Field(ge=0)
    review_rejection_count: int = Field(ge=0)
    next_action: str | None = Field(default=None, min_length=1)
    notes: str | None = None


class TemporalActivityEvidence(StrictModel):
    """Sanitizable Activity timing and retry evidence from one history."""

    activity_type: str
    scheduled_event_id: int
    attempt: int = Field(ge=1)
    status: Literal["completed", "failed", "timed_out", "cancelled", "started"]
    latency_seconds: float | None = Field(default=None, ge=0)


class TemporalHistoryEvidence(StrictModel):
    """Derived history facts plus an integrity digest for the raw private history."""

    workflow_id: str
    run_id: str | None
    workflow_status: str
    event_count: int = Field(ge=0)
    history_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    activities: list[TemporalActivityEvidence]
    activity_counts: dict[str, int]
    retry_activity_types: list[str]
    signal_names: list[str]
    fanout_overlap: bool
    raw_history_file: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*\.json$")


class CapturedCaseEvidence(StrictModel):
    """Immutable private evidence captured for one suite slot."""

    case_id: CaseId
    task_id: str = Field(min_length=1)
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expected: ReliabilitySuiteCase
    database: dict[str, Any]
    temporal: TemporalHistoryEvidence
    annotations: OperatorAnnotations
    gate_failures: list[str]


class EvidenceBundleManifest(StrictModel):
    """Incremental bundle index; captures themselves remain immutable."""

    schema_version: Literal[1] = 1
    identity: BundleIdentity
    suite_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    capture_files: dict[CaseId, CaptureFilePath] = Field(default_factory=dict)
    task_ids: list[Annotated[str, Field(min_length=1)]] = Field(default_factory=list)


class SanitizedCaseResult(StrictModel):
    """Explicit public allowlist for one case; no user or repository content."""

    case_id: CaseId
    category: CaseCategory
    expected_profile: str
    expected_terminal_status: ExpectedTerminalStatus
    observed_terminal_status: str
    valid: bool
    gate_failures: list[str]
    human_interventions: int
    repeated_clarification_questions: int
    manual_log_inspection: bool
    validation_evidence_present: bool
    provider_failure_kind: str | None
    activity_stage_latency_seconds: dict[str, float]
    time_to_terminal_seconds: float | None
    time_to_pr_seconds: float | None
    ci_rejection_count: int
    review_rejection_count: int


class SanitizedReliabilityMetrics(StrictModel):
    """Explicit metric allowlist for public JSON and Markdown."""

    human_interventions: int
    repeated_clarification_questions: int
    manual_log_inspection_cases: int
    validation_evidence_rate: float | None
    provider_failures: dict[str, int]
    profile_success_rates: dict[str, float]
    activity_stage_latency_seconds: dict[str, float]
    mean_time_to_terminal_seconds: float | None
    mean_time_to_pr_seconds: float | None
    ci_rejection_count: int
    review_rejection_count: int


class SanitizedReliabilityReport(StrictModel):
    """Allowlisted aggregate report safe to render as Markdown."""

    schema_version: Literal[1] = 1
    suite_name: str
    build_sha: str
    environment: str
    status: Literal["incomplete", "invalid", "ready_for_operator_review"]
    captured_cases: int
    required_cases: Literal[20] = 20
    valid_cases: int
    metrics: SanitizedReliabilityMetrics
    cases: list[SanitizedCaseResult]
