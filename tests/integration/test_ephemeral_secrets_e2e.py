"""Integration tests for secret reference resolution across process boundaries."""

import base64
import os
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from apps.api.auth import ApiAuthConfig
from apps.api.main import create_app
from db.enums import WorkerRuntimeMode
from orchestrator.execution import TaskExecutionService
from repositories import TaskRepository, session_scope
from sandbox import WorkspaceCleanupPolicy, WorkspaceHandle
from sandbox.capability import CapabilityGrantFactory, CapabilityViolationError
from sandbox.provider_hosts import CODEX_API_KEY_HOSTS
from sandbox.secrets import (
    RegisteredSecretDefinition,
    SecretExposurePolicy,
    SecretRef,
    SecretRegistry,
    SecretResolver,
    SecretScope,
    SecretSource,
    create_authoritative_secret_registry,
)
from tests.integration.task_endpoints_support import DEFAULT_SHARED_SECRET
from workers import CodexCliWorker, WorkerRequest
from workers.cli_runtime import CliRuntimeSettings

os.environ["CODE_AGENT_ENCRYPTION_KEY"] = base64.urlsafe_b64encode(os.urandom(32)).decode()


def test_ephemeral_secret_resolution_across_boundaries(session_factory):
    """Test secret resolution lifecycle from API ingress through native worker resolution."""
    with session_factory() as session:
        session.connection().exec_driver_sql("PRAGMA foreign_keys = ON")

    # 1. Authoritative registry shared between API and Worker runtimes
    shared_registry = SecretRegistry()
    shared_registry.register(
        RegisteredSecretDefinition(
            name="test_api_secret",
            source=SecretSource.ENV,
            source_key="TEST_API_KEY",
            required_scope=SecretScope.PROVIDER_AUTH,
            exposure_policy=SecretExposurePolicy.SANDBOX_ENV,
            destination_env_var="CODE_AGENT_SECRET_TEST",
        )
    )

    app = create_app(
        task_service=TaskExecutionService(
            session_factory=session_factory,
            worker=MagicMock(),
            secret_registry=shared_registry,
        ),
        auth_config=ApiAuthConfig(shared_secret=DEFAULT_SHARED_SECRET),
    )

    with TestClient(app) as client:
        client.headers["X-Webhook-Token"] = DEFAULT_SHARED_SECRET

        # 2. Raw secrets ingress is rejected fail-closed with zero leakage
        raw_payload = {
            "task_text": "Test raw secret rejection",
            "secrets": {"test_api_secret": "raw_sensitive_credential_123"},
        }
        raw_resp = client.post("/tasks", json=raw_payload)
        assert raw_resp.status_code == 422
        assert raw_resp.json()["detail"]["code"] == "DeprecatedLegacySecretsError"
        assert "raw_sensitive_credential_123" not in raw_resp.text

        # 3. Valid registered secret_refs submission succeeds
        valid_payload = {
            "task_text": "Test secret resolution",
            "secret_refs": [{"name": "test_api_secret"}],
        }
        response = client.post("/tasks", json=valid_payload)
        assert response.status_code == 202, response.text
        task_id = response.json()["task_id"]

        # 4. Verify persisted task has empty secrets and registered reference
        with session_scope(session_factory) as session:
            task = TaskRepository(session).get(task_id)
        assert task is not None
        assert task.secrets == {}
        assert len(task.secret_refs) == 1
        assert task.secret_refs[0]["name"] == "test_api_secret"

    # 5. Verify the worker can construct a CapabilityGrant and resolve the secret
    grant_factory = CapabilityGrantFactory(shared_registry)
    grant = grant_factory.create_grant(
        allowed_secret_refs=[SecretRef(name="test_api_secret")],
        granted_secret_scopes=frozenset([SecretScope.PROVIDER_AUTH]),
    )

    resolver = SecretResolver(shared_registry, env={"TEST_API_KEY": "raw_sensitive_credential_123"})
    resolved = resolver.resolve_for_sandbox(SecretRef(name="test_api_secret"), grant)
    assert resolved.reveal_secret_value() == "raw_sensitive_credential_123"
    assert resolved.destination_env_var == "CODE_AGENT_SECRET_TEST"

    # 6. Unregistered secret reference fails closed
    with pytest.raises(
        CapabilityViolationError, match="not registered in authoritative SecretRegistry"
    ):
        grant_factory.create_grant(
            allowed_secret_refs=[SecretRef(name="unregistered_secret")],
            granted_secret_scopes=frozenset([SecretScope.PROVIDER_AUTH]),
        )


def test_native_worker_resolves_submitted_registered_secret_reference(session_factory, tmp_path):
    """Verify submitted registered secret reference is resolved by native worker without leakage."""
    secret_env = {"OPENAI_API_KEY": "sk-test-authoritative-canary-456"}
    authoritative_registry = create_authoritative_secret_registry(secret_env)

    worker = CodexCliWorker(
        runtime_adapter=MagicMock(),
        secret_registry=authoritative_registry,
        secret_env=secret_env,
    )
    task_service = TaskExecutionService(
        session_factory=session_factory,
        worker=worker,
        secret_registry=authoritative_registry,
    )
    app = create_app(
        task_service=task_service,
        auth_config=ApiAuthConfig(shared_secret=DEFAULT_SHARED_SECRET),
    )

    with TestClient(app) as client:
        client.headers["X-Webhook-Token"] = DEFAULT_SHARED_SECRET
        response = client.post(
            "/tasks",
            json={
                "task_text": "Run agent with registered openai_api_key",
                "secret_refs": [{"name": "openai_api_key"}],
            },
        )
        assert response.status_code == 202
        task_id = response.json()["task_id"]

    with session_scope(session_factory) as session:
        task = TaskRepository(session).get(task_id)
    assert task is not None
    assert task.secrets == {}
    assert len(task.secret_refs) == 1
    assert task.secret_refs[0]["name"] == "openai_api_key"

    worker_request = WorkerRequest(
        task_id=task.id,
        session_id=task.session_id,
        task_text=task.task_text,
        repo_url="https://github.com/example/repo",
        branch="main",
        secret_refs=[SecretRef(name=ref["name"]) for ref in task.secret_refs],
        secrets={},
    )
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    workspace = WorkspaceHandle(
        workspace_id=task.id,
        task_id=task.id,
        workspace_path=repo_dir,
        repo_path=repo_dir,
        repo_url="https://github.com/example/repo",
        branch="main",
        cleanup_policy=WorkspaceCleanupPolicy(delete_on_success=False, retain_on_failure=True),
    )
    run_request, _ = worker._prepare_native_agent_run_request(
        request=worker_request,
        workspace=workspace,
        runtime_settings=CliRuntimeSettings(worker_timeout_seconds=30),
        runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
        system_prompt_override=None,
    )

    assert run_request.context is not None
    context = run_request.context
    assert context.grant.allowed_egress_hosts == CODEX_API_KEY_HOSTS
    assert "openai_api_key" in context.grant.allowed_secret_refs

    resolved = context.secret_resolver.resolve_for_sandbox(
        SecretRef(name="openai_api_key"), context.grant
    )
    assert resolved.reveal_secret_value() == "sk-test-authoritative-canary-456"
    assert resolved.destination_env_var == "OPENAI_API_KEY"
    assert "sk-test-authoritative-canary-456" not in str(task.secrets)


def test_custom_registered_secret_submission_and_persistence(session_factory):
    """Verify custom registered secrets are accepted at submission and stored safely."""
    secret_env = {
        "OPENAI_API_KEY": "sk-test-key-12345",
        "CODE_AGENT_REGISTERED_SECRETS": "custom_token,broker_token",
        "CODE_AGENT_SECRET_CUSTOM_TOKEN_HOSTS": "api.openai.com,auth.openai.com",
        "CUSTOM_TOKEN": "custom-canary-secret-8888",
        "BROKER_TOKEN": "broker-canary-secret-9999",
    }
    authoritative_registry = create_authoritative_secret_registry(secret_env)
    worker = CodexCliWorker(
        runtime_adapter=MagicMock(),
        secret_registry=authoritative_registry,
        secret_env=secret_env,
    )
    task_service = TaskExecutionService(
        session_factory=session_factory,
        worker=worker,
        secret_registry=authoritative_registry,
    )
    app = create_app(
        task_service=task_service,
        auth_config=ApiAuthConfig(shared_secret=DEFAULT_SHARED_SECRET),
    )

    with TestClient(app) as client:
        client.headers["X-Webhook-Token"] = DEFAULT_SHARED_SECRET
        response = client.post(
            "/tasks",
            json={
                "task_text": "Run agent with custom registered secret",
                "secret_refs": [
                    {"name": "openai_api_key"},
                    {"name": "custom_token"},
                    {"name": "broker_token"},
                ],
            },
        )
        assert response.status_code == 202
        task_id = response.json()["task_id"]

    with session_scope(session_factory) as session:
        task = TaskRepository(session).get(task_id)
    assert task is not None
    assert task.secrets == {}
    assert len(task.secret_refs) == 3
    assert "custom-canary-secret-8888" not in str(task.secrets)


def test_custom_registered_secret_native_worker_capability_grant(tmp_path):
    """Verify custom registered secret with configured audience succeeds in native worker."""
    secret_env = {
        "OPENAI_API_KEY": "sk-test-key-12345",
        "CODE_AGENT_REGISTERED_SECRETS": "custom_token,broker_token",
        "CODE_AGENT_SECRET_CUSTOM_TOKEN_HOSTS": "api.openai.com,auth.openai.com",
        "CUSTOM_TOKEN": "custom-canary-secret-8888",
        "BROKER_TOKEN": "broker-canary-secret-9999",
    }
    authoritative_registry = create_authoritative_secret_registry(secret_env)

    # Verify custom definitions
    custom_def = authoritative_registry.require("custom_token")
    assert custom_def.exposure_policy == SecretExposurePolicy.SANDBOX_ENV
    assert "api.openai.com" in custom_def.permitted_egress_hosts
    assert custom_def.destination_env_var == "CODE_AGENT_SECRET_CUSTOM_TOKEN"

    broker_def = authoritative_registry.require("broker_token")
    assert broker_def.exposure_policy == SecretExposurePolicy.BROKER_ONLY

    worker = CodexCliWorker(
        runtime_adapter=MagicMock(),
        secret_registry=authoritative_registry,
        secret_env=secret_env,
    )
    worker_request = WorkerRequest(
        task_id="task-custom-grant",
        session_id="session-custom-grant",
        task_text="Run agent with custom registered secret",
        repo_url="https://github.com/example/repo",
        branch="main",
        secret_refs=[
            SecretRef(name="openai_api_key"),
            SecretRef(name="custom_token"),
            SecretRef(name="broker_token"),
        ],
        secrets={},
    )
    repo_dir = tmp_path / "custom_repo"
    repo_dir.mkdir()
    workspace = WorkspaceHandle(
        workspace_id="task-custom-grant",
        task_id="task-custom-grant",
        workspace_path=repo_dir,
        repo_path=repo_dir,
        repo_url="https://github.com/example/repo",
        branch="main",
        cleanup_policy=WorkspaceCleanupPolicy(delete_on_success=False, retain_on_failure=True),
    )
    run_request, _ = worker._prepare_native_agent_run_request(
        request=worker_request,
        workspace=workspace,
        runtime_settings=CliRuntimeSettings(worker_timeout_seconds=30),
        runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
        system_prompt_override=None,
    )

    context = run_request.context
    assert context is not None
    assert context.grant.allowed_egress_hosts == CODEX_API_KEY_HOSTS
    assert "custom_token" in context.grant.allowed_secret_refs
    assert "broker_token" not in context.grant.allowed_secret_refs

    resolved = context.secret_resolver.resolve_for_sandbox(
        SecretRef(name="custom_token"), context.grant
    )
    assert resolved.reveal_secret_value() == "custom-canary-secret-8888"
    assert resolved.destination_env_var == "CODE_AGENT_SECRET_CUSTOM_TOKEN"


def test_custom_registered_secret_audience_mismatch_fails_grant(tmp_path):
    """Verify custom sandbox secret with incompatible audience fails grant construction."""
    secret_env = {
        "OPENAI_API_KEY": "sk-test-key-12345",
        "CODE_AGENT_REGISTERED_SECRETS": "restricted_token",
        "CODE_AGENT_SECRET_RESTRICTED_TOKEN_HOSTS": "restricted.internal.net",
        "RESTRICTED_TOKEN": "restricted-secret-value",
    }
    authoritative_registry = create_authoritative_secret_registry(secret_env)
    worker = CodexCliWorker(
        runtime_adapter=MagicMock(),
        secret_registry=authoritative_registry,
        secret_env=secret_env,
    )
    worker_request = WorkerRequest(
        task_id="task-mismatch",
        task_text="Run with audience mismatch",
        repo_url="https://github.com/example/repo",
        branch="main",
        secret_refs=[SecretRef(name="openai_api_key"), SecretRef(name="restricted_token")],
        secrets={},
    )
    repo_dir = tmp_path / "mismatch_repo"
    repo_dir.mkdir()
    workspace = WorkspaceHandle(
        workspace_id="task-mismatch",
        task_id="task-mismatch",
        workspace_path=repo_dir,
        repo_path=repo_dir,
        repo_url="https://github.com/example/repo",
        branch="main",
        cleanup_policy=WorkspaceCleanupPolicy(delete_on_success=False, retain_on_failure=True),
    )
    with pytest.raises(CapabilityViolationError, match="exceeds sandbox secret audience"):
        worker._prepare_native_agent_run_request(
            request=worker_request,
            workspace=workspace,
            runtime_settings=CliRuntimeSettings(worker_timeout_seconds=30),
            runtime_mode=WorkerRuntimeMode.NATIVE_AGENT,
            system_prompt_override=None,
        )
