"""Integration tests for ephemeral secrets across process boundaries."""

import base64
import os
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from apps.api.auth import ApiAuthConfig
from apps.api.main import create_app
from orchestrator.execution import TaskExecutionService
from sandbox.ephemeral_store_postgres import SessionFactoryEphemeralSecretStore
from sandbox.secrets import (
    RegisteredSecretDefinition,
    SecretExposurePolicy,
    SecretRef,
    SecretRegistry,
    SecretScope,
)
from tests.integration.task_endpoints_support import DEFAULT_SHARED_SECRET

os.environ["CODE_AGENT_ENCRYPTION_KEY"] = base64.urlsafe_b64encode(os.urandom(32)).decode()


def test_ephemeral_secret_resolution_across_boundaries(session_factory):
    """Test ephemeral secret lifecycle from API ingress through native worker resolution."""
    with session_factory() as session:
        session.connection().exec_driver_sql("PRAGMA foreign_keys = ON")

    # 1. Initialize API (submission service) and Worker service boundaries independently
    ephemeral_store = SessionFactoryEphemeralSecretStore(session_factory)

    # API Boundary Setup
    # The client fixture already has the API app set up with a TaskExecutionService
    # We just need to make sure the TaskExecutionService uses our ephemeral_store

    app = create_app(
        task_service=TaskExecutionService(
            session_factory=session_factory,
            worker=MagicMock(),  # The API doesn't run the worker directly here
        ),
        auth_config=ApiAuthConfig(shared_secret=DEFAULT_SHARED_SECRET),
    )

    # Worker Boundary Setup
    # (Removed unused worker instantiation)

    # 2. Register an expected mock provider secret in a clean registry
    worker_registry = SecretRegistry(ephemeral_store=ephemeral_store)
    worker_registry.register(
        RegisteredSecretDefinition(
            name="test_api_secret",
            source="ephemeral",
            source_key="test_api_secret",
            required_scope=SecretScope.PROVIDER_AUTH,
            exposure_policy=SecretExposurePolicy.SANDBOX_ENV,
            destination_env_var="CODE_AGENT_SECRET_TEST",
        )
    )

    # 3. Submit a payload containing a raw provider credential
    with TestClient(app) as client:
        client.headers["X-Webhook-Token"] = DEFAULT_SHARED_SECRET

        payload = {
            "task_text": "Test secret resolution",
            "secrets": {"test_api_secret": "raw_sensitive_credential_123"},
        }

        response = client.post("/tasks", json=payload)
        assert response.status_code == 202, response.text
        task_id = response.json()["task_id"]

        # 4. Verify API extracted raw credential into Postgres and payload has ephem_* reference
        from repositories import TaskRepository, session_scope

        with session_scope(session_factory) as session:
            task = TaskRepository(session).get(task_id)
        assert task is not None
        # The secret should now be an ephemeral reference starting with ephem_
        ephem_ref_obj = next((r for r in task.secret_refs if r["name"].startswith("ephem_")), None)
        assert ephem_ref_obj is not None
        ephem_ref = ephem_ref_obj["name"]

        # Verify it exists in Postgres under this task ID
        assert ephemeral_store.has(ephem_ref, task_id=task_id)

    # 5. Verify the worker can construct a CapabilityGrant and resolve the secret
    # In the actual worker execution, it passes the task_id into SecretRegistry
    task_registry = SecretRegistry(ephemeral_store=ephemeral_store, task_id=task_id)
    task_registry.register(
        RegisteredSecretDefinition(
            name="test_api_secret",
            source="ephemeral",
            source_key="test_api_secret",
            required_scope=SecretScope.PROVIDER_AUTH,
            exposure_policy=SecretExposurePolicy.SANDBOX_ENV,
            destination_env_var="CODE_AGENT_SECRET_TEST",
        )
    )

    from sandbox.capability import CapabilityGrantFactory

    grant_factory = CapabilityGrantFactory(task_registry)

    # This should succeed without raising SecretNotFoundError because task_id is correctly bound
    grant = grant_factory.create_grant(
        allowed_secret_refs=[SecretRef(name=ephem_ref)],
        granted_secret_scopes=frozenset([SecretScope.PROVIDER_AUTH, SecretScope.CUSTOM]),
    )

    # Resolve the secret to prove we can read the plaintext
    from sandbox.secrets import SecretResolver

    resolver = SecretResolver(task_registry, ephemeral_store=ephemeral_store, task_id=task_id)
    resolved = resolver.resolve_for_sandbox(SecretRef(name=ephem_ref), grant)
    assert resolved.reveal_secret_value() == "raw_sensitive_credential_123"

    # 6. Assert attempting to read the same ephemeral secret with a different task_id blocks access
    other_task_registry = SecretRegistry(
        ephemeral_store=ephemeral_store, task_id="different-task-id"
    )
    other_task_registry.register(
        RegisteredSecretDefinition(
            name="test_api_secret",
            source="ephemeral",
            source_key="test_api_secret",
            required_scope=SecretScope.PROVIDER_AUTH,
            exposure_policy=SecretExposurePolicy.SANDBOX_ENV,
            destination_env_var="CODE_AGENT_SECRET_TEST",
        )
    )
    other_grant_factory = CapabilityGrantFactory(other_task_registry)

    from sandbox.capability import CapabilityViolationError

    with pytest.raises(
        CapabilityViolationError, match="not registered in authoritative SecretRegistry"
    ):
        other_grant_factory.create_grant(
            allowed_secret_refs=[SecretRef(name=ephem_ref)],
            granted_secret_scopes=frozenset([SecretScope.PROVIDER_AUTH]),
        )
