"""Strict contracts for M28 real-worker paired evidence."""
# ruff: noqa: E501

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Scenario = Literal[
    "useful_hit", "irrelevant_rejection", "stale_reverification", "conflict_handling"
]
Conclusion = Literal["effective", "incomplete", "invalid", "unsafe", "inconclusive"]

PROFILES = (
    "codex-native-executor-read-only",
    "antigravity-native-executor-read-only",
)
SCENARIOS = ("useful_hit", "irrelevant_rejection", "stale_reverification", "conflict_handling")


class StrictModel(BaseModel):
    """Reject accidental private or unsupported evidence fields."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class RealWorkerPairCase(StrictModel):
    """One provider/scenario cold and assisted comparison."""

    case_id: str = Field(pattern=r"^[a-z0-9-]+$")
    scenario: Scenario
    worker_profile: str

    @model_validator(mode="after")
    def validate_profile(self) -> RealWorkerPairCase:
        if self.worker_profile not in PROFILES:
            raise ValueError("case requires a supported native read-only profile")
        return self


class RealWorkerSuite(StrictModel):
    """The frozen M28 real-worker matrix."""

    suite_name: Literal["m28-real-worker-memory-effectiveness"]
    schema_version: Literal[1]
    cases: list[RealWorkerPairCase]

    @model_validator(mode="after")
    def validate_matrix(self) -> RealWorkerSuite:
        expected = {(scenario, profile) for scenario in SCENARIOS for profile in PROFILES}
        actual = {(case.scenario, case.worker_profile) for case in self.cases}
        if len(self.cases) != len(expected) or actual != expected:
            raise ValueError(
                "suite must contain each scenario once for each native read-only profile"
            )
        if len({case.case_id for case in self.cases}) != len(self.cases):
            raise ValueError("suite case IDs must be unique")
        return self


class BundleIdentity(StrictModel):
    """Deployment and repository revision pinned for a collection."""

    build_sha: str = Field(pattern=r"^[0-9a-f]{7,64}$")
    environment: str = Field(min_length=1)
    repository_revision: str = Field(min_length=1)
    operator: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RealWorkerBundle(StrictModel):
    """Private bundle index; task identifiers remain in per-case captures only."""

    schema_version: Literal[1] = 1
    identity: BundleIdentity
    suite_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    completed_case_ids: list[str] = Field(default_factory=list)


class PairMeasurements(StrictModel):
    """Allowlisted metrics derived from a private task snapshot."""

    terminal_status: str
    memory_keys: list[str] = Field(default_factory=list)
    suppressed_keys: list[str] = Field(default_factory=list)
    accepted_reason_codes: list[str] = Field(default_factory=list)
    command_markers: list[str] = Field(default_factory=list)
    questions: int = Field(ge=0)
    interventions: int = Field(ge=0)
    time_to_terminal_seconds: float | None = Field(default=None, ge=0)
    session_continuity: bool = False


class PrivatePairCapture(StrictModel):
    """Private evidence. IDs are intentionally excluded from public rendering."""

    case_id: str
    scenario: Scenario
    worker_profile: str
    cold_task_id: str
    assisted_task_id: str
    cold: PairMeasurements
    assisted: PairMeasurements
    assisted_pre_run_session_context: dict[str, Any] = Field(default_factory=dict)
    cold_artifact_uris: list[str] = Field(default_factory=list)
    assisted_artifact_uris: list[str] = Field(default_factory=list)
    gate_failures: list[str] = Field(default_factory=list)


class PublicPairResult(StrictModel):
    """Sanitized per-pair evidence used by reports."""

    case_id: str
    scenario: Scenario
    worker_profile: str
    valid: bool
    gate_failures: list[str]
    cold_questions: int
    assisted_questions: int
    cold_interventions: int
    assisted_interventions: int
    cold_time_to_terminal_seconds: float | None
    assisted_time_to_terminal_seconds: float | None


class PublicEffectivenessReport(StrictModel):
    """The only report contract permitted to be committed."""

    schema_version: Literal[1] = 1
    suite_name: str
    build_sha: str
    environment: str
    repository_revision: str
    conclusion: Conclusion
    captured_pairs: int
    required_pairs: Literal[8] = 8
    valid_pairs: int
    pairs: list[PublicPairResult]
