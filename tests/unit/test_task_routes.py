"""Focused API route tests for task control edge cases."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient

from apps.api.auth import ApiAuthConfig
from apps.api.config import SystemConfig
from apps.api.main import create_app
from db.enums import HumanInteractionStatus, TaskStatus
from orchestrator.execution import (
    ApprovalDecisionResult,
    InteractionResponse,
    TaskReplayRequest,
    TaskReplayResult,
    TaskSnapshot,
    TaskSubmission,
    TaskSubmissionValidationError,
    TaskSummarySnapshot,
    TemporalUnavailableError,
)


def _timestamp() -> datetime:
    return datetime.now(UTC)


def _task_summary(*, task_id: str = "task-1", status: str = "pending") -> TaskSummarySnapshot:
    return TaskSummarySnapshot(
        task_id=task_id,
        session_id="session-1",
        status=status,
        task_text="Run the task",
        created_at=_timestamp(),
        updated_at=_timestamp(),
    )


def _task_snapshot(*, task_id: str = "task-1", status: str = "pending") -> TaskSnapshot:
    return TaskSnapshot(**_task_summary(task_id=task_id, status=status).model_dump())


class _FakeTaskService:
    def __init__(self) -> None:
        self.created_snapshot = _task_snapshot()
        self.create_calls: list[TaskSubmission] = []
        self.create_error: Exception | None = None
        self.availability_error: Exception | None = None
        self.list_result = [_task_summary()]
        self.list_calls: list[dict[str, Any]] = []
        self.get_result: TaskSnapshot | None = None
        self.get_calls: list[str] = []
        self.approval_result = ApprovalDecisionResult(status="not_found", detail="missing task")
        self.approval_calls: list[dict[str, Any]] = []
        self.cancel_result: TaskSnapshot | None = None
        self.cancel_calls: list[str] = []
        self.replay_result = TaskReplayResult(status="created", task_snapshot=None)
        self.replay_calls: list[dict[str, Any]] = []
        self.interaction_result: TaskSnapshot | None = None
        self.interaction_calls: list[dict[str, Any]] = []

    def create_task(self, payload: TaskSubmission) -> tuple[TaskSnapshot, object]:
        self.create_calls.append(payload)
        if self.create_error is not None:
            raise self.create_error
        return self.created_snapshot, object()

    def ensure_temporal_available(self) -> None:
        if self.availability_error is not None:
            raise self.availability_error

    def list_tasks(
        self,
        *,
        session_id: str | None,
        status: TaskStatus | None,
        limit: int,
        offset: int,
    ) -> list[TaskSummarySnapshot]:
        self.list_calls.append(
            {
                "session_id": session_id,
                "status": status,
                "limit": limit,
                "offset": offset,
            }
        )
        return self.list_result

    def get_task(self, task_id: str) -> TaskSnapshot | None:
        self.get_calls.append(task_id)
        return self.get_result

    def apply_task_approval_decision(
        self,
        *,
        task_id: str,
        approved: bool,
    ) -> ApprovalDecisionResult:
        self.approval_calls.append({"task_id": task_id, "approved": approved})
        return self.approval_result

    def cancel_task(self, *, task_id: str) -> TaskSnapshot | None:
        self.cancel_calls.append(task_id)
        return self.cancel_result

    def replay_task(
        self,
        *,
        source_task_id: str,
        replay_request: TaskReplayRequest | None,
    ) -> TaskReplayResult:
        self.replay_calls.append(
            {
                "source_task_id": source_task_id,
                "replay_request": replay_request,
            }
        )
        return self.replay_result

    def record_interaction_response(
        self,
        *,
        task_id: str,
        interaction_id: str,
        response: InteractionResponse,
    ) -> TaskSnapshot | None:
        self.interaction_calls.append(
            {
                "task_id": task_id,
                "interaction_id": interaction_id,
                "response": response,
            }
        )
        return self.interaction_result


@contextmanager
def _task_client(service: _FakeTaskService):
    app = create_app(
        task_service=service,  # type: ignore[arg-type]
        auth_config=ApiAuthConfig(shared_secret=("a" * 32)),  # gitleaks:allow
    )
    with TestClient(app) as client:
        client.headers["X-Webhook-Token"] = "a" * 32  # gitleaks:allow
        yield client


def test_list_tasks_forwards_filters_and_returns_summary_payload() -> None:
    """GET /tasks should preserve filter semantics and the summary API contract."""
    service = _FakeTaskService()
    service.list_result = [_task_summary(task_id="task-42", status="failed")]

    with _task_client(service) as client:
        response = client.get(
            "/tasks",
            params={
                "session_id": "session-42",
                "status_filter": "failed",
                "limit": 7,
                "offset": 3,
            },
        )

    assert response.status_code == 200
    assert response.json() == [service.list_result[0].model_dump(mode="json")]
    assert service.list_calls == [
        {
            "session_id": "session-42",
            "status": TaskStatus.FAILED,
            "limit": 7,
            "offset": 3,
        }
    ]


def test_submit_task_returns_created_snapshot() -> None:
    """POST /tasks should return the freshly created task snapshot on success."""
    service = _FakeTaskService()
    service.created_snapshot = _task_snapshot(task_id="task-created", status="pending")

    with _task_client(service) as client:
        response = client.post(
            "/tasks",
            json={
                "task_text": "Create the task",
            },
        )

    assert response.status_code == 202
    assert response.json()["task_id"] == "task-created"
    assert len(service.create_calls) == 1
    assert service.create_calls[0].task_text == "Create the task"
    anonymous_thread_id = service.create_calls[0].session.external_thread_id
    assert anonymous_thread_id != "http-default"
    assert str(uuid.UUID(anonymous_thread_id)) == anonymous_thread_id


def test_submit_task_returns_422_for_validation_errors() -> None:
    """Semantic task-submission validation failures should map to HTTP 422."""
    service = _FakeTaskService()
    service.create_error = TaskSubmissionValidationError("submission is invalid")

    with _task_client(service) as client:
        response = client.post("/tasks", json={"task_text": "Create the task"})

    assert response.status_code == 422
    assert response.json() == {"detail": "submission is invalid"}


def test_submit_task_returns_503_without_persisting_when_temporal_is_unavailable() -> None:
    """Submission outage must leave the API inspectable and create no task."""
    service = _FakeTaskService()
    service.availability_error = TemporalUnavailableError("Temporal is unavailable")

    with _task_client(service) as client:
        response = client.post("/tasks", json={"task_text": "Create the task"})
        read_response = client.get("/tasks")

    assert response.status_code == 503
    assert response.json() == {"detail": "Temporal is unavailable"}
    assert service.create_calls == []
    assert read_response.status_code == 200


def test_get_task_returns_snapshot_when_found() -> None:
    """GET /tasks/{id} should return the persisted snapshot when present."""
    service = _FakeTaskService()
    service.get_result = _task_snapshot(task_id="task-7", status="completed")

    with _task_client(service) as client:
        response = client.get("/tasks/task-7")

    assert response.status_code == 200
    assert response.json()["task_id"] == "task-7"
    assert response.json()["status"] == "completed"
    assert service.get_calls == ["task-7"]


def test_get_task_returns_not_found_for_missing_task() -> None:
    """GET /tasks/{id} should surface unknown tasks as 404."""
    service = _FakeTaskService()

    with _task_client(service) as client:
        response = client.get("/tasks/task-missing")

    assert response.status_code == 404
    assert response.json() == {"detail": "Task 'task-missing' was not found."}
    assert service.get_calls == ["task-missing"]


def test_task_approval_endpoint_returns_not_found_for_missing_task() -> None:
    """Approval decisions should surface missing tasks as a 404, not a silent no-op."""
    service = _FakeTaskService()
    service.approval_result = ApprovalDecisionResult(
        status="not_found",
        detail="task is gone",
    )

    with _task_client(service) as client:
        response = client.post("/tasks/task-404/approval", json={"approved": True})

    assert response.status_code == 404
    assert response.json() == {"detail": "task is gone"}
    assert service.approval_calls == [{"task_id": "task-404", "approved": True}]


def test_task_approval_endpoint_returns_conflict_for_opposing_decision() -> None:
    """Conflicting approval decisions should be reported as HTTP 409."""
    service = _FakeTaskService()
    service.approval_result = ApprovalDecisionResult(
        status="conflict",
        detail="approval already recorded as rejected",
    )

    with _task_client(service) as client:
        response = client.post("/tasks/task-1/approval", json={"approved": True})

    assert response.status_code == 409
    assert response.json() == {"detail": "approval already recorded as rejected"}


def test_task_approval_endpoint_returns_not_waiting_conflict() -> None:
    """Approval requests for tasks outside the waiting state should return 409."""
    service = _FakeTaskService()
    service.approval_result = ApprovalDecisionResult(
        status="not_waiting",
        detail="task is already running",
    )

    with _task_client(service) as client:
        response = client.post("/tasks/task-1/approval", json={"approved": True})

    assert response.status_code == 409
    assert response.json() == {"detail": "task is already running"}


def test_task_approval_endpoint_returns_snapshot_when_applied() -> None:
    """Successful approval decisions should return the refreshed task snapshot."""
    service = _FakeTaskService()
    service.approval_result = ApprovalDecisionResult(
        status="applied",
        task_snapshot=_task_snapshot(task_id="task-1", status="pending"),
    )

    with _task_client(service) as client:
        response = client.post("/tasks/task-1/approval", json={"approved": True})

    assert response.status_code == 200
    assert response.json()["task_id"] == "task-1"
    assert response.json()["status"] == "pending"


def test_task_approval_endpoint_returns_500_when_snapshot_reload_fails() -> None:
    """Approval responses should fail loudly when state mutates but the snapshot cannot reload."""
    service = _FakeTaskService()
    service.approval_result = ApprovalDecisionResult(status="applied", task_snapshot=None)

    with _task_client(service) as client:
        response = client.post("/tasks/task-1/approval", json={"approved": False})

    assert response.status_code == 500
    assert response.json() == {
        "detail": "Task decision was applied but the task snapshot could not be reloaded."
    }


def test_cancel_task_returns_not_found_for_unknown_task() -> None:
    """Cancellation should report missing tasks instead of pretending the request succeeded."""
    service = _FakeTaskService()

    with _task_client(service) as client:
        response = client.post("/tasks/task-missing/cancel")

    assert response.status_code == 404
    assert response.json() == {"detail": "Task 'task-missing' was not found."}
    assert service.cancel_calls == ["task-missing"]


def test_cancel_task_returns_snapshot_when_cancellation_succeeds() -> None:
    """Successful cancellations should return the terminal task snapshot to callers."""
    service = _FakeTaskService()
    service.cancel_result = _task_snapshot(task_id="task-9", status="failed")

    with _task_client(service) as client:
        response = client.post("/tasks/task-9/cancel")

    assert response.status_code == 200
    assert response.json()["task_id"] == "task-9"
    assert response.json()["status"] == "failed"


def test_replay_task_returns_500_when_reloaded_snapshot_is_missing() -> None:
    """Replay should not return 201 unless the follow-up task snapshot is available."""
    service = _FakeTaskService()
    service.replay_result = TaskReplayResult(status="created", task_snapshot=None)

    with _task_client(service) as client:
        response = client.post(
            "/tasks/task-1/replay",
            json={"constraints": {"note": "retry with additional logs"}},
        )

    assert response.status_code == 500
    assert response.json() == {
        "detail": "Replay task was created but the snapshot could not be reloaded."
    }
    assert service.replay_calls[0]["source_task_id"] == "task-1"
    replay_request = service.replay_calls[0]["replay_request"]
    assert replay_request is not None
    assert replay_request.constraints == {"note": "retry with additional logs"}


def test_replay_task_returns_422_for_submission_validation_errors() -> None:
    """Replay validation failures should map to HTTP 422."""
    service = _FakeTaskService()
    service.replay_result = TaskReplayResult(status="created", task_snapshot=None)

    def _raise_validation(
        *,
        source_task_id: str,
        replay_request: TaskReplayRequest | None,
    ) -> TaskReplayResult:
        raise TaskSubmissionValidationError("replay payload is invalid")

    service.replay_task = _raise_validation  # type: ignore[method-assign]

    with _task_client(service) as client:
        response = client.post("/tasks/task-1/replay", json={"budget": {"max_iterations": -1}})

    assert response.status_code == 422
    assert response.json() == {"detail": "replay payload is invalid"}


def test_replay_task_returns_not_found_when_source_task_is_missing() -> None:
    """Replay of a missing source task should surface as HTTP 404."""
    service = _FakeTaskService()
    service.replay_result = TaskReplayResult(status="not_found", detail="missing source task")

    with _task_client(service) as client:
        response = client.post("/tasks/task-404/replay")

    assert response.status_code == 404
    assert response.json() == {"detail": "missing source task"}


def test_replay_task_returns_conflict_for_non_replayable_state() -> None:
    """Replay of a non-terminal task should surface as HTTP 409."""
    service = _FakeTaskService()
    service.replay_result = TaskReplayResult(status="not_replayable", detail="task is running")

    with _task_client(service) as client:
        response = client.post("/tasks/task-1/replay")

    assert response.status_code == 409
    assert response.json() == {"detail": "task is running"}


def test_replay_task_returns_created_snapshot_when_successful() -> None:
    """Successful replay should return the new task snapshot with HTTP 201."""
    service = _FakeTaskService()
    service.replay_result = TaskReplayResult(
        status="created",
        task_snapshot=_task_snapshot(task_id="task-replayed", status="pending"),
    )

    with _task_client(service) as client:
        response = client.post("/tasks/task-1/replay")

    assert response.status_code == 201
    assert response.json()["task_id"] == "task-replayed"
    assert response.json()["status"] == "pending"


def test_record_interaction_response_returns_not_found_when_interaction_is_missing() -> None:
    """Interaction replies should return 404 when the pending record no longer exists."""
    service = _FakeTaskService()

    with _task_client(service) as client:
        response = client.post(
            "/tasks/task-1/interactions/interaction-404/response",
            json={"response_data": {"answer": "use main branch"}},
        )

    assert response.status_code == 404
    assert response.json() == {
        "detail": "Interaction 'interaction-404' for task 'task-1' was not found."
    }
    assert service.interaction_calls[0]["task_id"] == "task-1"
    assert service.interaction_calls[0]["interaction_id"] == "interaction-404"
    assert service.interaction_calls[0]["response"] == InteractionResponse(
        response_data={"answer": "use main branch"},
        status=HumanInteractionStatus.RESOLVED,
    )


def test_record_interaction_response_returns_updated_snapshot_when_found() -> None:
    """Interaction replies should return the refreshed task snapshot after persistence succeeds."""
    service = _FakeTaskService()
    service.interaction_result = _task_snapshot(task_id="task-1", status="pending")

    with _task_client(service) as client:
        response = client.post(
            "/tasks/task-1/interactions/interaction-1/response",
            json={"response_data": {"approved": True}},
        )

    assert response.status_code == 200
    assert response.json()["task_id"] == "task-1"
    assert response.json()["status"] == "pending"


def test_trigger_scout_task_returns_created_snapshot() -> None:
    """POST /tasks/scout/trigger should submit a scout task with configured defaults."""
    service = _FakeTaskService()
    service.created_snapshot = _task_snapshot(task_id="task-scout", status="pending")

    with _task_client(service) as client:
        # override config
        client.app.state.system_config = SystemConfig(
            default_image="test",
            workspace_root="/tmp",
            scout_repo_key="scout-repo",
            allowed_repos={"scout-repo": "https://github.com/scout/repo"},
            scout_branch="main",
            scout_task_text="Scout text",
        )
        response = client.post("/tasks/scout/trigger")

    assert response.status_code == 202
    assert response.json()["task_id"] == "task-scout"
    assert len(service.create_calls) == 1

    submission = service.create_calls[0]
    assert submission.task_text == "Scout text"
    assert submission.repo_url == "https://github.com/scout/repo"
    assert submission.branch == "main"
    assert submission.constraints == {
        "task_type": "scout",
        "trigger_source": "manual",
        "scout_mode": "repo",
        "scout_depth": "standard",
        "max_proposals": 5,
    }
    assert submission.session.external_user_id == "system:scout-scheduler"


def test_trigger_scout_task_with_explicit_parameters() -> None:
    """POST /tasks/scout/trigger should accept explicit parameters and cap max_proposals."""
    service = _FakeTaskService()
    service.created_snapshot = _task_snapshot(task_id="task-scout-2", status="pending")

    with _task_client(service) as client:
        client.app.state.system_config = SystemConfig(
            default_image="test",
            workspace_root="/tmp",
            allowed_repos={"test-key": "https://github.com/allowed/repo"},
        )
        response = client.post(
            "/tasks/scout/trigger",
            json={
                "mode": "research",
                "repo_key": "test-key",
                "branch": "feature",
                "focus": "improve performance",
                "depth": "deep",
                "max_proposals": 999,  # Should be capped at 20
            },
        )

    assert response.status_code == 202
    assert response.json()["task_id"] == "task-scout-2"
    assert len(service.create_calls) == 1

    submission = service.create_calls[0]
    assert submission.repo_url == "https://github.com/allowed/repo"
    assert submission.branch == "feature"
    assert submission.constraints == {
        "task_type": "scout",
        "trigger_source": "manual",
        "scout_mode": "research",
        "scout_depth": "deep",
        "max_proposals": 20,
        "scout_focus": "improve performance",
    }


def test_trigger_scout_task_normalizes_blank_strings() -> None:
    """POST /tasks/scout/trigger should treat blank strings as omitted."""
    service = _FakeTaskService()
    service.created_snapshot = _task_snapshot()

    with _task_client(service) as client:
        client.app.state.system_config = SystemConfig(
            default_image="test",
            workspace_root="/tmp",
            scout_repo_key="fallback-repo",
            allowed_repos={"fallback-repo": "https://github.com/fallback/repo"},
            scout_branch="main",
        )
        response = client.post(
            "/tasks/scout/trigger",
            json={
                "repo_key": "   ",
                "branch": "   ",
                "focus": "   ",
            },
        )

    assert response.status_code == 202
    submission = service.create_calls[0]
    assert submission.repo_url == "https://github.com/fallback/repo"
    assert submission.branch == "main"
    assert "scout_focus" not in submission.constraints


def test_trigger_scout_task_returns_400_for_unknown_repo_key() -> None:
    """POST /tasks/scout/trigger should return 400 if repo_key is provided but unknown."""
    service = _FakeTaskService()

    with _task_client(service) as client:
        client.app.state.system_config = SystemConfig(
            default_image="test",
            workspace_root="/tmp",
            allowed_repos={"known": "https://github.com/known/repo"},
        )
        response = client.post(
            "/tasks/scout/trigger",
            json={"repo_key": "unknown"},
        )

    assert response.status_code == 400
    assert "not in the allowlist" in response.json()["detail"]


def test_trigger_scout_task_returns_422_when_research_focus_is_missing() -> None:
    """POST /tasks/scout/trigger should return 422 if mode is research but focus is omitted."""
    service = _FakeTaskService()

    with _task_client(service) as client:
        client.app.state.system_config = SystemConfig(
            default_image="test",
            workspace_root="/tmp",
            scout_repo_key="fallback-repo",
            allowed_repos={"fallback-repo": "https://github.com/fallback/repo"},
        )
        response = client.post(
            "/tasks/scout/trigger",
            json={"mode": "research", "focus": "   "},
        )

    assert response.status_code == 422
    assert "requires a focus topic" in response.json()["detail"][0]["msg"]


def test_trigger_scout_task_returns_400_when_unconfigured() -> None:
    """POST /tasks/scout/trigger should return 400 if scout repo is missing."""
    service = _FakeTaskService()

    with _task_client(service) as client:
        client.app.state.system_config = SystemConfig(
            default_image="test",
            workspace_root="/tmp",
            scout_repo_key="",  # missing
        )
        response = client.post("/tasks/scout/trigger")

    assert response.status_code == 400
    assert "missing valid repo_key or default repo" in response.json()["detail"]
    assert len(service.create_calls) == 0


def test_trigger_scout_task_rejects_extra_fields() -> None:
    """POST /tasks/scout/trigger should return 422 if extra fields (like repo_url) are provided."""
    service = _FakeTaskService()

    with _task_client(service) as client:
        client.app.state.system_config = SystemConfig(
            default_image="test",
            workspace_root="/tmp",
            scout_repo_key="scout-repo",
            allowed_repos={"scout-repo": "https://github.com/scout/repo"},
        )
        response = client.post(
            "/tasks/scout/trigger",
            json={"repo_url": "https://github.com/scout/repo"},
        )

    assert response.status_code == 422
