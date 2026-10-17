"""Unit tests for M29 mutation evaluation runner and post-terminal external quality evaluator."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from db.enums import TimelineEventType
from db.models import TaskTimelineEvent
from evaluation.m29_mutation_runner import (
    M29_MUTATION_IDENTITY_CONTRACT,
    CandidateTaskExecutionResult,
    ExternalEvaluationResult,
    M29MutationRunner,
    _check_candidate_stop_conditions,
    _verify_external_evaluator,
    build_sanitized_case_entry,
    reconcile_m29_publication_gate,
    run_mutation_preflight_checks,
    validate_sanitized_archive,
)
from evaluation.m29_mutation_smokes import ExtractorStageSmokeResult
from evaluation.m29_mutation_suite import (
    DEFAULT_M29_NEUTRAL_EVALUATOR,
    M29MutationCase,
    build_m29_mutation_suite,
)
from evaluation.provider_reliability_models import (
    ExecutionIdentity,
)
from orchestrator.state import MemoryContext, MemoryEntry, TaskSpec
from sandbox.workspace import (
    WorkspaceCleanupPolicy,
    WorkspaceHandle,
    WorkspaceManager,
)

NOW = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)


def _make_case(
    case_id: str = "m29-mut-01-codex",
    worker_profile: str = "codex-native-executor",
    allowed_path: str = "taskboard/labels.py",
) -> M29MutationCase:
    return M29MutationCase(
        case_id=case_id,
        task_class="feature",
        mutation_mode="mutation",
        worker_profile=worker_profile,  # type: ignore[arg-type]
        topic="label_normalization",
        pair_group="m29-mut-pair-01-label-normalization",
        pair_order=1 if "codex" in worker_profile else 2,
        prompt="Implement normalize_label in taskboard/labels.py",
        allowed_source_path=allowed_path,
        entrypoint="normalize_label",
        acceptance_oracle="acceptance oracle specification",
        acceptance_oracle_sha256=hashlib.sha256(b"acceptance oracle specification").hexdigest(),
        target_repository_revision="0" * 40,
        neutral_evaluator=DEFAULT_M29_NEUTRAL_EVALUATOR,
    )


def _make_handle(tmp_path: Path, ws_id: str = "ws-01") -> WorkspaceHandle:
    ws_dir = tmp_path / "workspaces" / ws_id
    ws_dir.mkdir(parents=True, exist_ok=True)
    return WorkspaceHandle(
        workspace_id=ws_id,
        task_id=f"t-{ws_id}",
        workspace_path=ws_dir,
        repo_path=ws_dir,
        repo_url="https://github.invalid/fixture",
        cleanup_policy=WorkspaceCleanupPolicy(),
    )


def _make_exec_result(
    handle: WorkspaceHandle,
    profile: str = "codex-native-executor",
    status: str = "completed",
    changed_files: tuple[str, ...] = ("taskboard/labels.py",),
) -> CandidateTaskExecutionResult:
    expected_id = M29_MUTATION_IDENTITY_CONTRACT[profile]
    return CandidateTaskExecutionResult(
        task_id=handle.task_id,
        workspace_handle=handle,
        task_status=status,
        terminal_event_type=(
            TimelineEventType.TASK_COMPLETED.value
            if status == "completed"
            else TimelineEventType.TASK_FAILED.value
        ),
        terminal_timestamp=NOW,
        failure_kind=None if status == "completed" else "compile_error",
        diff_sha256="abc123diff",
        diff_text=(
            "diff --git a/taskboard/labels.py b/taskboard/labels.py\n+def normalize_label(): pass"
        ),
        changed_files=changed_files,
        execution_identity=expected_id,
        execution_identity_status="verified",
        time_to_terminal_seconds=12.5,
    )


def _make_eval_result(
    quality_status: str = "pass",
    reason: str | None = None,
) -> ExternalEvaluationResult:
    return ExternalEvaluationResult(
        acceptance_oracle_passed=quality_status == "pass",
        regression_suite_passed=quality_status == "pass",
        neutral_review_passed=quality_status == "pass",
        evaluator_identity=ExecutionIdentity(
            provider=DEFAULT_M29_NEUTRAL_EVALUATOR.provider,
            model=DEFAULT_M29_NEUTRAL_EVALUATOR.model,
            reasoning_effort=DEFAULT_M29_NEUTRAL_EVALUATOR.reasoning_effort,
        ),
        evaluator_identity_status="verified",
        evaluator_identity_matches=True,
        fallback_attempted=False,
        repairs_attempted=0,
        emitted_verification_events=False,
        emitted_review_artifacts=False,
        reason=reason,
        quality_status=quality_status,
        commands_run=("pytest tests/",),
        exit_codes=(0,),
    )


def test_preflight_retention_and_workspace_validation(tmp_path: Path) -> None:
    suite = build_m29_mutation_suite()
    empty_mem = MemoryContext()

    with pytest.raises(ValueError, match="exceeds maximum 72 hours"):
        run_mutation_preflight_checks(
            db_url="sqlite:///test.db",
            workspace_root=tmp_path / "workspaces",
            retention_seconds=300_000,
            suite=suite,
            baseline_memory_context=empty_mem,
            run_smokes_if_missing=False,
        )

    with pytest.raises(ValueError, match="cannot be the primary repository root"):
        run_mutation_preflight_checks(
            db_url="sqlite:///test.db",
            workspace_root=Path.cwd(),
            retention_seconds=259_200,
            suite=suite,
            baseline_memory_context=empty_mem,
            run_smokes_if_missing=False,
        )

    with pytest.raises(ValueError, match="database URL must not be empty"):
        run_mutation_preflight_checks(
            db_url="   ",
            workspace_root=tmp_path / "workspaces",
            retention_seconds=259_200,
            suite=suite,
            baseline_memory_context=empty_mem,
            run_smokes_if_missing=False,
        )


def test_preflight_smoke_and_personal_memory_validation(tmp_path: Path) -> None:
    suite = build_m29_mutation_suite()
    empty_mem = MemoryContext()

    invalid_smoke = ExtractorStageSmokeResult(
        total_smoke_tasks=20,
        candidate_profiles=("codex-native-executor", "antigravity-native-executor"),
        all_verification_inapplicable=False,
        all_review_inapplicable=True,
        report_verification_pass_rates={},
        report_review_pass_rates={},
        no_review_artifacts=True,
        status_and_events_unchanged=True,
        is_valid=False,
        violations=["smoke verification failed: verification was applicable"],
    )
    with pytest.raises(ValueError, match="extractor stage smoke verification failed"):
        run_mutation_preflight_checks(
            db_url="sqlite:///test.db",
            workspace_root=tmp_path / "workspaces",
            retention_seconds=259_200,
            suite=suite,
            baseline_memory_context=empty_mem,
            smoke_result=invalid_smoke,
            run_smokes_if_missing=False,
        )

    tainted_mem = MemoryContext(
        personal=[
            MemoryEntry(
                memory_key="preference",
                value={"preference": "fast"},
                source="operator",
                confidence=1.0,
                scope="global",
            )
        ]
    )
    valid_smoke = ExtractorStageSmokeResult(
        total_smoke_tasks=20,
        candidate_profiles=("codex-native-executor", "antigravity-native-executor"),
        all_verification_inapplicable=True,
        all_review_inapplicable=True,
        report_verification_pass_rates={},
        report_review_pass_rates={},
        no_review_artifacts=True,
        status_and_events_unchanged=True,
        is_valid=True,
        violations=[],
    )
    with pytest.raises(ValueError, match="personal memory store must be empty"):
        run_mutation_preflight_checks(
            db_url="sqlite:///test.db",
            workspace_root=tmp_path / "workspaces",
            retention_seconds=259_200,
            suite=suite,
            baseline_memory_context=tainted_mem,
            smoke_result=valid_smoke,
            run_smokes_if_missing=False,
        )


def test_preflight_success_and_snapshot_capture(tmp_path: Path) -> None:
    suite = build_m29_mutation_suite()
    empty_mem = MemoryContext()
    valid_smoke = ExtractorStageSmokeResult(
        total_smoke_tasks=20,
        candidate_profiles=("codex-native-executor", "antigravity-native-executor"),
        all_verification_inapplicable=True,
        all_review_inapplicable=True,
        report_verification_pass_rates={},
        report_review_pass_rates={},
        no_review_artifacts=True,
        status_and_events_unchanged=True,
        is_valid=True,
        violations=[],
    )
    snapshot = run_mutation_preflight_checks(
        db_url="sqlite:///test.db",
        workspace_root=tmp_path / "workspaces",
        retention_seconds=259_200,
        suite=suite,
        baseline_memory_context=empty_mem,
        smoke_result=valid_smoke,
        run_smokes_if_missing=False,
        as_of=NOW,
        baseline_ids_by_profile={
            "codex-native-executor": ["task-c1", "task-c2"],
            "antigravity-native-executor": ["task-ag1"],
        },
    )
    assert snapshot.as_of == NOW
    assert snapshot.window_start_at == NOW - timedelta(days=90)
    assert snapshot.baseline_ids_by_profile["codex-native-executor"] == ("task-c1", "task-c2")
    assert snapshot.baseline_ids_by_profile["antigravity-native-executor"] == ("task-ag1",)
    assert len(snapshot.frozen_baseline_ids_sha256_by_profile["codex-native-executor"]) == 64
    assert snapshot.smoke_result == valid_smoke


def test_candidate_stop_condition_pending_interaction(tmp_path: Path) -> None:
    case = _make_case()
    handle = _make_handle(tmp_path)
    res_clean = _make_exec_result(handle)
    assert _check_candidate_stop_conditions(res_clean, case) is None

    res_pending = CandidateTaskExecutionResult(
        task_id=handle.task_id,
        workspace_handle=handle,
        task_status="in_progress",
        terminal_event_type="waiting",
        terminal_timestamp=NOW,
        pending_interaction=True,
        execution_identity=M29_MUTATION_IDENTITY_CONTRACT["codex-native-executor"],
        changed_files=("taskboard/labels.py",),
    )
    assert (
        _check_candidate_stop_conditions(res_pending, case) == "pending_human_interaction_detected"
    )

    ev = TaskTimelineEvent(
        attempt_number=0,
        sequence_number=0,
        event_type=TimelineEventType.APPROVAL_REQUESTED,
        created_at=NOW,
    )
    res_ev = CandidateTaskExecutionResult(
        task_id=handle.task_id,
        workspace_handle=handle,
        task_status="in_progress",
        terminal_event_type="waiting",
        terminal_timestamp=NOW,
        timeline_events=(ev,),
        execution_identity=M29_MUTATION_IDENTITY_CONTRACT["codex-native-executor"],
        changed_files=("taskboard/labels.py",),
    )
    assert _check_candidate_stop_conditions(res_ev, case) == "pending_human_interaction_detected"


def test_candidate_stop_condition_delivery(tmp_path: Path) -> None:
    case = _make_case()
    handle = _make_handle(tmp_path)

    spec_bad = TaskSpec(
        task_type="feature",
        goal=case.prompt,
        delivery_mode="branch",
        allowed_actions=["modify_workspace_files"],
    )
    res_deliv = CandidateTaskExecutionResult(
        task_id=handle.task_id,
        workspace_handle=handle,
        task_status="completed",
        terminal_event_type=TimelineEventType.TASK_COMPLETED.value,
        terminal_timestamp=NOW,
        task_spec=spec_bad,
        execution_identity=M29_MUTATION_IDENTITY_CONTRACT["codex-native-executor"],
        changed_files=("taskboard/labels.py",),
    )
    err = _check_candidate_stop_conditions(res_deliv, case)
    assert err is not None and "delivery_mode" in err

    ev = TaskTimelineEvent(
        attempt_number=0,
        sequence_number=0,
        event_type=TimelineEventType.DELIVERY_STARTED,
        created_at=NOW,
    )
    res_deliv_ev = CandidateTaskExecutionResult(
        task_id=handle.task_id,
        workspace_handle=handle,
        task_status="completed",
        terminal_event_type=TimelineEventType.TASK_COMPLETED.value,
        terminal_timestamp=NOW,
        timeline_events=(ev,),
        execution_identity=M29_MUTATION_IDENTITY_CONTRACT["codex-native-executor"],
        changed_files=("taskboard/labels.py",),
    )
    assert _check_candidate_stop_conditions(res_deliv_ev, case) == "delivery_event_detected"


def test_candidate_stop_condition_task_spec_isolation(tmp_path: Path) -> None:
    case = _make_case()
    handle = _make_handle(tmp_path)

    res_bad_cons = CandidateTaskExecutionResult(
        task_id=handle.task_id,
        workspace_handle=handle,
        task_status="completed",
        terminal_event_type=TimelineEventType.TASK_COMPLETED.value,
        terminal_timestamp=NOW,
        execution_identity=M29_MUTATION_IDENTITY_CONTRACT["codex-native-executor"],
        changed_files=("taskboard/labels.py",),
        constraints={"enable_independent_verifier": True, "post_terminal_quality_evaluation": True},
    )
    err_cons = _check_candidate_stop_conditions(res_bad_cons, case)
    assert err_cons is not None and "task_spec_isolation_violation" in err_cons
    assert "enable_independent_verifier_must_be_false" in err_cons

    spec_bad = TaskSpec(
        task_type="feature",
        goal=case.prompt,
        delivery_mode="workspace",
        requires_clarification=True,
        allowed_actions=["modify_workspace_files"],
    )
    res_bad_spec = CandidateTaskExecutionResult(
        task_id=handle.task_id,
        workspace_handle=handle,
        task_status="completed",
        terminal_event_type=TimelineEventType.TASK_COMPLETED.value,
        terminal_timestamp=NOW,
        execution_identity=M29_MUTATION_IDENTITY_CONTRACT["codex-native-executor"],
        changed_files=("taskboard/labels.py",),
        task_spec=spec_bad,
    )
    err_spec = _check_candidate_stop_conditions(res_bad_spec, case)
    assert err_spec is not None and "task_spec_isolation_violation" in err_spec


def test_candidate_stop_condition_protected_paths(tmp_path: Path) -> None:
    case = _make_case()
    handle = _make_handle(tmp_path)

    res_bad_path = _make_exec_result(
        handle, changed_files=("taskboard/labels.py", "pyproject.toml")
    )
    err = _check_candidate_stop_conditions(res_bad_path, case)
    assert err is not None and "protected_paths_violation" in err
    assert "pyproject.toml" in err

    res_unauth = _make_exec_result(
        handle, changed_files=("taskboard/labels.py", "taskboard/ordering.py")
    )
    err_unauth = _check_candidate_stop_conditions(res_unauth, case)
    assert err_unauth is not None and "unauthorized_source_path:taskboard/ordering.py" in err_unauth


def test_candidate_stop_condition_identity_mismatch(tmp_path: Path) -> None:
    case = _make_case(worker_profile="codex-native-executor")
    handle = _make_handle(tmp_path)

    # Wrong model
    bad_id = ExecutionIdentity(provider="codex", model="gpt-4o", reasoning_effort="high")
    res_bad_id = CandidateTaskExecutionResult(
        task_id=handle.task_id,
        workspace_handle=handle,
        task_status="completed",
        terminal_event_type=TimelineEventType.TASK_COMPLETED.value,
        terminal_timestamp=NOW,
        execution_identity=bad_id,
        execution_identity_status="verified",
        changed_files=("taskboard/labels.py",),
    )
    err = _check_candidate_stop_conditions(res_bad_id, case)
    assert err is not None and "candidate_identity_mismatch" in err

    # Unverified status
    res_unver = CandidateTaskExecutionResult(
        task_id=handle.task_id,
        workspace_handle=handle,
        task_status="completed",
        terminal_event_type=TimelineEventType.TASK_COMPLETED.value,
        terminal_timestamp=NOW,
        execution_identity=M29_MUTATION_IDENTITY_CONTRACT["codex-native-executor"],
        execution_identity_status="unknown_execution_identity",
        changed_files=("taskboard/labels.py",),
    )
    assert "candidate_identity_mismatch" in str(_check_candidate_stop_conditions(res_unver, case))


def test_candidate_stop_condition_in_task_repairs(tmp_path: Path) -> None:
    case = _make_case()
    handle = _make_handle(tmp_path)

    res_repairs = CandidateTaskExecutionResult(
        task_id=handle.task_id,
        workspace_handle=handle,
        task_status="completed",
        terminal_event_type=TimelineEventType.TASK_COMPLETED.value,
        terminal_timestamp=NOW,
        in_task_repairs=1,
        execution_identity=M29_MUTATION_IDENTITY_CONTRACT["codex-native-executor"],
        changed_files=("taskboard/labels.py",),
    )
    assert (
        _check_candidate_stop_conditions(res_repairs, case)
        == "in_task_verification_review_isolation_violation"
    )

    res_art = CandidateTaskExecutionResult(
        task_id=handle.task_id,
        workspace_handle=handle,
        task_status="completed",
        terminal_event_type=TimelineEventType.TASK_COMPLETED.value,
        terminal_timestamp=NOW,
        review_artifacts=("independent_review_result.json",),
        execution_identity=M29_MUTATION_IDENTITY_CONTRACT["codex-native-executor"],
        changed_files=("taskboard/labels.py",),
    )
    assert (
        _check_candidate_stop_conditions(res_art, case)
        == "in_task_verification_review_isolation_violation"
    )


def test_external_evaluator_verification() -> None:
    evaluator_manifest = DEFAULT_M29_NEUTRAL_EVALUATOR

    # Happy path
    clean_eval = _make_eval_result("pass")
    assert _verify_external_evaluator(clean_eval, evaluator_manifest) is None

    # Mismatched model
    bad_eval = ExternalEvaluationResult(
        acceptance_oracle_passed=True,
        regression_suite_passed=True,
        neutral_review_passed=True,
        evaluator_identity=ExecutionIdentity(
            provider="anthropic", model="claude-3-opus", reasoning_effort=None
        ),
        evaluator_identity_status="verified",
    )
    err = _verify_external_evaluator(bad_eval, evaluator_manifest)
    assert err is not None and "evaluator_identity_mismatch" in err

    # Fallback attempted
    fallback_eval = ExternalEvaluationResult(
        acceptance_oracle_passed=True,
        regression_suite_passed=True,
        neutral_review_passed=True,
        evaluator_identity=ExecutionIdentity(
            provider=evaluator_manifest.provider,
            model=evaluator_manifest.model,
            reasoning_effort=evaluator_manifest.reasoning_effort,
        ),
        evaluator_identity_status="verified",
        fallback_attempted=True,
    )
    assert (
        _verify_external_evaluator(fallback_eval, evaluator_manifest)
        == "evaluator_fallback_attempted"
    )

    # Repairs attempted
    repair_eval = ExternalEvaluationResult(
        acceptance_oracle_passed=True,
        regression_suite_passed=True,
        neutral_review_passed=True,
        evaluator_identity=ExecutionIdentity(
            provider=evaluator_manifest.provider,
            model=evaluator_manifest.model,
            reasoning_effort=evaluator_manifest.reasoning_effort,
        ),
        evaluator_identity_status="verified",
        repairs_attempted=1,
    )
    assert (
        _verify_external_evaluator(repair_eval, evaluator_manifest)
        == "evaluator_repair_passes_forbidden"
    )


def test_build_sanitized_case_entry_basic() -> None:
    case = _make_case()
    eval_pass = _make_eval_result("pass")

    # Completed + quality pass
    entry_pass = build_sanitized_case_entry(
        case=case,
        task_status="completed",
        time_to_terminal_seconds=10.0,
        eval_result=eval_pass,
    )
    assert entry_pass.accepted is True
    assert entry_pass.quality_status == "pass"
    assert entry_pass.quality_reason is None

    # Completed + quality fail
    eval_fail = _make_eval_result("fail", reason="oracle_failed")
    entry_fail = build_sanitized_case_entry(
        case=case,
        task_status="completed",
        time_to_terminal_seconds=10.0,
        eval_result=eval_fail,
    )
    assert entry_fail.accepted is True
    assert entry_fail.quality_status == "fail"
    assert entry_fail.quality_reason == "oracle_failed"

    # Terminal failed task
    entry_term_fail = build_sanitized_case_entry(
        case=case,
        task_status="failed",
        failure_kind="test_failure",
        time_to_terminal_seconds=10.0,
        eval_result=eval_fail,
    )
    assert entry_term_fail.accepted is False
    assert entry_term_fail.terminal_status == "failed"
    assert entry_term_fail.report_failure_kind == "test_failure"


def test_build_sanitized_case_entry_evaluator_statuses() -> None:
    case = _make_case()
    eval_pass = _make_eval_result("pass")

    # Evaluator unavailable / unverified
    eval_unavail = ExternalEvaluationResult(
        acceptance_oracle_passed=False,
        regression_suite_passed=False,
        neutral_review_passed=False,
        evaluator_identity_status="unavailable",
        evaluator_identity_matches=False,
        reason="infra_timeout",
        quality_status="not_assessable",
    )
    entry_unavail = build_sanitized_case_entry(
        case=case,
        task_status="completed",
        eval_result=eval_unavail,
    )
    assert entry_unavail.quality_status == "not_assessable"
    assert entry_unavail.evaluator_identity_status == "unavailable"
    assert entry_unavail.evaluator_identity_matches is False
    assert entry_unavail.quality_reason == "infra_timeout"

    # Evaluator verified status but identity mismatch -> mapped to 'mismatched'
    eval_mismatch = ExternalEvaluationResult(
        acceptance_oracle_passed=False,
        regression_suite_passed=False,
        neutral_review_passed=False,
        evaluator_identity_status="verified",
        evaluator_identity_matches=False,
        reason="model_mismatch",
        quality_status="not_assessable",
    )
    entry_mismatch = build_sanitized_case_entry(
        case=case,
        task_status="completed",
        eval_result=eval_mismatch,
    )
    assert entry_mismatch.quality_status == "not_assessable"
    assert entry_mismatch.evaluator_identity_status == "mismatched"
    assert entry_mismatch.evaluator_identity_matches is False

    # Exclusion reason marks accepted=False even if completed
    entry_excluded = build_sanitized_case_entry(
        case=case,
        task_status="completed",
        exclusion_reason="terminal_snapshot_mutated:diff_sha256",
        eval_result=eval_pass,
    )
    assert entry_excluded.accepted is False
    assert entry_excluded.exclusion_reason == "terminal_snapshot_mutated:diff_sha256"


def test_validate_sanitized_archive() -> None:
    case = _make_case()
    eval_pass = _make_eval_result("pass")
    entry = build_sanitized_case_entry(case=case, task_status="completed", eval_result=eval_pass)

    # Valid 1-case archive
    chk = validate_sanitized_archive([entry], expected_case_count=1)
    assert len(chk) == 64

    # Count mismatch
    with pytest.raises(ValueError, match="archive case count mismatch"):
        validate_sanitized_archive([entry], expected_case_count=20)

    # Unsafe diff detection
    diffs = {case.case_id: "diff --git a/file b/file\n+TOKEN_URL = 'https://secret.token'"}
    with pytest.raises(ValueError, match="unsafe credential substring in diff"):
        validate_sanitized_archive([entry], sanitized_diffs=diffs, expected_case_count=1)


def test_runner_preflight_failure(tmp_path: Path) -> None:
    suite = build_m29_mutation_suite()
    dispatcher = MagicMock()
    evaluator = MagicMock()
    mgr = MagicMock(spec=WorkspaceManager)
    mgr.workspace_root = tmp_path / "workspaces"

    runner = M29MutationRunner(
        suite=suite,
        db_url="sqlite:///test.db",
        workspace_manager=mgr,
        baseline_memory_context=MemoryContext(),
        candidate_dispatcher=dispatcher,
        external_evaluator=evaluator,
        retention_seconds=300_000,  # exceeds 72h
    )
    result = runner.run()
    assert result.status == "preflight_failed"
    assert "exceeds maximum 72 hours" in str(result.stop_reason)
    assert len(result.raw_entries) == 0
    assert len(result.workspaces_cleaned) == 0


def test_runner_memory_drift_stop(tmp_path: Path) -> None:
    suite = build_m29_mutation_suite()
    dispatcher = MagicMock()
    evaluator = MagicMock()
    mgr = MagicMock(spec=WorkspaceManager)
    mgr.workspace_root = tmp_path / "workspaces"
    mgr.cleanup_policy = WorkspaceCleanupPolicy()

    # Preflight smoke
    valid_smoke = ExtractorStageSmokeResult(
        total_smoke_tasks=20,
        candidate_profiles=("codex-native-executor", "antigravity-native-executor"),
        all_verification_inapplicable=True,
        all_review_inapplicable=True,
        report_verification_pass_rates={},
        report_review_pass_rates={},
        no_review_artifacts=True,
        status_and_events_unchanged=True,
        is_valid=True,
        violations=[],
    )

    def provide_drifted_memory(case: M29MutationCase) -> MemoryContext:
        return MemoryContext(
            personal=[
                MemoryEntry(
                    memory_key="drift",
                    value={"drift": "val"},
                    source="operator",
                    confidence=1.0,
                    scope="global",
                )
            ]
        )

    runner = M29MutationRunner(
        suite=suite,
        db_url="sqlite:///test.db",
        workspace_manager=mgr,
        baseline_memory_context=MemoryContext(),
        candidate_dispatcher=dispatcher,
        external_evaluator=evaluator,
        smoke_result=valid_smoke,
        memory_context_provider=provide_drifted_memory,
    )

    result = runner.run()
    assert result.status == "halted"
    assert "memory_isolation_violation" in str(result.stop_reason)
    assert len(result.raw_entries) == 20
    assert all(r.case_outcome == "stopped_preflight" for r in result.raw_entries)
    assert "none" not in result.workspaces_retained
    assert result.workspaces_retained == ()


def test_runner_preflight_failure_non_disposable_db(tmp_path: Path) -> None:
    suite = build_m29_mutation_suite()
    dispatcher = MagicMock()
    evaluator = MagicMock()
    mgr = MagicMock(spec=WorkspaceManager)
    mgr.workspace_root = tmp_path / "workspaces"
    mgr.cleanup_policy = WorkspaceCleanupPolicy()

    # Preflight fails due to invalid db_url (non-disposable / persistent name)
    runner = M29MutationRunner(
        suite=suite,
        db_url="postgresql://user:pass@production-db:5432/live",
        workspace_manager=mgr,
        baseline_memory_context=MemoryContext(),
        candidate_dispatcher=dispatcher,
        external_evaluator=evaluator,
    )

    result = runner.run()
    assert result.status == "preflight_failed"
    assert result.stop_reason is not None
    assert len(result.raw_entries) == 0
    assert len(result.sanitized_cases) == 0


def test_validate_sanitized_archive_unknown_case_id() -> None:
    case = _make_case()
    san = build_sanitized_case_entry(case=case, task_status="completed")
    with pytest.raises(ValueError, match="not found in sanitized cases"):
        validate_sanitized_archive(
            [san],
            sanitized_diffs={"unknown-case-id": "diff --git a/foo b/foo\n"},
            expected_case_count=1,
        )


def test_reconcile_m29_publication_gate_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_assert = MagicMock()
    monkeypatch.setattr(
        "evaluation.m29_mutation_runner_archive.assert_mutation_manifest_publication_ready",
        mock_assert,
    )
    res = reconcile_m29_publication_gate(
        manifest=MagicMock(),
        suite=MagicMock(),
        canonical_report=MagicMock(),
        robustness_report=MagicMock(),
    )
    assert res is True
    mock_assert.assert_called_once()
