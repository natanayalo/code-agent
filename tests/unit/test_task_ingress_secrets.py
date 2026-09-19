"""Unit tests for task ingress secret rejection, zero leakage, and replay migration."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.api.auth import ApiAuthConfig
from apps.api.main import create_app
from orchestrator.execution_types import (
    TaskReplayRequest,
    TaskReplayResult,
    TaskSnapshot,
    TaskSubmission,
    TaskSubmissionValidationError,
)
from sandbox.secrets import (
    SecretRef,
    create_authoritative_secret_registry,
)


def _task_snapshot(*, task_id: str = "task-1", status: str = "pending") -> TaskSnapshot:
    now = datetime.now(UTC)
    return TaskSnapshot(
        task_id=task_id,
        session_id="session-1",
        status=status,
        task_text="Test task",
        created_at=now,
        updated_at=now,
    )


class _StubTaskService:
    def __init__(self) -> None:
        self.create_calls: list[TaskSubmission] = []
        self.replay_calls: list[dict[str, Any]] = []
        self.create_error: Exception | None = None
        self.replay_result = TaskReplayResult(
            status="created",
            task_snapshot=_task_snapshot(task_id="task-replayed"),
        )
        self.created_snapshot = _task_snapshot(task_id="task-created")
        self.secret_registry = create_authoritative_secret_registry()

    def create_task(
        self,
        submission: TaskSubmission,
    ) -> tuple[TaskSnapshot, object]:
        self.create_calls.append(submission)
        if self.create_error is not None:
            raise self.create_error
        return self.created_snapshot, object()

    def replay_task(
        self,
        *,
        source_task_id: str,
        replay_request: TaskReplayRequest | None = None,
    ) -> TaskReplayResult:
        self.replay_calls.append(
            {
                "source_task_id": source_task_id,
                "replay_request": replay_request,
            }
        )
        return self.replay_result


@contextlib.contextmanager
def _client(service: _StubTaskService):
    secret = "test-token-secret-1234567890123456"  # gitleaks:allow
    app = create_app(
        task_service=service,  # type: ignore[arg-type]
        auth_config=ApiAuthConfig(shared_secret=secret),
    )
    with TestClient(app) as client:
        client.headers["X-Webhook-Token"] = secret
        yield client


def test_create_task_rejects_legacy_raw_secrets_with_zero_leakage() -> None:
    service = _StubTaskService()
    canary = "super-sensitive-raw-api-key-98765"

    with _client(service) as client:
        response = client.post(
            "/tasks",
            json={
                "task_text": "Test raw secret rejection",
                "secrets": {"OPENAI_API_KEY": canary},
            },
        )

    assert response.status_code == 422
    data = response.json()
    assert data == {
        "detail": {
            "code": "DeprecatedLegacySecretsError",
            "message": "Legacy raw secrets are no longer accepted. Use secret_refs instead.",
        }
    }
    # Zero leakage guarantee
    assert canary not in response.text
    assert canary not in str(response.headers)
    assert service.create_calls == []


@pytest.mark.parametrize(
    "malformed_secrets",
    [
        "raw_string_secret_canary_001",
        ["list_secret_canary_002"],
        {"nested": {"inner": "nested_canary_003"}},
    ],
)
def test_create_task_rejects_malformed_secrets_with_zero_leakage(malformed_secrets: Any) -> None:
    service = _StubTaskService()

    with _client(service) as client:
        response = client.post(
            "/tasks",
            json={
                "task_text": "Test malformed secrets",
                "secrets": malformed_secrets,
            },
        )

    assert response.status_code == 422
    data = response.json()
    assert data["detail"]["code"] == "DeprecatedLegacySecretsError"
    assert "canary" not in response.text
    assert service.create_calls == []


def test_create_task_accepts_empty_secrets_dict() -> None:
    service = _StubTaskService()

    with _client(service) as client:
        response = client.post(
            "/tasks",
            json={
                "task_text": "Test empty secrets dict",
                "secrets": {},
            },
        )

    assert response.status_code == 202
    assert len(service.create_calls) == 1
    assert service.create_calls[0].secrets == {}


def test_create_task_accepts_omitted_secrets() -> None:
    service = _StubTaskService()

    with _client(service) as client:
        response = client.post(
            "/tasks",
            json={
                "task_text": "Test omitted secrets",
            },
        )

    assert response.status_code == 202
    assert len(service.create_calls) == 1
    assert service.create_calls[0].secrets == {}


def test_create_task_rejects_secret_ref_with_metadata() -> None:
    service = _StubTaskService()

    with _client(service) as client:
        response = client.post(
            "/tasks",
            json={
                "task_text": "Test secret ref with metadata",
                "secret_refs": [{"name": "github_token", "metadata": [["foo", "bar"]]}],
            },
        )

    assert response.status_code == 422
    assert "metadata must be empty" in response.text
    assert service.create_calls == []


def test_create_task_unregistered_secret_ref_fails_closed() -> None:
    service = _StubTaskService()
    service.create_error = TaskSubmissionValidationError(
        "Secret reference 'unregistered_secret' is not registered in authoritative SecretRegistry"
    )

    with _client(service) as client:
        response = client.post(
            "/tasks",
            json={
                "task_text": "Test unregistered ref",
                "secret_refs": [{"name": "unregistered_secret"}],
            },
        )

    assert response.status_code == 422
    assert "unregistered_secret" in response.text
    assert "authoritative SecretRegistry" in response.text


def test_replay_task_rejects_legacy_raw_secrets_with_zero_leakage() -> None:
    service = _StubTaskService()
    canary = "replay-sensitive-canary-secret-777"

    with _client(service) as client:
        response = client.post(
            "/tasks/task-1/replay",
            json={
                "secrets": {"TEST_KEY": canary},
            },
        )

    assert response.status_code == 422
    data = response.json()
    assert data == {
        "detail": {
            "code": "DeprecatedLegacySecretsError",
            "message": "Legacy raw secrets are no longer accepted. Use secret_refs instead.",
        }
    }
    assert canary not in response.text
    assert canary not in str(response.headers)
    assert service.replay_calls == []


def test_replay_task_maps_legacy_source_credentials_error_to_structured_422() -> None:
    service = _StubTaskService()
    service.replay_result = TaskReplayResult(
        status="validation_error",
        source_task_id="task-old",
        detail="This task uses legacy credentials. Submit a new task with registered secret_refs.",
    )

    with _client(service) as client:
        response = client.post("/tasks/task-old/replay")

    assert response.status_code == 422
    assert response.json() == {
        "detail": {
            "code": "legacy_source_credentials_not_replayable",
            "message": (
                "This task uses legacy credentials. "
                "Submit a new task with registered secret_refs."
            ),
        }
    }


def test_replay_task_accepts_replacement_secret_refs() -> None:
    service = _StubTaskService()

    with _client(service) as client:
        response = client.post(
            "/tasks/task-old/replay",
            json={
                "secret_refs": [{"name": "github_token"}],
            },
        )

    assert response.status_code == 201
    assert len(service.replay_calls) == 1
    replay_req = service.replay_calls[0]["replay_request"]
    assert replay_req is not None
    assert replay_req.secret_refs == (SecretRef(name="github_token"),)
