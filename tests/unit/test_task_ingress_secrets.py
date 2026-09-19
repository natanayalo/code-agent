"""Unit tests for task ingress secret rejection, zero leakage, and replay migration."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

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

    def create_task_outcome(
        self,
        submission: TaskSubmission,
        *,
        delivery_key: Any = None,
    ) -> Any:
        self.create_calls.append(submission)
        if self.create_error is not None:
            raise self.create_error
        outcome = MagicMock()
        outcome.task_snapshot = self.created_snapshot
        outcome.duplicate = False
        return outcome

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
                "This task uses legacy credentials. Submit a new task with registered secret_refs."
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


def test_webhook_rejects_legacy_raw_secrets_with_zero_leakage() -> None:
    service = _StubTaskService()
    canary = "webhook-sensitive-raw-token-998877"

    with _client(service) as client:
        response = client.post(
            "/webhook",
            json={
                "task_text": "Webhook raw secret test",
                "secrets": {"CUSTOM_KEY": canary},
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
    assert service.create_calls == []


def test_webhook_accepts_valid_payload_without_secrets() -> None:
    service = _StubTaskService()

    with _client(service) as client:
        response = client.post(
            "/webhook",
            json={
                "task_text": "Webhook valid test",
            },
        )

    assert response.status_code == 202
    assert len(service.create_calls) == 1
    assert service.create_calls[0].secrets == {}


def test_create_task_rejects_secret_ref_with_metadata_zero_leakage() -> None:
    service = _StubTaskService()
    canary_key = "sensitive_meta_canary_key_111"
    canary_val = "sensitive_meta_canary_val_222"

    with _client(service) as client:
        response = client.post(
            "/tasks",
            json={
                "task_text": "Metadata canary test",
                "secret_refs": [
                    {
                        "name": "github_token",
                        "metadata": [[canary_key, canary_val]],
                    }
                ],
            },
        )

    assert response.status_code == 422
    assert canary_key not in response.text
    assert canary_val not in response.text
    assert service.create_calls == []


def test_create_task_empty_registry_rejects_default_secrets_fail_closed() -> None:
    from sqlalchemy.pool import StaticPool

    from db.base import Base
    from orchestrator import execution as execution_module
    from repositories import create_engine_from_url, create_session_factory
    from sandbox.secrets import SecretRegistry
    from workers import Worker, WorkerRequest, WorkerResult

    class _LocalWorker(Worker):
        async def run(self, request: WorkerRequest) -> WorkerResult:
            return WorkerResult(status="success", summary="done")

    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)

    empty_registry = SecretRegistry()
    service = execution_module.TaskExecutionService(
        session_factory=session_factory,
        worker=_LocalWorker(),
        secret_registry=empty_registry,
    )

    submission = TaskSubmission(
        task_text="Test empty registry gating",
        secret_refs=(SecretRef(name="github_token"),),
    )

    with pytest.raises(
        TaskSubmissionValidationError,
        match="is not registered in authoritative SecretRegistry",
    ):
        service.create_task(submission)


def test_create_task_empty_registry_duplicate_delivery_fails_closed() -> None:
    from sqlalchemy.pool import StaticPool

    from db.base import Base
    from orchestrator import execution as execution_module
    from repositories import (
        create_engine_from_url,
        create_session_factory,
    )
    from sandbox.secrets import SecretRegistry
    from workers import Worker, WorkerRequest, WorkerResult

    class _LocalWorker(Worker):
        async def run(self, request: WorkerRequest) -> WorkerResult:
            return WorkerResult(status="success", summary="done")

    engine = create_engine_from_url(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)

    # 1. Create a prior task with delivery key under authoritative registry
    normal_service = execution_module.TaskExecutionService(
        session_factory=session_factory,
        worker=_LocalWorker(),
    )
    delivery_key = execution_module.DeliveryKey(
        channel="webhook:test", delivery_id="delivery-dup-1"
    )
    prior_outcome = normal_service.create_task_outcome(
        TaskSubmission(
            task_text="Prior task",
            session=execution_module.SubmissionSession(channel="webhook:test"),
        ),
        delivery_key=delivery_key,
    )
    assert not prior_outcome.duplicate

    # 2. Re-submit under an empty registry with unregistered secret_refs and the same delivery key
    empty_service = execution_module.TaskExecutionService(
        session_factory=session_factory,
        worker=_LocalWorker(),
        secret_registry=SecretRegistry(),
    )
    dup_submission = TaskSubmission(
        task_text="Duplicate delivery attempt with unregistered secret ref",
        session=execution_module.SubmissionSession(channel="webhook:test"),
        secret_refs=(SecretRef(name="github_token"),),
    )

    # Must fail closed on validation before duplicate retrieval can happen
    with pytest.raises(
        TaskSubmissionValidationError,
        match="is not registered in authoritative SecretRegistry",
    ):
        empty_service.create_task_outcome(dup_submission, delivery_key=delivery_key)


def test_ingress_rejects_raw_secrets_with_zero_leakage_in_logs_and_traces(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Verify raw secrets are rejected without leaking into logs or trace spans."""
    service = _StubTaskService()
    tasks_canary = "tasks-trace-log-canary-secret-1122"
    webhook_canary = "webhook-trace-log-canary-secret-3344"

    with (
        patch("apps.api.routes.tasks.set_span_input_output") as mock_tasks_span,
        patch("apps.api.routes.webhook.set_span_input_output") as mock_webhook_span,
        _client(service) as client,
    ):
        resp1 = client.post(
            "/tasks",
            json={"task_text": "Tasks test", "secrets": {"API_KEY": tasks_canary}},
        )
        resp2 = client.post(
            "/webhook",
            json={"task_text": "Webhook test", "secrets": {"API_KEY": webhook_canary}},
        )

    assert resp1.status_code == 422
    assert resp2.status_code == 422
    assert tasks_canary not in resp1.text and webhook_canary not in resp2.text
    assert tasks_canary not in caplog.text and webhook_canary not in caplog.text
    assert mock_tasks_span.call_count == 0
    assert mock_webhook_span.call_count == 0
    assert service.create_calls == []


def test_custom_secret_definitions_json_parsing() -> None:
    """Verify JSON custom secret definition parsing with policies, hosts, and mounts."""
    import json

    from sandbox.secrets import SecretExposurePolicy

    json_payload = json.dumps(
        [
            {
                "name": "json_sandbox_secret",
                "permitted_egress_hosts": ["api.vendor.com"],
                "exposure_policy": "sandbox_env",
                "destination_env_var": "CODE_AGENT_SECRET_VENDOR_API_KEY",
            },
            {
                "name": "json_file_secret",
                "permitted_egress_hosts": ["api.vendor.com"],
                "exposure_policy": "sandbox_file",
                "destination_mount_path": "/run/secrets/code-agent/vendor_secret",
            },
            {
                "name": "json_broker_secret",
                "source_key": "CUSTOM_BROKER_KEY",
            },
            "invalid_non_dict_entry",
        ]
    )
    registry = create_authoritative_secret_registry({"CODE_AGENT_REGISTERED_SECRETS": json_payload})

    sandbox_def = registry.require("json_sandbox_secret")
    assert sandbox_def.exposure_policy == SecretExposurePolicy.SANDBOX_ENV
    assert sandbox_def.permitted_egress_hosts == ("api.vendor.com",)
    assert sandbox_def.destination_env_var == "CODE_AGENT_SECRET_VENDOR_API_KEY"
    assert sandbox_def.destination_mount_path is None

    file_def = registry.require("json_file_secret")
    assert file_def.exposure_policy == SecretExposurePolicy.SANDBOX_FILE
    assert file_def.destination_mount_path == "/run/secrets/code-agent/vendor_secret"

    broker_def = registry.require("json_broker_secret")
    assert broker_def.exposure_policy == SecretExposurePolicy.BROKER_ONLY
    assert broker_def.source_key == "CUSTOM_BROKER_KEY"

    # Malformed JSON falls back safely
    bad_registry = create_authoritative_secret_registry(
        {"CODE_AGENT_REGISTERED_SECRETS": "{not_valid_json"}
    )
    assert "json_sandbox_secret" not in bad_registry


def test_custom_secret_definitions_inline_delimited_parsing() -> None:
    """Verify inline host and broker_only spec parsing in delimited registered secrets."""
    from sandbox.secrets import SecretExposurePolicy

    registry = create_authoritative_secret_registry(
        {
            "CODE_AGENT_REGISTERED_SECRETS": (
                "inline_token:vendor.api.com;other.com, "
                "broker_only_token:broker_only, "
                "plain_token"
            ),
            "CODE_AGENT_SECRET_PLAIN_TOKEN_HOSTS": "plain.api.com",
            "CODE_AGENT_SECRET_PLAIN_TOKEN_POLICY": "sandbox_env",
        }
    )

    inline_def = registry.require("inline_token")
    assert inline_def.exposure_policy == SecretExposurePolicy.SANDBOX_ENV
    assert "vendor.api.com" in inline_def.permitted_egress_hosts
    assert "other.com" in inline_def.permitted_egress_hosts

    broker_def = registry.require("broker_only_token")
    assert broker_def.exposure_policy == SecretExposurePolicy.BROKER_ONLY

    plain_def = registry.require("plain_token")
    assert plain_def.exposure_policy == SecretExposurePolicy.SANDBOX_ENV
    assert "plain.api.com" in plain_def.permitted_egress_hosts
