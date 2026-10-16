"""M29 feature/mutation evaluation runner and post-terminal external quality evaluator."""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from evaluation.m29_mutation_isolation import (
    MAX_EVALUATION_RETENTION_SECONDS,
    TerminalSnapshot,
    check_delivery_stop_condition,
    check_pending_interaction_stop_condition,
    check_protected_paths_stop_condition,
    cleanup_evaluation_workspace,
    create_terminal_snapshot,
    validate_post_overlay_task_spec,
    verify_memory_context_isolation,
    verify_terminal_snapshot_immutability,
)
from evaluation.m29_mutation_runner_archive import (
    build_sanitized_case_entry,
    reconcile_m29_publication_gate,
    validate_sanitized_Archive,
    validate_sanitized_archive,
)
from evaluation.m29_mutation_runner_models import (
    M29_MUTATION_IDENTITY_CONTRACT,
    CandidateDispatcher,
    CandidateTaskExecutionResult,
    ExternalEvaluationResult,
    ExternalEvaluator,
    M29MutationRunnerResult,
    RawCaseManifestEntry,
    SanitizedCaseManifestEntry,
    TaskCohortSnapshot,
)
from evaluation.m29_mutation_runner_preflight import (
    capture_task_cohort_snapshot,
    default_mutation_constraints,
    default_mutation_task_spec,
    run_mutation_preflight_checks,
)
from evaluation.m29_mutation_smokes import ExtractorStageSmokeResult
from evaluation.m29_mutation_suite import (
    DEFAULT_M29_NEUTRAL_EVALUATOR,
    M29MutationCase,
    M29MutationSuite,
    NeutralEvaluatorManifest,
)
from evaluation.provider_reliability_models import ExecutionIdentity
from orchestrator.state import MemoryContext
from sandbox.workspace import WorkspaceHandle, WorkspaceManager

LOGGER = logging.getLogger("m29_mutation_runner")


def _check_candidate_stop_conditions(
    result: CandidateTaskExecutionResult,
    case: M29MutationCase,
) -> str | None:
    """Evaluate candidate execution outcome against all M29 stop condition gates."""
    if result.pending_interaction or check_pending_interaction_stop_condition(
        timeline_events=result.timeline_events
    ):
        return "pending_human_interaction_detected"

    if (
        "delivery" in result.terminal_event_type.lower()
        or result.terminal_event_type == "TASK_DELIVERED"
    ):
        return f"delivery_violation:terminal_event_type={result.terminal_event_type}"

    delivery_err = check_delivery_stop_condition(
        result.task_spec or default_mutation_task_spec(case),
        result.timeline_events,
        result.artifacts,
    )
    if delivery_err:
        return delivery_err

    path_errs = check_protected_paths_stop_condition(
        result.changed_files, allowed_source_path=case.allowed_source_path
    )
    if path_errs:
        return f"protected_paths_violation:{';'.join(path_errs)}"

    expected_id = M29_MUTATION_IDENTITY_CONTRACT[case.worker_profile]
    actual_id = result.execution_identity
    if (
        result.execution_identity_status != "verified"
        or actual_id is None
        or actual_id.provider != expected_id.provider
        or actual_id.model != expected_id.model
        or actual_id.reasoning_effort != expected_id.reasoning_effort
    ):
        return f"candidate_identity_mismatch:expected={expected_id},actual={actual_id}"

    if result.in_task_repairs > 0 or result.verification_events or result.review_artifacts:
        return "in_task_verification_review_isolation_violation"

    return None


def _verify_external_evaluator(
    eval_res: ExternalEvaluationResult,
    expected_evaluator: NeutralEvaluatorManifest,
) -> str | None:
    """Verify evaluator identity, fallback policy, and isolation invariants."""
    if not eval_res.evaluator_identity_matches:
        return f"evaluator_identity_mismatch:{eval_res.reason or 'identity_not_matched'}"
    if eval_res.quality_status == "not_assessable":
        return f"evaluator_not_assessable:{eval_res.reason or 'unassessable'}"

    act_id = eval_res.evaluator_identity
    if (
        act_id is None
        or act_id.provider != expected_evaluator.provider
        or act_id.model != expected_evaluator.model
        or act_id.reasoning_effort != expected_evaluator.reasoning_effort
    ):
        return f"evaluator_identity_mismatch:expected={expected_evaluator},actual={act_id}"
    if eval_res.evaluator_identity_status != "verified":
        return f"evaluator_identity_unverified:{eval_res.evaluator_identity_status}"
    if eval_res.fallback_attempted:
        return "evaluator_fallback_attempted"
    if eval_res.repairs_attempted > 0:
        return "evaluator_repair_passes_forbidden"
    if eval_res.emitted_verification_events:
        return "evaluator_emitted_verification_events"
    if eval_res.emitted_review_artifacts:
        return "evaluator_emitted_review_artifacts"
    return None


def _make_raw_entry(
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
        stop_reason=stop_reason,
        case_outcome=case_outcome,
    )


class M29MutationRunner:
    """Runner coordinating preflight, per-case execution, quality evaluation, and cleanup."""

    def __init__(
        self,
        *,
        suite: M29MutationSuite,
        db_url: str,
        workspace_manager: WorkspaceManager,
        baseline_memory_context: MemoryContext,
        candidate_dispatcher: CandidateDispatcher,
        external_evaluator: ExternalEvaluator,
        retention_seconds: int = MAX_EVALUATION_RETENTION_SECONDS,
        smoke_result: ExtractorStageSmokeResult | None = None,
        as_of: datetime | None = None,
        baseline_ids_by_profile: Mapping[str, Collection[str]] | None = None,
        memory_context_provider: Callable[[M29MutationCase], MemoryContext] | None = None,
    ) -> None:
        self.suite = suite
        self.db_url = db_url
        self.workspace_manager = workspace_manager
        self.baseline_memory = baseline_memory_context
        self.candidate_dispatcher = candidate_dispatcher
        self.external_evaluator = external_evaluator
        self.retention_seconds = retention_seconds
        self.smoke_result = smoke_result
        self.as_of = as_of
        self.baseline_ids = baseline_ids_by_profile
        self.memory_context_provider = memory_context_provider
        self.workspace_root: Path = getattr(
            workspace_manager, "root_dir", getattr(workspace_manager, "workspace_root", Path("."))
        ).resolve()
        self._recorded_ws_ids: set[str] = set()
        self._recorded_ws_paths: set[str] = set()

    def _execute_case(
        self,
        case: M29MutationCase,
        current_mem: MemoryContext,
        base_hash: str,
    ) -> tuple[RawCaseManifestEntry, SanitizedCaseManifestEntry, bool, str | None]:
        mem_errs = verify_memory_context_isolation(
            current_mem, base_hash, require_empty_personal=True
        )
        if mem_errs:
            return self._build_stopped(case, f"memory_isolation_violation:{';'.join(mem_errs)}")

        spec, cons = default_mutation_task_spec(case), default_mutation_constraints()
        spec_errs = validate_post_overlay_task_spec(
            spec,
            cons,
            expected_profile=case.worker_profile,
            actual_profile=case.worker_profile,
            expected_fixture_revision=case.target_repository_revision,
            actual_fixture_revision=case.target_repository_revision,
        )
        if spec_errs:
            return self._build_stopped(case, f"task_spec_validation_failed:{';'.join(spec_errs)}")

        cand_res = self.candidate_dispatcher.dispatch_case(case, current_mem)
        self._recorded_ws_ids.add(cand_res.workspace_handle.workspace_id)
        self._recorded_ws_paths.add(str(cand_res.workspace_handle.workspace_path.resolve()))

        stop_cond = _check_candidate_stop_conditions(cand_res, case)
        if stop_cond:
            return self._build_post_dispatch_stopped(case, cand_res, stop_cond)

        snap_before = create_terminal_snapshot(
            cand_res.task_status,
            cand_res.terminal_event_type,
            cand_res.terminal_timestamp,
            failure_kind=cand_res.failure_kind,
            diff_sha256=cand_res.diff_sha256,
            changed_files=cand_res.changed_files,
        )
        eval_res = self.external_evaluator.evaluate_case(
            case, cand_res.workspace_handle.workspace_path, snap_before
        )
        eval_err = _verify_external_evaluator(
            eval_res, case.neutral_evaluator or DEFAULT_M29_NEUTRAL_EVALUATOR
        )
        if eval_err:
            eval_res = replace(
                eval_res,
                acceptance_oracle_passed=False,
                regression_suite_passed=False,
                neutral_review_passed=False,
                evaluator_identity_matches=False,
                reason=eval_err,
                quality_status="not_assessable",
            )

        snap_after = create_terminal_snapshot(
            cand_res.task_status,
            cand_res.terminal_event_type,
            cand_res.terminal_timestamp,
            failure_kind=cand_res.failure_kind,
            diff_sha256=cand_res.diff_sha256,
            changed_files=cand_res.changed_files,
        )
        snap_errs = verify_terminal_snapshot_immutability(snap_before, snap_after)
        if snap_errs:
            reason = f"terminal_snapshot_mutated:{';'.join(snap_errs)}"
            return self._build_post_eval_stopped(case, cand_res, snap_before, eval_res, reason)

        if eval_err:
            return self._build_post_eval_stopped(case, cand_res, snap_before, eval_res, eval_err)

        return self._build_completed(case, cand_res, snap_before, eval_res)

    def _build_stopped(
        self, case: M29MutationCase, reason: str
    ) -> tuple[RawCaseManifestEntry, SanitizedCaseManifestEntry, bool, str]:
        raw = _make_raw_entry(case, stop_reason=reason, case_outcome="stopped_preflight")
        san = build_sanitized_case_entry(
            case=case,
            task_status="failed",
            failure_kind="stopped",
            candidate_identity_status="unknown_execution_identity",
            identity_matches=False,
            exclusion_reason=reason,
        )
        return raw, san, False, reason

    def _build_post_dispatch_stopped(
        self, case: M29MutationCase, res: CandidateTaskExecutionResult, reason: str
    ) -> tuple[RawCaseManifestEntry, SanitizedCaseManifestEntry, bool, str]:
        snap = create_terminal_snapshot(
            res.task_status,
            res.terminal_event_type,
            res.terminal_timestamp,
            failure_kind=res.failure_kind,
            diff_sha256=res.diff_sha256,
            changed_files=res.changed_files,
        )
        hnd = res.workspace_handle
        trusted_git = str(hnd.trusted_git_dir) if hnd.trusted_git_dir else None
        raw = _make_raw_entry(
            case,
            task_id=res.task_id,
            workspace_id=hnd.workspace_id,
            workspace_path=str(hnd.workspace_path),
            trusted_git_dir=trusted_git,
            terminal_snapshot=snap,
            candidate_identity=res.execution_identity,
            candidate_identity_status=res.execution_identity_status,
            time_to_terminal_seconds=res.time_to_terminal_seconds,
            failure_kind=res.failure_kind,
            diff_sha256=res.diff_sha256,
            raw_diff=res.diff_text,
            changed_files=res.changed_files,
            stop_reason=reason,
            case_outcome="interaction_pending" if "interaction" in reason else "stopped_safety",
        )
        san = build_sanitized_case_entry(
            case=case,
            task_status=res.task_status,
            failure_kind=res.failure_kind,
            time_to_terminal_seconds=res.time_to_terminal_seconds,
            candidate_identity_status=res.execution_identity_status,
            identity_matches=False,
            exclusion_reason=reason,
        )
        return raw, san, False, reason

    def _build_post_eval_stopped(
        self,
        case: M29MutationCase,
        res: CandidateTaskExecutionResult,
        snap: TerminalSnapshot,
        eval_res: ExternalEvaluationResult,
        reason: str,
    ) -> tuple[RawCaseManifestEntry, SanitizedCaseManifestEntry, bool, str]:
        hnd = res.workspace_handle
        trusted_git = str(hnd.trusted_git_dir) if hnd.trusted_git_dir else None
        raw = _make_raw_entry(
            case,
            task_id=res.task_id,
            workspace_id=hnd.workspace_id,
            workspace_path=str(hnd.workspace_path),
            trusted_git_dir=trusted_git,
            terminal_snapshot=snap,
            candidate_identity=res.execution_identity,
            candidate_identity_status=res.execution_identity_status,
            time_to_terminal_seconds=res.time_to_terminal_seconds,
            failure_kind=res.failure_kind,
            diff_sha256=res.diff_sha256,
            raw_diff=res.diff_text,
            changed_files=res.changed_files,
            external_evaluation=eval_res,
            stop_reason=reason,
            case_outcome=(
                "stopped_mutation"
                if reason.startswith("terminal_snapshot_mutated")
                else "quality_not_assessable"
            ),
        )
        san = build_sanitized_case_entry(
            case=case,
            task_status=res.task_status,
            failure_kind=res.failure_kind,
            time_to_terminal_seconds=res.time_to_terminal_seconds,
            candidate_identity_status=res.execution_identity_status,
            identity_matches=True,
            exclusion_reason=None,
            eval_result=eval_res,
        )
        return raw, san, False, reason

    def _build_completed(
        self,
        case: M29MutationCase,
        res: CandidateTaskExecutionResult,
        snap: TerminalSnapshot,
        eval_res: ExternalEvaluationResult,
    ) -> tuple[RawCaseManifestEntry, SanitizedCaseManifestEntry, bool, None]:
        q_pass = eval_res.quality_status == "pass"
        term_comp = res.task_status == "completed"
        outcome = (
            "completed_quality_pass"
            if term_comp and q_pass
            else "completed_quality_fail"
            if term_comp
            else "failed_terminal"
        )
        hnd = res.workspace_handle
        trusted_git = str(hnd.trusted_git_dir) if hnd.trusted_git_dir else None
        raw = _make_raw_entry(
            case,
            task_id=res.task_id,
            workspace_id=hnd.workspace_id,
            workspace_path=str(hnd.workspace_path),
            trusted_git_dir=trusted_git,
            terminal_snapshot=snap,
            candidate_identity=res.execution_identity,
            candidate_identity_status=res.execution_identity_status,
            time_to_terminal_seconds=res.time_to_terminal_seconds,
            failure_kind=res.failure_kind,
            diff_sha256=res.diff_sha256,
            raw_diff=res.diff_text,
            changed_files=res.changed_files,
            external_evaluation=eval_res,
            stop_reason=None,
            case_outcome=outcome,
        )
        san = build_sanitized_case_entry(
            case=case,
            task_status=res.task_status,
            failure_kind=res.failure_kind,
            time_to_terminal_seconds=res.time_to_terminal_seconds,
            candidate_identity_status=res.execution_identity_status,
            identity_matches=True,
            exclusion_reason=None,
            eval_result=eval_res,
        )
        return raw, san, (outcome == "completed_quality_pass"), None

    def _clean_workspace_if_eligible(self, raw: RawCaseManifestEntry) -> RawCaseManifestEntry:
        hnd = WorkspaceHandle(
            workspace_id=raw.workspace_id,
            task_id=raw.task_id,
            workspace_path=Path(raw.workspace_path),
            repo_path=Path(raw.workspace_path),
            repo_url="",
            cleanup_policy=self.workspace_manager.cleanup_policy,
            trusted_git_dir=Path(raw.trusted_git_dir) if raw.trusted_git_dir else None,
        )
        cleanup_evaluation_workspace(
            self.workspace_manager,
            hnd,
            succeeded=True,
            recorded_workspace_ids=self._recorded_ws_ids,
            recorded_workspace_paths=self._recorded_ws_paths,
        )
        return replace(raw, cleaned_up=True)

    def _run_preflight(
        self,
    ) -> tuple[TaskCohortSnapshot | None, M29MutationRunnerResult | None]:
        try:
            snapshot = run_mutation_preflight_checks(
                db_url=self.db_url,
                workspace_root=self.workspace_root,
                retention_seconds=self.retention_seconds,
                suite=self.suite,
                baseline_memory_context=self.baseline_memory,
                smoke_result=self.smoke_result,
                run_smokes_if_missing=True,
                as_of=self.as_of,
                baseline_ids_by_profile=self.baseline_ids,
            )
            return snapshot, None
        except Exception as exc:
            LOGGER.error("M29 mutation preflight check failed", exc_info=True)
            fallback_snap = capture_task_cohort_snapshot(
                baseline_memory_context=self.baseline_memory,
                as_of=self.as_of or datetime.now(UTC),
                baseline_ids_by_profile=self.baseline_ids,
            )
            failed_res = M29MutationRunnerResult(
                suite_name=self.suite.suite_name,
                status="preflight_failed",
                stop_reason=str(exc),
                snapshot=fallback_snap,
                raw_entries=(),
                sanitized_cases=(),
                workspaces_cleaned=(),
                workspaces_retained=(),
            )
            return None, failed_res

    def run(self) -> M29MutationRunnerResult:
        """Run the full evaluation workflow across all cases in the frozen suite."""
        snapshot, preflight_err = self._run_preflight()
        if preflight_err is not None or snapshot is None:
            return preflight_err

        raw_list: list[RawCaseManifestEntry] = []
        san_list: list[SanitizedCaseManifestEntry] = []
        cleaned_ws: list[str] = []
        retained_ws: list[str] = []
        suite_stop: str | None = None
        halted = False

        for case in self.suite.cases:
            if halted:
                raw, san, _, _ = self._build_stopped(case, suite_stop or "halted")
                raw_list.append(raw)
                san_list.append(san)
                continue

            current_mem = (
                self.memory_context_provider(case)
                if self.memory_context_provider is not None
                else self.baseline_memory
            )
            raw, san, can_clean, stop_reason = self._execute_case(
                case, current_mem, snapshot.baseline_memory_context_hash
            )
            if stop_reason:
                halted = True
                suite_stop = stop_reason
                retained_ws.append(raw.workspace_id)
            elif can_clean:
                raw = self._clean_workspace_if_eligible(raw)
                cleaned_ws.append(raw.workspace_id)
            else:
                retained_ws.append(raw.workspace_id)

            raw_list.append(raw)
            san_list.append(san)

        final_status: Literal["completed", "halted"] = "halted" if halted else "completed"
        archive_chk = (
            validate_sanitized_archive(san_list, expected_case_count=len(self.suite.cases))
            if final_status == "completed"
            else None
        )
        return M29MutationRunnerResult(
            suite_name=self.suite.suite_name,
            status=final_status,
            stop_reason=suite_stop,
            snapshot=snapshot,
            raw_entries=tuple(raw_list),
            sanitized_cases=tuple(san_list),
            workspaces_cleaned=tuple(cleaned_ws),
            workspaces_retained=tuple(retained_ws),
            sanitized_archive_checksum=archive_chk,
        )


__all__ = [
    "CandidateDispatcher",
    "CandidateTaskExecutionResult",
    "ExternalEvaluationResult",
    "ExternalEvaluator",
    "M29MutationRunner",
    "M29MutationRunnerResult",
    "M29_MUTATION_IDENTITY_CONTRACT",
    "RawCaseManifestEntry",
    "SanitizedCaseManifestEntry",
    "TaskCohortSnapshot",
    "build_sanitized_case_entry",
    "capture_task_cohort_snapshot",
    "default_mutation_constraints",
    "default_mutation_task_spec",
    "reconcile_m29_publication_gate",
    "run_mutation_preflight_checks",
    "validate_sanitized_Archive",
    "validate_sanitized_archive",
]
