"""Evaluation-stage validations and manifest entry builders for M29 mutation runner."""

from __future__ import annotations

from evaluation.m29_mutation_isolation import (
    TerminalSnapshot,
    check_delivery_stop_condition,
    check_pending_interaction_stop_condition,
    check_protected_paths_stop_condition,
    create_terminal_snapshot,
    validate_post_overlay_task_spec,
)
from evaluation.m29_mutation_runner_archive import build_sanitized_case_entry
from evaluation.m29_mutation_runner_models import (
    M29_MUTATION_IDENTITY_CONTRACT,
    CandidateTaskExecutionResult,
    ExternalEvaluationResult,
    RawCaseManifestEntry,
    SanitizedCaseManifestEntry,
    make_raw_case_manifest_entry,
)
from evaluation.m29_mutation_runner_preflight import (
    default_mutation_constraints,
    default_mutation_task_spec,
)
from evaluation.m29_mutation_suite import (
    M29MutationCase,
    NeutralEvaluatorManifest,
)


def check_candidate_stop_conditions(
    result: CandidateTaskExecutionResult,
    case: M29MutationCase,
) -> str | None:
    """Evaluate candidate execution outcome against all M29 stop condition gates."""
    spec = result.task_spec or default_mutation_task_spec(case)
    cons = result.constraints if result.constraints is not None else default_mutation_constraints()
    spec_errs = validate_post_overlay_task_spec(
        spec,
        cons,
        expected_profile=case.worker_profile,
        actual_profile=case.worker_profile,
        expected_fixture_revision=case.target_repository_revision,
        actual_fixture_revision=case.target_repository_revision,
    )
    if spec_errs:
        return f"task_spec_isolation_violation:{';'.join(spec_errs)}"

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
        spec,
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


def verify_external_evaluator(
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


def build_stopped_case(
    case: M29MutationCase,
    reason: str,
) -> tuple[RawCaseManifestEntry, SanitizedCaseManifestEntry, bool, str]:
    """Build entries for a case stopped before dispatch."""
    raw = make_raw_case_manifest_entry(case, stop_reason=reason, case_outcome="stopped_preflight")
    san = build_sanitized_case_entry(
        case=case,
        task_status="failed",
        failure_kind="stopped",
        candidate_identity_status="unknown_execution_identity",
        identity_matches=False,
        exclusion_reason=reason,
    )
    return raw, san, False, reason


def build_post_dispatch_stopped_case(
    case: M29MutationCase,
    res: CandidateTaskExecutionResult,
    reason: str,
) -> tuple[RawCaseManifestEntry, SanitizedCaseManifestEntry, bool, str]:
    """Build entries for a case stopped after candidate dispatch."""
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
    raw = make_raw_case_manifest_entry(
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


def build_post_eval_stopped_case(
    case: M29MutationCase,
    res: CandidateTaskExecutionResult,
    snap: TerminalSnapshot,
    eval_res: ExternalEvaluationResult,
    reason: str,
) -> tuple[RawCaseManifestEntry, SanitizedCaseManifestEntry, bool, str]:
    """Build entries for a case stopped during/after external evaluation."""
    hnd = res.workspace_handle
    trusted_git = str(hnd.trusted_git_dir) if hnd.trusted_git_dir else None
    raw = make_raw_case_manifest_entry(
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
        exclusion_reason=reason,
        eval_result=eval_res,
    )
    return raw, san, False, reason


def build_completed_case(
    case: M29MutationCase,
    res: CandidateTaskExecutionResult,
    snap: TerminalSnapshot,
    eval_res: ExternalEvaluationResult,
) -> tuple[RawCaseManifestEntry, SanitizedCaseManifestEntry, bool, None]:
    """Build entries for a fully completed case with external evaluation."""
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
    raw = make_raw_case_manifest_entry(
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


__all__ = [
    "build_completed_case",
    "build_post_dispatch_stopped_case",
    "build_post_eval_stopped_case",
    "build_stopped_case",
    "check_candidate_stop_conditions",
    "verify_external_evaluator",
]
