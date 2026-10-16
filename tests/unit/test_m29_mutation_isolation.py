"""Unit tests for M29 mutation isolation gates, stop conditions, and cleanup helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from db.enums import TimelineEventType
from db.models import HumanInteraction, TaskTimelineEvent
from evaluation.m29_mutation_isolation import (
    MAX_EVALUATION_RETENTION_SECONDS,
    build_evaluation_cleanup_policy,
    check_delivery_stop_condition,
    check_pending_interaction_stop_condition,
    check_protected_paths_stop_condition,
    cleanup_evaluation_workspace,
    compute_memory_context_hash,
    create_terminal_snapshot,
    validate_evaluation_retention_seconds,
    validate_post_overlay_task_spec,
    verify_memory_context_isolation,
    verify_pair_semantic_equality,
    verify_terminal_snapshot_immutability,
)
from evaluation.m29_mutation_suite import (
    DEFAULT_M29_NEUTRAL_EVALUATOR,
    M29MutationCase,
    MutationTaskBudget,
)
from orchestrator.state import (
    MemoryContext,
    MemoryEntry,
    ObservationContextEntry,
    RepositoryMemoryProfile,
    RepositoryMemoryProfileItem,
    TaskSpec,
)
from sandbox.workspace import (
    WorkspaceCleanupPolicy,
    WorkspaceHandle,
    WorkspaceManager,
    WorkspaceManagerError,
)

NOW = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)


def _make_dummy_case(
    case_id: str,
    worker_profile: str,
    pair_group: str = "m29-mut-pair-01-slug",
    prompt: str = "Implement feature for testing purposes",
    oracle_hash: str = "0" * 64,
    revision: str = "a" * 40,
    timeout: int = 900,
) -> M29MutationCase:
    return M29MutationCase(
        case_id=case_id,
        worker_profile=worker_profile,  # type: ignore[arg-type]
        topic="test_topic",
        pair_group=pair_group,
        pair_order=1,
        prompt=prompt,
        allowed_source_path="taskboard/feature.py",
        entrypoint="run",
        acceptance_oracle="oracle string with min 10 chars",
        acceptance_oracle_sha256=oracle_hash,
        target_repository_revision=revision,
        candidate_timeout_seconds=timeout,
        task_budget=MutationTaskBudget(timeout_seconds=timeout),
        neutral_evaluator=DEFAULT_M29_NEUTRAL_EVALUATOR,
    )


def test_memory_context_hash_and_isolation() -> None:
    """Test deterministic hashing and isolation check for MemoryContext."""
    empty_mem = MemoryContext()
    hash1 = compute_memory_context_hash(empty_mem)
    hash2 = compute_memory_context_hash(MemoryContext())
    assert hash1 == hash2
    assert len(hash1) == 64

    # Baseline matches
    errors = verify_memory_context_isolation(empty_mem, hash1)
    assert errors == []

    # Personal memory is not empty
    mem_with_personal = MemoryContext(
        personal=[MemoryEntry(memory_key="user_pref", value={"editor": "vim"})]
    )
    errors = verify_memory_context_isolation(mem_with_personal, hash1)
    assert "personal_memory_store_not_empty" in errors
    assert any("memory_context_hash_mismatch" in e for e in errors)

    # Disabling require_empty_personal still catches hash mismatch
    errors = verify_memory_context_isolation(mem_with_personal, hash1, require_empty_personal=False)
    assert "personal_memory_store_not_empty" not in errors
    assert any("memory_context_hash_mismatch" in e for e in errors)

    # When baseline hash corresponds to non-empty memory, require_empty_personal=False passes
    hash_personal = compute_memory_context_hash(mem_with_personal)
    errors = verify_memory_context_isolation(
        mem_with_personal, hash_personal, require_empty_personal=False
    )
    assert errors == []


def test_memory_context_hash_sensitivity() -> None:
    """Hash differentiates observations, project memories, and repository profiles."""
    base_hash = compute_memory_context_hash(MemoryContext())

    obs_mem = MemoryContext(
        observations=[
            ObservationContextEntry(
                id="obs-1",
                observed_at=NOW,
                source="test",
                event_type="info",
                summary="Obs summary",
            )
        ]
    )
    assert compute_memory_context_hash(obs_mem) != base_hash

    proj_mem = MemoryContext(project=[MemoryEntry(memory_key="proj_key", value={"pattern": "mvc"})])
    assert compute_memory_context_hash(proj_mem) != base_hash

    profile_item = RepositoryMemoryProfileItem(
        memory_key="key", value={"cmd": "pytest"}, section="verification_commands"
    )
    repo_profile_mem = MemoryContext(
        repository_profile=RepositoryMemoryProfile(verification_commands=[profile_item])
    )
    assert compute_memory_context_hash(repo_profile_mem) != base_hash


def test_validate_post_overlay_task_spec_valid_and_invalid() -> None:
    """Test validation of finalized TaskSpec, profile, and fixture revision."""
    valid_spec = TaskSpec(
        goal="Implement feature",
        task_type="feature",
        allowed_actions=["modify_workspace_files"],
        delivery_mode="workspace",
        verification_commands=[],
        requires_clarification=False,
        requires_permission=False,
    )
    constraints = {"skip_independent_review": True}

    # Valid spec with matching profile and revision
    errors = validate_post_overlay_task_spec(
        valid_spec,
        constraints,
        expected_profile="codex-native-executor",
        actual_profile="codex-native-executor",
        expected_fixture_revision="rev-123",
        actual_fixture_revision="rev-123",
    )
    assert errors == []

    # Profile mismatch
    errors = validate_post_overlay_task_spec(
        valid_spec,
        constraints,
        expected_profile="codex-native-executor",
        actual_profile="antigravity-native-executor",
    )
    assert any("worker_profile_mismatch" in e for e in errors)

    # Revision mismatch
    errors = validate_post_overlay_task_spec(
        valid_spec,
        constraints,
        expected_fixture_revision="rev-123",
        actual_fixture_revision="rev-456",
    )
    assert any("fixture_revision_mismatch" in e for e in errors)

    # Missing spec
    assert validate_post_overlay_task_spec(None, constraints) == ["missing_task_spec"]

    # Invalid task spec type
    assert validate_post_overlay_task_spec(12345, constraints) == ["invalid_task_spec_type"]  # type: ignore[arg-type]


def test_validate_post_overlay_task_spec_dict_mapping() -> None:
    """Test validate_post_overlay_task_spec with dictionary mappings."""
    valid_dict = {
        "goal": "Implement feature",
        "task_type": "feature",
        "allowed_actions": ["modify_workspace_files"],
        "delivery_mode": "workspace",
        "verification_commands": [],
        "requires_clarification": False,
        "requires_permission": False,
    }
    constraints = {"skip_independent_review": True}
    assert validate_post_overlay_task_spec(valid_dict, constraints) == []

    invalid_dict = {"goal": "Bad task spec", "task_type": "invalid_type"}
    errors = validate_post_overlay_task_spec(invalid_dict, constraints)
    assert any("invalid_task_spec" in e or "task_type" in e for e in errors)


def test_verify_pair_semantic_equality_matches() -> None:
    """Matched pair cases with identical controlled semantics succeed."""
    case_a = _make_dummy_case("case-a", "codex-native-executor")
    case_b = _make_dummy_case("case-b", "antigravity-native-executor")

    spec = {
        "goal": "Implement feature",
        "task_type": "feature",
        "allowed_actions": ["modify_workspace_files"],
        "setup_commands": ["setup"],
        "verification_commands": [],
        "delivery_mode": "workspace",
    }
    constraints = {"skip_independent_review": True, "post_terminal_quality_evaluation": True}

    errors = verify_pair_semantic_equality(
        case_a, case_b, spec, spec, constraints, constraints, "profile", "profile"
    )
    assert errors == []


def test_verify_pair_semantic_equality_discrepancies() -> None:
    """Discrepancies in controlled fields are flagged while profile difference is allowed."""
    case_a = _make_dummy_case(
        "case-a", "codex-native-executor", prompt="Prompt 1 for testing purposes"
    )
    case_b = _make_dummy_case(
        "case-b", "antigravity-native-executor", prompt="Prompt 2 for testing purposes"
    )
    spec = {
        "goal": "Implement feature",
        "task_type": "feature",
        "allowed_actions": ["modify_workspace_files"],
    }
    constraints = {"skip_independent_review": True}

    # Prompt mismatch
    errors = verify_pair_semantic_equality(case_a, case_b, spec, spec, constraints, constraints)
    assert "pair_prompt_mismatch" in errors

    # Oracle hash mismatch
    case_b_oracle = _make_dummy_case("case-b", "antigravity-native-executor", oracle_hash="1" * 64)
    case_a_match = _make_dummy_case("case-a", "codex-native-executor")
    errors = verify_pair_semantic_equality(
        case_a_match, case_b_oracle, spec, spec, constraints, constraints
    )
    assert "pair_acceptance_oracle_hash_mismatch" in errors

    # Revision mismatch
    case_b_rev = _make_dummy_case("case-b", "antigravity-native-executor", revision="b" * 40)
    errors = verify_pair_semantic_equality(
        case_a_match, case_b_rev, spec, spec, constraints, constraints
    )
    assert "pair_target_repository_revision_mismatch" in errors

    # Timeout mismatch
    case_b_timeout = _make_dummy_case("case-b", "antigravity-native-executor", timeout=600)
    errors = verify_pair_semantic_equality(
        case_a_match, case_b_timeout, spec, spec, constraints, constraints
    )
    assert "pair_candidate_timeout_mismatch" in errors

    # Budget mismatch
    case_b_budget = _make_dummy_case("case-b", "antigravity-native-executor")
    case_b_budget = case_b_budget.model_copy(
        update={"task_budget": MutationTaskBudget(timeout_seconds=500)}
    )
    errors = verify_pair_semantic_equality(
        case_a_match, case_b_budget, spec, spec, constraints, constraints
    )
    assert "pair_task_budget_mismatch" in errors

    # Evaluator mismatch
    case_b_eval = _make_dummy_case("case-b", "antigravity-native-executor")
    eval_diff = DEFAULT_M29_NEUTRAL_EVALUATOR.model_copy(update={"model": "claude-other"})
    case_b_eval = case_b_eval.model_copy(update={"neutral_evaluator": eval_diff})
    errors = verify_pair_semantic_equality(
        case_a_match, case_b_eval, spec, spec, constraints, constraints
    )
    assert "pair_neutral_evaluator_mismatch" in errors

    # TaskSpec mismatch
    spec_diff = {"task_type": "feature", "allowed_actions": ["other_action"]}
    errors = verify_pair_semantic_equality(
        case_a_match, case_a_match, spec, spec_diff, constraints, constraints
    )
    assert any("pair_task_spec_mismatch" in e for e in errors)

    # Constraint mismatch
    constraints_diff = {"skip_independent_review": False}
    errors = verify_pair_semantic_equality(
        case_a_match, case_a_match, spec, spec, constraints, constraints_diff
    )
    assert "pair_constraint_mismatch:skip_independent_review" in errors

    # Repo profile mismatch
    errors = verify_pair_semantic_equality(
        case_a_match, case_a_match, spec, spec, constraints, constraints, "p1", "p2"
    )
    assert "pair_repo_profile_mismatch" in errors


def test_terminal_snapshot_checksum_and_immutability() -> None:
    """Test terminal snapshot creation, checksum computation, and immutability checks."""
    snap1 = create_terminal_snapshot(
        task_status="completed",
        terminal_event_type="task_completed",
        terminal_timestamp=NOW,
        failure_kind=None,
        diff_sha256="diff123",
        changed_files=["src/foo.py", "src/bar.py"],
    )
    assert snap1.task_status == "completed"
    assert snap1.changed_files == ("src/bar.py", "src/foo.py")
    assert len(snap1.checksum) == 64

    # Identical snapshots pass immutability check
    snap2 = create_terminal_snapshot(
        task_status="completed",
        terminal_event_type="task_completed",
        terminal_timestamp=NOW,
        failure_kind=None,
        diff_sha256="diff123",
        changed_files=["src/bar.py", "src/foo.py"],
    )
    assert verify_terminal_snapshot_immutability(snap1, snap2) == []

    # Dict input with lists passes when values match
    dict_before = {
        "task_status": "completed",
        "terminal_event_type": "task_completed",
        "terminal_timestamp": NOW.isoformat(),
        "failure_kind": None,
        "diff_sha256": "diff123",
        "changed_files": ["src/bar.py", "src/foo.py"],
        "checksum": "chk123",
    }
    dict_after = dict(dict_before)
    dict_after["changed_files"] = ["src/bar.py", "src/foo.py"]
    assert verify_terminal_snapshot_immutability(dict_before, dict_after) == []

    # Mutated status
    snap_status = create_terminal_snapshot(
        task_status="failed",
        terminal_event_type="task_completed",
        terminal_timestamp=NOW,
        diff_sha256="diff123",
        changed_files=["src/bar.py", "src/foo.py"],
    )
    violations = verify_terminal_snapshot_immutability(snap1, snap_status)
    assert any("terminal_snapshot_mutated:task_status" in v for v in violations)
    assert any("terminal_snapshot_mutated:checksum" in v for v in violations)

    # Mutated diff
    snap_diff = create_terminal_snapshot(
        task_status="completed",
        terminal_event_type="task_completed",
        terminal_timestamp=NOW,
        diff_sha256="mutated_diff",
        changed_files=["src/bar.py", "src/foo.py"],
    )
    violations = verify_terminal_snapshot_immutability(snap1, snap_diff)
    assert any("terminal_snapshot_mutated:diff_sha256" in v for v in violations)


def test_check_pending_interaction_stop_condition() -> None:
    """Test detection of pending interactions or interaction events."""
    assert check_pending_interaction_stop_condition([], []) is None

    # Pending interaction present
    interaction = HumanInteraction(
        interaction_type="clarification",
        summary="Please clarify",
        created_at=NOW,
    )
    res = check_pending_interaction_stop_condition([interaction], [])
    assert res == "pending_human_interaction_detected"

    # Timeline event present
    event = TaskTimelineEvent(
        attempt_number=0,
        sequence_number=1,
        event_type=TimelineEventType.APPROVAL_REQUESTED,
        created_at=NOW,
    )
    res = check_pending_interaction_stop_condition([], [event])
    assert res == "human_interaction_event_detected"


def test_check_delivery_stop_condition() -> None:
    """Test detection of delivery attempts in TaskSpec, timeline events, or artifacts."""
    valid_spec = TaskSpec(
        goal="Implement feature",
        task_type="feature",
        allowed_actions=["modify_workspace_files"],
        delivery_mode="workspace",
    )
    assert check_delivery_stop_condition(valid_spec, [], []) is None

    # Invalid delivery mode
    spec_branch = TaskSpec(
        goal="Implement feature",
        task_type="feature",
        allowed_actions=["modify_workspace_files"],
        delivery_mode="branch",
    )
    assert (
        check_delivery_stop_condition(spec_branch, [], []) == "invalid_delivery_mode_not_workspace"
    )

    # Delivery branch present
    spec_deliv_branch = TaskSpec(
        goal="Implement feature",
        task_type="feature",
        allowed_actions=["modify_workspace_files"],
        delivery_mode="workspace",
        delivery_branch="feature-branch",
    )
    assert check_delivery_stop_condition(spec_deliv_branch, [], []) == "delivery_branch_forbidden"

    # Remote delivery actions
    spec_remote_actions = TaskSpec(
        goal="Implement feature",
        task_type="feature",
        allowed_actions=["modify_workspace_files", "prepare_draft_pr_delivery"],
        delivery_mode="workspace",
    )
    assert (
        check_delivery_stop_condition(spec_remote_actions, [], [])
        == "remote_delivery_actions_forbidden"
    )

    # Delivery timeline event
    deliv_event = TaskTimelineEvent(
        attempt_number=0,
        sequence_number=1,
        event_type=TimelineEventType.DELIVERY_STARTED,
        created_at=NOW,
    )
    assert check_delivery_stop_condition(valid_spec, [deliv_event], []) == "delivery_event_detected"

    # Delivery artifact
    deliv_artifact = {"artifact_type": "pull_request", "uri": "https://pr"}
    assert (
        check_delivery_stop_condition(valid_spec, [], [deliv_artifact])
        == "delivery_artifact_detected"
    )


def test_check_protected_paths_stop_condition() -> None:
    """Test boundary checks for modified files."""
    allowed = "taskboard/calculator.py"

    # Clean file within allowed path
    assert check_protected_paths_stop_condition(["taskboard/calculator.py"], allowed) == []

    # Modification of protected path
    violations = check_protected_paths_stop_condition(["evaluation/harness.py"], allowed)
    assert any("protected_path_modified:evaluation/harness.py" in v for v in violations)

    # Modification outside allowed source boundary
    violations = check_protected_paths_stop_condition(["taskboard/unauthorized.py"], allowed)
    assert any("unauthorized_source_path:taskboard/unauthorized.py" in v for v in violations)


def test_retention_and_cleanup_helpers() -> None:
    """Test 72-hour retention validation and workspace cleanup policy enforcement."""
    # Retention limit validation
    validate_evaluation_retention_seconds(3600)
    validate_evaluation_retention_seconds(MAX_EVALUATION_RETENTION_SECONDS)

    with pytest.raises(ValueError, match="exceeds maximum 72 hours"):
        validate_evaluation_retention_seconds(MAX_EVALUATION_RETENTION_SECONDS + 1)

    with pytest.raises(ValueError, match="must be positive"):
        validate_evaluation_retention_seconds(0)

    # Cleanup policy builder
    policy = build_evaluation_cleanup_policy()
    assert policy.delete_on_success is True
    assert policy.retain_on_failure is False

    # Cleanup evaluation workspace
    mock_manager = MagicMock(spec=WorkspaceManager)
    mock_manager.cleanup_workspace.return_value = True

    handle = WorkspaceHandle(
        workspace_id="ws-eval-1",
        task_id="task-1",
        workspace_path=Path("/tmp/ws-eval-1"),
        repo_path=Path("/tmp/ws-eval-1"),
        repo_url="https://github.invalid/repo",
        cleanup_policy=WorkspaceCleanupPolicy(delete_on_success=False, retain_on_failure=True),
    )

    # Refuse cleanup if not in recorded IDs
    with pytest.raises(WorkspaceManagerError, match="not in recorded evaluation workspace IDs"):
        cleanup_evaluation_workspace(
            mock_manager, handle, succeeded=True, recorded_workspace_ids=["other-ws"]
        )

    # Successful cleanup with matching recorded ID
    result = cleanup_evaluation_workspace(
        mock_manager, handle, succeeded=True, recorded_workspace_ids=["ws-eval-1"]
    )
    assert result is True
    assert mock_manager.cleanup_workspace.call_count == 1
    call_handle = mock_manager.cleanup_workspace.call_args[0][0]
    assert call_handle.cleanup_policy.delete_on_success is True
    assert call_handle.cleanup_policy.retain_on_failure is False
