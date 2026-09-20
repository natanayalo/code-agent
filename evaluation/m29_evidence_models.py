"""Versioned contracts for M29 live provider evidence collection."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

TaskClass = Literal["investigation", "feature", "docs"]
SupportedProfile = Literal[
    "antigravity-native-executor-read-only",
    "codex-native-executor-read-only",
]

TOTAL_CASES = 28
INVESTIGATION_CASES = 2
FEATURE_CASES = 10
DOCS_PAIRED_TOPICS = 8
DOCS_CASES = DOCS_PAIRED_TOPICS * 2
WAVE2_CASES = 20
WAVE2_PAIRED_TOPICS = 10
WAVE3_CASES = 40
WAVE3_PAIRED_TOPICS = 10
WAVE4_CASES = 20
WAVE4_PAIRED_TOPICS = 10


class StrictModel(BaseModel):
    """Reject undeclared or private fields in evaluation models."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class M29EvidenceCase(StrictModel):
    """Single frozen evidence case specification."""

    case_id: str = Field(pattern=r"^[a-z0-9-]+$")
    task_class: TaskClass
    mutation_mode: Literal["read_only"] = "read_only"
    worker_profile: SupportedProfile
    prompt: str = Field(min_length=10)
    topic: str | None = None
    pair_group: str | None = None
    pair_order: int | None = Field(default=None, ge=1, le=2)


def _validate_paired_groups(
    cases: list[M29EvidenceCase], expected_pairs: int, task_class: str
) -> None:
    """Validate paired cases contain matching prompts and alternating providers."""
    paired_groups: dict[str, list[M29EvidenceCase]] = {}
    for c in cases:
        if not c.pair_group:
            raise ValueError(f"{task_class} case {c.case_id} is missing pair_group")
        paired_groups.setdefault(c.pair_group, []).append(c)

    if len(paired_groups) != expected_pairs:
        raise ValueError(
            f"expected {expected_pairs} {task_class} pair groups, got {len(paired_groups)}"
        )

    last_first_provider: str | None = None
    for group_name, group_cases in paired_groups.items():
        if len(group_cases) != 2:
            raise ValueError(
                f"{task_class} pair group '{group_name}' must contain exactly 2 cases, "
                f"got {len(group_cases)}"
            )
        pair_orders = {gc.pair_order for gc in group_cases}
        if pair_orders != {1, 2}:
            raise ValueError(f"pair group '{group_name}' must use pair_order values 1 and 2")
        if (
            len({gc.topic for gc in group_cases}) != 1
            or len({gc.prompt for gc in group_cases}) != 1
        ):
            raise ValueError(f"pair group '{group_name}' must match topic and prompt")
        profiles = {gc.worker_profile for gc in group_cases}
        if profiles != {
            "antigravity-native-executor-read-only",
            "codex-native-executor-read-only",
        }:
            raise ValueError(
                f"{task_class} pair group '{group_name}' must have one antigravity "
                "and one codex case"
            )
        sorted_cases = sorted(group_cases, key=lambda c: c.pair_order or 0)
        first_provider = sorted_cases[0].worker_profile
        if last_first_provider is not None and first_provider == last_first_provider:
            raise ValueError(
                f"pair group '{group_name}' does not alternate provider order "
                "to counterbalance ordering bias"
            )
        last_first_provider = first_provider


def _validate_wave4_cases(cases: list[M29EvidenceCase]) -> None:
    """Validate the balanced investigation-only Wave 4 distribution."""
    if len(cases) != WAVE4_CASES:
        raise ValueError(f"Wave 4 suite must contain exactly {WAVE4_CASES} cases, got {len(cases)}")
    if any(case.task_class != "investigation" for case in cases):
        raise ValueError("Wave 4 suite must contain only investigation cases")
    _validate_paired_groups(cases, WAVE4_PAIRED_TOPICS, "investigation")


class M29EvidenceSuite(StrictModel):
    """Frozen M29 live provider evidence suites for read-only waves."""

    suite_name: Literal[
        "m29-live-provider-evidence",
        "m29-live-provider-evidence-wave2",
        "m29-live-provider-evidence-wave3",
        "m29-live-provider-evidence-wave4",
    ]
    schema_version: Literal[1] = 1
    cases: list[M29EvidenceCase]

    @model_validator(mode="after")
    def validate_suite_structure(self) -> M29EvidenceSuite:
        case_ids = [c.case_id for c in self.cases]
        if len(set(case_ids)) != len(case_ids):
            raise ValueError("duplicate case IDs found in suite")

        if self.suite_name == "m29-live-provider-evidence":
            if len(self.cases) != TOTAL_CASES:
                raise ValueError(
                    f"suite must contain exactly {TOTAL_CASES} cases, got {len(self.cases)}"
                )

            inv_cases = [c for c in self.cases if c.task_class == "investigation"]
            feat_cases = [c for c in self.cases if c.task_class == "feature"]
            docs_cases = [c for c in self.cases if c.task_class == "docs"]

            if len(inv_cases) != INVESTIGATION_CASES:
                raise ValueError(
                    f"expected {INVESTIGATION_CASES} investigation cases, got {len(inv_cases)}"
                )
            if any(c.worker_profile != "antigravity-native-executor-read-only" for c in inv_cases):
                raise ValueError(
                    "investigation cases must use antigravity-native-executor-read-only"
                )

            if len(feat_cases) != FEATURE_CASES:
                raise ValueError(f"expected {FEATURE_CASES} feature cases, got {len(feat_cases)}")
            if any(c.worker_profile != "antigravity-native-executor-read-only" for c in feat_cases):
                raise ValueError("feature cases must use antigravity-native-executor-read-only")

            if len(docs_cases) != DOCS_CASES:
                raise ValueError(f"expected {DOCS_CASES} docs cases, got {len(docs_cases)}")

            _validate_paired_groups(docs_cases, DOCS_PAIRED_TOPICS, "docs")

        elif self.suite_name == "m29-live-provider-evidence-wave2":
            if len(self.cases) != WAVE2_CASES:
                raise ValueError(
                    f"Wave 2 suite must contain exactly {WAVE2_CASES} cases, got {len(self.cases)}"
                )
            docs_cases = [c for c in self.cases if c.task_class == "docs"]
            if len(docs_cases) != WAVE2_CASES:
                raise ValueError(
                    f"Wave 2 suite must contain only docs cases, got {len(docs_cases)}"
                )
            _validate_paired_groups(docs_cases, WAVE2_PAIRED_TOPICS, "docs")

        elif self.suite_name == "m29-live-provider-evidence-wave3":
            if len(self.cases) != WAVE3_CASES:
                raise ValueError(
                    f"Wave 3 suite must contain exactly {WAVE3_CASES} cases, got {len(self.cases)}"
                )
            investigation_cases = [c for c in self.cases if c.task_class == "investigation"]
            feature_cases = [c for c in self.cases if c.task_class == "feature"]
            if len(investigation_cases) != WAVE3_PAIRED_TOPICS * 2:
                raise ValueError(
                    "Wave 3 suite must contain exactly 20 investigation cases, "
                    f"got {len(investigation_cases)}"
                )
            if len(feature_cases) != WAVE3_PAIRED_TOPICS * 2:
                raise ValueError(
                    "Wave 3 suite must contain exactly 20 feature cases, "
                    f"got {len(feature_cases)}"
                )
            _validate_paired_groups(investigation_cases, WAVE3_PAIRED_TOPICS, "investigation")
            _validate_paired_groups(feature_cases, WAVE3_PAIRED_TOPICS, "feature")

        elif self.suite_name == "m29-live-provider-evidence-wave4":
            _validate_wave4_cases(self.cases)

        return self


class M29BundleIdentity(StrictModel):
    """Committed harness build SHA and pinned target repository revision."""

    build_sha: str = Field(pattern=r"^[0-9a-f]{7,64}$")
    target_repository_revision: str = Field(pattern=r"^[0-9a-f]{7,64}$")
    environment: str = Field(min_length=1)
    operator: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class M29CaseOutcome(StrictModel):
    """Outcome for a single executed case persisted to private bundle."""

    case_id: str
    task_id: str
    task_class: TaskClass
    worker_profile: SupportedProfile
    terminal_status: Literal["completed", "failed"]
    failure_kind: str | None = None
    files_changed_count: int = 0
    time_to_terminal_seconds: float | None = None
    created_at: str
    terminal_at: str
    orchestration_runtime: str = "temporal"
    runtime_mode: str = "native_agent"
    has_unresolved_interactions: bool = False
    gate_failures: list[str] = Field(default_factory=list)


class M29EvidenceBundle(StrictModel):
    """Private bundle tracking 28-task execution progress and outcomes."""

    schema_version: Literal[1] = 1
    identity: M29BundleIdentity
    suite_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    in_flight: dict[str, str] = Field(default_factory=dict)
    cases: dict[str, M29CaseOutcome] = Field(default_factory=dict)
