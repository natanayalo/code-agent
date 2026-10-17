"""Synthetic integration tests for M29 feature/mutation runner and quality evaluator."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from evaluation.m29_mutation_isolation import TerminalSnapshot, create_terminal_snapshot
from evaluation.m29_mutation_runner import (
    M29_MUTATION_IDENTITY_CONTRACT,
    CandidateDispatcher,
    CandidateTaskExecutionResult,
    ExternalEvaluationResult,
    ExternalEvaluator,
    M29MutationRunner,
)
from evaluation.m29_mutation_runner_archive import validate_sanitized_archive
from evaluation.m29_mutation_smokes import ExtractorStageSmokeResult
from evaluation.m29_mutation_suite import (
    DEFAULT_M29_NEUTRAL_EVALUATOR,
    M29MutationCase,
    build_m29_mutation_suite,
)
from orchestrator.state import MemoryContext
from sandbox.workspace import WorkspaceCleanupPolicy, WorkspaceHandle, WorkspaceManager


def _make_valid_smoke_result() -> ExtractorStageSmokeResult:
    return ExtractorStageSmokeResult(
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


def _provision_test_workspace(
    ws_root: Path, case: M29MutationCase, *, policy: WorkspaceCleanupPolicy
) -> WorkspaceHandle:
    ws_id = f"ws-m29-{case.case_id}"
    ws_path = ws_root / ws_id
    ws_path.mkdir(parents=True, exist_ok=True)
    target_file = ws_path / case.allowed_source_path
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text("x = 1\n", encoding="utf-8")

    git_dir = ws_root / ".code-agent-git" / ws_id
    git_dir.mkdir(parents=True, exist_ok=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/master\n", encoding="utf-8")

    return WorkspaceHandle(
        workspace_id=ws_id,
        task_id=f"task-m29-{case.case_id}",
        workspace_path=ws_path,
        repo_path=ws_path,
        repo_url="https://github.com/example/repo.git",
        branch="feature/eval",
        trusted_git_dir=git_dir,
        cleanup_policy=policy,
    )


class SyntheticDispatcher(CandidateDispatcher):
    """Synthetic candidate dispatcher supporting custom overrides per case."""

    def __init__(
        self,
        ws_root: Path,
        policy: WorkspaceCleanupPolicy,
        *,
        overrides: dict[str, Any] | None = None,
    ) -> None:
        self.ws_root = ws_root
        self.policy = policy
        self.overrides = overrides or {}
        self.dispatched_cases: list[str] = []

    def dispatch_case(
        self, case: M29MutationCase, memory_context: MemoryContext
    ) -> CandidateTaskExecutionResult:
        self.dispatched_cases.append(case.case_id)
        handle = _provision_test_workspace(self.ws_root, case, policy=self.policy)
        raw_diff = f"diff --git a/{case.allowed_source_path} b/{case.allowed_source_path}\n+x = 1\n"
        diff_sha = hashlib.sha256(raw_diff.encode("utf-8")).hexdigest()

        kwargs: dict[str, Any] = {
            "task_id": handle.task_id,
            "workspace_handle": handle,
            "task_status": "completed",
            "terminal_event_type": "TASK_COMPLETED",
            "terminal_timestamp": datetime.now(UTC),
            "time_to_terminal_seconds": 35.5,
            "diff_sha256": diff_sha,
            "diff_text": raw_diff,
            "changed_files": (case.allowed_source_path,),
            "execution_identity": M29_MUTATION_IDENTITY_CONTRACT[case.worker_profile],
            "execution_identity_status": "verified",
            "failure_kind": None,
            "pending_interaction": False,
            "in_task_repairs": 0,
            "verification_events": (),
            "review_artifacts": (),
            "timeline_events": (),
            "artifacts": (),
        }
        if case.case_id in self.overrides:
            kwargs.update(self.overrides[case.case_id])
        return CandidateTaskExecutionResult(**kwargs)


class SyntheticExternalEvaluator(ExternalEvaluator):
    """Synthetic neutral external evaluator."""

    def __init__(
        self,
        *,
        quality_status: str = "pass",
        evaluator_manifest: Any = None,
        overrides: dict[str, Any] | None = None,
    ) -> None:
        self.quality_status = quality_status
        self.evaluator_manifest = evaluator_manifest or DEFAULT_M29_NEUTRAL_EVALUATOR
        self.overrides = overrides or {}
        self.evaluated_cases: list[str] = []

    def evaluate_case(
        self,
        case: M29MutationCase,
        workspace_path: Path,
        terminal_snapshot: Any,
    ) -> ExternalEvaluationResult:
        self.evaluated_cases.append(case.case_id)
        is_pass = self.quality_status == "pass"
        kwargs: dict[str, Any] = {
            "acceptance_oracle_passed": is_pass,
            "regression_suite_passed": is_pass,
            "neutral_review_passed": is_pass,
            "evaluator_identity": self.evaluator_manifest,
            "evaluator_identity_matches": True,
            "quality_status": self.quality_status,
            "reason": None if is_pass else "synthetic_quality_failure",
        }
        if case.case_id in self.overrides:
            kwargs.update(self.overrides[case.case_id])
        return ExternalEvaluationResult(**kwargs)


def test_full_20_case_synthetic_happy_path(tmp_path: Path) -> None:
    suite = build_m29_mutation_suite()
    ws_root = tmp_path / "workspaces"
    policy = WorkspaceCleanupPolicy()
    mgr = WorkspaceManager(root_dir=ws_root, cleanup_policy=policy)
    dispatcher = SyntheticDispatcher(ws_root, policy)
    evaluator = SyntheticExternalEvaluator()

    runner = M29MutationRunner(
        suite=suite,
        db_url="sqlite:///test_disposable_m29.db",
        workspace_manager=mgr,
        baseline_memory_context=MemoryContext(),
        candidate_dispatcher=dispatcher,
        external_evaluator=evaluator,
        smoke_result=_make_valid_smoke_result(),
    )
    result = runner.run()

    assert result.status == "completed"
    assert result.stop_reason is None
    assert len(result.raw_entries) == 20
    assert len(result.sanitized_cases) == 20
    assert len(result.workspaces_cleaned) == 20
    assert len(result.workspaces_retained) == 0
    assert result.sanitized_archive_checksum is not None

    # Check publication gate archive checksum validation
    archive_chk = validate_sanitized_archive(result.sanitized_cases)
    assert archive_chk == result.sanitized_archive_checksum
    assert len(archive_chk) == 64

    # Assert that all workspaces were cleaned up on disk
    for raw in result.raw_entries:
        assert not Path(raw.workspace_path).exists()
        assert not Path(raw.trusted_git_dir).exists()


def test_mid_suite_candidate_stop_condition_delivery(tmp_path: Path) -> None:
    suite = build_m29_mutation_suite()
    ws_root = tmp_path / "workspaces"
    policy = WorkspaceCleanupPolicy()
    mgr = WorkspaceManager(root_dir=ws_root, cleanup_policy=policy)

    # Case 2 triggers stop condition: delivery attempt
    stop_case_id = suite.cases[2].case_id
    overrides = {stop_case_id: {"terminal_event_type": "TASK_DELIVERED"}}
    dispatcher = SyntheticDispatcher(ws_root, policy, overrides=overrides)
    evaluator = SyntheticExternalEvaluator()

    runner = M29MutationRunner(
        suite=suite,
        db_url="sqlite:///test_disposable_m29.db",
        workspace_manager=mgr,
        baseline_memory_context=MemoryContext(),
        candidate_dispatcher=dispatcher,
        external_evaluator=evaluator,
        smoke_result=_make_valid_smoke_result(),
    )
    result = runner.run()

    assert result.status == "halted"
    assert result.stop_reason is not None
    assert "delivery_violation" in result.stop_reason

    # Cases 0 and 1 passed cleanly and were retained (suite halted before archive verification)
    assert result.raw_entries[0].case_outcome == "completed_quality_pass"
    assert result.raw_entries[1].case_outcome == "completed_quality_pass"
    assert Path(result.raw_entries[0].workspace_path).exists()
    assert Path(result.raw_entries[1].workspace_path).exists()

    # Case 2 stopped on delivery and is retained for inspection
    assert result.raw_entries[2].case_outcome == "stopped_safety"
    assert Path(result.raw_entries[2].workspace_path).exists()

    # Cases 3-19 were halted before execution
    for i in range(3, 20):
        assert result.raw_entries[i].case_outcome == "stopped_preflight"
        assert "delivery_violation" in str(result.raw_entries[i].stop_reason)

    assert len(result.workspaces_cleaned) == 0
    assert len(result.workspaces_retained) == 3
    assert result.sanitized_archive_checksum is None


def test_mid_suite_evaluator_unavailability_halt(tmp_path: Path) -> None:
    suite = build_m29_mutation_suite()
    ws_root = tmp_path / "workspaces"
    policy = WorkspaceCleanupPolicy()
    mgr = WorkspaceManager(root_dir=ws_root, cleanup_policy=policy)

    stop_case_id = suite.cases[1].case_id
    eval_overrides = {
        stop_case_id: {
            "evaluator_identity_matches": False,
            "quality_status": "not_assessable",
            "reason": "evaluator identity mismatch / fallback",
        }
    }
    dispatcher = SyntheticDispatcher(ws_root, policy)
    evaluator = SyntheticExternalEvaluator(overrides=eval_overrides)

    runner = M29MutationRunner(
        suite=suite,
        db_url="sqlite:///test_disposable_m29.db",
        workspace_manager=mgr,
        baseline_memory_context=MemoryContext(),
        candidate_dispatcher=dispatcher,
        external_evaluator=evaluator,
        smoke_result=_make_valid_smoke_result(),
    )
    result = runner.run()

    assert result.status == "halted"
    assert result.stop_reason is not None
    assert "evaluator_identity_mismatch" in result.stop_reason

    assert result.raw_entries[0].case_outcome == "completed_quality_pass"
    assert Path(result.raw_entries[0].workspace_path).exists()

    # Case 1 retained on unassessable / fallback
    assert result.raw_entries[1].case_outcome == "quality_not_assessable"
    assert Path(result.raw_entries[1].workspace_path).exists()

    assert len(result.workspaces_cleaned) == 0
    assert len(result.workspaces_retained) == 2


def test_mid_suite_terminal_snapshot_mutation_halt(tmp_path: Path) -> None:
    suite = build_m29_mutation_suite()
    ws_root = tmp_path / "workspaces"
    policy = WorkspaceCleanupPolicy()
    mgr = WorkspaceManager(root_dir=ws_root, cleanup_policy=policy)
    dispatcher = SyntheticDispatcher(ws_root, policy)
    evaluator = SyntheticExternalEvaluator()

    def mutating_snapshot_hook(
        case: M29MutationCase, res: CandidateTaskExecutionResult
    ) -> TerminalSnapshot:
        if case.case_id == suite.cases[1].case_id:
            # Re-read detects mutated diff_sha256 post-evaluation
            return create_terminal_snapshot(
                res.task_status,
                res.terminal_event_type,
                res.terminal_timestamp,
                diff_sha256="mutated_diff_sha256",
            )
        return create_terminal_snapshot(
            res.task_status,
            res.terminal_event_type,
            res.terminal_timestamp,
            failure_kind=res.failure_kind,
            diff_sha256=res.diff_sha256,
            changed_files=res.changed_files,
        )

    runner = M29MutationRunner(
        suite=suite,
        db_url="sqlite:///test_disposable_m29.db",
        workspace_manager=mgr,
        baseline_memory_context=MemoryContext(),
        candidate_dispatcher=dispatcher,
        external_evaluator=evaluator,
        smoke_result=_make_valid_smoke_result(),
        post_evaluation_snapshot_hook=mutating_snapshot_hook,
    )
    result = runner.run()

    assert result.status == "halted"
    assert result.stop_reason is not None
    assert "terminal_snapshot_mutated" in result.stop_reason
    assert result.raw_entries[1].case_outcome == "stopped_mutation"
    assert Path(result.raw_entries[1].workspace_path).exists()

    # Thread 2: verify sanitized case is NOT accepted, and quality_status is not_assessable
    assert result.sanitized_cases[1].accepted is False
    assert result.sanitized_cases[1].quality_status == "not_assessable"
    assert result.sanitized_cases[1].exclusion_reason is not None
    assert "terminal_snapshot_mutated" in result.sanitized_cases[1].exclusion_reason


def test_outcome_cleanup_vs_retention_non_stopping_outcomes(tmp_path: Path) -> None:
    suite = build_m29_mutation_suite()
    ws_root = tmp_path / "workspaces"
    policy = WorkspaceCleanupPolicy()
    mgr = WorkspaceManager(root_dir=ws_root, cleanup_policy=policy)

    # Case 0: Clean candidate pass -> completed_pass (Cleaned)
    # Case 1: Candidate fails cleanly -> candidate_fail (Retained)
    # Case 2: Candidate passes, but quality fails -> quality_fail (Retained)
    # Remaining cases 3-19: Clean pass (Cleaned)
    c1_id = suite.cases[1].case_id
    c2_id = suite.cases[2].case_id

    cand_overrides = {
        c1_id: {
            "task_status": "failed",
            "terminal_event_type": "TASK_FAILED",
            "failure_kind": "test_failure",
        }
    }
    eval_overrides = {
        c2_id: {
            "acceptance_oracle_passed": False,
            "quality_status": "fail",
            "reason": "acceptance oracle rejected patch",
        }
    }

    dispatcher = SyntheticDispatcher(ws_root, policy, overrides=cand_overrides)
    evaluator = SyntheticExternalEvaluator(overrides=eval_overrides)

    runner = M29MutationRunner(
        suite=suite,
        db_url="sqlite:///test_disposable_m29.db",
        workspace_manager=mgr,
        baseline_memory_context=MemoryContext(),
        candidate_dispatcher=dispatcher,
        external_evaluator=evaluator,
        smoke_result=_make_valid_smoke_result(),
    )
    result = runner.run()

    assert result.status == "completed"
    assert result.stop_reason is None
    assert len(result.raw_entries) == 20

    assert result.raw_entries[0].case_outcome == "completed_quality_pass"
    assert not Path(result.raw_entries[0].workspace_path).exists()

    assert result.raw_entries[1].case_outcome == "failed_terminal"
    assert Path(result.raw_entries[1].workspace_path).exists()

    assert result.raw_entries[2].case_outcome == "completed_quality_fail"
    assert Path(result.raw_entries[2].workspace_path).exists()

    # 18 cleaned, 2 retained
    assert len(result.workspaces_cleaned) == 18
    assert len(result.workspaces_retained) == 2


def test_workspace_cleanup_failure_retains_and_halts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suite = build_m29_mutation_suite()
    ws_root = tmp_path / "workspaces"
    policy = WorkspaceCleanupPolicy()
    mgr = WorkspaceManager(root_dir=ws_root, cleanup_policy=policy)
    dispatcher = SyntheticDispatcher(ws_root, policy)
    evaluator = SyntheticExternalEvaluator()

    def failing_cleanup(*args: Any, **kwargs: Any) -> None:
        raise OSError("Permission denied: simulated disk lock on cleanup")

    monkeypatch.setattr(
        "evaluation.m29_mutation_runner.cleanup_evaluation_workspace",
        failing_cleanup,
    )

    runner = M29MutationRunner(
        suite=suite,
        db_url="sqlite:///test_disposable_m29.db",
        workspace_manager=mgr,
        baseline_memory_context=MemoryContext(),
        candidate_dispatcher=dispatcher,
        external_evaluator=evaluator,
        smoke_result=_make_valid_smoke_result(),
    )
    result = runner.run()

    assert result.status == "halted"
    assert result.stop_reason is not None
    assert "workspace_cleanup_failed" in result.stop_reason
    assert result.raw_entries[0].cleaned_up is False
    assert result.raw_entries[0].cleanup_error is not None
    assert "simulated disk lock" in result.raw_entries[0].cleanup_error
    assert result.raw_entries[0].workspace_id in result.workspaces_retained
    assert len(result.workspaces_cleaned) == 0
    assert result.sanitized_archive_checksum is None


def test_archive_validation_diff_credentials_halt(tmp_path: Path) -> None:
    suite = build_m29_mutation_suite()
    ws_root = tmp_path / "workspaces"
    policy = WorkspaceCleanupPolicy()
    mgr = WorkspaceManager(root_dir=ws_root, cleanup_policy=policy)

    leak_case_id = suite.cases[0].case_id
    overrides = {
        leak_case_id: {
            "diff_text": "diff --git a/token b/token\n+TOKEN_URL = 'https://secret.token'\n"
        }
    }
    dispatcher = SyntheticDispatcher(ws_root, policy, overrides=overrides)
    evaluator = SyntheticExternalEvaluator()

    runner = M29MutationRunner(
        suite=suite,
        db_url="sqlite:///test_disposable_m29.db",
        workspace_manager=mgr,
        baseline_memory_context=MemoryContext(),
        candidate_dispatcher=dispatcher,
        external_evaluator=evaluator,
        smoke_result=_make_valid_smoke_result(),
    )
    result = runner.run()

    assert result.status == "halted"
    assert result.stop_reason is not None
    assert "archive_validation_failed" in result.stop_reason
    assert "unsafe credential" in result.stop_reason
    assert len(result.workspaces_cleaned) == 0
    assert len(result.workspaces_retained) == 20
    assert Path(result.raw_entries[0].workspace_path).exists()
