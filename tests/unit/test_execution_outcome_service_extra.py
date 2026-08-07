"""Additional tests for orchestrator/execution_outcome_service.py helpers."""

from __future__ import annotations

from datetime import UTC
from unittest.mock import MagicMock, patch

from orchestrator.execution_outcome_service import (
    _apply_completion_control_constraints,
    _bridge_observations_after_outcome_commit,
    _existing_scout_fingerprints,
    _merge_scout_phase_result,
    _persist_artifacts_for_run,
    _persist_decomposed_node_outcomes,
    _persist_scout_proposal_if_needed,
    _persist_timeline_events,
    _result_artifacts_payload,
    _result_metadata_payload,
)
from orchestrator.state import (
    NodeOutcome,
    OrchestratorState,
    TaskSpec,
)
from workers import ArtifactReference, WorkerResult

# ---------------------------------------------------------------------------
# _apply_completion_control_constraints
# ---------------------------------------------------------------------------


def test_apply_completion_control_constraints_no_updates():
    task = MagicMock()
    task.constraints = {}
    state = OrchestratorState(task={"task_text": "txt", "repo_url": "url"})
    _apply_completion_control_constraints(task, state)
    # Nothing changed - no constraints keys
    assert task.constraints == {}


def test_apply_completion_control_constraints_with_keys():
    task = MagicMock()
    task.constraints = {}
    state = OrchestratorState(
        task={
            "task_text": "txt",
            "repo_url": "url",
            "constraints": {
                "independent_review_repair_passes_used": 2,
                "independent_verifier_repair_passes_used": 1,
            },
        }
    )
    _apply_completion_control_constraints(task, state)
    assert task.constraints["independent_review_repair_passes_used"] == 2
    assert task.constraints["independent_verifier_repair_passes_used"] == 1


def test_apply_completion_control_constraints_existing_task_constraints():
    task = MagicMock()
    task.constraints = {"existing": "value"}
    state = OrchestratorState(
        task={
            "task_text": "txt",
            "repo_url": "url",
            "constraints": {"independent_review_repair_passes_used": 1},
        }
    )
    _apply_completion_control_constraints(task, state)
    assert task.constraints["existing"] == "value"
    assert task.constraints["independent_review_repair_passes_used"] == 1


# ---------------------------------------------------------------------------
# _merge_scout_phase_result
# ---------------------------------------------------------------------------


def test_merge_scout_phase_result_none():
    scout_phase_metadata: list = []
    summary_parts: list = []
    files_changed: list = []
    all_artifacts: list = []
    budget_usage: dict = {}

    _merge_scout_phase_result(
        None,
        "repo",
        scout_phase_metadata,
        summary_parts,
        files_changed,
        all_artifacts,
        budget_usage,
    )

    assert len(scout_phase_metadata) == 1
    assert "No summary" in scout_phase_metadata[0]["summary"]
    assert len(summary_parts) == 1


def test_merge_scout_phase_result_with_result():
    pr_res = WorkerResult(
        status="success",
        summary="found stuff",
        files_changed=["a.py", "b.py"],
        artifacts=[ArtifactReference(name="log", uri="file:///log.txt", artifact_type="log")],
        budget_usage={"tokens": 100, "cost": 0.5},
    )
    scout_phase_metadata: list = []
    summary_parts: list = []
    files_changed: list = []
    all_artifacts: list = []
    budget_usage: dict = {}

    _merge_scout_phase_result(
        pr_res,
        "research",
        scout_phase_metadata,
        summary_parts,
        files_changed,
        all_artifacts,
        budget_usage,
    )

    assert "found stuff" in scout_phase_metadata[0]["summary"]
    assert "a.py" in files_changed
    assert "b.py" in files_changed
    assert len(all_artifacts) == 1
    assert budget_usage["tokens"] == 100


def test_merge_scout_phase_result_no_duplicate_files():
    pr_res = WorkerResult(status="success", summary="ok", files_changed=["a.py"])
    scout_phase_metadata: list = []
    summary_parts: list = []
    files_changed = ["a.py"]  # already present
    all_artifacts: list = []
    budget_usage: dict = {}

    _merge_scout_phase_result(
        pr_res,
        "repo",
        scout_phase_metadata,
        summary_parts,
        files_changed,
        all_artifacts,
        budget_usage,
    )
    assert files_changed.count("a.py") == 1


def test_merge_scout_phase_result_empty_summary():
    pr_res = WorkerResult(status="success", summary="  ")
    scout_phase_metadata: list = []
    summary_parts: list = []
    files_changed: list = []
    all_artifacts: list = []
    budget_usage: dict = {}

    _merge_scout_phase_result(
        pr_res,
        "deep",
        scout_phase_metadata,
        summary_parts,
        files_changed,
        all_artifacts,
        budget_usage,
    )
    assert "No summary" in scout_phase_metadata[0]["summary"]


# ---------------------------------------------------------------------------
# _result_artifacts_payload
# ---------------------------------------------------------------------------


def test_result_artifacts_payload_none_result():
    fallback = [ArtifactReference(name="ws", uri="file:///ws", artifact_type="workspace")]
    result = _result_artifacts_payload(None, fallback)
    assert len(result) == 1
    assert result[0]["name"] == "ws"


def test_result_artifacts_payload_with_result():
    worker_result = WorkerResult(
        status="success",
        artifacts=[ArtifactReference(name="log", uri="file:///log", artifact_type="log")],
    )
    result = _result_artifacts_payload(worker_result)
    assert len(result) == 1
    assert result[0]["name"] == "log"


def test_result_artifacts_payload_result_no_artifacts():
    worker_result = WorkerResult(status="success", artifacts=[])
    fallback = [ArtifactReference(name="ws", uri="file:///ws", artifact_type="workspace")]
    result = _result_artifacts_payload(worker_result, fallback)
    assert len(result) == 1


# ---------------------------------------------------------------------------
# _result_metadata_payload
# ---------------------------------------------------------------------------


def test_result_metadata_payload_basic():
    task = MagicMock()
    task.id = "t1"
    result = WorkerResult(status="success", summary="done")

    payload = _result_metadata_payload(
        task=task,
        result=result,
        scout_mode="repo",
        worker_run_id="wr-1",
        fallback_artifacts=[],
        scout_depth=None,
        scout_focus=None,
        scout_phase=None,
        scout_phase_metadata=None,
    )
    assert payload["task_id"] == "t1"
    assert payload["scout_mode"] == "repo"
    assert payload["worker_run_id"] == "wr-1"


def test_result_metadata_payload_with_optionals():
    task = MagicMock()
    task.id = "t1"
    result = WorkerResult(status="success", summary="done")

    payload = _result_metadata_payload(
        task=task,
        result=result,
        scout_mode="deep",
        worker_run_id="wr-2",
        fallback_artifacts=[],
        scout_depth="comprehensive",
        scout_focus="security",
        scout_phase="research",
        scout_phase_metadata=[{"phase": "repo", "summary": "found stuff"}],
    )
    assert payload["scout_depth"] == "comprehensive"
    assert payload["scout_focus"] == "security"
    assert payload["scout_phase"] == "research"
    assert len(payload["scout_phase_metadata"]) == 1


# ---------------------------------------------------------------------------
# _existing_scout_fingerprints
# ---------------------------------------------------------------------------


def test_existing_scout_fingerprints_empty():
    proposal_repo = MagicMock()
    proposal_repo.list_proposals.return_value = []
    result = _existing_scout_fingerprints(proposal_repo, task_id="t1")
    assert result == set()


def test_existing_scout_fingerprints_with_fingerprints():
    proposal_repo = MagicMock()
    existing = MagicMock()
    existing.metadata_payload = {"fingerprint": "fp-abc123"}
    proposal_repo.list_proposals.return_value = [existing]

    result = _existing_scout_fingerprints(proposal_repo, task_id="t1")
    assert "fp-abc123" in result


def test_existing_scout_fingerprints_no_metadata():
    proposal_repo = MagicMock()
    existing = MagicMock()
    existing.metadata_payload = "not a dict"
    proposal_repo.list_proposals.return_value = [existing]

    result = _existing_scout_fingerprints(proposal_repo, task_id="t1")
    assert result == set()


def test_existing_scout_fingerprints_no_fingerprint_key():
    proposal_repo = MagicMock()
    existing = MagicMock()
    existing.metadata_payload = {"other_key": "value"}
    proposal_repo.list_proposals.return_value = [existing]

    result = _existing_scout_fingerprints(proposal_repo, task_id="t1")
    assert result == set()


# ---------------------------------------------------------------------------
# _persist_scout_proposal_if_needed
# ---------------------------------------------------------------------------


def test_persist_scout_proposal_if_needed_not_scout():
    proposal_repo = MagicMock()
    task = MagicMock()
    task.constraints = {}
    state = OrchestratorState(
        task={"task_text": "txt", "repo_url": "url"},
        result=WorkerResult(status="success"),
    )
    # Not a scout task → should return without doing anything
    _persist_scout_proposal_if_needed(
        proposal_repo, task=task, state=state, artifacts=[], worker_run_id="wr-1"
    )
    proposal_repo.create_proposal.assert_not_called()


def test_persist_scout_proposal_if_needed_scout_invalid_json():
    proposal_repo = MagicMock()
    task = MagicMock()
    task.id = "t1"
    task.session_id = "s1"
    task.constraints = {"scout_mode": "repo"}

    state = OrchestratorState(
        task={
            "task_text": "scout task",
            "repo_url": "url",
            "constraints": {"scout_mode": "repo"},
        },
        task_spec=TaskSpec(goal="scout", acceptance_criteria=["find stuff"], task_type="scout"),
        result=WorkerResult(status="success", json_payload={"invalid": "proposals"}),
    )

    _persist_scout_proposal_if_needed(
        proposal_repo, task=task, state=state, artifacts=[], worker_run_id="wr-1"
    )
    # Should skip due to validation error
    proposal_repo.create_proposal.assert_not_called()


# ---------------------------------------------------------------------------
# _persist_decomposed_node_outcomes
# ---------------------------------------------------------------------------


def test_persist_decomposed_node_outcomes():
    plan_repo = MagicMock()
    plan_repo.update_node.return_value = None

    state = OrchestratorState(
        task={"task_text": "txt", "repo_url": "url"},
        node_outcomes=[
            NodeOutcome(
                node_id="n1",
                status="completed",
                attempts=1,
                result=WorkerResult(status="success", summary="done"),
            ),
            NodeOutcome(
                node_id="n2",
                status="failed",
                attempts=2,
                result=WorkerResult(status="failure", summary="failed", failure_kind="timeout"),
            ),
        ],
    )

    from datetime import datetime

    finished_at = datetime.now(UTC)

    _persist_decomposed_node_outcomes(
        plan_repo=plan_repo,
        plan_id="plan-1",
        state=state,
        worker_run_id="wr-1",
        finished_at=finished_at,
    )

    assert plan_repo.update_node.call_count == 2


# ---------------------------------------------------------------------------
# _persist_timeline_events
# ---------------------------------------------------------------------------


def test_persist_timeline_events_no_new_events():
    session = MagicMock()
    state = OrchestratorState(
        task={"task_text": "txt", "repo_url": "url"},
    )
    state.timeline_persisted_count = 0
    # No events → nothing to persist
    _persist_timeline_events(session, "t1", state)
    session.assert_not_called()


def test_persist_timeline_events_with_new_events():
    session = MagicMock()
    from db.enums import TimelineEventType
    from orchestrator.state import TaskTimelineEventState

    event = TaskTimelineEventState(
        event_type=TimelineEventType.TASK_INGESTED.value,
        message="task created",
        sequence_number=0,
        attempt_number=0,
    )
    state = OrchestratorState(
        task={"task_text": "txt", "repo_url": "url"},
    )
    state.timeline_events = [event]
    state.timeline_persisted_count = 0
    state.attempt_count = 0

    with patch(
        "orchestrator.execution_outcome_service.TaskTimelineRepository"
    ) as mock_timeline_cls:
        mock_timeline_repo = MagicMock()
        mock_timeline_cls.return_value = mock_timeline_repo
        _persist_timeline_events(session, "t1", state)
        mock_timeline_repo.create_batch.assert_called_once()


def test_persist_timeline_events_already_persisted():
    session = MagicMock()
    from db.enums import TimelineEventType
    from orchestrator.state import TaskTimelineEventState

    event = TaskTimelineEventState(
        event_type=TimelineEventType.TASK_INGESTED.value,
        message="task created",
        sequence_number=0,
        attempt_number=0,
    )
    state = OrchestratorState(
        task={"task_text": "txt", "repo_url": "url"},
    )
    state.timeline_events = [event]
    state.timeline_persisted_count = 1  # already persisted

    state.attempt_count = 0

    with patch(
        "orchestrator.execution_outcome_service.TaskTimelineRepository"
    ) as mock_timeline_cls:
        _persist_timeline_events(session, "t1", state)
        mock_timeline_cls.return_value.create_batch.assert_not_called()


# ---------------------------------------------------------------------------
# _bridge_observations_after_outcome_commit
# ---------------------------------------------------------------------------


def test_bridge_observations_success():
    session_factory = MagicMock()
    session = MagicMock()
    session_factory.return_value.__enter__.return_value = session

    with (
        patch("orchestrator.execution_outcome_service.session_scope") as mock_scope,
        patch("orchestrator.execution_outcome_service.start_optional_span") as mock_span,
        patch("orchestrator.execution_outcome_service.set_span_input_output"),
        patch("memory.observation.ObservationMemoryBridge.bridge_observations") as mock_bridge,
        patch("orchestrator.execution_outcome_service.TaskTimelineRepository") as mock_timeline_cls,
    ):
        mock_scope.return_value.__enter__.return_value = session
        mock_span.return_value.__enter__ = lambda s: None
        mock_span.return_value.__exit__ = lambda s, *a: None
        mock_bridge.return_value = {
            "extracted_candidate_count": 5,
            "proposal_count": 2,
            "durable_memory_count": 1,
            "decision_counts": {},
        }
        mock_timeline_cls.return_value = MagicMock()

        _bridge_observations_after_outcome_commit(session_factory, "t1", attempt_count=1)
        mock_bridge.assert_called_once()


def test_bridge_observations_exception_swallowed():
    session_factory = MagicMock()

    with (
        patch("orchestrator.execution_outcome_service.session_scope") as mock_scope,
        patch("orchestrator.execution_outcome_service.start_optional_span") as mock_span,
        patch("memory.observation.ObservationMemoryBridge.bridge_observations") as mock_bridge,
    ):
        mock_scope.return_value.__enter__.return_value = MagicMock()
        mock_span.return_value.__enter__ = lambda s: None
        mock_span.return_value.__exit__ = lambda s, *a: None
        mock_bridge.side_effect = RuntimeError("bridge failed")

        # Should not raise
        _bridge_observations_after_outcome_commit(session_factory, "t1", attempt_count=1)


# ---------------------------------------------------------------------------
# _persist_artifacts_for_run
# ---------------------------------------------------------------------------


def test_persist_artifacts_for_run_skips_unknown_type():
    artifact_repo = MagicMock()
    artifact = ArtifactReference(name="ws", uri="file:///ws", artifact_type="workspace")
    unknown_artifact = ArtifactReference(
        name="x", uri="file:///x", artifact_type="unknown_custom_type"
    )

    with patch(
        "orchestrator.execution_outcome_service._artifact_type_for_persistence",
        side_effect=lambda a: a.artifact_type if a.artifact_type == "workspace" else None,
    ):
        _persist_artifacts_for_run(
            artifact_repo=artifact_repo,
            worker_run_id="wr-1",
            artifacts=[artifact, unknown_artifact],
            review_artifact_entries=[],
        )
        artifact_repo.create.assert_called_once()


def test_persist_artifacts_for_run_review_entries():
    artifact_repo = MagicMock()
    review_entries = [
        (
            "review_result",
            {"name": "review", "uri": "file:///review.json", "artifact_metadata": {"k": "v"}},
        ),
    ]

    with patch(
        "orchestrator.execution_outcome_service._artifact_type_for_persistence",
        return_value=None,
    ):
        _persist_artifacts_for_run(
            artifact_repo=artifact_repo,
            worker_run_id="wr-1",
            artifacts=[],
            review_artifact_entries=review_entries,
        )
        artifact_repo.create.assert_called_once()
