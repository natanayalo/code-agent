"""Integration tests for secret reference resolution across process boundaries."""

import base64
import os
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from apps.api.auth import ApiAuthConfig
from apps.api.main import create_app
from orchestrator.execution import TaskExecutionService
from repositories import TaskRepository, session_scope
from sandbox.capability import CapabilityGrantFactory, CapabilityViolationError
from sandbox.secrets import (
    RegisteredSecretDefinition,
    SecretExposurePolicy,
    SecretRef,
    SecretRegistry,
    SecretResolver,
    SecretScope,
    SecretSource,
)
from tests.integration.task_endpoints_support import DEFAULT_SHARED_SECRET

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
