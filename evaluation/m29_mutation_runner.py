"""M29 feature/mutation evaluation runner and post-terminal external quality evaluator."""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from evaluation.m29_mutation_dispatcher import (
    DisposableDbMemoryIsolationManager,
    HttpStackCandidateDispatcher,
)
from evaluation.m29_mutation_fixture import (
    PINNED_TASKBOARD_FIXTURE_REVISION,
    TaskboardFixtureInfo,
    clone_case_workspace_from_fixture,
    create_taskboard_fixture_repo,
    verify_taskboard_fixture_repo,
)
from evaluation.m29_mutation_isolation import (
    MAX_EVALUATION_RETENTION_SECONDS,
    TerminalSnapshot,
    cleanup_evaluation_workspace,
    create_terminal_snapshot,
    validate_post_overlay_task_spec,
    verify_memory_context_isolation,
    verify_terminal_snapshot_immutability,
)
from evaluation.m29_mutation_oracles import compute_verifier_source_sha256
from evaluation.m29_mutation_reviewer_client import OpenRouterNeutralReviewerClient
from evaluation.m29_mutation_runner_archive import (
    build_sanitized_case_entry,
    reconcile_m29_publication_gate,
    validate_sanitized_Archive,
    validate_sanitized_archive,
)
from evaluation.m29_mutation_runner_eval import (
    build_completed_case,
    build_post_dispatch_stopped_case,
    build_post_eval_stopped_case,
    build_stopped_case,
    check_candidate_stop_conditions,
    verify_external_evaluator,
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
    make_raw_case_manifest_entry,
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
)
from evaluation.m29_mutation_verifier import (
    BlindedNeutralReviewRequest,
    NeutralReviewerClient,
    NeutralReviewOutcome,
    ReadOnlyExternalEvaluator,
)
from evaluation.m29_mutation_watchdog import EvaluationRetentionWatchdog
from orchestrator.state import MemoryContext
from sandbox.workspace import WorkspaceHandle, WorkspaceManager

LOGGER = logging.getLogger("m29_mutation_runner")

_check_candidate_stop_conditions = check_candidate_stop_conditions
_verify_external_evaluator = verify_external_evaluator
_make_raw_entry = make_raw_case_manifest_entry


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
        post_evaluation_snapshot_hook: (
            Callable[[M29MutationCase, CandidateTaskExecutionResult], TerminalSnapshot] | None
        ) = None,
        fixture_repo_path: Path | None = None,
        expected_verifier_sha256: str | None = None,
        retention_watchdog: EvaluationRetentionWatchdog | None = None,
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
        self.post_evaluation_snapshot_hook = post_evaluation_snapshot_hook
        self.fixture_repo_path = fixture_repo_path
        self.expected_verifier_sha256 = expected_verifier_sha256
        self.retention_watchdog = retention_watchdog
        self.workspace_root: Path = getattr(
            workspace_manager, "root_dir", getattr(workspace_manager, "workspace_root", Path("."))
        ).resolve()
        self._recorded_ws_ids: set[str] = set()
        self._recorded_ws_paths: set[str] = set()

    def _check_pre_dispatch(
        self, case: M29MutationCase, mem: MemoryContext, base_hash: str
    ) -> str | None:
        mem_errs = verify_memory_context_isolation(mem, base_hash, require_empty_personal=True)
        if mem_errs:
            return f"memory_isolation_violation:{';'.join(mem_errs)}"
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
            return f"task_spec_validation_failed:{';'.join(spec_errs)}"
        return None

    def _capture_terminal_snapshot(
        self,
        case: M29MutationCase,
        cand_res: CandidateTaskExecutionResult,
        *,
        post_eval: bool = False,
    ) -> TerminalSnapshot:
        if post_eval and self.post_evaluation_snapshot_hook is not None:
            return self.post_evaluation_snapshot_hook(case, cand_res)
        dispatcher_capture = getattr(self.candidate_dispatcher, "capture_terminal_snapshot", None)
        if callable(dispatcher_capture):
            return dispatcher_capture(case, cand_res)
        return create_terminal_snapshot(
            cand_res.task_status,
            cand_res.terminal_event_type,
            cand_res.terminal_timestamp,
            failure_kind=cand_res.failure_kind,
            diff_sha256=cand_res.diff_sha256,
            changed_files=cand_res.changed_files,
        )

    def _execute_case(
        self,
        case: M29MutationCase,
        current_mem: MemoryContext,
        base_hash: str,
    ) -> tuple[RawCaseManifestEntry, SanitizedCaseManifestEntry, bool, str | None]:
        pre_err = self._check_pre_dispatch(case, current_mem, base_hash)
        if pre_err:
            return self._build_stopped(case, pre_err)

        cand_res = self.candidate_dispatcher.dispatch_case(case, current_mem)
        self._recorded_ws_ids.add(cand_res.workspace_handle.workspace_id)
        self._recorded_ws_paths.add(str(cand_res.workspace_handle.workspace_path.resolve()))
        if self.retention_watchdog is not None:
            self.retention_watchdog.record_workspace(
                case.case_id, cand_res.task_id, cand_res.workspace_handle
            )

        stop_cond = _check_candidate_stop_conditions(cand_res, case)
        if stop_cond:
            return self._build_post_dispatch_stopped(case, cand_res, stop_cond)

        snap_before = self._capture_terminal_snapshot(case, cand_res, post_eval=False)
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
                evaluator_identity_status=(
                    "mismatched"
                    if eval_err.startswith("evaluator_identity_mismatch")
                    else "unavailable"
                ),
                reason=eval_err,
                quality_status="not_assessable",
            )

        snap_after = self._capture_terminal_snapshot(case, cand_res, post_eval=True)
        snap_errs = verify_terminal_snapshot_immutability(snap_before, snap_after)
        if snap_errs:
            reason = f"terminal_snapshot_mutated:{';'.join(snap_errs)}"
            eval_res = replace(
                eval_res,
                acceptance_oracle_passed=False,
                regression_suite_passed=False,
                neutral_review_passed=False,
                reason=reason,
                quality_status="not_assessable",
            )
            return self._build_post_eval_stopped(case, cand_res, snap_before, eval_res, reason)

        if eval_err:
            return self._build_post_eval_stopped(case, cand_res, snap_before, eval_res, eval_err)

        return self._build_completed(case, cand_res, snap_before, eval_res)

    _build_stopped = staticmethod(build_stopped_case)
    _build_post_dispatch_stopped = staticmethod(build_post_dispatch_stopped_case)
    _build_post_eval_stopped = staticmethod(build_post_eval_stopped_case)
    _build_completed = staticmethod(build_completed_case)

    def _clean_workspace_if_eligible(
        self, raw: RawCaseManifestEntry
    ) -> tuple[RawCaseManifestEntry, str | None]:
        hnd = WorkspaceHandle(
            workspace_id=raw.workspace_id,
            task_id=raw.task_id,
            workspace_path=Path(raw.workspace_path),
            repo_path=Path(raw.workspace_path),
            repo_url="",
            cleanup_policy=self.workspace_manager.cleanup_policy,
            trusted_git_dir=Path(raw.trusted_git_dir) if raw.trusted_git_dir else None,
        )
        try:
            cleanup_evaluation_workspace(
                self.workspace_manager,
                hnd,
                succeeded=True,
                recorded_workspace_ids=self._recorded_ws_ids,
                recorded_workspace_paths=self._recorded_ws_paths,
            )
            if self.retention_watchdog is not None:
                self.retention_watchdog.mark_cleaned(raw.workspace_id)
            return replace(raw, cleaned_up=True), None
        except Exception as exc:
            LOGGER.error(
                "Evaluation workspace cleanup failed for %s", raw.workspace_id, exc_info=True
            )
            err_msg = str(exc)
            if self.retention_watchdog is not None:
                self.retention_watchdog.mark_cleaned(raw.workspace_id, error=err_msg)
            return replace(raw, cleaned_up=False, cleanup_error=err_msg), err_msg

    def _cleanup_eligible_workspaces(
        self,
        eligible_indices: list[int],
        raw_list: list[RawCaseManifestEntry],
        cleaned_ws: list[str],
        retained_ws: list[str],
    ) -> str | None:
        stop: str | None = None
        for idx in eligible_indices:
            raw = raw_list[idx]
            if stop is not None:
                if raw.workspace_id != "none":
                    retained_ws.append(raw.workspace_id)
                continue
            cleaned_raw, err = self._clean_workspace_if_eligible(raw)
            raw_list[idx] = cleaned_raw
            if err:
                stop = f"workspace_cleanup_failed:{err}"
                if raw.workspace_id != "none":
                    retained_ws.append(raw.workspace_id)
            else:
                cleaned_ws.append(raw.workspace_id)
        return stop

    def _finalize_run(
        self,
        *,
        halted: bool,
        suite_stop: str | None,
        raw_list: list[RawCaseManifestEntry],
        san_list: list[SanitizedCaseManifestEntry],
        eligible_clean_indices: list[int],
        cleaned_ws: list[str],
        retained_ws: list[str],
    ) -> tuple[bool, str | None, str | None]:
        archive_chk: str | None = None
        if not halted:
            diffs = {r.case_id: r.raw_diff for r in raw_list if r.raw_diff}
            try:
                archive_chk = validate_sanitized_archive(
                    san_list,
                    sanitized_diffs=diffs,
                    expected_case_count=len(self.suite.cases),
                )
            except Exception as exc:
                LOGGER.error("Sanitized archive validation failed", exc_info=True)
                halted = True
                suite_stop = f"archive_validation_failed:{exc}"

        if not halted:
            cleanup_stop = self._cleanup_eligible_workspaces(
                eligible_clean_indices, raw_list, cleaned_ws, retained_ws
            )
            if cleanup_stop:
                halted = True
                suite_stop = cleanup_stop
                archive_chk = None
        else:
            for idx in eligible_clean_indices:
                r = raw_list[idx]
                if r.workspace_id != "none":
                    retained_ws.append(r.workspace_id)
        return halted, suite_stop, archive_chk

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
                fixture_repo_path=self.fixture_repo_path,
                expected_verifier_sha256=self.expected_verifier_sha256,
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
        eligible_clean_indices: list[int] = []
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
            raw_list.append(raw)
            san_list.append(san)

            if stop_reason:
                halted = True
                suite_stop = stop_reason
                if raw.workspace_id != "none":
                    retained_ws.append(raw.workspace_id)
            elif can_clean:
                eligible_clean_indices.append(len(raw_list) - 1)
            else:
                if raw.workspace_id != "none":
                    retained_ws.append(raw.workspace_id)

        halted, suite_stop, archive_chk = self._finalize_run(
            halted=halted,
            suite_stop=suite_stop,
            raw_list=raw_list,
            san_list=san_list,
            eligible_clean_indices=eligible_clean_indices,
            cleaned_ws=cleaned_ws,
            retained_ws=retained_ws,
        )
        final_status: Literal["completed", "halted"] = "halted" if halted else "completed"
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
    "BlindedNeutralReviewRequest",
    "CandidateDispatcher",
    "CandidateTaskExecutionResult",
    "DisposableDbMemoryIsolationManager",
    "EvaluationRetentionWatchdog",
    "ExternalEvaluationResult",
    "ExternalEvaluator",
    "HttpStackCandidateDispatcher",
    "M29MutationRunner",
    "M29MutationRunnerResult",
    "M29_MUTATION_IDENTITY_CONTRACT",
    "NeutralReviewOutcome",
    "NeutralReviewerClient",
    "OpenRouterNeutralReviewerClient",
    "PINNED_TASKBOARD_FIXTURE_REVISION",
    "RawCaseManifestEntry",
    "ReadOnlyExternalEvaluator",
    "SanitizedCaseManifestEntry",
    "TaskCohortSnapshot",
    "TaskboardFixtureInfo",
    "build_sanitized_case_entry",
    "capture_task_cohort_snapshot",
    "clone_case_workspace_from_fixture",
    "compute_verifier_source_sha256",
    "create_taskboard_fixture_repo",
    "default_mutation_constraints",
    "default_mutation_task_spec",
    "reconcile_m29_publication_gate",
    "run_mutation_preflight_checks",
    "validate_sanitized_Archive",
    "validate_sanitized_archive",
    "verify_taskboard_fixture_repo",
]
