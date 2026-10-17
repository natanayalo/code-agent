"""Data models and interfaces for the M29 feature/mutation evaluation runner."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from evaluation.m29_mutation_isolation import TerminalSnapshot
from evaluation.m29_mutation_manifest import M29MutationManifestCase
from evaluation.m29_mutation_smokes import ExtractorStageSmokeResult
from evaluation.m29_mutation_suite import (
    M29_FROZEN_MUTATION_CANDIDATE_IDENTITIES,
    M29MutationCase,
)
from evaluation.provider_reliability_models import ExecutionIdentity
from orchestrator.state import MemoryContext, TaskSpec
from sandbox.workspace import WorkspaceHandle

M29_MUTATION_IDENTITY_CONTRACT: dict[str, ExecutionIdentity] = (
    M29_FROZEN_MUTATION_CANDIDATE_IDENTITIES
)


@dataclass(frozen=True)
class TaskCohortSnapshot:
    """Frozen baseline task-ID and memory snapshot captured before live case 01."""

    as_of: datetime
    window_start_at: datetime
    window_end_at: datetime
    baseline_memory_context_hash: str
    baseline_ids_by_profile: dict[str, tuple[str, ...]]
    frozen_baseline_ids_sha256_by_profile: dict[str, str]
    smoke_result: ExtractorStageSmokeResult | None = None
    verifier_source_sha256: str | None = None


@dataclass(frozen=True)
class CandidateTaskExecutionResult:
    """Deterministic or live outcome of a candidate task run in its isolated workspace."""

    task_id: str
    workspace_handle: WorkspaceHandle
    task_status: str
    terminal_event_type: str
    terminal_timestamp: datetime
    failure_kind: str | None = None
    diff_sha256: str | None = None
    diff_text: str = ""
    changed_files: tuple[str, ...] = ()
    execution_identity: ExecutionIdentity | None = None
    execution_identity_status: str = "verified"
    time_to_terminal_seconds: float = 0.0
    pending_interaction: bool = False
    in_task_repairs: int = 0
    verification_events: tuple[str, ...] = ()
    review_artifacts: tuple[str, ...] = ()
    timeline_events: tuple[Any, ...] = ()
    artifacts: tuple[dict[str, Any], ...] = ()
    task_spec: TaskSpec | dict[str, Any] | None = None
    constraints: dict[str, Any] | None = None


class CandidateDispatcher(Protocol):
    """Pluggable boundary for dispatching candidate feature/mutation cases."""

    def dispatch_case(
        self, case: M29MutationCase, memory_context: MemoryContext
    ) -> CandidateTaskExecutionResult: ...


@dataclass(frozen=True)
class ExternalEvaluationResult:
    """Read-only external quality evaluation from neutral third-party checks."""

    acceptance_oracle_passed: bool
    regression_suite_passed: bool
    neutral_review_passed: bool
    evaluator_identity: ExecutionIdentity | None = None
    evaluator_identity_status: str = "verified"
    evaluator_identity_matches: bool = True
    fallback_attempted: bool = False
    repairs_attempted: int = 0
    emitted_verification_events: bool = False
    emitted_review_artifacts: bool = False
    reason: str | None = None
    quality_status: str = "pass"
    commands_run: tuple[str, ...] = ()
    exit_codes: tuple[int, ...] = ()


class ExternalEvaluator(Protocol):
    """Pluggable boundary for read-only external quality evaluations."""

    def evaluate_case(
        self,
        case: M29MutationCase,
        workspace_path: Path,
        terminal_snapshot: TerminalSnapshot,
    ) -> ExternalEvaluationResult: ...


@dataclass(frozen=True)
class RawCaseManifestEntry:
    """Private, detailed outcome entry for one feature/mutation evaluation case."""

    case_id: str
    task_id: str
    workspace_id: str
    workspace_path: str
    trusted_git_dir: str | None
    pair_group: str
    pair_order: int
    topic: str
    worker_profile: str
    provider: str
    target_repository_revision: str
    terminal_snapshot: TerminalSnapshot | None
    candidate_identity: ExecutionIdentity | None
    candidate_identity_status: str
    time_to_terminal_seconds: float | None
    failure_kind: str | None
    diff_sha256: str | None
    raw_diff: str
    changed_files: tuple[str, ...]
    external_evaluation: ExternalEvaluationResult | None
    cleaned_up: bool
    cleanup_error: str | None = None
    stop_reason: str | None = None
    case_outcome: str = "completed_quality_pass"


SanitizedCaseManifestEntry = M29MutationManifestCase


@dataclass(frozen=True)
class M29MutationRunnerResult:
    """Summary outcome of an M29 feature/mutation evaluation suite run."""

    suite_name: str
    status: Literal["completed", "halted", "preflight_failed"]
    stop_reason: str | None
    snapshot: TaskCohortSnapshot
    raw_entries: tuple[RawCaseManifestEntry, ...]
    sanitized_cases: tuple[SanitizedCaseManifestEntry, ...]
    workspaces_cleaned: tuple[str, ...]
    workspaces_retained: tuple[str, ...]
    sanitized_archive_checksum: str | None = None


def make_raw_case_manifest_entry(
    case: M29MutationCase,
    *,
    task_id: str = "unstarted",
    workspace_id: str = "none",
    workspace_path: str = "",
    trusted_git_dir: str | None = None,
    terminal_snapshot: TerminalSnapshot | None = None,
    candidate_identity: ExecutionIdentity | None = None,
    candidate_identity_status: str = "unknown_execution_identity",
    time_to_terminal_seconds: float | None = None,
    failure_kind: str | None = None,
    diff_sha256: str | None = None,
    raw_diff: str = "",
    changed_files: tuple[str, ...] = (),
    external_evaluation: ExternalEvaluationResult | None = None,
    cleaned_up: bool = False,
    cleanup_error: str | None = None,
    stop_reason: str | None = None,
    case_outcome: str = "stopped_preflight",
) -> RawCaseManifestEntry:
    prov = "codex" if case.worker_profile == "codex-native-executor" else "antigravity"
    return RawCaseManifestEntry(
        case_id=case.case_id,
        task_id=task_id,
        workspace_id=workspace_id,
        workspace_path=workspace_path,
        trusted_git_dir=trusted_git_dir,
        pair_group=case.pair_group,
        pair_order=case.pair_order,
        topic=case.topic,
        worker_profile=case.worker_profile,
        provider=prov,
        target_repository_revision=case.target_repository_revision,
        terminal_snapshot=terminal_snapshot,
        candidate_identity=candidate_identity,
        candidate_identity_status=candidate_identity_status,
        time_to_terminal_seconds=time_to_terminal_seconds,
        failure_kind=failure_kind,
        diff_sha256=diff_sha256,
        raw_diff=raw_diff,
        changed_files=changed_files,
        external_evaluation=external_evaluation,
        cleaned_up=cleaned_up,
        cleanup_error=cleanup_error,
        stop_reason=stop_reason,
        case_outcome=case_outcome,
    )


__all__ = [
    "CandidateDispatcher",
    "CandidateTaskExecutionResult",
    "ExternalEvaluationResult",
    "ExternalEvaluator",
    "M29MutationRunnerResult",
    "M29_MUTATION_IDENTITY_CONTRACT",
    "RawCaseManifestEntry",
    "SanitizedCaseManifestEntry",
    "TaskCohortSnapshot",
    "make_raw_case_manifest_entry",
]
